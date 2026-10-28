# rep

You own the outer loop; agents do the inner loop. rep gives each piece of work a **brief**: a one-screen contract with the goal, the chosen approach and the tradeoff it makes, the assumptions it rests on, a budget, and the check that decides done. You shape and approve it, agents work inside it, and you review evidence rather than claims. Hooks enforce the contract, so the model can't talk its way past it.

No brief means ordinary Claude Code. rep only acts on work you've shaped.

## Install

```
python3 install.py              # links into ~/.claude, registers the hooks
rep doctor                      # checks the install
python3 install.py --uninstall  # removes all of it; ~/.claude/settings.json.before-rep is the original
```

Everything in `~/.claude` is a symlink back to this repo, so editing a file here changes the live harness. **Upgrading from Phase 0: run `python3 install.py` again after pulling.** It replaces the old hook registrations and removes the retired `/predict`, `/retro` and `/dial` skills.

## The loop

| You type | What happens |
| --- | --- |
| `/shape <goal>` | The agent looks just enough to see the options. It proposes two or three approaches, smallest first, each with its tradeoff named (latency, availability, consistency, reliability, maintainability, simplicity, cost). It drafts a brief, listing the assumptions it rests on, each with a source. No code is touched until you approve. The first `/shape` in a repo turns rep on there. |
| `/approve [pre-mortem]` | The draft becomes binding. Anything after `/approve` is your pre-mortem, recorded verbatim: where you expect this to go wrong. The budget is measured from a snapshot of the code at this moment, so earlier uncommitted work never counts against it. |
| (the agent works) | It writes the check first if needed, runs `rep check` until it passes, and notes decisions with `rep note`. |
| `/review` | First a fresh subagent makes a **simplify pass** over the brief's diff (details below). Then you get an evidence packet measured by the harness: check history, size against budget, the simplify result, the chosen tradeoff, refused edits, mistakes recorded, and your pre-mortem. A second fresh subagent reviews the code using only the brief and the diff, and checks every assumption against its source. |
| `/review accept`, `/review redirect <note>`, `/review reject` | Your decision. Accept and reject close the brief; redirect keeps it open with your note. Start the note with a pattern (`/review redirect weak-check: pin the 8s delay`) to record a mistake. |
| `/shape drop` | Throw away a draft. |
| `/pin <tests>`, `/hint`, `/skip` | Optional: pinned tests are read-only for the agent; `TODO(human)` regions with a hint ladder, and `/skip` hands them back. |

A brief:

```
# Brief: Make job claiming atomic
Asked: "Under load, some jobs run twice. Make claiming a job atomic."

## Goal
A job is handed to at most one worker, even when many workers call claim() at the same moment.

## Approach
One UPDATE ... WHERE id = (first pending, runnable job) RETURNING id, payload.
Tradeoff: favours consistency and simplicity; gives up nothing measurable at this load
Rejected: BEGIN IMMEDIATE (serialises writers, can time out); SKIP LOCKED (different database)

## Assumptions
- SQLite is 3.35 or newer, for RETURNING (source: python3 -c "import sqlite3; print(sqlite3.sqlite_version)")
- claim() is the only code that moves a job to running (source: git grep -n "'running'")

## Budget
lines: 45
files: 2
paths: jobq/queue.py, tests/test_queue.py
deps: no

## Check
cmd: python3 -m unittest -q tests.test_queue
unverified: real multi-process load; needs SQLite 3.35+

## Pre-mortem
the forced-interleaving test will be hard to make deterministic; it'll reach for sleeps

## Log
- 10-20 09:45 approved by the user (base 79d6b11)
- 10-20 09:45 check failing
- 10-20 09:45 check passing
- 10-20 09:45 Claim is one UPDATE...RETURNING. Test forces the interleaving with a connection subclass, no sleeps.
```

A draft is refused unless the Approach has a `Tradeoff:` line and every assumption has a `source:`. Naming the tradeoff is how you steer the agent toward the one you want, and knowing it exists is half of that. The source is what the independent reviewer checks the assumption against: a primary source, not a summary. If an assumption turns out false mid-build, the agent stops and proposes a revision. An assumption revised away is recorded as a `wrong-assumption` mistake.

