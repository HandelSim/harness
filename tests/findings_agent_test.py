"""Tests for scripts/findings_agent.py with no model, no opencode and no docker:
throwaway git repositories, a fake proxy (an HTTP server that serves /v1/models
for the free-model guard) and a stub `harness` script that stands in for
`harness host --read-only -p`: it prints the session line and the answer the
test asks for.

  python3 tests/findings_agent_test.py      (tests/unit_findings_test.sh runs it)
"""
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "findings_agent.py")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import findings_agent as fa  # noqa: E402

FREE = {"id": "free-assist", "lane": "assist", "credit_weights": {"input": 0, "output": 0},
        "capabilities": {"chat": True}}
METERED = {"id": "paid-model", "lane": "default", "credit_weights": {"input": 3, "output": 15},
           "capabilities": {"chat": True}}
APP = "import os\n\n\ndef ratio(a, b):\n    # the caller checks b\n    return a / b\n\n\ndef total(items):\n" \
      "    return sum(items)\n\n\nLABEL = \"Compute Ratio\"\n"
USE = "from app import ratio\nprint(ratio(1, 0))\n"

# The stub harness: logs how it was run, then answers as STUB_MODE says (a checking
# session's prompt has VERDICT-<nonce>: it answers STUB_VERDICT for each report).
STUB = r"""#!/usr/bin/env bash
prompt="${@: -1}"
nonce=$(grep -o 'COVERED-[0-9a-f]\{8\}' <<<"$prompt" | head -1 | cut -d- -f2)
vnonce=$(grep -o 'VERDICT-[0-9a-f]\{8\}' <<<"$prompt" | head -1 | cut -d- -f2)
{ printf 'args=%s\n' "${*:1:$(($# - 1))}"; printf 'cwd=%s\n' "$PWD"; printf 'pass=%s\n' "${HARNESS_FINDINGS_PASS:-}"
  [[ -e .env ]] && echo "env=present"; } >>"$STUB_LOG"
printf '%s\n' "$prompt" >"$STUB_LOG.prompt"
session() { echo "[harness] session: ses_stub${HARNESS_FINDINGS_PASS:-0}" >&2; }
if [[ -n "$vnonce" && "${STUB_MODE:-find}" =~ ^(find|none)$ ]]; then
  session
  n=$(grep -c '^\[[0-9]*\] ' <<<"$prompt")
  [[ "${STUB_VERDICT:-real}" == silent ]] && { echo "I could not decide."; exit 0; }
  for i in $(seq 1 "$n"); do echo "VERDICT-$vnonce | $i | ${STUB_VERDICT:-real} | checked the code"; done
  exit 0
fi
case "${STUB_MODE:-find}" in
  find) session
        echo "I followed ratio to its callers."
        echo "**BUG-$nonce**"
        echo "- **title:** Compute Ratio crashes when the divisor is zero"
        echo "- severity: high"
        echo "- where: \`app.py:6\`"
        echo "- evidence: \`return a / b\`"
        echo "- ui: Compute Ratio..."
        echo "- steps:"
        echo "  1. Start the application"
        echo "  2. Click Compute Ratio with a divisor of 0"
        echo "- expected: a message says the divisor cannot be 0"
        echo "- actual: the application crashes with ZeroDivisionError"
        echo "- trace:"
        echo "  - lib/use.py:2 | print(ratio(1, 0))"
        echo "  - app.py:6 | return a / b"
        echo "- why: nothing checks b; lib/use.py calls ratio(1, 0)."
        echo "END-BUG-$nonce"
        echo "BUG-$nonce"
        echo "title: made up"
        echo "where: app.py:6"
        echo "evidence: return a / b"
        echo "ui: A Button Nobody Has"
        echo "steps:"
        echo "1. a"
        echo "2. b"
        echo "expected: x"
        echo "actual: y"
        echo "trace:"
        echo "- app.py:6 | return a / b"
        echo "- lib/use.py:2 | print(ratio(1, 0))"
        echo "END-BUG-$nonce"
        echo "ITEM-$nonce | unify | app.py:10, lib/use.py:2 | return sum(items) | use math.fsum in both"
        echo "ITEM-$nonce | safety | app.py:6 | return a / b | b = 0 raises"
        echo "ITEM-$nonce | nonsense | app.py:1 | import os | x"
        echo "ITEM-$nonce | safety | app.py:4 | not in the code at all | x"
        echo "KNOWLEDGE-$nonce"
        echo "# repo: ./"
        echo "app.py holds ratio(a, b), which divides a by b and leaves the check of b to its callers,"
        echo "and total(items), which sums a list. Entry points: none of its own; lib/use.py imports ratio."
        echo "Traced: lib/use.py -> ratio(1, 0) -> ZeroDivisionError at import time. Trap: b is never checked."
        echo "Left to look at: whether anything else ever calls total."
        echo "END-KNOWLEDGE-$nonce"
        echo "KNOWLEDGE-$nonce lib/"
        echo "lib/use.py is a script, not a module: importing it runs print(ratio(1, 0)), which raises at once."
        echo "It depends on app.py being importable from the working folder; nothing else in the repository"
        echo "imports it. When changing ratio, this is the one caller that passes a zero divisor on purpose."
        echo "END-KNOWLEDGE-$nonce"
        echo "KNOWLEDGE-$nonce nowhere"
        echo "a note on an area the repository does not have: dropped, however long it is ............"
        echo "COVERED-$nonce | app.py and its callers in lib/" ;;
  none) session; echo "COVERED-$nonce | looked, nothing" ;;
  noend) session; echo "I ran out of steps" ;;
  down) echo "HTTP 502: bad gateway"; exit 1 ;;
  hang) session; sleep 60 ;;
  busy) session; for i in 1 2 3 4 5; do echo '{"type":"tool_use"}' >>"$HARNESS_HOST_EVENTS_FILE"; sleep 1; done
        echo "COVERED-$nonce | worked a while" ;;
  rate) session; echo "[harness] opencode error: HTTP 429 Too Many Requests, try again in 7 seconds"; exit 1 ;;
esac
"""


class Proxy(http.server.BaseHTTPRequestHandler):
    """/v1/models from `catalog`."""
    catalog = [FREE, METERED]

    def log_message(self, *a):
        pass

    def do_GET(self):
        ok = self.path.rstrip("/").endswith("/models")
        body = json.dumps({"object": "list", "data": Proxy.catalog} if ok else {"error": "no"}).encode()
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


def make_repo(files):
    root = tempfile.mkdtemp(prefix="fa-repo-")
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    commit(root, files)
    return os.path.realpath(root)


def commit(root, files):
    for path, text in files.items():
        full = os.path.join(root, path)
        if text is None:
            os.remove(full)
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "c", "--allow-empty")


def files_of(spec):
    return {p: {"kind": k, "blob": "b-" + p} for p, k in spec.items()}


