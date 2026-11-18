"""Tests for the Changes page's "Show more lines" (serve/diff_view.py): the lines route
/runs/ticket/KEY/lines against a live server, and the browser's hunk arithmetic (dvHunks)
under node, checked against diff2html's own numbering.

Run: python3 -m pytest serve/test_diff_view.py -q
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diff_view  # noqa: E402
from test_runs import RunsServer, git_repo  # noqa: E402

LINES = [f"line {i}" for i in range(1, 101)]


class LinesRouteTest(RunsServer):
    """A file of 100 lines; the ticket's branch changes line 10, puts 2 lines after line 40
    and changes line 90 (92 on the new side): three hunks, the last two 2 lines apart."""

    def git(self, d, *a):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", d, *a], check=True, env=env, capture_output=True, text=True).stdout.strip()

    def write(self, name, rows, d=None):
        with open(os.path.join(d or self.proj, name), "w", newline="") as f:
            f.write("".join(r + "\n" for r in rows))

    def setUp(self):
        super().setUp()
        proj = self.proj = git_repo(os.path.join(self.tmp.name, "proj-git"))
        self.write("f.txt", LINES)
        self.write("bin.dat", ["x"])
        self.git(proj, "add", ".")
        self.git(proj, "commit", "-qm", "base")
        self.base = self.git(proj, "rev-parse", "HEAD")
        self.git(proj, "checkout", "-qb", "ABC-12")
        self.new = LINES[:9] + ["changed 10"] + LINES[10:40] + ["new a", "new b"] + LINES[40:89] + ["changed 90"] \
            + LINES[90:]
        self.write("f.txt", self.new)
        self.write("other.txt", ["fresh"])
        self.git(proj, "add", ".")
        self.git(proj, "commit", "-qm", "work")
        self.tip = self.git(proj, "rev-parse", "HEAD")
        self.git(proj, "checkout", "-q", "main")
        # uncommitted on main: line 50 changed, with CRLF endings in the folder
        rows = LINES[:49] + ["dirty 50"] + LINES[50:]
        with open(os.path.join(proj, "f.txt"), "w", newline="") as f:
            f.write("".join(r + "\r\n" for r in rows))
        self.ticket(project=proj, in_place=True, base_sha=self.base, status="done", questions=[])
        self.start()

    def lines(self, code=200, **q):
        q.setdefault("repo", ".")
        q.setdefault("view", "work")
        q.setdefault("rev", self.tip)
        q.setdefault("path", "f.txt")
        r, body = self.req("GET", "/runs/ticket/ABC-12/lines?" + urlencode(q))
        self.assertEqual(r.status, code, body)
        self.assertTrue(r.getheader("Content-Type").startswith("application/json"))
        return json.loads(body)

    def test_the_page_names_the_commit_it_diffed(self):
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff")
        self.assertIn(f"<div id='d2h' data-lines='/runs/ticket/ABC-12/lines?repo=.&amp;view=work&amp;rev={self.tip}'>",
                      body)
        self.assertIn("var dvHunks", body)
        r, body = self.req("GET", "/runs/ticket/ABC-12/diff?view=uncommitted")
        self.assertIn("data-lines='/runs/ticket/ABC-12/lines?repo=.&amp;view=uncommitted&amp;rev='", body)

    def test_new_side_lines_at_the_commit_diffed(self):
        got = self.lines(**{"from": 20, "to": 25})
        self.assertEqual(got, {"lines": LINES[19:25], "total": 102})
        got = self.lines(**{"from": 60, "to": 61})   # after the 2 new lines: old 58, 59
        self.assertEqual(got["lines"], ["line 58", "line 59"])
        got = self.lines(**{"from": 100, "to": 120})   # past the end: what there is
        self.assertEqual(got, {"lines": ["line 98", "line 99", "line 100"], "total": 102})
        self.assertEqual(self.lines(**{"from": 103, "to": 110}), {"lines": [], "total": 102})
        # the branch moves on: the page's commit still reads as it was; a later one too
        self.git(self.proj, "checkout", "-qf", "ABC-12")
        self.write("f.txt", ["top"] + self.new)
        self.git(self.proj, "commit", "-qam", "more")
        later = self.git(self.proj, "rev-parse", "HEAD")
        self.git(self.proj, "checkout", "-q", "main")
        self.assertEqual(self.lines(**{"from": 20, "to": 20})["lines"], ["line 20"])
        self.assertEqual(self.lines(rev=later, **{"from": 20, "to": 20})["lines"], ["line 19"])

    def test_uncommitted_reads_the_folder(self):
        got = self.lines(view="uncommitted", rev="", **{"from": 45, "to": 52})
        self.assertEqual(got["lines"], LINES[44:49] + ["dirty 50"] + LINES[50:52])   # no \r
        self.assertEqual(got["total"], 100)

    def test_refusals(self):
        one = {"from": 1, "to": 5}
        for q, why in ((dict(path="../f.txt"), "not a file this diff shows"),
                       (dict(path="/etc/passwd"), "not a file this diff shows"),
                       (dict(path="a.txt"), "not a file this diff shows"),      # not in the diff
                       (dict(path="other.txt"), "not a file this diff shows"),  # new: nothing around its hunk
                       (dict(path=".env"), "not a file this diff shows"),       # a secret's name
                       (dict(repo="../../etc"), "not a repository of this ticket"),
                       (dict(repo="libs/none"), "not a repository of this ticket"),
                       (dict(view="nope"), "no such view"),
                       (dict(rev="main"), "rev is not a commit"),
                       (dict(rev="HEAD~1"), "rev is not a commit"),
                       (dict(rev=self.base), "rev is not a commit of this view"),   # not the tip nor before it
                       (dict(rev="0" * 40), "rev is not a commit of this view")):
            self.assertEqual(self.lines(400, **one, **q)["error"], why, q)
        for lo, hi in ((0, 5), (5, 4), (1, diff_view.LINES_CAP + 1), ("x", 2), (-3, 2)):
            self.assertIn("error", self.lines(400, **{"from": lo, "to": hi}))
        self.assertEqual(self.lines(**{"from": 1, "to": diff_view.LINES_CAP})["total"], 102)
        r, _ = self.req("GET", "/runs/ticket/ABC-99/lines?from=1&to=2")
        self.assertEqual(r.status, 404)

    def test_a_secret_file_and_a_binary_one(self):
        self.git(self.proj, "checkout", "-qf", "ABC-12")
        self.write("server.key", LINES)
        self.git(self.proj, "add", "server.key")
        self.git(self.proj, "commit", "-qm", "key")
        self.write("server.key", ["x"] + LINES)
        with open(os.path.join(self.proj, "bin.dat"), "wb") as f:
            f.write(b"\0\1\2\nmore\n")
        self.git(self.proj, "commit", "-qam", "both")
        tip = self.git(self.proj, "rev-parse", "HEAD")
        self.git(self.proj, "checkout", "-q", "main")
        self.assertEqual(self.lines(400, rev=tip, path="server.key", **{"from": 1, "to": 2})["error"],
                         "not a file this diff shows")
        self.assertEqual(self.lines(400, rev=tip, path="bin.dat", **{"from": 1, "to": 2})["error"], "a binary file")

    def test_a_symlink_in_the_folder_is_not_followed(self):
        if os.name == "nt":
            self.skipTest("symlinks")
        os.remove(os.path.join(self.proj, "f.txt"))
        os.symlink("/etc/hostname", os.path.join(self.proj, "f.txt"))
        self.assertEqual(self.lines(400, view="uncommitted", rev="", **{"from": 1, "to": 2})["error"],
                         "not a file this diff shows")

    def test_a_repository_inside_the_project(self):
        inner = git_repo(os.path.join(self.proj, "libs", "inner"))
        self.write("g.txt", LINES, inner)
        self.git(inner, "add", "g.txt")
        self.git(inner, "commit", "-qm", "g")
        self.write("g.txt", LINES[:70] + ["x"] + LINES[71:], inner)
        got = self.lines(repo="libs/inner", view="uncommitted", rev="", path="g.txt", **{"from": 1, "to": 3})
        self.assertEqual(got, {"lines": LINES[:3], "total": 100})
        self.assertEqual(self.lines(400, repo="libs/inner", view="work", rev="", path="g.txt",
                                    **{"from": 1, "to": 3})["error"], "no such view")


NODE = shutil.which("node")
D2H = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "diff2html", "diff2html.min.js")
HEAD_RE = re.compile(r"^@@ -\S+ \+\S+ @@", re.M)


@unittest.skipUnless(NODE, "node is not installed")
class HunksTest(unittest.TestCase):
    """dvHunks (the page's own code) adds lines to a git diff as the page does; diff2html numbers
    the result."""

    def diff(self, old, new, *opts):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        for name, rows in (("a", old), ("b", new)):
            with open(os.path.join(t.name, name), "w") as f:
                f.write("".join(r + "\n" for r in rows))
        self.old, self.new = old, new
        return subprocess.run(["git", "diff", "--no-index", "--no-color", *opts, "a", "b"], cwd=t.name,
                              capture_output=True, text=True).stdout

    def grow(self, diff, steps):
        """steps: [(hunk index, n)], each as a click on Show n more lines there: the lines it
        asks for (span), read from the new file as the server does, added (add)."""
        script = (f"global.window = global; var Diff2Html = require({json.dumps(D2H)});\n"
                  + diff_view.HUNKS_JS +
                  f"var text = {json.dumps(diff)}, steps = {json.dumps(steps)}, NEW = {json.dumps(self.new)};\n"
                  "var spans = steps.map(function (s) {\n"
                  "  var f = dvHunks.cut(text), sp = dvHunks.span(f, s[0], s[1]);\n"
                  "  text = dvHunks.glue(dvHunks.add(f, s[0], NEW.slice(sp[0] - 1, sp[1])));\n"
                  "  return sp;\n"
                  "});\n"
                  "var file = Diff2Html.parse(text)[0];\n"
                  "console.log(JSON.stringify({spans: spans, text: text, rows: [].concat.apply([], file.blocks.map("
                  "function (b) { return b.lines.map(function (l) { return [l.oldNumber || 0, l.newNumber || 0, "
                  "l.content]; }); }))}));\n")
        p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = json.loads(p.stdout)
        for o, n, content in got["rows"]:   # each line at the number each side has it at
            if content[:1] in " +":
                self.assertEqual(content[1:], self.new[n - 1], (o, n, content))
            if content[:1] in " -":
                self.assertEqual(content[1:], self.old[o - 1], (o, n, content))
        return got

    def setUp(self):
        old = [str(i) for i in range(1, 61)]
        # line 5 changed, 2 lines put after 20, line 30 gone, line 50 changed: 61 lines
        new = old[:4] + ["five"] + old[5:20] + ["n1", "n2"] + old[20:29] + old[30:49] + ["fifty"] + old[50:]
        self.text = self.diff(old, new)

    def test_hunks_with_offsets_numbered_on_both_sides(self):
        self.assertEqual(HEAD_RE.findall(self.grow(self.text, [])["text"]),
                         ["@@ -2,7 +2,7 @@", "@@ -18,6 +18,8 @@", "@@ -27,7 +29,6 @@", "@@ -47,7 +48,7 @@"])
        # 10 more above the fourth: new 38..47 (old 37..46), the third's lines end at new 34
        got = self.grow(self.text, [(3, 10)])
        self.assertEqual(got["spans"], [[38, 47]])
        self.assertEqual(HEAD_RE.findall(got["text"])[2:], ["@@ -27,7 +29,6 @@", "@@ -37,17 +38,17 @@"])

    def test_the_lines_between_two_hunks_join_them(self):
        got = self.grow(self.text, [(2, 10)])   # new 26..28, all that is between the second and third
        self.assertEqual(got["spans"], [[26, 28]])
        self.assertEqual(HEAD_RE.findall(got["text"]), ["@@ -2,7 +2,7 @@", "@@ -18,16 +18,17 @@", "@@ -47,7 +48,7 @@"])

    def test_the_whole_file_one_click_at_a_time(self):
        got = self.grow(self.text, [(1, 10), (0, 10), (3, 10), (1, 10), (1, 10), (1, 10)])
        self.assertEqual(got["spans"], [[9, 17], [1, 1], [55, 64], [26, 28], [38, 47], [35, 37]])
        self.assertEqual(HEAD_RE.findall(got["text"]), ["@@ -1,60 +1,61 @@"])
        self.assertEqual([n for o, n, c in got["rows"] if n], list(range(1, 62)))
        self.assertEqual([o for o, n, c in got["rows"] if o and c[0] == " "], [
            o for o in range(1, 61) if o not in (5, 30, 50)])

    def test_a_hunk_with_no_lines_on_one_side(self):
        text = self.diff([str(i) for i in range(1, 31)], [str(i) for i in range(1, 16)] + ["x"]
                         + [str(i) for i in range(16, 31)], "-U0")
        self.assertEqual(HEAD_RE.findall(text), ["@@ -15,0 +16 @@"])
        got = self.grow(text, [(0, 10), (1, 10)])
        self.assertEqual(got["spans"], [[6, 15], [17, 26]])
        self.assertEqual(HEAD_RE.findall(got["text"]), ["@@ -6,20 +6,21 @@"])


if __name__ == "__main__":
    unittest.main()
