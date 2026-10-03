---
name: predict
description: Log your prediction before the agent acts (rep).
argument-hint: "[where the problem is, what the fix will touch, what might break]"
disable-model-invocation: true
---

The rep harness has already handled this command. Its result is in the `[rep]` lines added to this turn.

- A prediction was recorded: do not grade it or react to it. Carry on with the task, and give the one-line prediction check once you have evidence.
- Nothing was recorded because no text was given: ask the three prediction questions in one short message, with none of your own diagnosis, and stop. The user replies with a line starting `predict:`.
- rep is not enabled here: say so in one line.
