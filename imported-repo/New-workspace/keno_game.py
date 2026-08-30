"""Independent native-Telegram Keno implementation for Rollers Casino.

This module intentionally does not share the legacy Mines/Keno handlers,
callbacks, selection dictionaries, or payout code.  It only receives the
casino's existing balance and persistence primitives through ``configure``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from styled_buttons import StyledInlineKeyboardButton

KENO_CALLBACK_PREFIX = "nr:"
KENO_NUMBERS = 40
KENO_DRAW_COUNT = 10
KENO_SETUP_TIMEOUT_SECONDS = 15 * 60
# The top tier is intentionally capped at the multiplier shown to players.
# A cap is also a bankroll-safety limit: the house edge is created by the
# published RTP below, never by changing an already-generated draw.
KENO_MAX_PAYOUT_MULTIPLIER = 1_000.0
KENO_HOUSE_EDGE = 0.05
KENO_TARGET_RTP = 1.0 - KENO_HOUSE_EDGE
# Telegram rejects a zero-width space as an empty message when editing text.
# WORD JOINER remains visually invisible while satisfying the non-empty text
# requirement during the number-by-number reveal.
KENO_REVEAL_TEXT = "\u2060"


@dataclass(frozen=True)
class KenoMode:
    """A configurable risk profile used to generate the payout table."""

    key: str
    label: str
    base_multiplier: float
    house_edge: float
    minimum_hit_fraction: float
    odds_exponent: float
    target_rtp: float


# The base multipliers are anchors, not a hardcoded payout table.  The actual
# multiplier for each spot/hit pair is generated from the hypergeometric odds,
# then normalized to the configured RTP for that mode.  Every mode uses the
# same published 5% long-run house edge; high-risk modes only change which hit
# counts qualify and how sharply the payouts rise.
KENO_CONFIG: dict[str, Any] = {
    "number_count": KENO_NUMBERS,
    "draw_count": KENO_DRAW_COUNT,
    "minimum_selection": 10,
    "maximum_selection": 10,
    "reveal_delay_seconds": 0.65,
    "maximum_payout_multiplier": KENO_MAX_PAYOUT_MULTIPLIER,
    "modes": {
        "classic": KenoMode(
            "classic", "Classic", 3.00, KENO_HOUSE_EDGE, 0.50, 1.00, KENO_TARGET_RTP
        ),
        "easy": KenoMode(
            "easy", "Easy", 1.10, KENO_HOUSE_EDGE, 0.20, 0.86, KENO_TARGET_RTP
        ),
        "medium": KenoMode(
            "medium", "Medium", 1.60, KENO_HOUSE_EDGE, 0.35, 0.96, KENO_TARGET_RTP
        ),
        "hard": KenoMode(
            "hard", "Hard", 4.00, KENO_HOUSE_EDGE, 0.60, 1.08, KENO_TARGET_RTP
        ),
    },
}


@dataclass
class KenoServices:
    """Existing casino infrastructure needed by the independent game."""

    get_balance: Callable[[str], float]
    get_currency: Callable[[str], str]
    format_balance: Callable[[float, str], str]
    convert_to_usd: Callable[[float, str], float]
    convert_from_usd: Callable[[float, str], float]
    minimum_bet: Callable[[], float]
    maximum_bet: Callable[[], float]
    deduct_balance: Callable[[str, float, str], bool]
    credit_balance: Callable[[str, float, str], float]
    track_wagering: Callable[[str, float, float], None]
    adjust_house: Callable[[float], None]
    add_loss_to_rakeback: Callable[[str, float], None]
    add_match_history: Callable[[str, str, float, str, float], None]
    save: Callable[[], None]
    logger: logging.Logger
    show_main_menu: Callable[[Any, Any], Any] | None = None


_services: KenoServices | None = None
_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.RLock()
_reveal_tasks: dict[str, asyncio.Task] = {}
_payout_table_cache: dict[tuple[str, int], dict[int, float]] = {}


def _clean_numbers(values: Any, maximum_items: int) -> list[int]:
    """Return unique, in-range numbers suitable for a persisted game state."""
    if not isinstance(values, (list, tuple)):
        return []
    result: list[int] = []
    number_count = int(KENO_CONFIG["number_count"])
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= number_count and number not in result:
            result.append(number)
        if len(result) >= maximum_items:
            break
    return result


def configure(services: KenoServices) -> None:
    """Connect Keno to existing wallet, audit, and persistence primitives."""
    global _services
    _services = services


def _svc() -> KenoServices:
    if _services is None:
        raise RuntimeError("Keno services have not been configured")
    return _services


def _mode(mode_key: str) -> KenoMode:
    modes = KENO_CONFIG["modes"]
    return modes.get(mode_key, modes["medium"])


def _combination(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def hit_probability(spots: int, hits: int) -> float:
    """Probability of exactly ``hits`` matches in a 10-of-40 Keno draw."""
    number_count = int(KENO_CONFIG["number_count"])
    draw_count = int(KENO_CONFIG["draw_count"])
    if spots < 0 or spots > number_count or hits < 0:
        return 0.0
    if hits > spots or hits > draw_count:
        return 0.0
    numerator = _combination(spots, hits) * _combination(
        number_count - spots, draw_count - hits
    )
    denominator = _combination(number_count, draw_count)
    return numerator / denominator if denominator else 0.0


def _minimum_paying_hits(spots: int, profile: KenoMode) -> int:
    if spots <= 0:
        return 1
    return max(1, min(spots, math.ceil(spots * profile.minimum_hit_fraction)))


def build_payout_table(spots: int, mode_key: str) -> dict[int, float]:
    """Generate a deterministic, odds-based payout table.

    Keno outcomes are exact hit counts from a hypergeometric distribution.
    Positive payouts are assigned to the mode's qualifying hit band.  Each
    payout is shaped by inverse odds, the mode's risk curve, and its configured
    base anchor, then normalized so the positive outcomes target the mode's
    configured RTP.  The result is not an outcome manipulation mechanism:
    the draw is independent and the table is deterministic for the state.
    """
    spots = int(spots)
    mode_key = mode_key if mode_key in KENO_CONFIG["modes"] else "medium"
    cache_key = (mode_key, spots)
    if cache_key in _payout_table_cache:
        return dict(_payout_table_cache[cache_key])
    if not (
        int(KENO_CONFIG["minimum_selection"])
        <= spots
        <= int(KENO_CONFIG["maximum_selection"])
    ):
        return {0: 0.0}

    profile = _mode(mode_key)
    first_paid_hit = _minimum_paying_hits(spots, profile)
    paid_hits = [
        hits
        for hits in range(first_paid_hit, spots + 1)
        if hit_probability(spots, hits) > 0
    ]
    if not paid_hits:
        return {0: 0.0}

    # The fair inverse-odds value is the mathematical foundation.  The
    # additive base anchor keeps the configured mode visible in the curve,
    # while normalization applies the house edge/RTP consistently.
    raw_values: dict[int, float] = {}
    for hits in paid_hits:
        probability = hit_probability(spots, hits)
        fair_multiplier = 1.0 / probability
        raw_values[hits] = profile.base_multiplier + (
            max(0.0, fair_multiplier - 1.0) ** profile.odds_exponent
        )

    maximum = float(KENO_CONFIG["maximum_payout_multiplier"])

    def rounded_table(current_scale: float) -> dict[int, float]:
        """Round/cap payouts without introducing a payout floor."""
        return {
            hits: (
                round(min(maximum, max(0.0, raw_values[hits] * current_scale)), 2)
                if hits in raw_values
                else 0.0
            )
            for hits in range(0, spots + 1)
        }

    def expected_rtp(table_to_measure: dict[int, float]) -> float:
        return sum(
            hit_probability(spots, hits) * value
            for hits, value in table_to_measure.items()
        )

    # Solve for the largest deterministic scale whose rounded and capped
    # schedule still stays at or below the target RTP.  A one-way scale-down
    # is not sufficient here: adding the 1,000x cap can otherwise leave the
    # house with a much larger edge than the mode advertises.
    low = 0.0
    high = 1.0
    while expected_rtp(rounded_table(high)) < profile.target_rtp and high < 1e12:
        high *= 2.0
    for _ in range(80):
        scale = (low + high) / 2.0
        if expected_rtp(rounded_table(scale)) <= profile.target_rtp:
            low = scale
        else:
            high = scale
    table = rounded_table(low)

    # Rounding/capping can slightly change RTP, but never allows a negative
    # payout or a non-winning state to pay.
    _payout_table_cache[cache_key] = dict(table)
    return dict(table)


def payout_multiplier(spots: int, hits: int, mode_key: str) -> float:
    """Return the deterministic multiplier for a final exact hit count."""
    return float(build_payout_table(spots, mode_key).get(int(hits), 0.0))


def generate_draw() -> list[int]:
    """Generate a server-side 10-number draw without replacement."""
    rng = secrets.SystemRandom()
    return rng.sample(range(1, int(KENO_CONFIG["number_count"]) + 1), int(KENO_CONFIG["draw_count"]))


def export_state() -> dict[str, dict[str, Any]]:
    """Return JSON-safe Keno sessions for the casino save file."""
    with _sessions_lock:
        return {
            session_id: {
                key: value
                for key, value in session.items()
                if isinstance(value, (str, int, float, bool, list, dict, type(None)))
            }
            for session_id, session in _sessions.items()
        }


def restore_state(data: Any) -> None:
    """Restore only valid independent Keno session records."""
    if not isinstance(data, dict):
        return
    with _sessions_lock:
        _sessions.clear()
        for session_id, session in data.items():
            if not isinstance(session_id, str) or not isinstance(session, dict):
                continue
            if session.get("game") != "keno":
                continue
            session_copy = dict(session)
            status = session_copy.get("status")
            if status not in {"setup", "betting", "revealing", "settling", "finished"}:
                continue
            session_copy["selected_numbers"] = _clean_numbers(
                session_copy.get("selected_numbers", []),
                int(KENO_CONFIG["maximum_selection"]),
            )
            session_copy["draw_numbers"] = _clean_numbers(
                session_copy.get("draw_numbers", []),
                int(KENO_CONFIG["draw_count"]),
            )
            session_copy["revealed_numbers"] = [
                number
                for number in _clean_numbers(
                    session_copy.get("revealed_numbers", []),
                    int(KENO_CONFIG["draw_count"]),
                )
                if number in session_copy["draw_numbers"]
            ]
            session_copy["hit_numbers"] = [
                number
                for number in _clean_numbers(
                    session_copy.get("hit_numbers", []),
                    int(KENO_CONFIG["maximum_selection"]),
                )
                if number in session_copy["selected_numbers"]
                and number in session_copy["revealed_numbers"]
            ]
            _sessions[session_id] = session_copy


def recover_incomplete_sessions() -> int:
    """Safely refund bets from games interrupted before settlement.

    A setup has not been debited and is simply discarded when stale.  A game
    that was already debited but had not reached settlement is refunded once.
    Settling sessions are retained for audit rather than risking a duplicate
    credit after a process crash.
    """
    service = _svc()
    refunds: list[tuple[str, float]] = []
    now = time.time()
    with _sessions_lock:
        for session_id, session in list(_sessions.items()):
            status = session.get("status")
            created_at = float(session.get("created_at", 0) or 0)
            if status == "setup" and now - created_at > KENO_SETUP_TIMEOUT_SECONDS:
                del _sessions[session_id]
            elif status in {"betting", "revealing"} and session.get("bet_debited"):
                if not session.get("recovery_refunded"):
                    session["recovery_refunded"] = True
                    session["status"] = "finished"
                    session["result"] = "recovered_after_restart"
                    refunds.append((str(session.get("user_id")), float(session.get("bet_amount", 0))))

    for session_id, amount in refunds:
        try:
            if amount > 0:
                service.credit_balance(session_id, amount, "keno_recovery_refund")
        except Exception:
            service.logger.exception("[KENO] Recovery refund failed for user %s", session_id)
    if refunds:
        service.save()
    return len(refunds)


def _active_session_for_user(user_id: str) -> tuple[str, dict[str, Any]] | None:
    now = time.time()
    with _sessions_lock:
        for session_id, session in list(_sessions.items()):
            if (
                session.get("status") == "setup"
                and now - float(session.get("created_at", 0) or 0)
                > KENO_SETUP_TIMEOUT_SECONDS
            ):
                del _sessions[session_id]
                continue
            if (
                str(session.get("user_id")) == str(user_id)
                and session.get("status") in {"setup", "betting", "revealing", "settling"}
            ):
                return session_id, session
    return None


def _new_session(user_id: str, chat_id: int, bet_amount: float) -> tuple[str, dict[str, Any]]:
    session_id = uuid.uuid4().hex[:12]
    session = {
        "game": "keno",
        "session_id": session_id,
        "user_id": str(user_id),
        "chat_id": int(chat_id),
        "message_id": None,
        "mode": "medium",
        "bet_amount": round(float(bet_amount), 8),
        "selected_numbers": [],
        "draw_numbers": [],
        "revealed_numbers": [],
        "hit_numbers": [],
        "current_multiplier": 0.0,
        "potential_win": 0.0,
        "final_payout": 0.0,
        "status": "setup",
        "bet_debited": False,
        "payout_credited": False,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
    }
    with _sessions_lock:
        _sessions[session_id] = session
    return session_id, session


def _get_owned_session(session_id: str, user_id: str) -> dict[str, Any] | None:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if (
            session
            and session.get("status") == "setup"
            and time.time() - float(session.get("created_at", 0) or 0)
            > KENO_SETUP_TIMEOUT_SECONDS
        ):
            _sessions.pop(session_id, None)
            session = None
        if session and str(session.get("user_id")) == str(user_id):
            return session
    return None


async def _answer(query, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await query.answer(text=text, show_alert=show_alert)
    except Exception:
        pass


def _mode_label(mode_key: str) -> str:
    return _mode(mode_key).label


def _format_table(spots: int, mode_key: str) -> str:
    table = build_payout_table(spots, mode_key)
    paid = [f"{hits}: {value:g}x" for hits, value in table.items() if hits and value > 0]
    return " • ".join(paid) if paid else "No winning hit count"


def _number_button(number: int, session: dict[str, Any]):
    selected = number in session.get("selected_numbers", [])
    revealed = number in session.get("revealed_numbers", [])
    hit = number in session.get("hit_numbers", [])
    session_id = session["session_id"]
    callback = f"{KENO_CALLBACK_PREFIX}pick:{session_id}:{number}"
    style = None

    if hit:
        label = str(number)
        style = "success"
        callback = f"{KENO_CALLBACK_PREFIX}noop:{session_id}"
    elif revealed:
        label = str(number)
        style = "danger"
        callback = f"{KENO_CALLBACK_PREFIX}noop:{session_id}"
    elif selected:
        label = str(number)
        style = "primary"
    elif session.get("status") != "setup":
        label = str(number)
        callback = f"{KENO_CALLBACK_PREFIX}noop:{session_id}"
    else:
        label = str(number)
    return StyledInlineKeyboardButton(label, callback_data=callback, style=style)


def _render_number_grid(session: dict[str, Any]) -> list[list[InlineKeyboardButton]]:
    return [
        [
            _number_button(number, session)
            for number in range(start, min(start + 8, KENO_CONFIG["number_count"] + 1))
        ]
        for start in range(1, KENO_CONFIG["number_count"] + 1, 8)
    ]


def _render_mode_picker(session: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    service = _svc()
    user_id = str(session["user_id"])
    currency = service.get_currency(user_id)
    mode_key = session["mode"]
    mode_buttons: list[InlineKeyboardButton] = []
    for key in ("classic", "easy", "medium", "hard"):
        label = _mode(key).label
        button_text = f"✓ {label}" if key == mode_key else label
        mode_buttons.append(
            InlineKeyboardButton(
                button_text,
                callback_data=f"{KENO_CALLBACK_PREFIX}mode:{session['session_id']}:{key}",
            )
        )
    keyboard = [
        mode_buttons[:2],
        mode_buttons[2:],
        [
            InlineKeyboardButton(
                "Open number board",
                callback_data=f"{KENO_CALLBACK_PREFIX}open:{session['session_id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "Back",
                callback_data=f"{KENO_CALLBACK_PREFIX}back:{session['session_id']}",
            )
        ],
    ]
    text = (
        "🎯 <b>KENO</b>\n\n"
        "<blockquote>"
        f"<b>Mode:</b> {_mode_label(mode_key)}\n"
        f"<b>Bet:</b> {service.format_balance(session['bet_amount'], currency)}"
        "</blockquote>\n"
        "Choose a mode."
    )
    return text, InlineKeyboardMarkup(keyboard)


def _render_board(session: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    service = _svc()
    user_id = str(session["user_id"])
    currency = service.get_currency(user_id)
    selected = session.get("selected_numbers", [])
    spots = len(selected)
    mode_key = session["mode"]
    text = (
        "🎯 <b>KENO</b>\n\n"
        "<blockquote>"
        f"<b>Mode:</b> {mode_key.title()}\n"
        f"<b>Bet:</b> {service.format_balance(session['bet_amount'], currency)}\n"
        f"<b>Selected:</b> {spots}/10"
        "</blockquote>\n"
        "Select exactly 10 numbers."
    )
    grid = _render_number_grid(session)
    grid.extend(
        [
            [
                InlineKeyboardButton(
                    "Random Pick",
                    callback_data=f"{KENO_CALLBACK_PREFIX}random:{session['session_id']}",
                ),
                InlineKeyboardButton(
                    "Clear Table",
                    callback_data=f"{KENO_CALLBACK_PREFIX}clear:{session['session_id']}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Bet",
                    callback_data=f"{KENO_CALLBACK_PREFIX}bet:{session['session_id']}",
                )
            ],
            [
                InlineKeyboardButton(
                    "Back",
                    callback_data=f"{KENO_CALLBACK_PREFIX}back:{session['session_id']}",
                )
            ],
        ]
    )
    return text, InlineKeyboardMarkup(grid)


def _render_draw_board(session: dict[str, Any]) -> InlineKeyboardMarkup:
    """Render only the number grid while the draw is animated."""
    return InlineKeyboardMarkup(_render_number_grid(session))


def _render_result(session: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    service = _svc()
    user_id = str(session["user_id"])
    currency = service.get_currency(user_id)
    multiplier = float(session.get("current_multiplier", 0.0))
    multiplier_text = f"{multiplier:g}"
    bet_amount = float(session.get("bet_amount", 0.0))
    payout = float(session.get("final_payout", 0.0))
    hits = len(session.get("hit_numbers", []))
    spots = len(session.get("selected_numbers", []))
    balance = float(service.get_balance(user_id))
    session_id = session["session_id"]
    outcome_label = "WIN" if payout > 0 else "NO WIN"
    board = _render_number_grid(session)
    board.extend(
        [
            [
                InlineKeyboardButton(
                    "Play Again",
                    callback_data=f"{KENO_CALLBACK_PREFIX}change_mode:{session_id}",
                ),
                InlineKeyboardButton(
                    "Double",
                    callback_data=f"{KENO_CALLBACK_PREFIX}double_again:{session_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Back",
                    callback_data=f"{KENO_CALLBACK_PREFIX}back:{session_id}",
                )
            ],
        ]
    )
    return (
        "🎯 <b>KENO</b>\n\n"
        f"<b>OUTCOME ({_mode_label(session['mode']).upper()})</b>\n"
        "<blockquote>"
        f"<b>Bet:</b> {service.format_balance(bet_amount, currency)}\n"
        f"<b>Hits:</b> {hits}/{spots}\n"
        f"<b>Multiplier:</b> {multiplier_text}x\n"
        f"<b>Won:</b> {service.format_balance(payout, currency)}\n"
        f"<b>Balance:</b> {service.format_balance(balance, currency)}"
        "</blockquote>\n"
        f"<b>{outcome_label}</b>",
        InlineKeyboardMarkup(board),
    )


async def _edit(query, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception as exc:
        _svc().logger.warning("[KENO] Could not edit game message: %s", exc)


async def keno_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the independent native Telegram Keno game."""
    if not update.message or not update.message.from_user:
        return
    service = _svc()
    user_id = str(update.message.from_user.id)
    existing = _active_session_for_user(user_id)
    if existing:
        await update.message.reply_text(
            "⚠️ You already have a Keno round open. Finish it or press Back first."
        )
        return

    currency = service.get_currency(user_id)
    balance = float(service.get_balance(user_id))
    bet_amount = float(service.minimum_bet())
    if context.args:
        raw = context.args[0].lower()
        try:
            if raw in {"all", "full"}:
                bet_amount = balance
            elif raw == "half":
                bet_amount = balance / 2
            else:
                bet_amount = float(service.convert_to_usd(float(raw), currency))
        except (TypeError, ValueError):
            await update.message.reply_text("❌ Enter a valid Keno bet, for example: /keno 10")
            return
    bet_amount = round(bet_amount, 8)
    if bet_amount <= 0:
        await update.message.reply_text("❌ Your bet must be greater than zero.")
        return
    if bet_amount < float(service.minimum_bet()):
        await update.message.reply_text(
            f"❌ Minimum bet is {service.format_balance(float(service.minimum_bet()), currency)}"
        )
        return
    if bet_amount > float(service.maximum_bet()):
        await update.message.reply_text(
            f"❌ Maximum bet is {service.format_balance(float(service.maximum_bet()), currency)}"
        )
        return
    if bet_amount > balance:
        await update.message.reply_text(
            f"❌ Insufficient balance for {service.format_balance(bet_amount, currency)}."
        )
        return

    session_id, session = _new_session(user_id, update.message.chat_id, bet_amount)
    text, markup = _render_mode_picker(session)
    sent = await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    with _sessions_lock:
        session["message_id"] = sent.message_id
    service.save()


