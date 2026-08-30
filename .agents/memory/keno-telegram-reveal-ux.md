---
name: Keno Telegram reveal UX
description: The expected Telegram Keno interaction and the stale-board behavior to preserve.
---

Keno's Telegram board uses Bot API button styles: primary for selected numbers, danger for revealed misses, and success for revealed hits. The reveal must edit the board after each draw in sequence, then send a separate outcome message with action buttons. Setup boards expire after the configured timeout so old boards cannot block a new `/keno` command.

**Why:** The intended experience is an animated one-by-one draw like the supplied reference screenshots; rendering all final states at once and retaining stale setup sessions makes the game look broken and produces confusing ownership alerts.

**How to apply:** Keep the in-progress board and settlement message as separate message states. Do not replace button styles with emoji markers, and always clean up expired setup sessions before rejecting a new game.