Briefs live in `.rep/briefs/`. You can edit one in your editor at any time; the agent can't edit it at all, and proposes changes with `rep brief draft` for you to `/approve`.

## The four rules

| Rule | Hook | What it does |
| --- | --- | --- |
| Budget breaker | `budget_gate.py`, `bash_audit.py` | The edit that would push the diff past the budget is refused; edits that shrink the diff always go through. A Bash command that crosses the line is reported. The agent has to stop, explain, and cut scope or propose a revision. |
| Scope | `budget_gate.py` | Edits outside the brief's `paths`, and to dependency manifests unless `deps: yes`, are refused. |
| You own the contract | `guard_human.py`, `bash_guard.py`, `on_prompt.py` | The agent can't edit the brief, approve it, or review it. State only changes on something you typed. |
| Done means passing | `done_gate.py` | After any turn that changed code, you get one line measured by the harness: whether the check passes on the current code, and the budget used. If the check hasn't run on the change, the agent is sent back once to run it. |

`session_start.py` loads the active brief and its log into every new session, after `/clear`, and after compaction. Context survives between sessions as one file rather than memory.

The Phase 0 protections still apply: `TODO(human)` regions can't be changed by the agent (the edit is simulated and every region must survive byte for byte), pinned tests are read-only, and Bash writes to either are caught and can be undone with `rep restore`.

## The simplify pass

When you type `/review` and the brief's diff is 30 lines or more, a fresh subagent first tries to make the change smaller and plainer. It deletes dead code, inlines single-use helpers, reuses existing utilities, and drops defensive code for cases that can't happen. Three rules are enforced in code while the pass is open:

- **Shrink-only.** Any edit that would grow the diff is refused, so "simplify" can't turn into new abstractions.
- **Tests are off-limits.** They are the oracle the simplified code is measured against. A shrink-only pass would otherwise find deleting assertions the easiest win.
- **The check must pass before and after.** `rep simplify done` refuses until it does.

The subagent also gets the brief, so it keeps the approach and its tradeoff. The result appears in the packet (`Simplify pass: 41 -> 21 lines (-49%)`). A pass that removes a quarter of the diff or more is recorded as `overengineering`, which over time shows how much your agents overbuild. Add `simplify: no` under a brief's Budget to turn the pass off for that brief.

The pass runs inside `/review`, rather than at the end of the build, because Claude Code tells the model to start subagents only when the user asks. Your `/review` is that request. A reminder from a hook isn't, and in testing the model then simplified its own work instead.

## The mistakes register

`.rep/mistakes.jsonl` is an append-only record of what went wrong, grouped by a fixed set of patterns so it adds up instead of collecting free text:

| Pattern | Meaning | Recorded by |
| --- | --- | --- |
| overengineering | built more than the goal needed | budget breaker, or a simplify pass that cut 25% or more |
| skipped-verification | stopped or claimed done without the check passing on the current code | the done gate |
| stopped-short | the check passed but the goal was not met | you or the reviewer |
| weak-check | the check would also pass a wrong implementation | you or the reviewer |
| destructive | changed or removed something it should not have | a Bash write to a protected file |
| scope-drift | worked outside the brief's approach or paths | the scope and dependency rules |
| wrong-assumption | an assumption in the brief turned out false | a revision that drops an assumption |
| mechanism-not-outcome | checked that it ran, not that it worked | you or the reviewer |
| stale-copy | changed one copy of a fact and missed the others | you or the reviewer |
| trusted-summary | trusted a summary over the primary source | you or the reviewer |

Harness-recorded patterns count at most once per brief. The agent records reviewer findings it agrees with (`rep mistake <pattern> "<what>"`), and you record by starting a `/review` note with a pattern. `rep mistakes` shows the register grouped and most frequent first. The top three are shown to the agent at every `/shape`, so the next brief is shaped against them.

## What the harness measures

- `rep check` runs the brief's command itself. It records pass or fail against a snapshot of the code, prints only the last 60 lines of output, flags a different result on identical code as **flaky**, and calls out checks slower than a minute.
- `rep report` adds up the closed briefs:
  - your budget estimates against actual size;
  - breaker trips;
  - how often the check failed before it passed (red before green);
  - whether it passed on the final code;
  - flaky runs and refused edits;
  - the simplify passes and how much they cut;
  - the most frequent mistake patterns;
  - Bash bypasses, which should stay at 0.
