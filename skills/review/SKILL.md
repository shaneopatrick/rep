---
name: review
description: Review the work against its brief, on evidence, then accept, redirect or reject it (rep).
argument-hint: "[accept | redirect <what to change> | reject]"
disable-model-invocation: true
allowed-tools: Bash(rep check *) Bash(rep brief *) Bash(rep status *) Bash(git diff *) Bash(git status *) Bash(git log *)
---

The harness has handled this command; the `[rep]` lines in this turn say what happened.

**If the user decided** (accept, redirect or reject), relay it in one line.
- Accepted: if the work taught something the code can't show (a constraint, a trade-off, a reason), offer one line for `lessons.md` and add it only if the user agrees.
- Redirected: carry on under the brief, taking the user's note as the next instruction.
- Rejected: leave the code as it is unless the user asks you to undo it.

**If the user typed `/review` alone**, the `[rep]` lines hold a review packet the harness measured. Then:

1. If the packet says the code changed since the last check run, run `rep check` first.
2. Get an independent review. Start one subagent and give it only the brief (from `rep brief`) and the diff (`git diff <base>` from the packet, plus any new files). Do not give it this conversation or your opinion of the work. Ask it for at most five findings, each with a file and line:
   - Does the change meet the goal?
   - Is anything outside the approach, the budget or the paths?
   - Is there an implementation that would pass the check and still miss the goal?
3. Reply in this order, briefly:
   - **Evidence**: the packet's facts, as given. Do not restate them in rosier words.
   - **Reviewer findings**: say which of them you agree with.
   - **Pre-mortem**: the user's prediction next to what actually happened.
4. Ask the user to decide with `/review accept`, `/review redirect <what to change>` or `/review reject`. Then stop.
