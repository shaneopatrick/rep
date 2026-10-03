#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure (Bash): did that command change a protected file?

Compares each protected file with the snapshot bash_guard.py took just
before the command ran. The window is one command long, so an edit the user
saves from their editor between tool calls is never blamed on the agent.

The command has already run, so this cannot prevent anything. It keeps the
pre-image, logs a violation, and tells the agent to put it back. It does not
restore on its own: `git checkout` and formatters change files too, and the
user may have saved the file in the same moment.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

MESSAGE = (
    "rep: protected file(s) changed while your Bash command ran: {files}. TODO(human) regions and pinned tests "
    "belong to the user. If your command wrote there, that is a violation and it is logged: put each file back "
    "with `rep restore <file>` (the harness kept the pre-image) and tell the user plainly what happened. If your "
    "command did not touch the file, the user probably saved it from their editor at the same moment: change "
    "nothing and mention it."
)


def main(ev):
    snap_file = replib.snap_path(ev.get("tool_use_id"))
    try:
        with open(snap_file) as f:
            snap = json.load(f)
        os.remove(snap_file)
    except (OSError, ValueError):
        return
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
    if not changed:
        return

    with replib.state_tx(root) as state:
        state["work"]["violations"] += len(changed)
    command = (ev.get("tool_input") or {}).get("command") or ""
    replib.event(root, "violation", files=changed, command=replib._clip(command, 300),
                 task=(state["task"] or {}).get("id"))
    replib.block(MESSAGE.format(files=", ".join(changed)))


if __name__ == "__main__":
    replib.run(main)