BUG_TEXT = """Here is what I found:
**BUG-ab12cd34**
- **Title:** Save As loses the file name
- **severity**: High
- **where:** `src/save.cpp:12`
- evidence: `name = QString();`
- ui: "File > Save As..."
- steps:
  1. Open a project
  2. Choose File > Save As...
     and type a name
- expected: the file is saved
- actual: nothing is written
- trace:
  - src/menu.cpp:4 | connect(saveAs, &QAction::triggered, this, &W::saveAs);
  - src/save.cpp:12 | name = QString();
- why: the name is cleared
  before it is used.
END-BUG-ab12cd34
BUG-ffffffff
title: another nonce
END-BUG-ffffffff
ITEM-ab12cd34 | unify | a.cpp:10, b.cpp:42 | v = clamp(v, lo, hi) | use std::clamp
- `ITEM-ab12cd34 | Unused | old/x.cpp | | nothing names it`
ITEM-ab12cd34 | short
\x1b[0mCOVERED-ab12cd34 | the | save path
"""

LINES = {"src/save.cpp": ["// save"] * 11 + ["    name = QString();", "    write(name);"],
         "src/menu.cpp": ["#include <QMenu>", "", "void W::setup() {",
                          "    connect(saveAs, &QAction::triggered, this, &W::saveAs);", "}"],
         "a.cpp": ["int f() {"] + ["  // a"] * 8 + ["  v = clamp(v, lo, hi);"],
         "b.cpp": ["  // b"] * 50}


def lines_of(path):
    path = path.lstrip("./")
    return path, LINES.get(path)


class ParseTest(unittest.TestCase):
    def test_blocks_items_and_covered(self):
        bugs, items, covered = fa.parse_session(BUG_TEXT, "ab12cd34")
        (b,) = bugs
        self.assertEqual((b["title"], b["severity"], b["where"], b["ui"]),
                         ("Save As loses the file name", "High", "`src/save.cpp:12`", '"File > Save As..."'))
        self.assertEqual(b["steps"], ["Open a project", "Choose File > Save As... and type a name"])
        self.assertEqual(len(b["trace"]), 2)
        self.assertEqual(b["why"], "the name is cleared before it is used.")
        self.assertEqual([i[:2] for i in items], [("unify", "a.cpp:10, b.cpp:42"), ("unused", "old/x.cpp")])
        self.assertEqual(covered, "the / save path")

    def test_no_covered_line_is_an_unfinished_session(self):
        self.assertEqual(fa.parse_session("I think the code is fine.", "ab12cd34"), ([], [], None))
        self.assertEqual(fa.parse_session("", "ab12cd34"), ([], [], None))
        self.assertEqual(fa.parse_session("COVERED-ab12cd34 | x", "ab12cd34"), ([], [], "x"))

    def test_verdicts(self):
        text = ("VERDICT-ab12cd34 | 1 | Not real | a guard in open()\n**VERDICT-ab12cd34** | [2] | REAL | yes\n"
                "VERDICT-ab12cd34 | 3 | maybe | ?\nVERDICT-ffffffff | 4 | real | other nonce\n"
                "- VERDICT-ab12cd34 | 5 | duplicate | f-1 says it")
        self.assertEqual(fa.parse_verdicts(text, "ab12cd34"),
                         {1: ("not-real", "a guard in open()"), 2: ("real", "yes"), 5: ("duplicate", "f-1 says it")})



class KnowledgeTest(unittest.TestCase):
    def test_parse_blocks(self):
        text = ("Some talk.\n**KNOWLEDGE-ab12cd34**\n```markdown\n# r: ./\nbody one\n```\nEND-KNOWLEDGE-ab12cd34\n"
                "KNOWLEDGE-ab12cd34 | lib/sub/\nbody two\n"
                "KNOWLEDGE-ab12cd34 docs\nbody three\nFINDING-ab12cd34 | bug | low | a.py:1 | t | e | d\n"
                "KNOWLEDGE-ffffffff x\nother nonce\nCOVERED-ab12cd34 | x\n")
        self.assertEqual(fa.parse_knowledge(text, "ab12cd34"),
                         [("", "# r: ./\nbody one"), ("lib/sub", "body two"), ("docs", "body three")])
        self.assertEqual(fa.parse_knowledge("KNOWLEDGE-ab12cd34\nto the end", "ab12cd34"), [("", "to the end")])
        self.assertEqual(fa.parse_knowledge("nothing", "ab12cd34"), [])

    def test_refusal_and_slugs(self):
        good = "app.py does things. " * 20
        self.assertEqual(fa.knowledge_refusal(good, "", ["d/app.py"]), "")
        self.assertIn("too short", fa.knowledge_refusal("app.py", "", ["app.py"]))
        self.assertIn("would replace", fa.knowledge_refusal(good, good * 3, ["app.py"]))
        self.assertIn("names none", fa.knowledge_refusal("x" * 400, "", ["app.py"]))
        self.assertEqual((fa.name_slug("."), fa.name_slug("lib/sub"), fa.name_slug("../x")),
                         ("_top", "lib__sub", "x"))
        self.assertEqual(fa.note_summary("# r: ./\n\n- **app.py** holds ratio\nmore"), "**app.py** holds ratio")


