#!/usr/bin/env python3
"""UserPromptSubmit: the human channel.

This hook sees what the user typed, verbatim, before the model does. Claude
Code does not fire it when the model invokes a skill, so anything recorded
here came from the user's own keystrokes. It does three things:

  1. applies the commands the user typed: /shape /approve /review /pin /hint /skip
  2. remembers how the code stood when the turn began (for done_gate.py)
  3. gives the model one `[rep]` line of live state, when there is any
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

EVENT = "UserPromptSubmit"
COMMAND = re.compile(r"^\s*/(shape|approve|review|pin|hint|skip)(?![\w-])[ \t]*(.*)$", re.S)
OFF = "[rep] not enabled in this repo. /shape <goal> turns it on."


def main(ev):
    if ev.get("agent_id"):
        return
    prompt = ev.get("prompt") or ""
    cwd = ev.get("cwd") or os.getcwd()
    match = COMMAND.match(prompt)
    name, args = (match.group(1), match.group(2).strip()) if match else (None, "")

    root, notes = replib.find_root(cwd), []
    if not root:
        if name == "shape" and args:
            root, notes = replib.init_root(cwd)
        elif name:
            replib.add_context(EVENT, OFF)
        else:
            return

    with replib.state_tx(root) as state:
        replib.refresh_region_files(root, state)
        if name == "shape":
            notes.append(replib.shape(root, state, args))
        elif name == "approve":
            notes.append(replib.approve(root, state, args))
        elif name == "review":
            notes.append(replib.review(root, state, args))
        elif name == "pin":
            notes.append(replib.pin(root, state, args, cwd))
        elif name == "hint":
            notes.append(replib.hint(root, state))
        elif name == "skip":
            notes.append(replib.skip(root, state))
        if state["active"] and state["base"]:
            state["turn_tree"] = replib.snapshot(root)
        showed_packet = name == "review" and bool(state["review_at"]) and not args
        if not showed_packet:
            notes.append(replib.status_line(root, state))

    text = "\n".join(n for n in notes if n)
    if text:
        replib.add_context(EVENT, text)


if __name__ == "__main__":
    replib.run(main)
