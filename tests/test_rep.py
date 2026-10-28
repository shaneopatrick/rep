"""Drives each hook the way Claude Code does: JSON on stdin, decision on stdout.

Payload shapes were captured from Claude Code 2.1.288 (see README).
Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HOOKS = os.path.join(REPO, "hooks")
REP = os.path.join(REPO, "bin", "rep")
sys.path.insert(0, HOOKS)
sys.path.insert(0, REPO)
import install  # noqa: E402
import replib  # noqa: E402

JOBS = "def claim(conn, worker):\n    row = select_pending(conn)\n    mark_running(conn, row)\n    return row\n\n"
STUB = '''def claim(conn, worker):
    # TODO(human): claim one job atomically; two workers must never get the same row
    raise NotImplementedError
    # END TODO(human)


def release(conn, job):
    return conn.execute("update jobs set status = 'free' where id = ?", (job,))
'''


TRADEOFF = "Tradeoff: favours simplicity and consistency; gives up throughput under heavy contention"


def brief_text(lines=20, files=2, paths="jobs.py, tests/", deps="no", cmd="grep -q atomic jobs.py",
               title="No job runs twice", tradeoff=TRADEOFF, assumptions=(), simplify=None):
    text = """# Brief: %s
## Goal
A job is never executed by two workers.
## Approach
Atomic claim with UPDATE ... RETURNING.
%s
Rejected: advisory locks (more moving parts); a queue library (new dependency)
""" % (title, tradeoff)
    if assumptions:
        text += "## Assumptions\n" + "".join("- %s\n" % a for a in assumptions)
    text += """## Budget