class CheckTest(unittest.TestCase):
    def bug(self, **kw):
        b = fa.parse_session(BUG_TEXT, "ab12cd34")[0][0]
        b.update(kw)
        return b

    def ui(self, t):
        return fa.ui_norm(t.split(">")[-1]) == "save as"

    def test_a_good_report(self):
        c, why = fa.check_bug(self.bug(), lines_of, self.ui)
        self.assertEqual(why, "")
        self.assertEqual((c["path"], c["line"], c["evidence"], c["severity"], c["ui"]),
                         ("src/save.cpp", 12, "name = QString();", "high", "File > Save As..."))
        self.assertEqual([(h["path"], h["line"]) for h in c["trace"]], [("src/menu.cpp", 4), ("src/save.cpp", 12)])
        self.assertEqual(c["detail"], "the name is cleared before it is used.")

    def test_each_check(self):
        good_trace = self.bug()["trace"]
        cases = {
            "format": self.bug(where="somewhere"),
            "evidence": self.bug(evidence="name = made_up();"),
            "ui": self.bug(ui="Export"),
            "steps": self.bug(steps=["only one"]),
            "trace": self.bug(trace=good_trace[:1]),
        }
        for why, b in cases.items():
            self.assertEqual(fa.check_bug(b, lines_of, self.ui), (None, why), why)
        self.assertEqual(fa.check_bug(self.bug(expected=""), lines_of, self.ui), (None, "steps"))
        # one bad hop of three is let go; two are not
        bad = "src/menu.cpp:1 | something else"
        c, _ = fa.check_bug(self.bug(trace=good_trace + [bad]), lines_of, self.ui)
        self.assertEqual(len(c["trace"]), 2)
        self.assertEqual(fa.check_bug(self.bug(trace=good_trace + [bad, bad]), lines_of, self.ui), (None, "trace"))

    def test_items(self):
        ev = " v = clamp(v, lo, hi) |"
        it, why = fa.check_item("unify", "a.cpp:10, b.cpp:42", ev + " use std::clamp", lines_of, LINES.__contains__)
        self.assertEqual((it["path"], it["line"], it["evidence"], it["note"], it["locations"], why),
                         ("a.cpp", 10, "v = clamp(v, lo, hi)", "use std::clamp", ["a.cpp:10", "b.cpp:42"], ""))
        self.assertEqual(fa.check_item("unify", "a.cpp:10", ev + " n", lines_of, LINES.__contains__),
                         (None, "places"))
        self.assertEqual(fa.check_item("safety", "a.cpp:10", " hi = clamp(lo) | n", lines_of, LINES.__contains__),
                         (None, "evidence"))
        self.assertEqual(fa.check_item("safety", "a.cpp:99", ev + " n", lines_of, LINES.__contains__),
                         (None, "evidence"))
        self.assertEqual(fa.check_item("safety", "nowhere", ev + " n", lines_of, LINES.__contains__),
                         (None, "evidence"))
        it, why = fa.check_item("unused", "b.cpp", " | nothing", lines_of, LINES.__contains__)
        self.assertEqual((it["path"], it["locations"], it["note"]), ("b.cpp", ["b.cpp"], "nothing"))
        self.assertEqual(fa.check_item("unused", "gone.cpp", "", lines_of, LINES.__contains__), (None, "evidence"))

    def test_duplicates(self):
        c = fa.check_bug(self.bug(), lines_of, self.ui)[0]
        known = [{"path": "src/save.cpp", "line": 20, "title": "Save As loses the name of the file", "ui": ""}]
        self.assertTrue(fa.duplicate_of(c, known))   # 8 lines off, most words shared
        self.assertFalse(fa.duplicate_of(c, [dict(known[0], line=40)]))
        self.assertFalse(fa.duplicate_of(c, [dict(known[0], title="Crash on open")]))
        # a ticket's finding: its place is in its text; a hop of the trace counts too
        self.assertTrue(fa.duplicate_of(c, [{"path": "", "line": 0, "title": "",
                                             "text": "menu.cpp:5 Save As loses the file name"}]))
        self.assertTrue(fa.duplicate_of(c, [{"path": "src/save.cpp", "line": 400, "ui": "Save As",
                                             "title": "Save As fails"}]))

    def test_ui_texts(self):
        cpp = 'auto *a = new QAction(tr("&Save As..."), this);\nmsg("Hello there");\nQMessageBox::warning(this, "Oops", "Disk full");'
        lits, shown = fa.ui_texts("w.cpp", cpp)
        self.assertIn("&Save As...", shown)
        self.assertNotIn("Hello there", shown)
        self.assertIn("Hello there", lits)   # every literal can be a report's control text; only the shown ones are listed
        lits, shown = fa.ui_texts("w.ui", '<property name="text"><string>Open &amp;File</string></property>'
                                          '<string notr="true">objName</string>')
        self.assertEqual(shown, ["Open &amp;File"])
        self.assertEqual(fa.ui_norm("Open &amp;File"), "open file")
        self.assertEqual(fa.ui_norm('"Save &As…"'), "save as")
        self.assertEqual(fa.ui_norm("Fish && Chips:"), "fish & chips")

    def test_fingerprint_stable_and_whitespace_blind(self):
        a = fa.fingerprint("r", "p.py", "bug", "return  a / b")
        self.assertEqual(a, fa.fingerprint("r", "p.py", "bug", "Return a / b "))
        self.assertRegex(a, r"^f-[0-9a-f]{12}$")
        self.assertNotEqual(a, fa.fingerprint("r", "p.py", "simplify", "return a / b"))

    def test_themes(self):
        got = {k: (t, d) for k, t, d in fa.themes_of(
            "intro text\n## nvector\ntitle: Position math not through the n-vector library\nUse nvlib for all\n"
            "position math.\n## dead\noff\n## Safety\nOnly memory errors.\n## bad slug!!\n\n## empty\n")}
        self.assertEqual(sorted(got), ["bad-slug", "nvector", "safety", "unify", "unused"][:0] or
                         sorted(["nvector", "safety", "unify", "unused"]))
        self.assertEqual(got["nvector"], ("Position math not through the n-vector library",
                                          "Use nvlib for all position math."))
        self.assertEqual(got["safety"], ("Safety and robustness", "Only memory errors."))
        self.assertEqual(len(fa.themes_of("")), len(fa.THEMES))


class UnusedTest(unittest.TestCase):
    def test_what_nothing_names(self):
        texts = {"CMakeLists.txt": "add_executable(app src/main.cpp src/a.cpp src/b.cpp res/app.qrc)\n",
                 "src/main.cpp": '#include "a.h"\n', "src/a.cpp": '#include "a.h"\n#include "ui_form.h"\n',
                 "src/a.h": "", "src/b.cpp": '#include "only_b.h"\n', "src/only_b.h": "",
                 "src/old.cpp": '#include "old.h"\n', "src/old.h": "", "src/form.ui": "<ui/>",
                 "src/dead.ui": "<ui/>", "res/app.qrc": "<qresource><file>icons/used.png</file></qresource>",
                 "tests/t.cpp": "", "third_party/x.h": "", "docs/x.md": "see gone.png"}
        paths = list(texts) + ["res/icons/used.png", "res/icons/lost.png", "res/icons/gone.png"]
        got = fa.unused_files(paths, texts)
        # old.cpp is named by nothing, so old.h (named only by old.cpp) goes with it; tests, vendored code and
        # main are left out; gone.png is named by a doc; what only an unused file names is unused too
        self.assertEqual(got, ["res/icons/lost.png", "src/dead.ui", "src/old.cpp", "src/old.h"])
        texts["CMakeLists.txt"] = texts["CMakeLists.txt"].replace(" res/app.qrc", "")
        self.assertEqual(fa.unused_files(paths, texts), ["res/app.qrc", "res/icons/lost.png", "res/icons/used.png",
                                                         "src/dead.ui", "src/old.cpp", "src/old.h"])
        # a build that globs its sources: sources are never "unused", so what they name is used
        texts["CMakeLists.txt"] = "file(GLOB SRC src/*.cpp)\n"
        got = fa.unused_files(paths, texts)
        self.assertNotIn("src/old.cpp", got)
        self.assertNotIn("src/old.h", got)
        self.assertIn("src/dead.ui", got)
        self.assertEqual(fa.unused_files(["a.py", "b.py"], {"a.py": "", "b.py": ""}), [])


