#!/usr/bin/env python3
"""Link rep into ~/.claude and register its hooks.

  python3 install.py              install, or repair an existing install
  python3 install.py --uninstall  remove the links and the settings entries

Everything is a symlink back into this repo, so editing a file here changes
the live harness. settings.json is backed up before the first change. Nothing
here touches any repo's .rep/ directory.
"""
import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.realpath(__file__))
CLAUDE = os.path.expanduser("~/.claude")
SETTINGS = os.path.join(CLAUDE, "settings.json")
LOCAL_BIN = os.path.expanduser("~/.local/bin")

HOOKS = ("replib", "on_prompt", "session_start", "budget_gate", "lock_tests", "guard_human", "bash_guard",
         "bash_audit", "done_gate", "predict_gate")
SKILLS = ("shape", "approve", "review", "pin", "hint", "skip")
# Retired with the move to briefs. Their links are removed and their settings entries stripped.
RETIRED_SKILLS = ("predict", "retro", "dial")
LINKS = (
    [("hooks/%s.py" % h, os.path.join(CLAUDE, "hooks", h + ".py")) for h in HOOKS]
    + [("skills/%s" % s, os.path.join(CLAUDE, "skills", s)) for s in SKILLS]
    + [("output-styles/rep-tutor.md", os.path.join(CLAUDE, "output-styles", "rep-tutor.md")),
       ("bin/rep", os.path.join(CLAUDE, "bin", "rep")),
       ("bin/rep", os.path.join(LOCAL_BIN, "rep"))]
)

EDITS = "Edit|Write|MultiEdit|NotebookEdit"
# (event, matcher, hook scripts). A failing Bash command fires PostToolUseFailure
# rather than PostToolUse, so the audit is registered on both.
REGISTRATIONS = (
    ("UserPromptSubmit", None, ("on_prompt",)),
    ("SessionStart", None, ("session_start",)),
    ("PreToolUse", EDITS, ("budget_gate", "lock_tests", "guard_human")),
    ("PreToolUse", "Bash", ("bash_guard",)),
    ("PostToolUse", "Bash", ("bash_audit",)),
    ("PostToolUseFailure", "Bash", ("bash_audit",)),
    ("Stop", None, ("done_gate",)),
)
# The agent's own rep commands, so working inside a brief never stops on a permission prompt.
PERMISSIONS = ("Bash(rep status:*)", "Bash(rep log:*)", "Bash(rep report:*)", "Bash(rep restore:*)",
               "Bash(rep check:*)", "Bash(rep note:*)", "Bash(rep brief:*)")


def command(script):
    # A missing script must not block: Python exits 2 when it can't open a file, and
    # Claude Code reads exit 2 as "block". -S skips site-packages (stdlib only, ~15 ms).
    return 'f="$HOME/.claude/hooks/%s.py"; [ -f "$f" ] || exit 0; exec python3 -S "$f"' % script


def is_ours(handler):
    return "/.claude/hooks/" in handler.get("command", "") and any(
        "/%s.py" % h in handler["command"] for h in HOOKS)


def link(src, dst):
    src = os.path.join(REPO, src)
    if os.path.islink(dst):
        if os.path.realpath(dst) == os.path.realpath(src):
            return "ok      %s" % dst
        os.remove(dst)
    elif os.path.exists(dst):
        return "SKIPPED %s exists and is not a rep link; move it aside and re-run" % dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.symlink(src, dst)
    return "linked  %s" % dst


def unlink(src, dst):
    if os.path.islink(dst) and os.path.realpath(dst) == os.path.realpath(os.path.join(REPO, src)):
        os.remove(dst)
        return "removed %s" % dst
    return None


def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except OSError:
        return {}


def strip_ours(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if not is_ours(h)]
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    allow = settings.get("permissions", {}).get("allow")
    if allow is not None:
        settings["permissions"]["allow"] = [rule for rule in allow if rule not in PERMISSIONS]
        if not settings["permissions"]["allow"]:
            del settings["permissions"]["allow"]
        if not settings["permissions"]:
            del settings["permissions"]


def add_ours(settings):
    hooks = settings.setdefault("hooks", {})
    for event, matcher, scripts in REGISTRATIONS:
        group = {"hooks": [{"type": "command", "command": command(s), "timeout": 10} for s in scripts]}
        if matcher:
            group = dict(matcher=matcher, **group)
        hooks.setdefault(event, []).append(group)
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    allow.extend(rule for rule in PERMISSIONS if rule not in allow)


def write_settings(settings, before):
    after = json.dumps(settings, indent=2) + "\n"
    if after == before:
        return "ok      %s" % SETTINGS
    backup = SETTINGS + ".before-rep"
    if os.path.exists(SETTINGS) and not os.path.exists(backup):
        shutil.copy2(SETTINGS, backup)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        f.write(after)
    os.replace(tmp, SETTINGS)
    return "updated %s (original kept at %s)" % (SETTINGS, backup)


def main():
    uninstall = "--uninstall" in sys.argv[1:]
    try:
        with open(SETTINGS) as f:
            before = f.read()
    except OSError:
        before = ""
    settings = load_settings()
    strip_ours(settings)

    for name in RETIRED_SKILLS:
        dst = os.path.join(CLAUDE, "skills", name)
        if os.path.islink(dst) and os.path.realpath(dst).startswith(REPO + os.sep):
            os.remove(dst)
            print("removed %s (retired)" % dst)

    if uninstall:
        for src, dst in LINKS:
            print(unlink(src, dst) or "absent  %s" % dst)
        print(write_settings(settings, before))
        print("\nrep is uninstalled. Each repo's .rep/ folder and lessons.md are untouched.")
        return

    for src, dst in LINKS:
        if dst.startswith(LOCAL_BIN) and not os.path.isdir(LOCAL_BIN):
            continue
        print(link(src, dst))
    add_ours(settings)
    print(write_settings(settings, before))

    on_path = any(os.path.realpath(p) == os.path.realpath(os.path.dirname(dst))
                  for p in os.environ.get("PATH", "").split(os.pathsep) if p
                  for _, dst in LINKS[-2:])
    if not on_path:
        print("\nNOTE: add ~/.claude/bin to your PATH so `rep` resolves:  export PATH=\"$HOME/.claude/bin:$PATH\"")
    print("\nrep is installed. In any repo, type /shape <goal> inside Claude Code.")
    print("Check any time with:        rep doctor")


if __name__ == "__main__":
    main()
