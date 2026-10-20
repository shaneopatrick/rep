"""rep: shared library for the hooks and the `rep` CLI.

rep's unit of work is the brief: a one-screen contract for one piece of work.
You shape and approve it; agents work inside it. Everything that reads or
writes <repo>/.rep/ goes through this module. Stdlib only, Python 3.8+.
Hooks run on every tool call, so modules that only some paths need are
imported where they are used.

<repo>/.rep/
  .gitignore    "*": nothing in here is ever committed, from any worktree
  briefs/       one markdown file per brief, plus draft.md while shaping
  state.json    active brief, base snapshot, check runs, pins, counters
  reps.jsonl    one line per closed brief (and any Phase 0 reps)
  events.jsonl  audit trail: approvals, budget trips, refused edits, ...
  keep/         pre-images of protected files changed through Bash

Who changes what: the brief's contract changes only when the user types
/approve. The agent drafts (`rep brief draft`), notes (`rep note`) and runs
the check (`rep check`). bash_guard.py keeps the user's commands the user's.
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
STYLE = "rep-tutor"
MAX_BRIEF_LINES = 40           # the contract has to fit on one screen
MAX_TODOS = 2                  # TODO(human) regions per brief
MAX_SCAN_BYTES = 2000000
NOT_COUNTED = {"lessons.md"}   # codifying a lesson never trips the budget
MANIFESTS = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "pyproject.toml",
    "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg", "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "composer.lock",
}
HINTS = (
    ("nudge", "a question or pointer that directs attention; no concept names, no code"),
    ("concept", "name the concept or pattern and why it applies here; no code"),
    ("pseudocode", "the approach as plain-language steps; no real syntax"),
    ("partial code", "real code for the scaffolding, with the crux left blank"),
    ("full solution", "the complete solution, shown in chat only; the user still types it"),
)
PREMORTEM_PLACEHOLDER = "(yours: add it with /approve <where you expect this to go wrong>)"


# --- time -------------------------------------------------------------------

def now():
    return int(time.time())


def iso(ts=None):
    import datetime
    ts = time.time() if ts is None else ts
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(ts=None):
    return time.strftime("%m-%d %H:%M", time.localtime(now() if ts is None else ts))


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


def ensure_ignored(root):
    """A .gitignore of "*" inside .rep/ ignores the folder from every worktree,
    whatever the repo's own ignore rules say."""
    path = rep_path(root, ".gitignore")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("*\n")


def init_root(start):
    """Turn rep on for the repo containing `start`. Returns (root, notes)."""
    root = git_top(start) or _existing_dir(start)
    os.makedirs(rep_path(root, "briefs"), exist_ok=True)
    ensure_ignored(root)
    notes = ["[rep] enabled in %s (state lives in .rep/, never committed)." % root]
    if not os.path.exists(rep_path(root, "state.json")):
        save_state(root, new_state())

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
    else:
        notes.append("[rep] output style is %r here; left as is. /output-style %s gives the brief-aware voice."
                     % (local["outputStyle"], STYLE))
    return root, notes


# --- state ------------------------------------------------------------------

def new_state():
    return {
        "v": 2,
        "active": None,        # briefs/<file>.md once approved
        "draft": None,         # briefs/draft.md while shaping or revising
        "asked": None,         # the user's /shape words, verbatim
        "base": None,          # snapshot of the code at /approve; the budget is measured from it
        "approved_at": None,
        "review_at": None,
        "runs": [],            # `rep check` history: at, ok, secs, tree, code
        "flakes": 0,
        "turn_tree": None,     # state of the code when the user's last prompt arrived
        "trips": 0,            # budget breaker trips
        "refused": 0,          # edits refused for scope or dependencies
        "over_reported": 0,
        "pins": {},
        "region_files": [],
        "todos": {},
        "hints": 0,
        "todo_released": False,
        "violations": 0,
    }


def load_state(root):
    """Read state.json. Phase 0 state (v1) and unreadable files read as fresh."""
    state = new_state()
    try:
        with open(rep_path(root, "state.json")) as f:
            saved = json.load(f)
        if isinstance(saved, dict) and saved.get("v") == 2:
            state.update(saved)
    except (OSError, ValueError):
        pass
    return state


