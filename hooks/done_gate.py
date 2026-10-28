#!/usr/bin/env python3
"""Stop: done means the check passes on the current code.

Only turns that changed code are judged, so a turn that just answers a
question never gets held up. Each nudge below happens at most once per stop
(Claude Code's stop_hook_active guards against loops).

- Changed code the check has not run on: run `rep check` before handing back.
  Recorded as skipped verification.
- A simplify pass left open: finish it.
- Otherwise you get one line, measured by the harness rather than written by
  the model: the check's result on the current code and the budget used.

The simplify pass itself runs at the start of /review. Claude Code tells the
model to start subagents only when the user asks, and /review is the user
asking; a reminder from a hook is not, so the model would simplify its own
work instead of handing it to fresh eyes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

UNVERIFIED = (
    "rep: you changed code this turn and the check has not run on it. Run `rep check` before handing back. If it "
    "cannot run, say why in one line."
)
OPEN_PASS = "rep: the simplify pass is still open. Make sure `rep check` passes, then run `rep simplify done`."


def main(ev):
    root = replib.session_root(ev)
    if not root:
        return
    state = replib.load_state(root)
    if not state["active"] or not state["base"]:
        return
    f = replib.facts(root, state)
    if f["tree"] == state["turn_tree"]:
        return
    again = bool(ev.get("stop_hook_active"))

    if not state["runs"] or state["runs"][-1]["tree"] != f["tree"]:
        if again:
            replib.event(root, "unverified_stop")
            replib.tell_user("rep: this turn changed code that the check has not run on · " + replib.usage(f))
        with replib.state_tx(root) as state:
            replib.record_mistake(root, state, "skipped-verification", "harness",
                                  "stopped with code the check had not run on", once=True)
        replib.event(root, "stop_nudge")
        replib.add_context("Stop", UNVERIFIED)

    simplify = state["simplify"]
    if not again and simplify and simplify["phase"] == "running":
        replib.add_context("Stop", OPEN_PASS)

    summary = "rep: %s · %s" % (f["check"], replib.usage(f))
    if simplify and simplify["phase"] == "done":
        summary += " · simplified %d -> %d" % (simplify["before"], simplify["after"])
    replib.tell_user(summary)


if __name__ == "__main__":
    replib.run(main)
