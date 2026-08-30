---
name: Keno Telegram reveal UX
description: The expected Telegram Keno interaction and the stale-board behavior to preserve.
---

Keno's Telegram board uses Bot API button styles: primary for selected numbers, danger for revealed misses, and success for revealed hits. After Bet, the caption and setup controls disappear while only the grid is edited after each draw in sequence; the same message then returns with the outcome caption and action buttons. Setup boards expire after the configured timeout so old boards cannot block a new `/keno` command.

**Why:** The intended experience is an animated one-by-one draw like the supplied reference screenshots; rendering all final states at once, changing the requested reveal layout, or retaining stale setup sessions makes the game look broken and produces confusing ownership alerts.

**How to apply:** On Bet, edit to a zero-width caption with only the styled number grid; await the configured delay between each draw update; then edit that same message with the final caption, colored board, and Play Again / Double / Back actions. Do not replace button styles with emoji markers, and always clean up expired setup sessions before rejecting a new game.

Telegram treats a zero-width space as empty text and rejects message edits. Use a visually invisible word-joiner character for the reveal message instead.