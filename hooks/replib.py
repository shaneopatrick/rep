"""rep: shared library for the Phase 0 hooks and the `rep` CLI.

Everything that reads or writes <repo>/.rep/ goes through this module, so
state changes in exactly one place. Stdlib only; runs on Python 3.8+. Hooks run on every tool call, so modules
that only some paths need are imported where they are used.

<repo>/.rep/
  state.json    dial, open task, pins, counters for the work in progress
  reps.jsonl    one line per finished rep (the Phase 1 seed data)
  events.jsonl  audit trail: gate denials, blocked edits, dial changes, ...
  keep/         pre-images of protected files that changed during a Bash call

Who may change state: hooks acting on something the user typed, and the user
running `rep` in a terminal. The agent's own surface is `rep status`,
`rep report`, `rep restore` and `rep log retro` (only after the user typed
/retro). bash_guard.py enforces that split.
"""
import fcntl
import json
import os
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager

REP_DIR = ".rep"
DIALS = ("dark", "dim", "lit")
STYLE = "rep-tutor"
OUTCOMES = ("match", "partial", "miss")
STALE_SECONDS = 12 * 3600      # an untouched task closes itself, ungraded
MAX_TODOS = 2                  # friction budget: TODO(human) regions per task
MAX_WAIT = 900                 # longer gaps before a prediction are not counted as overhead
MAX_SCAN_BYTES = 2000000
HINTS = (
    ("nudge", "a question or pointer that directs attention; no concept names, no code"),
    ("concept", "name the concept or pattern and why it applies here; no code"),
    ("pseudocode", "the approach as plain-language steps; no real syntax"),
    ("partial code", "real code for the scaffolding, with the crux left blank"),
    ("full solution", "the complete solution, shown in chat only; the user still types it"),
)
DIAL_MEANING = {
    "dark": "Ghostwriter mode: no prediction needed, pinned tests unlocked, open TODO(human) regions released.",
    "dim": "One prediction before edits, a retro at the end.",
    "lit": "Prediction before edits, tests the user wrote or approved and pinned (/pin), "
           "one TODO(human) on core logic, a retro at the end.",
}


# --- time -------------------------------------------------------------------

def now():
    return int(time.time())


