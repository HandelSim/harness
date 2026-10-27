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
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

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
    if args[1] == "_stop_agents":
        log.write("stop-agents " + args[2] + "\n")
        log.write("stop-launch " + args[3] + "\n")
        print(os.environ.get("FAKE_STOPPED", "1"))
        sys.exit(int(os.environ.get("FAKE_STOP_RC", "0")))
    if args[1] == "_pull":
        os.makedirs(os.path.join(vault, ".raw", "tickets"), exist_ok=True)
        open(os.path.join(vault, ".raw", "tickets", args[2] + ".md"), "w").write(os.environ["FAKE_TICKET"])
    sys.exit(0)
prompt = args[-1]
assert "--ticket" in args and "-p" in args and "--yolo" in args, args
ticket = args[args.index("--ticket") + 1]
task = os.path.join(vault, "wiki", "notes", "tickets", ticket + "-task.md")
pointer = re.search(r"Your full instructions for the \w+ stage of ticket \S+ are in (\S+?)\. Read it first", prompt)
if pointer:   # the short prompt of a long one on Windows: the agent reads the task file
    open(os.environ["FAKE_CALLS"] + ".pointer", "a").write(prompt + "\n=====\n")
    prompt = open(pointer.group(1), encoding="utf-8").read()
same = os.path.exists(task) and open(task, encoding="utf-8").read() == prompt
open(os.environ["FAKE_CALLS"] + ".task", "a").write(f"{ticket} {same}\n")
stage = re.match(r"You are (\w+)", prompt).group(1)
if os.environ.get("FAKE_EXIT"):   # N:RC: the first N launches die at once, having done nothing
    n, rc = os.environ["FAKE_EXIT"].split(":")
    count = os.environ["FAKE_CALLS"] + ".exits"
    done = len(open(count).read()) if os.path.exists(count) else 0
    if done < int(n):
        open(count, "a").write("x")
        print("launch failed: no container runtime")
        sys.exit(int(rc))
if os.environ.get("FAKE_SLEEP") and os.environ.get("FAKE_SLEEP_KEY", ticket) == ticket:
    import time
    open(os.environ["FAKE_CALLS"] + ".sleeping", "a").write(ticket + "\n")
    if os.environ.get("FAKE_SLEEP_NESTED"):   # a commit there, then stopped while it sleeps
        d = os.environ["FAKE_SLEEP_NESTED"]
        open(os.path.join(d, "agent.txt"), "a").write("before the stop\n")
        subprocess.run(["git", "-C", d, "add", "agent.txt"], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-q",
                        "-m", "agent"], check=True)
    time.sleep(int(os.environ["FAKE_SLEEP"]))
log.write(stage + " " + " ".join(a for a in args[:-1]) + "\n")
open(os.environ["FAKE_CALLS"] + ".card", "a").write(os.environ.get("HARNESS_TASK_CARD", "") + "\n")
open(os.environ["FAKE_CALLS"] + "." + stage, "a").write(prompt + "\n=====\n")
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
    plan = ("# ABC-1 plan\n## Goal\nA feature file.\n## Acceptance criteria\n" + crit +
            "## Approach\nWrite feature.txt.\n## Files\n- feature.txt: new\n## Questions\n" + qs +
            "## Assumptions\n- None\n")
    if os.environ.get("FAKE_ANSWER_DURING") and "planning ticket ABC-2" in prompt:  # the owner answers ABC-1
        subprocess.run([sys.executable, os.environ["FAKE_TQ"], "--state", os.environ["FAKE_STATE"], "answer",
                        "ABC-1", "--vault", vault, "--text", "Use the blue one.", "--go"], check=True, capture_output=True)
    where = os.environ.get("FAKE_PLAN_WHERE")
    if where == "print":      # an agent that could not write the vault path
        print("I could not write the file, so here is the plan:\n\n" + plan)
    elif where == "clone":    # the file name taken from a Windows path the container cannot see
        target = path("Write the plan to")
        open(os.path.join(os.getcwd(), "C:\\x\\" + os.path.basename(target)), "w").write(plan)
    else:
        open(path("Write the plan to"), "w").write(plan)
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
    for op, d in (x.split(":", 1) for x in os.environ.get("FAKE_NESTED", "").split(",") if x):
        me = ("-c", "user.name=A", "-c", "user.email=a@example.com")
        if op == "gitlink":    # a repository of its own, made and added whole
            git("init", "-q", d)
            git("-C", d, *me, "commit", "-q", "--allow-empty", "-m", "new")
            git("add", d)
            continue
        if op == "switch":
            git("-C", d, "switch", "-q", "-c", "agent")
            continue
        if op == "submodule":  # a submodule the ticket adds: .gitmodules names it
            git("-c", "protocol.file.allow=always", "submodule", "add", "-q", os.environ["FAKE_SUB_URL"], d)
            continue
        if op == "touch":      # each changed file written again as it is: no change
            for f in subprocess.run(["git", "-C", d, "ls-files", "-m", "-o", "--exclude-standard"],
                                    capture_output=True, text=True).stdout.split():
                data = open(os.path.join(d, f), "rb").read()
                open(os.path.join(d, f), "wb").write(data)
                os.utime(os.path.join(d, f), (1e9, 1e9))
            continue
        with open(os.path.join(d, "agent.txt"), "a") as f:   # edit, or commit
            f.write("the agent was here\n")
        if op == "commit":
            git("-C", d, "add", "agent.txt")
            git("-C", d, *me, "commit", "-q", "-m", "agent")
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
if os.environ.get("FAKE_HANG_AFTER") and os.environ.get("FAKE_HANG_STAGE", stage) == stage:
    import time   # its work done, the agent does not exit
    open(os.environ["FAKE_CALLS"] + ".hanging", "a").write(stage + "\n")
    time.sleep(int(os.environ["FAKE_HANG_AFTER"]))
