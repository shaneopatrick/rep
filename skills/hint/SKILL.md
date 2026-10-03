---
name: hint
description: "Pull the next hint level for the open TODO(human): nudge, concept, pseudocode, partial code, solution (rep)."
disable-model-invocation: true
---

The rep harness has already handled this command. Its result is in the `[rep]` lines added to this turn.

The `[rep]` line names the level the user just pulled and what it allows. Give exactly that level for the open `TODO(human)` region and nothing beyond it. Keep it short.

At the last level the full solution goes in chat only. The region stays the user's to type.