class UnitsTest(unittest.TestCase):
    def test_areas_and_questions(self):
        self.assertEqual(fa.area_of("app.py"), ".")
        self.assertEqual(fa.area_of("lib/use.py"), "lib")
        self.assertEqual(fa.area_of("lib/sub/deep/x.py"), "lib/sub")
        files = files_of({"app.py": "code", "lib/use.py": "code", "lib/sub/deep/x.py": "code",
                          "docs/a.md": "doc", "conf/c.yml": "config", "serve/page.py": "code"})
        files["lib/skip.py"] = {"skip": True}
        got = sorted(u.key for u in fa.units_of("/r", files))
        self.assertEqual(got, sorted(["/r:.:bug", "/r:.:cleanup", "/r:lib:bug", "/r:lib:cleanup",
                                      "/r:lib/sub:bug", "/r:lib/sub:cleanup", "/r:serve:bug",
                                      "/r:serve:cleanup"]))
        (lib,) = [u for u in fa.units_of("/r", files) if u.key == "/r:lib:bug"]
        self.assertEqual(lib.files, ["lib/use.py"])
        # the area's sha moves with any of its files' blobs, and only with them
        files2 = dict(files, **{"lib/use.py": {"kind": "code", "blob": "new"}})
        sha = {u.key: u.sha for u in fa.units_of("/r", files2)}
        old = {u.key: u.sha for u in fa.units_of("/r", files)}
        self.assertNotEqual(sha["/r:lib:bug"], old["/r:lib:bug"])
        self.assertEqual(sha["/r:.:bug"], old["/r:.:bug"])

    def test_entry_points(self):
        files = files_of({"src/main.cpp": "code", "src/mainwindow.cpp": "code", "src/mainwindow.h": "code",
                          "src/mainwindow.ui": "config", "src/dialog.ui": "config", "src/math.cpp": "code",
                          "lib/x.cpp": "code"})
        files["src/main.cpp"]["entry"] = True
        got = [(u.key, u.entry) for u in fa.units_of("/r", files) if u.lens == "bug"]
        # a form is reached through its code twin; one with none is its own entry point; lib/ has none
        self.assertEqual(got, [("/r:src/dialog.ui:bug", "src/dialog.ui"), ("/r:src/main.cpp:bug", "src/main.cpp"),
                               ("/r:src/mainwindow.cpp:bug", "src/mainwindow.cpp")])
        self.assertEqual(sorted(u.key for u in fa.units_of("/r", files) if u.lens == "cleanup"),
                         ["/r:lib:cleanup", "/r:src:cleanup"])
        self.assertTrue(fa.ENTRY_RE.search("connect(ui->okButton, &QPushButton::clicked, this, &D::accept);"))
        self.assertTrue(fa.ENTRY_RE.search("void MainWindow::on_actionOpen_triggered()"))
        self.assertFalse(fa.ENTRY_RE.search("double d = haversine(a, b);"))

    def test_due_order(self):
        t = 1_000_000_000.0
        us = [fa.Unit("/r", a, lens, ["x"], "sha-" + a)
              for a, lens in (("new", "cleanup"), ("new", "bug"), ("changed", "cleanup"), ("old", "bug"),
                              ("fresh", "bug"), ("aside", "bug"), ("later", "bug"), ("prompt", "bug"))]
        h = {"bug": "h", "cleanup": "h"}
        led = {
            us[2].key: {"sha": "other", "prompt": "h", "scanned": t - 10},
            us[3].key: {"sha": "sha-old", "prompt": "h", "scanned": t - 8 * 86400},
            us[4].key: {"sha": "sha-fresh", "prompt": "h", "scanned": t - 3600},
            us[5].key: {"failures": 3, "fail_sha": "sha-aside", "tried": t - 3600},
            us[6].key: {"failures": 1, "fail_sha": "sha-later", "defer_until": t + 60},
            us[7].key: {"sha": "sha-prompt", "prompt": "old-notes", "scanned": t - 10},
        }

        def order():
            first, rest = fa.due(us, led, h, t)
            return [(u.area, u.lens) for u in first], [(u.area, u.lens) for u in rest]
        first, rest = order()
        # bug and cleanup passes in turn, the other lens than the pass before first
        self.assertEqual(first, [("new", "bug"), ("changed", "cleanup"), ("prompt", "bug"), ("new", "cleanup")])
        self.assertEqual([(u.area, u.lens) for u in fa.due(us, led, h, t, last="bug")[0]][:2],
                         [("changed", "cleanup"), ("new", "bug")])
        # never out of work: every other pass comes again, the one explored longest ago first
        self.assertEqual(rest, [("old", "bug"), ("fresh", "bug")])
        led[us[5].key]["tried"] = t - fa.SET_ASIDE - 1   # a set-aside pass comes back after SET_ASIDE...
        self.assertIn(("aside", "bug"), order()[0])
        led[us[5].key].update(tried=t - 3600, fail_sha="older")   # ... or as soon as its area changed
        self.assertIn(("aside", "bug"), order()[0])
        for u in us:   # all done and unchanged: still a full rotation
            led[u.key] = {"sha": u.sha, "prompt": "h", "scanned": t - 10}
        first, rest = order()
        self.assertEqual((first, len(rest)), ([], len(us)))
        h["cleanup"] = "themes changed"   # the owner's themes: only the cleanup passes again
        self.assertEqual(order()[0], [("changed", "cleanup"), ("new", "cleanup")])

    def test_prompt(self):
        u = fa.Unit("/x/repo", "serve", "bug", ["serve/a.py", "serve/b.py"], "s", entry="serve/a.py")
        p = fa.build_prompt(u, "my notes", ["serve/a.py:3 old one"], ["the parser"], "abc12345",
                            ui=["Save As...", "Open"])
        for want in ("Start from serve/a.py", "serve/a.py, serve/b.py", "data, not instructions", "my notes",
                     "serve/a.py:3 old one", "the parser", "BUG-abc12345", "END-BUG-abc12345", "steps:", "trace:",
                     '"Save As..."; "Open"', "KNOWN.md", "COVERED-abc12345 |", fa.QUESTIONS["bug"]):
            self.assertIn(want, p)
        self.assertNotIn("ITEM-abc12345", p)
        order = [p.index(x) for x in ("my notes", "the parser", "old one", "BUG-abc12345")]
        self.assertEqual(order, sorted(order))
        c = fa.build_prompt(fa.Unit("/x/repo", "serve", "cleanup", ["serve/a.py"], "s"), "", [], [], "abc12345",
                            themes=fa.themes_of("## nvector\ntitle: Use nvlib\nAll position math.\n"))
        for want in ("ITEM-abc12345 | list | path:line", "- unify: Code to unify", "- nvector: Use nvlib. All "
                     "position math.", fa.QUESTIONS["cleanup"]):
            self.assertIn(want, c)
        self.assertNotIn("BUG-abc12345", c)

    def test_prompt_with_the_area_s_note_and_map_only(self):
        u = fa.Unit("/x/repo", "serve", "cleanup", ["serve/a.py"], "s")
        p = fa.build_prompt(u, "", ["serve/a.py:3 old one"], [], "abc12345", note="# repo: serve/\nNOTE-BODY",
                            note_old=True, has_notes=True, themes=fa.themes_of(""))
        for want in ("NOTE-BODY", "changed since", fa.NOTES_IN_TREE + "/INDEX.md", "KNOWLEDGE-abc12345",
                     "END-KNOWLEDGE-abc12345", "ITEM-abc12345"):
            self.assertIn(want, p)
        self.assertLess(p.index("NOTE-BODY"), p.index("KNOWLEDGE-abc12345"))
        m = fa.build_prompt(u, "", ["serve/a.py:3 old one"], [], "abc12345", map_only=True)
        self.assertIn("board is full", m)
        self.assertNotIn("ITEM-abc12345 |", m)
        self.assertNotIn("serve/a.py:3", m)
        self.assertIn("no note yet", fa.build_prompt(u, "", [], [], "abc12345"))
        v = fa.build_verify_prompt("repo", [dict(fa.check_bug(fa.parse_session(BUG_TEXT, "ab12cd34")[0][0],
                                                              lines_of, lambda t: True)[0],
                                                 context="--- src/save.cpp:1-13\nCODE", near=["k:1 known one"])],
                                   "abc12345")
        for want in ("[1] Save As loses the file name (high)", "  1. Open a project", "CODE", "k:1 known one",
                     "VERDICT-abc12345 | number | real, not-real or duplicate"):
            self.assertIn(want, v)

    def test_kinds(self):
        self.assertEqual(fa.file_kind("a.py", "x = 1\n"), "code")
        self.assertEqual(fa.file_kind("README.md", "# hi\n"), "doc")
        self.assertEqual(fa.file_kind("c.yml", "a: 1\n"), "config")
        self.assertIsNone(fa.file_kind("g.py", "# @generated by protoc\nx = 1\n"))
        self.assertFalse(fa.eligible(".env", 10))
        self.assertFalse(fa.eligible("node_modules/x.js", 10))
        self.assertFalse(fa.eligible("big.py", 500 * 1024))
        self.assertTrue(fa.eligible("src/a.py", 10))


