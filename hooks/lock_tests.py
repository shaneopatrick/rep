#!/usr/bin/env python3
"""PreToolUse (Edit|Write|MultiEdit|NotebookEdit): pinned tests are read-only for the agent.

The user owns the oracle: a session that implements must not also rewrite the
tests that judge it. Pins hold in dim and lit and clear when the task closes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

MESSAGE = (
    "rep: {file} is a pinned test. The user owns the oracle, so you may read and run it but not change it. "
    "If the test itself looks wrong, show the change you would make as a diff in chat and let the user edit it "
    "or unpin it (`rep unpin {file}` in their terminal). Do not change it through Bash either."
)


def main(ev):
    path = replib.target_path(ev)
    root = replib.find_root(path) if path else None
    if not root:
        return
    state = replib.load_state(root)
    if state["dial"] == "dark":
        return
    file = replib.rel(root, path)
    if file not in state["pins"]:
        return
    with replib.state_tx(root) as state:
        state["work"]["blocked"] += 1
    replib.event(root, "lock_deny", tool=ev.get("tool_name"), file=file)
    replib.deny(MESSAGE.format(file=file))


if __name__ == "__main__":
    replib.run(main)