- `.rep/reps.jsonl` holds one record per closed brief: budget against actual, check history, pre-mortem, outcome. `.rep/events.jsonl` is the audit trail. It records no prompt text and no command lines, only the program name when a protected file was changed.
- `.rep/` carries its own `.gitignore` of `*`, so nothing in it is ever committed, from any worktree.

## Verified: simplify, tradeoffs, assumptions, mistakes

- 86 tests cover the brief contract, the register's automatic and manual entries, and the simplify pass: shrink-only, tests off-limits, check before and after, the cut measured.
- Live headless sessions on Haiku, with the retry-with-backoff task:
  - **`/shape`:** each approach named its tradeoff. The brief carried three assumptions, each backed by a primary source: a `git grep` result, the official SQLite docs, and a line of code.
  - **`/review`:** the reviewer checked each assumption against its source and said plainly which one it hadn't fetched. It found a real weak check: the backoff test only pinned the 1s and 2s delays, so a linear schedule would also pass. The finding went into the register.
  - **The first version of the simplify pass** was triggered by a reminder from the done gate. The model did the pass itself, saying the user hadn't asked for a subagent. Moved into `/review`, the pass and the review each ran in their own fresh subagent.

## Verified

- `python3 -m unittest discover -s tests`: 65 tests drive every hook with the payloads Claude Code 2.1.288 actually sends.
- Live headless sessions on Haiku in `scripts/sandbox.sh`'s trial repo:
  - **`/shape`:** three approaches, smallest first, a draft brief, no code touched.
  - **`/approve`:** test written first, the check seen failing, the fix, then the check seen passing. 28 of 45 lines.
  - **`/review`:** the packet, findings from an independent subagent (it checked the new test fails on the old code), and the pre-mortem set against what happened. Then redirect, then accept.
  - **Budget tightened to 15 lines:** the test was written, the next edit was refused at 24 lines, and the agent stopped, logged why, and drafted a revision raising the budget to 30. It made no attempt to get around the limit.
  - **A brand-new session with no history:** it described exactly where the work stood, from the brief alone.

## Known limits

- Budgets need git. Outside a git repo the breaker and scope rules are off. In a very large repo the first snapshot hashes every file once.
- Scope is enforced on edit tools only. A Bash command can write outside `paths`; its lines still count against the budget.
- The simplifier and the reviewer are subagents the `/review` skill asks for. In testing they ran every time, but no hook guarantees it.
- The simplify pass protects tests by path (`tests/`, `test_*`, `*_test.*`, `*.spec.*`, `*.test.*`). Test files named any other way aren't recognised.
- The register is append-only, so a defect recorded twice stays counted twice. `rep mistake` warns when a brief already has that pattern.
- The pre-mortem is shown next to what happened, but nothing grades it automatically.
- There is one active brief per checkout. Parallel sessions in the same checkout share it; worktrees get their own.
- `TODO(human)` answers pasted into chat are prevented by instruction only.

## Not built yet

These wait until the brief and budget have proven themselves in daily use:
- `/run --away`: unattended runs in a worktree, allowed only when the check is fast, deterministic and strong.
- `/split`: child briefs run in parallel worktrees.
- A strength test: wrong-but-plausible variants run against the check.

## Files

```
hooks/replib.py         state, briefs, snapshots, measurement, hook plumbing
hooks/on_prompt.py      /shape /approve /review /pin /hint /skip; one [rep] line per turn
hooks/session_start.py  the active brief at the start of every session
hooks/budget_gate.py    draft lock, scope, dependencies, budget breaker, shrink-only simplify
hooks/done_gate.py      done means the check passes on the current code; an open simplify pass gets finished
hooks/guard_human.py    TODO(human) regions and .rep/ are not the agent's
hooks/lock_tests.py     pinned tests are read-only
hooks/bash_guard.py     the user's commands stay the user's; snapshot before Bash
hooks/bash_audit.py     protected files and budget after Bash
hooks/predict_gate.py   retired no-op, kept so an old install never blocks edits
bin/rep                 the CLI
skills/                 shape, approve, review, pin, hint, skip
output-styles/rep-tutor.md
scripts/sandbox.sh      a throwaway repo with three pieces of work to try
```
