---
name: Persistent project handoff
description: Restoring source files and workflows when a conversation becomes a persistent project.
---

When a conversation is moved into a project, the new project can initially contain
only a generated workspace scaffold while the prior source tree is preserved
separately. Restore the preserved project files before making further changes,
excluding temporary system folders and Git metadata, then recreate any
non-artifact workflow the app needs.

**Why:** The project bootstrap can replace the temporary conversation workflow
and leave the user's source files unavailable in the persistent project until
they are explicitly restored.

**How to apply:** Check `.local/conversation-workspace/files` first after a
handoff, copy the preserved application tree into the project, and verify the
app workflow and required secrets before declaring the project runnable.

After attaching a bot token to the new project, check for another running
instance using the same token before validating Telegram behavior. Telegram
polling conflicts can make one instance display an error while another instance
processes the same user's action.

**Why:** A previous temporary workspace or deployment can continue polling even
after its project workflow is no longer visible in the new project.

**How to apply:** Treat repeated `getUpdates` conflict warnings as a
single-instance problem first; stop the old process/deployment, then restart
only the intended persistent workflow before investigating game logic.