class SnapshotTest(unittest.TestCase):
    def test_head_without_secrets_made_once_per_head(self):
        repo = make_repo({"app.py": APP, ".env": "TOKEN=x\n", "secrets/k.txt": "k\n", "d/id_rsa": "k\n",
                          ".env.example": "TOKEN=\n"})
        with open(os.path.join(repo, "uncommitted.py"), "w") as f:
            f.write("x = 1\n")
        trees = tempfile.mkdtemp(prefix="fa-tree-")
        head = fa.head_of(repo)
        t = fa.snapshot(repo, head, trees)
        self.assertTrue(t.startswith(trees))
        got = sorted(os.path.relpath(os.path.join(d, n), t) for d, _, ns in os.walk(t) for n in ns)
        self.assertEqual(got, [".env.example", "app.py"])
        marker = os.path.join(t, "marker")
        open(marker, "w").close()
        self.assertEqual(fa.snapshot(repo, head, trees), t)
        self.assertTrue(os.path.exists(marker))   # same HEAD: not made again
        commit(repo, {"app.py": APP + "# more\n"})
        self.assertEqual(fa.snapshot(repo, fa.head_of(repo), trees), t)
        self.assertFalse(os.path.exists(marker))
        with open(os.path.join(t, "app.py")) as f:
            self.assertIn("# more", f.read())


class StubCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Proxy.catalog = [FREE, METERED]
        self.state = os.path.join(tempfile.mkdtemp(prefix="fa-state-"), "findings")
        os.makedirs(self.state)
        bin_dir = tempfile.mkdtemp(prefix="fa-bin-")
        self.harness = os.path.join(bin_dir, "harness")
        with open(self.harness, "w") as f:
            f.write(STUB)
        self.log = os.path.join(bin_dir, "stub.log")
        self.env = dict(os.environ, FINDINGS_GAP="0", STUB_LOG=self.log, STUB_MODE="find")
        self.old_env = dict(os.environ)
        os.environ.update(STUB_LOG=self.log, STUB_MODE="find")
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def run_once(self, repo, model="free-assist", mode="find", extra_env=None):
        env = dict(self.env, STUB_MODE=mode, **(extra_env or {}))
        return subprocess.run([sys.executable, SCRIPT, "run", "--state", self.state, "--proxy", self.url,
                               "--model", model, "--repo", repo, "--harness", self.harness, "--once"],
                              capture_output=True, text=True, timeout=120, env=env)

    def read(self, name):
        with open(os.path.join(self.state, name)) as f:
            return json.load(f)

    def stub_log(self):
        try:
            with open(self.log) as f:
                return f.read()
        except OSError:
            return ""


class GuardTest(StubCase):
    def test_free_rule(self):
        entries = [FREE, METERED, {"id": "w0", "credit_weights": {"input": 0, "output": 0}}]
        self.assertTrue(fa.is_free("free-assist", entries))
        self.assertTrue(fa.is_free("w0", entries))
        self.assertFalse(fa.is_free("paid-model", entries))
        self.assertTrue(fa.is_free("unlisted-assist", entries))
        self.assertTrue(fa.is_free("gemini-enterprise", entries))
        self.assertFalse(fa.is_free("unlisted", entries))

    def test_pick_and_guard(self):
        Proxy.catalog = [METERED, FREE, dict(FREE, id="gemini-2.5-pro")]
        self.assertEqual(fa.guard(self.url, ""), ("gemini-2.5-pro", None))
        model, why = fa.guard(self.url, "paid-model")
        self.assertIn("not a free model", why)
        Proxy.catalog = [METERED]
        self.assertEqual(fa.guard(self.url, "")[1], "the catalog lists no free model")
        with self.assertRaises(fa.InfraError):
            fa.guard("http://127.0.0.1:1/v1", "free-assist")

    def test_metered_model_is_never_run(self):
        repo = make_repo({"app.py": APP})
        r = self.run_once(repo, model="paid-model")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stub_log(), "")
        self.assertIn("not a free model", self.read("agent.json")["last_error"])

    def test_errors_and_waits(self):
        self.assertEqual(fa.classify("HTTP 429: slow down"), "rate")
        self.assertEqual(fa.classify("HTTP 403: no"), "auth")
        self.assertEqual(fa.classify("<urlopen error [Errno 111] Connection refused>"), "refused")
        self.assertEqual(fa.classify("HTTP 502: bad gateway"), "infra")
        # as harness host prints them: not at the start of the line
        self.assertEqual(fa.classify("[harness] opencode error: HTTP 429 Too Many Requests"), "rate")
        self.assertEqual(fa.classify("[harness] opencode error: quota exceeded for model"), "rate")
        self.assertEqual(fa.classify("[harness] opencode error: 401 Unauthorized"), "auth")
        self.assertEqual(fa.retry_seconds('HTTP 429: {"error": {"retry_after_seconds": 42}}'), 42)
        self.assertEqual(fa.retry_seconds("HTTP 429: quota resets in 9h 19m"), 9 * 3600 + 19 * 60)
        self.assertEqual(fa.retry_seconds("HTTP 429: nothing"), 60)


