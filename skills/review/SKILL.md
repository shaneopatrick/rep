---
name: review
description: Review the work against its brief, on evidence, then accept, redirect or reject it (rep).
argument-hint: "[accept | redirect <what to change> | reject]"
disable-model-invocation: true
allowed-tools: Bash(rep check *) Bash(rep brief *) Bash(rep diff *) Bash(rep status *) Bash(rep mistake *) Bash(rep simplify *) Bash(git log *) Bash(git status *)
---

The harness has handled this command; the `[rep]` lines in this turn say what happened.

**If the user decided** (accept, redirect or reject), relay it in one line.
- **Accepted**: if the work taught something the code can't show (a constraint, a trade-off, a reason), offer one line for `lessons.md` and add it only if the user agrees.
- **Redirected**: carry on under the brief, taking the user's note as the next instruction. If the note started with a pattern, the harness has already recorded it; don't record it again.
- **Rejected**: leave the code as it is unless the user asks you to undo it.

**If the user typed `/review` alone**, the `[rep]` lines hold a review packet the harness measured. The user's `/review` asks for the two subagents below. Start them; don't do either job yourself.

1. If the packet says the code changed since the last check run, run `rep check` first.
2. **If the packet says the simplify pass is due, run it first.** `rep simplify start` prints a prompt. Start one fresh subagent with exactly that prompt, and nothing from this conversation. When it finishes, run `rep check` (it must pass), then `rep simplify done`. Report the harness's line ("simplify pass closed: N -> M lines") as given.
3. **Get an independent review of the code as it now stands.** Start one subagent. Give it only the brief (from `rep brief`) and the change (from `rep diff`). Do not give it this conversation or your opinion of the work. Ask it for at most five findings, each with a file and line:
   - Does the change meet the goal, within the approach and its stated tradeoff?
   - **Check every assumption against its source**: read the file, run the command, open the doc. Is it actually true? A primary source beats any summary, including yours.
   - Is anything outside the approach, the budget or the paths?
   - Is there an implementation that would pass the check and still miss the goal?
   - Tag each finding with one pattern: overengineering, skipped-verification, stopped-short, weak-check, destructive, scope-drift, wrong-assumption, mechanism-not-outcome, stale-copy, trusted-summary.
4. **Record each finding you agree is a real defect** with `rep mistake <pattern> "<what, in one line>"`. Don't record findings you dispute.
5. Reply in this order, briefly:
   - **Evidence**: the packet's facts, as given, plus the simplify line. Do not restate them in rosier words.
   - **Reviewer findings**: say which you agree with and which you recorded.
   - **Pre-mortem**: the user's prediction next to what actually happened.
6. Ask the user to decide with `/review accept`, `/review redirect <what to change>` or `/review reject`. Mention that starting the note with a pattern records it, for example `/review redirect stopped-short: retries aren't covered`. Then stop.
