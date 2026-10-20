---
name: pin
description: Pin test files so the agent can read and run them but not edit them (rep).
argument-hint: "<test paths>"
disable-model-invocation: true
---

The rep harness has already handled this command. Its result is in the `[rep]` lines added to this turn.

Relay which files are pinned in one line, and name any path that was not found.

Pinned tests are the user's definition of done. Implement against them. If one looks wrong, show the change you would make as a diff in chat and let the user make it. Pins clear when the brief closes.