'''


class FakeProxy(http.server.BaseHTTPRequestHandler):
    # ok | down | badquote | reject (the verifier's request fails, the probe does not)
    # | hang (the verifier's request gets no answer for hang_secs) | garbage (a verifier reply that is not JSON)
    mode = "ok"
    seen = []
    down_left = 0      # the next N requests get 502 whatever the mode
    hang_secs = 5

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
        if FakeProxy.down_left > 0:
            FakeProxy.down_left -= 1
            return self.reply(502, {"error": {"message": "upstream down"}})
        if FakeProxy.mode == "down":
            return self.reply(502, {"error": {"message": "upstream down"}})
        if FakeProxy.mode == "reject" and "check a" in prompt:
            return self.reply(400, {"error": {"message": "context too long"}})
        if FakeProxy.mode == "hang" and "check a" in prompt:
            time.sleep(FakeProxy.hang_secs)
            return self.reply(200, {"choices": [{"message": {"content": "too late"}}]})
        if FakeProxy.mode == "garbage" and "check a" in prompt:
            return self.reply(200, {"choices": [{"message": {"content": "Looks fine to me, ship it."}}]})
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
        FakeProxy.down_left = 0
        FakeProxy.hang_secs = 5
        self.pauses, tq.INFRA_RETRY_PAUSES = tq.INFRA_RETRY_PAUSES, (0, 0)

    def tearDown(self):
        tq.INFRA_RETRY_PAUSES = self.pauses
        os.environ.clear()
        os.environ.update(self.env_keep)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, *args, defaults=False):
        # add works in place and holds the plan by default; most tests are about a
        # clone that goes on after the plan, so they say so unless they ask otherwise
        if args and args[0] == "add" and not defaults:
            if not {"--in-place", "--clone"} & set(args):
                args += ("--clone",)
            if not {"--review-plan", "--no-plan-approval"} & set(args):
                args += ("--no-plan-approval",)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = tq.main(["ticket_queue.py", "--state", self.state, *args])
        return rc, out.getvalue() + err.getvalue()

    def add(self, *keys):
        rc, out = self.q("add", *keys, "--project", self.project)
        self.assertEqual(rc, 0, out)

    def run_queue(self, *extra):
        # an outage ends the run and no idle watchdog, unless a test asks (the last one given wins)
        return self.q("run", "--vault", self.vault, "--proxy", self.proxy, "--harness", self.harness,
                      "--default-model", "free-withtools", "--outage-wait", "0", "--idle-min", "0", *extra)

    def status(self, key="ABC-1"):
        return tq.Queue(self.state).load(key)

    def calls_of(self, stage):
        return [l for l in cat(self.calls).splitlines() if l.startswith(stage)]

    def git_p(self, *a):
        return sh("git", "-C", self.project, *a).strip()

    def feature_branch(self):
        """origin/feat: a commit adding feature.txt, only on the remote and as a
        remote-tracking ref; main has moved on since it branched."""
        self.git_p("checkout", "-q", "-b", "feat")
        put(os.path.join(self.project, "feature.txt"), "hello feature\n")
        self.git_p("add", "feature.txt")
        self.git_p("commit", "-q", "-m", "feat: hello")
        feat = self.git_p("rev-parse", "HEAD")
        self.git_p("push", "-q", "origin", "feat")
        self.git_p("checkout", "-q", "main")
        self.git_p("branch", "-q", "-D", "feat")
        fork = self.git_p("rev-parse", "HEAD")
        put(os.path.join(self.project, "other.txt"), "later\n")
        self.git_p("add", "other.txt")
        self.git_p("commit", "-q", "-m", "main moves on")
        return feat, fork



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
        # Each stage's task card (HARNESS_TASK_CARD, which harness sends the proxy):
        # the stage, the branch, and done in the words of the stage's checks.
        cards = [json.loads(c) for c in cat(self.calls + ".card").splitlines()]
        self.assertEqual([c["stage"] for c in cards], ["plan", "implement", "review", "fix"])
        self.assertTrue(all(c["branch"] == "ABC-1" and c["attempt"] == 1 and "clone" in c["where"] for c in cards), cards)
        plan_file = os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-1-plan.md")
        self.assertIn(f"the plan is written to {plan_file}: # ABC-1 plan", cards[0]["done"])
        self.assertIn("committed on ABC-1", cards[1]["done"])
        self.assertIn("## Must fix", cards[2]["done"])
        self.assertEqual((cards[3]["must_fix"], "must_fix" in cards[2]), (1, False))
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

    def test_an_uncommitted_agents_md_reaches_the_clone_and_is_never_committed(self):
        put(os.path.join(self.project, "AGENTS.md"), "Use tabs.\n")
        self.add("ABC-1")
        rc, out = self.run_queue()
        self.assertEqual((rc, self.status()["status"]), (0, "done"), out)
        repo = os.path.join(self.state, "ABC-1", "repo")
        self.assertEqual(cat(os.path.join(repo, "AGENTS.md")), "Use tabs.\n")
        self.assertIn("/AGENTS.md", cat(os.path.join(repo, ".git", "info", "exclude")).splitlines())
        self.assertNotIn("AGENTS.md", sh("git", "-C", repo, "status", "--porcelain", "--untracked-files=all"))
        self.assertNotIn("AGENTS.md", sh("git", "-C", self.remote, "ls-tree", "-r", "--name-only", "ABC-1"))
        # A file the clone has already is left alone; copying again adds no second exclude line.
        put(os.path.join(repo, "CLAUDE.md"), "the clone's\n")
        put(os.path.join(self.project, "CLAUDE.md"), "the project's\n")
        tq.copy_instructions(self.project, repo)
        self.assertEqual(cat(os.path.join(repo, "CLAUDE.md")), "the clone's\n")
        self.assertEqual(cat(os.path.join(repo, ".git", "info", "exclude")).splitlines().count("/AGENTS.md"), 1)

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
        # The plan made with the answer is held to be agreed before anything is built.
        self.assertEqual(self.status()["status"], "plan-review", out)
        self.assertEqual(len(self.calls_of("implementing")), 0)
        # More feedback: planned again, held again.
        rc, out = self.q("answer", "ABC-1", "--vault", self.vault, "--text", "Keep the file short.")
        self.assertEqual(rc, 0, out)
        self.run_queue()
        self.assertEqual(self.status()["status"], "plan-review")
        last = cat(self.calls + ".planning").split("\n=====\n")[-2]
        self.assertIn("Keep the file short.", last)
        self.assertIn("revise that plan", last)
        rc, out = self.q("approve", "ABC-1")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        # The answer reached the planner in its prompt.
        answers = cat(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-1-answers.md"))
        self.assertIn("Use the blue one.", answers)

    def test_answered_during_the_run_is_planned_again_in_the_same_run(self):
        os.environ.update(FAKE_BLOCKING="1", FAKE_ANSWER_DURING="1", FAKE_STATE=self.state,
                          FAKE_TQ=os.path.abspath(tq.__file__))
        self.add("ABC-1", "ABC-2")
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        # ABC-1 asked, the queue went on to ABC-2, where the owner answered ABC-1: the run took ABC-1 again.
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(self.status("ABC-2")["status"], "needs-answers")
        self.assertEqual(len([c for c in self.calls_of("planning")]), 3)
        st = self.status()
        results = [(h["stage"], h["result"]) for h in st["history"]]
        self.assertEqual(results[0], ("plan", "questions"))
        self.assertIn(("push", "passed"), results)
        q2 = self.status("ABC-2")["questions"]
        self.assertEqual(q2, [{"text": "[blocking] Which colour? Default: blue", "blocking": True}])

    def test_a_plan_written_in_the_clone_or_printed_is_recovered(self):
        for where in ("clone", "print"):
            with self.subTest(where=where):
                os.environ["FAKE_PLAN_WHERE"] = where
                key = "ABC-1" if where == "clone" else "ABC-2"
                self.add(key)
                rc, out = self.run_queue("--push", "0")
                self.assertEqual(self.status(key)["status"], "done", out)
                plan = cat(os.path.join(self.vault, "wiki", "notes", "tickets", f"{key}-plan.md"))
                self.assertTrue(plan.startswith("## Goal" if where == "print" else "# ABC-1 plan"), plan[:60])
                log = cat(os.path.join(self.state, key, "log.md"))
                self.assertIn("the plan was not at", log)
                # Not left in the clone for the implement stage to commit.
                files = sh("git", "-C", self.project, "show", "--name-only", "--format=", key).split()
                self.assertEqual(files, ["feature.txt"])

    def test_a_failed_attempt_is_in_the_history_with_why(self):
        os.environ["FAKE_CRITERIA"] = ""
        self.add("ABC-1")
        self.run_queue("--retries", "0")
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        failed = [h for h in st["history"] if h["result"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]["stage"], failed[0]["attempt"]), ("plan", 1))
        self.assertTrue(any("lists no criteria" in d for d in failed[0]["details"]), failed[0])

    def test_recovery_never_takes_the_prompt_template(self):
        with tempfile.TemporaryDirectory() as d:
            q = tq.Queue(os.path.join(d, "q"))
            os.makedirs(os.path.join(q.dir("ABC-9"), "repo"))
            ctx = type("C", (), {"q": q, "vault": os.path.join(d, "v")})()
            out = os.path.join(d, "plan-0.out")
            with open(out, "w") as f:
                f.write(tq.render("plan", KEY="ABC-9"))   # the prompt echoed, the agent said nothing
            self.assertEqual(tq.recover_note(ctx, {"key": "ABC-9"}, "plan", time.time(), out), "")
            with open(out, "a") as f:
                f.write("\n## Goal\nFix it.\n## Acceptance criteria\n- it works\n")
            self.assertIn("Fix it.", tq.recover_note(ctx, {"key": "ABC-9"}, "plan", time.time(), out))

    def test_agent_paths(self):
        ctx = type("C", (), {"host": False, "single": False})()
        self.assertEqual(tq.agent_path(ctx, "/x/y"), "/x/y")   # not Windows: as it is
        if os.name != "nt":
            keep = tq.os.name
            tq.os.name = "nt"
            try:
                got = tq.agent_path(ctx, "C:\\Users\\me\\vault\\a.md")
                ctx.host = True
                host = tq.agent_path(ctx, "C:\\Users\\me\\vault\\a.md")
            finally:
                tq.os.name = keep
            self.assertEqual(got, "/c/Users/me/vault/a.md")
            self.assertEqual(host, "C:/Users/me/vault/a.md")   # opencode takes /; JSON needs no escapes

    def test_no_commit_is_retried_once_then_blocked(self):
        os.environ["FAKE_NO_COMMIT"] = "1"
        self.add("ABC-1")
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("blocked", "implement"), out)
        self.assertEqual(len(self.calls_of("implementing")), 2)
        self.assertIn("no commits on branch ABC-1", st["reason"])
        # The card counts the attempts.
        self.assertEqual([json.loads(c)["attempt"] for c in cat(self.calls + ".card").splitlines()][-2:], [1, 2])
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
        # not left "running" with no run on it: back in the queue, saying why
        self.assertEqual(self.status()["status"], "queued")
        self.assertIn("the upstream does not answer", self.status()["reason"])
        self.assertEqual(self.status()["history"][-1]["result"], "interrupted")
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
        self.assertEqual(problems, [])   # format slips are notes, not a failed attempt
        # An unmarked question with no default holds the ticket; one with a default does not.
        self.assertEqual(tq.blocking(qs), ["[blocking] a? Default: b", "e?", "[blocking] f? Default: g"])
        self.assertEqual(tq.blocking(["Which one? Default: the first"]), [])
        # A bold or code-styled marker still counts; a statement is not a question.
        self.assertEqual(tq.blocking(["**[blocking]** Which DB? Default: postgres",
                                      "`[non-blocking]` Log level? Default: info",
                                      "The ticket is clear enough."]),
                         ["**[blocking]** Which DB? Default: postgres"])
        # The ways of saying there are no questions are not questions; criteria keep "None of".
        _, crit, qs = tq.check_plan("## Goal\nx\n## Acceptance criteria\n- None of the tests break\n"
                                    "## Questions\n- No questions.\n- None; the ticket is clear\n- (none)\n")
        self.assertEqual((crit, qs), (["None of the tests break"], []))
        notes = tq.plan_notes(plan)
        self.assertTrue(any("4 questions" in n for n in notes), notes)
        self.assertTrue(any("not marked" in n and "taken as blocking" in n for n in notes), notes)
        problems, _, _ = tq.check_plan("## Goal\nx\n")
        self.assertTrue(any("Acceptance criteria" in p for p in problems))
        # Only Goal and Acceptance criteria are needed.
        self.assertEqual(tq.check_plan("## Goal\nx\n## Acceptance criteria\n- y\n")[0], [])
        self.assertTrue(any("Approach" in n for n in tq.plan_notes("## Goal\nx\n## Acceptance criteria\n- y\n")))
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
        # A plan the agent left in its clone and committed is not part of the change.
        problems, _ = tq.check_change(st, ("sha", "+x\n", ["b.py", "docs/ABC-1-plan.md"], 1), "## Files\n- b.py\n")
        self.assertEqual(problems, ["docs/ABC-1-plan.md: the queue's notes live in the vault, not the branch; git rm it"])

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
        st = self.status()
        self.assertTrue(st["reason"].startswith("verifier failed at plan: the verifier request failed"), st["reason"])
        self.assertIn("harness queue retry ABC-1 asks the verifier again without running the agent", st["reason"])
        self.assertEqual(st["verify_only"]["stage"], "plan")   # the agent's work, kept for that retry
        self.assertEqual(self.status("ABC-2")["status"], "blocked")
        # A 400 is the request's own fault: not tried again as is, once more with a
        # shorter prompt; the agent is never launched again for it.
        self.assertEqual(len([p for _, _, p in FakeProxy.seen if "check a" in p]), 4)
        self.assertEqual(len(self.calls_of("planning")), 2)

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
        rc, out = self.q("answer", "ABC-1", "--vault", self.vault, "--text", "x", "--go")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("base_sha", self.status())   # cloned again by the run (no delete under the lock)
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
            st = dict(self.status(), start="ABC-1")   # carry on the branch the first go made
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


class OwnerControlTest(Base):
    """Stages, an existing ref, owner notes, stop, halt, move, status and retry --stage."""

    def prompts(self, stage):
        path = self.calls + "." + stage
        return cat(path).split("\n=====\n")[:-1] if os.path.exists(path) else []

    def run_bg(self, *extra):
        out = open(os.path.join(self.tmp, "run.out"), "w")
        self.addCleanup(out.close)
        p = subprocess.Popen([sys.executable, tq.__file__, "--state", self.state, "run", "--vault", self.vault,
                              "--proxy", self.proxy, "--harness", self.harness, "--default-model", "free-withtools",
                              *extra], stdout=out, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def wait_for(self, cond, what, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}; run output:\n" + cat(os.path.join(self.tmp, "run.out"))[-2000:])

    def sleeping(self, key="ABC-1"):
        path = self.calls + ".sleeping"
        return os.path.exists(path) and key in cat(path).split()

    def status_json(self):
        rc, out = self.q("status", "--json")
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    def test_add_validates_stages_and_ref(self):
        for bad in ("", "plan,bogus", "fix,push"):
            rc, out = self.q("add", "ABC-1", "--project", self.project, "--stages", bad)
            self.assertEqual(rc, 1, (bad, out))
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--ref", "origin/nope")
        self.assertEqual(rc, 1, out)
        self.assertIn("not a commit", out)
        self.assertIsNone(self.status())
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--stages", "push, Implement")
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertEqual((st["stages"], st["stage"], st["notes"]), (["implement", "push"], "implement", []))

    def test_review_only_of_existing_work_at_a_ref(self):
        feat, fork = self.feature_branch()
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--stages", "review", "--ref", "origin/feat")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.status()["stage"], self.status()["start"]), ("review", "origin/feat"))
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("done", "done"), out)
        self.assertIn("nothing pushed", st["reason"])
        self.assertEqual([len(self.calls_of(s)) for s in ("planning", "implementing", "reviewing", "fixing")],
                         [0, 0, 1, 0])
        # The change reviewed is the existing work, from the merge-base with main.
        self.assertEqual(st["base_sha"], fork)
        repo = os.path.join(self.state, "ABC-1", "repo")
        self.assertEqual(sh("git", "-C", repo, "rev-parse", "ABC-1").strip(), feat)
        review = self.prompts("reviewing")[0]
        self.assertIn("+hello feature", review)
        self.assertNotIn("other.txt", review)
        self.assertIn(tq.NO_PLAN, review)
        self.assertEqual(self.git_p("for-each-ref", "refs/harness-queue-ref"), "")   # the fetch's ref is gone
        self.assertEqual(sh("git", "-C", self.remote, "branch", "--list", "ABC-1"), "")

    def test_must_fix_without_a_fix_stage(self):
        os.environ["FAKE_MUSTFIX"] = "1"
        self.feature_branch()
        for key, stages in (("ABC-1", "review"), ("ABC-2", "review,push")):
            rc, out = self.q("add", key, "--project", self.project, "--stages", stages, "--ref", "origin/feat")
            self.assertEqual(rc, 0, out)
        self.run_queue()
        # Review was the last stage: done, with the findings as the reason.
        st = self.status("ABC-1")
        self.assertEqual(st["status"], "done")
        self.assertIn("1 must-fix item(s)", st["reason"])
        # Something was to follow: held, and the push did not happen.
        st = self.status("ABC-2")
        self.assertEqual((st["status"], st["stage"]), ("blocked", "review"))
        self.assertIn("harness queue retry ABC-2 --stage fix", st["reason"])
        self.assertEqual(sh("git", "-C", self.remote, "branch", "--list", "ABC-2"), "")
        # retry --stage fix adds the stage and goes on from the review.
        rc, out = self.q("retry", "ABC-2", "--stage", "fix")
        self.assertEqual(rc, 0, out)
        st = self.status("ABC-2")
        self.assertEqual((st["status"], st["stage"], st["stages"]), ("queued", "fix", ["review", "fix", "push"]))
        rc, out = self.run_queue()
        self.assertEqual(self.status("ABC-2")["status"], "done", out)
        self.assertEqual(len(self.calls_of("fixing")), 1)
        self.assertIn("ABC-2", sh("git", "-C", self.remote, "branch", "--list", "ABC-2"))
        # No plan and no criteria in the ticket: held to the ticket as a whole, and the review.
        prompt = [pr for _, _, pr in FakeProxy.seen if "check a code change" in pr][-1]
        self.assertIn("1. " + tq.NO_PLAN_CRITERION + "\n2. (review) feature.txt:1: say it twice", prompt)

    def test_implement_without_a_plan_takes_the_tickets_criteria(self):
        os.environ["FAKE_TICKET"] = TICKET + "\n## Acceptance criteria\n- A file says hello feature\n\n## Notes\nx\n"
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--stages", "implement,push")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(len(self.calls_of("planning")), 0)
        self.assertIn(tq.NO_PLAN, self.prompts("implementing")[0])
        prompt = [pr for _, _, pr in FakeProxy.seen if "check a code change" in pr][0]
        self.assertIn("numbered:\n1. A file says hello feature\n\n", prompt)
        self.assertIn("the ticket's 1 acceptance criteria", cat(os.path.join(self.state, "ABC-1", "log.md")))
        self.assertIn("ABC-1", sh("git", "-C", self.remote, "branch", "--list", "ABC-1"))

    def test_owner_notes_reach_every_stage(self):
        os.environ["FAKE_MUSTFIX"] = "1"
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--note", "Use tabs, not spaces.")
        self.assertEqual(rc, 0, out)
        rc, out = self.q("note", "abc-1", "--text", "The {{PLAN}} is at origin/x; review it.")
        self.assertEqual(rc, 0, out)
        self.assertEqual([n["text"] for n in self.status()["notes"]],
                         ["Use tabs, not spaces.", "The {{PLAN}} is at origin/x; review it."])
        self.run_queue()
        self.assertEqual(self.status()["status"], "done")
        for stage in ("planning", "implementing", "reviewing", "fixing"):
            prompt = self.prompts(stage)[0]
            self.assertIn("NOTES_FROM_THE_OWNER", prompt, stage)
            self.assertIn("Use tabs, not spaces.", prompt, stage)
            self.assertIn("The {{PLAN}} is at origin/x; review it.", prompt, stage)
        v = tq.common(type("C", (), {"vault": self.vault, "q": tq.Queue(self.state)})(), {"key": "ABC-9"})
        self.assertEqual(v["NOTES"], "(none)")
        rc, out = self.q("note", "ABC-1", "--text", "  ")
        self.assertEqual(rc, 1)
        rc, out = self.q("note", "ABC-7", "--text", "x")
        self.assertEqual(rc, 1)

    def test_notes_are_capped_and_survive_a_run_saving_old_state(self):
        self.add("ABC-1")
        q = tq.Queue(self.state)
        held_by_run = q.load("ABC-1")        # what a run holds in memory
        self.q("note", "ABC-1", "--text", "x" * 5000)
        held_by_run["stage"] = "implement"
        q.save(held_by_run)                   # the run saves after the note
        st = self.status()
        self.assertEqual(st["stage"], "implement")
        self.assertEqual(len(st["notes"]), 1)
        self.assertIn("cut at 4000", st["notes"][0]["text"])
        with open(os.path.join(self.state, "ABC-1", "state.json")) as f:
            self.assertEqual(len(json.load(f)["notes"]), 1)   # in state.json itself, for the viewer
        for i in range(55):
            q.add_note("ABC-1", f"n{i}")
        notes = self.status()["notes"]
        self.assertEqual((len(notes), notes[-1]["text"]), (50, "n54"))

    def test_stop_a_waiting_ticket_and_resume_it(self):
        self.add("ABC-1", "ABC-2")
        rc, out = self.q("stop", "ABC-1")
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertEqual((st["status"], st["reason"]), ("stopped", "stopped by the owner"))
        self.assertEqual(self.q("stop", "ABC-1")[0], 0)   # already stopped
        rc, out = self.run_queue()
        self.assertEqual(self.status("ABC-2")["status"], "done", out)
        self.assertEqual(self.status()["status"], "stopped")
        self.assertFalse([c for c in cat(self.calls).splitlines() if "--ticket ABC-1" in c])
        self.assertEqual(self.q("stop", "ABC-2")[0], 1)   # done: nothing to stop
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.status()["status"], self.status()["stage"]), ("queued", "plan"))
        self.assertEqual(self.q("retry", "ABC-1")[0], 1)  # queued: nothing to retry
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)

    def test_a_stopped_plan_review_goes_back_to_plan_review(self):
        self.add("ABC-1")
        self.run_queue("--plan-review", "1")
        self.assertEqual(self.status()["status"], "plan-review")
        self.q("stop", "ABC-1")
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.status()["status"], self.status()["stage"]), ("plan-review", "implement"))

    def test_stop_a_running_ticket_with_no_run_left(self):
        self.add("ABC-1")
        q = tq.Queue(self.state)
        st = q.load("ABC-1")
        st["status"] = "running"   # a run that died
        q.save(st)
        rc, out = self.q("stop", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["status"], "stopped")
        self.assertFalse(os.path.exists(q.sentinel("ABC-1")))

    def test_stop_the_running_ticket_and_the_run_goes_on(self):
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1")
        self.add("ABC-1", "ABC-2")
        p = self.run_bg("--push", "0")
        self.wait_for(self.sleeping, "the ABC-1 agent")
        r = self.status_json()["runner"]
        self.assertTrue(r["running"], r)
        self.assertEqual((r["ticket"], r["stage"], r["pid"], r["host"]), ("ABC-1", "plan", p.pid, False))
        self.assertEqual(self.status()["status"], "running")
        asked = time.time()
        rc, out = self.q("stop", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.assertIn("asked the run", out)
        self.wait_for(lambda: self.status()["status"] == "stopped", "ABC-1 to stop", timeout=10)
        self.assertLess(time.time() - asked, 6)
        self.assertEqual(p.wait(timeout=30), 0, cat(os.path.join(self.tmp, "run.out")))
        st = self.status()
        self.assertEqual((st["stage"], st["attempt"], st["history"][-1]["result"]), ("plan", 0, "stopped"))
        self.assertFalse(any(h["result"] == "failed" for h in st["history"]))
        self.assertFalse(os.path.exists(os.path.join(self.state, "ABC-1", "stop")))
        self.assertEqual(self.status("ABC-2")["status"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.state, "run.json")))
        self.assertEqual(self.status_json()["runner"], {"running": False})
        # Its container (a container launch) is stopped too, found by the clone it mounts.
        self.assertIn("stop-agents " + os.path.join(self.state, "ABC-1", "repo"), cat(self.calls).splitlines())
        self.assertIn("stopped 1 agent container(s)", cat(os.path.join(self.state, "ABC-1", "log.md")))
        # Only that launch's: the label harness gave its container.
        self.assertIn("stop-launch " + self.status()["launch_id"], cat(self.calls).splitlines())

    def test_halt_puts_the_ticket_back_and_ends_the_run(self):
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1")
        self.add("ABC-1", "ABC-2")
        p = self.run_bg()
        self.wait_for(self.sleeping, "the ABC-1 agent")
        rc, out = self.q("halt")
        self.assertEqual(rc, 0, out)
        self.assertEqual(p.wait(timeout=15), 0)
        self.assertIn("halted by the owner", cat(os.path.join(self.tmp, "run.out")))
        st = self.status()
        self.assertEqual((st["status"], st["stage"], st["attempt"]), ("queued", "plan", 0))
        self.assertEqual(self.status("ABC-2")["status"], "queued")
        self.assertFalse(os.path.exists(os.path.join(self.state, "halt")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "run.json")))
        # No run: say so, and an old halt request does not stop the next run.
        put(os.path.join(self.state, "halt"), "x")
        rc, out = self.q("halt")
        self.assertIn("no queue run is going; removed an old halt request", out)
        put(os.path.join(self.state, "halt"), "x")
        del os.environ["FAKE_SLEEP"]
        rc, out = self.run_queue()
        self.assertEqual((rc, self.status()["status"], self.status("ABC-2")["status"]), (0, "done", "done"), out)

    def test_a_ticket_left_running_is_not_the_runs_to_stop(self):
        # ABC-1 was left running by an earlier run; this run is on ABC-2 and has not
        # got to ABC-1, so a stop request would wait for it forever.
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-2")
        self.add("ABC-1", "ABC-2", "ABC-3")
        self.q("move", "ABC-2", "top")
        q = tq.Queue(self.state)
        st = q.load("ABC-1")
        st["status"] = "running"
        q.save(st)
        p = self.run_bg("--push", "0")
        self.wait_for(lambda: self.sleeping("ABC-2"), "the ABC-2 agent")
        rc, out = self.q("stop", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("asked the run", out)
        self.assertEqual(self.status()["status"], "stopped")
        self.assertFalse(os.path.exists(q.sentinel("ABC-1")))
        # Retried from the plan, and removed: neither waits on the run either.
        st = q.load("ABC-1")
        st["status"] = "running"
        q.save(st)
        rc, out = self.q("retry", "ABC-1", "--stage", "plan")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.status()["status"], self.status()["stage"]), ("queued", "plan"))
        st = q.load("ABC-3")
        st["status"] = "running"
        q.save(st)
        rc, out = self.q("remove", "ABC-3")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("ABC-3", q.order())
        # The ticket the run has in hand still goes through the run.
        rc, out = self.q("retry", "ABC-2")
        self.assertEqual(rc, 1, out)
        rc, out = self.q("remove", "ABC-2")
        self.assertEqual(rc, 1, out)
        self.assertIn("stop it first", out)
        p.kill()
        p.wait()

    def test_halt_force_ends_a_run_that_does_not_halt(self):
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1")
        self.add("ABC-1", "ABC-2")
        p = self.run_bg()
        self.wait_for(self.sleeping, "the ABC-1 agent")
        self.wait_for(lambda: self.status_json()["runner"].get("agent"), "the agent's pid in run.json")
        agent, launch = self.status_json()["runner"]["agent"], self.status_json()["runner"]["launch"]
        rc, out = self.q("halt", "--force", "--harness", self.harness)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"ended the run (process {p.pid})", out)
        self.assertNotEqual(p.wait(timeout=15), 0)
        with self.assertRaises(ProcessLookupError):
            os.killpg(agent, 0)
        st = self.status()
        self.assertEqual((st["status"], st["stage"], st["history"][-1]["result"]), ("queued", "plan", "interrupted"))
        self.assertIn("halt --force", st["reason"])
        self.assertEqual(self.status("ABC-2")["status"], "queued")
        for f in ("halt", "run.json", os.path.join("ABC-1", "stop")):
            self.assertFalse(os.path.exists(os.path.join(self.state, f)), f)
        self.assertIn("stop-launch " + launch, cat(self.calls).splitlines())
        rc, out = self.q("retry", "ABC-1", "--stage", "plan")
        self.assertEqual((rc, self.status()["status"]), (0, "queued"), out)
        # No run going: halt --force only tidies up.
        rc, out = self.q("halt", "--force")
        self.assertEqual(rc, 0, out)
        self.assertIn("no queue run is going", out)

    def test_move(self):
        self.add("ABC-1", "ABC-2", "ABC-3")
        order = lambda: tq.Queue(self.state).order()  # noqa: E731
        for args, want in ((("ABC-3", "top"), ["ABC-3", "ABC-1", "ABC-2"]),
                           (("ABC-3", "down"), ["ABC-1", "ABC-3", "ABC-2"]),
                           (("ABC-1", "up"), ["ABC-1", "ABC-3", "ABC-2"]),
                           (("abc-1", "bottom"), ["ABC-3", "ABC-2", "ABC-1"]),
                           (("ABC-2", "up"), ["ABC-2", "ABC-3", "ABC-1"])):
            rc, out = self.q("move", *args)
            self.assertEqual((rc, order()), (0, want), (args, out))
        self.assertEqual(self.q("move", "ABC-9", "up")[0], 1)

    def test_status_json(self):
        self.add("ABC-1", "ABC-2")
        self.q("move", "ABC-2", "top")
        got = self.status_json()
        self.assertEqual(got["runner"], {"running": False})
        self.assertEqual([t["key"] for t in got["tickets"]], ["ABC-2", "ABC-1"])
        self.assertEqual(got["tickets"][0]["stages"], list(tq.ALL_STAGES))
        put(os.path.join(self.state, "run.json"), json.dumps({"pid": 1, "beat": int(time.time()) - 60}))
        self.assertEqual(self.status_json()["runner"], {"pid": 1, "beat": int(time.time()) - 60, "running": False})
        rc, out = self.q("status")
        self.assertIn("run: not going", out)

    def test_retry_from_a_stage(self):
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--stages", "plan,implement")
        self.run_queue()
        self.assertEqual(self.status()["status"], "done")
        self.assertEqual(len(self.calls_of("reviewing")), 0)
        rc, out = self.q("retry", "ABC-1", "--stage", "review")
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertEqual((st["status"], st["stage"], st["stages"]), ("queued", "review", ["plan", "implement", "review"]))
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual([len(self.calls_of(s)) for s in ("planning", "implementing", "reviewing")], [1, 1, 1])
        # Done, without --stage: from the start, the plan on a fresh clone.
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual((rc, self.status()["stage"]), (0, "plan"), out)
        self.assertNotIn("base_sha", self.status())

    def test_an_old_state_file_runs_every_stage(self):
        os.makedirs(os.path.join(self.state, "ABC-1"))
        put(os.path.join(self.state, "ABC-1", "state.json"), json.dumps(
            {"key": "ABC-1", "project": self.project, "base": "main", "status": "queued", "stage": "plan",
             "attempt": 0, "feedback": "", "reason": "", "created": "2026-01-01T00:00:00Z"}))
        put(os.path.join(self.state, "queue.list"), "ABC-1\n")
        self.assertEqual(self.status_json()["tickets"][0]["notes"], [])
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((rc, st["status"]), (0, "done"), out)
        self.assertNotIn("stages", st)
        self.assertEqual([h["stage"] for h in st["history"]], ["plan", "implement", "review", "push"])
        self.assertIn("ABC-1", sh("git", "-C", self.remote, "branch", "--list", "ABC-1"))


class AuditFixesTest(Base):
    """The task file, the Windows command-line limit, UTF-8 git output, clone and
    push options, Windows agent paths and the verifier's retries."""

    def test_each_stage_has_its_prompt_in_the_task_file_while_it_runs(self):
        os.environ["FAKE_MUSTFIX"] = "1"
        self.add("ABC-1")
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(cat(self.calls + ".task").split(), ["ABC-1", "True"] * 4)   # plan, implement, review, fix
        self.assertFalse(os.path.exists(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-1-task.md")))
        # The agent was told where it is (the full prompt) and a copy in the branch is refused.
        self.assertIn("ABC-1-task.md", tq.check_change({"key": "ABC-1"}, ("x", "+a", ["notes/ABC-1-task.md"], 1), "")[0][0])

    def test_the_task_file_is_gone_after_a_stopped_stage(self):
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1")
        self.add("ABC-1")
        task = os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-1-task.md")
        q = tq.Queue(self.state)
        def stop_when_running():
            end = time.time() + 30
            while not os.path.exists(self.calls + ".sleeping") and time.time() < end:
                time.sleep(0.1)
            self.assertTrue(os.path.exists(task))   # there while the agent runs
            put(q.sentinel("ABC-1"), "x")
        threading.Thread(target=stop_when_running, daemon=True).start()
        rc, out = self.run_queue()
        self.assertEqual((rc, self.status()["status"]), (0, "stopped"), out)
        self.assertFalse(os.path.exists(task))

    def test_a_long_prompt_on_windows_goes_through_the_task_file(self):
        keep = tq.WINDOWS, tq.WIN_PROMPT_MAX, tq.PROMPT_MAX_BYTES
        tq.WINDOWS, tq.WIN_PROMPT_MAX, tq.PROMPT_MAX_BYTES = True, 100, 100   # 100: Linux would refuse it
        try:
            self.add("ABC-1")
            rc, out = self.run_queue()
        finally:
            tq.WINDOWS, tq.WIN_PROMPT_MAX, tq.PROMPT_MAX_BYTES = keep
        self.assertEqual(self.status()["status"], "done", out)
        short = cat(self.calls + ".pointer").split("\n=====\n")[:-1]
        self.assertEqual(len(short), 3)   # plan, implement, review
        task = os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-1-task.md")
        self.assertIn("ticket ABC-1, stage plan, in a clone of the project on branch ABC-1", short[0])
        self.assertIn(f"Your full instructions for the plan stage of ticket ABC-1 are in {task}. Read it first", short[0])
        self.assertLess(max(len(x) for x in short), 1000)
        self.assertIn("You are planning ticket ABC-1", cat(self.calls + ".planning"))

    def test_git_output_is_utf8_whatever_the_locale(self):
        sh("git", "-C", self.project, "commit", "-q", "--allow-empty", "-m", "Łódź Ёлка")
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
                   PYTHONPATH=os.path.dirname(os.path.abspath(tq.__file__)))
        code = ("import locale, sys, ticket_queue as tq; assert 'utf' not in locale.getpreferredencoding(False).lower();"
                "sys.stdout.buffer.write(tq.git(sys.argv[1], 'log', '-1', '--format=%s').stdout.encode('utf-8'))")
        r = subprocess.run([sys.executable, "-c", code, self.project], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
        self.assertEqual(r.stdout.decode("utf-8").strip(), "Łódź Ёлка")

    def test_windows_clone_options(self):
        with mock.patch.object(tq.os, "name", "posix"):
            self.assertEqual(tq.clone_opts(True), [])
        with mock.patch.object(tq.os, "name", "nt"):
            self.assertEqual(tq.clone_opts(False), ["-c", "core.fsmonitor=false", "-c", "core.longpaths=true"])
            self.assertEqual(tq.clone_opts(True)[-2:], ["-c", "core.autocrlf=false"])
        # The options stay in the clone's config (git clone -c writes them there).
        self.add("ABC-1")
        q = tq.Queue(self.state)
        st = q.load("ABC-1")
        with mock.patch.object(tq, "clone_opts", lambda c: ["-c", "core.autocrlf=false"] if c else []):
            tq.make_clone(q, st, container=True)
        repo = os.path.join(self.state, "ABC-1", "repo")
        self.assertEqual(sh("git", "-C", repo, "config", "--local", "core.autocrlf").strip(), "false")

    def test_a_push_never_waits_for_a_credential_prompt(self):
        seen = []
        real = tq.git

        def spy(cwd, *args, **kw):
            seen.append((args, kw.get("env")))
            return real(cwd, *args, **kw)
        for k in ("GIT_SSH_COMMAND", "GIT_SSH"):
            os.environ.pop(k, None)
        self.add("ABC-1")
        with mock.patch.object(tq, "git", spy):
            rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        push = [(a, e) for a, e in seen if "push" in a]
        self.assertEqual(len(push), 1)
        args, env = push[0]
        self.assertEqual(args[:len(tq.NET_OPTS) + 1], (*tq.NET_OPTS, "push"))
        self.assertIn("credential.interactive=false", args)
        self.assertEqual(env, {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_SSH_COMMAND": tq.SSH_BATCH})
        self.assertIn("BatchMode=yes", tq.SSH_BATCH)
        # The owner's own ssh is kept: GIT_SSH_COMMAND, GIT_SSH (plink) or core.sshCommand.
        quiet = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        for k, v in (("GIT_SSH_COMMAND", "ssh -i mykey"), ("GIT_SSH", "plink.exe")):
            os.environ[k] = v
            self.assertEqual(tq.net_env(self.project), quiet, k)
            del os.environ[k]
        sh("git", "-C", self.project, "config", "core.sshCommand", "ssh -i other")
        self.assertEqual(tq.net_env(self.project), quiet)

    def test_agent_paths_on_windows(self):
        ctx = type("C", (), {"host": True, "single": False})()
        with mock.patch.object(tq.os, "name", "nt"):
            self.assertEqual(tq.agent_path(ctx, "C:\\Users\\me\\vault\\x.md"), "C:/Users/me/vault/x.md")
            ctx.host = False
            with mock.patch.object(tq.subprocess, "run", side_effect=OSError):
                self.assertEqual(tq.agent_path(ctx, "D:\\v\\y.md"), "/d/v/y.md")
        with mock.patch.object(tq.os, "name", "posix"):
            self.assertEqual(tq.agent_path(ctx, "/a\\b"), "/a\\b")

    def test_the_verifier_is_asked_again_when_the_upstream_fails(self):
        calls = []

        def flaky(proxy, model, messages, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise tq.InfraError("HTTP 429: slow down")
            return '{"criteria": [], "verdict": "PASS"}'
        q = tq.Queue(self.state)
        os.makedirs(q.dir("ABC-1"), exist_ok=True)
        steps = []
        ctx = type("C", (), {"q": q, "key": "ABC-1", "proxy": "p", "verify_model": "m"})()
        with mock.patch.object(tq, "INFRA_RETRY_PAUSES", (0, 0)), mock.patch.object(tq, "call_model", flaky), \
                mock.patch.object(tq, "step", lambda c, t: steps.append(t)):
            self.assertEqual(tq.ask_verifier(ctx, "x", "ABC-1", "plan")["verdict"], "PASS")
            self.assertEqual(len(calls), 3)
            self.assertIn("trying again", cat(os.path.join(q.dir("ABC-1"), "log.md")))
            self.assertIn("asking the verifier (m) about the plan", cat(os.path.join(q.dir("ABC-1"), "log.md")))
            self.assertEqual(steps, ["asking the verifier", "the verifier request failed; trying again in 0s",
                                     "asking the verifier (try 2)", "the verifier request failed; trying again in 0s",
                                     "asking the verifier (try 3)"])
            # Down for good: three tries, then the probe's three, then the outage.
            calls.clear()
            with mock.patch.object(tq, "call_model", side_effect=tq.InfraError("HTTP 502: down")) as cm:
                with self.assertRaises(tq.Infra):
                    tq.ask_verifier(ctx, "x", "ABC-1", "plan")
                self.assertEqual(cm.call_count, 6)
            # A 4xx about the request: not asked again.
            with mock.patch.object(tq, "call_model", side_effect=tq.InfraError("HTTP 400: too long")) as cm:
                with self.assertRaises(tq.InfraError):
                    tq.model_call(ctx, [], 1)
                self.assertEqual(cm.call_count, 1)
        self.assertEqual([tq.transient(tq.InfraError(m)) for m in
                          ("HTTP 429: x", "HTTP 503: x", "timed out", "HTTP 400: x", "HTTP 408: x")],
                         [True, True, True, False, True])


class ReviewerFixesTest(Base):
    """remove of a running ticket, agent containers, a stop at push, retry of a done
    ticket, the vault's .gitignore, halt at start-up, the heartbeat's end, notes
    and a checked-out branch."""

    def bg_run(self, *extra):
        out = open(os.path.join(self.tmp, "run.out"), "w")
        self.addCleanup(out.close)
        p = subprocess.Popen([sys.executable, tq.__file__, "--state", self.state, "run", "--vault", self.vault,
                              "--proxy", self.proxy, "--harness", self.harness, "--default-model", "free-withtools",
                              *extra], stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        end = time.time() + 30
        while not os.path.exists(self.calls + ".sleeping"):
            self.assertLess(time.time(), end, cat(os.path.join(self.tmp, "run.out")))
            time.sleep(0.1)
        return p

    def test_a_running_ticket_is_not_removed(self):
        os.environ.update(FAKE_SLEEP="60")
        self.add("ABC-1")
        p = self.bg_run()
        rc, out = self.q("remove", "ABC-1")
        self.assertEqual(rc, 1)
        self.assertIn("stop it first", out)
        self.assertEqual(self.status()["status"], "running")
        self.assertEqual(tq.Queue(self.state).order(), ["ABC-1"])
        self.q("stop", "ABC-1")
        self.assertEqual(p.wait(timeout=30), 0)
        self.assertEqual(self.q("remove", "ABC-1")[0], 0)
        self.assertIsNone(self.status())

    def test_a_running_ticket_left_by_a_dead_run_can_be_removed(self):
        self.add("ABC-1")
        q = tq.Queue(self.state)
        st = q.load("ABC-1")
        st["status"] = "running"
        q.save(st)
        self.assertEqual(self.q("remove", "ABC-1")[0], 0)
        self.assertIsNone(self.status())

    def test_containers_unknown_or_host(self):
        self.add("ABC-1")
        q = tq.Queue(self.state)
        st = q.load("ABC-1")
        ctx = type("C", (), {"q": q, "harness": self.harness, "host": False, "single": False})()
        os.environ["FAKE_STOP_RC"] = "1"   # no docker, or it failed: say the container may be running
        tq.stop_containers(ctx, st)
        self.assertIn("container may still be running; check 'harness list'", cat(os.path.join(q.dir("ABC-1"), "log.md")))
        os.environ.update(FAKE_STOP_RC="0", FAKE_STOPPED="0")
        tq.stop_containers(ctx, st)
        self.assertNotIn("stopped 0", cat(os.path.join(q.dir("ABC-1"), "log.md")))
        n = cat(self.calls).count("stop-agents")
        ctx.host = True    # a host launch has no container
        tq.stop_containers(ctx, st)
        self.assertEqual(cat(self.calls).count("stop-agents"), n)

    def test_a_stop_at_the_push_pushes_nothing(self):
        real = tq.fetch_branch

        def fetch(q, st):
            if st["stage"] == "push":   # the owner stops it as the push stage starts
                put(q.sentinel(st["key"]), "x")
            return real(q, st)
        self.add("ABC-1")
        with mock.patch.object(tq, "fetch_branch", fetch):
            rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("stopped", "push"), out)
        self.assertEqual(sh("git", "-C", self.remote, "branch", "--list", "ABC-1"), "")
        self.assertFalse(os.path.exists(os.path.join(self.state, "ABC-1", "stop")))

    def test_a_stop_too_late_does_not_outlive_the_run(self):
        real = tq.record

        def rec(st, result, details=()):
            if st.get("stage") == "push" and result == "passed":
                put(tq.Queue(self.state).sentinel(st["key"]), "x")
            return real(st, result, details)
        self.add("ABC-1")
        with mock.patch.object(tq, "record", rec):
            self.run_queue()
        self.assertEqual(self.status()["status"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.state, "ABC-1", "stop")))

    def test_retry_of_a_done_ticket_starts_on_a_fresh_clone(self):
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--stages", "implement,push")
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "done")
        self.assertIn("base_sha", st)
        # The redone work would start at the base again, so ending it on ABC-1 would
        # drop the first go's pushed commits: refused up front, with what to do.
        for stage in ((), ("--stage", "plan")):
            rc, out = self.q("retry", "ABC-1", *stage)
            self.assertEqual(rc, 1, out)
            self.assertIn("branch ABC-1 has commits that main does not", out)
            self.assertIn("origin/ABC-1 at", out)
            self.assertIn("--stage implement (or a later stage) goes on from the work", out)
            self.assertEqual(self.status(), st)
        sh("git", "-C", self.remote, "branch", "-D", "ABC-1")
        sh("git", "-C", self.project, "branch", "-D", "ABC-1")
        sh("git", "-C", self.project, "fetch", "--prune", "origin")
        rc, out = self.q("retry", "ABC-1")
        st = self.status()
        self.assertEqual((rc, st["status"], st["stage"]), (0, "queued", "implement"), out)
        self.assertNotIn("base_sha", st)
        self.assertNotIn("sha", st)
        rc, out = self.run_queue("--push", "0")
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(cat(os.path.join(self.state, "ABC-1", "log.md")).count(" cloned "), 2)

    def test_retry_of_a_pushed_ticket_is_refused_only_where_it_starts_over(self):
        self.add("ABC-1")
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "done")
        q = tq.Queue(self.state)
        why = tq.retry_choices(q, st)
        self.assertEqual({s for s, w in why.items() if not w}, {"implement", "review", "push"}, why)
        self.assertIn("has commits that main does not", why[None])
        self.assertIn("has commits that main does not", why["plan"])
        self.assertIn("no Must fix items", why["fix"])
        self.assertEqual(tq.retry_choices(q, st, fetch=True), why)
        # a ticket an earlier retry sent back to the start (its work gone) blocked at
        # setup over the pushed branch: every stage would start over, all refused
        tq.requeue(q, "ABC-1", ("done",), stage="plan", fresh=True)
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("has commits that main does not", st["reason"])
        why = tq.retry_choices(q, st)
        self.assertTrue(all(why.values()), why)
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual(rc, 1, out)
        self.assertIn("delete it (here and on the remote, then git fetch --prune), or remove ABC-1 and add it "
                      "again with --start ABC-1", out)
        self.assertEqual(self.status(), st)

    def test_task_files_are_kept_out_of_the_vaults_git(self):
        tickets = os.path.join(self.vault, "wiki", "notes", "tickets")
        os.makedirs(tickets)
        put(os.path.join(self.vault, ".gitignore"), ".trash/")    # no newline at the end
        self.add("ABC-1")
        put(os.path.join(tickets, "ABC-1-task.md"), "left by a killed run")
        put(os.path.join(tickets, "OTHER-9-task.md"), "not a queue ticket's")
        q = tq.Queue(self.state)
        tq.keep_tasks_out(q, self.vault)
        tq.keep_tasks_out(q, self.vault)
        self.assertEqual(cat(os.path.join(self.vault, ".gitignore")),
                         ".trash/\n# harness queue: a stage's prompt while it runs\nwiki/notes/tickets/*-task.md\n")
        self.assertFalse(os.path.exists(os.path.join(tickets, "ABC-1-task.md")))
        self.assertTrue(os.path.exists(os.path.join(tickets, "OTHER-9-task.md")))
        sh("git", "init", "-q", self.vault)
        put(os.path.join(tickets, "ABC-1-task.md"), "x")
        self.assertEqual(sh("git", "-C", self.vault, "status", "--porcelain", "--", "wiki"), "")

    def test_a_halt_asked_as_the_run_starts_is_kept(self):
        self.add("ABC-1")
        halt = tq.Queue(self.state).sentinel()
        put(halt, "x")
        os.utime(halt, (time.time() + 5,) * 2)   # written after the run took its lock
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.status()["status"], self.status()["stage"]), ("queued", "plan"))
        self.assertFalse(os.path.exists(halt))
        self.assertFalse(os.path.exists(self.calls))   # nothing launched

    def test_no_beat_after_the_run_ended(self):
        q = tq.Queue(self.state)
        ctx = type("C", (), {"q": q, "beat_lock": threading.Lock(), "beating": threading.Event(),
                             "run_info": {}})()
        tq.beat(ctx)
        self.assertTrue(os.path.exists(os.path.join(self.state, "run.json")))
        os.remove(os.path.join(self.state, "run.json"))
        ctx.beating.set()
        tq.beat(ctx)
        self.assertFalse(os.path.exists(os.path.join(self.state, "run.json")))

    def test_a_note_does_not_write_over_a_running_ticket(self):
        self.add("ABC-1")
        q = tq.Queue(self.state)
        st = q.load("ABC-1")
        st["status"] = "running"
        q.save(st)
        path = os.path.join(self.state, "ABC-1", "state.json")
        before = cat(path)
        self.assertEqual(self.q("note", "ABC-1", "--text", "hi")[0], 0)
        self.assertEqual(cat(path), before)                 # the run's file, untouched
        self.assertEqual([n["text"] for n in q.load("ABC-1")["notes"]], ["hi"])   # and the note there on load

    def test_a_checked_out_branch_is_not_moved(self):
        wt = os.path.join(self.tmp, "wt")
        sh("git", "-C", self.project, "worktree", "add", "-q", "-b", "ABC-1", wt, "main")
        at = sh("git", "-C", self.project, "rev-parse", "ABC-1").strip()
        self.add("ABC-1")
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "done", out)
        self.assertEqual(sh("git", "-C", self.project, "rev-parse", "ABC-1").strip(), at)
        self.assertEqual(sh("git", "-C", wt, "status", "--porcelain"), "")
        self.assertIn("branch ABC-1 is checked out in the project; not moved", cat(os.path.join(self.state, "ABC-1", "log.md")))
        self.assertEqual(sh("git", "-C", self.remote, "rev-parse", "ABC-1").strip(), st["sha"])


class StartAndBranchTest(Base):
    """Where the work starts (--start) and the branch it ends on (--branch)."""

    def remote_parent(self, branch):
        return sh("git", "-C", self.remote, "rev-parse", branch + "^").strip()

    def test_from_a_remote_branch_onto_a_new_branch(self):
        feat, _ = self.feature_branch()
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--start", "origin/feat",
                         "--branch", "abc-new", "--stages", "implement,push")
        self.assertEqual(rc, 0, out)
        self.assertIn("from origin/feat on branch abc-new", out)
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(self.remote_parent("abc-new"), feat)                  # built on the fetched start
        self.assertEqual(sh("git", "-C", self.remote, "rev-parse", "feat").strip(), feat)   # left alone
        self.assertEqual(sh("git", "-C", self.remote, "branch", "--list", "ABC-1"), "")

    def test_onto_an_existing_branch_only_forward(self):
        feat, fork = self.feature_branch()
        self.git_p("branch", "behind", fork)        # the start contains it: moved forward
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--start", "origin/feat",
                         "--branch", "behind", "--stages", "implement,push")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(self.remote_parent("behind"), feat)
        # A branch with commits the start does not have is not ended on: they would be lost.
        self.git_p("branch", "ahead", "main")
        rc, out = self.q("add", "ABC-2", "--project", self.project, "--start", "origin/feat",
                         "--branch", "ahead")
        self.assertEqual(rc, 1, out)
        self.assertIn("has commits that origin/feat does not", out)
        self.assertIsNone(self.status("ABC-2"))

    def test_carry_on_an_existing_branch(self):
        feat, _ = self.feature_branch()
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--start", "origin/feat",
                         "--branch", "feat", "--stages", "implement,push")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(self.remote_parent("feat"), feat)                    # a fast-forward of feat
        # done and pushed, but its start is the branch: going again from the start loses nothing
        first = sh("git", "-C", self.remote, "rev-parse", "feat").strip()
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(self.remote_parent("feat"), first)

    def test_start_here_in_a_clone_is_the_commit_checked_out(self):
        self.git_p("checkout", "-q", "-b", "wip")
        put(os.path.join(self.project, "wip.txt"), "wip\n")
        self.git_p("add", "wip.txt")
        self.git_p("commit", "-q", "-m", "wip")
        wip = self.git_p("rev-parse", "HEAD")
        put(os.path.join(self.project, "AGENTS.md"), "rules\n")   # not committed: not in a clone
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--start", "here", "--stages", "implement")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["start"], wip)
        self.assertIn("1 uncommitted file(s) are not in it", out)

    def test_in_place_keeps_the_owners_files_out(self):
        put(os.path.join(self.project, "AGENTS.md"), "rules\n")            # untracked
        os.makedirs(os.path.join(self.project, "build"))
        put(os.path.join(self.project, "build", "make.sh"), "echo hi\n")  # untracked, in a folder
        put(os.path.join(self.project, "README"), "project, edited\n")    # changed, not committed
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--in-place", "--stages", "implement,push")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "done", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "ABC-1", "repo")))   # no clone
        # done: the folder is back on the branch it was on, the owner's changes with it
        self.assertEqual(self.git_p("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual((st["orig_branch"], st["folder_branch_now"]), ("main", "main"))
        self.assertEqual(sh("git", "-C", self.remote, "ls-tree", "--name-only", "-r", "ABC-1").split(),
                         ["README", "feature.txt"])
        self.assertEqual(sh("git", "-C", self.remote, "show", "ABC-1:README"), "project\n")
        # The owner's files are as they were, and still not committed.
        self.assertEqual(cat(os.path.join(self.project, "README")), "project, edited\n")
        self.assertEqual(cat(os.path.join(self.project, "AGENTS.md")), "rules\n")
        self.assertIn("?? AGENTS.md", self.git_p("status", "--porcelain"))
        self.assertNotIn(tq.EXCLUDE_MARK, cat(os.path.join(self.project, ".git", "info", "exclude")))
        # What the folder held before the stage is in the snapshot.
        snap = "refs/harness-queue/snap/ABC-1"
        self.assertEqual(self.git_p("show", snap + ":AGENTS.md"), "rules")
        self.assertEqual(self.git_p("show", snap + ":build/make.sh"), "echo hi")
        self.assertTrue(st["launches"])
        # In place, the card says whose folder it is and how to commit there.
        card = json.loads(cat(self.calls + ".card").splitlines()[-1])
        self.assertEqual(card["where"], tq.CARD_WHERE_IN_PLACE)

    def test_the_queues_git_never_signs(self):
        # A signing key on a smart card would ask for its PIN on the snapshot of
        # every stage; gpg.program=false stands in for the card.
        self.git_p("config", "commit.gpgsign", "true")
        self.git_p("config", "gpg.program", "false")
        tree = tq.git(self.project, "write-tree").stdout.strip()
        sha = tq.git(self.project, "commit-tree", tree, "-m", "snap").stdout.strip()
        self.assertNotIn("gpgsig", self.git_p("cat-file", "commit", sha))
        tq.git(self.project, "commit", "-q", "--allow-empty", "-m", "queue")

    def test_in_place_commit_of_an_owners_file_fails(self):
        put(os.path.join(self.project, "README"), "project, edited\n")
        os.environ["FAKE_FILE"] = "README"       # the agent commits the owner's edit with its own
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--in-place", "--stages", "implement")
        self.assertEqual(rc, 0, out)
        self.run_queue("--retries", "0")
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("README", st["reason"])

    def test_in_place_needs_the_branch_checked_out(self):
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--in-place", "--stages", "implement,review")
        self.assertEqual(rc, 0, out)
        os.environ["FAKE_MUSTFIX"] = "1"
        self.run_queue("--retries", "0")
        self.assertEqual(self.git_p("rev-parse", "--abbrev-ref", "HEAD"), "main")   # parked where it was
        self.git_p("checkout", "-q", "-b", "other")   # a branch of the owner's own choosing
        rc, out = self.q("retry", "ABC-1", "--stage", "review")
        self.assertEqual(rc, 0, out)
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("is on other, not the ticket's branch ABC-1", st["reason"])
        self.assertEqual(self.git_p("rev-parse", "--abbrev-ref", "HEAD"), "other")

    def test_review_plan_holds_this_tickets_plan(self):
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--review-plan")
        self.assertEqual(rc, 0, out)
        self.run_queue()
        self.assertEqual(self.status()["status"], "plan-review")
        self.assertEqual(len(self.calls_of("implementing")), 0)


    def test_defaults_are_in_place_and_wait_for_the_plan(self):
        head = self.git_p("rev-parse", "HEAD")
        rc, out = self.q("add", "ABC-1", "--project", self.project, defaults=True)
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertIs(st["in_place"], True)
        self.assertIs(st["plan_review"], True)
        self.assertEqual(st["start"], head)                  # here, fixed when added
        self.assertIn("the plan waits for approve", out)
        rc, out = self.q("add", "ABC-2", "--project", self.project, "--clone", "--no-plan-approval", defaults=True)
        self.assertEqual(rc, 0, out)
        st = self.status("ABC-2")
        self.assertNotIn("in_place", st)
        self.assertNotIn("plan_review", st)
        rc, out = self.q("add", "ABC-3", "--project", self.project, "--clone", "--in-place", defaults=True)
        self.assertEqual(rc, 1, out)

    def two_in_place(self):
        put(os.path.join(self.project, "AGENTS.md"), "rules\n")   # the owner's, untracked
        for key in ("ABC-1", "ABC-2"):
            rc, out = self.q("add", key, "--project", self.project, "--stages", "plan,implement", defaults=True)
            self.assertEqual(rc, 0, out)
        self.run_queue()
        self.assertEqual([self.status(k)["status"] for k in ("ABC-1", "ABC-2")], ["plan-review"] * 2)
        # each plan waiting for the owner put the folder back on main, where it was
        self.assertEqual(self.git_p("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.q("approve", "ABC-1")[0], 0)

    def test_in_place_tickets_take_turns_in_the_folder(self):
        self.two_in_place()
        rc, out = self.run_queue()
        self.assertEqual(self.status("ABC-1")["status"], "done", out)
        self.assertEqual(self.git_p("rev-parse", "--abbrev-ref", "HEAD"), "main")
        log = cat(os.path.join(self.state, "ABC-1", "log.md"))
        self.assertIn("switched " + self.project + " from main back to ABC-1", log)
        self.assertIn("switched " + self.project + " back to main, the branch it was on before ABC-1", log)
        self.assertEqual(self.q("approve", "ABC-2")[0], 0)
        rc, out = self.run_queue()
        self.assertEqual(self.status("ABC-2")["status"], "done", out)
        self.assertEqual(self.git_p("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(cat(os.path.join(self.project, "AGENTS.md")), "rules\n")
        for key in ("ABC-1", "ABC-2"):   # each built on main, not on the other
            self.assertEqual(self.git_p("rev-parse", key + "~1"), self.git_p("rev-parse", "main"))

    def test_another_tickets_leftovers_are_not_carried_over(self):
        self.two_in_place()
        self.git_p("checkout", "-q", "ABC-2")
        put(os.path.join(self.project, "half.txt"), "ABC-2's\n")   # not there when ABC-1 started
        self.run_queue()
        st = self.status("ABC-1")
        self.assertEqual(st["status"], "blocked")
        self.assertIn("uncommitted changes that were not there when ABC-1 started (half.txt)", st["reason"])
        self.assertEqual(self.git_p("rev-parse", "--abbrev-ref", "HEAD"), "ABC-2")

    def test_a_new_ticket_starts_where_the_folders_ticket_did(self):
        self.two_in_place()
        self.git_p("checkout", "-q", "ABC-2")   # the folder on ABC-2's branch
        self.git_p("commit", "-q", "--allow-empty", "-m", "on ABC-2")
        rc, out = self.q("add", "ABC-3", "--project", self.project, defaults=True)
        self.assertEqual(rc, 0, out)
        st = self.status("ABC-3")
        self.assertEqual(st["start"], self.status("ABC-2")["start_sha"])
        self.assertEqual(st["base"], "main")
        self.assertIn("is on ABC-2's branch ABC-2", out)


class ResultsTest(Base):
    def note(self, key="ABC-1"):
        return cat(os.path.join(self.vault, "wiki", "notes", "tickets", f"{key}-result.md"))

    def test_a_pushed_ticket_records_what_it_ended_with(self):
        self.add("ABC-1")
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((rc, st["status"], st["reason"]), (0, "done", ""), out)
        self.assertEqual(st["push"], {"remote": "origin", "url": self.remote, "branch": "ABC-1", "sha": st["sha"],
                                      "result": "pushed", "error": ""})
        self.assertEqual(st["compare_url"], "")   # a folder remote has no web page
        self.assertEqual([c["subject"] for c in st["commits"]], ["ABC-1: implementing"])
        self.assertEqual(st["commits"][0]["sha"], st["sha"])
        self.assertEqual(st["diffstat"], {"files": 1, "insertions": 1, "deletions": 0})
        self.assertEqual((st["orig_branch"], st["folder_branch_now"], st["warnings"]), ("", "main", []))
        note = self.note()
        self.assertTrue(note.startswith("---\ntype: note\nticket: ABC-1\n"), note[:80])
        for part in ("## Goal\n\nA feature file.", "- A file feature.txt says hello feature", "pushed to origin",
                     "ABC-1: implementing", "1 file(s), +1 -0", "## Review notes"):
            self.assertIn(part, note)
        # A retry clears them and the stale note; the next go writes its own.
        rc, out = self.q("retry", "ABC-1", "--stage", "push")
        self.assertEqual(rc, 0, out)
        self.assertFalse({"push", "compare_url", "commits", "diffstat"} & set(self.status()), self.status())
        self.assertFalse(os.path.exists(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-1-result.md")))
        rc, out = self.run_queue()
        self.assertEqual(self.status()["push"]["result"], "pushed", out)

    def test_with_push_off_or_no_remote_the_branch_is_kept_local(self):
        self.add("ABC-1")
        rc, out = self.run_queue("--push", "0")
        st = self.status()
        self.assertEqual((rc, st["status"], st["push"]["result"]), (0, "done", "local"), out)
        self.assertTrue(st["reason"].startswith("kept local (QUEUE_PUSH=0): branch ABC-1 at "), st["reason"])
        self.assertEqual(sh("git", "-C", self.remote, "branch"), "")
        self.assertIn("kept local (not pushed)", self.note())
        self.git_p("remote", "remove", "origin")
        self.add("ABC-2")
        rc, out = self.run_queue()
        st = self.status("ABC-2")
        self.assertEqual((rc, st["status"], st["push"]["result"], st["push"]["url"]), (0, "done", "local", ""), out)
        self.assertIn("kept local: the project has no remote origin", st["reason"])

    def test_a_refused_push_blocks_with_the_error(self):
        self.add("ABC-1")
        self.git_p("remote", "set-url", "origin", os.path.join(self.tmp, "nowhere.git"))
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((st["status"], st["stage"], st["push"]["result"]), ("blocked", "push", "rejected"), out)
        self.assertTrue(st["push"]["error"], st)
        self.assertIn("push to origin refused", st["reason"])
        self.assertIn("harness queue retry ABC-1 --stage push", st["reason"])

    def test_a_branch_moved_since_it_was_checked_is_not_pushed(self):
        self.add("ABC-1")
        real = tq.STAGES["push"]

        def late_commit(ctx, st):   # something commits on the branch after the verifier passed it
            repo = os.path.join(self.state, "ABC-1", "repo")
            sh("git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "unchecked")
            return real(ctx, st)
        with mock.patch.dict(tq.STAGES, {"push": late_commit}):
            rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("blocked", "push"), out)
        self.assertIn("moved since it was checked", st["reason"])
        self.assertEqual(sh("git", "-C", self.remote, "branch"), "")
        # The owner looked and pushes it as it is.
        rc, out = self.q("retry", "ABC-1", "--stage", "push")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((st["status"], st["push"]["result"]), ("done", "pushed"), out)
        self.assertEqual([c["subject"] for c in st["commits"]], ["unchecked", "ABC-1: implementing"])

    def test_stages_without_a_push_record_what_they_left(self):
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--stages", "plan,implement")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((st["status"], st["push"]["result"], len(st["commits"])), ("done", "skipped", 1), out)
        self.assertIn("no push stage", self.note())

    def test_compare_urls(self):
        cases = {
            "https://github.com/o/r.git": "https://github.com/o/r/compare/main...ABC-1?expand=1",
            "git@github.com:o/r.git": "https://github.com/o/r/compare/main...ABC-1?expand=1",
            "ssh://git@github.com/o/r": "https://github.com/o/r/compare/main...ABC-1?expand=1",
            "https://gitlab.example.com/g/sub/r.git":
                "https://gitlab.example.com/g/sub/r/-/merge_requests/new?merge_request[source_branch]=ABC-1",
            "https://bitbucket.org/o/r.git": "",
            "/srv/git/r.git": "",
            "C:/repos/r.git": "",
            "file:///srv/r.git": "",
        }
        for url, want in cases.items():
            self.assertEqual(tq.compare_url(url, "main", "ABC-1"), want, url)
        self.assertEqual(tq.compare_url("https://github.com/o/r", "rel/1", "a b"),
                         "https://github.com/o/r/compare/rel/1...a%20b?expand=1")
        # A token in the remote's url is never recorded.
        self.git_p("remote", "set-url", "origin", "https://user:tok3n@github.com/o/r.git")
        self.assertEqual(tq.remote_url(self.project, "origin"), "https://github.com/o/r.git")
        self.assertEqual(tq.remote_url(self.project, "nope"), "")


class StallTest(Base):
    """A stage always moves on when its agent returns: outages are waited out, a launch
    that dies at once is not an attempt, an idle agent is stopped, a broken verifier blocks."""

    def setUp(self):
        super().setUp()
        for name, v in (("POLL", 0.1), ("OUTAGE_PAUSES", (0.2,))):
            p = mock.patch.object(tq, name, v)
            p.start()
            self.addCleanup(p.stop)

    def test_outage_is_waited_out(self):
        self.add("ABC-1")
        FakeProxy.down_left = 8   # the verifier, its retries and the probes all fail for a while
        rc, out = self.run_queue("--outage-wait", "60")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["status"], "done", out)
        self.assertIn("waiting: the upstream does not answer since", out)
        self.assertIn("the run waits and carries on", cat(os.path.join(self.state, "ABC-1", "log.md")))
        self.assertEqual(len(self.calls_of("planning")), 1)   # the plan was kept, only its check waited

    def test_outage_past_the_wait_ends_the_run(self):
        self.add("ABC-1")
        FakeProxy.mode = "down"
        rc, out = self.run_queue("--outage-wait", "1")
        self.assertEqual(rc, 3, out)
        self.assertIn("waited", out)
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("queued", "plan"), out)

    def test_halt_during_an_outage_wait(self):
        self.add("ABC-1")
        FakeProxy.mode = "down"
        q = tq.Queue(self.state)

        def halt():
            end = time.time() + 30
            while time.time() < end:
                try:
                    if json.load(open(os.path.join(self.state, "run.json"))).get("waiting"):
                        put(q.sentinel(), "")
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(0.05)
        t = threading.Thread(target=halt, daemon=True)
        t.start()
        with mock.patch.object(tq, "OUTAGE_PAUSES", (30,)):
            t0 = time.time()
            rc, out = self.run_queue("--outage-wait", "600")
        self.assertLess(time.time() - t0, 25, out)
        self.assertEqual(rc, 0, out)
        self.assertIn("halted by the owner", out)
        self.assertEqual(self.status()["status"], "queued")

    def test_launch_that_dies_at_once_is_not_an_attempt(self):
        os.environ["FAKE_EXIT"] = "1:1"
        self.add("ABC-1")
        rc, out = self.run_queue("--outage-wait", "60")
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertEqual(st["status"], "done", out)
        self.assertNotIn("launch_fails", st)
        self.assertTrue(any("having started nothing" in " ".join(h["details"]) for h in st["history"]),
                        st["history"])
        passed = [h for h in st["history"] if h["stage"] == "plan" and h["result"] == "passed"]
        self.assertEqual([h["attempt"] for h in passed], [1], st["history"])   # its first attempt, not its second

    def test_launch_failing_again_and_again_blocks(self):
        os.environ["FAKE_EXIT"] = "9:1"
        self.add("ABC-1")
        rc, out = self.run_queue("--outage-wait", "60")
        st = self.status()
        self.assertEqual(st["status"], "blocked", out)
        self.assertIn("3 times in a row", st["reason"])
        self.assertEqual(len(cat(self.calls + ".exits")), 3)

    def test_agent_done_but_not_exiting_is_stopped_and_checked(self):
        os.environ.update(FAKE_HANG_AFTER="60", FAKE_HANG_STAGE="implementing")
        self.add("ABC-1")
        t0 = time.time()
        rc, out = self.run_queue("--idle-min", "0.03")
        self.assertLess(time.time() - t0, 45, out)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["status"], "done", out)
        self.assertIn("made no progress", cat(os.path.join(self.state, "ABC-1", "log.md")))

    def test_idle_agent_with_the_upstream_down_goes_back(self):
        os.environ.update(FAKE_HANG_AFTER="60", FAKE_HANG_STAGE="planning")
        self.add("ABC-1")
        FakeProxy.mode = "down"
        rc, out = self.run_queue("--idle-min", "0.03")
        self.assertEqual(rc, 3, out)
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("queued", "plan"), out)
        self.assertIn("made no progress", st["reason"])

    def broken_verifier(self, mode):
        self.add("ABC-1")
        FakeProxy.mode = mode
        FakeProxy.hang_secs = 3
        with mock.patch.object(tq, "VERIFY_TIMEOUT", 1):
            rc, out = self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked", out)
        self.assertIn("verifier failed at plan", st["reason"])
        self.assertIn("retry ABC-1", st["reason"])   # what to do about it
        self.assertTrue(st.get("verify_only"), st)    # the agent's plan is kept
        FakeProxy.mode = "ok"
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(len(self.calls_of("planning")), 1, "the plan was checked again, not made again")
        return st

    def test_verifier_that_hangs_blocks_then_retry_rechecks(self):
        self.broken_verifier("hang")

    def test_verifier_that_gives_garbage_blocks_then_retry_rechecks(self):
        st = self.broken_verifier("garbage")
        self.assertIn("not the JSON asked for", st["reason"])

    def test_wait_call_gives_up_at_its_deadline(self):
        t0 = time.time()
        with self.assertRaisesRegex(tq.InfraError, "no answer in"):
            tq.wait_call(types.SimpleNamespace(), lambda: time.sleep(3), deadline=0.5)
        self.assertLess(time.time() - t0, 2)

    def test_run_killed_while_verifying_resumes_without_a_new_launch(self):
        self.add("ABC-1")
        FakeProxy.mode = "hang"
        FakeProxy.hang_secs = 30
        p = self.run_bg()
        try:
            self.wait_for(lambda: any("check a plan" in s[2] for s in FakeProxy.seen), "the plan's check", 60)
            time.sleep(0.3)
        finally:
            p.kill()
            p.wait()
        st = self.status()
        self.assertEqual(st["status"], "running")
        self.assertTrue(st.get("verify_only"), st)
        FakeProxy.mode = "ok"
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(len(self.calls_of("planning")), 1)

    run_bg = OwnerControlTest.run_bg
    wait_for = OwnerControlTest.wait_for

    def test_verifier_must_say_met_and_an_empty_diff_fails(self):
        diff = "+++ b/f.txt\n+hello feature\n"
        crit = ["says hello"]
        self.assertTrue(tq.judge({"verdict": "PASS", "criteria": [{"n": 1, "quote": "hello feature"}]},
                                 crit, diff, "quote", "diff"))
        self.assertEqual(tq.judge({"verdict": "PASS", "criteria": [{"n": 1, "met": True, "quote": "hello feature"}]},
                                  crit, diff, "quote", "diff"), [])


class AddGuardTest(Base):
    def test_branch_that_is_shared_needs_allow_branch(self):
        self.git_p("checkout", "-q", "-b", "dev")
        self.git_p("push", "-q", "origin", "dev", "main")
        sh("git", "-C", self.remote, "symbolic-ref", "HEAD", "refs/heads/dev")
        self.git_p("remote", "set-head", "origin", "dev")
        self.git_p("checkout", "-q", "main")
        for branch, base in (("main", "main"), ("master", "main"), ("dev", "main"), ("feat", "origin/feat"),
                             ("feat", "feat")):
            if base in ("origin/feat", "feat"):
                self.git_p("branch", "-q", "-f", "feat", "main")
                if base == "origin/feat":
                    self.git_p("push", "-q", "origin", "feat")
                    self.git_p("fetch", "-q", "origin")
            with self.subTest(branch=branch, base=base):
                rc, out = self.q("add", "ABC-1", "--project", self.project, "--branch", branch, "--base", base)
                self.assertEqual(rc, 1, out)
                self.assertIn("--allow-branch", out)
                self.assertFalse(os.path.exists(os.path.join(self.state, "ABC-1")))
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--branch", "main", "--allow-branch")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["branch"], "main")

    def test_one_branch_for_many_tickets_is_refused(self):
        rc, out = self.q("add", "ABC-1", "ABC-2", "--project", self.project, "--branch", "work")
        self.assertEqual(rc, 1, out)
        self.assertIn("same branch", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "ABC-1")))
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--branch", "work")
        self.assertEqual(rc, 0, out)

    def test_empty_ticket_or_missing_project_blocks(self):
        os.environ["FAKE_TICKET"] = ""
        self.add("ABC-1")
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked", out)
        self.assertIn("ticket's text is not at", st["reason"])
        self.assertEqual(self.calls_of("planning"), [])
        os.environ["FAKE_TICKET"] = TICKET
        self.add("ABC-2")
        shutil.rmtree(self.project)
        rc, out = self.run_queue()
        self.assertEqual(self.status("ABC-2")["status"], "blocked", out)

    def test_owner_command_on_a_held_lock_fails_cleanly(self):
        self.add("ABC-1")
        self.run_queue("--plan-review", "1")
        got = threading.Event()
        release = threading.Event()

        def hold():
            with tq.held(os.path.join(self.state, "queue.lock")):
                got.set()
                release.wait(30)
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        got.wait(10)
        try:
            rc, out = self.q("approve", "ABC-1")   # waits 10 s for the lock, then gives up
        finally:
            release.set()
            t.join()
        self.assertEqual(rc, 1, out)


class RetryMatrixTest(Base):
    """Every status x (retry, retry --stage S): refused leaving state.json as it was, or
    queued with its counters, reason and last go's results cleared, and a run then
    takes it to an end (done or plan-review), never stranded (a pushed ticket sent
    back to the start is refused up front, not blocked at setup). The page offers
    the same: retry_choices refuses exactly what retry does."""

    def make(self, status):
        if status == "plan-review":
            rc, out = self.q("add", "ABC-1", "--project", self.project, "--review-plan")
            self.assertEqual(rc, 0, out)
        else:
            self.add("ABC-1")
        if status == "queued":
            return
        if status == "stopped":
            self.q("stop", "ABC-1")
        elif status == "needs-answers":
            os.environ["FAKE_BLOCKING"] = "1"
            self.run_queue()
        elif status in ("blocked", "running-orphan"):
            os.environ["FAKE_NO_COMMIT"] = "1"
            self.run_queue()
            if status == "running-orphan":   # a run that died left it running, no run holds it
                st = self.status()
                st["status"] = "running"
                tq.Queue(self.state).save(st)
        else:
            self.run_queue()
        for k in ("FAKE_BLOCKING", "FAKE_NO_COMMIT"):
            os.environ.pop(k, None)

    def test_matrix(self):
        statuses = ("queued", "stopped", "needs-answers", "plan-review", "blocked", "running-orphan", "done")
        want = {"done": "done", "plan-review": "plan-review"}
        table, pauses = [], self.pauses
        self.addCleanup(setattr, self, "pauses", pauses)
        for status in statuses:
            for stage in (None,) + tq.ALL_STAGES:
                with self.subTest(status=status, stage=stage):
                    self.setUp()
                    try:
                        self.make(status)
                        before = self.status()
                        self.assertEqual(before["status"], status.replace("running-orphan", "running"))
                        offered = not tq.retry_choices(tq.Queue(self.state), before)[stage]
                        rc, out = self.q("retry", "ABC-1", *(("--stage", stage) if stage else ()))
                        self.assertEqual(offered, rc == 0, out)
                        st = self.status()
                        if rc:
                            self.assertEqual({k: v for k, v in st.items() if k != "updated"},
                                             {k: v for k, v in before.items() if k != "updated"}, out)
                            table.append((status, stage or "-", "refused: " + out.strip().splitlines()[-1][8:90]))
                            continue
                        self.assertEqual((st["status"], st["attempt"], st["reason"]), ("queued", 0, ""), out)
                        self.assertEqual(st["stage"], stage or st["stage"])
                        for k in tq.RESULTS:
                            self.assertNotIn(k, st)
                        rc, out = self.run_queue()
                        end = self.status()
                        self.assertIn(end["status"], want.values(), out + str(end.get("reason")))
                        table.append((status, stage or "-", f"queued at {st['stage']} -> {end['status']}"))
                    finally:
                        self.tearDown()
        print("\nretry matrix:")
        for row in table:
            print("  %-15s %-10s %s" % row)


@unittest.skipUnless(os.path.isdir("/proc"), "finds the agent through /proc")
class OrphanAgentTest(Base):
    def spawn(self, launch):
        """An agent whose run is gone: its own group, not this process's child (init reaps it)."""
        pid = int(subprocess.run(["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], start_new_session=True,
                                 env=dict(os.environ, HARNESS_QUEUE_LAUNCH=launch), capture_output=True,
                                 text=True).stdout)
        p = types.SimpleNamespace(pid=os.getpgid(pid), poll=lambda: None if self.alive(pid) else 0)
        self.addCleanup(lambda: self.alive(pid) and os.kill(pid, 9))
        return p

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1][0] != "Z"

    def orphan(self, pid, launch):
        os.environ["FAKE_NO_COMMIT"] = "1"
        self.add("ABC-1")
        self.run_queue()
        del os.environ["FAKE_NO_COMMIT"]
        st = self.status()
        st.update(status="running", agent={"pid": pid, "launch": launch, "host": socket.gethostname(),
                                           "container": False, "workdir": self.project})
        tq.Queue(self.state).save(st)

    def gone(self, p, secs=10):
        end = time.time() + secs
        while time.time() < end and p.poll() is None:
            time.sleep(0.1)
        return p.poll() is not None

    def test_retry_ends_the_agent_a_dead_run_left(self):
        mine, other = self.spawn("L-1"), self.spawn("L-2")
        self.orphan(mine.pid, "L-1")
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.gone(mine), out)
        self.assertIsNone(other.poll())
        self.assertNotIn("agent", self.status())

    def test_a_pid_that_is_not_that_launch_is_left_alone(self):
        other = self.spawn("L-2")
        self.orphan(other.pid, "L-1")
        rc, out = self.q("remove", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.gone(other, 1))

    def test_run_ends_the_orphan_before_taking_the_ticket(self):
        mine = self.spawn("L-1")
        self.orphan(mine.pid, "L-1")
        rc, out = self.run_queue()
        self.assertTrue(self.gone(mine), out)
        self.assertEqual(self.status()["status"], "done", out)


class NestedReposTest(Base):
    """In place: repositories inside the project (their own .git, not submodules)
    are not the ticket's; the queue never commits there, and a stage that changes
    one is blocked with its result kept."""

    run_bg, wait_for, sleeping = OwnerControlTest.run_bg, OwnerControlTest.wait_for, OwnerControlTest.sleeping

    def repo(self, rel, commit=True):
        d = os.path.join(self.project, rel)
        sh("git", "init", "-q", "-b", "main", d)
        if commit:
            put(os.path.join(d, "lib.txt"), "lib\n")
            sh("git", "-C", d, "add", "lib.txt")
            sh("git", "-C", d, "-c", "user.name=O", "-c", "user.email=o@example.com", "commit", "-q", "-m", "lib")
        return d

    def ignored_libA(self):
        put(os.path.join(self.project, ".gitignore"), "libA/\n")
        self.git_p("add", ".gitignore")
        self.git_p("commit", "-q", "-m", "ignore libA")
        return self.repo("libA")

    def add_in_place(self, stages="implement,push"):
        rc, out = self.q("add", "ABC-1", "--project", self.project, "--in-place", "--stages", stages)
        self.assertEqual(rc, 0, out)

    def test_the_snapshot_leaves_repositories_inside_out(self):
        # one with no commit failed the snapshot's git add -A; lib[1] is no glob
        self.repo("libA", commit=False)
        self.repo("vendor/x/libB", commit=False)
        self.repo("lib[1]", commit=False)
        put(os.path.join(self.project, "lx.txt"), "mine\n")
        put(os.path.join(self.project, "vendor", "y.txt"), "mine too\n")
        os.makedirs(os.path.join(self.project, "lib1"))
        put(os.path.join(self.project, "lib1", "z.txt"), "what the glob lib[1]/ would take\n")
        self.add_in_place()
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        tree = self.git_p("ls-tree", "-r", "--name-only", "refs/harness-queue/snap/ABC-1").split("\n")
        self.assertIn("lx.txt", tree)
        self.assertIn("vendor/y.txt", tree)
        self.assertIn("lib1/z.txt", tree)
        self.assertFalse([f for f in tree if f.startswith(("libA", "vendor/x", "lib["))], tree)
        self.assertNotIn("no snapshot", cat(os.path.join(self.state, "ABC-1", "log.md")))

    def test_a_commit_in_one_blocks_and_keeps_the_result(self):
        lib = self.ignored_libA()
        mine = sh("git", "-C", lib, "rev-parse", "HEAD").strip()
        os.environ["FAKE_NESTED"] = "commit:libA"
        self.add_in_place()
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked", out)
        self.assertIn("libA: 1 new commit(s), " + mine[:12], st["reason"])
        self.assertIn("on main", st["reason"])
        self.assertIn("--project " + self.project + "/<repository>", st["reason"])
        self.assertIn("harness queue retry ABC-1 then checks the agent's result", st["reason"])
        self.assertEqual(st["verify_only"]["stage"], "implement")
        self.assertNotIn("nested_base", st)
        # the queue left libA as the agent did, and pushed nothing
        self.assertEqual(sh("git", "-C", lib, "log", "-1", "--format=%s"), "agent\n")
        self.assertEqual(sh("git", "-C", self.remote, "branch", "--list", "ABC-1"), "")
        self.assertIn("libA", st["history"][-1]["details"][0])
        # The owner looks; a plain retry checks the kept work without a new launch.
        del os.environ["FAKE_NESTED"]
        rc, out = self.q("retry", "ABC-1")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(len(self.calls_of("implementing")), 1)
        self.assertIn("ABC-1", sh("git", "-C", self.remote, "branch", "--list", "ABC-1"))

    def test_a_branch_switch_in_one_blocks(self):
        self.ignored_libA()
        os.environ["FAKE_NESTED"] = "switch:libA"
        self.add_in_place()
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("libA: switched from main to agent", st["reason"])

    def test_the_owners_changes_there_are_theirs_and_the_agents_block(self):
        lib = self.ignored_libA()
        put(os.path.join(lib, "lib.txt"), "lib, the owner's edit\n")
        put(os.path.join(lib, "notes.txt"), "the owner's\n")
        self.add_in_place()
        rc, out = self.run_queue()     # the agent leaves libA alone: the owner's changes are no matter
        self.assertEqual(self.status()["status"], "done", out)
        os.environ["FAKE_NESTED"] = "edit:libA"   # an uncommitted edit is a change there too
        rc, out = self.q("retry", "ABC-1", "--stage", "implement")
        self.assertEqual(rc, 0, out)
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("libA: uncommitted files changed: agent.txt", st["reason"])
        self.assertEqual(cat(os.path.join(lib, "lib.txt")), "lib, the owner's edit\n")

    def test_the_vault_and_the_projects_own_worktrees_are_not_ones(self):
        # the vault inside the project (a repository the stages write), and a
        # worktree of the project's: neither is a repository inside it
        put(os.path.join(self.project, ".gitignore"), "vault/\n.wt/\n")
        self.git_p("add", ".gitignore")
        self.git_p("commit", "-q", "-m", "ignore")
        self.vault = os.path.join(self.project, "vault")
        sh("git", "init", "-q", self.vault)
        os.environ["FAKE_VAULT"] = self.vault
        self.git_p("worktree", "add", "-q", "-b", "wt", os.path.join(self.project, ".wt", "x"))
        os.environ["FAKE_NESTED"] = "commit:.wt/x"
        self.add_in_place("plan,implement,push")
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        self.assertEqual(tq.nested_repos(self.project, (self.vault,)), [])
        self.assertEqual(tq.nested_repos(self.project), ["vault"])

    def test_a_repository_committed_whole_fails(self):
        os.environ["FAKE_NESTED"] = "gitlink:newlib"
        self.add_in_place()
        self.run_queue("--retries", "0")
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("newlib: a repository inside the project is committed as a bare pointer", st["reason"])
        self.assertEqual(sh("git", "-C", self.remote, "branch", "--list", "ABC-1"), "")

    def test_a_deep_one_the_owner_has_not_committed_is_looked_at(self):
        # 4 levels down: not walked, and hidden from git ls-files once the owner's
        # untracked files are excluded for the stage; the baseline is taken before
        self.repo("a/b/c/deep")
        os.environ["FAKE_NESTED"] = "commit:a/b/c/deep"
        self.add_in_place()
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("a/b/c/deep: 1 new commit(s)", st["reason"])

    def test_one_changed_by_a_stopped_agent_blocks_when_it_goes_on(self):
        lib = self.ignored_libA()
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1", FAKE_SLEEP_NESTED=lib)
        self.add_in_place()
        p = self.run_bg()
        self.wait_for(self.sleeping, "the ABC-1 agent")
        self.wait_for(lambda: sh("git", "-C", lib, "log", "-1", "--format=%s").strip() == "agent", "the commit in libA")
        rc, out = self.q("stop", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.wait_for(lambda: self.status()["status"] == "stopped", "ABC-1 to stop", timeout=20)
        p.wait(timeout=30)
        st = self.status()
        self.assertNotIn("nested_base", st)   # the next launch takes its own: the owner's work meanwhile is theirs
        self.assertIn("libA: 1 new commit(s)", st["nested_moved"]["lines"][0])
        for k in ("FAKE_SLEEP", "FAKE_SLEEP_NESTED"):
            del os.environ[k]
        self.q("retry", "ABC-1")
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("the implement agent changed repositories inside the project", st["reason"])
        self.assertIn("push: libA: 1 new commit(s)", st["reason"])
        self.assertIn("harness queue retry ABC-1 then runs the stage again", st["reason"])
        self.assertNotIn("nested_moved", st)
        self.assertEqual(self.calls_of("implementing"), [])   # not launched again to find it
        self.q("retry", "ABC-1")   # the owner has looked
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)

    def stopped_after_a_commit_in_libA(self):
        lib = self.ignored_libA()
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1", FAKE_SLEEP_NESTED=lib)
        self.add_in_place()
        p = self.run_bg()
        self.wait_for(self.sleeping, "the ABC-1 agent")
        self.wait_for(lambda: sh("git", "-C", lib, "log", "-1", "--format=%s").strip() == "agent", "the commit in libA")
        self.q("stop", "ABC-1")
        self.wait_for(lambda: self.status()["status"] == "stopped", "ABC-1 to stop", timeout=20)
        p.wait(timeout=30)
        for k in ("FAKE_SLEEP", "FAKE_SLEEP_NESTED"):
            del os.environ[k]
        self.assertIn("nested_moved", self.status())
        return lib

    def test_another_block_first_says_it_too(self):
        self.stopped_after_a_commit_in_libA()
        gd = self.git_p("rev-parse", "--absolute-git-dir")   # what a killed agent can leave
        os.makedirs(os.path.join(gd, "rebase-merge"))
        self.q("retry", "ABC-1")
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("rebase", st["reason"])
        self.assertIn("Also, the implement agent changed repositories inside the project", st["reason"])
        self.assertIn("libA: 1 new commit(s)", st["reason"])
        self.assertNotIn("nested_moved", st)

    def test_a_retry_from_the_push_is_checked_too(self):
        self.stopped_after_a_commit_in_libA()
        c = self.git_p("-c", "user.name=A", "-c", "user.email=a@example.com", "commit-tree", "ABC-1^{tree}", "-p",
                       "ABC-1", "-m", "ABC-1: the owner's commit on the branch")
        self.git_p("update-ref", "refs/heads/ABC-1", c)
        rc, out = self.q("retry", "ABC-1", "--stage", "push")
        self.assertEqual(rc, 0, out)
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("the implement agent changed repositories inside the project", st["reason"])
        self.assertEqual(sh("git", "-C", self.remote, "branch", "--list", "ABC-1"), "")

    def test_a_baseline_left_by_a_run_that_died_is_looked_at_first(self):
        lib = self.ignored_libA()
        self.add_in_place()
        q = tq.Queue(self.state)
        st = q.load("ABC-1")
        st.update(nested_base=tq.nested_state(self.project), nested_stage="implement")
        q.save(st)
        sh("git", "-C", lib, "-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-q", "--allow-empty",
           "-m", "the agent before the run died")
        self.run_queue()
        st = self.status()
        self.assertEqual(st["status"], "blocked")
        self.assertIn("the implement agent changed repositories inside the project: libA: 1 new commit(s)",
                      st["reason"].replace(", which the queue does not verify, commit or push", ""))
        self.assertNotIn("nested_base", st)
        self.assertEqual(self.calls_of("implementing"), [])

    def test_not_looked_at_is_not_unchanged(self):
        self.ignored_libA()
        st = {"key": "ABC-1", "project": self.project, "nested_base": tq.nested_state(self.project)}
        ctx = types.SimpleNamespace(q=types.SimpleNamespace(log=lambda *a: None, save=lambda s: None))
        with mock.patch.object(tq, "nested_moved", side_effect=RuntimeError("git ls-files timed out")):
            tq.nested_check(ctx, st, "implement", (), ended=False)   # a launch that did not end: kept for the next
            self.assertIn("nested_base", st)
            self.assertNotIn("nested_moved", st)
            tq.nested_check(ctx, st, "implement", ())
        self.assertNotIn("nested_base", st)
        self.assertTrue(st["nested_moved"]["unknown"])
        with self.assertRaises(tq.NestedMoved) as e:
            tq.nested_block(st)
        self.assertIn("the implement agent may have changed repositories inside the project: they could not be "
                      "looked at after it (git ls-files timed out)", str(e.exception).replace(
                          ", which the queue does not verify, commit or push", ""))

    def test_a_link_to_a_name_that_is_not_utf8(self):
        lib = self.ignored_libA()
        os.symlink(b"caf\xe9".decode("utf-8", "surrogateescape"), os.path.join(lib, "odd"))
        self.assertTrue(tq.nested_state(self.project)["libA"]["files"]["odd"].startswith("?? link "))

    def test_a_tracked_file_made_a_repository_is_a_bare_pointer(self):
        put(os.path.join(self.project, "lib"), "a file\n")
        self.git_p("add", "lib")
        self.git_p("commit", "-q", "-m", "a file")
        base = self.git_p("rev-parse", "HEAD")
        self.git_p("rm", "-q", "--cached", "lib")
        os.remove(os.path.join(self.project, "lib"))
        self.repo("lib")
        self.git_p("add", "lib")
        self.git_p("commit", "-q", "-m", "a repository in its place")
        self.assertEqual(tq.added_gitlinks(self.project, base, "HEAD"), ["lib"])

    def test_a_file_written_again_as_it_was_is_no_change(self):
        lib = self.ignored_libA()
        put(os.path.join(lib, "lib.txt"), "lib, the owner's edit\n")
        put(os.path.join(lib, "notes.txt"), "the owner's\n")
        os.environ["FAKE_NESTED"] = "touch:libA"
        self.add_in_place()
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)

    def test_a_submodule_is_not_a_bare_pointer(self):
        sub = os.path.join(self.tmp, "sub")
        sh("git", "init", "-q", "-b", "main", sub)
        sh("git", "-C", sub, "-c", "user.name=O", "-c", "user.email=o@example.com", "commit", "-q", "--allow-empty",
           "-m", "sub")
        os.environ.update(FAKE_NESTED="submodule:ext/sub", FAKE_SUB_URL=sub)
        self.add_in_place()
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)
        # and git's own setting to hide submodule changes does not hide a bare one
        self.git_p("config", "diff.ignoreSubmodules", "all")
        os.environ["FAKE_NESTED"] = "gitlink:newlib"
        rc, out = self.q("add", "ABC-2", "--project", self.project, "--in-place", "--stages", "implement,push")
        self.assertEqual(rc, 0, out)
        self.run_queue("--retries", "0")
        st = self.status("ABC-2")
        self.assertEqual(st["status"], "blocked")
        self.assertIn("newlib: a repository inside the project is committed as a bare pointer", st["reason"])

    def test_the_projects_submodules_are_its_own(self):
        # a build step (git submodule update) or the ticket moves one's HEAD: the top commit says so
        sub = os.path.join(self.tmp, "sub")
        sh("git", "init", "-q", "-b", "main", sub)
        sh("git", "-C", sub, "-c", "user.name=O", "-c", "user.email=o@example.com", "commit", "-q", "--allow-empty",
           "-m", "sub")
        self.git_p("-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "mods/sub")
        self.git_p("commit", "-q", "-m", "a submodule")
        self.assertEqual(tq.nested_repos(self.project), [])
        os.environ["FAKE_NESTED"] = "switch:mods/sub"
        self.add_in_place()
        rc, out = self.run_queue()
        self.assertEqual(self.status()["status"], "done", out)

    def test_tools_checkouts_and_a_stray_git_folder_are_not_ones(self):
        self.repo(".venv/src/pkg")
        self.repo(".terraform/modules/m")
        os.makedirs(os.path.join(self.project, "bogus", ".git"))   # git there is the project's
        self.repo("libA")
        self.assertEqual(tq.nested_repos(self.project), ["libA"])

    def test_many_of_them_go_to_git_add_in_a_file(self):
        # Windows: past ~32k characters the command line cannot hold the excludes
        self.repo("libA", commit=False)
        self.repo("lib[1]", commit=False)
        put(os.path.join(self.project, "lx.txt"), "mine\n")
        os.makedirs(os.path.join(self.state, "ABC-1"), exist_ok=True)
        q = types.SimpleNamespace(dir=lambda k: os.path.join(self.state, k), log=lambda *a: self.fail(a))
        with mock.patch.object(tq, "SNAP_ARGS_MAX", 0):
            tq.snapshot(q, {"project": self.project, "key": "ABC-1"}, "implement")
        tree = self.git_p("ls-tree", "-r", "--name-only", "refs/harness-queue/snap/ABC-1").split("\n")
        self.assertIn("lx.txt", tree)
        self.assertFalse([f for f in tree if f.startswith("lib")], tree)
        self.assertEqual(os.listdir(os.path.join(self.state, "ABC-1")), [])

    def test_the_prompt_and_card_say_so(self):
        self.add_in_place("implement")
        self.run_queue()
        self.assertIn("change nothing in them, no edit, commit or branch switch", cat(self.calls + ".implementing"))
        card = json.loads(cat(self.calls + ".card").splitlines()[-1])
        self.assertTrue(card["where"].endswith("change nothing in repositories inside it"))


if __name__ == "__main__":
    unittest.main()