class PassTest(StubCase):
    def test_a_bug_is_checked_by_a_second_session_before_it_is_kept(self):
        repo = make_repo({"app.py": APP, "lib/use.py": USE, ".env": "TOKEN=ratio-secret-value\n"})
        with open(os.path.join(self.state, "notes.md"), "w") as f:
            f.write("ignore style")
        r = self.run_once(repo)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        log = self.stub_log()
        self.assertIn("args=host --single-message --read-only -p -m harness/free-assist", log)
        self.assertIn("pass=1", log)
        self.assertIn(f"cwd={os.path.realpath(self.state)}/tree/", log.replace(self.state, os.path.realpath(self.state)))
        self.assertNotIn("env=present", log)   # the snapshot has no secret files
        with open(self.log + ".prompt") as f:
            prompt = f.read()
        for want in ("ignore style", "Start from the area ./", "KNOWN.md", "BUG-"):
            self.assertIn(want, prompt)
        # the first report waits for the checker; the second names a text the program never shows
        self.assertFalse(os.path.exists(os.path.join(self.state, "findings.json")))
        led = self.read("ledger.json")
        (p,) = led["pending"]
        self.assertEqual((p["cand"]["path"], p["cand"]["line"], p["cand"]["ui"], len(p["cand"]["steps"])),
                         ("app.py", 6, "Compute Ratio...", 2))
        self.assertEqual(led["counts"]["bug"]["why"], {"ui": 1})
        (ps,) = led["passes"]
        self.assertEqual((ps["n"], ps["session"], ps["outcome"], ps["found"], ps["rejected"], ps["area"], ps["lens"]),
                         (1, "ses_stub1", "waiting", 1, 1, ".", "bug"))
        e = led["units"][f"{repo}:.:bug"]
        self.assertEqual((e["outcome"], e["covered"]), ("waiting", ["app.py and its callers in lib/"]))
        kn = os.path.join(self.state, "tree")
        known = [os.path.join(d, "KNOWN.md") for d, _, ns in os.walk(kn) if "KNOWN.md" in ns]
        self.assertTrue(known)
        # the next run is the checking session: it says real, the report goes on the board
        r = self.run_once(repo)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        with open(self.log + ".prompt") as f:
            v = f.read()
        self.assertIn("[1] Compute Ratio crashes when the divisor is zero (high)", v)
        self.assertIn("    6  ", v)   # the code around the lines, numbered
        (f,) = self.read("findings.json")["findings"]
        self.assertEqual((f["category"], f["path"], f["line"], f["project"], f["evidence"], f["session"],
                          f["area"], f["lens"], f["status"], f["ui"]),
                         ("bug", "app.py", 6, repo, "return a / b", "ses_stub1", ".", "bug", "open",
                          "Compute Ratio..."))
        self.assertEqual(f["steps"], ["Start the application", "Click Compute Ratio with a divisor of 0"])
        self.assertEqual([h["path"] for h in f["trace"]], ["lib/use.py", "app.py"])
        self.assertEqual(f["verdict"], "real: checked the code")
        led = self.read("ledger.json")
        self.assertEqual(led["pending"], [])
        self.assertEqual(led["passes"][-1]["lens"], "verify")
        # then the cleanup pass of ./: its items go on the lists
        self.assertEqual(self.run_once(repo).returncode, 0)
        store = self.read("findings.json")["findings"]
        rolls = {x["theme"]: x for x in store if x.get("kind") == "rolling"}
        self.assertEqual(sorted(rolls), ["safety", "unify"])
        (it,) = rolls["unify"]["items"]
        self.assertEqual((it["path"], it["line"], it["locations"], it["note"], it["status"]),
                         ("app.py", 10, ["app.py:10", "lib/use.py:2"], "use math.fsum in both", "open"))
        self.assertTrue(rolls["unify"]["id"].startswith("r-unify-"))
        self.assertEqual(self.read("ledger.json")["counts"]["cleanup"]["why"], {"list": 1, "evidence": 1})
        # the same items again: nothing new
        w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
        w.refresh()
        u = [x for x in w.units[repo] if x.area == "." and x.lens == "cleanup"][0]
        self.assertEqual(w.accept_items(u, [("unify", "app.py:10, lib/use.py:2", " return sum(items) | again"),
                                            ("safety", "app.py:6", " return a / b | again")], "s"), (0, 2))
        out = io.StringIO()
        with redirect_stdout(out):
            fa.main(["status", "--state", self.state])
        self.assertIn("1 open", out.getvalue())

    def test_the_checker_s_no(self):
        repo = make_repo({"app.py": APP, "lib/use.py": USE})
        self.assertEqual(self.run_once(repo).returncode, 0)
        self.assertEqual(self.run_once(repo, extra_env={"STUB_VERDICT": "not-real"}).returncode, 0)
        led = self.read("ledger.json")
        self.assertEqual((led["pending"], led["counts"]["verify"]["why"]), ([], {"not-real": 1}))
        self.assertFalse(os.path.exists(os.path.join(self.state, "findings.json")))
        # a checker that gives no verdict: the report waits for VERIFY_TRIES sessions, then goes
        commit(repo, {"app.py": APP + "# more\n"})
        self.assertEqual(self.run_once(repo).returncode, 0)   # ./'s cleanup pass: the other lens goes first
        self.assertEqual(self.read("ledger.json")["passes"][-1]["lens"], "cleanup")
        self.assertEqual(self.run_once(repo).returncode, 0)
        self.assertEqual(len(self.read("ledger.json")["pending"]), 1)
        for i in range(fa.VERIFY_TRIES):
            self.assertEqual(self.run_once(repo, extra_env={"STUB_VERDICT": "silent"}).returncode, 0)
            self.assertEqual(len(self.read("ledger.json")["pending"]), 0 if i == fa.VERIFY_TRIES - 1 else 1)
        self.assertEqual(self.read("ledger.json")["counts"]["verify"]["why"]["no verdict"], 1)

    def test_known_findings_are_not_reported_again(self):
        repo = make_repo({"app.py": APP, "lib/use.py": USE})
        # a ticket agent's finding on the same place: in KNOWN.md, and the report is a duplicate
        root = os.path.dirname(self.state)
        vault = os.path.join(root, "vaults", "v1")
        os.makedirs(os.path.join(vault, "wiki", "notes", "tickets"))
        os.makedirs(os.path.join(root, "queue", "ABC-1"))
        with open(os.path.join(root, "vault.active"), "w") as f:
            f.write("v1\n")
        with open(os.path.join(root, "queue", "ABC-1", "state.json"), "w") as f:
            json.dump({"vault": vault}, f)
        with open(os.path.join(vault, "wiki", "notes", "tickets", "ABC-1-findings.md"), "w") as f:
            f.write("# ABC-1 findings\n\n- app.py:6 Compute Ratio crashes when the divisor is zero\n")
        w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
        w.refresh()
        known = w.known(repo)
        self.assertEqual([(k["path"], k["line"], k["state"]) for k in known], [("app.py", 6, "from ticket ABC-1")])
        self.assertEqual(self.run_once(repo).returncode, 0)
        led = self.read("ledger.json")
        self.assertEqual((led["pending"], led["counts"]["bug"]["why"]), ([], {"duplicate": 1, "ui": 1}))
        known_md = [os.path.join(d, "KNOWN.md") for d, _, ns in os.walk(os.path.join(self.state, "tree"))
                    if "KNOWN.md" in ns]
        with open(known_md[0]) as f:
            self.assertIn("app.py:6 | Compute Ratio crashes when the divisor is zero | from ticket ABC-1", f.read())
        # an archived finding the store drops stays known (as dismissed)
        w.store["findings"] = [{"id": "f-00000000000a", "project": repo, "path": "lib/use.py", "line": 2,
                                "title": "old one", "status": "open"}]
        with open(os.path.join(self.state, "board.json"), "w") as f:
            json.dump({"version": 1, "order": [], "archived": {"f-00000000000a": "2026-01-01T00:00:00Z"}}, f)
        old, fa.STORE_MAX = fa.STORE_MAX, 0
        try:
            w.save_store()
        finally:
            fa.STORE_MAX = old
        self.assertEqual(w.store["findings"], [])
        self.assertIn(("lib/use.py", 2, "dismissed by the owner"),
                      [(k["path"], k["line"], k["state"]) for k in w.known(repo)])

    def test_rolling_lists_and_their_generations(self):
        repo = make_repo({"app.py": APP})
        w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
        w.refresh()
        t, d = "Code to unify", "desc"
        item = {"path": "app.py", "line": 10, "evidence": "return sum(items)", "note": "n",
                "locations": ["app.py:10", "x.py:1"]}
        self.assertTrue(w.add_item(repo, "unify", t, d, item, "s"))
        self.assertFalse(w.add_item(repo, "unify", t, d, dict(item, line=12, evidence="other"), "s"))
        (r,) = w.rolls(repo, "unify")
        self.assertEqual(w.open_count(), 1)   # a list is one finding, however long
        # the owner archives the list: a new one starts, the old items are not added again
        with open(os.path.join(self.state, "board.json"), "w") as f:
            json.dump({"version": 1, "order": [], "archived": {r["id"]: "2026-01-01T00:00:00Z"}}, f)
        self.assertFalse(w.add_item(repo, "unify", t, d, item, "s"))
        self.assertTrue(w.add_item(repo, "unify", t, d, dict(item, path="lib.py", line=1, evidence="q",
                                                             locations=["lib.py:1", "y.py:2"]), "s"))
        ids = sorted(x["id"] for x in w.rolls(repo, "unify"))
        self.assertEqual(ids, [r["id"], r["id"] + "-1"])
        # an item whose code left HEAD is gone; it comes back when found again
        commit(repo, {"app.py": "x = 1\n"})
        w.refresh()
        (it,) = [i for i in r["items"]]
        self.assertEqual(it["status"], "gone")

    def test_unused_files_scan(self):
        repo = make_repo({"CMakeLists.txt": "add_executable(app src/main.cpp src/a.cpp)\n",
                          "src/main.cpp": '#include "a.h"\nint main() {}\n', "src/a.cpp": '#include "a.h"\n',
                          "src/a.h": "int a();\n", "src/old.cpp": "int old() { return 1; }\n"})
        w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
        w.refresh()
        (r,) = w.rolls(repo, "unused")
        self.assertEqual([(i["path"], i["source"], i["status"]) for i in r["items"]],
                         [("src/old.cpp", "scan", "open")])
        commit(repo, {"CMakeLists.txt": "add_executable(app src/main.cpp src/a.cpp src/old.cpp)\n"})
        w.refresh()
        self.assertEqual(r["items"][0]["status"], "gone")
        self.assertTrue(w.unused_ok(repo, "src/a.h") is False)

    def test_archive_old(self):
        store = {"version": 1, "findings": [
            {"id": "f-000000000001", "status": "open", "project": "/x", "path": "p", "title": "old", "line": 1},
            {"id": "f-000000000002", "status": "open", "project": "/x", "path": "p", "title": "new", "line": 1,
             "steps": ["a", "b"]},
            {"id": "r-unify-1", "kind": "rolling", "status": "open", "project": "/x", "items": []}]}
        with open(os.path.join(self.state, "findings.json"), "w") as f:
            json.dump(store, f)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(fa.main(["archive-old", "--state", self.state]), 0)
        self.assertIn("1", out.getvalue())
        self.assertEqual(list(self.read("board.json")["archived"]), ["f-000000000001"])

    def test_a_pass_writes_its_notes_and_the_next_passes_read_them(self):
        repo = make_repo({"app.py": APP, "lib/use.py": USE})
        self.assertEqual(self.run_once(repo).returncode, 0)
        kdir = os.path.join(self.state, "knowledge", fa.repo_slug(repo))
        with open(os.path.join(kdir, "_top.md")) as f:
            top = f.read()
        self.assertTrue(top.startswith("# repo: ./"))
        self.assertIn("ratio(1, 0)", top)
        self.assertTrue(os.path.isfile(os.path.join(kdir, "lib.md")))   # another area, corrected in passing
        self.assertEqual(sorted(os.listdir(kdir)), ["INDEX.md", "_top.md", "lib.md"])   # "nowhere" dropped
        with open(os.path.join(kdir, "INDEX.md")) as f:
            index = f.read()
        self.assertIn("- ./ (_top.md): app.py holds ratio", index)
        self.assertIn("- lib/ (lib.md)", index)
        listing = self.read("knowledge/index.json")["notes"]
        self.assertEqual(sorted((n["area"], n["changed"], n["session"]) for n in listing),
                         [(".", False, "ses_stub1"), ("lib", False, "ses_stub1")])
        self.assertEqual(self.read("ledger.json")["passes"][0]["notes"], [".", "lib"])
        # the passes after (./ cleanup, then lib/ bug) get their area's note and every note in their snapshot
        led = self.read("ledger.json")
        led["pending"] = []
        with open(os.path.join(self.state, "ledger.json"), "w") as f:
            json.dump(led, f)
        self.assertEqual(self.run_once(repo, mode="none").returncode, 0)
        self.assertEqual(self.run_once(repo, mode="none").returncode, 0)
        with open(self.log + ".prompt") as f:
            prompt = f.read()
        self.assertIn("Start from the area lib/", prompt)
        self.assertIn("importing it runs print(ratio(1, 0))", prompt)
        cwd = [x for x in self.stub_log().splitlines() if x.startswith("cwd=")][-1][4:]
        self.assertTrue(os.path.isfile(os.path.join(cwd, fa.NOTES_IN_TREE, "INDEX.md")))
        self.assertTrue(os.path.isfile(os.path.join(cwd, fa.NOTES_IN_TREE, "lib.md")))
        # a later note on ./ replaces the old one, which is kept as .prev; a much shorter one is refused
        w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
        w.refresh()
        u = [x for x in w.units[repo] if x.area == "." and x.lens == "cleanup"][0]
        new = top + "\nAlso traced: total(items) is unused.\n" + "More on how ratio is used. " * 30
        self.assertEqual(w.take_notes(u, [("", new)], "ses_x"), ["."])
        with open(os.path.join(kdir, "_top.md.prev")) as f:
            self.assertEqual(f.read(), top)
        self.assertEqual(w.take_notes(u, [("", "# r\napp.py " + "x" * 300)], "ses_y"), [])
        # the area changes: its note is marked out of date
        commit(repo, {"app.py": APP + "# more\n"})
        w.refresh()
        listing = {n["area"]: n for n in self.read("knowledge/index.json")["notes"]}
        self.assertEqual((listing["."]["changed"], listing["lib"]["changed"]), (True, False))
        self.assertTrue(w.note_old([x for x in w.units[repo] if x.area == "."][0]))

    def test_a_session_that_never_answers_fails_its_pass(self):
        repo = make_repo({"app.py": APP})
        self.assertEqual(self.run_once(repo, mode="noend").returncode, 0)
        led = self.read("ledger.json")
        e = led["units"][f"{repo}:.:bug"]
        self.assertEqual(e["failures"], 1)
        self.assertGreater(e["defer_until"], time.time())
        self.assertEqual(led["passes"][-1]["outcome"], "failed")
        self.assertEqual(led["passes"][-1]["session"], "ses_stub1")

    def test_no_session_with_the_upstream_up_blames_the_pass(self):
        repo = make_repo({"app.py": APP})
        self.assertEqual(self.run_once(repo, mode="down").returncode, 0)
        led = self.read("ledger.json")
        self.assertEqual(led["units"][f"{repo}:.:bug"]["failures"], 1)
        self.assertIn("502", self.read("agent.json")["last_error"])

    def test_a_silent_session_is_ended(self):
        repo = make_repo({"app.py": APP})
        os.environ["STUB_MODE"] = "hang"
        old = fa.IDLE_TIMEOUT, fa.GAP
        fa.IDLE_TIMEOUT, fa.GAP = 2, 0
        try:
            w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
            t0 = time.time()
            w.iteration()
            self.assertLess(time.time() - t0, 9)   # the idle limit, not the kill's grace
        finally:
            fa.IDLE_TIMEOUT, fa.GAP = old
        self.assertEqual(w.ledger["units"][f"{repo}:.:bug"]["failures"], 1)
        self.assertEqual(w.ledger["passes"][-1]["session"], "ses_stub1")

    def test_a_session_that_keeps_working_is_not_idle(self):
        # its answer comes only at the end; its events (HARNESS_HOST_EVENTS_FILE) show it works
        repo = make_repo({"app.py": APP})
        os.environ["STUB_MODE"] = "busy"
        old = fa.IDLE_TIMEOUT, fa.GAP
        fa.IDLE_TIMEOUT, fa.GAP = 3, 0
        try:
            w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
            w.iteration()
        finally:
            fa.IDLE_TIMEOUT, fa.GAP = old
        self.assertEqual(w.ledger["passes"][-1]["outcome"], "none")
        self.assertEqual(w.ledger["units"][f"{repo}:.:bug"]["failures"], 0)
        self.assertEqual([x for x in os.listdir(os.path.join(self.state, "sessions")) if x.endswith(".events")], [])

    def test_a_rate_limited_session_waits_and_is_not_the_pass_s_fault(self):
        repo = make_repo({"app.py": APP})
        os.environ["STUB_MODE"] = "rate"
        w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
        waits = []
        w.wait = lambda secs, state, **kw: waits.append((secs, state)) or True
        w.iteration()
        self.assertEqual(waits, [(7.0, "backoff")])
        self.assertNotIn("failures", w.ledger["units"].get(f"{repo}:.:bug") or {})

    def test_board_full_writes_notes_only(self):
        repo = make_repo({"app.py": APP})
        store = {"version": 1, "findings": [{"id": f"f-{i:012x}", "status": "open", "project": "/x", "path": "p",
                                             "title": f"t{i}", "line": 1} for i in range(3)]}
        with open(os.path.join(self.state, "findings.json"), "w") as f:
            json.dump(store, f)
        self.assertEqual(self.run_once(repo, extra_env={"FINDINGS_OPEN_MAX": "3"}).returncode, 0)
        self.assertIn("pass=1", self.stub_log())   # the agent keeps working...
        with open(self.log + ".prompt") as f:
            self.assertIn("board is full", f.read())
        self.assertEqual(len(self.read("findings.json")["findings"]), 3)   # ... but adds no finding
        self.assertTrue(os.path.isfile(os.path.join(self.state, "knowledge", fa.repo_slug(repo), "_top.md")))
        self.assertEqual(self.read("ledger.json")["passes"][-1]["found"], 0)
        self.assertIn("board full", self.read("agent.json").get("reason", ""))

    def test_stale_and_moved(self):
        repo = make_repo({"a.py": "x = 1\ntotal = compute_total(items)\nother = keep_me(1)\n"})
        w = fa.Worker(self.state, "http://127.0.0.1:1/v1", "m", [repo])
        w.store["findings"] = [
            {"id": "f-000000000001", "path": "a.py", "line": 2, "project": repo, "status": "open",
             "evidence": "total = compute_total(items)", "title": "t1"},
            {"id": "f-000000000002", "path": "a.py", "line": 3, "project": repo, "status": "open",
             "evidence": "other = keep_me(1)", "title": "t2"}]
        w.refresh()
        self.assertEqual([f["status"] for f in w.store["findings"]], ["open", "open"])
        commit(repo, {"a.py": "# new top\n\nx = 1\nother = keep_me(1)\n"})
        w.refresh()
        a, b = w.store["findings"]
        self.assertEqual((a["status"], b["status"], b["line"]), ("stale", "open", 4))
        self.assertEqual(w.open_count(), 1)
        with open(os.path.join(self.state, "board.json"), "w") as f:
            json.dump({"version": 1, "order": [], "archived": {"f-000000000002": "2026-01-01T00:00:00Z"}}, f)
        self.assertEqual(w.open_count(), 0)


