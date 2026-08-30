---
name: Keno Telegram reveal UX
description: The expected Telegram Keno interaction and the stale-board behavior to preserve.
---

Keno's Telegram board uses Bot API button styles: primary for selected numbers, danger for revealed misses, and success for revealed hits. The reveal must keep the caption and control rows, edit the same board after each draw in sequence, and finish by replacing that same message's caption and controls with the outcome. Setup boards expire after the configured timeout so old boards cannot block a new `/keno` command.

**Why:** The intended experience is an animated one-by-one draw like the supplied reference screenshots; rendering all final states at once, removing the board during the draw, or retaining stale setup sessions makes the game look broken and produces confusing ownership alerts.

**How to apply:** Keep the in-progress board visible throughout the reveal, disable its setup actions without removing their rows, and edit the board in place for the final outcome and action buttons. Do not replace button styles with emoji markers, and always clean up expired setup sessions before rejecting a new game.