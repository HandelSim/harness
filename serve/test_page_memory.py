"""Page memory (page_memory.py): every run viewer page carries it, and it parses."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import urlencode

import page_memory
import runs as runs_view
import test_runs

MARK = "var P = 'hmem:'"


class MemoryScript(unittest.TestCase):
    def test_status_and_post_are_filled_in(self):
        s = page_memory.js(409, True)
        self.assertIn("var CODE = 409, POST = 1,", s)
        self.assertNotIn("%", s.split("var P")[0])
        self.assertIn("var CODE = 200, POST = 0,", page_memory.js("200"))
        self.assertNotIn("%CODE%", s)
        self.assertNotIn("%POST%", s)

    def test_no_inline_handler_or_eval(self):
        s = page_memory.MEMORY_JS
        for bad in ("eval(", "new Function", "innerHTML", "onclick", "</script"):
            self.assertNotIn(bad, s)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_it_parses(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mem.js")
            with open(p, "w") as f:
                f.write(page_memory.js(200, False))
            r = subprocess.run(["node", "--check", p], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)


class EveryPage(test_runs.RunsServer):
    def script(self, body):
        """The page's one nonce'd script: memory first, then COMMON_JS."""
        got = re.findall(r"<script nonce='[^']+'>(.*?)</script>", body, re.S)
        self.assertEqual(len(got), 1, body[-400:])
        s = got[0]
        self.assertIn(MARK, s)
        self.assertLess(s.index(MARK), s.index(runs_view.COMMON_JS[:200]))
        return s

    def test_every_run_viewer_page_carries_it(self):
        self.ticket()
        self.start()
        for path in ("/", "/runs", "/runs/queue", "/runs/ticket/ABC-12", "/runs/ticket/ABC-12/edit",
                     "/runs/ticket/ABC-12/diff", f"/runs/{test_runs.ROOT}", "/vault", "/settings",
                     "/vault/browse/no/such-note.md"):
            r, body = self.req("GET", path)
            with self.subTest(path=path):
                self.assertIn("var CODE = %d, POST = 0," % r.status, self.script(body))

    def test_a_refused_send_is_marked_so_its_draft_stays(self):
        self.ticket()
        self.start()
        h = {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{self.port}"}
        r, body = self.req("POST", "/runs/queue/add", urlencode([("keys", "ABC-7"), ("project", "relative")]), h)
        self.assertEqual(r.status, 409, body)
        self.assertIn("var CODE = 409, POST = 1,", self.script(body))


if __name__ == "__main__":
    unittest.main()