class LifeTest(StubCase):
    def test_second_instance_exits_zero(self):
        repo = make_repo({"app.py": APP})
        lock = open(os.path.join(self.state, "agent.lock"), "a")
        self.assertTrue(fa.try_lock(lock))
        try:
            r = self.run_once(repo)
            self.assertEqual(r.returncode, 0)
            self.assertIn("already running", r.stdout)
            self.assertEqual(self.stub_log(), "")
            self.assertEqual(fa.main(["status", "--state", self.state, "--running"]), 0)
        finally:
            lock.close()
        self.assertEqual(fa.main(["status", "--state", self.state, "--running"]), 1)

    def test_it_runs_until_stopped_and_a_stop_is_remembered(self):
        repo = make_repo({"app.py": APP, "lib/use.py": USE})
        env = dict(self.env, STUB_MODE="none")
        proc = subprocess.Popen([sys.executable, SCRIPT, "run", "--state", self.state, "--proxy", self.url,
                                 "--model", "free-assist", "--repo", repo, "--harness", self.harness],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        try:
            for _ in range(300):   # it goes on past its first passes
                if len(self.read_passes()) >= 3:
                    break
                time.sleep(0.1)
            self.assertGreaterEqual(len(self.read_passes()), 3)
            self.assertTrue(os.path.exists(os.path.join(self.state, "wanted")))
            out = io.StringIO()
            with redirect_stdout(out):
                fa.main(["stop", "--state", self.state])
            self.assertEqual(proc.wait(timeout=40), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        self.assertEqual(self.read("agent.json")["state"], "stopped")
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "wanted")))   # serve leaves it stopped
        self.assertFalse(fa.running(self.state))

    def read_passes(self):
        try:
            return self.read("ledger.json").get("passes") or []
        except (OSError, ValueError):
            return []

    def test_a_session_a_dead_worker_left_is_ended(self):
        # (": " after it: bash stays bash, with harness in its command line, instead of becoming sleep)
        proc = subprocess.Popen(["bash", "-c", "sleep 60; :", "harness"], start_new_session=True)
        try:
            with open(os.path.join(self.state, "agent.json"), "w") as f:
                json.dump({"pass_pid": proc.pid, "pass_started": fa.iso(time.time() - 3 * 86400)}, f)
            fa.end_left_pass(self.state)   # started days before the pid's process: a reused pid, left alone
            time.sleep(0.3)
            self.assertIsNone(proc.poll())
            with open(os.path.join(self.state, "agent.json"), "w") as f:
                json.dump({"pass_pid": proc.pid, "pass_started": fa.iso()}, f)
            fa.end_left_pass(self.state)
            self.assertIsNotNone(proc.wait(timeout=20))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        other = subprocess.Popen(["sleep", "60"], start_new_session=True)   # not harness's: left alone
        try:
            with open(os.path.join(self.state, "agent.json"), "w") as f:
                json.dump({"pass_pid": other.pid, "pass_started": fa.iso()}, f)
            fa.end_left_pass(self.state)
            time.sleep(0.3)
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            other.wait()


if __name__ == "__main__":
    unittest.main()
