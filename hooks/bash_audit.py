#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure (Bash): what did that command change?

Two questions, because the edit hooks never see what a shell command writes:

1. Did it change a protected file (a TODO(human) region or a pinned test)?
   Each file is compared with the snapshot bash_guard.py took just before the
   command ran, so an edit the user saves between tool calls is never blamed
   on the agent. The pre-image is kept for `rep restore`; nothing is restored
   automatically, because `git checkout` and formatters change files too.
2. Did it push the work past the brief's budget? A code generator or a
   package install can. The agent hears about it once per new high. During
   the simplify pass any growth at all is reported, since the pass is
   shrink-only.

Protected-file changes and budget trips go into the mistakes register.

The command has already run, so this cannot prevent anything; it makes sure
the agent and the log both know.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

PROTECTED = (
    "rep: protected file(s) changed while your Bash command ran: {files}. TODO(human) regions and pinned tests "
    "belong to the user. If your command wrote there, put each file back with `rep restore <file>` (the harness "
    "kept the pre-image) and tell the user plainly what happened. If your command did not touch the file, the "
    "user probably saved it from their editor at the same moment: change nothing and mention it."
)
SIMPLIFY_GROW = (
    "rep: the simplify pass is shrink-only, and that command grew the diff from {before} to {lines} lines. Undo the "
    "growth, then carry on simplifying."
)
BUDGET = (
    "rep: budget breaker. After that command the work is at {lines} lines across {files} file(s); the brief allows "
    "{max_lines} lines / {max_files} files. Stop and tell the user what the extra work is for, then cut scope or "
    "propose a bigger budget with `rep brief draft` for the user to /approve."
)


def main(ev):
    messages = []
    root = audit_protected(ev, messages) or replib.session_root(ev)
    if root:
        audit_budget(root, messages)
    if messages:
        replib.block("\n\n".join(messages))


def audit_protected(ev, messages):
    snap_file = replib.snap_path(ev.get("tool_use_id"))
    try:
        with open(snap_file) as f:
            snap = json.load(f)
        os.remove(snap_file)
    except (OSError, ValueError):
        return None
    root = snap["root"]
    changed = []
    for file, pre in snap["files"].items():
        path = os.path.join(root, file)
        text = replib.read_text(path)
        if pre["kind"] == "pin":
            hit = text != pre["text"] or os.path.exists(path) != pre["exists"]
        else:
            hit = bool(replib.lost_regions(pre["text"], text))
        if hit:
            os.makedirs(replib.rep_path(root, "keep"), exist_ok=True)
            replib.write_text(replib.keep_path(root, file), pre["text"])
            changed.append(file)
    if changed:
        with replib.state_tx(root) as state:
            state["violations"] += len(changed)
            replib.record_mistake(root, state, "destructive", "harness",
                                  "changed %s through Bash" % ", ".join(changed))
        command = ((ev.get("tool_input") or {}).get("command") or "").split()
        replib.event(root, "violation", files=changed, program=os.path.basename(command[0]) if command else "")
        messages.append(PROTECTED.format(files=", ".join(changed)))
    return root


def audit_budget(root, messages):
    state = replib.load_state(root)
    if not state["active"] or not state["base"]:
        return
    budget = replib.budget_of(replib.load_brief(root, state["active"]))
    files, lines = replib.size(replib.changes(root, state["base"]))
    simplify = state["simplify"]
    if simplify and simplify["phase"] == "running" and lines > simplify["before"]:
        replib.event(root, "simplify_grow", via="bash")
        messages.append(SIMPLIFY_GROW.format(before=simplify["before"], lines=lines))
    if not replib.over_budget(budget, files, lines):
        return
    with replib.state_tx(root) as state:
        if lines <= state["over_reported"]:
            return
        state["over_reported"] = lines
        state["trips"] += 1
        replib.record_mistake(root, state, "overengineering", "harness",
                              "budget breaker tripped at %d lines" % lines, once=True)
    replib.event(root, "budget_trip", via="bash", lines=lines, files=files)
    messages.append(BUDGET.format(lines=lines, files=files, max_lines=budget["lines"], max_files=budget["files"]))


if __name__ == "__main__":
    replib.run(main)