async def keno_lobby_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open Keno from the game lobby using the minimum valid bet."""
    if not query or not query.from_user or not query.message:
        return
    service = _svc()
    user_id = str(query.from_user.id)
    existing = _active_session_for_user(user_id)
    if existing:
        await _answer(query, "⚠️ You already have a Keno round open.", show_alert=True)
        return

    currency = service.get_currency(user_id)
    balance = float(service.get_balance(user_id))
    bet_amount = float(service.minimum_bet())
    if bet_amount <= 0 or balance < bet_amount:
        await _answer(
            query,
            f"❌ You need at least {service.format_balance(bet_amount, currency)} to play Keno.",
            show_alert=True,
        )
        return

    session_id, session = _new_session(user_id, query.message.chat_id, bet_amount)
    text, markup = _render_mode_picker(session)
    sent = await query.message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )
    with _sessions_lock:
        session["message_id"] = sent.message_id
    service.save()


async def _open_board(query, session: dict[str, Any]) -> None:
    await _answer(query)
    text, markup = _render_board(session)
    await _edit(query, text, markup)


async def _change_bet(query, session: dict[str, Any], factor: float) -> None:
    service = _svc()
    user_id = str(session["user_id"])
    current = float(session["bet_amount"])
    next_amount = current * factor
    next_amount = max(float(service.minimum_bet()), min(float(service.maximum_bet()), next_amount))
    next_amount = min(next_amount, float(service.get_balance(user_id)))
    if next_amount < float(service.minimum_bet()):
        await _answer(query, "❌ Your balance is below the minimum bet.", show_alert=True)
        return
    with _sessions_lock:
        session["bet_amount"] = round(next_amount, 8)
    await _answer(query)
    text, markup = _render_board(session)
    await _edit(query, text, markup)
    service.save()


async def _place_bet(query, context: ContextTypes.DEFAULT_TYPE, session: dict[str, Any]) -> None:
    service = _svc()
    user_id = str(session["user_id"])
    selected = _clean_numbers(
        session.get("selected_numbers", []),
        int(KENO_CONFIG["maximum_selection"]),
    )
    selected.sort()
    max_spots = int(KENO_CONFIG["maximum_selection"])
    if len(selected) != max_spots:
        await _answer(
            query,
            f"❌ Select exactly {max_spots} numbers before betting.",
            show_alert=True,
        )
        return

    with _sessions_lock:
        if session.get("status") != "setup":
            await _answer(query, "⚠️ This Keno round has already started.", show_alert=True)
            return
        session["status"] = "betting"
        session["selected_numbers"] = selected
        bet_amount = float(session["bet_amount"])

    currency = service.get_currency(user_id)
    balance = float(service.get_balance(user_id))
    if bet_amount < float(service.minimum_bet()) or bet_amount > float(service.maximum_bet()) or bet_amount > balance:
        with _sessions_lock:
            session["status"] = "setup"
        await _answer(query, "❌ Bet amount is invalid or your balance is insufficient.", show_alert=True)
        return

    deducted = False
    try:
        deducted = bool(service.deduct_balance(user_id, bet_amount, "keno_bet"))
        if not deducted:
            raise ValueError("insufficient balance")
        draw = generate_draw()
        with _sessions_lock:
            session.update({
                "status": "revealing",
                "bet_debited": True,
                "draw_numbers": draw,
                "revealed_numbers": [],
                "hit_numbers": [],
                "started_at": time.time(),
            })
        service.save()
    except Exception as exc:
        if deducted:
            try:
                service.credit_balance(user_id, bet_amount, "keno_bet_refund")
            except Exception:
                # Keep the debit marked as recoverable if compensation itself
                # fails. Startup recovery will refund it exactly once.
                with _sessions_lock:
                    session.update({
                        "status": "revealing",
                        "bet_debited": True,
                        "recovery_refund_pending": True,
                    })
                try:
                    service.save()
                except Exception:
                    pass
                service.logger.exception(
                    "[KENO] Bet refund failed; marked for recovery for %s",
                    user_id,
                )
        with _sessions_lock:
            if not session.get("recovery_refund_pending"):
                session.update({
                    "status": "setup",
                    "bet_debited": False,
                })
        service.logger.exception("[KENO] Bet placement failed for %s: %s", user_id, exc)
        await _answer(
            query,
            "❌ We could not place that bet. Any debit is being recovered automatically.",
            show_alert=True,
        )
        return

    await _answer(query)
    await _edit(query, KENO_REVEAL_TEXT, _render_draw_board(session))
    task = asyncio.create_task(_reveal_session(context, session["session_id"]))
    _reveal_tasks[session["session_id"]] = task


async def _reveal_session(context: ContextTypes.DEFAULT_TYPE, session_id: str) -> None:
    service = _svc()
    try:
        with _sessions_lock:
            session = _sessions.get(session_id)
            if not session or session.get("status") != "revealing":
                return
            draw = list(session.get("draw_numbers", []))

        for number in draw:
            await asyncio.sleep(float(KENO_CONFIG["reveal_delay_seconds"]))
            with _sessions_lock:
                session = _sessions.get(session_id)
                if not session or session.get("status") != "revealing":
                    return
                session["revealed_numbers"].append(number)
                if number in session["selected_numbers"]:
                    session["hit_numbers"].append(number)
                hits = len(session["hit_numbers"])
                spots = len(session["selected_numbers"])
                session["current_multiplier"] = payout_multiplier(spots, hits, session["mode"])
                session["potential_win"] = round(
                    float(session["bet_amount"]) * session["current_multiplier"], 8
                )
                chat_id = session["chat_id"]
                message_id = session["message_id"]
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=KENO_REVEAL_TEXT,
                    reply_markup=_render_draw_board(session),
                )
            except Exception as exc:
                service.logger.warning("[KENO] Reveal update failed for %s: %s", session_id, exc)

        await _settle_session(context, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        service.logger.exception("[KENO] Reveal task failed for %s: %s", session_id, exc)
    finally:
        _reveal_tasks.pop(session_id, None)


async def _settle_session(context: ContextTypes.DEFAULT_TYPE, session_id: str) -> None:
    service = _svc()
    with _sessions_lock:
        session = _sessions.get(session_id)
        if not session or session.get("status") != "revealing":
            return
        session["status"] = "settling"
        spots = len(session["selected_numbers"])
        hits = len(session["hit_numbers"])
        multiplier = payout_multiplier(spots, hits, session["mode"])
        bet_amount = float(session["bet_amount"])
        payout = round(bet_amount * multiplier, 8)
        session["current_multiplier"] = multiplier
        session["potential_win"] = payout
        session["final_payout"] = payout
        session["finished_at"] = time.time()
    service.save()

    user_id = str(session["user_id"])
    try:
        if payout > 0:
            service.credit_balance(user_id, payout, "keno_payout")
        if payout <= 0:
            service.add_loss_to_rakeback(user_id, bet_amount)
        service.adjust_house(round(bet_amount - payout, 8))
        service.track_wagering(user_id, bet_amount, multiplier)
        result = f"{session['mode']}_{hits}_of_{spots}"
        service.add_match_history(user_id, "keno", bet_amount, result, payout)
        with _sessions_lock:
            session["payout_credited"] = payout > 0
            session["status"] = "finished"
        service.save()
    except Exception as exc:
        # Keep the state as settling so a restart does not blindly issue a
        # second payout.  The balance audit log contains the debit/credit trail.
        service.logger.exception("[KENO] Settlement failed for %s: %s", session_id, exc)
        with _sessions_lock:
            session["status"] = "settling"
        service.save()
        return

    try:
        text, markup = _render_result(session)
        await context.bot.edit_message_text(
            chat_id=session["chat_id"],
            text=text,
            reply_markup=markup,
            message_id=session["message_id"],
            parse_mode=ParseMode.HTML,
        )
        service.save()
    except Exception as exc:
        # The animated board has already finished; settlement must remain
        # recorded even if Telegram temporarily rejects the final edit.
        service.logger.exception("[KENO] Could not update outcome board for %s: %s", session_id, exc)


async def keno_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle only the independent ``nr:`` Keno callback namespace."""
    query = update.callback_query
    if not query or not query.from_user or not query.data:
        return
    data = query.data
    if not data.startswith(KENO_CALLBACK_PREFIX):
        return
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    session_id = parts[2] if len(parts) > 2 else ""
    user_id = str(query.from_user.id)

    if action == "noop":
        await _answer(query, "This button is no longer active.", show_alert=True)
        return

    session = _get_owned_session(session_id, user_id)
    if not session:
        await _answer(query, "⚠️ This Keno round is not yours or has expired.", show_alert=True)
        return

    status = session.get("status")
    if action == "back":
        if status in {"betting", "revealing", "settling"}:
            await _answer(query, "⚠️ You cannot cancel a round after betting.", show_alert=True)
            return
        _reveal_tasks.pop(session_id, None)
        with _sessions_lock:
            _sessions.pop(session_id, None)
        await _answer(query)
        service = _svc()
        if service.show_main_menu:
            await service.show_main_menu(query, context)
        else:
            try:
                await query.message.delete()
            except Exception:
                pass
        service.save()
        return

    if action in {"again", "double_again", "change_mode"}:
        if status != "finished":
            await _answer(query, "⚠️ This round is not finished yet.", show_alert=True)
            return
        bet_amount = float(session["bet_amount"])
        if action == "double_again":
            service = _svc()
            bet_amount = min(
                float(service.maximum_bet()),
                round(bet_amount * 2.0, 8),
            )
        new_id, new_session = _new_session(user_id, session["chat_id"], bet_amount)
        new_session["mode"] = session["mode"]
        new_session["message_id"] = session["message_id"]
        with _sessions_lock:
            _sessions.pop(session_id, None)
        await _answer(query)
        if action == "change_mode":
            text, markup = _render_mode_picker(new_session)
        else:
            text, markup = _render_board(new_session)
        await _edit(query, text, markup)
        _svc().save()
        return

    if status != "setup":
        await _answer(query, "⚠️ This round is already locked.", show_alert=True)
        return

    if action == "mode":
        mode_key = parts[3] if len(parts) > 3 else ""
        if mode_key not in KENO_CONFIG["modes"]:
            await _answer(query, "❌ Invalid Keno mode.", show_alert=True)
            return
        session["mode"] = mode_key
        await _answer(query)
        text, markup = _render_board(session)
        await _edit(query, text, markup)
        _svc().save()
        return

    if action == "open":
        await _open_board(query, session)
        return
    if action == "random":
        selected_count = secrets.SystemRandom().randint(
            int(KENO_CONFIG["minimum_selection"]),
            int(KENO_CONFIG["maximum_selection"]),
        )
        session["selected_numbers"] = sorted(
            secrets.SystemRandom().sample(
                range(1, int(KENO_CONFIG["number_count"]) + 1), selected_count
            )
        )
        await _answer(query)
        text, markup = _render_board(session)
        await _edit(query, text, markup)
        _svc().save()
        return
    if action == "clear":
        session["selected_numbers"] = []
        await _answer(query)
        text, markup = _render_board(session)
        await _edit(query, text, markup)
        _svc().save()
        return
    if action == "pick":
        try:
            number = int(parts[3])
        except (TypeError, ValueError, IndexError):
            await _answer(query, "❌ Invalid number.", show_alert=True)
            return
        if not 1 <= number <= int(KENO_CONFIG["number_count"]):
            await _answer(query, "❌ Invalid number.", show_alert=True)
            return
        selected = session["selected_numbers"]
        if number in selected:
            selected.remove(number)
        elif len(selected) >= int(KENO_CONFIG["maximum_selection"]):
            await _answer(
                query,
                f"❌ You can select exactly {KENO_CONFIG['maximum_selection']} numbers.",
                show_alert=True,
            )
            return
        else:
            selected.append(number)
            selected.sort()
        await _answer(query)
        text, markup = _render_board(session)
        await _edit(query, text, markup)
        _svc().save()
        return
    if action == "bet":
        await _place_bet(query, context, session)
        return

    await _answer(query, "❌ Unknown Keno action.", show_alert=True)