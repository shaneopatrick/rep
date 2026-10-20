#!/usr/bin/env python3
"""PreToolUse (Edit|Write|MultiEdit|NotebookEdit): TODO(human) regions belong to the user.

The edit is simulated in memory. It goes through only if every region that
exists now is still there, byte for byte, afterwards. The agent may add a
region (that is how a rep gets placed) but can never change or remove one,
until the user releases it with /skip.
This hook also keeps the edit tools out of .rep/ itself.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

TOUCHED = (
    "rep: this edit would change a TODO(human) region in {file}. That block is the user's to write; it is the "
    "rep. Leave it exactly as it is: do not fill it, move it, delete it, or route around it with a sibling "
    "implementation or a Bash write. If the user is stuck they can pull /hint, and if they want you to take it "
    "over they type /skip."
)
UNVERIFIABLE = (
    "rep: {file} contains a TODO(human) region and this edit cannot be checked against it (old_string is not in "
    "the file verbatim, or the tool is not one rep can simulate). Re-read the file and retry with exact text "
    "that stays outside the region."
)
BUDGET = (
    "rep: this would bring the brief to {n} TODO(human) regions, and the limit is {max}. Keep the one or two "
    "that sit on core logic and write the rest yourself."
)
STATE = (
    "rep: .rep/ is harness state and is not editable by tools. Read it with `rep status`. Changes come from "
    "the user: /shape, /approve, /review, /pin, /hint, /skip."
)


def main(ev):
    path = replib.target_path(ev)
    root = replib.find_root(path) if path else None
    if not root:
        return
    file = replib.rel(root, path)
    if file == replib.REP_DIR or file.startswith(replib.REP_DIR + "/"):
        replib.deny(STATE)

    state = replib.load_state(root)
    if state["todo_released"]:
        return
    before = replib.read_text(path)
    after = replib.simulate(ev.get("tool_name"), ev.get("tool_input") or {}, before)

    if after is None:
        if replib.regions(before):
            _blocked(root, ev, file, "unverifiable")
            replib.deny(UNVERIFIABLE.format(file=file))
        return
    if replib.lost_regions(before, after):
        _blocked(root, ev, file, "region")
        replib.deny(TOUCHED.format(file=file))

    added = len(replib.new_regions(before, after))
    if not added:
        return
    with replib.state_tx(root) as state:
        todos = state["todos"]
        total = sum(todos.values()) + added
        if total > replib.MAX_TODOS:
            over = True
        else:
            over = False
            todos[file] = todos.get(file, 0) + added
            if file not in state["region_files"]:
                state["region_files"] = sorted(state["region_files"] + [file])
    if over:
        replib.deny(BUDGET.format(n=total, max=replib.MAX_TODOS))
    replib.event(root, "todo_placed", file=file, regions=added)


def _blocked(root, ev, file, why):
    with replib.state_tx(root) as state:
        state["refused"] += 1
    replib.event(root, "guard_deny", tool=ev.get("tool_name"), file=file, why=why)


if __name__ == "__main__":
    replib.run(main)
