"""Tests for the queue page's findings board (serve/findings_view.py): the tickets' findings
notes and the findings agent's findings.json read into one list, the owner's board.json
(archive, restore, order), the panel's HTML, and its POST routes on a live server.

Run: cd serve && python3 -m unittest -q test_findings
"""

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pub  # noqa: E402
import runs  # noqa: E402
import findings_view as fv  # noqa: E402

NOTE = """---
tags:
- not-a-finding
---
# Findings

- serve/runs.py:120: the cap is off by one and
  drops the last row (seen while reviewing ABC-1)
- `scripts/a.py:3`: leaks a file handle (seen while planning ABC-1).

Some prose, not a finding.
  indented after prose: not one either
* no path here
"""

AGENT = {"version": 1, "findings": [
    {"id": "f-0123456789ab", "category": "bug", "severity": "high", "title": "Race <in> x", "detail": "two writers",
     "path": "a/b.py", "line": 5, "found_at": "2026-11-03T10:00:00Z", "project": "/p/proj", "extra": [1]},
    {"id": "f-aaaaaaaaaaaa", "category": "naming-stuff", "title": "older one", "found_at": "2026-11-01T10:00:00Z"},
    {"id": "bad id", "title": "skipped: not an id"},
    {"id": "f-bbbbbbbbbbbb"},   # no title, no detail: skipped
    "not a dict",
]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.queue = os.path.join(root, "queue")
        self.vault = os.path.join(root, "vault")
        self.notes = os.path.join(self.vault, "wiki", "notes", "tickets")
        os.makedirs(os.path.join(self.queue, "ABC-1"))
        os.makedirs(os.path.join(self.queue, "ABC-2"))
        os.makedirs(self.notes)
        os.makedirs(os.path.join(root, "findings"))
        with open(os.path.join(self.queue, "queue.list"), "w") as f:
            f.write("ABC-1\n")
        with open(os.path.join(self.queue, "archived.list"), "w") as f:
            f.write("ABC-2\n")
        self.note("ABC-1", NOTE)
        self.note("ABC-2", "- x/y.go:9: an archived ticket's finding (seen while fixing ABC-2)\n")
        self.agent(AGENT)
        self.runs = runs.Runs([], self.queue, self.vault)
        fv._CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def note(self, key, text, age=0):
        p = os.path.join(self.notes, f"{key}-findings.md")
        with open(p, "w") as f:
            f.write(text)
        t = time.time() - age
        os.utime(p, (t, t))

    def agent(self, data):
        with open(os.path.join(self.tmp.name, "findings", "findings.json"), "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def board(self):
        with open(os.path.join(self.tmp.name, "findings", "board.json")) as f:
            return json.load(f)

    def ids(self):
        return [f["id"] for f in fv.collect(self.runs)]

    def open_ids(self):
        b = fv.read_board(fv.board_path(self.runs))
        return [f["id"] for f in fv.arrange(fv.collect(self.runs), b)[0]]


class Parse(Base):
    def test_note_items(self):
        got = fv.parse_note(NOTE)
        self.assertEqual(got, ["serve/runs.py:120: the cap is off by one and drops the last row "
                               "(seen while reviewing ABC-1)",
                               "`scripts/a.py:3`: leaks a file handle (seen while planning ABC-1).",
                               "no path here"])

    def test_split(self):
        self.assertEqual(fv._split("serve/runs.py:120: the cap (seen while reviewing ABC-1)"),
                         ("serve/runs.py", 120, "the cap", "review"))
        self.assertEqual(fv._split("**`a/b.py:7-9`**: x"), ("a/b.py", 7, "x", ""))
        self.assertEqual(fv._split("Note:12 is not a path"), ("", 0, "Note:12 is not a path", ""))

    def test_id_survives_rewrap(self):
        self.assertEqual(fv.ticket_id("ABC-1", "a  b\nc"), fv.ticket_id("ABC-1", "a b c"))
        self.assertNotEqual(fv.ticket_id("ABC-1", "a b c"), fv.ticket_id("ABC-2", "a b c"))
        self.assertTrue(re.match(r"^t-[0-9a-f]{12}$", fv.ticket_id("ABC-1", "x")))
        before = self.ids()
        self.note("ABC-1", NOTE.replace("off by one and\n  drops", "off by one\n    and drops"))
        self.assertEqual(self.ids(), before)

    def test_collect(self):
        got = {f["id"]: f for f in fv.collect(self.runs)}
        self.assertEqual(len(got), 6)   # 3 + 1 from the notes (an archived ticket's too), 2 of the agent's
        a = got["f-0123456789ab"]
        self.assertEqual((a["src"], a["cat"], a["sev"], a["path"], a["line"]), ("agent", "bug", "high", "a/b.py", 5))
        self.assertEqual(a["md"], "- a/b.py:5: Race <in> x: two writers (bug, high, findings agent)")
        t = [f for f in got.values() if f["key"] == "ABC-2"][0]
        self.assertEqual((t["stage"], t["path"], t["line"]), ("fix", "x/y.go", 9))
        nopath = [f for f in got.values() if f["title"] == "no path here"][0]
        self.assertEqual(nopath["md"], "- no path here (ABC-1)")

    def test_bad_agent_file(self):
        for bad in ("{not json", "[]", '{"findings": {}}', '{"findings": [{"id": 3}]}'):
            self.agent(bad)
            fv._CACHE.clear()
            self.assertEqual([f for f in fv.collect(self.runs) if f["src"] == "agent"], [], bad)
        os.remove(os.path.join(self.tmp.name, "findings", "findings.json"))
        self.assertEqual(len(fv.collect(self.runs)), 4)

    def test_ticket_vault(self):
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(os.path.join(other, "wiki", "notes", "tickets"))
        with open(os.path.join(other, "wiki", "notes", "tickets", "ABC-1-findings.md"), "w") as f:
            f.write("- in/the/ticket.vault:1: here\n")
        with open(os.path.join(self.queue, "ABC-1", "state.json"), "w") as f:
            json.dump({"vault": other}, f)
        titles = [f["title"] for f in fv.collect(self.runs) if f["key"] == "ABC-1"]
        self.assertEqual(titles, ["here"])

    def test_no_queue(self):
        self.assertEqual(fv.collect(runs.Runs([], "", self.vault)), [])
        self.assertIn("No findings yet", fv.findings_region(runs.Runs([], "", self.vault)))


class Board(Base):
    def test_order_newest_first(self):
        self.note("ABC-2", "- x/y.go:9: old (seen while fixing ABC-2)\n", age=86400 * 30)
        got = fv.arrange(fv.collect(self.runs), fv.read_board(""))[0]
        # ABC-1's note is the newest: its last bullet first; then the agent's by found_at; then ABC-2's
        self.assertEqual([f["title"] for f in got],
                         ["no path here", "leaks a file handle", "the cap is off by one and drops the last row",
                          "Race <in> x", "older one", "old"])

    def test_archive_restore(self):
        ids = self.open_ids()
        ok, msg = fv.action(self.runs, "archive", {"sel": ids[:2], "back": ["queue"]})
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "Archived 2 findings.")
        self.assertEqual(set(self.board()["archived"]), set(ids[:2]))
        self.assertEqual(self.open_ids(), ids[2:])
        # a row's own button (id) wins over what is ticked (sel)
        ok, msg = fv.action(self.runs, "restore", {"id": [ids[0]], "sel": [ids[3]]})
        self.assertEqual((ok, msg), (True, "Restored 1 finding."))
        self.assertEqual(set(self.board()["archived"]), {ids[1]})
        self.assertEqual(fv.action(self.runs, "archive", {})[0], False)
        self.assertEqual(fv.action(self.runs, "archive", {"id": ["f-gonegonegone"]})[0], False)

    def test_move(self):
        ids = self.open_ids()
        ok, msg = fv.action(self.runs, "move", {"key": [ids[-1]], "before": [ids[0]]})
        self.assertTrue(ok, msg)
        self.assertEqual(self.open_ids(), [ids[-1]] + ids[:-1])
        ok, _ = fv.action(self.runs, "move", {"key": [ids[-1]], "before": [""]})   # to the end
        self.assertTrue(ok)
        self.assertEqual(self.open_ids(), ids)
        self.assertEqual(self.board()["order"], ids)
        self.assertFalse(fv.action(self.runs, "move", {"key": ["f-nope"], "before": [""]})[0])
        self.assertFalse(fv.action(self.runs, "move", {"key": [ids[0]], "before": ["f-nope"]})[0])

    def test_new_after_order_goes_last_and_is_marked(self):
        ids = self.open_ids()
        fv.action(self.runs, "move", {"key": [ids[1]], "before": [ids[0]]})
        self.note("ABC-1", NOTE + "- new/one.py:1: a new one\n")
        got = self.open_ids()
        self.assertEqual(len(got), len(ids) + 1)
        new = [i for i in got if i not in ids][0]
        self.assertEqual(got[-1], new)
        html = fv.findings_region(self.runs)
        self.assertEqual(html.count(">new</span>"), 1)

    def test_prune(self):
        ids = self.open_ids()
        gone = [f["id"] for f in fv.collect(self.runs) if f["key"] == "ABC-2"][0]
        fv.action(self.runs, "archive", {"id": [gone]})
        fv.action(self.runs, "move", {"key": [ids[0]], "before": [""]})
        self.assertIn(gone, self.board()["order"])
        os.remove(os.path.join(self.notes, "ABC-2-findings.md"))
        fv.action(self.runs, "move", {"key": [ids[1]], "before": [""]})
        b = self.board()
        self.assertNotIn(gone, b["order"])       # order: pruned
        self.assertIn(gone, b["archived"])       # archived: kept, it may come back
        self.note("ABC-2", "- x/y.go:9: an archived ticket's finding (seen while fixing ABC-2)\n")
        self.assertNotIn(gone, self.open_ids())

    def test_bad_board_file(self):
        p = fv.board_path(self.runs)
        for bad in ("{", "[]", '{"order": "x", "archived": []}', '{"order": [1, "a b", "ok"], "archived": {"x y": 1}}'):
            with open(p, "w") as f:
                f.write(bad)
            b = fv.read_board(p)
            self.assertIsInstance(b["order"], list)
            self.assertIsInstance(b["archived"], dict)
            self.assertEqual(len(fv.collect(self.runs)), 6)
            self.assertIn("6 open", fv.findings_region(self.runs))

    def test_busy_lock(self):
        lock = os.path.join(self.tmp.name, "findings", "board.lock")
        got = {}
        entered, release = threading.Event(), threading.Event()

        def hold():
            with fv.held(lock):
                entered.set()
                release.wait(10)
        t = threading.Thread(target=hold)
        t.start()
        entered.wait(5)
        try:
            # flock is per open file: another open of the lock file in this process waits too
            start = time.time()
            with self.assertRaises(TimeoutError):
                with fv.held(lock, wait=0.3):
                    got["in"] = True
            self.assertGreater(time.time() - start, 0.25)
        finally:
            release.set()
            t.join(5)
        self.assertNotIn("in", got)


class Render(Base):
    def test_region(self):
        h = fv.findings_region(self.runs)
        self.assertEqual(h, fv.findings_region(self.runs))   # the same data, the same HTML: no churn
        self.assertIn("id='fboard'", h)
        self.assertIn("data-sort-url='/runs/findings/move'", h)
        self.assertEqual(h.count("class='grip'"), 6)
        self.assertEqual(h.count("data-sort-key="), 6)
        self.assertIn("Race &lt;in&gt; x", h)
        self.assertNotIn("<in>", h)
        self.assertIn("<a href='/runs/ticket/ABC-2'>ABC-2</a> &middot; fix", h)
        self.assertIn("<option value='agent'>findings agent</option>", h)
        self.assertIn("<option value='naming-stuff'>naming-stuff</option>", h)
        self.assertIn("<code>serve/runs.py:120</code>", h)
        self.assertNotIn("findings-archived", h)

    def test_archived_folded(self):
        ids = self.open_ids()
        fv.action(self.runs, "archive", {"id": [ids[0]]})
        h = fv.findings_region(self.runs)
        self.assertIn("5 open &middot; 1 archived", h)
        self.assertIn("<details class='farch' data-k='findings-archived'>", h)
        arch = h[h.index("findings-archived"):]
        self.assertIn(f"value='{ids[0]}' formaction='/runs/findings/restore'", arch)
        self.assertEqual(arch.count("class='grip'"), 0)

    def test_cap(self):
        self.note("ABC-1", "".join(f"- f/{i}.py:{i}: number {i}\n" for i in range(fv.OPEN_CAP + 7)))
        h = fv.findings_region(self.runs)
        self.assertEqual(h.count("class='grip'"), fv.OPEN_CAP)
        self.assertIn("more not shown", h)

    def test_all_archived(self):
        fv.action(self.runs, "archive", {"sel": self.open_ids()})
        h = fv.findings_region(self.runs)
        self.assertIn("Nothing open", h)
        self.assertNotIn("data-sort-url", h)


class Routes(Base):
    def setUp(self):
        super().setUp()
        data = os.path.join(self.tmp.name, "data")
        os.makedirs(data)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = srv.server_address[1]
        cfg = pub.Config(self.port, self.vault, "", 0, "", (), agent_data=[("host", data)], queue_dir=self.queue)
        srv.RequestHandlerClass = pub.make_handler(cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.srv = srv

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, path, fields=(), origin=None, live=False):
        h = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/x-www-form-urlencoded",
             "Origin": origin or f"http://127.0.0.1:{self.port}"}
        if live:
            h["X-Harness-Live"] = "1"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, urlencode(list(fields)), h)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r, body

    def test_archive_redirects_to_the_board(self):
        ids = self.open_ids()
        r, _ = self.post("/runs/findings/archive", [("sel", ids[0]), ("sel", ids[1]), ("back", "queue")])
        self.assertEqual(r.status, 303)
        loc = urlsplit(r.getheader("Location"))
        self.assertEqual((loc.path, loc.fragment), ("/runs/queue", "findings"))
        self.assertEqual(parse_qs(loc.query)["done"], ["Archived 2 findings."])
        self.assertEqual(set(self.board()["archived"]), set(ids[:2]))
        r, body = self.post("/runs/findings/restore", [("id", ids[0])])
        self.assertEqual(r.status, 303)
        self.assertEqual(set(self.board()["archived"]), {ids[1]})

    def test_refused(self):
        r, body = self.post("/runs/findings/archive", [("back", "queue")])
        self.assertEqual(r.status, 409)
        self.assertIn("no finding picked", body)
        r, _ = self.post("/runs/findings/nope", [])
        self.assertEqual(r.status, 404)
        r, _ = self.post("/runs/findings/archive", [("id", self.open_ids()[0])], origin="http://evil.example")
        self.assertEqual(r.status, 403)
        self.assertFalse(os.path.exists(fv.board_path(self.runs)))

    def test_move_live(self):
        ids = self.open_ids()
        r, body = self.post("/runs/findings/move", [("key", ids[2]), ("before", ids[0])], live=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "msg": "Moved."})
        self.assertEqual(self.open_ids()[:3], [ids[2], ids[0], ids[1]])
        r, body = self.post("/runs/findings/move", [("key", "f-nope"), ("before", "")], live=True)
        self.assertEqual(r.status, 409)
        self.assertFalse(json.loads(body)["ok"])

    def test_queue_page_has_the_script(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/runs/queue", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertIn("function newTicket(list)", body)


if __name__ == "__main__":
    unittest.main()
