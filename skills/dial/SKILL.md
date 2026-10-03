---
name: dial
description: "Set the rep light switch for this repo: dark, dim or lit."
argument-hint: "dark|dim|lit"
disable-model-invocation: true
---

The rep harness has already handled this command. Its result is in the `[rep]` lines added to this turn.

Relay the result in one line and carry on under the new setting. If the line shows usage text, the argument was not one of `dark`, `dim` or `lit`; say so.

Changing the dial does not close the open task or unpin anything.
