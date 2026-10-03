#!/usr/bin/env python3
"""PreToolUse (Edit|Write|MultiEdit|NotebookEdit): no edits until a prediction exists.

A task opens only when the user logs a prediction or types /skip, so "no open
task" means the gate is shut. Dark mode has no gate.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

MESSAGE = (
    "rep ({dial}): edits are locked until the user logs a prediction for this task. Stop and ask for it in one "
    "short message: where they think the problem (or the main change) is, what the change will touch, and what "
    "might break. Leave your own diagnosis, plan and findings out of that message, because a prediction made "
    "after reading your answer is not a rep. They reply with a line starting `predict:` or type /skip, which is "
    "free. Do not retry this edit, write the file through Bash, or log anything on their behalf."
)


def main(ev):
    path = replib.target_path(ev)
    root = replib.find_root(path) if path else None
    if not root:
        return
    state = replib.load_state(root)
    if state["dial"] == "dark" or state["task"]:
        return
    with replib.state_tx(root) as state:
        work = state["work"]
        work["denials"] += 1
        work["asked_at"] = work["asked_at"] or replib.now()
    replib.event(root, "gate_deny", tool=ev.get("tool_name"), file=replib.rel(root, path))
    replib.deny(MESSAGE.format(dial=state["dial"]))


if __name__ == "__main__":
    replib.run(main)
