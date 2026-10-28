#!/usr/bin/env python3
"""PreToolUse (Edit|Write|MultiEdit|NotebookEdit): work stays inside the brief.

- No brief: nothing to enforce. This is ordinary Claude Code.
- A draft waiting for /approve: no code edits. The approach is not chosen yet.
- An active brief: edits stay inside its paths, leave dependency manifests
  alone unless `deps: yes`, and stop at the budget. The edit is simulated, so
  the one that would cross the line is refused, while edits that shrink the
  diff always go through: cutting scope is never blocked.
- During the simplify pass: shrink-only, and tests are off-limits. A pass
  that may only make the diff smaller would otherwise find deleting test
  assertions the easiest way to do it.

Budget trips and scope refusals are recorded in the mistakes register.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

DRAFT = (
    "rep: the brief \"{title}\" is still a draft, so the approach is not chosen yet. Do not edit code until the "
    "user approves it with /approve (or drops it with /shape drop)."
)
SCOPE = (
    "rep: {file} is outside the brief's paths ({paths}). If the work really needs it, stop and propose a revised "
    "brief with `rep brief draft`; the user approves it with /approve."
)
DEPS = (
    "rep: {file} is a dependency manifest and the brief says no new dependencies. If one is truly needed, stop "
    "and propose a revision with `deps: yes` and the reason; the user approves it with /approve."
)
SIMPLIFY_GROW = (
    "rep: the simplify pass is shrink-only, and this edit would grow the diff ({now_lines} -> {lines} lines). "
    "Simplify by removing or inlining, not by adding. If something is missing, finish the pass first."
)
SIMPLIFY_TESTS = (
    "rep: {file} looks like a test, and tests are off-limits during the simplify pass: they are the oracle the "
    "simplified code is measured against."
)
BUDGET = (
    "rep: budget breaker. This edit would bring the work to {lines} lines across {files} file(s); the brief allows "
    "{max_lines} lines / {max_files} files (now at {now_lines} / {now_files}). Stop and tell the user what the extra "
    "work is for. Then either cut scope (edits that shrink the diff always go through) or propose a bigger budget "
    "with `rep brief draft` for the user to /approve. Do not split edits or use Bash to get around this."
)


def main(ev):
    path = replib.target_path(ev)
    root = replib.find_root(path) if path else None
    if not root:
        return
    file = replib.rel(root, path)
    if file == replib.REP_DIR or file.startswith(replib.REP_DIR + "/"):
        return   # guard_human.py owns .rep/
    state = replib.load_state(root)
    if not state["active"]:
        if state["draft"]:
            replib.deny(DRAFT.format(title=replib.load_brief(root, state["draft"])["title"]))
        return

    budget = replib.budget_of(replib.load_brief(root, state["active"]))
    if os.path.basename(file) in replib.MANIFESTS and not budget["deps"]:
        refuse(root, "deps", file, DEPS.format(file=file), "scope-drift")
    if not replib.in_scope(file, budget["paths"]):
        refuse(root, "scope", file, SCOPE.format(file=file, paths=", ".join(budget["paths"])), "scope-drift")
    simplifying = bool(state["simplify"] and state["simplify"]["phase"] == "running")
    if simplifying and replib.TESTLIKE.search(file):
        refuse(root, "simplify_test", file, SIMPLIFY_TESTS.format(file=file))
    if not state["base"]:
        return

    before = replib.read_text(path)
    after = replib.simulate(ev.get("tool_name"), ev.get("tool_input") or {}, before)
    if after is None:
        return
    original = replib.base_text(root, state["base"], file)
    now_map = replib.changes(root, state["base"])
    now_map.pop(file, None)
    next_map = dict(now_map)
    for target, text in ((now_map, before), (next_map, after)):
        added, deleted = replib.line_delta(original, text)
        if added or deleted:
            target[file] = (added, deleted)
    now_files, now_lines = replib.size(now_map)
    files, lines = replib.size(next_map)
    grows = lines > now_lines or files > now_files
    if grows and simplifying:
        replib.event(root, "simplify_grow", file=file)
        replib.deny(SIMPLIFY_GROW.format(now_lines=now_lines, lines=lines))
    if grows and replib.over_budget(budget, files, lines):
        with replib.state_tx(root) as state:
            state["trips"] += 1
            replib.record_mistake(root, state, "overengineering", "harness",
                                  "budget breaker tripped at %d lines" % lines, once=True)
        replib.event(root, "budget_trip", via="edit", lines=lines, files=files)
        replib.deny(BUDGET.format(lines=lines, files=files, max_lines=budget["lines"], max_files=budget["files"],
                                  now_lines=now_lines, now_files=now_files))


def refuse(root, why, file, message, pattern=None):
    with replib.state_tx(root) as state:
        state["refused"] += 1
        if pattern:
            replib.record_mistake(root, state, pattern, "harness", "%s refused for %s" % (file, why), once=True)
    replib.event(root, "refused", why=why, file=file)
    replib.deny(message)


if __name__ == "__main__":
    replib.run(main)
