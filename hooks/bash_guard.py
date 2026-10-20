#!/usr/bin/env python3
"""PreToolUse (Bash): keep the agent off the user's controls, then snapshot.

Two denials, both aimed at the easy ways around the other hooks:
  - `rep approve`, `rep review`, `rep pin` ... are the user's commands. An
    agent that can run them can approve its own contract.
  - anything that names the .rep/ directory. State is read with `rep status`.

Then it saves the current text of every protected file (files with
TODO(human) regions, pinned tests) so bash_audit.py can tell whether the
command changed one. The edit hooks cannot see `sed -i`, `cat >` or a Python
one-liner; this pair is what closes that hole.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

HUMAN_ONLY = {"approve", "review", "pin", "unpin", "skip", "init"}
REP_CALL = re.compile(r"(?:^|[\s;&|(`])(?:\S*/)?rep['\"]?\s+([a-z-]+)")
# The one command allowed to change a protected file: the harness putting it back.
RESTORE_ONLY = re.compile(r"^\s*rep\s+restore\s+[\w./-]+\s*$")
STATE_REF = re.compile(r"(?:^|[\s/'\"=<>(])\.rep(?:/|\s|$|['\");&|])")

NOT_YOURS = (
    "rep: `rep {sub}` is the user's command, not the agent's. If it is needed, ask the user to type it "
    "(/shape, /approve, /review, /pin, /hint and /skip work inside Claude Code). Your own commands are "
    "`rep brief draft`, `rep check`, `rep note`, `rep status`, `rep report` and `rep restore <file>`."
)
STATE = (
    "rep: .rep/ is harness state. Read it with `rep status`; do not read, write, move or delete it from the "
    "shell. Changes come from the user: /shape, /approve, /review, /pin, /hint, /skip."
)


def main(ev):
    root = replib.session_root(ev)
    if not root:
        return
    command = (ev.get("tool_input") or {}).get("command") or ""
    for call in REP_CALL.finditer(command):
        if call.group(1) in HUMAN_ONLY:
            replib.event(root, "bash_deny", why="human_only", sub=call.group(1))
            replib.deny(NOT_YOURS.format(sub=call.group(1)))
    if STATE_REF.search(command):
        replib.event(root, "bash_deny", why="state_ref")
        replib.deny(STATE)

    files = replib.protected_files(replib.load_state(root))
    if not files or RESTORE_ONLY.match(command):
        return
    snap = {"root": root, "files": {}}
    for file, kind in files.items():
        path = os.path.join(root, file)
        snap["files"][file] = {"kind": kind, "exists": os.path.exists(path), "text": replib.read_text(path)}
    target = replib.snap_path(ev.get("tool_use_id"))
    sweep(os.path.dirname(target))
    with open(target, "w") as f:
        json.dump(snap, f)


def sweep(folder, max_age=3600):
    """A call that was refused after this hook ran never reaches the audit; drop its snapshot."""
    cutoff = replib.now() - max_age
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    replib.run(main)
