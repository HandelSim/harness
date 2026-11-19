"""Tests for serve/api.py, the desktop app's JSON API (/api/v1), against a live pub server
over an opencode database (test_runs.build_db) and a queue folder built in a temp dir.

Run: python3 -m unittest serve/test_api.py (tests/unit_serve_test.sh does).
     python3 serve/test_api.py --dump DIR  writes every read endpoint's and poll topic's
     answer on the fixture as DIR/<name>.json (the desktop app's mock serve replays them).
"""

import http.client
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import quote, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pub  # noqa: E402
import runs  # noqa: E402
import api  # noqa: E402
import settings_view  # noqa: E402
from test_runs import build_db, ROOT, CHILD, QSES, PLAN_ITEMS  # noqa: E402

STATUSES = ("queued", "running", "interrupted", "needs-answers", "plan-review", "blocked", "stopped", "done")
TOPICS6 = ("status", "queue", "sessions", "home", "ticket:ABC-12", "session:" + ROOT)


class Fixture:
    """A queue (ABC-12 and friends), an opencode db, a vault, and a pub server on 127.0.0.1."""

    def __init__(self, root, **kw):
        self.root = root
        self.data = os.path.join(root, "data")
        os.makedirs(os.path.join(self.data, "opencode"))
        self.live = os.path.join(self.data, "harness", "live")
        os.makedirs(self.live)
        self.state = os.path.join(root, "state")
        self.queue = os.path.join(self.state, "queue")
        repo = os.path.join(self.queue, "ABC-12", "repo")
        os.makedirs(repo)
        with open(os.path.join(self.queue, "ABC-12", "log.md"), "w") as f:
            f.write("12:00 plan-1 started\n12:01 verifier: pass\n")
        with open(os.path.join(self.queue, "ABC-12", "plan-1.out"), "w") as f:
            f.write("the plan\n")
        self.vault = os.path.join(root, "vault")
        os.makedirs(os.path.join(self.vault, "wiki"))
        with open(os.path.join(self.vault, "wiki", "index.md"), "w") as f:
            f.write("# Index\n")
        self.now_ms = int(time.time() * 1000)
        build_db(os.path.join(self.data, "opencode", "opencode.db"), os.path.join(root, "proj"), repo, self.now_ms)
        self.ticket()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.srv.server_address[1]
        kw.setdefault("agent_data", [("host", self.data)])
        kw.setdefault("queue_dir", self.queue)
        self.cfg = pub.Config(self.port, self.vault, "notes", kw.pop("opencode_port", 0), "", (), **kw)
        self.srv.RequestHandlerClass = pub.make_handler(self.cfg)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def ticket(self, key="ABC-12", listed=True, **st):
        base = {"key": key, "project": "/p", "base": "main", "status": "needs-answers", "stage": "plan",
                "attempt": 0, "feedback": "", "reason": "1 blocking question(s) in the plan", "title": "Fix it",
                "questions": [{"text": "Which db?", "blocking": True},
                              {"text": "Log level? Default: info", "blocking": False}],
                "history": [{"at": "2026-10-26T20:00:00Z", "stage": "plan", "attempt": 2, "result": "failed",
                             "details": ["the plan file was not written"]}]}
        base.update(st)
        os.makedirs(os.path.join(self.queue, key), exist_ok=True)
        with open(os.path.join(self.queue, key, "state.json"), "w") as f:
            json.dump(base, f)
        if listed:
            path = os.path.join(self.queue, "queue.list")
            have = []
            if os.path.exists(path):
                with open(path) as f:
                    have = f.read().split()
            if key not in have:
                with open(path, "w") as f:
                    f.write("\n".join(have + [key]) + "\n")

    def run_on(self, key="ABC-12", **info):
        """A queue run going (a fresh heartbeat), on key."""
        info.update(beat=time.time(), ticket=key)
        with open(os.path.join(self.queue, "run.json"), "w") as f:
            json.dump(info, f)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def get(self, path, headers=None):
        r, data = self.req("GET", "/api/v1" + path, headers=headers)
        return r, json.loads(data.decode()) if data else None

    def post(self, path, obj, headers=None, raw=None):
        h = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        h.update(headers or {})
        body = raw if raw is not None else json.dumps(obj).encode()
        r, data = self.req("POST", "/api/v1" + path, body, h)
        return r, json.loads(data.decode()) if data else None

    def wait_pending(self):
        for _ in range(200):
            if not self.cfg.runs._pending:
                return
            time.sleep(0.05)


class ApiTest(unittest.TestCase):
    kw = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self.tmp.name, **dict(self.kw))

    def tearDown(self):
        self.fx.close()
        self.tmp.cleanup()

    def ok(self, path):
        r, d = self.fx.get(path)
        self.assertEqual(r.status, 200, d)
        self.assertTrue(d["ok"], d)
        return d["data"]