def iso(ts=None):
    import datetime
    ts = time.time() if ts is None else ts
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(ts):
    if not ts:
        return "?"
    minutes = max(0, now() - ts) // 60
    return "%dm" % minutes if minutes < 120 else "%dh" % (minutes // 60)


# --- locating the repo ------------------------------------------------------

def _existing_dir(path):
    p = os.path.realpath(path)
    while not os.path.isdir(p):
        p = os.path.dirname(p)
    return p


def find_root(path):
    """Nearest ancestor of `path` that holds .rep/, without leaving its git repo.

    Stopping at the git boundary means a .rep/ in a parent folder of several
    repos never switches rep on for the repos beneath it.
    """
    p = _existing_dir(path)
    while True:
        if os.path.isdir(os.path.join(p, REP_DIR)):
            return p
        if os.path.exists(os.path.join(p, ".git")):
            return None
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def git_top(path):
    p = _existing_dir(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def rep_path(root, *parts):
    return os.path.join(root, REP_DIR, *parts)


def rel(root, path):
    return os.path.relpath(os.path.realpath(path), root).replace(os.sep, "/")


def init_root(start, dial="dim"):
    """Turn rep on for the repo containing `start`. Returns (root, notes)."""
    root = git_top(start) or _existing_dir(start)
    os.makedirs(rep_path(root), exist_ok=True)
    notes = ["[rep] enabled in %s (state lives in .rep/)." % root]
    if not os.path.exists(rep_path(root, "state.json")):
        save_state(root, new_state(dial))

    info = os.path.join(root, ".git", "info")
    if os.path.isdir(os.path.join(root, ".git")):
        os.makedirs(info, exist_ok=True)
        exclude = os.path.join(info, "exclude")
        have = read_text(exclude)
        if ".rep/" not in have.split():
            with open(exclude, "a") as f:
                f.write(("" if have.endswith("\n") or not have else "\n") + ".rep/\n")

    settings = os.path.join(root, ".claude", "settings.local.json")
    try:
        with open(settings) as f:
            local = json.load(f)
    except (OSError, ValueError):
        local = None if os.path.exists(settings) else {}
    if local is None:
        notes.append("[rep] could not parse .claude/settings.local.json; run /output-style %s yourself." % STYLE)
    elif local.get("outputStyle") in (None, STYLE):
        local["outputStyle"] = STYLE
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        _atomic_write(settings, json.dumps(local, indent=2) + "\n")
        notes.append("[rep] output style set to %s for this repo (takes effect on the next message or session)." % STYLE)
    else:
        notes.append("[rep] output style is %r here; left as is. /output-style %s switches to the tutor voice."
                     % (local["outputStyle"], STYLE))
    return root, notes


# --- state ------------------------------------------------------------------

def new_work():
    """Counters for the work in progress. Reset whenever a task closes."""
    return {"goal": "", "goal_at": None, "goal_session": None, "asked_at": None, "denials": 0, "blocked": 0,
            "violations": 0, "hints": 0, "todos": {}, "todo_released": False}


def new_state(dial="dim"):
    return {"v": 1, "dial": dial, "seq": 0, "task": None, "work": new_work(),
            "pins": {}, "region_files": [], "last_session": None, "touched_at": None}


def load_state(root):
    """Read state.json. A missing or unreadable file reads as a fresh state,
    which has no open task, so the prediction gate fails closed."""
    state = new_state()
    try:
        with open(rep_path(root, "state.json")) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            state.update(saved)
    except (OSError, ValueError):
        pass
    if state["dial"] not in DIALS:
        state["dial"] = "dim"
    work = new_work()
    work.update(state.get("work") or {})
    state["work"] = work
    return state


def _atomic_write(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save_state(root, state):
    os.makedirs(rep_path(root), exist_ok=True)
    _atomic_write(rep_path(root, "state.json"), json.dumps(state, indent=2, sort_keys=True) + "\n")


@contextmanager
def state_tx(root):
    """Read-modify-write of state.json under a lock. Hooks for parallel tool
    calls run as separate processes, so every mutation goes through here."""
    os.makedirs(rep_path(root), exist_ok=True)
    fd = os.open(rep_path(root, ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = load_state(root)
        yield state
        save_state(root, state)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- logs -------------------------------------------------------------------

def _append(root, name, record):
    with open(rep_path(root, name), "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def event(root, kind, **fields):
    record = {"at": iso(), "event": kind}
    record.update(fields)
    _append(root, "events.jsonl", record)


def read_jsonl(root, name):
    rows = []
    try:
        with open(rep_path(root, name)) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


# --- TODO(human) regions ----------------------------------------------------
#
# A region runs from a comment line containing TODO(human) through the next
# comment line containing END TODO(human), both inclusive. A region with no
# END marker runs to the end of the file, which errs toward protecting more.
# The markers must open a comment line, so prose that merely mentions
# TODO(human) is not a region.

_LEAD = r"^[ \t]*(?:#|//|--|;|%|/\*+|<!--|\{/\*)[ \t]*"
_START = re.compile(_LEAD + r"TODO\(human\)")
_END = re.compile(_LEAD + r"END[ \t]+TODO\(human\)")


def regions(text):
    out, cur = [], None
    for line in text.splitlines(True):
        if cur is None:
            if _START.match(line):
                cur = [line]
        else:
            cur.append(line)
            if _END.match(line):
                out.append("".join(cur))
                cur = None
    if cur is not None:
        out.append("".join(cur))
    return out


def lost_regions(before, after):
    """Regions in `before` that are missing or altered in `after`."""
    return list((Counter(regions(before)) - Counter(regions(after))).elements())


def new_regions(before, after):
    return list((Counter(regions(after)) - Counter(regions(before))).elements())


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_SCAN_BYTES:
            return ""
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
            return f.read()
    except OSError:
        return ""


def write_text(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


def simulate(tool, tool_input, before):
    """The file's text after this edit, or None when that cannot be worked out.

    Checking the simulated result against the regions that exist now is
    simpler and stricter than reasoning about where old_string overlaps.
    """
    if tool == "Write":
        return tool_input.get("content") or ""
    if tool == "Edit":
        edits = [tool_input]
    elif tool == "MultiEdit":
        edits = tool_input.get("edits") or []
    else:
        return None
    text = before
    for edit in edits:
        old, new = edit.get("old_string") or "", edit.get("new_string") or ""
        if not old:
            if text:
                return None
            text = new
        elif old not in text:
            return None
        elif edit.get("replace_all"):
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
    return text


def refresh_region_files(root, state):
    """Rebuild the list of files that hold regions: the ones already known
    plus whatever git can find, so regions typed by hand are covered too."""
    candidates = set(state.get("region_files") or [])
    if os.path.exists(os.path.join(root, ".git")):
        import subprocess
        try:
            out = subprocess.run(
                ["git", "-C", root, "grep", "-l", "-I", "--untracked", "-F", "TODO(human)"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, universal_newlines=True)
            candidates.update(line for line in out.stdout.splitlines() if line)
        except (OSError, subprocess.SubprocessError):
            pass
    state["region_files"] = sorted(
        r for r in candidates
        if not r.startswith(REP_DIR + "/") and regions(read_text(os.path.join(root, r))))


# --- protected files during Bash ---------------------------------------------

def protected_files(state):
    """Map of relpath -> 'region' | 'pin' for what a Bash call must not change."""
    files = {}
    if state["dial"] == "dark":
        return files
    if not state["work"]["todo_released"]:
        for r in state.get("region_files") or []:
            files[r] = "region"
    for r in state.get("pins") or {}:
        files[r] = "pin"
    return files


def snap_path(tool_use_id):
    import tempfile
    folder = os.path.join(tempfile.gettempdir(), "rep-snap-%d" % os.getuid())
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return os.path.join(folder, re.sub(r"\W", "_", tool_use_id or "none") + ".json")


def keep_path(root, relpath):
    import hashlib
    return rep_path(root, "keep", hashlib.sha1(relpath.encode()).hexdigest()[:16] + ".pre")


# --- tasks and reps ---------------------------------------------------------

def open_task(state, session):
    state["seq"] += 1
    work = state["work"]
    if work["goal_session"] != session:
        # Sessions sharing a checkout share one task, but not each other's words.
        work["goal"], work["goal_at"], work["asked_at"] = "", None, None
    state["task"] = {
        "id": "t%04d" % state["seq"], "goal": work["goal"], "opened_at": now(),
        "started_at": work["goal_at"] or now(), "session": session, "dial": state["dial"],
        "predictions": [], "skipped": [], "retro_at": None,
    }
    return state["task"]


def record_prediction(root, state, text, via, session, waited=True):
    """Store the user's words verbatim. `waited` is False when the prediction
    arrived in the same message as the task, so it cost no extra turn."""
    task = state["task"] or open_task(state, session)
    work = state["work"]
    if task["predictions"] and task["predictions"][-1]["text"] == text:
        # An interrupted prompt that gets resent must not count twice.
        return "[rep] prediction #%d is already recorded. Edits are unlocked." % len(task["predictions"])
    asked = work["asked_at"] or work["goal_at"]
    wait = now() - asked if (waited and asked and not task["predictions"]) else 0
    if not 0 <= wait <= MAX_WAIT:
        wait = 0    # the user walked away; that is not time rep cost them
    task["predictions"].append({"text": text, "at": now(), "via": via, "wait_s": wait})
    n = len(task["predictions"])
    event(root, "predict", task=task["id"], n=n, via=via, wait_s=wait)
    return ("[rep] prediction #%d recorded verbatim by the harness: \"%s\". Edits are unlocked. "
            "Do not grade or correct it now. Once you have evidence, give a one-line prediction check."
            % (n, _clip(text, 400)))


def close_task(root, state, reason, retro=None):
    """Write the task's reps to reps.jsonl and reset for the next task.

    Rep lines follow the plan's Rep type (id, taskId, phase, skills,
    prediction, evidence, outcome, hintsUsed, codified, createdAt) plus a few
    fields Phase 0 wants to learn from.
    """
    task, work = state["task"], state["work"]
    retro = retro or {}
    closed = now()
    skills = retro.get("skills") or []
    base = {"taskId": task["id"], "dial": task["dial"], "skills": skills}

    def rep(suffix, **fields):
        row = {"id": "%s.%s" % (task["id"], suffix)}
        row.update(base)
        row.update({"prediction": "", "evidence": "", "outcome": None, "hintsUsed": 0, "createdAt": iso(closed)})
        row.update(fields)
        _append(root, "reps.jsonl", row)

    grades, evidence = retro.get("grades") or {}, retro.get("evidence") or {}
    for i, p in enumerate(task["predictions"], 1):
        rep("p%d" % i, kind="predict", phase="research", prediction=p["text"],
            evidence=evidence.get(i, ""), outcome=grades.get(i), gradedBy="user" if i in grades else None,
            via=p["via"], waitS=p["wait_s"], createdAt=iso(p["at"]))
    if "predict" in task["skipped"] and not task["predictions"]:
        rep("p0", kind="predict", phase="research", outcome="skipped")

    todo = retro.get("todo")
    if work["todos"] or todo or "todo" in task["skipped"]:
        released = work["todo_released"] or todo == "released"
        files = sorted(set(work["todos"]) | set(state.get("region_files") or []))
        rep("todo", kind="todo", phase="implement", evidence=", ".join(files),
            outcome="skipped" if released else ("match" if todo == "done" else None),
            hintsUsed=work["hints"], rating=retro.get("todo_rating"))

    retro_s = max(0, closed - task["retro_at"]) if task["retro_at"] else 0
    if reason == "retro":
        rep("retro", kind="retro", phase="codify", surprise=retro.get("surprise", ""),
            lesson=retro.get("lesson", ""), codified=retro.get("codified"))
    else:
        rep("retro", kind="retro", phase="codify", outcome="skipped")

    predict_s = sum(p["wait_s"] for p in task["predictions"])
    event(root, "task_close", task=task["id"], reason=reason, goal=task["goal"], dial=task["dial"],
          openedAt=iso(task["started_at"]), closedAt=iso(closed), durationS=max(0, closed - task["started_at"]),
          predictWaitS=predict_s, retroS=retro_s, overheadS=predict_s + retro_s,
          predictions=len(task["predictions"]), skipped=task["skipped"], denials=work["denials"],
          blocked=work["blocked"], violations=work["violations"], hints=work["hints"],
          todos=sorted(work["todos"]), pins=sorted(state["pins"]))
    state["task"], state["work"], state["pins"] = None, new_work(), {}
    return task["id"]


# --- commands the user types (prompt hook) or runs (CLI) ---------------------

def set_dial(root, state, value):
    value = (value or "").strip().lower()
    if value not in DIALS:
        return "[rep] usage: /dial dark|dim|lit (currently %s)." % state["dial"]
    old, state["dial"] = state["dial"], value
    event(root, "dial", old=old, new=value, task=(state["task"] or {}).get("id"))
    if value == "dark" and state.get("region_files") and not state["work"]["todo_released"]:
        # Dark means every gate is off. Leaving a region locked here would cost a
        # second command at exactly the moment the user is out of time.
        skip(root, state, "todo", None)
    return "[rep] dial: %s -> %s. %s" % (old, value, DIAL_MEANING[value])


def skip(root, state, what, session):
    what = (what or "").strip().lower()
    if not what:
        # Bare /skip skips whatever is pending, so the way out is one word under deadline.
        open_todo = state.get("region_files") and not state["work"]["todo_released"]
        what = "todo" if (state["task"] and open_todo) else "predict"
    if what == "todo":
        state["work"]["todo_released"] = True
        if state["task"] and "todo" not in state["task"]["skipped"]:
            state["task"]["skipped"].append("todo")
        event(root, "skip", what="todo", task=(state["task"] or {}).get("id"))
        return ("[rep] TODO(human) released by the user for this task (logged as skipped, no penalty). "
                "You may now complete the marked regions and remove the markers.")
    if what == "predict":
        if state["task"]:
            return ("[rep] nothing to skip: this task already has a prediction or a skip logged. "
                    "Other skips: /skip todo, /retro skip.")
        task = open_task(state, session)
        task["skipped"].append("predict")
        event(root, "skip", what="predict", task=task["id"])
        return "[rep] prediction skipped by the user (logged, no penalty). Edits are unlocked."
    return "[rep] usage: /skip [predict|todo]"


def hint(root, state):
    if not state.get("region_files"):
        return "[rep] no open TODO(human) region, so there is nothing to hint."
    work = state["work"]
    work["hints"] = min(len(HINTS), work["hints"] + 1)
    level = work["hints"]
    name, meaning = HINTS[level - 1]
    event(root, "hint", level=level, task=(state["task"] or {}).get("id"))
    return ("[rep] hint %d/%d pulled by the user: %s. Give exactly this level for the open TODO(human) "
            "and nothing beyond it: %s." % (level, len(HINTS), name, meaning))


def pin(root, state, args, base):
    """Pin test files: the agent may read and run them, never edit them."""
    import glob
    import hashlib
    import shlex
    try:
        wanted = shlex.split(args) if isinstance(args, str) else list(args)
    except ValueError:
        wanted = args.split()
    if not wanted:
        pins = sorted(state["pins"])
        return "[rep] pinned: %s" % (", ".join(pins) if pins else "nothing. Usage: /pin <test paths>")
    added, missing = [], []
    for arg in wanted:
        hits = []
        for start in (base, root):
            hits = glob.glob(os.path.join(start, arg), recursive=True)
            if hits:
                break
        files = []
        for h in hits:
            if os.path.isdir(h):
                files += [os.path.join(d, f) for d, _, fs in os.walk(h) for f in fs]
            else:
                files.append(h)
        files = [f for f in files if find_root(f) == root and "/%s/" % REP_DIR not in f]
        if not files:
            missing.append(arg)
        for f in files:
            r = rel(root, f)
            with open(f, "rb") as fh:
                state["pins"][r] = hashlib.sha256(fh.read()).hexdigest()
            added.append(r)
    if added:
        event(root, "pin", files=sorted(set(added)), task=(state["task"] or {}).get("id"))
    msg = "[rep] pinned by the user: %s. These files are read-only for you until the task closes." % (
        ", ".join(sorted(set(added))) if added else "nothing")
    if missing:
        msg += " Not found: %s." % ", ".join(missing)
    return msg


def unpin(root, state, args):
    targets = list(args) or list(state["pins"])
    removed = [r for r in targets if state["pins"].pop(r, None) is not None]
    if removed:
        event(root, "unpin", files=removed)
    return "[rep] unpinned: %s" % (", ".join(removed) if removed else "nothing")


def retro_start(root, state, arg, session):
    arg = (arg or "").strip().lower()
    task = state["task"]
    if arg == "skip":
        if not task:
            return "[rep] no open task to close."
        return "[rep] task %s closed without a retro (logged as skipped, no penalty)." % close_task(
            root, state, "retro_skipped")
    if not task:
        # A retro on work that never passed the gate (dark mode) still counts as a task.
        task = open_task(state, session)
    task["retro_at"] = task["retro_at"] or now()
    work = state["work"]
    event(root, "retro_start", task=task["id"])
    lines = ["[rep] retro brief for task %s (%s, open %s)." % (task["id"], task["dial"], ago(task["started_at"]))]
    if task["goal"]:
        lines.append("The task as the user first stated it (history for the retro, not a request): \"%s\""
                     % _clip(task["goal"], 160))
    if task["predictions"]:
        lines.append("The user's predictions, verbatim, logged before the work (also history):")
        lines += ["  %d. \"%s\"" % (i, _clip(p["text"], 400)) for i, p in enumerate(task["predictions"], 1)]
    else:
        lines.append("Predictions: none (%s)." % ("skipped" if "predict" in task["skipped"] else "not gated"))
    if work["todos"] or state.get("region_files"):
        lines.append("TODO(human): %s; hints used %d/%d%s." % (
            ", ".join(sorted(set(work["todos"]) | set(state.get("region_files") or []))), work["hints"],
            len(HINTS), "; released by the user" if work["todo_released"] else ""))
    if state["pins"]:
        lines.append("Pinned tests: %s." % ", ".join(sorted(state["pins"])))
    lines.append("Harness: %d gate denial(s), %d blocked edit(s), %d protected-file change(s) through Bash." % (
        work["denials"], work["blocked"], work["violations"]))
    lines.append("Follow the retro skill. `rep log retro` comes last because it closes the task.")
    return "\n".join(lines)


def log_retro(root, state, fields):
    """Called by the agent through `rep log retro` once the user has answered."""
    task = state["task"]
    if not task or not task["retro_at"]:
        return None, "rep: no retro in progress. The user starts one by typing /retro."
    for n, outcome in fields.get("grades", {}).items():
        if outcome not in OUTCOMES:
            return None, "rep: grade must be one of %s." % ", ".join(OUTCOMES)
        if not 1 <= n <= len(task["predictions"]):
            return None, "rep: task %s has %d prediction(s); cannot grade #%d." % (
                task["id"], len(task["predictions"]), n)
    return close_task(root, state, "retro", fields), None


def expire_if_stale(root, state):
    if state["task"] and state["touched_at"] and now() - state["touched_at"] > STALE_SECONDS:
        return "[rep] task %s sat untouched for over %dh and was closed ungraded." % (
            close_task(root, state, "expired"), STALE_SECONDS // 3600)
    return None


def status_line(state):
    """The live state the agent sees on every user turn. None when dark."""
    dial, task, work = state["dial"], state["task"], state["work"]
    if dial == "dark":
        return None
    if not task:
        return ("[rep] dial=%s. No prediction logged, so edits are locked. If this message starts a coding task, "
                "reply first with the prediction ask (one short message, no diagnosis, no plan). The user answers "
                "with a line starting `predict:` or types /skip. Questions and discussion need no prediction." % dial)
    parts = ["[rep] dial=%s" % dial, "task %s open %s" % (task["id"], ago(task["started_at"]))]
    if task["predictions"]:
        parts.append("%d prediction(s) logged" % len(task["predictions"]))
    elif "predict" in task["skipped"]:
        parts.append("prediction skipped")
    if state.get("region_files") and not work["todo_released"]:
        parts.append("TODO(human) open in %s (the user's to write; hints %d/%d)" % (
            ", ".join(state["region_files"]), work["hints"], len(HINTS)))
    if state["pins"]:
        parts.append("pinned, read-only for you: %s" % ", ".join(sorted(state["pins"])))
    elif dial == "lit":
        parts.append("lit and nothing pinned yet: the user owns the tests, so draft or request them "
                     "and have the user /pin them before you implement")
    if task["retro_at"]:
        parts.append("retro in progress")
    return " · ".join(parts) + "."


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# --- hook plumbing ----------------------------------------------------------

def target_path(ev):
    """Absolute path an edit tool is about to write, or None."""
    tool_input = ev.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(ev.get("cwd") or os.getcwd(), path)
    return os.path.realpath(path)


def session_root(ev):
    """Root for hooks that have no file path: where the shell is, else where
    the session started (a `cd` inside a Bash call must not lose the repo)."""
    for start in (ev.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if start:
            root = find_root(start)
            if root:
                return root
    return None


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    sys.exit(0)


def add_context(event_name, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}))
    sys.exit(0)


def block(reason):
    """PostToolUse feedback: the tool already ran, this puts the reason in front of the agent."""
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def run(main):
    """Entry point for every hook script. A hook that crashes inside a
    rep-enabled repo denies the tool call: failing open would be a silent
    bypass, and Phase 0 is judged on never having one."""
    ev = {}
    try:
        ev = json.load(sys.stdin)
        main(ev)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - last line of defence
        enabled = False
        try:
            path = target_path(ev)
            enabled = bool(find_root(path) if path else session_root(ev))
        except Exception:  # noqa: BLE001
            pass
        detail = "%s: %s" % (type(exc).__name__, exc)
        if enabled and ev.get("hook_event_name") == "PreToolUse":
            deny("rep hook %s crashed (%s) and is failing closed. Tell the user; `rep doctor` checks the install."
                 % (os.path.basename(sys.argv[0]), detail))
        sys.stderr.write("rep hook %s crashed: %s\n" % (os.path.basename(sys.argv[0]), detail))
        sys.exit(1)