lines: %s
files: %s
paths: %s
deps: %s
%s## Check
cmd: %s
unverified: behaviour on Postgres
""" % (lines, files, paths, deps, "simplify: %s\n" % simplify if simplify else "", cmd)
    return text


class Harness(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="rep-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.git("init", "-q")
        self.write("jobs.py", JOBS)
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.session = "s1"
        self.n = 0

    # -- helpers --
    def git(self, *args):
        subprocess.run(["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
                       check=True, stdout=subprocess.DEVNULL)

    def write(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def read(self, name):
        with open(os.path.join(self.root, name)) as f:
            return f.read()

    def hook(self, script, payload):
        payload.setdefault("cwd", self.root)
        payload.setdefault("session_id", self.session)
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.root)
        proc = subprocess.run([sys.executable, os.path.join(HOOKS, script + ".py")], input=json.dumps(payload),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def say(self, prompt, **extra):
        """The user submits a prompt. Returns the context the model would see."""
        out = self.hook("on_prompt", dict({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, **extra))
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")

    def edit(self, script, tool, name, **tool_input):
        tool_input["file_path"] = os.path.join(self.root, name)
        out = self.hook(script, {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})
        spec = out.get("hookSpecificOutput", {})
        return spec.get("permissionDecision"), spec.get("permissionDecisionReason", "")

    def denied(self, script, tool, name, **tool_input):
        decision, reason = self.edit(script, tool, name, **tool_input)
        self.assertEqual(decision, "deny", "expected %s to deny" % script)
        return reason

    def allowed(self, script, tool, name, **tool_input):
        decision, reason = self.edit(script, tool, name, **tool_input)
        self.assertIsNone(decision, reason)

    def bash(self, command, effect=None):
        """Run the Pre hook, then `effect` (what the command does), then the Post hook."""
        self.n += 1
        base = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "toolu_%d" % self.n}
        pre = self.hook("bash_guard", dict(base, hook_event_name="PreToolUse"))
        if pre.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            return "deny", pre["hookSpecificOutput"]["permissionDecisionReason"]
        if effect:
            effect()
        post = self.hook("bash_audit", dict(base, hook_event_name="PostToolUse"))
        return post.get("decision"), post.get("reason", "")

    def stop(self, active=False):
        return self.hook("done_gate", {"hook_event_name": "Stop", "stop_hook_active": active,
                                       "last_assistant_message": "done"})

    def session_start(self, source="startup"):
        out = self.hook("session_start", {"hook_event_name": "SessionStart", "source": source})
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")

    def rep(self, *args, ok=True, stdin=None):
        proc = subprocess.run([sys.executable, REP] + list(args), cwd=self.root, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def draft(self, text=None, ok=True):
        return self.rep("brief", "draft", stdin=text or brief_text(), ok=ok)

    def activate(self, premortem="", **budget):
        self.say("/shape make claims atomic so no job runs twice")
        self.draft(brief_text(**budget))
        return self.say("/approve " + premortem)

    def state(self):
        return replib.load_state(self.root)

    def brief(self):
        return self.read(".rep/" + self.state()["active"])

    def events(self, kind):
        return [e for e in replib.read_jsonl(self.root, "events.jsonl") if e["event"] == kind]

    def closed(self):
        return [r for r in replib.read_jsonl(self.root, "reps.jsonl") if r.get("kind") == "brief"]


class Enabling(Harness):
    def test_without_rep_everything_is_ordinary_claude_code(self):
        self.assertEqual(self.say("refactor the claim function please"), "")
        self.allowed("budget_gate", "Write", "anything.py", content="x = 1\n" * 500)
        self.assertEqual(self.bash("rep approve"), (None, ""))
        self.assertEqual(self.stop(), {})
        self.assertEqual(self.session_start(), "")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".rep")))

    def test_shape_turns_rep_on_and_records_the_users_words(self):
        text = self.say("/shape make claims atomic so no job runs twice")
        self.assertIn("enabled", text)
        self.assertIn("rep brief draft", text)
        self.assertEqual(self.state()["asked"], "make claims atomic so no job runs twice")
        with open(os.path.join(self.root, ".claude", "settings.local.json")) as f:
            self.assertEqual(json.load(f)["outputStyle"], "rep-tutor")

    def test_state_is_never_committed_even_from_a_worktree(self):
        tree = os.path.join(os.path.dirname(self.root), os.path.basename(self.root) + "-wt")
        self.addCleanup(shutil.rmtree, tree, True)
        self.git("worktree", "add", "-q", tree)
        subprocess.run([sys.executable, REP, "init"], cwd=tree, check=True, stdout=subprocess.DEVNULL)
        status = subprocess.run(["git", "-C", tree, "status", "--short"], stdout=subprocess.PIPE,
                                universal_newlines=True).stdout
        self.assertEqual(status, "")

    def test_other_commands_explain_that_rep_is_off(self):
        self.assertIn("not enabled", self.say("/approve"))
        self.assertIn("not enabled", self.say("/shape"))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".rep")))

    def test_a_parent_rep_dir_does_not_reach_into_a_git_repo(self):
        parent = os.path.realpath(tempfile.mkdtemp(prefix="rep-parent-"))
        self.addCleanup(shutil.rmtree, parent, True)
        os.makedirs(os.path.join(parent, ".rep"))
        child = os.path.join(parent, "child")
        subprocess.run(["git", "init", "-q", child], check=True)
        self.assertIsNone(replib.find_root(os.path.join(child, "x.py")))

    def test_a_missing_hook_script_never_blocks(self):
        home = tempfile.mkdtemp(prefix="rep-home-")
        self.addCleanup(shutil.rmtree, home, True)
        proc = subprocess.run(["sh", "-c", install.command("nope")], env=dict(os.environ, HOME=home))
        self.assertEqual(proc.returncode, 0)


class Shaping(Harness):
    def setUp(self):
        super().setUp()
        self.say("/shape make claims atomic so no job runs twice")

    def test_a_draft_must_be_a_usable_contract(self):
        proc = self.draft("# Brief: x\n## Goal\ny\n", ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("'cmd: <command>' under Check", proc.stdout)
        self.assertIn("'lines: <n>' and 'files: <n>'", proc.stdout)
        self.assertIsNone(self.state()["draft"])

    def test_a_draft_must_fit_on_one_screen(self):
        long = brief_text().replace("## Approach\n", "## Approach\n" + "more detail\n" * 40)
        self.assertIn("fits on one screen", self.draft(long, ok=False).stdout)

    def test_a_draft_waits_for_approval_and_blocks_code(self):
        self.assertIn("Do not edit code", self.draft().stdout)
        self.assertIn("waiting for the user's /approve", self.say("ok, looks right?"))
        reason = self.denied("budget_gate", "Edit", "jobs.py", old_string="row", new_string="r")
        self.assertIn("still a draft", reason)
        text = self.read(".rep/briefs/draft.md")
        self.assertIn('Asked: "make claims atomic so no job runs twice"', text)
        self.assertIn("/approve <where you expect", text)

    def test_the_agent_cannot_write_its_own_pre_mortem_or_log(self):
        sneaky = brief_text() + "## Pre-mortem\nnothing will go wrong\n## Log\n- approved by the user\n"
        self.draft(sneaky)
        text = self.read(".rep/briefs/draft.md")
        self.assertNotIn("nothing will go wrong", text)
        self.assertNotIn("approved by the user", text)

    def test_shape_drop_discards_the_draft(self):
        self.draft()
        self.assertIn("dropped", self.say("/shape drop"))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".rep", "briefs", "draft.md")))
        self.allowed("budget_gate", "Edit", "jobs.py", old_string="row", new_string="r")

    def test_approve_needs_a_draft(self):
        self.assertIn("nothing to approve", self.say("/approve"))

    def test_approve_makes_it_binding_with_the_users_pre_mortem_verbatim(self):
        self.draft()
        nasty = "the race test will flake; it'll reach for `retry()` && \"sleep\" $HOME"
        text = self.say("/approve " + nasty)
        self.assertIn("brief approved by the user", text)
        state = self.state()
        self.assertIsNone(state["draft"])
        self.assertRegex(state["base"], r"^[0-9a-f]{40}$")
        brief = self.brief()
        self.assertIn(nasty, brief)
        self.assertIn("approved by the user (base %s)" % state["base"][:7], brief)

    def test_approve_without_a_pre_mortem_is_fine(self):
        self.draft()
        self.say("/approve")
        self.assertIn("## Pre-mortem\n(none)", self.brief())

    def test_a_draft_the_user_broke_by_hand_is_not_approved(self):
        self.draft()
        path = os.path.join(self.root, ".rep", "briefs", "draft.md")
        self.write(".rep/briefs/draft.md", self.read(".rep/briefs/draft.md").replace("cmd:", "command:"))
        self.assertIn("can't be approved", self.say("/approve"))
        self.assertTrue(os.path.exists(path))

    def test_the_agent_cannot_approve_its_own_draft(self):
        self.draft()
        for command in ("rep approve", "rep review accept", "cd x && rep pin tests", "~/.claude/bin/rep unpin"):
            self.assertEqual(self.bash(command)[0], "deny", command)
        self.assertIsNone(self.state()["active"])

    def test_the_agent_keeps_its_own_commands(self):
        for command in ("rep brief draft <<'EOF'", "rep check", "rep note decided x", "rep status", "rep report",
                        "grep approve notes.txt", "prep review"):
            self.assertEqual(self.bash(command)[0], None, command)


class Budget(Harness):
    def setUp(self):
        super().setUp()
        self.activate()

    def test_uncommitted_work_from_before_the_brief_does_not_count(self):
        self.say("/review accept")
        self.write("jobs.py", JOBS + "leftover = 1\n" * 30)        # earlier work, never committed
        self.write("tests/old_notes.py", "x = 1\n" * 30)
        self.activate()
        self.assertIn("budget 0/20 lines, 0/2 files", self.say("go"))
        self.allowed("budget_gate", "Edit", "jobs.py", old_string="    return row\n",
                     new_string="    return row  # atomic\n")
        self.assertEqual(self.read("jobs.py").count("leftover"), 30)
        index_untouched = subprocess.run(["git", "-C", self.root, "diff", "--cached", "--name-only"],
                                         stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertEqual(index_untouched, "")

    def test_edits_within_budget_go_through(self):
        self.allowed("budget_gate", "Edit", "jobs.py", old_string="    mark_running(conn, row)\n",
                     new_string="    mark_running(conn, row)  # atomic\n")

    def test_the_edit_that_would_cross_the_line_is_refused(self):
        reason = self.denied("budget_gate", "Edit", "jobs.py", old_string="    return row\n",
                             new_string="    return row\n" + "    pass\n" * 30)
        self.assertIn("budget breaker", reason)
        self.assertIn("30 lines across 1 file(s)", reason)
        self.assertEqual(self.state()["trips"], 1)
        self.assertEqual(self.events("budget_trip")[0]["via"], "edit")

    def test_cutting_scope_is_never_blocked(self):
        self.write("jobs.py", JOBS + "x = 1\n" * 40)          # over budget, as if done through Bash
        self.allowed("budget_gate", "Edit", "jobs.py", old_string="x = 1\n" * 40, new_string="x = 1\n" * 10)
        self.denied("budget_gate", "Edit", "jobs.py", old_string="x = 1\n" * 40, new_string="x = 1\n" * 41)

    def test_the_file_budget_counts_new_files(self):
        self.write("tests/test_a.py", "a = 1\n")
        self.allowed("budget_gate", "Edit", "jobs.py", old_string="row = ", new_string="row  = ")
        self.write("jobs.py", JOBS.replace("row = ", "row  = "))
        reason = self.denied("budget_gate", "Write", "tests/test_b.py", content="b = 1\n")
        self.assertIn("across 3 file(s)", reason)

    def test_edits_outside_the_paths_are_refused(self):
        reason = self.denied("budget_gate", "Write", "scheduler.py", content="x = 1\n")
        self.assertIn("outside the brief's paths", reason)
        self.assertEqual(self.state()["refused"], 1)

    def test_lessons_are_free(self):
        self.allowed("budget_gate", "Write", "lessons.md", content="- lesson\n" * 100)

    def test_a_revision_raises_the_budget_once_the_user_approves(self):
        grow = dict(old_string="    return row\n", new_string="    return row\n" + "    pass\n" * 30)
        self.denied("budget_gate", "Edit", "jobs.py", **grow)
        self.draft(brief_text(lines=60))
        self.assertIn("revision draft is waiting", self.say("go on"))
        self.denied("budget_gate", "Edit", "jobs.py", **grow)
        self.assertIn("Budget changed", self.say("/approve"))
        self.allowed("budget_gate", "Edit", "jobs.py", **grow)
        self.assertIn("revision approved by the user: Budget changed", self.brief())

    def test_a_revision_keeps_the_users_pre_mortem(self):
        self.say("/review redirect keep it small")
        before = self.brief().split("## Pre-mortem")[1]
        self.draft(brief_text(lines=30))
        self.say("/approve")
        self.assertEqual(self.brief().split("## Pre-mortem")[1].split("## Log")[0], before.split("## Log")[0])

    def test_dependency_manifests_need_deps_yes(self):
        self.say("/review reject")
        self.activate(paths="", lines=200, files=5)
        self.assertIn("dependency manifest", self.denied("budget_gate", "Write", "package.json", content="{}\n"))
        self.say("/review reject")
        self.activate(paths="", lines=200, files=5, deps="yes")
        self.allowed("budget_gate", "Write", "package.json", content="{}\n")

    def test_a_bash_command_that_crosses_the_budget_is_reported_once(self):
        decision, reason = self.bash("python3 gen.py", lambda: self.write("tests/gen.py", "x = 1\n" * 50))
        self.assertEqual(decision, "block")
        self.assertIn("budget breaker", reason)
        self.assertEqual(self.bash("ls"), (None, ""))
        self.assertEqual(self.bash("python3 gen.py", lambda: self.write("tests/gen.py", "x = 1\n" * 60))[0], "block")


class Check(Harness):
    def setUp(self):
        super().setUp()
        self.flag = os.path.join(tempfile.mkdtemp(prefix="rep-flag-"), "flag")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.flag), True)

    def test_the_harness_records_red_then_green(self):
        self.activate()
        fail = self.rep("check", ok=False)
        self.assertEqual(fail.returncode, 1)
        self.assertIn("rep check: FAIL", fail.stdout)
        self.assertIn("budget 0/20 lines", fail.stdout)
        self.write("jobs.py", JOBS.replace("mark_running", "mark_running_atomic"))
        self.assertIn("rep check: PASS", self.rep("check").stdout)
        runs = self.state()["runs"]
        self.assertEqual([r["ok"] for r in runs], [False, True])
        self.assertIn("check failing", self.brief())
        self.assertIn("check passing", self.brief())

    def test_the_same_code_with_a_different_result_is_flagged_flaky(self):
        self.activate(cmd="test -f %s" % self.flag)
        self.rep("check", ok=False)
        open(self.flag, "w").close()
        self.assertIn("FLAKY", self.rep("check").stdout)
        self.assertEqual(self.state()["flakes"], 1)

    def test_long_output_is_trimmed_for_the_agent(self):
        self.activate(cmd="seq 1 200")
        out = self.rep("check").stdout
        self.assertIn("140 earlier lines omitted", out)
        self.assertNotIn("\n1\n", out)

    def test_a_check_run_while_shaping_is_kept_as_evidence(self):
        self.say("/shape make claims atomic so no job runs twice")
        self.draft()
        self.rep("check", ok=False)
        self.say("/approve")
        self.assertEqual(len(self.state()["runs"]), 1)

    def test_no_brief_no_check(self):
        self.rep("init")
        self.assertIn("no brief", self.rep("check", ok=False).stderr)


class DoneGate(Harness):
    def setUp(self):
        super().setUp()
        self.activate()

    def test_a_turn_that_changed_nothing_is_left_alone(self):
        self.say("what does claim do?")
        self.assertEqual(self.stop(), {})

    def test_unchecked_changes_get_one_nudge(self):
        self.say("go")
        self.write("jobs.py", JOBS + "# atomic\n")
        out = self.stop()
        self.assertIn("rep check", out["hookSpecificOutput"]["additionalContext"])
        again = self.stop(active=True)
        self.assertIn("not run on", again["systemMessage"])
        self.assertEqual(len(self.events("stop_nudge")), 1)

    def test_the_user_sees_the_measured_result(self):
        self.say("go")
        self.write("jobs.py", JOBS + "# nope\n")
        self.rep("check", ok=False)
        self.assertIn("check failing on the current code", self.stop()["systemMessage"])
        self.write("jobs.py", JOBS + "# atomic\n")
        self.rep("check")
        message = self.stop()["systemMessage"]
        self.assertIn("check passing on the current code", message)
        self.assertIn("budget 1/20 lines, 1/2 files", message)

    def test_edits_the_user_made_between_turns_do_not_count(self):
        self.write("jobs.py", JOBS + "# mine\n")
        self.say("go")
        self.assertEqual(self.stop(), {})


class Sessions(Harness):
    def test_a_new_session_starts_from_the_brief_and_its_log(self):
        self.write("lessons.md", "# Lessons\n\n- claims must be one statement\n")
        self.activate("it will flake")
        self.rep("note", "chose", "UPDATE", "RETURNING", "over", "SELECT", "FOR", "UPDATE")
        for source in ("startup", "clear", "compact"):
            text = self.session_start(source)
            self.assertIn("Active brief", text)
            self.assertIn("# Brief: No job runs twice", text)
            self.assertIn("chose UPDATE RETURNING over SELECT FOR UPDATE", text)
            self.assertIn("it will flake", text)
            self.assertIn("lessons.md holds 1 lesson", text)

    def test_a_waiting_draft_is_mentioned(self):
        self.say("/shape something")
        self.draft()
        self.assertIn("waiting for the user's /approve", self.session_start())

    def test_ordinary_prompts_are_not_stored(self):
        self.rep("init")
        secret = "select * from patients where mrn = '0042'"
        self.say(secret)
        with open(os.path.join(self.root, ".rep", "state.json")) as f:
            self.assertNotIn("0042", f.read())


class Review(Harness):
    def setUp(self):
        super().setUp()
        self.activate("the race test will flake")
        self.rep("check", ok=False)
        self.write("jobs.py", JOBS.replace("mark_running", "mark_running_atomic"))
        self.rep("check")

    def test_the_packet_is_measured_not_narrated(self):
        packet = self.say("/review")
        self.assertIn("measured by the harness", packet)
        self.assertIn("2 run(s); first run failed (red before green); check passing on the current code", packet)
        self.assertIn("1 file(s), 2 line(s) changed; budget 2 files / 20 lines, within budget", packet)
        self.assertIn("jobs.py +1 -1", packet)
        self.assertIn('"the race test will flake"', packet)
        self.assertIn("behaviour on Postgres", packet)
        self.assertIsNotNone(self.state()["review_at"])

    def test_accept_closes_with_a_record_of_estimate_against_actual(self):
        self.say("/review")
        self.assertIn("closed by the user: accepted", self.say("/review accept looks right"))
        record = self.closed()[0]
        self.assertEqual(record["outcome"], "accepted")
        self.assertEqual(record["budget"], {"lines": 20, "files": 2})
        self.assertEqual(record["actual"], {"lines": 2, "files": 1})
        self.assertEqual(record["check"], {"runs": 2, "redFirst": True, "flakes": 0, "passingAtClose": True})
        self.assertEqual(record["premortem"], "the race test will flake")
        self.assertIsNone(self.state()["active"])
        self.allowed("budget_gate", "Write", "anything.py", content="x = 1\n" * 500)

    def test_redirect_keeps_the_brief_and_logs_the_note(self):
        self.say("/review")
        self.assertIn("redirected", self.say("/review redirect also cover the retry path"))
        self.assertIsNotNone(self.state()["active"])
        self.assertIn("redirected by the user: also cover the retry path", self.brief())

    def test_reject_closes_as_rejected(self):
        self.say("/review reject")
        self.assertEqual(self.closed()[0]["outcome"], "rejected")

    def test_report_and_log_read_the_records(self):
        self.say("/review accept")
        report = self.rep("report").stdout
        self.assertIn("briefs     1 closed: 1 accepted, 0 rejected", report)
        self.assertIn("actual/estimated lines: median 0.10", report)
        self.assertIn("red before green: 1/1", report)
        self.assertIn("bypasses   0", report)
        self.assertIn("No job runs twice", self.rep("log").stdout)


class GuardHuman(Harness):
    def setUp(self):
        super().setUp()
        self.rep("init")
        self.write("jobs.py", STUB)

    def test_filling_the_region_is_denied(self):
        reason = self.denied("guard_human", "Edit", "jobs.py", old_string="    raise NotImplementedError",
                             new_string="    return conn.execute('select ... for update skip locked')")
        self.assertIn("user's to write", reason)

    def test_editing_outside_the_region_is_allowed(self):
        self.allowed("guard_human", "Edit", "jobs.py", old_string="status = 'free'", new_string="status = 'ready'")

    def test_deleting_or_rewording_a_marker_is_denied(self):
        self.denied("guard_human", "Edit", "jobs.py", old_string="    # END TODO(human)\n", new_string="")
        self.denied("guard_human", "Edit", "jobs.py", old_string="two workers must never", new_string="workers never")

    def test_a_write_must_carry_the_region_through_unchanged(self):
        self.allowed("guard_human", "Write", "jobs.py", content="import sqlite3\n\n" + STUB)
        self.denied("guard_human", "Write", "jobs.py", content=STUB.replace("raise NotImplementedError", "return 1"))

    def test_replace_all_and_multiedit_are_checked_as_a_whole(self):
        self.denied("guard_human", "Edit", "jobs.py", old_string="job", new_string="task", replace_all=True)
        edits = [{"old_string": "status = 'free'", "new_string": "status = 'ready'"},
                 {"old_string": "raise NotImplementedError", "new_string": "return 1"}]
        self.denied("guard_human", "MultiEdit", "jobs.py", edits=edits)
        self.allowed("guard_human", "MultiEdit", "jobs.py", edits=edits[:1])

    def test_an_edit_that_cannot_be_simulated_fails_closed(self):
        self.assertIn("cannot be checked",
                      self.denied("guard_human", "Edit", "jobs.py", old_string="not in the file", new_string="x"))

    def test_an_unterminated_region_runs_to_the_end_of_the_file(self):
        self.write("open.py", "a = 1\n# TODO(human): finish this\nb = 2\n")
        self.denied("guard_human", "Edit", "open.py", old_string="b = 2", new_string="b = 3")
        self.allowed("guard_human", "Edit", "open.py", old_string="a = 1", new_string="a = 0")

    def test_placing_regions_is_limited_to_two(self):
        region = "# TODO(human): %d\npass\n# END TODO(human)\n"
        self.allowed("guard_human", "Write", "one.py", content=region % 1 + region % 2)
        self.assertIn("limit is 2", self.denied("guard_human", "Write", "three.py", content=region % 3))

    def test_prose_that_mentions_the_marker_is_not_a_region(self):
        self.assertEqual(replib.regions("Place a `TODO(human)` marker.\n- TODO(human) regions\n"), [])

    def test_skip_releases_the_regions(self):
        self.say("ok")
        self.assertIn("released", self.say("/skip"))
        self.allowed("guard_human", "Edit", "jobs.py", old_string="    raise NotImplementedError", new_string="    return 1")

    def test_harness_state_is_not_editable(self):
        self.assertIn("harness state", self.denied("guard_human", "Write", ".rep/state.json", content="{}"))
        self.denied("guard_human", "Write", ".rep/briefs/x.md", content="# Brief: mine now\n")

    def test_hints_climb_one_level_per_request(self):
        self.say("ok")
        self.assertIn("hint 1/5", self.say("/hint"))
        self.assertIn("hint 2/5", self.say("/hint"))


class LockTests(Harness):
    def setUp(self):
        super().setUp()
        self.rep("init")
        self.write("tests/test_jobs.py", "def test_one():\n    assert True\n")

    def test_a_pinned_test_cannot_be_edited(self):
        self.assertIn("tests/test_jobs.py", self.say("/pin tests/test_jobs.py"))
        self.assertIn("owns the oracle", self.denied("lock_tests", "Edit", "tests/test_jobs.py",
                                                     old_string="True", new_string="False"))
        self.allowed("lock_tests", "Edit", "jobs.py", old_string="row", new_string="r")

    def test_pinning_a_directory_pins_its_files_and_missing_paths_are_named(self):
        self.say("/pin tests")
        self.assertEqual(list(self.state()["pins"]), ["tests/test_jobs.py"])
        self.assertIn("Not found: nope.py", self.say("/pin nope.py"))


class BashHole(Harness):
    def setUp(self):
        super().setUp()
        self.rep("init")
        self.write("jobs.py", STUB)
        self.write("tests/test_jobs.py", "def test_one():\n    assert True\n")
        self.say("/pin tests/test_jobs.py")

    def test_the_state_directory_is_off_limits_from_the_shell(self):
        for command in ("echo '{}' > .rep/state.json", "cat .rep/reps.jsonl", "rm -rf .rep", "cd .rep && ls"):
            self.assertEqual(self.bash(command)[0], "deny", command)
        for command in ("ls -la", "cat foo.rep", "git status", "cat .replit"):
            self.assertEqual(self.bash(command)[0], None, command)

    def test_filling_a_region_through_bash_is_caught_and_can_be_restored(self):
        fill = lambda: self.write("jobs.py", STUB.replace("raise NotImplementedError", "return 1"))  # noqa: E731
        decision, reason = self.bash("sed -i '' 's/raise NotImplementedError/return 1/' jobs.py", fill)
        self.assertEqual(decision, "block")
        self.assertIn("jobs.py", reason)
        self.assertEqual(self.bash("rep restore jobs.py", lambda: self.rep("restore", "jobs.py")), (None, ""))
        self.assertEqual(self.read("jobs.py"), STUB)
        self.assertEqual(self.state()["violations"], 1)

    def test_changing_a_pinned_test_through_bash_is_caught(self):
        self.assertEqual(self.bash("echo > tests/test_jobs.py", lambda: self.write("tests/test_jobs.py", "\n"))[0],
                         "block")

    def test_the_users_own_edits_between_commands_are_not_blamed(self):
        self.write("jobs.py", STUB.replace("raise NotImplementedError", "return conn.claim(worker)"))
        self.assertEqual(self.bash("python3 -m unittest -q"), (None, ""))

    def test_the_log_keeps_no_command_text(self):
        self.bash("cat .rep/state.json")
        self.bash("sed -i '' s/x/y/ jobs.py --secret-value 0042",
                  lambda: self.write("jobs.py", STUB.replace("raise NotImplementedError", "return 1")))
        log = self.read(".rep/events.jsonl")
        self.assertNotIn("0042", log)
        self.assertNotIn("state.json", log)
        self.assertEqual(self.events("violation")[0]["program"], "sed")


SOURCED = ("SQLite is 3.35 or newer (source: python3 -c 'import sqlite3; print(sqlite3.sqlite_version)')",
           "claim() is the only writer of 'running' (source: grep -rn running jobq/)")


class BriefContract(Harness):
    def setUp(self):
        super().setUp()
        self.say("/shape make claims atomic so no job runs twice")

    def test_a_draft_must_name_its_tradeoff(self):
        out = self.draft(brief_text(tradeoff="Keeps it simple."), ok=False).stdout
        self.assertIn("'Tradeoff:' line under Approach", out)

    def test_every_assumption_needs_a_source(self):
        out = self.draft(brief_text(assumptions=(SOURCED[0], "workers share one database")), ok=False).stdout
        self.assertIn("'source:' on every assumption", out)
        self.assertIn("workers share one database", out)

    def test_assumptions_and_tradeoff_reach_the_review_packet(self):
        self.draft(brief_text(assumptions=SOURCED))
        self.say("/approve")
        self.assertIn("## Assumptions\n- SQLite is 3.35 or newer", self.brief())
        packet = self.say("/review")
        self.assertIn("Assumptions: 2, each with a source. The reviewer checks every one", packet)
        self.assertIn("Chosen tradeoff: favours simplicity and consistency", packet)

    def test_a_brief_without_assumptions_has_no_empty_section(self):
        self.draft()
        self.say("/approve")
        self.assertNotIn("## Assumptions", self.brief())

    def test_revising_an_assumption_away_records_a_wrong_assumption(self):
        self.draft(brief_text(assumptions=SOURCED))
        self.say("/approve")
        self.draft(brief_text(assumptions=SOURCED[1:]))
        self.assertIn("Assumptions changed", self.say("/approve"))
        rows = replib.read_jsonl(self.root, "mistakes.jsonl")
        self.assertEqual([r["pattern"] for r in rows], ["wrong-assumption"])
        self.assertIn("revised away: SQLite is 3.35 or newer", rows[0]["note"])


class Mistakes(Harness):
    def setUp(self):
        super().setUp()
        self.activate()

    def patterns(self):
        return [r["pattern"] for r in replib.read_jsonl(self.root, "mistakes.jsonl")]

    def test_budget_trips_record_overengineering_once_per_brief(self):
        grow = dict(old_string="    return row\n", new_string="    return row\n" + "    pass\n" * 30)
        self.denied("budget_gate", "Edit", "jobs.py", **grow)
        self.denied("budget_gate", "Edit", "jobs.py", **grow)
        self.assertEqual(self.patterns(), ["overengineering"])

    def test_scope_refusals_record_scope_drift(self):
        self.denied("budget_gate", "Write", "scheduler.py", content="x = 1\n")
        self.assertEqual(self.patterns(), ["scope-drift"])

    def test_unchecked_changes_record_skipped_verification(self):
        self.say("go")
        self.write("jobs.py", JOBS + "# atomic\n")
        self.stop()
        self.assertEqual(self.patterns(), ["skipped-verification"])

    def test_a_bash_write_to_a_protected_region_records_destructive(self):
        self.write("jobs.py", STUB)
        self.say("ok")
        self.bash("sed ...", lambda: self.write("jobs.py", STUB.replace("raise NotImplementedError", "return 1")))
        self.assertEqual(self.patterns(), ["destructive"])

    def test_the_user_records_a_pattern_by_starting_the_review_note_with_it(self):
        self.say("/review redirect stopped-short: retries are not covered")
        row = replib.read_jsonl(self.root, "mistakes.jsonl")[0]
        self.assertEqual((row["pattern"], row["source"], row["note"]), ("stopped-short", "user", "retries are not covered"))
        self.assertIn("redirected by the user: stopped-short: retries are not covered", self.brief())

    def test_a_pattern_the_user_named_is_recorded_once_and_doubles_are_flagged(self):
        self.assertIn("don't record it again", self.say("/review redirect weak-check: pin the 8s delay"))
        self.assertIn("already had 1 weak-check entry", self.rep("mistake", "weak-check", "same gap").stdout)

    def test_unknown_patterns_are_refused(self):
        self.rep("mistake", "stale-copy", "changed", "one", "copy", "of", "the", "limit")
        proc = self.rep("mistake", "bogus", "x", ok=False)
        self.assertIn("unknown pattern", proc.stderr)
        self.assertEqual(self.patterns(), ["stale-copy"])

    def test_the_register_groups_by_pattern_and_shapes_the_next_brief(self):
        for pattern in ("stale-copy", "trusted-summary", "stale-copy"):
            self.rep("mistake", pattern, "an example")
        self.assertTrue(self.rep("mistakes").stdout.startswith("stale-copy"))
        self.say("/review accept")
        self.assertEqual(self.closed()[0]["mistakes"], {"stale-copy": 2, "trusted-summary": 1})
        self.assertIn("Mistakes seen most in this repo: stale-copy x2, trusted-summary x1",
                      self.say("/shape add retries"))
        self.assertIn("mistakes   stale-copy x2, trusted-summary x1", self.rep("report").stdout)


class Simplify(Harness):
    BIG = JOBS + "# atomic\n" + "".join("x%d = %d\n" % (i, i) for i in range(40))      # 41 lines over base
    SMALL = JOBS + "# atomic\n" + "".join("x%d = %d\n" % (i, i) for i in range(20))    # 21 lines

    def setUp(self):
        super().setUp()
        self.activate(lines=200, files=3)

    def green(self, text):
        self.say("go")
        self.write("jobs.py", text)
        self.rep("check")

    def test_the_pass_is_due_at_review_not_at_the_end_of_the_build_turn(self):
        self.green(self.BIG)
        self.assertIn("check passing", self.stop()["systemMessage"])      # no nudge: the user's /review asks for it
        self.assertIn("Simplify pass: due (41 lines). Run it first", self.say("/review"))

    def test_small_diffs_and_briefs_that_opt_out_skip_it(self):
        self.green(JOBS + "# atomic\n")
        self.assertIn("check passing", self.stop()["systemMessage"])
        self.assertIn("skipped, the diff is under 30 lines", self.say("/review"))
        self.say("/review reject")
        self.activate(lines=200, files=3, simplify="no")
        self.green(self.BIG)
        self.assertIn("systemMessage", self.stop())
        self.assertIn("turned off in the brief", self.say("/review"))

    def test_the_pass_starts_only_from_passing_code_and_hands_over_a_cold_prompt(self):
        self.write("jobs.py", self.BIG)
        self.assertIn("has to pass", self.rep("simplify", "start", ok=False).stdout)
        self.rep("check")
        out = self.rep("simplify", "start").stdout
        self.assertIn("simplify pass started at 41 lines", out)
        self.assertIn("You are simplifying a change before it is reviewed", out)
        self.assertIn("# Brief: No job runs twice", out)
        self.assertIn("rep diff", out)
        self.assertNotIn("## Log", out)

    def test_the_pass_is_shrink_only_and_leaves_tests_alone(self):
        self.write("tests/test_jobs.py", "def test_one():\n    assert True\n")
        self.green(self.BIG)
        self.rep("simplify", "start")
        self.assertIn("shrink-only", self.denied("budget_gate", "Edit", "jobs.py", old_string="x0 = 0\n",
                                                 new_string="x0 = 0\nextra = 1\n"))
        self.allowed("budget_gate", "Edit", "jobs.py", old_string="x0 = 0\nx1 = 1\n", new_string="")
        self.assertIn("off-limits", self.denied("budget_gate", "Write", "tests/test_jobs.py", content=""))
        decision, reason = self.bash("python3 gen.py", lambda: self.write("jobs.py", self.BIG + "y = 1\n"))
        self.assertEqual(decision, "block")
        self.assertIn("shrink-only", reason)

    def test_done_needs_the_check_passing_on_the_simplified_code_and_measures_the_cut(self):
        self.green(self.BIG)
        self.rep("simplify", "start")
        self.write("jobs.py", self.SMALL)
        self.assertIn("run `rep check` first", self.rep("simplify", "done", ok=False).stdout)
        self.rep("check")
        self.assertIn("41 -> 21 lines", self.rep("simplify", "done").stdout)
        self.assertIn("simplified: 41 -> 21 lines", self.brief())
        self.assertEqual([r["pattern"] for r in replib.read_jsonl(self.root, "mistakes.jsonl")], ["overengineering"])
        self.assertIn("Simplify pass: 41 -> 21 lines (-49%)", self.say("/review"))
        self.say("/review accept")
        self.assertEqual(self.closed()[0]["simplify"], {"before": 41, "after": 21})
        self.assertIn("simplify   1 pass(es); median cut 49% of the diff", self.rep("report").stdout)

    def test_an_open_pass_is_called_out_at_stop(self):
        self.green(self.BIG)
        self.rep("simplify", "start")
        self.say("go on")
        self.write("jobs.py", self.SMALL)
        self.rep("check")
        self.assertIn("still open", self.stop()["hookSpecificOutput"]["additionalContext"])

    def test_a_redirect_reopens_the_pass_for_the_next_round(self):
        self.green(self.BIG)
        self.rep("simplify", "start")
        self.rep("simplify", "done")
        self.say("/review redirect also handle retries")
        self.assertIsNone(self.state()["simplify"])

    def test_rep_diff_shows_the_whole_change_including_new_files(self):
        self.write("jobs.py", JOBS + "# atomic\n")
        self.write("tests/test_new.py", "def test_new():\n    pass\n")
        out = self.rep("diff").stdout
        self.assertIn("+# atomic", out)
        self.assertIn("tests/test_new.py", out)


class FailClosed(Harness):
    def test_a_crashing_hook_denies_instead_of_waving_the_edit_through(self):
        self.rep("init")
        decision, reason = self.edit("guard_human", "Edit", "jobs.py", old_string=["not", "a", "string"],
                                     new_string="2")
        self.assertEqual(decision, "deny")
        self.assertIn("failing closed", reason)


if __name__ == "__main__":
    unittest.main()