class EnvelopeTest(ApiTest):
    def test_status_envelope_headers_and_sig(self):
        r, d = self.fx.get("/status")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(r.getheader("X-Harness-Api"), "1")
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(d["sig"], api.sig_of(d["data"]))
        s = d["data"]
        self.assertEqual(s["api"], 1)
        self.assertEqual(s["mode"], "viewer")
        self.assertTrue(s["runs"])
        self.assertIsNone(s["opencode"])
        self.assertEqual(s["vault"], {"name": "notes", "dir": self.fx.vault})
        for k in ("pid", "state_id", "stale", "root", "state_dir", "codes", "user", "proxy", "modes", "version"):
            self.assertIn(k, s)
        r, d2 = self.fx.get("/status?sig=" + d["sig"])
        self.assertEqual(d2, {"ok": True, "sig": d["sig"], "same": True})
        r, d3 = self.fx.get("/status?sig=nope")
        self.assertIn("data", d3)

    def test_head_is_get_without_body(self):
        r, data = self.fx.req("HEAD", "/api/v1/status")
        self.assertEqual(r.status, 200)
        self.assertEqual(data, b"")
        self.assertEqual(r.getheader("X-Harness-Api"), "1")

    def test_unknown_and_wrong_method(self):
        r, d = self.fx.get("/nope")
        self.assertEqual((r.status, d["error"]["code"]), (404, "not-found"))
        r, d = self.fx.get("/../status")
        self.assertEqual(r.status, 404)
        r, d = self.fx.req("GET", "/api/v2/status")
        self.assertEqual(r.status, 404)
        r, d = self.fx.get("/sessions/%s/send" % ROOT)
        self.assertEqual((r.status, d["error"]["code"]), (405, "method"))
        r, d = self.fx.post("/status", {})
        self.assertEqual((r.status, d["error"]["code"]), (405, "method"))

    def test_stale_header(self):
        with mock.patch.object(pub.Config, "code_changed", return_value=True):
            r, d = self.fx.get("/status")
            self.assertEqual(r.getheader("X-Harness-Stale"), "1")
            self.assertTrue(d["data"]["stale"])

    def test_internal_error_is_json_500(self):
        with mock.patch.object(api, "_git_version", side_effect=RuntimeError("boom")), \
                mock.patch.dict(api._CACHE, clear=True), mock.patch("sys.stderr"):
            r, d = self.fx.get("/status")
        self.assertEqual(r.status, 500)
        self.assertEqual(d["error"]["code"], "internal")
        self.assertIn("RuntimeError", d["error"]["message"])

    def test_existing_routes_unchanged(self):
        r, data = self.fx.req("GET", "/healthz")
        self.assertEqual((r.status, data), (200, b"ok\n"))
        r, _ = self.fx.req("GET", "/runs")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))


