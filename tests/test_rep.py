"""Drives each hook the way Claude Code does: JSON on stdin, decision on stdout.

Payload shapes were captured from Claude Code 2.1.288 (see README).
Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HOOKS = os.path.join(REPO, "hooks")
REP = os.path.join(REPO, "bin", "rep")
sys.path.insert(0, HOOKS)
import replib  # noqa: E402

STUB = '''def claim(conn, worker):
    # TODO(human): claim one job atomically; two workers must never get the same row
    raise NotImplementedError
    # END TODO(human)


def release(conn, job):
    return conn.execute("update jobs set status = 'free' where id = ?", (job,))
'''


class Harness(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="rep-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        subprocess.run(["git", "init", "-q", self.root], check=True)
        self.session = "s1"
        self.n = 0

    # -- helpers --
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

    def rep(self, *args, ok=True):
        proc = subprocess.run([sys.executable, REP] + list(args), cwd=self.root, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def state(self):
        return replib.load_state(self.root)

    def events(self, kind):
        return [e for e in replib.read_jsonl(self.root, "events.jsonl") if e["event"] == kind]

    def reps(self):
        return replib.read_jsonl(self.root, "reps.jsonl")

    def on(self, dial="dim"):
        self.say("/dial %s" % dial)


class Enabling(Harness):
    def test_everything_is_a_no_op_until_rep_is_turned_on(self):
        self.write("a.py", STUB)
        self.assertEqual(self.say("fix the bug in a.py"), "")
        self.allowed("predict_gate", "Edit", "a.py", old_string="raise NotImplementedError", new_string="pass")
        self.allowed("guard_human", "Edit", "a.py", old_string="raise NotImplementedError", new_string="pass")
        self.assertEqual(self.bash("rep dial dark"), (None, ""))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".rep")))

    def test_dial_turns_it_on_and_sets_the_style(self):
        text = self.say("/dial lit")
        self.assertIn("enabled", text)
        self.assertEqual(self.state()["dial"], "lit")
        with open(os.path.join(self.root, ".claude", "settings.local.json")) as f:
            self.assertEqual(json.load(f)["outputStyle"], "rep-tutor")
        self.assertIn(".rep/", self.read(".git/info/exclude"))

    def test_other_commands_explain_that_rep_is_off(self):
        self.assertIn("not enabled", self.say("/predict the bug is in claim"))

    def test_a_parent_rep_dir_does_not_reach_into_a_git_repo(self):
        parent = os.path.realpath(tempfile.mkdtemp(prefix="rep-parent-"))
        self.addCleanup(shutil.rmtree, parent, True)
        os.makedirs(os.path.join(parent, ".rep"))
        child = os.path.join(parent, "child")
        subprocess.run(["git", "init", "-q", child], check=True)
        self.assertIsNone(replib.find_root(os.path.join(child, "x.py")))
        self.assertEqual(replib.find_root(os.path.join(parent, "loose.py")), parent)

    def test_an_existing_output_style_is_left_alone(self):
        self.write(".claude/settings.local.json", json.dumps({"outputStyle": "Concise", "permissions": {}}))
        text = self.say("/dial dim")
        self.assertIn("left as is", text)
        self.assertEqual(json.loads(self.read(".claude/settings.local.json"))["outputStyle"], "Concise")


class PredictGate(Harness):
    def setUp(self):
        super().setUp()
        self.on()
        self.write("a.py", "x = 1\n")

    def test_edits_are_locked_until_a_prediction_exists(self):
        reason = self.denied("predict_gate", "Edit", "a.py", old_string="1", new_string="2")
        self.assertIn("predict:", reason)
        self.assertEqual(self.state()["work"]["denials"], 1)
        self.assertEqual(len(self.events("gate_deny")), 1)

    def test_a_new_file_is_gated_too(self):
        self.denied("predict_gate", "Write", "brand/new.py", content="x = 1\n")

    def test_inline_prediction_in_the_task_message_costs_no_extra_turn(self):
        text = self.say("fix the duplicate job execution bug that shows up under load\n"
                        "predict: claim() selects then updates with no lock; fix stays in claim.py")
        self.assertIn("recorded verbatim", text)
        task = self.state()["task"]
        self.assertEqual(task["predictions"][0]["text"],
                         "claim() selects then updates with no lock; fix stays in claim.py")
        self.assertEqual(task["predictions"][0]["wait_s"], 0)
        self.assertIn("duplicate job execution", task["goal"])
        self.allowed("predict_gate", "Edit", "a.py", old_string="1", new_string="2")

    def test_prediction_as_a_reply_keeps_the_goal_and_measures_the_wait(self):
        self.say("fix the duplicate job execution bug that shows up under load")
        self.denied("predict_gate", "Edit", "a.py", old_string="1", new_string="2")
        with replib.state_tx(self.root) as state:
            state["work"]["asked_at"] -= 20
        self.say("predict: no lock between select and update")
        task = self.state()["task"]
        self.assertIn("duplicate job execution", task["goal"])
        self.assertTrue(19 <= task["predictions"][0]["wait_s"] <= 25)

    def test_slash_predict_records_and_bare_slash_predict_asks(self):
        self.assertIn("nothing was recorded", self.say("/predict"))
        self.assertIsNone(self.state()["task"])
        self.assertIn("recorded verbatim", self.say("/predict the cache key ignores locale"))
        self.assertEqual(self.state()["task"]["predictions"][0]["via"], "slash")

    def test_prediction_text_survives_shell_metacharacters(self):
        nasty = "it's in `claim()` near $HOME; \"quotes\" && rm -rf /"
        self.say("/predict " + nasty)
        self.assertEqual(self.state()["task"]["predictions"][0]["text"], nasty)

    def test_skip_is_free_and_logged(self):
        self.assertIn("skipped", self.say("/skip"))
        self.allowed("predict_gate", "Edit", "a.py", old_string="1", new_string="2")
        self.assertEqual(self.state()["task"]["skipped"], ["predict"])
        self.assertEqual(self.events("skip")[0]["what"], "predict")

    def test_dark_has_no_gate(self):
        self.say("/dial dark")
        self.allowed("predict_gate", "Edit", "a.py", old_string="1", new_string="2")
        self.assertEqual(self.say("an ordinary message in dark mode, nothing to report"), "")

    def test_a_second_prediction_joins_the_same_task(self):
        self.say("/predict first")
        self.say("predict: the pinned test will pass on the first run")
        self.assertEqual(len(self.state()["task"]["predictions"]), 2)

    def test_a_resent_prediction_is_not_counted_twice(self):
        self.say("predict: the cache key ignores locale")
        self.assertIn("already recorded", self.say("predict: the cache key ignores locale"))
        self.assertEqual(len(self.state()["task"]["predictions"]), 1)

    def test_the_model_sees_live_state_every_turn(self):
        self.assertIn("edits are locked", self.say("what does this module do?"))
        self.say("/predict x")
        self.assertIn("1 prediction(s) logged", self.say("carry on"))

    def test_subagent_prompts_are_ignored(self):
        self.assertEqual(self.say("/dial dark", agent_id="a1"), "")
        self.assertEqual(self.state()["dial"], "dim")

    def test_predictions_are_not_mined_from_pasted_documents(self):
        self.say("x" * 5000 + "\npredict: buried in a paste")
        self.assertIsNone(self.state()["task"])


class GuardHuman(Harness):
    def setUp(self):
        super().setUp()
        self.on("lit")
        self.say("/predict x")
        self.write("jobs.py", STUB)

    def test_filling_the_region_is_denied(self):
        reason = self.denied("guard_human", "Edit", "jobs.py", old_string="    raise NotImplementedError",
                             new_string="    return conn.execute('select ... for update skip locked')")
        self.assertIn("user's to write", reason)
        self.assertEqual(self.state()["work"]["blocked"], 1)

    def test_editing_outside_the_region_is_allowed(self):
        self.allowed("guard_human", "Edit", "jobs.py", old_string="status = 'free'", new_string="status = 'ready'")

    def test_deleting_or_rewording_a_marker_is_denied(self):
        self.denied("guard_human", "Edit", "jobs.py", old_string="    # END TODO(human)\n", new_string="")
        self.denied("guard_human", "Edit", "jobs.py", old_string="two workers must never", new_string="workers never")

    def test_a_write_must_carry_the_region_through_unchanged(self):
        self.allowed("guard_human", "Write", "jobs.py", content="import sqlite3\n\n" + STUB)
        self.denied("guard_human", "Write", "jobs.py", content=STUB.replace("raise NotImplementedError", "return 1"))
        self.denied("guard_human", "Write", "jobs.py", content="def claim(conn, worker):\n    return 1\n")

    def test_replace_all_that_reaches_into_the_region_is_denied(self):
        self.denied("guard_human", "Edit", "jobs.py", old_string="job", new_string="task", replace_all=True)
        self.allowed("guard_human", "Edit", "jobs.py", old_string="conn", new_string="db", replace_all=True)

    def test_multiedit_is_checked_as_a_whole(self):
        edits = [{"old_string": "status = 'free'", "new_string": "status = 'ready'"},
                 {"old_string": "raise NotImplementedError", "new_string": "return 1"}]
        self.denied("guard_human", "MultiEdit", "jobs.py", edits=edits)
        self.allowed("guard_human", "MultiEdit", "jobs.py", edits=edits[:1])

    def test_an_edit_that_cannot_be_simulated_fails_closed(self):
        reason = self.denied("guard_human", "Edit", "jobs.py", old_string="not in the file", new_string="x")
        self.assertIn("cannot be checked", reason)
        self.write("plain.py", "x = 1\n")
        self.allowed("guard_human", "Edit", "plain.py", old_string="not in the file", new_string="x")

    def test_an_unterminated_region_runs_to_the_end_of_the_file(self):
        self.write("open.py", "a = 1\n# TODO(human): finish this\nb = 2\n")
        self.denied("guard_human", "Edit", "open.py", old_string="b = 2", new_string="b = 3")
        self.allowed("guard_human", "Edit", "open.py", old_string="a = 1", new_string="a = 0")

    def test_the_agent_may_place_a_region_and_it_is_tracked(self):
        self.write("new.py", "def f():\n    pass\n")
        self.allowed("guard_human", "Edit", "new.py", old_string="    pass\n",
                     new_string="    # TODO(human): the core\n    raise NotImplementedError\n    # END TODO(human)\n")
        self.assertEqual(self.state()["work"]["todos"], {"new.py": 1})
        self.assertIn("new.py", self.state()["region_files"])
        self.assertEqual(self.events("todo_placed")[0]["file"], "new.py")

    def test_the_budget_is_two_regions_per_task(self):
        region = "# TODO(human): %d\npass\n# END TODO(human)\n"
        self.allowed("guard_human", "Write", "one.py", content=region % 1 + region % 2)
        with replib.state_tx(self.root) as state:
            self.assertEqual(sum(state["work"]["todos"].values()), 2)
        reason = self.denied("guard_human", "Write", "three.py", content=region % 3)
        self.assertIn("budget", reason)

    def test_prose_that_mentions_the_marker_is_not_a_region(self):
        text = "Place a `TODO(human)` marker on core logic.\n- TODO(human) regions are protected\n"
        self.assertEqual(replib.regions(text), [])
        for lead in ("# ", "// ", "-- ", "    # ", "<!-- ", "{/* ", "/* "):
            self.assertEqual(len(replib.regions(lead + "TODO(human): x\ncode\n" + lead + "END TODO(human)\n")), 1, lead)

    def test_skip_todo_releases_the_regions(self):
        self.assertIn("released", self.say("/skip todo"))
        self.allowed("guard_human", "Edit", "jobs.py", old_string="    raise NotImplementedError", new_string="    return 1")
        self.assertIn("todo", self.state()["task"]["skipped"])

    def test_bare_skip_releases_the_region_when_that_is_what_is_pending(self):
        self.say("ok")   # registers jobs.py
        self.assertIn("released", self.say("/skip"))
        self.assertTrue(self.state()["work"]["todo_released"])

    def test_harness_state_is_not_editable(self):
        reason = self.denied("guard_human", "Write", ".rep/state.json", content='{"dial": "dark"}')
        self.assertIn("harness state", reason)
        self.denied("guard_human", "Edit", ".rep/state.json", old_string='"lit"', new_string='"dark"')

    def test_dial_dark_switches_every_gate_off_and_logs_the_skip(self):
        self.say("ok")   # registers jobs.py
        self.say("/dial dark")
        self.allowed("guard_human", "Edit", "jobs.py", old_string="    raise NotImplementedError", new_string="    return 1")
        self.assertEqual([e["what"] for e in self.events("skip")], ["todo"])
        self.assertIn("todo", self.state()["task"]["skipped"])

    def test_dark_has_no_region_guard_and_lit_brings_it_back(self):
        self.say("/dial dark")
        with replib.state_tx(self.root) as state:
            state["work"]["todo_released"] = False   # isolate the dial from the release flag
        self.allowed("guard_human", "Edit", "jobs.py", old_string="    raise NotImplementedError", new_string="    return 1")
        self.assertEqual(self.bash("sed -i '' s/raise/return/ jobs.py", lambda: self.write("jobs.py", "gone\n")), (None, ""))
        self.write("jobs.py", STUB)
        self.say("/dial lit")
        self.denied("guard_human", "Edit", "jobs.py", old_string="    raise NotImplementedError", new_string="    return 1")

    def test_a_region_typed_by_hand_is_found_on_the_next_prompt(self):
        self.write("hand.py", "# TODO(human): mine\npass\n# END TODO(human)\n")
        self.assertIn("hand.py", self.say("ok"))
        self.assertIn("hand.py", self.state()["region_files"])

    def test_hints_climb_one_level_per_request(self):
        self.say("ok")   # registers jobs.py
        self.assertIn("hint 1/5", self.say("/hint"))
        self.assertIn("hint 2/5", self.say("/hint"))
        for _ in range(5):
            last = self.say("/hint")
        self.assertIn("hint 5/5", last)
        self.assertEqual(self.state()["work"]["hints"], 5)


class LockTests(Harness):
    def setUp(self):
        super().setUp()
        self.on("lit")
        self.say("/predict x")
        self.write("tests/test_jobs.py", "def test_one():\n    assert True\n")
        self.write("jobs.py", "x = 1\n")

    def test_a_pinned_test_cannot_be_edited(self):
        self.assertIn("tests/test_jobs.py", self.say("/pin tests/test_jobs.py"))
        reason = self.denied("lock_tests", "Edit", "tests/test_jobs.py", old_string="True", new_string="False")
        self.assertIn("owns the oracle", reason)
        self.allowed("lock_tests", "Edit", "jobs.py", old_string="1", new_string="2")

    def test_pinning_a_directory_pins_its_files(self):
        self.say("/pin tests")
        self.assertEqual(list(self.state()["pins"]), ["tests/test_jobs.py"])

    def test_pins_hold_in_dim_and_release_in_dark(self):
        self.say("/pin tests/test_jobs.py")
        self.say("/dial dim")
        self.denied("lock_tests", "Write", "tests/test_jobs.py", content="")
        self.say("/dial dark")
        self.allowed("lock_tests", "Write", "tests/test_jobs.py", content="")

    def test_lit_reminds_the_model_that_the_user_owns_the_tests(self):
        self.assertIn("nothing pinned yet", self.say("ok"))
        self.say("/pin tests/test_jobs.py")
        self.assertIn("read-only for you", self.say("ok"))

    def test_a_missing_path_is_reported(self):
        self.assertIn("Not found: nope.py", self.say("/pin nope.py"))


class BashHole(Harness):
    def setUp(self):
        super().setUp()
        self.on("lit")
        self.say("/predict x")
        self.write("jobs.py", STUB)
        self.write("tests/test_jobs.py", "def test_one():\n    assert True\n")
        self.say("/pin tests/test_jobs.py")

    def test_the_agent_cannot_run_the_users_commands(self):
        for command in ("rep dial dark", "cd src && rep skip", "rep   pin tests/x.py", "~/.claude/bin/rep dial dark",
                        "$HOME/.claude/bin/rep unpin", "true; rep close"):
            decision, reason = self.bash(command)
            self.assertEqual(decision, "deny", command)
            self.assertIn("user's command", reason)
        self.assertEqual(self.state()["dial"], "lit")

    def test_the_agent_keeps_its_own_commands(self):
        for command in ("rep status", "rep report", "rep log retro --lesson x", "grep dial config.py", "prep dial"):
            self.assertEqual(self.bash(command)[0], None, command)

    def test_the_state_directory_is_off_limits_from_the_shell(self):
        for command in ("echo '{}' > .rep/state.json", "cat .rep/reps.jsonl", "rm -rf .rep", "cd .rep && ls",
                        "python3 -c \"open('.rep/state.json','w')\""):
            self.assertEqual(self.bash(command)[0], "deny", command)
        for command in ("ls -la", "cat foo.rep", "git status", "cat .replit"):
            self.assertEqual(self.bash(command)[0], None, command)

    def test_filling_a_region_through_bash_is_caught_and_can_be_restored(self):
        fill = lambda: self.write("jobs.py", STUB.replace("raise NotImplementedError", "return 1"))  # noqa: E731
        decision, reason = self.bash("sed -i '' 's/raise NotImplementedError/return 1/' jobs.py", fill)
        self.assertEqual(decision, "block")
        self.assertIn("jobs.py", reason)
        self.assertEqual(self.state()["work"]["violations"], 1)
        self.assertEqual(self.events("violation")[0]["files"], ["jobs.py"])
        self.assertEqual(self.bash("rep restore jobs.py", lambda: self.rep("restore", "jobs.py")), (None, ""))
        self.assertEqual(self.read("jobs.py"), STUB)
        self.assertEqual(self.state()["work"]["violations"], 1)
        # Chaining something onto the restore gets no such pass.
        self.assertEqual(self.bash("rep restore jobs.py; sed ...", lambda: self.write("jobs.py", "gone\n"))[0], "block")

    def test_changing_a_pinned_test_through_bash_is_caught(self):
        decision, reason = self.bash("echo > tests/test_jobs.py", lambda: self.write("tests/test_jobs.py", "\n"))
        self.assertEqual(decision, "block")
        self.assertIn("tests/test_jobs.py", reason)
        decision, _ = self.bash("rm tests/test_jobs.py", lambda: os.remove(os.path.join(self.root, "tests/test_jobs.py")))
        self.assertEqual(decision, "block")

    def test_a_command_that_leaves_protected_files_alone_is_quiet(self):
        outside = lambda: self.write("jobs.py", STUB.replace("status = 'free'", "status = 'ready'"))  # noqa: E731
        self.assertEqual(self.bash("sed -i '' s/free/ready/ jobs.py", outside), (None, ""))
        self.assertEqual(self.bash("pytest -q"), (None, ""))

    def test_the_users_own_edits_between_commands_are_not_blamed_on_the_agent(self):
        self.write("jobs.py", STUB.replace("raise NotImplementedError", "return conn.claim(worker)"))
        self.assertEqual(self.bash("pytest -q"), (None, ""))

    def test_a_cd_inside_the_command_does_not_lose_the_repo(self):
        self.n += 1
        base = {"tool_name": "Bash", "tool_input": {"command": "cd /tmp && sed ..."}, "tool_use_id": "toolu_cd"}
        self.hook("bash_guard", dict(base, hook_event_name="PreToolUse"))
        self.write("jobs.py", "gone\n")
        post = self.hook("bash_audit", dict(base, hook_event_name="PostToolUse", cwd="/tmp"))
        self.assertEqual(post.get("decision"), "block")


class Retro(Harness):
    def setUp(self):
        super().setUp()
        self.on()
        self.say("fix the duplicate job execution bug that shows up under load")
        self.say("predict: no lock between select and update")

    def test_the_agent_cannot_log_a_retro_the_user_did_not_start(self):
        proc = self.rep("log", "retro", "--grade", "match", ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("/retro", proc.stderr)
        self.assertIsNotNone(self.state()["task"])

    def test_retro_writes_the_reps_and_rearms_the_gate(self):
        brief = self.say("/retro")
        self.assertIn("no lock between select and update", brief)
        self.rep("log", "retro", "--grade", "1=partial", "--evidence", "1=the lock existed but was taken too late",
                 "--surprise", "advisory locks were already in use", "--lesson", "queue claims must be atomic",
                 "--codify", "test", "--path", "tests/test_claim.py", "--skills", "craft.data.concurrency")
        self.assertIsNone(self.state()["task"])
        predict, retro = self.reps()
        self.assertEqual((predict["id"], predict["phase"], predict["outcome"]), ("t0001.p1", "research", "partial"))
        self.assertEqual(predict["prediction"], "no lock between select and update")
        self.assertEqual(predict["skills"], ["craft.data.concurrency"])
        self.assertEqual(retro["codified"], {"kind": "test", "path": "tests/test_claim.py"})
        self.assertEqual(retro["lesson"], "queue claims must be atomic")
        self.write("a.py", "x = 1\n")
        self.denied("predict_gate", "Edit", "a.py", old_string="1", new_string="2")

    def test_the_brief_presents_the_goal_as_history(self):
        brief = self.say("/retro")
        self.assertIn("not a request", brief)
        self.assertIn("duplicate job execution", brief)

    def test_a_goal_stated_in_another_session_is_not_borrowed(self):
        self.say("/retro skip")
        self.say("add a comment line at the very top of the queue module, right now")
        self.session = "s2"
        self.say("predict: the cache key ignores locale")
        self.assertEqual(self.state()["task"]["goal"], "")

    def test_a_bad_grade_is_refused_and_the_task_stays_open(self):
        self.say("/retro")
        self.assertNotEqual(self.rep("log", "retro", "--grade", "2=match", ok=False).returncode, 0)
        self.assertIsNotNone(self.state()["task"])

    def test_retro_skip_closes_and_logs_the_skip(self):
        self.assertIn("closed without a retro", self.say("/retro skip"))
        self.assertEqual([r["outcome"] for r in self.reps()], [None, "skipped"])

    def test_a_todo_rep_carries_hints_and_rating(self):
        self.write("jobs.py", STUB)
        self.say("ok")
        self.say("/hint")
        self.say("/retro")
        self.rep("log", "retro", "--grade", "match", "--todo", "done", "--todo-rating", "4")
        todo = [r for r in self.reps() if r["kind"] == "todo"][0]
        self.assertEqual((todo["phase"], todo["outcome"], todo["hintsUsed"], todo["rating"]), ("implement", "match", 1, 4))

    def test_pins_clear_when_the_task_closes(self):
        self.write("tests/test_a.py", "x = 1\n")
        self.say("/pin tests/test_a.py")
        self.say("/retro skip")
        self.assertEqual(self.state()["pins"], {})

    def test_a_dark_task_can_still_have_a_retro(self):
        self.say("/retro skip")
        self.say("/dial dark")
        self.say("/retro")
        self.rep("log", "retro", "--lesson", "nothing new")
        closes = self.events("task_close")
        self.assertEqual([c["dial"] for c in closes], ["dim", "dark"])

    def test_a_stale_task_closes_itself_ungraded(self):
        with replib.state_tx(self.root) as state:
            state["touched_at"] = int(time.time()) - replib.STALE_SECONDS - 60
        self.assertIn("closed ungraded", self.say("new day, new task to work on in this repository"))
        self.assertEqual(self.events("task_close")[0]["reason"], "expired")
        self.assertIsNone(self.state()["task"])

    def test_a_new_session_is_told_about_the_open_task_and_lessons(self):
        self.write("lessons.md", "# Lessons\n\n- 2026-10-03 queue claims must be atomic\n")
        self.session = "s2"
        text = self.say("hello again")
        self.assertIn("still open from an earlier session", text)
        self.assertIn("lessons.md holds 1 lesson", text)

    def test_report_runs_on_real_data(self):
        self.say("/retro")
        self.rep("log", "retro", "--grade", "match", "--lesson", "x", "--codify", "context", "--path", "lessons.md")
        out = self.rep("report").stdout
        self.assertIn("tasks      1 closed (dark 0, dim 1, lit 0)", out)
        self.assertIn("match 1", out)
        self.assertIn("bypasses   0", out)


class FailClosed(Harness):
    def test_a_corrupt_state_file_locks_edits(self):
        self.on()
        self.say("/predict x")
        self.write(".rep/state.json", "{not json")
        self.write("a.py", "x = 1\n")
        self.denied("predict_gate", "Edit", "a.py", old_string="1", new_string="2")

    def test_a_crashing_hook_denies_instead_of_waving_the_edit_through(self):
        self.on()
        self.write("a.py", "x = 1\n")
        decision, reason = self.edit("guard_human", "Edit", "a.py", old_string=["not", "a", "string"], new_string="2")
        self.assertEqual(decision, "deny")
        self.assertIn("failing closed", reason)


if __name__ == "__main__":
    unittest.main()
