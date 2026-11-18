"""Tests for the queue page's Jira panel (serve/jira_view.py) against a fake Jira
on 127.0.0.1, and its route on a live pub server.

Run: python3 -m pytest serve/test_jira_view.py -q
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jira_view  # noqa: E402
import pub  # noqa: E402
import runs  # noqa: E402


def issue(key, summary, status="To Do", cat="new", who="Ann Lee", kind="Story", prio="Medium"):
    return {"key": key, "fields": {"summary": summary, "status": {"name": status, "statusCategory": {"key": cat}},
                                   "assignee": {"displayName": who} if who else None,
                                   "issuetype": {"name": kind}, "priority": {"name": prio}}}


class FakeJira:
    """A Jira answering searches: `paths` the search paths it has (others 404, or
    410 for the old Cloud ones), `status` an error code to answer, `delay` seconds."""

    def __init__(self):
        self.seen = []        # (path, jql, Authorization)
        self.paths = {"/rest/api/2/search"}
        self.gone = set()
        self.status = 0
        self.body = b""
        self.delay = 0
        self.issues = lambda jql: []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                fake.seen.append((u.path, (q.get("jql") or [""])[0], self.headers.get("Authorization", ""),
                                  (q.get("fields") or [""])[0], (q.get("maxResults") or [""])[0]))
                if fake.delay:
                    time.sleep(fake.delay)
                code, body = 200, b""
                if u.path in fake.gone:
                    code = 410
                elif u.path not in fake.paths:
                    code = 404
                elif fake.status:
                    code, body = fake.status, fake.body
                else:
                    body = json.dumps({"issues": fake.issues((q.get("jql") or [""])[0])}).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jira = FakeJira()
        self.queue = os.path.join(self.tmp.name, "queue")
        self.vault = os.path.join(self.tmp.name, "vault")
        os.makedirs(os.path.join(self.vault, "wiki"))
        os.makedirs(self.queue)
        self.charter(self.jira.url)
        self.queued("ABC-2", "blocked")
        jira_view._cache.clear()
        jira_view._endpoint.clear()
        jira_view.set_token("pat-secret-123")
        self.runs = runs.Runs([], queue_dir=self.queue, vault=self.vault)
        self.jira.issues = lambda jql: ([issue("ABC-3", "Third by rank"), issue("ABC-1", "First <b>", who=""),
                                         issue("ABC-2", "Second", "In Progress", "indeterminate")]
                                        if "openSprints()" in jql and "not in" not in jql else
                                        [issue("ABC-9", "Backlog top"), issue("ABC-5", "Backlog next")])

    def tearDown(self):
        self.jira.close()
        jira_view.set_token("")
        jira_view._cache.clear()
        jira_view._endpoint.clear()
        self.tmp.cleanup()

    def charter(self, url, pattern="[A-Z][A-Z0-9]+-[0-9]+"):
        with open(os.path.join(self.vault, "wiki", "overview.md"), "w") as f:
            f.write(f"---\ntype: overview\nproject: 'x'\nticket_pattern: '{pattern}'\ntracker_url: '{url}'\n---\n")

    def queued(self, key, status, archived=False):
        os.makedirs(os.path.join(self.queue, key), exist_ok=True)
        with open(os.path.join(self.queue, key, "state.json"), "w") as f:
            json.dump({"key": key, "status": status, "stage": "plan", "project": "/p"}, f)
        name = "archived.list" if archived else "queue.list"
        with open(os.path.join(self.queue, name), "a") as f:
            f.write(key + "\n")

    def get(self, which="sprint", mine=False):
        return jira_view.panel_json(self.runs, {"list": [which], "mine": ["1" if mine else "0"]})


class PanelTest(Base):
    def test_placeholder_is_instant(self):
        self.jira.delay = 3
        t = time.time()
        h = jira_view.panel_html(self.runs)
        self.assertLess(time.time() - t, 0.5)
        self.assertEqual(self.jira.seen, [])
        self.assertIn("id='jira'", h)
        self.assertIn("data-url='/runs/queue/jira'", h)
        self.assertIn("Sprint", h)
        self.assertIn("Backlog", h)
        self.assertNotIn("pat-secret", h)

    def test_sprint_rank_order_and_rows(self):
        d = self.get()
        self.assertEqual(d["error"], "")
        self.assertEqual(d["count"], 3)
        h = d["html"]
        self.assertLess(h.index("ABC-3"), h.index("ABC-1"))
        self.assertLess(h.index("ABC-1"), h.index("ABC-2"))
        self.assertIn(f"href='{self.jira.url}/browse/ABC-3' target='_blank' rel='noopener'", h)
        self.assertIn("First &lt;b&gt;", h)              # escaped
        self.assertIn("unassigned", h)
        self.assertIn("Story", h)
        self.assertIn("Medium", h)
        path, jql, auth, fields, n = self.jira.seen[0]
        self.assertEqual(path, "/rest/api/2/search")    # not Cloud: Server/DC search first
        self.assertEqual(jql, 'project = "ABC" AND sprint in openSprints() ORDER BY Rank ASC')
        self.assertEqual(fields, jira_view.FIELDS)
        self.assertEqual(n, "50")
        self.assertEqual(auth, "Bearer pat-secret-123")
        self.assertNotIn("pat-secret", json.dumps(d))

    def test_queued_badge_and_add(self):
        h = self.get()["html"]
        two = h[h.index("data-key='ABC-2'"):]
        two = two[:two.index("<div class='jr", 5)] if "<div class='jr" in two[5:] else two
        self.assertIn("/runs/ticket/ABC-2", two)
        self.assertIn(">blocked<", two)
        self.assertNotIn("data-add", two)
        self.assertIn("data-add='ABC-3'", h)
        self.assertIn("class='jcb' value='ABC-3'", h)
        self.assertNotIn("value='ABC-2'", h)

    def test_done_in_jira_is_not_offered(self):
        self.jira.issues = lambda jql: [issue("ABC-7", "Finished", "Done", "done"), issue("ABC-8", "Open")]
        h = self.get()["html"]
        seven = h[h.index("data-key='ABC-7'"):h.index("data-key='ABC-8'")]
        self.assertIn("<span class='muted'>done</span>", seven)   # no Add, no checkbox
        self.assertNotIn("data-add", seven)
        self.assertNotIn("value='ABC-7'", h)
        self.assertIn("data-add='ABC-8'", h)

    def test_jira_down_says_why_without_errno(self):
        jira_view._cache.clear()
        self.jira.close()   # nothing listens there now
        e = self.get()["error"]
        self.assertTrue(e.startswith("Jira is not reachable: "), e)
        self.assertNotIn("Errno", e)

    def test_archived_badge(self):
        self.queued("ABC-5", "done", archived=True)
        h = self.get("backlog")["html"]
        five = h[h.index("data-key='ABC-5'"):]
        self.assertIn("archived", five[:600])
        self.assertIn("data-add='ABC-9'", h)

    def test_backlog_and_mine_jql(self):
        d = self.get("backlog", mine=True)
        self.assertLess(d["html"].index("ABC-9"), d["html"].index("ABC-5"))
        self.assertEqual(self.jira.seen[-1][1],
                         'project = "ABC" AND (sprint is EMPTY OR sprint not in openSprints()) AND statusCategory '
                         '!= Done AND assignee = currentUser() ORDER BY Rank ASC')

    def test_project_from_pattern_then_queue(self):
        self.charter(self.jira.url, "XY-[0-9]+")
        self.get()
        self.assertTrue(self.jira.seen[-1][1].startswith('project = "XY" AND'))
        self.charter(self.jira.url)
        self.queued("DEF-1", "queued")
        self.queued("DEF-4", "queued")
        self.queued("LOCAL-3", "queued")                  # a ticket of your own: no Jira project
        self.get()
        self.assertTrue(self.jira.seen[-1][1].startswith('project in ("DEF", "ABC") AND'))

    def test_no_project_hint(self):
        os.remove(os.path.join(self.queue, "queue.list"))
        import shutil
        shutil.rmtree(os.path.join(self.queue, "ABC-2"))
        d = self.get()
        self.assertIn("No Jira project known yet", d["error"])
        self.assertEqual(self.jira.seen, [])

    def test_basic_auth_for_email_token(self):
        jira_view.set_token("me@example.com:api-tok")
        self.get()
        self.assertEqual(self.jira.seen[0][2], "Basic bWVAZXhhbXBsZS5jb206YXBpLXRvaw==")

    def test_cloud_endpoint_choice(self):
        self.assertEqual(jira_view._paths("https://acme.atlassian.net"), [jira_view.CLOUD, jira_view.SERVER])
        self.assertEqual(jira_view._paths("https://jira.acme.com"), [jira_view.SERVER, jira_view.CLOUD])

    def test_falls_back_on_410_and_remembers(self):
        self.jira.paths = {"/rest/api/3/search/jql"}
        self.jira.gone = {"/rest/api/2/search"}
        d = self.get()
        self.assertEqual(d["count"], 3)
        self.assertEqual([s[0] for s in self.jira.seen], ["/rest/api/2/search", "/rest/api/3/search/jql"])
        self.get("backlog")
        self.assertEqual(self.jira.seen[-1][0], "/rest/api/3/search/jql")   # the one that answered, first
        self.assertEqual(len(self.jira.seen), 3)

    def test_errors_are_one_line(self):
        cases = []
        self.jira.status = 401
        cases.append(self.get()["error"])
        jira_view._cache.clear()
        self.jira.status, self.jira.body = 400, json.dumps(
            {"errorMessages": ["The value 'ABC' does not exist for the field 'project'."], "errors": {}}).encode()
        cases.append(self.get()["error"])
        self.assertIn("refused the token (HTTP 401)", cases[0])
        self.assertEqual(cases[1], "Jira answered HTTP 400: The value 'ABC' does not exist for the field 'project'.")
        for c in cases:
            self.assertNotIn("\n", c)
            self.assertNotIn("pat-secret", c)

    def test_timeout(self):
        old = jira_view.TIMEOUT
        jira_view.TIMEOUT = 0.3
        try:
            self.jira.delay = 1.5
            d = self.get()
        finally:
            jira_view.TIMEOUT = old
        self.assertIn("did not answer", d["error"])

    def test_no_url_no_token_bad_url_no_vault(self):
        jira_view.set_token("")
        self.assertIn("no Jira token", self.get()["error"])
        jira_view.set_token("t")
        self.charter("")
        self.assertIn("No Jira URL", self.get()["error"])
        self.charter("http://jira.example.com")
        self.assertIn("https", self.get()["error"])
        self.runs = runs.Runs([], queue_dir=self.queue, vault="")
        self.assertIn("No active vault", self.get()["error"])
        self.assertEqual(self.jira.seen, [])

    def test_cache_fresh_then_stale_while_revalidate(self):
        self.get()
        self.get()
        self.assertEqual(len(self.jira.seen), 1)            # fresh: from the cache
        self.get(mine=True)
        self.assertEqual(len(self.jira.seen), 2)            # another (list, mine): its own entry
        for e in jira_view._cache.values():
            e["at"] -= jira_view.FRESH + 1
        self.jira.delay = 0.5
        t = time.time()
        d = self.get()
        self.assertLess(time.time() - t, 0.3)                # stale: served now, refreshed behind
        self.assertEqual(d["count"], 3)
        for _ in range(50):
            if len(self.jira.seen) == 3 and not any(e.get("busy") for e in jira_view._cache.values()):
                break
            time.sleep(0.05)
        self.assertEqual(len(self.jira.seen), 3)

    def test_failed_refresh_keeps_rows(self):
        self.get()
        for e in jira_view._cache.values():
            e["at"] -= jira_view.FRESH + 1
        self.jira.status = 503
        self.get()
        for _ in range(50):
            if not any(e.get("busy") for e in jira_view._cache.values()):
                break
            time.sleep(0.05)
        d = self.get()
        self.assertEqual(d["count"], 3)
        self.assertIn("earlier list", d["note"])

    def test_badge_is_not_cached(self):
        self.get()
        self.queued("ABC-3", "queued")
        h = self.get()["html"]
        self.assertNotIn("data-add='ABC-3'", h)
        self.assertEqual(len(self.jira.seen), 1)


class RouteTest(Base):
    def test_route_and_page(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = srv.server_address[1]
        cfg = pub.Config(port, self.vault, "", 0, "", (), agent_data=[("host", self.tmp.name)], queue_dir=self.queue)
        srv.RequestHandlerClass = pub.make_handler(cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def get(path):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
                r = c.getresponse()
                body = r.read().decode()
                c.close()
                return r, body
            r, body = get("/runs/queue")
            self.assertEqual(r.status, 200)
            self.assertIn("id='jira'", body)
            self.assertIn("hjira:list", body)                 # the panel's script is in the page's
            self.assertEqual(self.jira.seen, [])              # the page did not ask Jira
            # Add opens the Add dialog for the ticket (its settings first); it does not post the form itself
            self.assertIn("<dialog id='adddlg'", body)
            self.assertIn("window.hAddOpen(keys", body)
            self.assertIn("window.hAddOpen = function", body)
            self.assertNotIn("new FormData(", jira_view.JS)
            self.assertIn("Add opens the ticket's settings first", body)
            r, body = get("/runs/queue/jira?list=backlog")
            self.assertEqual(r.status, 200)
            d = json.loads(body)
            self.assertIn("ABC-9", d["html"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
