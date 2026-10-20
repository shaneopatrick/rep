---
name: approve
description: Approve the draft brief, optionally with your pre-mortem (rep).
argument-hint: "[where you expect this to go wrong]"
disable-model-invocation: true
allowed-tools: Bash(rep check *) Bash(rep note *) Bash(rep brief *)
---

The harness has handled this command; the `[rep]` lines in this turn say what happened. If nothing was approved, relay why in one line and stop. Do not comment on the pre-mortem: it is the user's prediction and the review will compare it with what happens.

If a new brief was approved, start the work:

1. If the check needs a new test, write it first, then run `rep check` and see it fail.
2. Make the smallest change that meets the goal, inside the approach and the budget.
3. Run `rep check` until it passes. Use `rep check` rather than calling the test command yourself, so the harness sees the result.
4. When you make a real decision, or stop partway, record it with `rep note "<one line>"`.
5. When the check passes, end with: `Check passing. /review when you're ready.`

If a revision was approved, carry on under the revised brief.
