# rep

You own the outer loop; agents do the inner loop. rep gives each piece of work a **brief**: a one-screen contract with the goal, the chosen approach, a budget, and the check that decides done. You shape and approve it, agents work inside it, and you review evidence rather than claims. Hooks enforce the contract, so the model can't talk its way past it.

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
| `/shape <goal>` | The agent looks just enough to see the options, proposes two or three approaches (smallest first), and drafts a brief. No code is touched until you approve. The first `/shape` in a repo turns rep on there. |
| `/approve [pre-mortem]` | The draft becomes binding. Anything after `/approve` is your pre-mortem, recorded verbatim: where you expect this to go wrong. The budget is measured from a snapshot of the code at this moment, so earlier uncommitted work never counts against it. |
| (the agent works) | It writes the check first if needed, runs `rep check` until it passes, and notes decisions with `rep note`. |
| `/review` | An evidence packet measured by the harness: check history, size against budget, refused edits, your pre-mortem. The agent adds findings from an independent reviewer that sees only the brief and the diff. |
| `/review accept`, `/review redirect <note>`, `/review reject` | Your decision. Accept and reject close the brief; redirect keeps it open with your note. |
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
Rejected: BEGIN IMMEDIATE (serialises writers, can time out); SKIP LOCKED (different database)

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

## What the harness measures

- `rep check` runs the brief's command itself. It records pass or fail against a snapshot of the code, prints only the last 60 lines of output, flags a different result on identical code as **flaky**, and calls out checks slower than a minute.
- `rep report` adds up the closed briefs:
  - your budget estimates against actual size;
  - breaker trips;
  - how often the check failed before it passed (red before green);
  - whether it passed on the final code;
  - flaky runs and refused edits;
  - Bash bypasses, which should stay at 0.
- `.rep/reps.jsonl` holds one record per closed brief: budget against actual, check history, pre-mortem, outcome. `.rep/events.jsonl` is the audit trail. It records no prompt text and no command lines, only the program name when a protected file was changed.
- `.rep/` carries its own `.gitignore` of `*`, so nothing in it is ever committed, from any worktree.

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
- The reviewer is a subagent the review skill asks for. No hook guarantees it ran.
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
hooks/budget_gate.py    draft lock, scope, dependencies, budget breaker
hooks/done_gate.py      done means the check passes on the current code
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
