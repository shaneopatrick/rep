---
name: shape
description: Shape a piece of work into a one-screen brief before any code is written (rep).
argument-hint: "<what you want done>  |  drop"
disable-model-invocation: true
allowed-tools: Read Grep Glob Bash(rep brief *) Bash(rep check *) Bash(git log *) Bash(git diff *) Bash(git status *)
---

The harness has recorded the request; the `[rep]` lines in this turn say what it did. If they say a draft was dropped or rep is not enabled, relay that in one line and stop. If a brief is already active, this is a revision: draft the whole brief again with the change, say in one line what changed, and keep working under the current brief until the user approves.

Shape the work. Do not edit code until the user approves the brief.

1. **Look just enough to see the options**: the files involved, the tests that exist, how the code is laid out. Minutes, not a full investigation.
2. **Propose two or three approaches, smallest first.** For each: what changes, roughly how many files and lines, and what it costs or risks. Recommend the smallest one that fully meets the goal, and say plainly what the bigger options would buy.
3. **Pick the check that decides done.** Use an existing test command if one covers the goal; otherwise the command for a test you will write first. It must be fast and give the same result on every run. If it can run now, run it once with `rep check` so the brief starts with evidence. New behaviour should fail now.
4. **Write the draft** with `rep brief draft`, using your recommended approach. Set the budget to what that approach needs plus a little headroom. The budget is not room for the bigger option.
5. **Reply briefly**: the approaches in one line each, then the draft's budget and check, then the user's next step: `/approve <where they expect this to go wrong>` (the pre-mortem is optional), or tell you what to change. Then stop.

The draft must fit on one screen (40 lines). The harness adds the Pre-mortem and Log sections itself.

```
rep brief draft <<'EOF'
# Brief: <short title>
## Goal
<one sentence: the outcome, in terms a user of the code would notice>
## Approach
<the chosen approach, two or three lines>
Rejected: <option> (<why>); <option> (<why>)
## Budget
lines: <most lines this should change, added plus removed>
files: <most files this should touch>
paths: <optional: folders or files the work may touch, comma-separated>
deps: no
## Check
cmd: <command that passes only when the goal is met>
unverified: <what the check does not cover>
EOF
```
