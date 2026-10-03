---
name: retro
description: Close the task with a one-minute retro. Compare predictions with what happened and capture a lesson (rep).
argument-hint: "[skip]"
disable-model-invocation: true
allowed-tools: Bash(rep log *) Bash(rep status *) Bash(git diff *) Bash(git status *) Bash(git log *)
---

The harness has put a `[rep] retro brief` in this turn: the task, the user's predictions verbatim, and what the hooks saw. If it says the task was closed (`/retro skip`) or that nothing is open, relay that in one line and stop.

Otherwise run the retro. It should cost the user about a minute, so keep every message short.

## 1. Show prediction against evidence, then ask

One message:

- For each prediction: the user's words, then what actually happened (the cause found, the files the change touched, what broke or passed). Use evidence only: a test result, a diff, a file. Suggest `match`, `partial` or `miss`. The grade is the user's call.
- Then the three questions. What surprised you? What is the lesson, in one sentence? Should it be codified for the agent, and as what: a test, a lint rule, a hook, a line in lessons.md, or nothing?
- If a `TODO(human)` was part of this task, add: on a 1 to 5 scale, did it land on real core logic?
- If the brief reports protected-file changes through Bash, say so plainly. The user needs to know the hooks were bypassed.

Stop and wait for the answer.

## 2. Codify what the user chose

- A lesson goes in `lessons.md` at the repo root as one line: `- YYYY-MM-DD <area>: <the lesson>`. Write only what the code cannot say: a constraint, a trade-off, a reason. Create the file with a `# Lessons` heading if it does not exist.
- A test, lint rule or hook is better than prose whenever the lesson can be enforced mechanically. Show exactly what you would add and apply it only after the user says yes.
- Nothing is a fine answer. Most tasks do not produce a rule.

## 3. Log it, last

`rep log retro` closes the task, so run it only once the edits above are done:

```
rep log retro \
  --grade 1=partial --evidence "1=the lock existed but was taken after the read" \
  --surprise "advisory locks were already in use elsewhere" \
  --lesson "queue claims must be atomic" \
  --codify test --path tests/test_claim.py \
  --skills craft.data.concurrency
```

- `--grade N=match|partial|miss` and `--evidence N=text`, once per prediction, using the user's grade.
- `--codify none|context|test|lint|hook|skill` with `--path`. `context` means a lessons.md line.
- `--todo done|released --todo-rating N` if a `TODO(human)` was involved.
- `--skills`: one or two ids for what the task exercised, as `<area>.<leaf>`. Areas: `craft.fullstack`, `craft.data`, `craft.architecture`, `craft.reliability`, `craft.production`, `agentic.shaping`, `agentic.directing`, `agentic.autonomy`, `agentic.reviewing`, `agentic.environment`, `agentic.foundations`.
- Record the user's own words for surprise and lesson. Trim them; do not improve them.

Finish with one line: what was logged and what, if anything, was codified.
