#!/usr/bin/env python3
"""SessionStart: every session begins with the active brief.

Fires on startup, resume, /clear and after compaction, so the contract and
its log survive the end of a conversation. This is how context moves between
sessions: one file, not memory.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402


def main(ev):
    root = replib.session_root(ev)
    if not root:
        return
    text = replib.session_context(root, replib.load_state(root))
    if text:
        replib.add_context("SessionStart", text)


if __name__ == "__main__":
    replib.run(main)
