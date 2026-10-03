#!/usr/bin/env python3
"""UserPromptSubmit: the human channel.

This hook sees what the user typed, verbatim, before the model does. Claude
Code does not fire it when the model invokes a skill, so anything recorded
here came from the user's own keystrokes. It does three things:

  1. applies the commands the user typed: /dial /predict /skip /pin /hint /retro
  2. captures a prediction written inline as a line starting `predict:`
  3. gives the model one `[rep]` line of live state for this turn
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import replib  # noqa: E402

EVENT = "UserPromptSubmit"
COMMAND = re.compile(r"^\s*/(predict|retro|dial|skip|pin|hint)(?![\w-])[ \t]*(.*)$", re.S)
# `predict: ...` at the start of a line, up to the next blank line.
INLINE = re.compile(r"^[ \t]*predict(?:ion)?[ \t]*:[ \t]*(.+?)(?=\n[ \t]*\n|\Z)", re.I | re.M | re.S)
MAX_INLINE_PROMPT = 4000   # do not go looking for predictions in pasted documents
MIN_GOAL = 40              # shorter leftovers are a reply, not a new task statement

OFF = "[rep] not enabled in this repo. The user turns it on with /dial dim (or `rep init` in a terminal)."
ASK = ("[rep] /predict with no text, so nothing was recorded. Ask the three prediction questions in one short "
       "message with no diagnosis; the user replies with a line starting `predict:`.")


def main(ev):
    if ev.get("agent_id"):
        return
    prompt = ev.get("prompt") or ""
    cwd = ev.get("cwd") or os.getcwd()
    session = ev.get("session_id")
    match = COMMAND.match(prompt)
    name, args = (match.group(1), match.group(2).strip()) if match else (None, "")

    root, notes, fresh = replib.find_root(cwd), [], False
    if not root:
        if name == "dial" and args.lower() in replib.DIALS:
            root, notes = replib.init_root(cwd, args.lower())
            fresh = True
        elif name:
            replib.add_context(EVENT, OFF)
        else:
            return

    with replib.state_tx(root) as state:
        notes.append(replib.expire_if_stale(root, state))
        replib.refresh_region_files(root, state)
        if state["last_session"] != session:
            state["last_session"] = session
            notes += session_notes(root, state)

        if name == "dial":
            notes.append("[rep] dial=%s. %s" % (state["dial"], replib.DIAL_MEANING[state["dial"]]) if fresh
                         else replib.set_dial(root, state, args))
        elif name == "predict":
            notes.append(predict(root, state, args, session))
        elif name == "skip":
            notes.append(replib.skip(root, state, args, session))
        elif name == "pin":
            notes.append(replib.pin(root, state, args, cwd))
        elif name == "hint":
            notes.append(replib.hint(root, state))
        elif name == "retro":
            notes.append(replib.retro_start(root, state, args, session))
        else:
            notes.append(plain(root, state, prompt, session))

        state["touched_at"] = replib.now()
        in_retro = bool(state["task"] and state["task"]["retro_at"])
        notes.append(None if (name == "retro" and in_retro) else replib.status_line(state))

    text = "\n".join(n for n in notes if n)
    if text:
        replib.add_context(EVENT, text)


def predict(root, state, args, session):
    if not args:
        state["work"]["asked_at"] = replib.now()
        return ASK
    if args.lower() == "skip":
        return replib.skip(root, state, "predict", session)
    return replib.record_prediction(root, state, args, "slash", session)


def plain(root, state, prompt, session):
    """An ordinary message: note it as the pending goal, and pick up an inline prediction."""
    found = INLINE.search(prompt) if len(prompt) <= MAX_INLINE_PROMPT else None
    text = found.group(1).strip() if found else ""
    rest = (prompt[:found.start()] + prompt[found.end():]).strip() if found else prompt.strip()

    work = state["work"]
    new_goal = not state["task"] and len(rest) >= MIN_GOAL
    if new_goal:
        work["goal"], work["goal_at"], work["asked_at"] = replib._clip(rest, 300), replib.now(), None
        work["goal_session"] = session
    if not text:
        return None
    if text.lower() in ("skip", "pass"):
        return replib.skip(root, state, "predict", session)
    return replib.record_prediction(root, state, text, "inline", session, waited=not new_goal)


def session_notes(root, state):
    notes = []
    lessons = [line for line in replib.read_text(os.path.join(root, "lessons.md")).splitlines()
               if line.startswith("- ")]
    if lessons:
        notes.append("[rep] lessons.md holds %d lesson(s) from earlier tasks in this repo. Read it before "
                     "non-trivial work here." % len(lessons))
    if state["dial"] == "dark":
        notes.append("[rep] dial=dark: ghostwriter mode, no reps required. When a task wraps up you may offer "
                     "/retro once.")
    task = state["task"]
    if task and task.get("session") not in (None, state["last_session"]):
        notes.append("[rep] task %s is still open from an earlier session. If this session is new work, the user "
                     "closes it with /retro or /retro skip." % task["id"])
    return notes


if __name__ == "__main__":
    replib.run(main)