def _atomic_write(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save_state(root, state):
    os.makedirs(rep_path(root), exist_ok=True)
    ensure_ignored(root)
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


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# --- files ------------------------------------------------------------------

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
    """The file's text after this edit, or None when that cannot be worked out."""
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


# --- measuring the work against its base -------------------------------------

def git(root, *args):
    """(exit code, stdout) for a git command in `root`; (1, "") if git fails."""
    import subprocess
    try:
        proc = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.decode("utf-8", "surrogateescape")


def snapshot(root):
    """The working tree as a git tree object, untracked files included.

    Uses a private index in .rep/, so the user's index, branch and stash are
    never touched. Equal snapshots mean identical code. Measuring the budget
    from the snapshot taken at /approve means work that was already sitting
    uncommitted never counts against a new brief. None outside git.
    """
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    import subprocess
    env = dict(os.environ, GIT_INDEX_FILE=rep_path(root, "snapshot.index"))
    with open(rep_path(root, ".snaplock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        out = b""
        for args in (["add", "-A"], ["write-tree"]):
            try:
                proc = subprocess.run(["git", "-C", root] + args, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.SubprocessError):
                return None
            if proc.returncode != 0:
                return None
            out = proc.stdout
    return out.decode().strip() or None


def _noise(path):
    return "__pycache__/" in path or path.endswith(".pyc")


def changes(root, base, current=None):
    """{path: (added, deleted)} between the `base` snapshot and the code now."""
    current = current or snapshot(root)
    out = {}
    if not base or not current:
        return out
    _, text = git(root, "diff", "--numstat", "--no-renames", base, current)
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and not _noise(parts[2]):
            added, deleted, path = parts
            out[path] = (int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0)
    return out


def size(change_map):
    """(files, lines) changed, leaving out lessons.md."""
    counted = [v for p, v in change_map.items() if p not in NOT_COUNTED]
    return len(counted), sum(a + d for a, d in counted)


def line_delta(old, new):
    """(added, deleted) lines between two texts."""
    import difflib
    added = deleted = 0
    in_hunk = False
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            deleted += 1
    return added, deleted


def base_text(root, base, relpath):
    code, out = git(root, "show", "%s:%s" % (base, relpath))
    return out if code == 0 else ""


# --- briefs -----------------------------------------------------------------
#
# A brief is markdown with fixed sections. Goal, Approach, Budget and Check
# are the contract: the agent drafts them, the user approves them. Pre-mortem
# (the user's words) and Log (notes and harness facts) are written by rep.

CONTRACT = ("goal", "approach", "budget", "check")
_SECTION = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
_FIELD = re.compile(r"^[ \t]*[-*]?[ \t]*(lines|files|paths|deps|cmd|unverified)[ \t]*:[ \t]*(.*?)[ \t]*$", re.I | re.M)


def parse_brief(text):
    title = re.search(r"^#[ \t]+(?:Brief:[ \t]*)?(.+?)[ \t]*$", text, re.M)
    asked = re.search(r"^Asked:[ \t]*\"?(.*?)\"?[ \t]*$", text, re.M)
    parts = _SECTION.split(text)
    sections, order = {}, []
    for i in range(1, len(parts), 2):
        name = parts[i].strip().lower()
        sections[name] = parts[i + 1].strip("\n")
        order.append(name)
    fields = {}
    for name in ("budget", "check"):
        for m in _FIELD.finditer(sections.get(name, "")):
            fields[m.group(1).lower()] = m.group(2)
    return {"title": title.group(1) if title else "", "asked": asked.group(1) if asked else "",
            "sections": sections, "order": order, "fields": fields}


def budget_of(brief):
    fields = brief["fields"]

    def number(key):
        m = re.match(r"\d+", fields.get(key, ""))
        return int(m.group()) if m else None

    paths = [p.strip().rstrip("/") for p in re.split(r"[,\s]+", fields.get("paths", "")) if p.strip()]
    return {"lines": number("lines"), "files": number("files"), "paths": paths,
            "deps": fields.get("deps", "no").strip().lower() in ("yes", "y", "true", "allowed")}


def check_cmd(brief):
    return brief["fields"].get("cmd", "").strip("` ") or None


def problems(brief):
    """What stops a draft from being a usable contract."""
    issues = []
    if not brief["title"]:
        issues.append("a '# Brief: <title>' line")
    for name in CONTRACT:
        if not brief["sections"].get(name, "").strip():
            issues.append("a '## %s' section" % name.capitalize())
    budget = budget_of(brief)
    if budget["lines"] is None or budget["files"] is None:
        issues.append("'lines: <n>' and 'files: <n>' under Budget")
    if not check_cmd(brief):
        issues.append("'cmd: <command>' under Check")
    contract_lines = 1 + sum(len(brief["sections"].get(name, "").strip().splitlines()) + 1 for name in CONTRACT)
    if contract_lines > MAX_BRIEF_LINES:
        issues.append("fewer lines: %d, and the limit is %d so it fits on one screen" % (contract_lines, MAX_BRIEF_LINES))
    return issues


def render_brief(brief, premortem, log, asked):
    out = ["# Brief: %s" % brief["title"]]
    if asked:
        out.append("Asked: \"%s\"" % asked)
    extras = [n for n in brief["order"] if n not in CONTRACT + ("pre-mortem", "log")]
    for name in CONTRACT + tuple(extras):
        out += ["", "## %s" % ("Pre-mortem" if name == "pre-mortem" else name.capitalize()),
                brief["sections"].get(name, "").strip()]
    out += ["", "## Pre-mortem", premortem.strip(), "", "## Log", log.strip()]
    return "\n".join(out).rstrip() + "\n"


def load_brief(root, relpath):
    return parse_brief(read_text(rep_path(root, relpath))) if relpath else None


def append_log(root, state, line):
    brief = load_brief(root, state["active"])
    if not brief:
        return
    log = (brief["sections"].get("log", "") + "\n- %s %s" % (stamp(), line)).strip()
    write_text(rep_path(root, state["active"]),
               render_brief(brief, brief["sections"].get("pre-mortem", ""), log, brief["asked"]))


def _slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "brief"


def shape(root, state, args):
    """The user typed /shape <goal>."""
    words = args.strip()
    if words.lower() == "drop":
        if not state["draft"]:
            return "[rep] no draft to drop."
        try:
            os.remove(rep_path(root, state["draft"]))
        except OSError:
            pass
        state["draft"] = None
        event(root, "drop_draft")
        return "[rep] draft dropped by the user."
    if state["active"]:
        title = load_brief(root, state["active"])["title"]
        event(root, "shape", revision=True)
        return ("[rep] brief \"%s\" is active, so this is a revision request: \"%s\". Draft the whole brief again "
                "with the change using `rep brief draft`, say what changed, and keep working under the current "
                "brief until the user approves it." % (title, _clip(words, 300)))
    if not words:
        return "[rep] usage: /shape <what you want done>"
    state.update(asked=_clip(words, 300), runs=[], flakes=0)
    event(root, "shape", revision=False)
    return ("[rep] shaping, at the user's request: \"%s\". Follow the shape skill. Write the draft with "
            "`rep brief draft`; no code edits until the user approves it." % _clip(words, 300))


def draft(root, state, text):
    """The agent wrote a draft. Returns (message, ok)."""
    brief = parse_brief(text)
    missing = problems(brief)
    if missing:
        return "rep: draft not saved. It needs " + "; ".join(missing) + ".", False
    active = load_brief(root, state["active"])
    asked = active["asked"] if active else (state["asked"] or "")
    os.makedirs(rep_path(root, "briefs"), exist_ok=True)
    write_text(rep_path(root, "briefs", "draft.md"), render_brief(brief, PREMORTEM_PLACEHOLDER, "", asked))
    state["draft"] = "briefs/draft.md"
    event(root, "draft", revision=bool(active))
    budget = budget_of(brief)
    kind = "Revision" if active else "Draft"
    return ("%s saved: \"%s\". Budget %d lines / %d files, check `%s`. Ask the user to approve with "
            "/approve <their pre-mortem> or to say what to change. Do not edit code until then."
            % (kind, brief["title"], budget["lines"], budget["files"], check_cmd(brief))), True


def approve(root, state, premortem):
    """The user typed /approve [pre-mortem]."""
    if not state["draft"]:
        return "[rep] nothing to approve: there is no draft brief. /shape <goal> starts one."
    proposed = load_brief(root, state["draft"])
    missing = problems(proposed)
    if missing:
        return "[rep] the draft can't be approved yet. It needs " + "; ".join(missing) + "."
    premortem = premortem.strip()
    active = load_brief(root, state["active"])
    if active:
        changed = [n.capitalize() for n in CONTRACT
                   if proposed["sections"].get(n, "").split() != active["sections"].get(n, "").split()]
        log = active["sections"].get("log", "") + "\n- %s revision approved by the user: %s changed" % (
            stamp(), ", ".join(changed) or "nothing")
        write_text(rep_path(root, state["active"]), render_brief(
            proposed, premortem or active["sections"].get("pre-mortem", ""), log, active["asked"]))
        message = ("[rep] revision approved by the user (%s changed). Keep working inside the brief."
                   % (", ".join(changed) or "nothing"))
    else:
        base = snapshot(root)
        name = "%s-%s.md" % (time.strftime("%Y%m%d"), _slug(proposed["title"]))
        while os.path.exists(rep_path(root, "briefs", name)):
            name = name[:-3] + "-2.md"
        log = "- %s approved by the user (base %s)" % (stamp(), base[:7] if base else "none: not a git repo")
        write_text(rep_path(root, "briefs", name), render_brief(proposed, premortem or "(none)", log, state["asked"]))
        state.update(active="briefs/" + name, base=base, approved_at=now(), review_at=None, trips=0, refused=0,
                     over_reported=0, todos={}, hints=0, todo_released=False)
        budget = budget_of(proposed)
        message = ("[rep] brief approved by the user: \"%s\". Budget %d lines / %d files, check `%s`.%s Start the work."
                   % (proposed["title"], budget["lines"], budget["files"], check_cmd(proposed),
                      "" if base else " Not a git repo, so the budget can't be measured."))
    try:
        os.remove(rep_path(root, state["draft"]))
    except OSError:
        pass
    state["draft"] = None
    event(root, "approve", revision=bool(active), premortem=bool(premortem))
    return message


def facts(root, state):
    """What the harness knows about the active brief right now."""
    brief = load_brief(root, state["active"])
    budget = budget_of(brief)
    out = {"title": brief["title"], "cmd": check_cmd(brief), "budget": budget, "brief": brief,
           "files": None, "lines": None, "tree": None, "change_map": {}}
    if state["base"]:
        out["tree"] = snapshot(root)
        out["change_map"] = changes(root, state["base"], out["tree"])
        out["files"], out["lines"] = size(out["change_map"])
    runs = state["runs"]
    last = runs[-1] if runs else None
    if not last:
        out["check"] = "check not run yet"
    elif out["tree"] and last["tree"] != out["tree"]:
        out["check"] = "code changed since the last check run"
    else:
        out["check"] = "check %s on the current code" % ("passing" if last["ok"] else "failing")
    return out


def usage(f):
    if f["lines"] is None:
        return "budget not measured (no git)"
    return "budget %d/%d lines, %d/%d files" % (f["lines"], f["budget"]["lines"], f["files"], f["budget"]["files"])


def over_budget(budget, files, lines):
    return (budget["lines"] is not None and lines > budget["lines"]) or (
        budget["files"] is not None and files > budget["files"])


def in_scope(relpath, paths):
    import fnmatch
    return not paths or relpath in NOT_COUNTED or any(
        relpath == p or relpath.startswith(p + "/") or fnmatch.fnmatch(relpath, p) for p in paths)


def review(root, state, args):
    """The user typed /review, or /review accept|redirect|reject [note]."""
    if not state["active"]:
        return "[rep] no active brief to review."
    word, _, note = args.strip().partition(" ")
    word, note = word.lower(), note.strip()
    if word in ("accept", "reject"):
        return close(root, state, "accepted" if word == "accept" else "rejected", note)
    if word == "redirect":
        append_log(root, state, "redirected by the user: %s" % (note or "(no note)"))
        state["review_at"] = None
        event(root, "redirect")
        return "[rep] redirected by the user: \"%s\". Keep working inside the brief." % _clip(note, 300)
    if word:
        return "[rep] usage: /review, then /review accept | redirect <what to change> | reject"
    state["review_at"] = now()
    event(root, "review_start")
    return packet(root, state)


def packet(root, state):
    f = facts(root, state)
    runs = state["runs"]
    lines = ["[rep] review packet for \"%s\", measured by the harness rather than written by the model. Base %s."
             % (f["title"], (state["base"] or "none")[:7])]
    if runs:
        lines.append("Check `%s`: %d run(s); first run %s; %s; flaky runs: %d." % (
            f["cmd"], len(runs), "failed (red before green)" if not runs[0]["ok"] else "passed",
            f["check"], state["flakes"]))
    else:
        lines.append("Check `%s`: never run." % f["cmd"])
    if f["lines"] is not None:
        verdict = "over budget" if over_budget(f["budget"], f["files"], f["lines"]) else "within budget"
        lines.append("Size: %d file(s), %d line(s) changed; budget %d files / %d lines, %s. Breaker trips: %d. "
                     "Edits refused for scope or dependencies: %d." % (
                         f["files"], f["lines"], f["budget"]["files"], f["budget"]["lines"], verdict,
                         state["trips"], state["refused"]))
        listed = sorted(f["change_map"].items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:8]
        lines += ["  %s +%d -%d" % (p, a, d) for p, (a, d) in listed]
    premortem = f["brief"]["sections"].get("pre-mortem", "").strip()
    lines.append("Pre-mortem, written by the user before the work: %s" % (
        "\"%s\"" % _clip(premortem, 400) if premortem and premortem != "(none)" else "none."))
    unverified = f["brief"]["fields"].get("unverified")
    if unverified:
        lines.append("Not covered by the check, per the brief: %s" % unverified)
    lines.append("Follow the review skill, then ask the user to decide: /review accept, /review redirect "
                 "<what to change>, or /review reject.")
    return "\n".join(lines)


def close(root, state, outcome, note):
    f = facts(root, state)
    runs = state["runs"]
    last = runs[-1] if runs else None
    record = {
        "id": os.path.basename(state["active"])[:-3], "kind": "brief", "title": f["title"], "outcome": outcome,
        "note": note, "budget": {"lines": f["budget"]["lines"], "files": f["budget"]["files"]},
        "actual": {"lines": f["lines"], "files": f["files"]}, "trips": state["trips"], "refused": state["refused"],
        "check": {"runs": len(runs), "redFirst": (not runs[0]["ok"]) if runs else None, "flakes": state["flakes"],
                  "passingAtClose": bool(last and last["ok"] and last["tree"] == f["tree"])},
        "premortem": f["brief"]["sections"].get("pre-mortem", "").strip(), "hintsUsed": state["hints"],
        "todos": sorted(state["todos"]), "violations": state["violations"],
        "approvedAt": iso(state["approved_at"]) if state["approved_at"] else None, "createdAt": iso(),
        "durationS": now() - state["approved_at"] if state["approved_at"] else None,
    }
    _append(root, "reps.jsonl", record)
    append_log(root, state, "closed by the user: %s%s" % (outcome, ": " + note if note else ""))
    event(root, "close", brief=record["id"], outcome=outcome)
    if state["draft"]:
        try:
            os.remove(rep_path(root, state["draft"]))
        except OSError:
            pass
    fresh = new_state()
    fresh["region_files"] = state["region_files"]
    state.clear()
    state.update(fresh)
    return "[rep] brief \"%s\" closed by the user: %s. Pins cleared. The next piece of work starts with /shape." % (
        f["title"], outcome)


def record_run(root, state, ok, secs, tree, code):
    """Store one `rep check` result. Same code with a different result is a flake."""
    runs = state["runs"]
    flaky = any(r["tree"] == tree and r["ok"] != ok for r in runs)
    changed = not runs or runs[-1]["ok"] != ok
    runs.append({"at": now(), "ok": ok, "secs": round(secs, 2), "tree": tree, "code": code})
    del runs[:-30]
    if flaky:
        state["flakes"] += 1
    if state["active"] and changed:
        append_log(root, state, "check %s" % ("passing" if ok else "failing"))
    event(root, "check", ok=ok, secs=round(secs, 2), flaky=flaky)
    return flaky


def status_line(root, state):
    """The live state the agent sees on every user turn, or None."""
    parts = []
    if state["active"]:
        f = facts(root, state)
        parts = ["brief \"%s\"" % f["title"], usage(f), f["check"]]
        if state["draft"]:
            parts.append("a revision draft is waiting for the user's /approve")
        if state["review_at"]:
            parts.append("review in progress")
        line = "[rep] " + " · ".join(parts) + ". Run the check with `rep check`; record decisions with `rep note`."
    elif state["draft"]:
        title = load_brief(root, state["draft"])["title"]
        line = "[rep] draft brief \"%s\" is waiting for the user's /approve. Don't edit code until then." % title
    else:
        line = None
    extras = []
    if state["region_files"] and not state["todo_released"]:
        extras.append("TODO(human) open in %s (the user's to write; hints %d/%d)" % (
            ", ".join(state["region_files"]), state["hints"], len(HINTS)))
    if state["pins"]:
        extras.append("pinned, read-only for you: %s" % ", ".join(sorted(state["pins"])))
    if extras:
        line = (line + " " if line else "[rep] ") + " · ".join(extras) + "."
    return line


def session_context(root, state):
    """Loaded at the start of every session, after /clear and after compaction."""
    out = []
    lessons = [l for l in read_text(os.path.join(root, "lessons.md")).splitlines() if l.startswith("- ")]
    if lessons:
        out.append("[rep] lessons.md holds %d lesson(s) from earlier work in this repo. Read it before "
                   "non-trivial work here." % len(lessons))
    if state["active"]:
        text = read_text(rep_path(root, state["active"])).rstrip()
        head_part, _, log = text.partition("\n## Log")
        entries = [l for l in log.splitlines() if l.startswith("- ")][-8:]
        out.append("[rep] Active brief: the user's contract for the current work. You may not edit it; propose "
                   "changes with `rep brief draft`.\n\n" + head_part.rstrip() + "\n\n## Log (latest)\n"
                   + "\n".join(entries))
    line = status_line(root, state)
    if line:
        out.append(line)
    return "\n\n".join(out) or None


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


def refresh_region_files(root, state):
    """Rebuild the list of files that hold regions: the ones already known
    plus whatever git can find, so regions typed by hand are covered too."""
    candidates = set(state.get("region_files") or [])
    if os.path.exists(os.path.join(root, ".git")):
        _, out = git(root, "grep", "-l", "-I", "--untracked", "-F", "TODO(human)")
        candidates.update(line for line in out.splitlines() if line)
    state["region_files"] = sorted(
        r for r in candidates
        if not r.startswith(REP_DIR + "/") and regions(read_text(os.path.join(root, r))))


def skip(root, state):
    """The user typed /skip: release this brief's TODO(human) regions."""
    if not state["region_files"] or state["todo_released"]:
        return "[rep] no open TODO(human) to release."
    state["todo_released"] = True
    event(root, "skip", what="todo")
    return ("[rep] TODO(human) released by the user (logged, no penalty). You may now complete the marked "
            "regions and remove the markers.")


def hint(root, state):
    if not state["region_files"]:
        return "[rep] no open TODO(human) region, so there is nothing to hint."
    state["hints"] = min(len(HINTS), state["hints"] + 1)
    name, meaning = HINTS[state["hints"] - 1]
    event(root, "hint", level=state["hints"])
    return ("[rep] hint %d/%d pulled by the user: %s. Give exactly this level for the open TODO(human) "
            "and nothing beyond it: %s." % (state["hints"], len(HINTS), name, meaning))


# --- pins and protected files -------------------------------------------------

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
        event(root, "pin", files=sorted(set(added)))
    msg = "[rep] pinned by the user: %s. These files are read-only for you until the brief closes." % (
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


def protected_files(state):
    """Map of relpath -> 'region' | 'pin' for what a Bash call must not change."""
    files = {}
    if not state["todo_released"]:
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


def tell_user(text):
    print(json.dumps({"systemMessage": text}))
    sys.exit(0)


def run(main):
    """Entry point for every hook script. A hook that crashes inside a
    rep-enabled repo denies the tool call: failing open would be a silent
    bypass."""
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
