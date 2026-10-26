"""Tests for ticket_queue.py with no model, no docker and no tracker: a fake
harness (a script that acts as the agent of each stage) and a fake proxy (an
HTTP server that answers as the verifier) drive the whole loop on a throwaway
git project with a bare remote.

  python3 tests/queue_test.py      (tests/unit_queue_test.sh runs it)
"""
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import ticket_queue as tq  # noqa: E402

TICKET = "# ABC-1: Add a feature file\n\nPlease add a feature file that says hello feature.\n"

# The agent of every stage. Reads the stage from the prompt (the last argument)
# and does what FAKE_* in its environment say.
FAKE_AGENT = r'''
import os, re, subprocess, sys
args = sys.argv[1:]
vault = os.environ["FAKE_VAULT"]
log = open(os.environ["FAKE_CALLS"], "a")
if args and args[0] == "queue":
    log.write("queue " + args[1] + "\n")
    if args[1] == "_pull":
        os.makedirs(os.path.join(vault, ".raw", "tickets"), exist_ok=True)
        open(os.path.join(vault, ".raw", "tickets", args[2] + ".md"), "w").write(os.environ["FAKE_TICKET"])
    sys.exit(0)
prompt = args[-1]
if os.environ.get("FAKE_SLEEP"):
    import time
    time.sleep(int(os.environ["FAKE_SLEEP"]))
assert "--ticket" in args and "-p" in args and "--yolo" in args, args
stage = re.match(r"You are (\w+)", prompt).group(1)
log.write(stage + " " + " ".join(a for a in args[:-1]) + "\n")
def path(label):
    p = re.search(label + r" (\S+?)[ )\n]", prompt).group(1)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p
def git(*a):
    subprocess.run(["git", *a], check=True, capture_output=True)
if stage == "planning":
    blocking = os.environ.get("FAKE_BLOCKING") and "Use the blue one" not in prompt
    qs = "- [blocking] Which colour? Default: blue\n" if blocking else "- None\n"
    crit = os.environ.get("FAKE_CRITERIA", "- A file feature.txt says hello feature\n")
    open(path("Write the plan to"), "w").write(
        "# ABC-1 plan\n## Goal\nA feature file.\n## Acceptance criteria\n" + crit +
        "## Approach\nWrite feature.txt.\n## Files\n- feature.txt: new\n## Questions\n" + qs +
        "## Assumptions\n- None\n")
elif stage in ("implementing", "fixing"):
    if os.environ.get("FAKE_NO_COMMIT") or (stage == "fixing" and os.environ.get("FAKE_FIX_NOOP")):
        sys.exit(0)
    if stage == "fixing" and os.environ.get("FAKE_FIX_REJECT"):  # the review item is wrong
        git("commit", "-q", "--allow-empty", "-m", "ABC-1: review fixes",
            "-m", "Left as it is: feature.txt already says hello feature once, which the ticket asks for")
        sys.exit(0)
    if os.environ.get("FAKE_EDIT_PLAN"):  # an agent trying to move the goalposts
        open(os.path.join(vault, "wiki", "notes", "tickets", "ABC-1-plan.md"), "w").write(
            "## Acceptance criteria\n- Nothing at all\n")
    name = os.environ.get("FAKE_FILE", "feature.txt")
    os.makedirs(os.path.dirname(name) or ".", exist_ok=True)
    with open(name, "a") as f:
        f.write("hello feature\n" if stage == "implementing" else "fixed by review\n")
    git("add", name)
    git("commit", "-q", "-m", "ABC-1: " + stage)
    with open(path("append one bullet to"), "a") as f:
        f.write("- other.py:3: an unrelated bug (seen while " + stage + " ABC-1)\n")
elif stage == "reviewing":
    must = "- feature.txt:1: say it twice\n" if os.environ.get("FAKE_MUSTFIX") else "- None\n"
    open(path("Write the review to"), "w").write("# ABC-1 review\n## Must fix\n" + must + "## Notes\n- None\n")
'''


