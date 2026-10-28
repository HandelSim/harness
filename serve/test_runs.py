"""Tests for the pub page's run viewer (serve/runs.py, /runs) against a live server, over
an opencode database built with opencode 1.19.3's own session/message/part tables.

Run: python3 -m unittest serve/test_runs.py (tests/unit_serve_test.sh does).
"""

import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import quote, unquote, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pub  # noqa: E402
import runs  # noqa: E402
import diff_view  # noqa: E402

# As opencode 1.19.3 creates them (sqlite_master), trimmed to the tables the viewer reads.
SCHEMA = """
CREATE TABLE `session` (`id` text PRIMARY KEY, `project_id` text NOT NULL, `workspace_id` text, `parent_id` text,
  `slug` text NOT NULL, `directory` text NOT NULL, `path` text, `title` text NOT NULL, `version` text NOT NULL,
  `share_url` text, `summary_additions` integer, `summary_deletions` integer, `summary_files` integer,
  `summary_diffs` text, `metadata` text, `cost` real DEFAULT 0 NOT NULL, `tokens_input` integer DEFAULT 0 NOT NULL,
  `tokens_output` integer DEFAULT 0 NOT NULL, `tokens_reasoning` integer DEFAULT 0 NOT NULL,
  `tokens_cache_read` integer DEFAULT 0 NOT NULL, `tokens_cache_write` integer DEFAULT 0 NOT NULL, `revert` text,
  `permission` text, `agent` text, `model` text, `time_created` integer NOT NULL, `time_updated` integer NOT NULL,
  `time_compacting` integer, `time_archived` integer);
CREATE TABLE `message` (`id` text PRIMARY KEY, `session_id` text NOT NULL, `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL, `data` text NOT NULL);
CREATE TABLE `part` (`id` text PRIMARY KEY, `message_id` text NOT NULL, `session_id` text NOT NULL,
  `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL);
"""

ROOT = "ses_root1"
CHILD = "ses_child1"
QSES = "ses_queue1"


def build_db(path, project, queue_repo, now_ms):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    def session(sid, parent, directory, title, t, agent="yolo"):
        con.execute("insert into session (id, project_id, parent_id, slug, directory, title, version, agent, model, "
                    "tokens_input, tokens_output, time_created, time_updated) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, "global", parent, "s", directory, title, "1.19.3", agent,
                     json.dumps({"id": "big", "providerID": "harness"}), 1200, 300, t, t + 5000))

    def message(sid, mid, t, data, parts):
        con.execute("insert into message values (?,?,?,?,?)", (mid, sid, t, t, json.dumps(data)))
        for i, p in enumerate(parts):
            con.execute("insert into part values (?,?,?,?,?,?)", (f"prt_{mid[4:]}{i}", mid, sid, t, t, json.dumps(p)))

    user = {"role": "user", "agent": "yolo", "model": {"providerID": "harness", "modelID": "big"}}
    session(ROOT, None, project, "Fix the <b>parser</b>", now_ms - 60000)
    message(ROOT, "msg_a1", now_ms - 60000, user, [{"type": "text", "text": "fix it <script>alert(1)</script>"}])
    message(ROOT, "msg_a2", now_ms - 59000, {"role": "assistant", "agent": "yolo", "providerID": "harness",
                                             "modelID": "big", "tokens": {"input": 900, "output": 40}}, [
        {"type": "step-start"},
        {"type": "reasoning", "text": "look at the parser first"},
        {"type": "tool", "tool": "bash", "callID": "c1", "state": {
            "status": "completed", "title": "ls", "input": {"command": "ls"},
            "output": "x" * (runs.TEXT_CAP + 50), "time": {"start": 1, "end": 2001}}},
        {"type": "tool", "tool": "task", "callID": "c2", "state": {
            "status": "completed", "title": "explore", "input": {"prompt": "find it"},
            "output": "found", "metadata": {"sessionId": CHILD}}},
        {"type": "tool", "tool": "read", "callID": "c3", "state": {
            "status": "error", "input": {"filePath": "/nope"}, "error": "no such file"}},
        {"type": "tool", "tool": "edit", "callID": "c4", "state": {
            "status": "completed", "title": "src/parse.py",
            "input": {"filePath": "src/parse.py", "oldString": "old <line>", "newString": "new line"},
            "metadata": {"diff": "--- src/parse.py\n+++ src/parse.py\n@@ -1 +1 @@\n-old <line>\n+new line\n"},
            "output": "Edit applied."}},
        {"type": "tool", "tool": "todowrite", "callID": "c5", "state": {
            "status": "completed", "title": "1 todo", "input": {"todos": [
                {"content": "read the parser", "status": "completed"},
                {"content": "fix <the> bug", "status": "in_progress"}]}, "output": "[]"}},
        {"type": "text", "text": "Done."}])
    session(CHILD, ROOT, project, "explore subagent", now_ms - 58000, agent="explore")
    message(CHILD, "msg_c1", now_ms - 58000, {"role": "user", "agent": "explore"}, [{"type": "text", "text": "find it"}])
    session(QSES, None, queue_repo, "plan ABC-12", now_ms - 120000)
    message(QSES, "msg_q1", now_ms - 120000, user, [{"type": "text", "text": "plan the ticket"}])
    con.commit()
    con.close()


class RunsServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.data = os.path.join(root, "data")
        os.makedirs(os.path.join(self.data, "opencode"))
        self.live = os.path.join(self.data, "harness", "live")
        os.makedirs(self.live)
        self.queue = os.path.join(root, "queue")
        repo = os.path.join(self.queue, "ABC-12", "repo")
        os.makedirs(repo)
        with open(os.path.join(self.queue, "ABC-12", "log.md"), "w") as f:
            f.write("12:00 plan-1 started\n12:01 verifier: pass <ok>\n")
        out = os.path.join(self.queue, "ABC-12", "plan-1.out")
        with open(out, "w") as f:
            f.write("the plan\n")
        self.vault = os.path.join(root, "vault")
        os.makedirs(os.path.join(self.vault, "wiki", "notes", "tickets"))
        with open(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-12-plan.md"), "w") as f:
            f.write("## Goal\n\nFix it.\n\n## Questions\n\n1. [blocking] Which <db>?\n")
        self.now_ms = int(time.time() * 1000)
        os.utime(out, (time.time() - 30, time.time() - 30))
        build_db(os.path.join(self.data, "opencode", "opencode.db"), os.path.join(root, "proj"), repo, self.now_ms)
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        self.tmp.cleanup()

    def start(self, **kw):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = srv.server_address[1]
        kw.setdefault("vault", self.vault)
        vault = kw.pop("vault")
        cfg = pub.Config(port, vault, "", 0, "", (), agent_data=[("host", self.data)], queue_dir=self.queue, **kw)
        srv.RequestHandlerClass = pub.make_handler(cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        self.port = port
        return cfg

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data.decode("utf-8", "replace")

    def ticket(self, **st):
        base = {"key": "ABC-12", "project": "/p", "base": "main", "status": "needs-answers", "stage": "plan",
                "attempt": 0, "feedback": "", "reason": "1 blocking question(s) in the plan",
                "questions": [{"text": "Which <db>?", "blocking": True},
                              {"text": "Log level? Default: info", "blocking": False}],
                "history": [{"at": "2026-10-26T20:00:00Z", "stage": "plan", "attempt": 2, "result": "failed",
                             "details": ["the plan file was not written: write it to <C:\\x>"]}]}
        base.update(st)
        with open(os.path.join(self.queue, "ABC-12", "state.json"), "w") as f:
            json.dump(base, f)
        with open(os.path.join(self.queue, "queue.list"), "w") as f:
            f.write("ABC-12\n")

    def run_on(self, key="ABC-12", **info):
        """A queue run going (a fresh heartbeat), on key."""
        info.update(beat=time.time(), ticket=key)
        with open(os.path.join(self.queue, "run.json"), "w") as f:
            json.dump(info, f)

    def state(self):
        with open(os.path.join(self.queue, "ABC-12", "state.json")) as f:
            return json.load(f)

    def act(self, action, fields=()):
        h = {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{self.port}"}
        return self.req("POST", f"/runs/ticket/ABC-12/{action}", urlencode(list(fields)), h)

    def go_live(self, sid=ROOT):
        with open(os.path.join(self.live, sid + ".json"), "w") as f:
            f.write("{}")

    def send(self, text, origin=None, sid=ROOT, cookie=None):
        h = {"Content-Type": "application/x-www-form-urlencoded",
             "Origin": origin or f"http://127.0.0.1:{self.port}"}
        if cookie:
            h["Cookie"] = cookie
        return self.req("POST", f"/runs/{sid}/send", urlencode({"text": text}), h)

    def inbox(self, sid=ROOT):
        d = os.path.join(self.live, sid + ".inbox")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []


class RunsTest(RunsServer):
    def test_list_groups_queue_stages_and_marks_live(self):
        self.start()
        self.go_live()
        r, body = self.req("GET", "/runs")
        self.assertEqual(r.status, 200)
        self.assertIn("Fix the &lt;b&gt;parser&lt;/b&gt;", body)
        self.assertNotIn("<b>parser</b>", body)
        self.assertIn("Live now", body)
        self.assertIn("<span class='pill t-run dot pulse'>live</span>", body)
        self.assertIn("href='/runs/ticket/ABC-12'", body)
        # plan-1.out is the plan's second attempt (outputs count from 0)
        self.assertIn("<span class='tag'>plan &middot; 2</span> <a href='/runs/ses_queue1'>", body)
        self.assertIn("1 subagent session", body)
        self.assertNotIn(f"href='/runs/{CHILD}'", body)  # subagents sit under their parent
        r, body = self.req("GET", "/runs?q=parser")
        self.assertNotIn("plan ABC-12", body)

    def test_detail_shows_thinking_tools_and_subagents_escaped(self):
        self.start()
        r, body = self.req("GET", f"/runs/{ROOT}")
        self.assertEqual(r.status, 200)
        self.assertIn("fix it &lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertIn("look at the parser first", body)
        self.assertIn("<b>bash</b> ls", body)
        self.assertIn("completed 2.0s", body)
        self.assertIn(f"href='/runs/{ROOT}/part/prt_a22'", body)   # the capped output links to all of it
        self.assertIn(f"href='/runs/{CHILD}'", body)
        self.assertIn("no such file", body)
        self.assertIn("Read-only", body)
        self.assertNotIn("<form method='post'", body)
        # one inline script, allowed by this response's nonce only
        nonce = re.search(r"'nonce-([^']+)'", r.getheader("Content-Security-Policy")).group(1)
        self.assertEqual(re.findall(r"<script nonce='([^']+)'>", body), [nonce])

    def test_subagent_page_links_back_to_its_parent(self):
        self.start()
        self.go_live()
        r, body = self.req("GET", f"/runs/{CHILD}")
        self.assertIn(f"<a href='/runs/{ROOT}'>Fix the &lt;b&gt;parser&lt;/b&gt;</a>", body)
        self.assertIn("subagent of a live run", body)

    def test_tail_export_and_part(self):
        self.start()
        r, body = self.req("GET", f"/runs/{ROOT}/tail?from=1")
        d = json.loads(body)
        self.assertEqual((d["from"], d["count"]), (1, 2))
        self.assertIn("data-i='1'", d["html"])
        self.assertNotIn("data-i='0'", d["html"])
        r, body = self.req("GET", f"/runs/{ROOT}.json")
        d = json.loads(body)
        self.assertEqual(d["children"], [CHILD])
        self.assertEqual(len(d["messages"]), 2)
        r, body = self.req("GET", f"/runs/{ROOT}/part/prt_a22")
        self.assertEqual(len(body), runs.TEXT_CAP + 50)
        self.assertTrue(r.getheader("Content-Security-Policy").startswith("sandbox"))
        for path in ("/runs/ses_nope", "/runs/../etc", f"/runs/{ROOT}/part/bad", "/runs/ticket/ABC-99",
                     "/runs/ticket/ABC-12/..%2flog.md", "/runs/nope.json"):
            r, _ = self.req("GET", path)
            self.assertEqual(r.status, 404, path)

    def test_ticket_page(self):
        self.start()
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertEqual(r.status, 200)
        self.assertIn("verifier: pass &lt;ok&gt;", body)
        self.assertIn("plan-1.out", body)
        r, body = self.req("GET", "/runs/ticket/ABC-12/plan-1.out")
        self.assertEqual(body, "the plan\n")

    def test_queue_panel_says_what_each_ticket_waits_on(self):
        self.ticket()
        self.start()
        r, body = self.req("GET", "/runs")
        self.assertIn("<h2>Queue</h2>", body)
        self.assertIn("<a href='/runs/ticket/ABC-12'><b>ABC-12</b></a> "
                      "<span class='pill t-wait dot'>needs your answers</span>", body)
        self.assertIn("does not hold the queue", body)

    def test_a_done_ticket_says_where_its_work_went(self):
        sha = "ab" * 20
        self.ticket(status="done", stage="done", questions=[], reason="",
                    push={"remote": "origin", "url": "https://github.com/acme/web", "branch": "ABC-12", "sha": sha,
                          "result": "pushed", "error": ""},
                    compare_url="https://github.com/acme/web/compare/main...ABC-12?expand=1",
                    commits=[{"sha": sha, "subject": "Remember the <user>"}],
                    diffstat={"files": 3, "insertions": 42, "deletions": 7}, folder_branch_now="ABC-12")
        self.start()
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("<h2>Result</h2>", body)
        self.assertIn("<b>pushed to origin</b>", body)
        self.assertIn("href='https://github.com/acme/web/compare/main...ABC-12?expand=1'", body)
        self.assertIn("Open a pull request", body)
        self.assertIn(f"<code>{sha[:12]}</code> Remember the &lt;user&gt;", body)
        self.assertIn("3 file(s), +42 &minus;7", body)
        r, body = self.req("GET", "/runs/queue")
        self.assertIn(f"pushed branch ABC-12 to origin ({sha[:12]})", body)
        self.assertIn(">Run again</button>", body)   # the row says what its retry does, as the page does
        # a refused push says why; a link that is not a web page is not one
        self.ticket(status="blocked", stage="push", questions=[], reason="push to origin refused",
                    push={"remote": "origin", "url": "", "branch": "ABC-12", "sha": sha, "result": "rejected",
                          "error": "! [rejected] (non-fast-forward)"},
                    compare_url="javascript:alert(1)", commits=[], diffstat={})
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("the push to origin was refused", body)
        self.assertIn("! [rejected] (non-fast-forward)", body)
        self.assertNotIn("javascript:", body)
        self.assertIn("No commits on the branch.", body)
        # several repositories: each one's branch, and where the change is left uncommitted
        self.ticket(status="done", stage="done", questions=[], reason="changes left uncommitted in the project",
                    push={"remote": "origin", "url": "", "branch": "ABC-12", "sha": sha, "result": "pushed",
                          "error": "", "repo": "api"},
                    pushes=[{"remote": "origin", "url": "", "branch": "ABC-12", "sha": sha, "result": "pushed",
                             "error": "", "repo": "api"}],
                    left_uncommitted=[".", "web"], commits=[{"sha": sha, "subject": "api: the <route>"}],
                    diffstat={"files": 2, "insertions": 5, "deletions": 1})
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("api: branch <b>ABC-12</b>", body)
        self.assertIn("api: <b>pushed to origin</b>", body)
        self.assertIn("left uncommitted in <b>the project, web</b>", body)
        r, body = self.req("GET", "/runs/queue")
        self.assertIn(f"api: pushed branch ABC-12 to origin ({sha[:12]}); changes left uncommitted in the project",
                      body)
        # everything left uncommitted: no branch, nothing pushed
        self.ticket(status="done", stage="done", questions=[], reason="changes left uncommitted in the project",
                    push={"remote": "", "url": "", "branch": "", "sha": "", "result": "local", "error": "",
                          "repo": "."}, pushes=[], left_uncommitted=["."], commits=[],
                    diffstat={"files": 1, "insertions": 1, "deletions": 0})
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("branch <b></b>", body)
        self.assertNotIn("No commits on the branch.", body)
        self.assertIn("left uncommitted in <b>the project</b>", body)
        # an old state.json without results: no panel
        self.ticket(status="done", stage="done", questions=[])
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("<h2>Result</h2>", body)

    def test_home_says_what_the_queue_is_doing(self):
        self.ticket()
        self.start()
        r, body = self.req("GET", "/")
        self.assertIn("<b>Queue not running</b>", body)
        self.assertIn("<b>1 waiting on you</b>", body)
        self.run_on("ABC-1", stage="implement")
        r, body = self.req("GET", "/")
        self.assertIn("<b>Queue running</b> on <b>ABC-1</b> &middot; implement", body)
        self.assertIn("title='1 ticket waiting on you'", body)

    def test_ticket_page_shows_questions_attempts_and_notes(self):
        self.ticket()
        self.start()
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertEqual(r.status, 200)
        self.assertIn("<div class='q blocking'>Which &lt;db&gt;?</div>", body)
        self.assertIn("name='q0' value='Which &lt;db&gt;?'", body)
        self.assertIn("action='/runs/ticket/ABC-12/answer'", body)
        self.assertIn("Go with the defaults", body)
        self.assertIn("Plan again without answering", body)
        self.assertIn("<span class='pill t-bad'>failed</span><span class='tag'>plan</span><span>attempt 2</span>",
                      body)
        self.assertIn("write it to &lt;C:\\x&gt;", body)
        self.assertIn("href='/runs/ticket/ABC-12/plan-1.out'", body)   # the failed attempt's output
        self.assertIn("1. [blocking] Which &lt;db&gt;?", body)          # the plan note
        self.assertEqual(body.count("1. [blocking] Which"), 1)          # open by the answers, not again below
        self.assertIn("<details class='doc' data-k='qplan' open>", body)
        self.ticket(status="plan-review", questions=[])
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("action='/runs/ticket/ABC-12/approve'", body)
        self.assertIn("Approve the plan, or send feedback", body)   # a free feedback box
        self.assertIn("data-k='qplan' open", body)                  # the plan to approve, open beside it
        self.assertIn("Send feedback and plan again", body)
        self.assertNotIn("Go with the defaults", body)
        # blocked at review: no planner's questions, a note that re-plans, and Retry runs review as is
        self.ticket(status="blocked", stage="review", reason="the <review> found a bug")
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("Get it going again", body)
        self.assertIn("It stopped at review: the &lt;review&gt; found a bug.", body)
        self.assertIn("Send and plan again", body)
        self.assertNotIn("name='q0'", body)
        self.assertNotIn("Answer the planner", body)
        self.assertIn("<button type='submit' title='queue it again at the stage it stopped at, as it is'>Retry"
                      "</button>", body)
        self.ticket(status="blocked", stage="review", stages=["implement", "review"])
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("Send and run review again", body)
        self.ticket(status="running", attempt=1)
        self.run_on(stage="plan", step="asking the <verifier>")
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("/answer'", body)
        self.assertNotIn("/retry'", body)
        self.assertIn("action='/runs/ticket/ABC-12/stop'", body)   # a running ticket can be stopped
        self.assertIn("going with the default", body)
        self.assertIn("plan, attempt 2: asking the &lt;verifier&gt;", body)   # what the run is doing in it
        # left running by a run that is gone (killed, its window closed): not shown as running
        os.remove(os.path.join(self.queue, "run.json"))
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("run ended</span>", body)
        self.assertIn("the queue run ended while it was at plan; start the queue to carry on there", body)
        self.assertIn("action='/runs/queue/start'", body)          # the queue page's Start, here
        self.assertIn("<input type='hidden' name='back' value='ABC-12'>", body)
        self.assertIn("ABC-12 waits until a run takes it", body)
        self.assertIn("action='/runs/ticket/ABC-12/stop'", body)
        self.assertNotIn(">running</span>", body)
        r, body = self.req("GET", "/runs/queue")
        self.assertIn("run ended</span>", body)
        self.run_on("ABC-1")   # a run on another ticket takes this one again in its turn
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("the run going now takes it again there", body)
        # or it is queued again now, from the stage picked: no run has it in hand
        self.assertIn("Queue again</button>", body)
        r, body = self.act("retry", [("stage", "plan")])
        self.assertEqual(r.status, 303, body)
        self.assertEqual((self.state()["status"], self.state()["stage"]), ("queued", "plan"))
        self.assertNotIn("the queue is not running", r.getheader("Location"))   # the run takes it
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("action='/runs/queue/start'", body)

    def test_answering_on_the_page_saves_the_answers_and_requeues(self):
        self.ticket()
        self.start()
        r, body = self.act("answer", [("q0", "Which <db>?"), ("a0", ""), ("q1", "Log level?"), ("a1", "")])
        self.assertEqual(r.status, 409)
        self.assertIn("nothing to send", body)
        r, _ = self.act("answer", [("q0", "Which <db>?"), ("a0", "postgres"), ("note", "keep it small")])
        self.assertEqual(r.status, 303)
        self.assertIn("/runs/ticket/ABC-12?done=", r.getheader("Location"))
        st = self.state()
        self.assertEqual((st["status"], st["stage"]), ("queued", "plan"))
        self.assertNotIn("questions", st)
        self.assertIs(st["plan_hold"], True)   # the plan made with the answers comes back to be agreed
        with open(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-12-answers.md")) as f:
            text = f.read()
        self.assertIn("## Which <db>?\n\npostgres", text)
        self.assertIn("## Other\n\nkeep it small", text)
        r, body = self.req("GET", r.getheader("Location"))
        self.assertIn("<p class='flash' role='status'>", body)
        r, body = self.act("answer", [("note", "one more thing")])
        self.assertEqual(r.status, 303)   # a queued ticket takes more answers before it runs
        r, body = self.act("answer", [("defaults", "1")])
        self.assertEqual(r.status, 303)
        self.assertIs(self.state()["plan_hold"], False)   # the defaults taken: it goes on without coming back
        self.ticket(status="running")
        self.run_on()
        r, body = self.act("answer", [("defaults", "1")])
        self.assertEqual(r.status, 409)   # but not a running one
        self.assertIn("ABC-12 is running now", body)
        r, body = self.act("approve")
        self.assertEqual(r.status, 409)
        self.assertIn("not a plan waiting for approval", body)

    def test_an_in_place_tickets_sessions_are_those_made_while_its_stages_ran(self):
        proj = os.path.join(self.tmp.name, "proj")
        rs = runs.Runs([("host", self.data)], queue_dir=self.queue)
        row = {"directory": proj, "time_created": self.now_ms - 60000}   # ROOT, made in the project
        self.ticket(in_place=True, project=proj, launches=[[self.now_ms - 70000, self.now_ms - 50000]])
        self.assertEqual(rs.ticket_of_row(row), "ABC-12")
        self.assertEqual([d["id"] for d in rs.ticket_sessions("ABC-12")], [ROOT])
        self.ticket(in_place=True, project=proj, launches=[[self.now_ms - 50000, 0]])   # the owner's own, before
        self.assertEqual(rs.ticket_of_row(row), "")
        self.ticket(in_place=False, project=proj, launches=[[self.now_ms - 70000, self.now_ms - 50000]])
        self.assertEqual(rs.ticket_of_row(row), "")

    def test_approve_and_retry_on_the_page(self):
        self.ticket(status="plan-review", stage="implement")
        self.start()
        r, _ = self.act("approve")
        self.assertEqual(r.status, 303)
        self.assertEqual((self.state()["status"], self.state()["plan_approved"]), ("queued", True))
        self.assertIn("the queue is not running, so nothing happens until you start it", unquote(r.getheader("Location")))
        r, body = self.req("GET", r.getheader("Location"))
        self.assertIn("action='/runs/queue/start'", body)   # shown, never started by the approve
        self.assertFalse(os.path.exists(os.path.join(self.queue, "run.json")))
        self.ticket(status="blocked", stage="review", reason="review failed 2 time(s)")
        r, _ = self.act("retry")
        self.assertEqual(r.status, 303)
        # its clone and commits are gone: from the start, not a review of nothing
        self.assertEqual((self.state()["status"], self.state()["stage"]), ("queued", "plan"))
        r, _ = self.act("delete")
        self.assertEqual(r.status, 404)
        r, _ = self.req("POST", "/runs/ticket/ABC-12/retry", "", {"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)

    def test_continue_on_the_page_goes_on_in_the_agents_session(self):
        session = {"stage": "implement", "attempt": 1, "id": "ses_abc", "container": True}
        self.ticket(status="blocked", stage="implement", base_sha="abc", reason="no commits on branch ABC-12",
                    questions=[], session=session)
        self.start()
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("action='/runs/ticket/ABC-12/continue'", body)
        self.assertIn("Continue implement</button>", body)
        self.assertIn("its agent goes on in the same session", body)
        r, body = self.act("continue", [("text", "the <db> is up now")])
        self.assertEqual(r.status, 303, body)
        st = self.state()
        self.assertEqual((st["status"], st["stage"]), ("queued", "implement"))
        self.assertEqual(st["resume"]["message"], "the <db> is up now")
        self.assertIn("no commits on branch ABC-12", st["resume"]["why"])
        # stopped mid-stage, no session known: offered all the same, a new agent finishing the work
        self.ticket(status="stopped", stage="review", base_sha="abc", questions=[])
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("Go on with it", body)
        self.assertIn("a new agent is told to finish the work there", body)
        # not where no agent works, nor where nothing waits to go on
        self.ticket(status="blocked", stage="push", base_sha="abc", questions=[])
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("/continue'", body)
        r, _ = self.act("continue")
        self.assertEqual(r.status, 409)
        self.ticket(status="done", stage="done", base_sha="abc", questions=[])
        r, _ = self.act("continue")
        self.assertEqual(r.status, 409)

    def test_continue_is_on_the_queue_page_and_atop_the_ticket(self):
        session = {"stage": "implement", "attempt": 0, "id": "ses_abc", "container": True}
        self.ticket(status="stopped", stage="implement", base_sha="abc", questions=[], session=session)
        self.start()
        r, body = self.req("GET", "/runs/queue")
        self.assertIn("action='/runs/ticket/ABC-12/continue'", body)
        self.assertIn("Continue implement</button>", body)
        self.assertIn("Start over</button>", body)        # the retry beside it says what it does
        self.assertNotIn(">Resume</button>", body)
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("<a class='btn primary' href='#go-on'", body)
        self.assertIn("<section class='panel' id='go-on'><h2>Go on with it</h2>", body)
        self.assertIn("Start over</button>", body)
        # from the queue page, no message: it goes on in the session, back on the queue page
        r, body = self.act("continue", [("back", "queue")])
        self.assertEqual(r.status, 303, body)
        self.assertIn("/runs/queue", r.getheader("Location"))
        st = self.state()
        self.assertEqual((st["status"], st["stage"], st["resume"]["message"]), ("queued", "implement", ""))
        # queued to go on: a message for its agent, on its page only
        r, body = self.req("GET", "/runs/queue")
        self.assertNotIn("/continue'", body)
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("It is queued to go on with implement where its agent was cut off.", body)
        r, body = self.act("continue", [("text", "and run the <linter>")])
        self.assertEqual(r.status, 303, body)
        self.assertEqual(self.state()["resume"]["message"], "and run the <linter>")
        # queued to start over (a retry) or not begun: nothing to go on with
        self.ticket(status="queued", stage="implement", base_sha="abc", questions=[], session=session)
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("/continue'", body)
        # blocked with no vault: the Continue form all the same
        self.ticket(status="blocked", stage="fix", base_sha="abc", questions=[], session=session)
        self.start(vault="")
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("Continue fix</button>", body)
        self.assertIn("(it was blocked)", body)
        # its work gone: the queue would refuse, so none is offered, and Resume is Resume again
        shutil.rmtree(os.path.join(self.queue, "ABC-12", "repo"))
        self.ticket(status="stopped", stage="implement", base_sha="abc", questions=[], session=session)
        for page in ("/runs/queue", "/runs/ticket/ABC-12"):
            r, body = self.req("GET", page)
            self.assertNotIn("/continue'", body, page)
            self.assertNotIn("#go-on", body, page)
            self.assertIn(">Resume</button>", body, page)

    def test_answers_go_to_the_vault_the_queue_ran_with(self):
        other = os.path.join(self.tmp.name, "vault-b")
        os.makedirs(other)
        self.ticket(vault=other)
        self.start()
        r, _ = self.act("answer", [("a0", "postgres"), ("q0", "Which <db>?")])
        self.assertEqual(r.status, 303)
        self.assertTrue(os.path.exists(os.path.join(other, "wiki", "notes", "tickets", "ABC-12-answers.md")))
        self.assertFalse(os.path.exists(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-12-answers.md")))

    def test_a_malformed_state_does_not_take_the_pages_down(self):
        self.ticket(status=["x"], stage=3, reason={"a": 1}, attempt="2", updated=5, history={"x": 1},
                    questions=[{"text": ["q"]}, "q", {"text": "ok?", "blocking": True}])
        self.start()
        r, body = self.req("GET", "/runs")
        self.assertEqual(r.status, 200)
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertEqual(r.status, 200)
        self.assertIn("ok?", body)

    def test_answers_need_a_vault(self):
        self.ticket()
        self.start(vault="")
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("/answer'", body)
        r, body = self.act("answer", [("a0", "x")])
        self.assertEqual(r.status, 409)
        self.assertIn("no vault", body)

    def test_detail_shows_diffs_and_the_todo_list(self):
        self.start()
        r, body = self.req("GET", f"/runs/{ROOT}")
        self.assertIn("<span class='del'>-old &lt;line&gt;</span>", body)
        self.assertIn("<span class='add'>+new line</span>", body)
        self.assertIn("+1 &minus;1", body)
        self.assertNotIn("oldString", body)        # the diff stands in for the edit's input
        self.assertIn("Todo <span class='muted'>1/2 done</span>", body)
        self.assertIn("<li class='in_progress'><span>&#9656;</span><span>fix &lt;the&gt; bug</span></li>", body)
        self.go_live()
        r, body = self.req("GET", f"/runs/{ROOT}")
        self.assertLess(body.index("Message the agent"), body.index("Todo <span"))

    def test_send_to_a_live_run_carries_its_agent_and_model(self):
        self.start()
        r, body = self.send("hello")
        self.assertEqual(r.status, 409)
        self.assertIn("not live", body)
        self.go_live()
        r, body = self.req("GET", f"/runs/{ROOT}")
        self.assertIn(f"action='/runs/{ROOT}/send'", body)
        r, _ = self.send("also check the tests")
        self.assertEqual(r.status, 303)
        names = self.inbox()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".json") and "-local-" in names[0])
        with open(os.path.join(self.live, ROOT + ".inbox", names[0])) as f:
            msg = json.load(f)
        self.assertEqual(msg, {"parts": [{"type": "text", "text": "also check the tests"}], "agent": "yolo",
                               "model": {"providerID": "harness", "modelID": "big"}})
        r, body = self.req("GET", f"/runs/{ROOT}")
        self.assertIn("<b>waiting</b>: also check the tests", body)

    def test_send_refuses_cross_origin_stale_empty_and_oversized(self):
        self.start()
        self.go_live()
        r, _ = self.send("x", origin="http://evil.example")
        self.assertEqual(r.status, 403)
        r, _ = self.send("   ")
        self.assertEqual(r.status, 409)
        r, _ = self.send("x" * (runs.MAX_MESSAGE + 1))
        self.assertEqual(r.status, 409)
        old = time.time() - runs.LIVE_FRESH - 5
        os.utime(os.path.join(self.live, ROOT + ".json"), (old, old))
        r, _ = self.send("x")
        self.assertEqual(r.status, 409)
        self.assertEqual(self.inbox(), [])

    def test_access_codes_gate_the_viewer_and_sending(self):
        codes = os.path.join(self.tmp.name, "codes")
        with open(codes, "w") as f:
            f.write("ann:" + pub.code_hash("letmein") + "\n")
        cfg = self.start(access_codes=codes)
        self.go_live()
        r, _ = self.req("GET", "/runs")
        self.assertEqual(r.status, 303)
        r, _ = self.req("GET", f"/runs/{ROOT}.json")
        self.assertEqual(r.status, 303)
        r, _ = self.send("x")
        self.assertEqual(r.status, 401)
        sid, _ = cfg.sessions.create("ann", pub.code_hash("letmein"))
        r, _ = self.send("from ann", cookie=f"{cfg.cookie_name}={sid}")
        self.assertEqual(r.status, 303)
        self.assertIn("-ann-", self.inbox()[0])

    def test_viewer_off_without_agent_data(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = srv.server_address[1]
        srv.RequestHandlerClass = pub.make_handler(pub.Config(self.port, "", "", 0, "", ()))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        r, _ = self.req("GET", "/runs")
        self.assertEqual(r.status, 404)
        r, body = self.req("GET", "/")
        self.assertNotIn("Agent runs", body)

    def test_follow_waits_then_opens_the_newest_session(self):
        self.start()
        later = self.now_ms + 1000
        r, body = self.req("GET", f"/runs/follow?since={later}")
        self.assertEqual(r.status, 200)
        self.assertIn("Waiting for the agent", body)
        self.assertIn(f"data-follow='/runs/follow.json?since={later}'", body)
        self.assertNotIn("data-jump=", body)
        r, body = self.req("GET", f"/runs/follow.json?since={later}")
        self.assertEqual(json.loads(body), {"sid": None})
        # ROOT started 60 s ago, the queue stage 120 s ago: since 70 s ago is ROOT alone.
        r, _ = self.req("GET", f"/runs/follow?since={self.now_ms - 70000}")
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), f"/runs/{ROOT}")
        r, body = self.req("GET", "/runs/follow.json?since=junk")
        self.assertEqual(json.loads(body)["sid"], ROOT)  # newest of all
        self.go_live(QSES)
        r, body = self.req("GET", "/runs/follow.json?since=0")
        self.assertEqual(json.loads(body)["sid"], QSES)  # a live one first

    def test_follow_a_queue_keeps_to_its_stages(self):
        self.start()
        r, _ = self.req("GET", f"/runs/follow?since={self.now_ms - 70000}&queue=1")
        self.assertEqual(r.status, 200)  # ROOT is not a queue stage
        r, _ = self.req("GET", "/runs/follow?since=0&queue=1&x=<b>")
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), f"/runs/{QSES}?since=0&queue=1")
        r, body = self.req("GET", f"/runs/{QSES}?since=0&queue=1")
        self.assertIn("data-follow='/runs/follow.json?since=0&amp;queue=1'", body)
        r, body = self.req("GET", f"/runs/{QSES}")
        self.assertNotIn("data-follow=", body)
        r, body = self.req("GET", "/runs/follow?since=99999999999999&queue=1")
        self.assertIn("Waiting for the queue", body)
        self.assertIn("data-jump='1'", body)



def git_repo(path):
    import subprocess
    os.makedirs(path)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "one"],
                ["branch", "ABC-7-work"]):
        subprocess.run(["git", "-C", path, *cmd], check=True, env=env, capture_output=True)
    return path


class QueuePageTest(RunsServer):
    """/runs/queue and the ticket's buttons, through the real scripts/ticket_queue.py."""

    def post(self, path, fields=(), origin=None):
        h = {"Content-Type": "application/x-www-form-urlencoded",
             "Origin": origin or f"http://127.0.0.1:{self.port}"}
        return self.req("POST", path, urlencode(list(fields)), h)

    def pushed(self):
        """A project whose origin has ABC-12 one commit past main (an earlier go's
        push); the ticket's clone has that branch too. (project, main's sha)."""
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")

        def git(*cmd):
            return subprocess.run(["git", *cmd], check=True, env=env, capture_output=True, text=True).stdout.strip()
        proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        remote = os.path.join(self.tmp.name, "remote.git")
        git("init", "-q", "--bare", remote)
        git("-C", proj, "remote", "add", "origin", remote)
        main = git("-C", proj, "rev-parse", "main")
        tip = git("-C", proj, "commit-tree", "main^{tree}", "-p", main, "-m", "two")
        git("-C", proj, "push", "-q", "origin", "main", f"{tip}:refs/heads/ABC-12")
        git("-C", proj, "fetch", "-q", "origin")
        clone = os.path.join(self.queue, "ABC-12", "repo")
        git("-C", clone, "init", "-q", "-b", "main")
        git("-C", clone, "fetch", "-q", remote, "ABC-12:ABC-12")
        return proj, main

    def test_retry_offers_only_the_stages_the_queue_takes(self):
        proj, main = self.pushed()
        with open(os.path.join(self.queue, "ABC-12", "plan.md"), "w") as f:
            f.write("## Goal\n\nFix it.\n")
        self.ticket(project=proj, status="done", stage="done", base_sha=main, made_branch=True, questions=[])
        self.start()
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("Run again</button>", body)
        self.assertEqual(re.findall(r"<option value='(\w*)'", body), ["implement", "review", "push"])
        self.assertIn("not from the start: ABC-12&#x27;s branch ABC-12 has commits that main does not "
                      "(origin/ABC-12 at", body)
        before = self.state()
        r, body = self.act("retry", [("stage", "plan")])   # posted anyway: the queue says no, the same way
        self.assertEqual(r.status, 409)
        self.assertIn("starts the work over from main", body)
        self.assertEqual(self.state(), before)
        r, body = self.act("retry", [("stage", "review")])
        self.assertEqual(r.status, 303, body)
        self.assertEqual((self.state()["status"], self.state()["stage"]), ("queued", "review"))
        self.assertIn("the queue is not running, so nothing happens until you start it", unquote(r.getheader("Location")))
        # its work gone too: no stage it can go again at, and the page says why
        os.remove(os.path.join(self.queue, "ABC-12", "plan.md"))
        self.ticket(project=proj, status="blocked", stage="plan", made_branch=True, questions=[],
                    reason="the branch has commits")
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("action='/runs/ticket/ABC-12/retry'", body)
        self.assertIn("Retry: ABC-12&#x27;s branch ABC-12 has commits that main does not", body)
        self.assertIn("delete it (here and on the remote, then git fetch --prune)", body)
        # the queue's module not to be had (an older checkout): every stage, as before
        with mock.patch.object(runs, "_tq", lambda: None):
            r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertEqual(re.findall(r"<option value='(\w*)'", body), ["", *runs.STAGES])

    def test_start_from_a_ticket_goes_back_to_it(self):
        self.ticket(status="queued", questions=[])
        self.start()
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("ABC-12 waits until a run takes it", body)
        self.assertIn("<input type='hidden' name='back' value='ABC-12'>", body)
        r, body = self.req("GET", "/runs/ticket/ABC-12/live")
        self.assertIn("action='/runs/queue/start'", json.loads(body)["regions"]["start"])
        r, body = self.post("/runs/queue/start", [("mode", "host"), ("back", "ABC-12")])
        self.assertEqual(r.status, 409)       # host mode needs its box ticked
        self.assertIn("<a href='/runs/ticket/ABC-12'>Back to the ticket</a>", body)
        r, body = self.post("/runs/queue/start", [("mode", "host"), ("back", "../x")])
        self.assertIn("<a href='/runs/queue'>Back to the queue</a>", body)
        self.run_on("ABC-1")
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("action='/runs/queue/start'", body)
        self.assertNotIn("waits until a run takes it", body)

    def test_queue_page_lists_tickets_with_stages_and_actions(self):
        self.ticket()
        self.start()
        r, body = self.req("GET", "/runs/queue")
        self.assertEqual(r.status, 200)
        self.assertIn("<nav class='bar'", body)
        self.assertIn("href='/runs/ticket/ABC-12'", body)
        self.assertIn("class='strip", body)
        self.assertIn("action='/runs/queue/add'", body)
        self.assertIn("action='/runs/queue/start'", body)
        self.assertIn("action='/runs/ticket/ABC-12/stop'", body)
        self.assertIn("action='/runs/ticket/ABC-12/retry'", body)
        self.assertIn("data-confirm='Remove ABC-12", body)
        self.assertIn("data-live='/runs/queue/live'", body)
        r, body = self.req("GET", "/runs/queue/live")
        d = json.loads(body)
        self.assertEqual(set(d["regions"]), {"runner", "tickets", "nav"})
        self.assertIn("ABC-12", d["regions"]["tickets"])
        r, body = self.req("GET", "/runs/ticket/ABC-12/live")
        d = json.loads(body)
        self.assertIn("Which &lt;db&gt;?", d["regions"]["qs"])
        self.assertNotIn("Which <db>?", json.dumps(d))
        r, body = self.req("GET", "/runs/ticket/ABC-99/live")
        self.assertEqual(json.loads(body), {"gone": "/runs/queue"})

    def test_the_project_folder_defaults_to_where_serve_was_started(self):
        proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        os.makedirs(os.path.join(proj, "sub"))
        self.ticket()                      # added with /p
        self.start(project_dir=os.path.join(proj, "sub"))
        r, body = self.req("GET", "/runs/queue")
        top = subprocess.run(["git", "-C", proj, "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True).stdout.strip()
        self.assertIn(f"name='project' required list='projects' spellcheck='false' value='{top}'", body)
        self.assertIn("<option value='/p'>", body)
        # not in a repository: the folder the last ticket was added with
        self.start(project_dir=self.tmp.name)
        r, body = self.req("GET", "/runs/queue")
        self.assertIn("spellcheck='false' value='/p'", body)

    def test_add_with_a_start_a_branch_and_the_defaults(self):
        proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        head = subprocess.run(["git", "-C", proj, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.start()
        r, body = self.req("GET", "/runs/queue")
        # the defaults, each choice saying what it does; the agents always work in the folder
        self.assertNotIn("type='radio' name='where'", body)
        self.assertNotIn("separate copy", body)
        self.assertIn("name='after_plan' value='wait' checked>Wait for my approval", body)
        self.assertIn("Nothing is built until you approve", body)
        self.assertNotIn("hold the plan", body)
        r, body = self.post("/runs/queue/add", [("keys", "ABC-7"), ("project", proj), ("preset", "full"),
                                                ("ref", "here"), ("branch", "feat/x")])
        self.assertEqual(r.status, 303, body)
        with open(os.path.join(self.queue, "ABC-7", "state.json")) as f:
            st = json.load(f)
        self.assertEqual((st["start"], st["branch"], st["in_place"], st["plan_review"]),
                         (head, "feat/x", True, True))
        r, body = self.req("GET", "/runs/queue")
        self.assertIn(">in your folder</span>", body)
        self.assertIn(">plan approval</span>", body)
        self.assertIn(">on feat/x</span>", body)
        r, body = self.req("GET", "/runs/ticket/ABC-7")
        self.assertIn("works <b>in your project folder</b>", body)
        self.assertIn("after the plan <b>waits for your approval</b>", body)
        r, body = self.post("/runs/queue/add", [("keys", "ABC-9"), ("project", proj), ("after_plan", "go")])
        self.assertEqual(r.status, 303, body)
        with open(os.path.join(self.queue, "ABC-9", "state.json")) as f:
            self.assertIsNone(json.load(f).get("plan_review"))
        # no plan stage: nothing waits on an approval, whatever the radio said
        r, body = self.post("/runs/queue/add", [("keys", "ABC-10"), ("project", proj), ("preset", "review"),
                                                ("start", "continue"), ("cont", "ABC-7-work")])
        self.assertEqual(r.status, 303, body)
        with open(os.path.join(self.queue, "ABC-10", "state.json")) as f:
            self.assertIsNone(json.load(f).get("plan_review"))
        info = runs.project_branches(proj)
        self.assertEqual((info["current"], info["remotes"], info["defaults"]), ("main", [], []))
        r, body = self.req("GET", "/runs/ticket/ABC-9")
        self.assertIn("after the plan <b>goes straight on</b>", body)
        r, body = self.post("/runs/queue/add", [("keys", "ABC-8"), ("project", proj), ("preset", "review"),
                                                ("branch", "a..b")])
        self.assertEqual(r.status, 409)
        self.assertIn("branch: not a branch", body)

    def test_add_starting_points_and_the_branch_list(self):
        proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        g = lambda *a: subprocess.run(["git", "-C", proj, *a], capture_output=True, text=True, check=True).stdout.strip()
        head = g("rev-parse", "HEAD")
        g("update-ref", "refs/remotes/origin/ABC-5", head)
        g("branch", "side")
        self.start()
        r, body = self.req("GET", "/runs/queue")
        self.assertIn("name='start' value='new' checked>New work", body)
        self.assertIn("<datalist id='branches'></datalist>", body)
        r, body = self.req("GET", "/runs/queue/branches?project=" + quote(proj))
        d = json.loads(body)
        self.assertEqual(d["current"], g("symbolic-ref", "--short", "HEAD"))
        self.assertIn("side", d["local"])
        self.assertEqual(d["remote"], ["origin/ABC-5"])
        self.assertIn("error", json.loads(self.req("GET", "/runs/queue/branches?project=relative")[1]))
        self.assertIn("error", json.loads(self.req("GET", "/runs/queue/branches?project=" + quote(self.tmp.name))[1]))
        # continue a remote branch: the work goes on on a branch of the same name
        r, body = self.post("/runs/queue/add", [("keys", "ABC-5"), ("project", proj), ("preset", "review"),
                                                ("start", "continue"), ("cont", "origin/ABC-5")])
        self.assertEqual(r.status, 303, body)
        with open(os.path.join(self.queue, "ABC-5", "state.json")) as f:
            st = json.load(f)
        self.assertEqual((st["start"], st["branch"]), ("origin/ABC-5", "ABC-5"))
        # continue a local branch: that branch
        r, body = self.post("/runs/queue/add", [("keys", "ABC-6"), ("project", proj), ("start", "continue"),
                                                ("cont", "side")])
        self.assertEqual(r.status, 303, body)
        with open(os.path.join(self.queue, "ABC-6", "state.json")) as f:
            self.assertEqual(json.load(f)["branch"], "side")
        # the folder as it is now
        r, body = self.post("/runs/queue/add", [("keys", "ABC-7"), ("project", proj), ("start", "here"),
                                                ("cont", "ignored")])
        self.assertEqual(r.status, 303, body)
        with open(os.path.join(self.queue, "ABC-7", "state.json")) as f:
            self.assertEqual(json.load(f)["start"], head)
        # new work from a target branch
        r, body = self.post("/runs/queue/add", [("keys", "ABC-8"), ("project", proj), ("start", "new"),
                                                ("base", "side")])
        self.assertEqual(r.status, 303, body)
        for fields, why in (
                ([("start", "continue")], "name the branch"),
                ([("start", "continue"), ("cont", "x"), ("keys", "ABC-1 ABC-2")], "one ticket"),
                ([("branch", "b"), ("keys", "ABC-1 ABC-2")], "one ticket"),
                ([("start", "sideways")], "not a starting point")):
            f = [("keys", "ABC-1"), ("project", proj)]
            f = [x for x in f if x[0] not in dict(fields)] + fields
            r, body = self.post("/runs/queue/add", f)
            self.assertEqual(r.status, 409, (fields, body[:300]))
            self.assertIn(why, body)
        # a refused continue keeps what was typed and the choice
        self.assertFalse(os.path.exists(os.path.join(self.queue, "ABC-1")))
        r, body = self.post("/runs/queue/add", [("keys", "ABC-1"), ("project", "rel"), ("start", "continue"),
                                                ("cont", "origin/ABC-5")])
        self.assertIn("name='start' value='continue' checked>", body)
        self.assertIn("placeholder='origin/ABC-123'", body)
        self.assertIn("value='origin/ABC-5'", body)

    def test_add_a_review_only_ticket_with_its_ref_and_a_note(self):
        proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        self.start()
        r, body = self.post("/runs/queue/add", [("keys", "abc-7, ABC-8"), ("project", proj), ("base", "main"),
                                                ("preset", "review"), ("ref", "ABC-7-work"),
                                                ("note", "the fix is on <ABC-7-work>")])
        self.assertEqual(r.status, 303, body)
        self.assertTrue(r.getheader("Location").startswith("/runs/queue?done="))
        with open(os.path.join(self.queue, "ABC-7", "state.json")) as f:
            st = json.load(f)
        self.assertEqual((st["stages"], st["start"], st["stage"]), (["review"], "ABC-7-work", "review"))
        r, body = self.req("GET", "/runs/ticket/ABC-7")
        self.assertIn("the fix is on &lt;ABC-7-work&gt;", body)
        self.assertNotIn("<ABC-7-work>", body)
        r, body = self.req("GET", "/runs/queue")
        self.assertLess(body.index("ABC-7"), body.index("ABC-8"))
        # notes, move, stop, retry, remove on the ticket
        r, _ = self.post("/runs/ticket/ABC-7/note", [("text", "keep the API")])
        self.assertEqual(r.status, 303)
        r, body = self.req("GET", "/runs/ticket/ABC-7")
        self.assertIn("keep the API", body)
        r, _ = self.post("/runs/ticket/ABC-8/move", [("where", "top"), ("back", "queue")])
        self.assertEqual((r.status, r.getheader("Location").split("?")[0]), (303, "/runs/queue"))
        r, body = self.req("GET", "/runs/queue")
        self.assertLess(body.index("ticket/ABC-8"), body.index("ticket/ABC-7"))
        r, _ = self.post("/runs/ticket/ABC-7/stop")
        self.assertEqual(r.status, 303)
        with open(os.path.join(self.queue, "ABC-7", "state.json")) as f:
            self.assertEqual(json.load(f)["status"], "stopped")
        r, _ = self.post("/runs/ticket/ABC-7/retry", [("stage", "push")])   # no commits: nothing to push
        self.assertEqual(r.status, 409)
        with open(os.path.join(self.queue, "ABC-7", "state.json")) as f:
            self.assertEqual(json.load(f)["status"], "stopped")
        r, _ = self.post("/runs/ticket/ABC-7/retry")
        self.assertEqual(r.status, 303)
        with open(os.path.join(self.queue, "ABC-7", "state.json")) as f:
            st = json.load(f)
        self.assertEqual((st["status"], st["stage"]), ("queued", "review"))
        r, _ = self.post("/runs/ticket/ABC-8/remove", [("back", "queue")])
        self.assertEqual(r.status, 303)
        self.assertFalse(os.path.exists(os.path.join(self.queue, "ABC-8")))

    def test_stop_retry_and_remove_end_the_container_a_gone_run_left(self):
        # a run killed outright left its agent's container: the buttons pass --harness, so it is stopped
        fake = os.path.join(self.tmp.name, "harness")
        calls = os.path.join(self.tmp.name, "calls")
        with open(fake, "w") as f:
            f.write(f'#!/bin/bash\necho "$@" >> {calls}\necho 1\n')
        agent = {"pid": 999999, "launch": "ABC-12-1-1", "host": socket.gethostname(), "container": True,
                 "workdir": "/x/repo"}
        self.start(harness=fake)
        for n, action in enumerate(("stop", "retry", "remove"), 1):
            self.ticket(status="running", stage="implement", agent=agent, questions=[])
            r, body = self.act(action)
            self.assertEqual(r.status, 303, body)
            with open(calls) as f:
                self.assertEqual(f.read().splitlines().count("queue _stop_agents /x/repo ABC-12-1-1"), n, action)
            if action != "remove":
                self.assertNotIn("agent", self.state(), action)

    def test_add_and_actions_refuse_bad_values(self):
        proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        self.ticket(status="running")
        self.run_on()
        self.start()
        good = [("keys", "ABC-9"), ("project", proj), ("preset", "full")]
        for fields, why in (
                ([("keys", "a b; <x>"), ("project", proj)], "not a ticket key"),
                ([("keys", "ABC-9"), ("project", "relative/dir")], "absolute path"),
                (good[:2] + [("base", "main..x")], "not a branch"),
                (good[:2] + [("ref", "-x")], "not a branch"),
                (good[:2] + [("preset", "custom"), ("stage_fix", "1"), ("stage_implement", "1")], "pick review"),
                (good[:2] + [("preset", "custom")], "at least one stage"),
                (good[:2] + [("preset", "<b>")], "not a preset: &lt;b&gt;"),
                # what the form greys out, refused here too
                (good[:2] + [("preset", "review"), ("start", "new")], "the work must already exist"),
                (good + [("start", "continue"), ("cont", "main")], "straight onto main; give it a work branch name"),
                (good + [("start", "continue"), ("cont", "ABC-7-work"), ("branch", "main")], "straight onto main"),
                (good + [("start", "new"), ("branch", "master")], "straight onto master"),
                (good + [("start", "continue"), ("cont", "ABC-7-work"), ("base", "ABC-7-work")],
                 "straight onto ABC-7-work"),
                (good + [("note", "x" * 4001)], "over 4000")):
            r, body = self.post("/runs/queue/add", fields)
            self.assertEqual(r.status, 409, fields)
            self.assertIn(why, body)
        self.assertFalse(os.path.exists(os.path.join(self.queue, "ABC-9")))
        # continuing the branch the folder is on, with no target: the work would go onto itself
        subprocess.run(["git", "-C", proj, "checkout", "-q", "ABC-7-work"], check=True, capture_output=True)
        runs._BRANCHES.clear()
        r, body = self.post("/runs/queue/add", good + [("start", "continue"), ("cont", "ABC-7-work")])
        self.assertEqual(r.status, 409)
        self.assertIn("empty means the branch the folder is on, ABC-7-work", body)
        # a refused add is the queue page again: the reason in the form, nothing typed lost
        r, body = self.post("/runs/queue/add", [("keys", "ABC-9"), ("project", "relative/dir"), ("base", "dev"),
                                                ("preset", "custom"), ("stage_review", "review"),
                                                ("ref", "origin/x"), ("note", "keep <me>")])
        self.assertEqual(r.status, 409)
        self.assertIn("id='add' data-err=1", body)
        self.assertIn("Not added: the project folder", body)
        for frag in ("name='keys' required autocomplete='off' spellcheck='false' value='ABC-9'",
                     "value='dev'", "value='origin/x'", ">keep &lt;me&gt;</textarea>",
                     "value='custom' checked", "value='review' checked", "value='plan'> plan"):
            self.assertIn(frag, body)
        self.assertNotIn("keep <me>", body)
        r, _ = self.post("/runs/queue/add", good, origin="http://evil.example")
        self.assertEqual(r.status, 403)
        r, body = self.post("/runs/ticket/ABC-12/remove")
        self.assertEqual(r.status, 409)       # its agent is working in the clone
        self.assertIn("stop it first", body)
        r, body = self.post("/runs/ticket/ABC-12/retry", [("stage", "deploy")])
        self.assertEqual(r.status, 409)
        r, body = self.post("/runs/ticket/ABC-12/move", [("where", "sideways")])
        self.assertEqual(r.status, 409)
        r, _ = self.post("/runs/ticket/ABC-99/stop")
        self.assertEqual(r.status, 409)
        r, body = self.post("/runs/queue/start", [("mode", "host")])
        self.assertEqual(r.status, 409)       # host mode needs its box ticked
        self.assertIn("host mode is OK", body)
        r, _ = self.post("/runs/queue/nope")
        self.assertEqual(r.status, 404)

    def nested(self):
        """A project with repositories inside it: app (a new file, one commit past its
        upstream), lib (on a branch of its own), pkgs/a/deep (no commit yet) and
        broken (a .git git cannot read); the project has one new file. The project."""
        proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        g = lambda d, *a: subprocess.run(["git", "-C", os.path.join(proj, d), *a], check=True, env=env,
                                         capture_output=True)
        git_repo(os.path.join(proj, "app"))
        g("app", "commit", "-q", "--allow-empty", "-m", "two")
        g("app", "branch", "-q", "-u", "ABC-7-work", "main")
        git_repo(os.path.join(proj, "lib"))
        g("lib", "checkout", "-q", "ABC-7-work")
        os.makedirs(os.path.join(proj, "pkgs", "a", "deep"))
        g("pkgs/a/deep", "init", "-q", "-b", "main")
        os.makedirs(os.path.join(proj, "broken"))
        for name, text in (("app/new.txt", "x"), ("top.txt", "y"), ("broken/.git", "gitdir: /nonexistent\n")):
            with open(os.path.join(proj, name), "w") as f:
                f.write(text)
        return proj

    def test_the_add_forms_tree_of_repositories(self):
        proj = self.nested()
        self.start()
        r, body = self.req("GET", "/runs/queue")
        self.assertIn("<fieldset id='f-repos' hidden><legend>Repositories</legend>", body)
        self.assertIn("id='repotree' data-sent='{}'", body)
        r, body = self.req("GET", "/runs/queue/repos?project=" + quote(proj))
        self.assertEqual(r.status, 200)
        d = json.loads(body)
        rows = {x["path"]: x for x in d["repos"]}
        self.assertEqual([x["path"] for x in d["repos"]], [".", "app", "broken", "lib", "pkgs/a/deep"])
        self.assertEqual([x["depth"] for x in d["repos"]], [0, 1, 1, 1, 3])
        # the project's new file counts, the repositories inside it do not
        self.assertEqual({k: x["dirty"] for k, x in rows.items()},
                         {".": 1, "app": 1, "broken": 0, "lib": 0, "pkgs/a/deep": 0})
        self.assertEqual({k: x["branch"] for k, x in rows.items()},
                         {".": "main", "app": "main", "broken": None, "lib": "ABC-7-work", "pkgs/a/deep": "main"})
        self.assertEqual((rows["app"]["ahead"], rows["app"]["behind"]), (1, 0))
        self.assertEqual((rows["."]["ahead"], rows["."]["behind"]), (None, None))
        self.assertRegex(rows["lib"]["head"], r"^[0-9a-f]{7,}$")
        self.assertIsNone(rows["pkgs/a/deep"]["head"])
        self.assertEqual(sorted(rows["lib"]["branches"]), ["ABC-7-work", "main"])
        self.assertTrue(rows["broken"]["err"], rows["broken"])   # git's wording for a .git it cannot read varies by version
        self.assertNotIn("err", rows["app"])
        # a folder that is not a repository's top is said so
        for p, why in (("relative", "absolute path"), (os.path.join(proj, "pkgs"), "top folder, which is " + proj),
                       (self.tmp.name, "not in a git repository")):
            self.assertIn(why, json.loads(self.req("GET", "/runs/queue/repos?project=" + quote(p))[1])["error"])

    def test_add_with_the_changes_left_uncommitted_or_on_branches(self):
        proj = self.nested()
        self.start()
        calls = []

        def queue(runs_, args, text=None):
            calls.append(args)
            return True, "added"
        good = [("keys", "ABC-7"), ("project", proj), ("preset", "full")]
        with mock.patch.object(runs, "_queue", queue):
            # the project uncommitted, each repository inside it as asked
            r, body = self.post("/runs/queue/add", good + [
                ("repo_mode:.", "uncommitted"), ("repo_mode:app", "branch"), ("repo_branch:app", "feat/a"),
                ("repo_target:app", "main"), ("repo_mode:lib", "branch"), ("repo_mode:pkgs/a/deep", "branch"),
                ("repo_target:pkgs/a/deep", "main"), ("repo_mode:broken", "uncommitted"),
                ("repo_branch:broken", "ignored")])
            self.assertEqual(r.status, 303, body)
            args = calls.pop()
            self.assertIn("--no-branch", args)
            self.assertEqual([a for a in args if a.startswith("--repo-branch")],
                             ["--repo-branch=app=feat/a:main", "--repo-branch=lib", "--repo-branch=pkgs/a/deep=:main"])
            self.assertFalse([a for a in args if a.startswith(("--branch", "--base", "--start", "--clone",
                                                               "--in-place"))], args)
            r, body = self.post("/runs/queue/add", good + [("repo_mode:.", "branch"), ("repo_mode:lib", "branch"),
                                                           ("repo_branch:lib", "lib-work"), ("branch", "feat/x")])
            self.assertEqual(r.status, 303, body)
            args = calls.pop()
            self.assertIn("--repo-branch=lib=lib-work", args)
            self.assertIn("--branch=feat/x", args)
            self.assertNotIn("--no-branch", args)
            # no tree sent (it did not load): on a branch, as before
            r, body = self.post("/runs/queue/add", good)
            self.assertEqual(r.status, 303, body)
            self.assertFalse([a for a in calls.pop() if a in ("--no-branch", "--clone", "--in-place")])
            un = [("repo_mode:.", "uncommitted")]
            for fields, why in (
                    (un + [("base", "main")], "left uncommitted, so there is no starting point"),
                    (un + [("start", "here")], "left uncommitted"),
                    (un + [("branch", "feat/x")], "left uncommitted"),
                    ([("repo_mode:.", "sideways")], "not a choice: sideways"),
                    ([("repo_mode:app", "both")], "app: not a choice"),
                    ([("repo_mode:../x", "branch")], "not a repository inside the project: ../x"),
                    ([("repo_mode:/etc", "branch")], "not a repository inside the project: /etc"),
                    ([("repo_mode:app/../lib", "branch")], "not a repository inside the project"),
                    ([("repo_mode:pkgs", "branch")], "not a repository inside the project: pkgs"),
                    ([("repo_mode:a=b", "branch")], "not a repository inside the project"),
                    ([("repo_mode:app", "branch"), ("repo_branch:app", "-x")], "app: branch: not a branch name"),
                    ([("repo_mode:app", "branch"), ("repo_branch:app", "a b")], "not a branch name"),
                    ([("repo_mode:app", "branch"), ("repo_branch:app", "a:b")], "not a branch name"),
                    ([("repo_mode:app", "branch"), ("repo_target:app", "x=y")], "app: target branch: not a branch"),
                    ([("repo_mode:app", "branch"), ("repo_branch:app", "b"), ("keys", "ABC-1 ABC-2")],
                     "a branch name is for one ticket")):
                f = [x for x in good if x[0] not in dict(fields)] + fields
                r, body = self.post("/runs/queue/add", f)
                self.assertEqual(r.status, 409, (fields, body[:300]))
                self.assertIn(why, body)
            self.assertEqual(calls, [])
            # a refused add keeps the tree's choices for the form to put back
            self.assertIn(runs.esc(json.dumps({"repo_mode:app": "branch", "repo_branch:app": "b"})), body)

    def test_project_path_as_pasted(self):
        self.assertEqual(runs.project_path('  "/home/me/p"  '), "/home/me/p")
        self.assertEqual(runs.project_path("'/home/me/p'"), "/home/me/p")
        self.assertEqual(runs.project_path("~/p"), os.path.expanduser("~/p"))
        self.assertEqual(runs.project_path("/c/Users/me/p", nt=True), "C:/Users/me/p")
        self.assertEqual(runs.project_path("/d", nt=True), "D:/")
        self.assertEqual(runs.project_path("/c/Users/me/p", nt=False), "/c/Users/me/p")
        self.assertEqual(runs.project_path("/home/me", nt=True), "/home/me")
        self.assertEqual(runs.project_path('"C:\\Users\\me\\p"', nt=True), "C:\\Users\\me\\p")

    def test_runner_status_from_run_json_and_halt(self):
        self.ticket(status="running")
        self.start()
        with open(os.path.join(self.queue, "run.json"), "w") as f:
            json.dump({"pid": 1, "started": "2026-10-27T10:00:00Z", "beat": time.time(), "host": True,
                       "single": False, "model": "big <m>", "ticket": "ABC-12", "stage": "plan"}, f)
        r, body = self.req("GET", "/runs/queue")
        self.assertIn("big &lt;m&gt;", body)
        self.assertIn("action='/runs/queue/halt'", body)
        self.assertNotIn("action='/runs/queue/start'", body)
        import fcntl
        with open(os.path.join(self.queue, "run.lock"), "a") as lock:   # a run holds it while it goes
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r, _ = self.post("/runs/queue/halt")
        self.assertEqual(r.status, 303)
        self.assertTrue(os.path.exists(os.path.join(self.queue, "halt")))
        r, body = self.req("GET", "/runs/queue")
        self.assertIn("halting", body)
        self.assertIn("action='/runs/queue/end'", body)   # for a run that does not halt
        # a stop the run has not acted on for a while: the ticket offers to end the run too
        stop = os.path.join(self.queue, "ABC-12", "stop")
        with open(stop, "w") as f:
            f.write("x\n")
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("stopping: the run stops its agent within seconds", body)
        self.assertNotIn("/runs/queue/end'", body)
        os.utime(stop, (time.time() - runs.STUCK_AFTER - 5,) * 2)
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("still stopping", body)
        self.assertIn("action='/runs/queue/end'", body)
        with open(os.path.join(self.queue, "run.json"), "w") as f:
            json.dump({"beat": time.time() - runs.RUN_FRESH - 5}, f)
        r, body = self.req("GET", "/runs/queue")
        self.assertIn("action='/runs/queue/start'", body)   # a stale beat is a run that is gone
        # End the run with no run left: the ticket it left running is put back, the stop kept
        r, body = self.post("/runs/queue/end")
        self.assertEqual(r.status, 303, body)
        self.assertEqual(self.state()["status"], "stopped")
        for f in ("halt", "run.json", stop):
            self.assertFalse(os.path.exists(os.path.join(self.queue, f)), f)

    def test_start_runs_harness_queue_run_and_reports_an_early_exit(self):
        fake = os.path.join(self.tmp.name, "harness")
        args = os.path.join(self.tmp.name, "args")
        with open(fake, "w") as f:
            f.write(f'#!/bin/bash\necho "$@ HOST_CONFIRM=$HARNESS_HOST_CONFIRM OPEN=$HARNESS_OPEN" > {args}\n'
                    'echo "no active vault <x>"\nexit 3\n')
        self.ticket()
        cfg = self.start(harness=fake)
        r, body = self.post("/runs/queue/start", [("mode", "single"), ("confirm_host", "1")])
        self.assertEqual(r.status, 409)
        self.assertIn("ended at once (status 3): no active vault &lt;x&gt;", body)
        with open(args) as f:
            self.assertEqual(f.read().strip(), "queue run --single-message HOST_CONFIRM=1 OPEN=0")
        r, body = self.req("GET", "/runs/queue/runner.out")
        self.assertIn("no active vault <x>", body)
        self.assertEqual(r.getheader("Content-Type").split(";")[0], "text/plain")
        # started from a ticket's page: back to the ticket, which shows the run starting
        with open(fake, "w") as f:
            f.write("#!/bin/bash\nexec sleep 30\n")
        self.ticket(status="queued", questions=[])
        r, body = self.post("/runs/queue/start", [("mode", "container"), ("back", "ABC-12")])
        try:
            self.assertEqual(r.status, 303, body)
            self.assertTrue(r.getheader("Location").startswith("/runs/ticket/ABC-12?done="))
            r, body = self.req("GET", "/runs/ticket/ABC-12")
            self.assertIn("Queue starting", body)
            self.assertNotIn("action='/runs/queue/start'", body)
        finally:
            if cfg.runs.launched:
                cfg.runs.launched[0].kill()
                cfg.runs.launched[0].wait()

    def test_a_queue_session_has_a_stop_button_and_the_host_an_opencode_link(self):
        self.ticket(status="running", stage="plan")
        self.run_on()
        cfg = self.start()
        cfg.opencode_port = 4096
        cfg.opencode_password = lambda: "p w"
        self.go_live(QSES)
        r, body = self.req("GET", f"/runs/{QSES}")
        self.assertIn(f"action='/runs/{QSES}/stop'", body)
        self.assertIn("http://opencode:p%20w@127.0.0.1:4096/server/aHR0cDovLzEyNy4wLjAuMTo0MDk2/session/"
                      f"{QSES}", body)
        r, _ = self.post(f"/runs/{QSES}/stop")
        self.assertEqual(r.status, 303)
        self.assertEqual(self.state()["status"], "stopped")
        self.ticket(status="stopped", stage="plan", base_sha="abc")   # its work there: Continue, from here too
        r, body = self.req("GET", f"/runs/{QSES}")
        self.assertIn("href='/runs/ticket/ABC-12#go-on'", body)
        self.assertIn("Continue plan</a>", body)
        r, body = self.post(f"/runs/{ROOT}/stop")
        self.assertEqual(r.status, 409)     # not a queue stage
        self.assertIn("not a queue stage", body)

    def test_access_codes_gate_the_queue_forms(self):
        codes = os.path.join(self.tmp.name, "codes")
        with open(codes, "w") as f:
            f.write("ann:" + pub.code_hash("letmein") + "\n")
        self.ticket()
        self.start(access_codes=codes)
        for path in ("/runs/queue", "/runs/queue/live", "/runs/ticket/ABC-12/live", "/runs/queue/runner.out"):
            r, _ = self.req("GET", path)
            self.assertEqual(r.status, 303, path)
        for path in ("/runs/queue/halt", "/runs/ticket/ABC-12/stop", f"/runs/{QSES}/stop"):
            r, _ = self.post(path)
            self.assertEqual(r.status, 401, path)
        self.assertEqual(self.state()["status"], "needs-answers")


class DiffPageTest(RunsServer):
    """/runs/ticket/KEY/diff: the repository picked, its diff drawn by diff2html."""

    def git(self, d, *a):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", d, *a], check=True, env=env, capture_output=True, text=True).stdout.strip()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def setUp(self):
        super().setUp()
        proj = self.proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        self.write(os.path.join(proj, "a.txt"), "one\n")
        self.git(proj, "add", "a.txt")
        self.git(proj, "commit", "-qm", "a")
        self.base = self.git(proj, "rev-parse", "HEAD")
        self.git(proj, "checkout", "-qb", "ABC-12")
        self.write(os.path.join(proj, "a.txt"), "one\nwork <b>line</b>\n")
        self.write(os.path.join(proj, ".env"), "TOKEN=nope\n")
        self.git(proj, "add", "a.txt", ".env")
        self.git(proj, "commit", "-qm", "the ticket's work")
        self.git(proj, "checkout", "-q", "main")
        self.write(os.path.join(proj, "a.txt"), "one\nnot committed\n")
        self.write(os.path.join(proj, "new.txt"), "hello\n")
        self.write(os.path.join(proj, ".env.local"), "TOKEN=nope\n")
        inner = git_repo(os.path.join(proj, "libs", "inner"))
        self.write(os.path.join(inner, "x.txt"), "inner change\n")
        sub = git_repo(os.path.join(proj, "sub"))   # a gitlink in the project's index: a submodule, not listed
        self.git(proj, "add", "sub")
        self.git(proj, "commit", "-qm", "sub")
        self.assertTrue(os.path.isdir(sub))
        self.ticket(project=proj, in_place=True, base_sha=self.base, status="done", questions=[])
        self.start()

    def shown(self, body):
        m = re.search(r"<script type='application/json' id='dv-data'>(.*?)</script>", body, re.S)
        return json.loads(m.group(1)) if m else ""

    def test_the_tickets_work_and_the_repository_pick(self):
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("href='/runs/ticket/ABC-12/diff'>Changes</a>", body)
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff")
        self.assertEqual(r.status, 200, body)
        self.assertIn("<option value='libs/inner'>libs/inner</option>", body)
        self.assertNotIn("value='sub'", body)
        self.assertIn("name='view' value='work' checked>", body)
        self.assertIn("1 commit(s)", body)
        self.assertIn("Diff2Html", body)
        self.assertIn(".d2h-wrapper", body)
        diff = self.shown(body)
        self.assertIn("+work <b>line</b>", diff)
        self.assertNotIn("<b>", body.split("id='dv-data'>", 1)[1].split("</script>", 1)[0])
        self.assertNotIn("TOKEN", diff)
        self.assertIn("left out because they look like secrets: .env", body)
        self.assertNotIn("not committed", diff)

    def test_uncommitted_new_files_and_a_nested_repository(self):
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff?view=uncommitted")
        diff = self.shown(body)
        self.assertIn("+not committed", diff)
        self.assertIn("+++ b/new.txt\n@@ -0,0 +1,1 @@\n+hello", diff)
        self.assertNotIn(".env.local", diff)
        self.assertNotIn("+work", diff)
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff?repo=libs/inner&view=work")
        self.assertIn("value='libs/inner' selected", body)
        self.assertIn("name='view' value='work' disabled>", body)   # only the project has the ticket's branch
        self.assertIn("name='view' value='uncommitted' checked>", body)
        self.assertIn("+inner change", self.shown(body))
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff?repo=libs/inner&view=unpushed")
        self.assertIn("has no upstream", body)
        self.assertEqual(self.shown(body), "")

    def test_refusals_caps_and_a_copy(self):
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff?repo=../../etc")
        self.assertIn("not a repository of this ticket: ../../etc; showing the project", body)
        self.assertIn("+work", self.shown(body))
        r, _ = self.req("GET", "/runs/ticket/ABC-99/diff")
        self.assertEqual(r.status, 404)
        cap = diff_view.DIFF_CAP
        self.addCleanup(setattr, diff_view, "DIFF_CAP", cap)
        diff_view.DIFF_CAP = 100
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff?view=uncommitted")
        self.assertIn("too big to show in full", body)
        self.assertLessEqual(len(self.shown(body)), 100)
        diff_view.DIFF_CAP = cap
        self.ticket(project=self.proj, base_sha=self.base, status="done", questions=[])   # a copy, now gone
        self.git(self.proj, "update-ref", "refs/harness-queue/ABC-12", "ABC-12")
        shutil.rmtree(os.path.join(self.queue, "ABC-12", "repo"))
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff")
        self.assertNotIn("libs/inner", body)
        self.assertIn("holds only the project's own repository", body)
        self.assertIn("+work", self.shown(body))   # what push fetched into the project
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff?view=uncommitted")
        self.assertIn("the separate copy is gone", body)


class BackgroundTest(unittest.TestCase):
    def test_start_reuses_a_running_page_and_stop_ends_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            pid_file, log = os.path.join(tmp, "viewer.pid"), os.path.join(tmp, "viewer.log")
            start = ["start", "--pid-file", pid_file, "--log", log, "--port", str(port)]
            self.assertEqual(pub.main(start), 0)
            self.assertTrue(pub.pub_answers(port))
            pid = open(pid_file).read().split()[0]
            self.assertEqual(pub.main(start), 0)  # already up: left as it is
            self.assertEqual(open(pid_file).read().split()[0], pid)
            self.assertEqual(pub.main(["stop", "--pid-file", pid_file]), 0)
            self.assertFalse(os.path.exists(pid_file))
            for _ in range(50):
                if not pub.pub_answers(port):
                    break
                time.sleep(0.1)
            self.assertFalse(pub.pub_answers(port))
            self.assertEqual(pub.main(["stop", "--pid-file", pid_file]), 0)  # nothing to stop

    # A page on the port answering /healthz as one whose code changed on disk.
    OLD_VIEWER = (
        "import sys, http.server\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.send_header('X-Harness-Stale', '1')\n"
        "        self.send_header('X-Harness-State', sys.argv[2]); self.end_headers(); self.wfile.write(b'ok\\n')\n"
        "    def log_message(self, *a): pass\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n")

    def old_viewer(self, port, state=""):
        proc = subprocess.Popen([sys.executable, "-c", self.OLD_VIEWER, str(port), state])
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        for _ in range(50):
            if pub.pub_answers(port):
                return proc
            time.sleep(0.1)
        self.fail("the old viewer did not come up")

    def test_start_replaces_its_viewer_on_old_code_and_leaves_another_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            pid_file, log = os.path.join(tmp, "viewer.pid"), os.path.join(tmp, "viewer.log")
            start = ["start", "--pid-file", pid_file, "--log", log, "--port", str(port), "--state-dir", tmp]
            # Not the viewer the pid file names (a harness serve in a terminal): left up, and said.
            old = self.old_viewer(port)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(pub.main(start), 0)
            self.assertIn("run harness serve again", err.getvalue())
            self.assertIsNone(old.poll())
            self.assertFalse(os.path.exists(pid_file))
            # The viewer a launch left up (harness upgrade since): ended, a new one on the new code.
            with open(pid_file, "w") as f:
                f.write(f"{old.pid} {port}\n")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(pub.main(start), 0)
            try:
                self.assertIsNotNone(old.wait(10))
                self.assertEqual(pub.pub_health(port), (False, pub.state_id(tmp)))
                with open(pid_file) as f:
                    self.assertNotEqual(f.read().split()[0], str(old.pid))
            finally:
                pub.main(["stop", "--pid-file", pid_file])

    def test_start_says_when_the_port_has_another_installs_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            # a current page (not stale) of another state folder
            srv = ThreadingHTTPServer(("127.0.0.1", port), None)
            srv.RequestHandlerClass = pub.make_handler(pub.Config(port, "", "", 0, "", (), state_dir=os.path.join(tmp, "other")))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(pub.main(["start", "--pid-file", os.path.join(tmp, "p"), "--port", str(port),
                                           "--state-dir", tmp]), 0)
            self.assertIn("another harness install", err.getvalue())


if __name__ == "__main__":
    unittest.main()
