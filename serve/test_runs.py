"""Tests for the pub page's run viewer (serve/runs.py, /runs) against a live server, over
an opencode database built with opencode 1.19.3's own session/message/part tables.

Run: python3 -m unittest serve/test_runs.py (tests/unit_serve_test.sh does).
"""

import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pub  # noqa: E402
import runs  # noqa: E402

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
        self.assertIn("<span class='live'>live</span>", body)
        self.assertIn("href='/runs/ticket/ABC-12'", body)
        self.assertRegex(body, r"<code>plan-1</code> <a href='/runs/ses_queue1'>")
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
        self.assertRegex(body, r"<b>ABC-12</b></a> <span class='ask'>needs your answers</span>")
        self.assertIn("does not hold the queue", body)

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
        self.assertIn("<span class='failed'>failed</span> <code>plan</code> attempt 2", body)
        self.assertIn("write it to &lt;C:\\x&gt;", body)
        self.assertIn("href='/runs/ticket/ABC-12/plan-1.out'", body)   # the failed attempt's output
        self.assertIn("1. [blocking] Which &lt;db&gt;?", body)          # the plan note
        self.ticket(status="plan-review", questions=[])
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertIn("action='/runs/ticket/ABC-12/approve'", body)
        self.assertIn("Answer the planner", body)   # a free answer box without questions
        self.ticket(status="running", attempt=1)
        r, body = self.req("GET", "/runs/ticket/ABC-12")
        self.assertNotIn("<form method='post'", body)
        self.assertIn("going with the default", body)

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
        with open(os.path.join(self.vault, "wiki", "notes", "tickets", "ABC-12-answers.md")) as f:
            text = f.read()
        self.assertIn("## Which <db>?\n\npostgres", text)
        self.assertIn("## Other\n\nkeep it small", text)
        r, body = self.req("GET", r.getheader("Location"))
        self.assertIn("<p class='flash'>", body)
        r, body = self.act("answer", [("defaults", "1")])
        self.assertEqual(r.status, 409)   # sent twice: the ticket is not waiting any more
        self.assertIn("ABC-12 is queued now", body)
        r, body = self.act("approve")
        self.assertEqual(r.status, 409)
        self.assertIn("not plan-review", body)

    def test_approve_and_retry_on_the_page(self):
        self.ticket(status="plan-review", stage="implement")
        self.start()
        r, _ = self.act("approve")
        self.assertEqual(r.status, 303)
        self.assertEqual((self.state()["status"], self.state()["plan_approved"]), ("queued", True))
        self.ticket(status="blocked", stage="review", reason="review failed 2 time(s)")
        r, _ = self.act("retry")
        self.assertEqual(r.status, 303)
        self.assertEqual((self.state()["status"], self.state()["stage"]), ("queued", "review"))
        r, _ = self.act("delete")
        self.assertEqual(r.status, 404)
        r, _ = self.req("POST", "/runs/ticket/ABC-12/retry", "", {"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)

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


if __name__ == "__main__":
    unittest.main()