class FakeProxy(http.server.BaseHTTPRequestHandler):
    mode = "ok"        # ok | down | badquote | reject (the verifier's request fails, the probe does not)
    seen = []

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(200, {"data": [{"id": "free"}, {"id": "free-withtools"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = req["messages"][-1]["content"]
        FakeProxy.seen.append((req["model"], self.headers.get("x-harness-raw"), prompt))
        if FakeProxy.mode == "down":
            return self.reply(502, {"error": {"message": "upstream down"}})
        if FakeProxy.mode == "reject" and "check a" in prompt:
            return self.reply(400, {"error": {"message": "context too long"}})
        m = re.search(r"numbered:\n((?:\d+\. .*\n)+)", prompt)
        n = len(m.group(1).splitlines()) if m else 0
        if "check a plan" in prompt:
            crit = [{"n": i, "ticket_quote": "add a feature file", "ok": True, "why": "asked"} for i in range(1, n + 1)]
        elif "check a code change" in prompt:
            quote = "hello feature" if FakeProxy.mode != "badquote" else "a line that is not there"
            crit = [{"n": i, "met": True, "quote": quote, "why": "added"} for i in range(1, n + 1)]
        else:
            return self.reply(200, {"choices": [{"message": {"content": "OK"}}]})
        text = "Here it is:\n```json\n" + json.dumps({"criteria": crit, "verdict": "PASS", "problems": []}) + "\n```"
        self.reply(200, {"choices": [{"message": {"content": text}}]})


def put(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def cat(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeProxy)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.proxy = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = self.tmp
        self.remote = os.path.join(t, "remote.git")
        self.project = os.path.join(t, "project")
        self.vault = os.path.join(t, "vault")
        self.state = os.path.join(t, "state")
        os.makedirs(self.vault)
        sh("git", "init", "-q", "--bare", self.remote)
        sh("git", "init", "-q", "-b", "main", self.project)
        for k, v in (("user.name", "T"), ("user.email", "t@example.com")):
            sh("git", "-C", self.project, "config", k, v)
        put(os.path.join(self.project, "README"), "project\n")
        sh("git", "-C", self.project, "add", "README")
        sh("git", "-C", self.project, "commit", "-q", "-m", "init")
        sh("git", "-C", self.project, "remote", "add", "origin", self.remote)
        agent = os.path.join(t, "fake_agent.py")
        put(agent, FAKE_AGENT)
        self.harness = os.path.join(t, "harness")
        put(self.harness, f'exec {sys.executable} {agent} "$@"\n')
        self.calls = os.path.join(t, "calls")
        self.env_keep = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("FAKE_"):
                del os.environ[k]
        os.environ.update(FAKE_VAULT=self.vault, FAKE_CALLS=self.calls, FAKE_TICKET=TICKET)
        FakeProxy.mode = "ok"
        FakeProxy.seen = []

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_keep)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = tq.main(["ticket_queue.py", "--state", self.state, *args])
        return rc, out.getvalue() + err.getvalue()

    def add(self, *keys):
        rc, out = self.q("add", *keys, "--project", self.project)
        self.assertEqual(rc, 0, out)

    def run_queue(self, *extra):
        return self.q("run", "--vault", self.vault, "--proxy", self.proxy, "--harness", self.harness,
                      "--default-model", "free-withtools", *extra)

    def status(self, key="ABC-1"):
        return tq.Queue(self.state).load(key)

    def calls_of(self, stage):
        return [l for l in cat(self.calls).splitlines() if l.startswith(stage)]


class QueueTest(Base):
    def test_whole_loop_pushes_the_branch(self):
        os.environ["FAKE_MUSTFIX"] = "1"
        self.add("abc-1")
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("done", "done"), out)
        # Every stage ran once, the fix because the review had a Must fix item.
        for stage in ("planning", "implementing", "reviewing", "fixing"):
            self.assertEqual(len(self.calls_of(stage)), 1, stage)
        # The free twin of the -withtools default, on the agent and the verifier.
        self.assertIn("-m harness/free", self.calls_of("planning")[0])
        self.assertTrue(all(m == "free" and raw == "1" for m, raw, _ in FakeProxy.seen))
        # The branch reached the remote and the project, with both commits.
        log = sh("git", "-C", self.remote, "log", "--format=%s", "ABC-1")
        self.assertIn("ABC-1: implementing", log)
        self.assertIn("ABC-1: fixing", log)
        self.assertEqual(sh("git", "-C", self.project, "rev-parse", "ABC-1"),
                         sh("git", "-C", self.remote, "rev-parse", "ABC-1"))
        # The branch is named after the ticket alone.
        refs = sh("git", "-C", self.remote, "for-each-ref", "--format=%(refname)").split()
        self.assertIn("refs/heads/ABC-1", refs)
        self.assertFalse([r for r in refs if "ticket/" in r], refs)
        # The agent's clone has no remote to push to.
        self.assertEqual(sh("git", "-C", os.path.join(self.state, "ABC-1", "repo"), "remote"), "")
        # Each stage's session was captured, then the outcome; the ticket was pulled first.
        self.assertEqual(self.calls_of("queue _pull"), ["queue _pull"])
        self.assertEqual(len(self.calls_of("queue _sync")), 5)
        # The queue's log, verdicts and outcome included, is in the vault.
        vlog = cat(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-1-log.md"))
        self.assertTrue(vlog.startswith("---\ntype: note\nticket: ABC-1\n"), vlog[:80])
        self.assertIn("plan verifier: PASS", vlog)
        self.assertIn("pushed branch ABC-1 to", vlog)
        rc, out = self.q("findings", "--vault", self.vault)
        self.assertIn("an unrelated bug", out)
        self.assertIn("plan verifier: PASS", cat(os.path.join(self.state, "run.log")))

    def test_blocking_question_waits_for_an_answer(self):
        os.environ["FAKE_BLOCKING"] = "1"
        self.add("ABC-1", "ABC-2")
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        # ABC-1 waits; the queue went on to ABC-2.
        self.assertEqual(self.status()["status"], "needs-answers")
        self.assertEqual(self.status("ABC-2")["status"], "needs-answers")
        self.assertEqual(len(self.calls_of("implementing")), 0)
        rc, out = self.q("answer", "ABC-1", "--vault", self.vault, "--text", "Use the blue one.")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.status()["status"], self.status()["stage"]), ("queued", "plan"))
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        # The answer reached the planner in its prompt.
        answers = cat(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-1-answers.md"))
        self.assertIn("Use the blue one.", answers)

    def test_no_commit_is_retried_once_then_blocked(self):
        os.environ["FAKE_NO_COMMIT"] = "1"
        self.add("ABC-1")
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("blocked", "implement"), out)
        self.assertEqual(len(self.calls_of("implementing")), 2)
        self.assertIn("no commits on branch ABC-1", st["reason"])
        # The retry was told what was wrong.
        log = cat(os.path.join(self.state, "ABC-1", "log.md"))
        self.assertIn("implement attempt 1 failed", log)
        # retry queues it again at the same stage.
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual((self.status()["status"], self.status()["stage"]), ("queued", "implement"))

    def test_a_made_up_quote_fails_the_change(self):
        FakeProxy.mode = "badquote"
        self.add("ABC-1")
        rc, out = self.run_queue("--retries", "0")
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("blocked", "implement"), out)
        self.assertIn("quoted diff text is not in the diff", st["reason"])

    def test_ci_change_is_refused(self):
        os.environ["FAKE_FILE"] = ".github/workflows/x.yml"
        self.add("ABC-1")
        rc, out = self.run_queue("--retries", "0")
        self.assertEqual(self.status()["status"], "blocked", out)
        self.assertIn(".github/", self.status()["reason"])
        self.assertEqual(sh("git", "-C", self.remote, "branch"), "")

    def test_upstream_down_stops_the_run_and_keeps_the_ticket(self):
        FakeProxy.mode = "down"
        self.add("ABC-1", "ABC-2")
        rc, out = self.run_queue()
        self.assertEqual(rc, 3, out)
        self.assertIn("the upstream does not answer", out)
        self.assertEqual((self.status()["stage"], self.status()["attempt"]), ("plan", 0))
        self.assertEqual(self.status("ABC-2")["status"], "queued")
        # Back up: the run carries on from the same place.
        FakeProxy.mode = "ok"
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["status"], "done")
        self.assertEqual(self.status("ABC-2")["status"], "done")

    def test_plan_review_holds_until_approved(self):
        self.add("ABC-1")
        rc, out = self.run_queue("--plan-review", "1", "--push", "0")
        self.assertEqual((self.status()["status"], self.status()["stage"]), ("plan-review", "implement"), out)
        rc, out = self.q("approve", "ABC-1")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue("--plan-review", "1", "--push", "0")
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(len(self.calls_of("planning")), 1)
        # QUEUE_PUSH=0: the branch is in the project only.
        sh("git", "-C", self.project, "rev-parse", "--verify", "ABC-1")
        self.assertEqual(sh("git", "-C", self.remote, "branch"), "")

    def test_add_list_remove(self):
        self.add("ABC-1")
        rc, out = self.q("add", "ABC-1", "--project", self.project)
        self.assertEqual(rc, 1)
        self.assertIn("already in the queue", out)
        rc, out = self.q("add", "nope", "--project", self.project)
        self.assertEqual(rc, 1)
        rc, out = self.q("add", "ABC-2", "--project", self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("not in a git repository", out)
        rc, out = self.q("list")
        self.assertRegex(out, r"ABC-1\s+queued\s+plan")
        self.q("remove", "ABC-1")
        self.assertFalse(os.path.exists(os.path.join(self.state, "ABC-1")))
        self.assertEqual(tq.Queue(self.state).order(), [])

    def test_check_plan(self):
        plan = ("## Goal\nx\n## Acceptance criteria\n- one\n- two\n## Approach\nx\n## Files\n- a\n"
                "## Questions\n- [blocking] a? Default: b\n- [non-blocking] c? Default: d\n- e?\n- [blocking] f? Default: g\n"
                "## Assumptions\n- None\n")
        problems, crit, qs = tq.check_plan(plan)
        self.assertEqual(crit, ["one", "two"])
        self.assertEqual(len(tq.blocking(qs)), 2)
        self.assertTrue(any("4 questions" in p for p in problems))
        self.assertTrue(any("without 'Default:'" in p for p in problems))
        problems, _, _ = tq.check_plan("## Goal\nx\n")
        self.assertTrue(any("Acceptance criteria" in p for p in problems))
        self.assertEqual(tq.check_plan("")[0], ["the plan file was not written"])

    def test_secrets_and_scope(self):
        st = {"key": "ABC-1"}
        diff = "+++ b/a.py\n+token = 'AKIAABCDEFGHIJKLMNOP'\n"
        problems, warnings = tq.check_change(st, ("sha", diff, ["a.py", "id_rsa"], 1), "## Files\n- b.py: x\n")
        self.assertTrue(any("key or token" in p for p in problems))
        self.assertTrue(any("id_rsa" in p for p in problems))
        self.assertEqual(warnings, ["changed but not in the plan's Files: a.py, id_rsa"])
        os.environ["TRACKER_TOKEN"] = "s3cr3t-value"
        problems, _ = tq.check_change(st, ("sha", "+x = 's3cr3t-value'\n", ["b.py"], 1), "## Files\n- b.py\n")
        self.assertEqual(problems, ["the diff contains the value of TRACKER_TOKEN; remove it"])

    def test_json_object(self):
        self.assertEqual(tq.json_object('text {"a": "}{", "b": {"c": 1}} more'), {"a": "}{", "b": {"c": 1}})
        self.assertIsNone(tq.json_object("no json"))

    def test_prompts_have_no_unfilled_placeholders(self):
        st = {"key": "ABC-1", "base_sha": "abc", "feedback": ""}
        ctx = type("C", (), {"vault": self.vault, "q": tq.Queue(self.state)})()
        v = tq.common(ctx, st)
        for name in ("plan", "implement", "review", "fix"):
            text = tq.render(name, DIFF="d", MUST_FIX="m", **v)
            self.assertNotIn("{{", text, name)
            self.assertLess(len(text), 120_000)
        for name, vals in (("verify_plan", dict(TICKET="t", PLAN="p", CRITERIA="1. c")),
                           ("verify_change", dict(TICKET="t", CRITERIA="1. c", DIFF="d", COMMITS="m"))):
            self.assertNotIn("{{", tq.render(name, **vals), name)


class ReviewFixesTest(Base):
    """What the first review of the queue found."""

    def test_fix_without_a_new_commit_fails(self):
        os.environ.update(FAKE_MUSTFIX="1", FAKE_FIX_NOOP="1")
        self.add("ABC-1")
        rc, out = self.run_queue("--retries", "0")
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("blocked", "fix"), out)
        self.assertIn("no new commit since the review", st["reason"])

    def test_fix_is_checked_against_the_must_fix_items(self):
        os.environ["FAKE_MUSTFIX"] = "1"
        self.add("ABC-1")
        self.run_queue()
        fix_prompts = [pr for _, _, pr in FakeProxy.seen if "check a code change" in pr][-1]
        self.assertIn("2. (review) feature.txt:1: say it twice", fix_prompts)

    def test_a_wrong_review_item_can_be_answered_in_the_commit_message(self):
        os.environ.update(FAKE_MUSTFIX="1", FAKE_FIX_REJECT="1")
        self.add("ABC-1")
        rc, out = self.run_queue("--retries", "0")
        self.assertEqual(self.status()["status"], "done", out)
        prompt = [pr for _, _, pr in FakeProxy.seen if "check a code change" in pr][-1]
        self.assertIn("Left as it is: feature.txt already says hello feature once", prompt)

    def test_run_lock_is_held_by_one_process(self):
        path = os.path.join(self.tmp, "run.lock")
        held = open(path, "a")
        self.addCleanup(held.close)
        self.assertTrue(tq.try_lock(held))
        other = subprocess.run([sys.executable, "-c",
                                "import sys; sys.path.insert(0, sys.argv[1]); import vault_ingest as v; "
                                "sys.exit(0 if v.try_lock(open(sys.argv[2], 'a')) else 3)",
                                os.path.dirname(tq.__file__), path])
        self.assertEqual(other.returncode, 3)

    def test_diff_quotes_must_come_from_changed_lines(self):
        diff = ("diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,3 @@\n"
                " def unchanged_context():\n-    return legacy_value\n+    return shiny_new_value\n")
        crit = ["Return the new value", "Remove the legacy value", "(review) f.py: wrong name"]

        def verdict(*quotes):
            return {"verdict": "PASS", "criteria": [{"n": i, "met": True, "quote": q} for i, q in enumerate(quotes, 1)]}
        ok = tq.judge(verdict("return shiny_new_value", "return legacy_value", "the name is right because x"),
                      crit, diff, "quote", "diff", "The name is right because x.")
        self.assertEqual(ok, [])
        bad = tq.judge(verdict("def unchanged_context():", "def unchanged_context():", "return legacy_value"),
                       crit, diff, "quote", "diff", "The name is right because x.")
        self.assertEqual(len(bad), 3, bad)      # context lines never; removed lines only to remove
        bad = tq.judge(verdict("return legacy_value", "return legacy_value", "return shiny_new_value"),
                       crit, diff, "quote", "diff", "")
        self.assertEqual(len(bad), 1, bad)      # a commit message is no evidence for a plan criterion
        self.assertIn("criterion 1", bad[0])
        bad = tq.judge(verdict("the name is right because x", "return legacy_value", "x"),
                       crit, diff, "quote", "diff", "The name is right because x.")
        self.assertEqual(len(bad), 2, bad)

    def test_an_agent_editing_the_plan_does_not_change_the_criteria(self):
        os.environ["FAKE_EDIT_PLAN"] = "1"
        self.add("ABC-1")
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        prompt = [pr for _, _, pr in FakeProxy.seen if "check a code change" in pr][0]
        self.assertIn("1. A file feature.txt says hello feature", prompt)
        self.assertNotIn("Nothing at all", prompt)

    def test_a_timed_out_stage_is_blocked_not_retried(self):
        os.environ["FAKE_SLEEP"] = "30"
        self.add("ABC-1")
        rc, out = self.run_queue("--stage-timeout", "1")
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("blocked", "plan"), out)
        self.assertIn("ran past 1s", st["reason"])

    def test_a_failed_verifier_request_is_not_an_outage(self):
        FakeProxy.mode = "reject"
        self.add("ABC-1", "ABC-2")
        rc, out = self.run_queue("--retries", "0")
        self.assertEqual(rc, 0, out)
        self.assertIn("the verifier request failed", self.status()["reason"])
        self.assertEqual(self.status("ABC-2")["status"], "blocked")

    def test_an_error_blocks_the_ticket_and_the_queue_goes_on(self):
        keep = tq.PROMPT_MAX_BYTES
        tq.PROMPT_MAX_BYTES = 100
        try:
            self.add("ABC-1", "ABC-2")
            rc, out = self.run_queue()
        finally:
            tq.PROMPT_MAX_BYTES = keep
        self.assertEqual(rc, 0, out)
        for key in ("ABC-1", "ABC-2"):
            self.assertEqual(self.status(key)["status"], "blocked")
            self.assertIn("RuntimeError: the plan prompt is", self.status(key)["reason"])

    def test_answering_starts_from_a_fresh_clone_and_not_after_done(self):
        os.environ["FAKE_NO_COMMIT"] = "1"
        self.add("ABC-1")
        self.run_queue("--retries", "0")
        repo = os.path.join(self.state, "ABC-1", "repo")
        self.assertTrue(os.path.isdir(repo))
        rc, out = self.q("answer", "ABC-1", "--vault", self.vault, "--text", "x")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(repo))
        self.assertNotIn("base_sha", self.status())
        del os.environ["FAKE_NO_COMMIT"]
        self.run_queue()
        self.assertEqual(self.status()["status"], "done")
        rc, out = self.q("answer", "ABC-1", "--vault", self.vault, "--text", "y")
        self.assertEqual(rc, 1)
        self.assertIn("is done", out)

    def test_a_left_over_clone_is_cleared_and_cloned_in_place(self):
        # What an interrupted older run left: repo and repo.new, with read-only
        # files as git's objects are. The clone is made in place (Windows will not
        # rename a directory that something has a file open in).
        self.add("ABC-1")
        for d in ("repo", "repo.new"):
            p = os.path.join(self.state, "ABC-1", d, "objects")
            os.makedirs(p)
            put(os.path.join(p, "pack"), "x")
            os.chmod(os.path.join(p, "pack"), 0o444)
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["status"], "done", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "ABC-1", "repo.new")))
        made = []
        real = tq.git
        tq.git = lambda cwd, *a, **k: made.append(cwd) or real(cwd, *a, **k)
        try:
            st = self.status()
            tq.make_clone(tq.Queue(self.state), st)
        finally:
            tq.git = real
        self.assertNotIn(os.path.join(self.state, "ABC-1", "repo.new"), made)

    def test_a_clone_that_cannot_be_removed_blocks_with_the_reason(self):
        self.add("ABC-1")
        real = tq.rmtree
        tq.rmtree = lambda path: f"could not remove {path}: [WinError 5] Access is denied"
        try:
            rc, out = self.run_queue()
        finally:
            tq.rmtree = real
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("blocked", "plan"), out)
        self.assertIn("could not clone", st["reason"])
        self.assertIn("harness queue retry ABC-1", st["reason"])
        self.assertNotIn("base_sha", st)

    def test_retry_is_not_a_way_round_plan_review(self):
        self.add("ABC-1")
        self.run_queue("--plan-review", "1")
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual(rc, 1)
        self.assertEqual(self.status()["status"], "plan-review")

    def test_quotes_escapes_and_paths(self):
        reply = '{"criteria": [{"n": 1, "met": true, "quote": "msg = \\"{\\" + name"}], "verdict": "PASS"}'
        self.assertEqual(tq.json_object("Sure: " + reply)["criteria"][0]["quote"], 'msg = "{" + name')
        diff = "+++ b/doc.md\n+- alpha one\n+- beta two\n"
        crit = ["lists alpha and beta"]
        self.assertEqual(tq.judge({"verdict": "PASS", "criteria": [{"n": 1, "met": True, "quote": "- alpha one\n- beta two"}]},
                                  crit, diff, "quote", "diff"), [])
        self.assertTrue(tq.judge({"verdict": "PASS", "criteria": [{"n": 1, "met": True, "quote": "gamma delta"}]},
                                 crit, diff, "quote", "diff"))
        self.assertIsNone(tq.SECRET_PATH.search(".env.example"))
        self.assertIsNotNone(tq.SECRET_PATH.search("app/.env.local"))
        self.assertIsNotNone(tq.SECRET_PATH.search(".env"))

    def test_ticket_text_is_not_expanded(self):
        text = tq.render("plan", TICKET="the {{PLAN}} macro", PLAN="SECRET PLAN")
        self.assertIn("the {{PLAN}} macro", text)
        self.assertEqual(text.count("SECRET PLAN"), 1)


if __name__ == "__main__":
    unittest.main()