class PollTest(ApiTest):
    def test_poll_status_and_queue(self):
        r, d = self.fx.get("/poll?t=status&t=queue")
        self.assertEqual(r.status, 200)
        self.assertTrue(d["ok"])
        self.assertIsInstance(d["now"], int)
        self.assertIn("stale", d)
        q = d["topics"]["queue"]
        self.assertEqual(q["sig"], api.sig_of(q["data"]))
        self.assertEqual([t["key"] for t in q["data"]["tickets"]], ["ABC-12"])
        # the same sig through GET and poll (one implementation)
        _, g = self.fx.get("/queue")
        self.assertEqual(g["sig"], q["sig"])
        r, d2 = self.fx.get("/poll?" + urlencode([("t", "queue~" + q["sig"]), ("t", "status~stale")]))
        self.assertEqual(d2["topics"]["queue"], {"sig": q["sig"], "same": True})
        self.assertIn("data", d2["topics"]["status"])

    def test_queue_sig_is_stable(self):
        self.fx.get("/poll?t=queue&t=ticket:ABC-12")
        self.fx.wait_pending()   # the retry choices are worked out in the background (git)
        _, a = self.fx.get("/poll?t=queue&t=ticket:ABC-12")
        time.sleep(1.1)
        _, b = self.fx.get("/poll?t=queue&t=ticket:ABC-12")
        self.assertEqual(a["topics"]["queue"]["sig"], b["topics"]["queue"]["sig"])
        self.assertEqual(a["topics"]["ticket:ABC-12"]["sig"], b["topics"]["ticket:ABC-12"]["sig"])

    def test_bad_topics(self):
        for q in ("t=Status", "t=queue:a/b", "t=" + quote("x;y"), "&".join(["t=status"] * 25)):
            r, d = self.fx.get("/poll?" + q)
            self.assertEqual((r.status, d["error"]["code"]), (400, "bad-request"), q)

    def test_one_failing_topic_never_fails_the_others(self):
        r, d = self.fx.get("/poll?t=nope&t=status&t=ticket&t=ticket:NOPE-1&t=session:ses_nope")
        self.assertEqual(r.status, 200)
        t = d["topics"]
        self.assertEqual(t["nope"]["error"]["code"], "bad-request")
        self.assertEqual(t["ticket"]["error"]["code"], "bad-request")
        self.assertIn("data", t["status"])
        self.assertEqual(t["ticket:NOPE-1"]["data"], {"gone": True})
        self.assertEqual(t["session:ses_nope"]["data"], {"gone": True})
        with mock.patch.dict(api.READS, {"home": mock.Mock(side_effect=KeyError("x"))}), mock.patch("sys.stderr"):
            r, d = self.fx.get("/poll?t=home&t=status")
        self.assertEqual(r.status, 200)
        self.assertEqual(d["topics"]["home"]["error"]["code"], "internal")
        self.assertIn("data", d["topics"]["status"])

    def test_parallel_polls(self):
        got, lock = [], threading.Lock()

        def one():
            r, _ = self.fx.get("/poll?" + "&".join("t=" + t for t in TOPICS6))
            with lock:
                got.append(r.status)
        threads = [threading.Thread(target=one) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(got, [200] * 20)

    def test_perf_six_topics_fifty_tickets(self):
        for i in range(50):
            self.fx.ticket(f"PERF-{i}", status=STATUSES[i % len(STATUSES)], stage="implement")
        q = "/poll?" + "&".join("t=" + t for t in TOPICS6)
        self.fx.get(q)
        self.fx.wait_pending()
        self.fx.get(q)
        times = []
        for _ in range(7):
            t = time.perf_counter()
            r, d = self.fx.get(q)
            times.append(time.perf_counter() - t)
            self.assertEqual(r.status, 200)
        med = statistics.median(times)
        sys.stderr.write(f"\n[test_api] 6 topics on 51 tickets: median {med * 1000:.1f} ms\n")
        self.assertLess(med, 0.5)


class AuthTest(ApiTest):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.codes = os.path.join(self.tmp.name, "codes")
        open(self.codes, "w").close()
        self.code = pub.add_code(self.codes, "alice")
        os.makedirs(os.path.join(self.tmp.name, "fx"))
        self.fx = Fixture(os.path.join(self.tmp.name, "fx"), access_codes=self.codes)

    def test_codes_on_401_then_200_after_login(self):
        r, d = self.fx.get("/status")
        self.assertEqual((r.status, d["error"]["code"]), (401, "auth"))
        self.assertEqual(r.getheader("Content-Type"), "application/json; charset=utf-8")
        r, d = self.fx.get("/poll?t=status")
        self.assertEqual(r.status, 401)
        r, d = self.fx.post("/queue/halt", {})
        self.assertEqual((r.status, d["error"]["code"]), (401, "auth"))
        r, _ = self.fx.req("POST", "/login", urlencode({"code": self.code}),
                           {"Origin": f"http://127.0.0.1:{self.fx.port}",
                            "Content-Type": "application/x-www-form-urlencoded"})
        cookie = [c for c in r.msg.get_all("Set-Cookie") if c.startswith("harness_pub=")][0].split(";")[0]
        r, d = self.fx.get("/status", {"Cookie": cookie})
        self.assertEqual(r.status, 200)
        self.assertEqual(d["data"]["user"], "alice")
        self.assertTrue(d["data"]["codes"])
        with mock.patch.object(settings_view, "gather", return_value={}), \
                mock.patch.object(settings_view, "system_info", return_value={}):
            r, d = self.fx.get("/settings", {"Cookie": cookie})
        self.assertEqual(d["data"]["access"], {"enabled": True, "names": ["alice"], "me": "alice"})
        r, d = self.fx.post("/settings/access", {"act": "add", "name": "bob"}, {"Cookie": cookie})
        self.assertEqual(r.status, 200, d)
        self.assertEqual(d["data"]["name"], "bob")
        self.assertTrue(self.fx.cfg.codes.match(d["data"]["code"]))
        r, d = self.fx.post("/settings/access", {"act": "remove", "name": "alice"}, {"Cookie": cookie})
        self.assertEqual((r.status, d["error"]["code"]), (409, "refused"))
        r, d = self.fx.post("/settings/access", {"act": "remove", "name": "bob"}, {"Cookie": cookie})
        self.assertEqual(r.status, 200, d)


class CsrfTest(ApiTest):
    def test_post_checks(self):
        r, d = self.fx.post("/queue/halt", {}, {"Origin": "http://evil.example"})
        self.assertEqual((r.status, d["error"]["code"]), (403, "origin"))
        r, d = self.fx.post("/queue/halt", {}, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((r.status, d["error"]["code"]), (415, "content-type"))
        r, d = self.fx.post("/queue/halt", {}, {"Content-Type": "text/plain"})
        self.assertEqual(r.status, 415)
        r, d = self.fx.post("/sessions/%s/send" % ROOT, {"text": "x" * (runs.MAX_MESSAGE * 4)})
        self.assertEqual((r.status, d["error"]["code"]), (413, "too-large"))
        r, d = self.fx.post("/queue/halt", None, raw=b"[1, 2]")
        self.assertEqual((r.status, d["error"]["code"]), (400, "bad-request"))
        r, d = self.fx.post("/queue/halt", None, raw=b"{nope")
        self.assertEqual((r.status, d["error"]["code"]), (400, "bad-request"))
        r, d = self.fx.post("/tickets/ABC-12/note", {"text": {"deep": 1}})
        self.assertEqual(r.status, 400)

    def test_wrong_host_refused(self):
        r, _ = self.fx.req("GET", "/api/v1/status", headers={"Host": "evil.example"})
        self.assertNotEqual(r.status, 200)


class NoRunsTest(ApiTest):
    kw = {"agent_data": ()}

    def test_runs_none_is_503(self):
        for path in ("/queue", "/sessions", "/tickets/ABC-12", "/findings"):
            r, d = self.fx.get(path)
            self.assertEqual((r.status, d["error"]["code"]), (503, "unavailable"), path)
        r, d = self.fx.post("/queue/halt", {})
        self.assertEqual(r.status, 503)
        s = self.ok("/status")
        self.assertFalse(s["runs"])
        r, d = self.fx.get("/poll?t=queue&t=status")
        self.assertEqual(d["topics"]["queue"]["error"]["code"], "unavailable")
        self.assertIn("data", d["topics"]["status"])


class TicketTest(ApiTest):
    def gates(self, **st):
        if os.path.exists(os.path.join(self.fx.queue, "run.json")):
            os.remove(os.path.join(self.fx.queue, "run.json"))
        if st.get("status") == "running":
            self.fx.run_on()
        self.fx.ticket(**st)
        t = self.ok("/tickets/ABC-12")
        return {a: g["ok"] for a, g in t["actions"].items()}, t

    def test_gates_table(self):
        # status: the actions that must be on (the rest checked off where the predicate is plain);
        # continue is the queue's own continue_refused, which wants a stage session to go on with
        table = {
            "queued": {"stop", "remove", "archive", "move", "note", "edit"},
            "running": {"stop", "move", "note"},
            "interrupted": {"stop", "retry", "remove", "archive"},
            "needs-answers": {"answer", "retry", "stop", "remove", "archive"},
            "plan-review": {"answer", "approve", "stop", "remove", "archive"},
            "blocked": {"answer", "retry", "stop", "remove", "archive"},
            "stopped": {"retry", "remove", "archive"},
            "done": {"retry", "remove", "archive"},
        }
        never = {
            "queued": {"approve", "retry", "unarchive", "followup"},
            "running": {"remove", "archive", "approve", "retry", "answer", "unarchive"},
            "done": {"stop", "approve", "answer", "continue", "unarchive"},
            "stopped": {"stop", "approve", "answer", "unarchive"},
        }
        for status, on in table.items():
            got, t = self.gates(status=status, stage="implement" if status != "plan-review" else "plan")
            self.assertEqual(set(got), set(runs.TICKET_ACTIONS), status)
            for a in on:
                self.assertTrue(got[a], f"{status}: {a} should be on ({t['actions'][a]})")
            for a in never.get(status, ()):
                self.assertFalse(got[a], f"{status}: {a} should be off")
                self.assertTrue(t["actions"][a]["reason"], f"{status}: {a} has no reason")
            for a, g in t["actions"].items():
                self.assertEqual(g["reason"] == "", g["ok"], (status, a, g))

    def test_stopping_and_archived(self):
        open(os.path.join(self.fx.queue, "ABC-12", "stop"), "w").close()
        got, t = self.gates(status="running", stage="implement")
        self.assertTrue(t["state"]["stopping"])
        self.assertFalse(got["stop"])
        self.assertEqual(t["actions"]["stop"]["reason"], "it is stopping")
        os.remove(os.path.join(self.fx.queue, "ABC-12", "stop"))
        os.remove(os.path.join(self.fx.queue, "run.json"))
        self.fx.ticket()
        with open(os.path.join(self.fx.queue, "queue.list"), "w") as f:
            f.write("")
        with open(os.path.join(self.fx.queue, "archived.list"), "w") as f:
            f.write("ABC-12\n")
        t = self.ok("/tickets/ABC-12")
        self.assertTrue(t["archived"])
        on = {a for a, g in t["actions"].items() if g["ok"]}
        self.assertEqual(on, {"unarchive", "remove", "note"})
        q = self.ok("/queue")
        self.assertEqual([x["key"] for x in q["archived"]], ["ABC-12"])
        self.assertEqual(q["tickets"], [])

    def test_missing_corrupt_and_removed(self):
        with open(os.path.join(self.fx.queue, "ABC-12", "state.json"), "w") as f:
            f.write("{not json")
        q = self.ok("/queue")
        self.assertEqual(q["tickets"][0]["status"], "")
        t = self.ok("/tickets/ABC-12")
        self.assertEqual({a for a, g in t["actions"].items() if g["ok"]}, {"remove"})
        os.remove(os.path.join(self.fx.queue, "ABC-12", "state.json"))
        t = self.ok("/tickets/ABC-12")
        self.assertEqual(t["state"].get("status", ""), "")
        shutil.rmtree(os.path.join(self.fx.queue, "ABC-12"))
        r, d = self.fx.get("/tickets/ABC-12")
        self.assertEqual(r.status, 404)
        _, d = self.fx.get("/poll?t=ticket:ABC-12")
        self.assertEqual(d["topics"]["ticket:ABC-12"]["data"], {"gone": True})
        r, d = self.fx.get("/tickets/..%2Fetc")
        self.assertEqual(r.status, 404)

    def test_ticket_fields(self):
        t = self.ok("/tickets/ABC-12")
        for k in ("key", "archived", "state", "actions", "retry_choices", "sessions", "outs", "log"):
            self.assertIn(k, t)
        self.assertNotIn("stop_wait", t["state"])
        self.assertEqual([o["name"] for o in t["outs"]], ["plan-1.out"])
        self.assertIn("verifier: pass", t["log"])
        sids = [s["sid"] for s in t["sessions"]]
        self.assertIn(QSES, sids)
        for c in t["retry_choices"]:
            self.assertEqual(set(c), {"value", "label"})

    def test_log_capped_newest_kept(self):
        with open(os.path.join(self.fx.queue, "ABC-12", "log.md"), "w") as f:
            f.write("old line\n" * 40000 + "the newest line\n")
        t = self.ok("/tickets/ABC-12")
        self.assertLessEqual(len(t["log"].encode()), api.LOG_CAP)
        self.assertTrue(t["log"].endswith("the newest line\n"))


class OutTest(ApiTest):
    def write(self, data):
        with open(os.path.join(self.fx.queue, "ABC-12", "plan-1.out"), "wb") as f:
            f.write(data)

    def test_chunks(self):
        d = self.ok("/tickets/ABC-12/out/plan-1.out")
        self.assertEqual(d, {"name": "plan-1.out", "from": 0, "next": 9, "size": 9, "text": "the plan\n",
                             "reset": False})
        d = self.ok("/tickets/ABC-12/out/plan-1.out?from=9")
        self.assertEqual((d["text"], d["next"]), ("", 9))
        d = self.ok("/tickets/ABC-12/out/plan-1.out?from=100")
        self.assertTrue(d["reset"])
        self.assertEqual((d["from"], d["text"]), (0, "the plan\n"))
        for bad in ("../state.json", "state.json", "nope.out"):
            r, _ = self.fx.get("/tickets/ABC-12/out/" + quote(bad, safe=""))
            self.assertEqual(r.status, 404, bad)
        r, _ = self.fx.get("/tickets/ABC-12/out/plan-1.out?from=-1")
        self.assertEqual(r.status, 400)

    def test_utf8_boundary_and_big_file(self):
        body = b"a" * (api.OUT_CHUNK - 1) + "é".encode() + b"tail"
        self.write(body)
        d = self.ok("/tickets/ABC-12/out/plan-1.out")
        self.assertEqual(d["next"], api.OUT_CHUNK - 1)
        self.assertNotIn("�", d["text"])
        d2 = self.ok(f"/tickets/ABC-12/out/plan-1.out?from={d['next']}")
        self.assertEqual(d2["text"], "étail")
        self.assertEqual(d2["next"], len(body))


class SessionTest(ApiTest):
    kw = {"opencode_port": 4096}

    def test_messages_pages_are_the_whole_reads_slices(self):
        """A-3: /messages reads its page with SQL limit/offset; the same as the slice of all."""
        whole = self.ok(f"/sessions/{ROOT}/messages?limit=1000")
        n = whole["count"]
        self.assertGreater(n, 0)
        for start in range(0, n + 2):
            for limit in (1, 2, 1000):
                m = self.ok(f"/sessions/{ROOT}/messages?from={start}&limit={limit}")
                self.assertEqual((m["from"], m["count"]), (min(start, n), n))
                self.assertEqual(m["messages"], whole["messages"][start:start + limit])

    def test_list_head_messages_part(self):
        rows = self.ok("/sessions")
        ids = [r["id"] for r in rows]
        self.assertIn(ROOT, ids)
        self.assertNotIn(CHILD, ids)
        self.assertIn(CHILD, [r["id"] for r in self.ok("/sessions?all=1")])
        q = [r for r in rows if r["id"] == QSES][0]
        self.assertEqual(q["ticket"], "ABC-12")
        self.assertEqual(q["kind"], "queue")
        r0 = [r for r in rows if r["id"] == ROOT][0]
        self.assertEqual(r0["source"], "host")
        self.assertTrue(r0["web"].startswith(f"http://127.0.0.1:4096/server/"))
        self.assertNotIn("@", r0["web"])
        self.assertEqual(self.ok("/sessions?limit=1")[0]["id"], rows[0]["id"])
        self.assertEqual([r["id"] for r in self.ok("/sessions?q=parser")], [ROOT])
        h = self.ok("/sessions/" + ROOT)
        self.assertEqual(h["count"], 2)
        self.assertEqual([c["id"] for c in h["children"]], [CHILD])
        self.assertFalse(h["send"]["ok"])
        self.assertTrue(h["send"]["reason"])
        m = self.ok(f"/sessions/{ROOT}/messages")
        self.assertEqual((m["from"], m["count"]), (0, 2))
        self.assertEqual([x["index"] for x in m["messages"]], [0, 1])
        tool = [p for p in m["messages"][1]["parts"] if p.get("type") == "tool" and p["tool"] == "bash"][0]
        self.assertTrue(tool["truncated"])
        self.assertEqual(len(tool["state"]["output"]), api.PART_CAP)
        m = self.ok(f"/sessions/{ROOT}/messages?from=1&limit=1")
        self.assertEqual([x["index"] for x in m["messages"]], [1])
        full = self.ok(f"/sessions/{ROOT}/parts/{tool['id']}")
        self.assertGreater(len(full["text"]), api.PART_CAP)
        r, _ = self.fx.get("/sessions/ses_nope")
        self.assertEqual(r.status, 404)
        r, _ = self.fx.get(f"/sessions/{ROOT}/messages?limit=0")
        self.assertEqual(r.status, 400)

    def test_opencode(self):
        d = self.ok("/opencode")
        self.assertEqual((d["port"], d["user"]), (4096, "opencode"))
        self.assertEqual(self.ok("/status")["mode"], "serve")

    def test_send_and_session_stop(self):
        with open(os.path.join(self.fx.live, ROOT + ".json"), "w") as f:
            f.write("{}")
        r, d = self.fx.post(f"/sessions/{ROOT}/send", {"text": "hello\r\nthere"})
        self.assertEqual(r.status, 200, d)
        self.assertTrue(d["ok"])
        inbox = os.path.join(self.fx.live, ROOT + ".inbox")
        names = os.listdir(inbox)
        self.assertEqual(len(names), 1)
        with open(os.path.join(inbox, names[0])) as f:
            self.assertEqual(json.load(f)["parts"][0]["text"], "hello\nthere")
        r, d = self.fx.post(f"/sessions/{ROOT}/send", {"text": 3})
        self.assertEqual(r.status, 400)
        r, d = self.fx.post("/sessions/ses_nope/send", {"text": "x"})
        self.assertEqual(r.status, 404)
        with mock.patch.object(runs, "session_stop", return_value=(False, "not a queue stage")):
            r, d = self.fx.post(f"/sessions/{ROOT}/stop", {})
        self.assertEqual((r.status, d["error"]["code"], d["error"]["message"]), (409, "refused", "not a queue stage"))


class DiffTest(ApiTest):
    """/tickets/<K>/diff and /lines over a real git project (the ticket's work in place)."""

    def git(self, *a):
        import subprocess
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", self.proj, *a], check=True, env=env, capture_output=True,
                              text=True).stdout.strip()

    def write(self, name, text):
        with open(os.path.join(self.proj, name), "w") as f:
            f.write(text)

    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.tmp.name, "proj-git")
        os.makedirs(self.proj)
        self.git("init", "-q", "-b", "main")
        self.write("a.txt", "one\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "a")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "ABC-12")
        self.write("a.txt", "one\nwork line\n")
        self.write(".env", "TOKEN=nope\n")
        self.git("add", "a.txt", ".env")
        self.git("commit", "-qm", "the ticket's work")
        self.git("checkout", "-q", "main")
        self.write("a.txt", "one\nnot committed\n")
        self.fx.ticket(project=self.proj, in_place=True, base_sha=base, status="done", questions=[])

    def test_queue_polls_do_not_spawn_git_again_per_done_ticket(self):
        """A-4: the queue's gates ask fix_refused (a git rev-list) for every done ticket; a poll
        a minute on asks git again only when the branch's ref changed, not every minute."""
        import subprocess
        real_popen, real_time, spawned, off = subprocess.Popen, time.time, [], [0]

        class Counting(real_popen):
            def __init__(self, cmd, *a, **k):
                if isinstance(cmd, (list, tuple)) and "rev-list" in cmd:
                    spawned.append(cmd)
                super().__init__(cmd, *a, **k)

        def poll():
            r, d = self.fx.get("/poll?t=queue")
            self.assertEqual(r.status, 200, d)
            return d
        with mock.patch("subprocess.Popen", Counting), mock.patch("time.time", lambda: real_time() + off[0]):
            off[0] = 5
            poll()
            first = len(spawned)
            for i in range(1, 4):   # each a minute and more on: the old memo expired every minute
                off[0] = 5 + 70 * i
                poll()
            self.assertEqual(len(spawned), first, spawned)
            # a commit on the branch (made without a checkout: the tree has changes): asked again, once
            sha = self.git("commit-tree", self.git("rev-parse", "ABC-12^{tree}"), "-p", "ABC-12", "-m", "more")
            self.git("update-ref", "refs/heads/ABC-12", sha)
            off[0] = 5 + 70 * 5
            poll()
            poll()
            self.assertEqual(len(spawned), first + 1, spawned)

    def test_work_and_uncommitted(self):
        d = self.ok("/tickets/ABC-12/diff")
        self.assertEqual((d["repo"], d["view"]), (".", "work"))
        self.assertIn(".", [r["rel"] for r in d["repos"]])
        self.assertIn("+work line", d["diff"])
        self.assertNotIn("TOKEN", d["diff"])
        self.assertNotIn("not committed", d["diff"])
        self.assertEqual(d["error"], "")
        for c in d["commits"]:
            self.assertEqual(set(c), {"sha", "subject"})
        d = self.ok("/tickets/ABC-12/diff?view=uncommitted")
        self.assertEqual(d["view"], "uncommitted")
        self.assertIn("+not committed", d["diff"])
        r, e = self.fx.get("/tickets/ABC-12/diff?repo=nope")
        self.assertEqual((r.status, e["error"]["code"]), (404, "not-found"))

    def test_lines(self):
        d = self.ok("/tickets/ABC-12/lines?" + urlencode({"repo": ".", "view": "uncommitted", "path": "a.txt",
                                                          "from": 1, "to": 2}))
        self.assertEqual(d["total"], 2)
        self.assertIn("not committed", json.dumps(d["lines"]))
        for q in ({"view": "uncommitted", "path": "a.txt", "from": "x", "to": 2},
                  {"view": "uncommitted", "path": ".env", "from": 1, "to": 1},
                  {"view": "bogus", "path": "a.txt", "from": 1, "to": 1}):
            r, e = self.fx.get("/tickets/ABC-12/lines?" + urlencode(q))
            self.assertEqual((r.status, e["error"]["code"]), (400, "bad-request"), q)
        r, e = self.fx.get("/tickets/ABC-99/lines?view=uncommitted&path=a.txt&from=1&to=1")
        self.assertEqual(r.status, 404)


class WebLinkTest(unittest.TestCase):
    def test_same_link_as_pub_without_the_login(self):
        for url in ("https://opencode:s3cret@Example.org:443", "http://127.0.0.1:4096"):
            cfg = pub.Config(8080, "", "", 0, "", (), opencode_url=url)
            ctx = mock.Mock(cfg=cfg)
            ours = api._web_link(ctx)("host", ROOT)
            theirs = pub.opencode_session_link(cfg, "127.0.0.1")("host", ROOT)
            self.assertNotIn("s3cret", ours)
            self.assertEqual(ours, theirs.replace("opencode:s3cret@", "").replace("Example.org:443", "example.org"))
            self.assertIsNone(api._web_link(ctx)("container", ROOT))


class ActionTest(ApiTest):
    def act(self, path, body, ret=(True, "done")):
        target = "queue_action" if path.startswith("/queue/") else "ticket_action"
        with mock.patch.object(runs, target, return_value=ret) as m:
            r, d = self.fx.post(path, body)
        return r, d, (m.call_args[0] if m.called else None)

    def test_queue_actions(self):
        r, d, call = self.act("/queue/start", {"mode": "host", "confirm_host": True})
        self.assertEqual((r.status, d), (200, {"ok": True, "msg": "done"}))
        self.assertEqual(call[1], "start")
        self.assertEqual(call[2], {"mode": ["host"], "confirm_host": ["1"]})
        r, d, call = self.act("/queue/add", {"key": "NEW-1", "stages": ["plan", "push"],
                                             "repo_modes": {"sub": "clone"}, "plan_review": True,
                                             "in_place": False, "debug": None})
        self.assertEqual(call[2], {"key": ["NEW-1"], "stage_plan": ["1"], "stage_push": ["1"],
                                   "repo_mode:sub": ["clone"], "plan_review": ["1"]})
        r, d, _ = self.act("/queue/halt", {}, (False, "no run is going"))
        self.assertEqual((r.status, d["error"]["code"], d["error"]["message"]), (409, "refused", "no run is going"))

    def test_ticket_actions_and_changed(self):
        for a in runs.TICKET_ACTIONS:
            r, d, call = self.act(f"/tickets/ABC-12/{a}", {"text": "hi"} if a == "note" else {})
            self.assertEqual(r.status, 200, (a, d))
            self.assertEqual(call[1:3], ("ABC-12", a))
        r, d, _ = self.act("/tickets/ABC-12/answer", {"sig": "old"}, (False, runs.CHANGED))
        self.assertEqual((r.status, d["error"]["code"]), (409, "changed"))
        r, d, _ = self.act("/tickets/ABC-12/bogus", {})
        self.assertEqual(r.status, 400)
        r, d, _ = self.act("/tickets/NOPE-1/retry", {})
        self.assertEqual(r.status, 404)
        r, d, call = self.act("/tickets/ABC-12/fix", {"comments": [{"body": "x"}]})
        self.assertEqual(json.loads(call[3]["comments"][0]), [{"body": "x"}])
        r, d, call = self.act("/tickets/ABC-12/move", {"before": None})
        self.assertEqual(call[3], {"before": [""]})

    def test_answer_simple_form(self):
        self.fx.ticket(questions=[{"id": "Q1", "text": "Which db?", "blocking": True},
                                  {"id": "Q2", "text": "Log level?", "blocking": False}])
        r, d, call = self.act("/tickets/ABC-12/answer", {"answers": {"Q2": "debug", "Q9": "stray"},
                                                         "feedback": "thanks", "go": True, "sig": "s1"})
        self.assertEqual(r.status, 200, d)
        f = call[3]
        self.assertEqual((f["q1"], f["a1"]), (["Log level?"], ["debug"]))
        self.assertEqual(f["defaults"], ["1"])
        self.assertEqual(f["sig"], ["s1"])
        self.assertIn("thanks", f["note"][0])
        self.assertIn("stray", f["note"][0])

    def test_answer_items_form(self):
        self.fx.ticket(status="plan-review", **PLAN_ITEMS)
        r, d, call = self.act("/tickets/ABC-12/answer", {
            "answers": {"Q2": "warn"}, "picks": {"Q1": 1, "Q3": "other"},
            "criteria": {"AC2": {"reject": True, "comment": "not needed"}}, "assumptions": {"A1": "no"},
            "feedback": "see above", "sig": "abc"})
        f = call[3]
        self.assertEqual(f["qt.Q2"], ["warn"])
        self.assertEqual((f["q.Q1"], f["q.Q3"]), (["1"], ["other"]))
        self.assertEqual((f["ac.AC2"], f["acc.AC2"]), (["reject"], ["not needed"]))
        self.assertEqual(f["as.A1"], ["no"])
        self.assertEqual(f["note"], ["see above"])
        r, d, _ = self.act("/tickets/ABC-12/answer", {"answers": ["x"]})
        self.assertEqual(r.status, 400)

    def test_real_note_action(self):
        r, d = self.fx.post("/tickets/ABC-12/note", {"text": "a note from the app"})
        self.assertEqual(r.status, 200, d)
        t = self.ok("/tickets/ABC-12")
        self.assertIn("a note from the app", json.dumps(t["state"].get("notes")))

    def test_findings_and_settings(self):
        with mock.patch.object(api.findings_view, "action", return_value=(True, "archived")) as m:
            r, d = self.fx.post("/findings/archive", {"id": ["f1", "f2"]})
        self.assertEqual(r.status, 200, d)
        self.assertEqual(m.call_args[0][2], {"id": ["f1", "f2"]})
        with mock.patch.object(settings_view, "save_config", return_value=(True, "saved")) as m:
            r, d = self.fx.post("/settings/config", {"set": {"MODEL": "big"}, "clear": ["KEY_X"]})
        self.assertEqual(r.status, 200, d)
        self.assertEqual(m.call_args[0][1], {"v_MODEL": ["big"], "clear_KEY_X": ["1"]})
        r, d = self.fx.post("/settings/access", {"act": "add", "name": "bob"})
        self.assertEqual((r.status, d["error"]["code"]), (409, "refused"))


class SettingsReadTest(ApiTest):
    def test_secrets_masked(self):
        rows = [{"key": "API_KEY", "value": "sk-very-secret", "secret": True, "desc": "key"},
                {"key": "PROXY", "value": "http://***@h:1", "secret": True, "desc": ""},
                {"key": "MASKED", "value": "(set, 9 chars)", "secret": True, "desc": ""},
                {"key": "MODEL", "value": "big", "secret": False, "desc": "the model"}]
        g = {"config": (rows, ""), "net": ([{"host": "x.org", "push": False, "note": ""}], ""),
             "mcp": (None, "harness failed")}
        with mock.patch.object(settings_view, "gather", return_value=g), \
                mock.patch.object(settings_view, "system_info", return_value={"os": "linux"}):
            s = self.ok("/settings")
        vals = {r["key"]: r["value"] for r in s["config"]}
        self.assertEqual(vals, {"API_KEY": "(set, 14 chars)", "PROXY": "http://***@h:1",
                                "MASKED": "(set, 9 chars)", "MODEL": "big"})
        self.assertNotIn("sk-very-secret", json.dumps(s))
        self.assertEqual(s["mcp"], [])
        self.assertEqual(s["mcp_error"], "harness failed")
        self.assertEqual(s["access"], {"enabled": False, "names": [], "me": None})


class ReadsSmokeTest(ApiTest):
    """Every P0/P1 read answers its envelope (no 500) on the fixture."""

    def test_every_read(self):
        paths = ["/status", "/home", "/queue", "/queue/runner-log", "/tickets/ABC-12", "/tickets/ABC-12/edit",
                 "/sessions", "/sessions/" + ROOT, f"/sessions/{ROOT}/messages", f"/sessions/{ROOT}/export",
                 "/follow?since=0", "/follow?queue=1", "/findings", "/findings/agent", "/vaults", "/vault",
                 "/vault/ingest", "/vault/tree", "/vault/file?path=wiki/index.md", "/update", "/debug"]
        for p in paths:
            r, d = self.fx.get(p)
            self.assertIn(r.status, (200,), (p, d))
            self.assertEqual(d["sig"], api.sig_of(d["data"]), p)
        # these answer an error on this fixture (no git project, no Jira): an envelope, never a 500
        for p in ("/tickets/ABC-12/diff", "/tickets/ABC-12/diff?repo=nope", "/tickets/ABC-12/lines?from=x",
                  "/jira", "/queue/branches?project=/nope", "/queue/repos?project=/nope",
                  "/findings/notes/nope/x.md", "/debug/runs/nope", "/debug/bogus/x"):
            with mock.patch.object(settings_view, "read_doctor", return_value=(0, "all good")):
                r, d = self.fx.get(p)
            self.assertNotEqual(r.status, 500, (p, d))
            self.assertIn("ok", d, p)
            if d["ok"]:
                self.assertEqual(d["sig"], api.sig_of(d["data"]), p)
        for p in ("/vault/file?path=../state", "/vault/file?path=.git/config", "/vault/tree?path=nope"):
            r, d = self.fx.get(p)
            self.assertEqual(r.status, 404, p)
        r, d = self.fx.get("/vault/search?q=x")
        self.assertEqual((r.status, d["error"]["code"]), (501, "unavailable"))
        self.assertEqual(self.ok("/vault/file?path=wiki/index.md")["text"], "# Index\n")
        r, d = self.fx.get("/poll?" + "&".join("t=" + t for t in api.READS if t not in api.ARG_TOPICS))
        for t, v in d["topics"].items():
            self.assertNotEqual(v.get("error", {}).get("code"), "internal", (t, v))


# --- --dump: the fixture's answers for the desktop app's mock serve --------------------------

DUMP_PATHS = {
    "status": "/status", "home": "/home", "queue": "/queue", "sessions": "/sessions",
    "sessions-all": "/sessions?all=1", "session": "/sessions/" + ROOT, "messages": f"/sessions/{ROOT}/messages",
    "export": f"/sessions/{ROOT}/export", "ticket": "/tickets/ABC-12", "ticket-edit": "/tickets/ABC-12/edit",
    "out": "/tickets/ABC-12/out/plan-1.out", "runner-log": "/queue/runner-log", "follow": "/follow?since=0",
    "findings": "/findings", "findings-agent": "/findings/agent", "vaults": "/vaults", "vault": "/vault",
    "ingest": "/vault/ingest", "vault-tree": "/vault/tree", "vault-file": "/vault/file?path=wiki/index.md",
    "update": "/update", "debug": "/debug", "opencode": "/opencode",
    "poll": "/poll?" + "&".join("t=" + t for t in ("status", "home", "queue", "sessions", "findings",
                                                   "findings-agent", "vault", "ingest", "debug",
                                                   "ticket:ABC-12", "session:" + ROOT)),
}


def dump(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp, opencode_port=4096)
        try:
            for i, status in enumerate(STATUSES):
                if status != "needs-answers":
                    fx.ticket(f"DEMO-{i + 1}", status=status, stage="implement", title=f"A {status} ticket")
            fx.run_on("DEMO-2", stage="implement", step="agent", model="big", started="2026-11-19T06:00:00Z")
            fx.get("/queue")
            fx.wait_pending()
            n = 0
            for name, path in sorted(DUMP_PATHS.items()):
                r, d = fx.get(path)
                text = json.dumps(d, indent=1, sort_keys=True)
                text = text.replace(tmp, "/home/user/harness-fixture").replace(api.ROOT, "/home/user/harness")
                with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as f:
                    f.write(text + "\n")
                n += 1
                print(f"{r.status} {path} -> {name}.json")
            print(f"wrote {n} files to {out_dir}")
        finally:
            fx.close()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        dump(sys.argv[2])
    else:
        unittest.main()
