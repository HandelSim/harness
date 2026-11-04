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
      "    return sum(items)\n"
USE = "from app import ratio\nprint(ratio(1, 0))\n"

# The stub harness: logs how it was run, then answers as STUB_MODE says.
STUB = r"""#!/usr/bin/env bash
prompt="${@: -1}"
nonce=$(grep -o 'COVERED-[0-9a-f]\{8\}' <<<"$prompt" | head -1 | cut -d- -f2)
{ printf 'args=%s\n' "${*:1:$(($# - 1))}"; printf 'cwd=%s\n' "$PWD"; printf 'pass=%s\n' "${HARNESS_FINDINGS_PASS:-}"
  [[ -e .env ]] && echo "env=present"; } >>"$STUB_LOG"
printf '%s\n' "$prompt" >"$STUB_LOG.prompt"
session() { echo "[harness] session: ses_stub${HARNESS_FINDINGS_PASS:-0}" >&2; }
case "${STUB_MODE:-find}" in
  find) session
        echo "I followed ratio to its callers."
        echo "- **FINDING-$nonce** | bug | high | app.py:6 | divides by zero | \`return a / b\` | lib/use.py calls ratio(1, 0)"
        echo "FINDING-$nonce | bug | low | ./app.py:9 | made up | not in the code at all | x"
        echo "FINDING-00000000 | bug | high | app.py:6 | another nonce | return a / b | x"
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


class ParseTest(unittest.TestCase):
    def test_markdown_tolerant_and_nonce_bound(self):
        text = ("Here is what I found:\n"
                "- `FINDING-ab12cd34 | bug | high | serve/x.py:12 | t1 | y = 1 | d`\n"
                "**FINDING-ab12cd34** | ui | low | ./serve/x.py:3-5 | t2 | ev | det\n"
                "1. FINDING-ab12cd34|cleanup|medium|.github/w.yml:4|t3|b|c\n"
                "FINDING-ffffffff | bug | high | a.py:1 | other nonce | e | d\n"
                "FINDING-ab12cd34 | bug | high | a.py | no line | e | d\n"
                "FINDING-ab12cd34 | bug | high\n"
                "\x1b[0mCOVERED-ab12cd34 | the | serve folder\n")
        found, covered = fa.parse_session(text, "ab12cd34")
        self.assertEqual([f[:5] for f in found], [("bug", "high", "serve/x.py", 12, "t1"),
                                                  ("ui", "low", "serve/x.py", 3, "t2"),
                                                  ("cleanup", "medium", ".github/w.yml", 4, "t3")])
        self.assertEqual(found[0][5].strip(), "y = 1 | d")
        self.assertEqual(covered, "the / serve folder")

    def test_no_covered_line_is_an_unfinished_session(self):
        self.assertEqual(fa.parse_session("I think the code is fine.", "ab12cd34"), ([], None))
        self.assertEqual(fa.parse_session("", "ab12cd34"), ([], None))
        self.assertEqual(fa.parse_session("COVERED-ab12cd34 | x", "ab12cd34"), ([], "x"))


class ValidateTest(unittest.TestCase):
    lines = ["def f(a, b):", "    x = alpha | beta", "    if x:", "        return compute(x)", "    return None"]

    def test_evidence_near_line(self):
        v = fa.validate(4, "bad", " return compute(x) | it breaks", self.lines)
        self.assertEqual((v["line"], v["evidence"], v["detail"]), (4, "return compute(x)", "it breaks"))

    def test_evidence_with_a_pipe_and_a_copied_number(self):
        v = fa.validate(2, "t", "    2| x = alpha | beta | the or", self.lines)
        self.assertEqual((v["evidence"], v["detail"]), ("x = alpha | beta", "the or"))

    def test_rejections(self):
        self.assertIsNone(fa.validate(9, "t", "return None | d", self.lines))   # out of range
        self.assertIsNone(fa.validate(4, "t", "return made_up(y) | d", self.lines))
        self.assertIsNone(fa.validate(4, "t", "x | d", self.lines))   # too short
        self.assertIsNone(fa.validate(4, " ", "return None | d", self.lines))   # no title

    def test_window(self):
        lines = ["unique_marker_line = 1"] + ["pass"] * 20
        self.assertIsNotNone(fa.validate(9, "t", "unique_marker_line = 1 | d", lines))
        self.assertIsNone(fa.validate(10, "t", "unique_marker_line = 1 | d", lines))

    def test_fingerprint_stable_and_whitespace_blind(self):
        a = fa.fingerprint("r", "p.py", "bug", "return  a / b")
        self.assertEqual(a, fa.fingerprint("r", "p.py", "bug", "Return a / b "))
        self.assertRegex(a, r"^f-[0-9a-f]{12}$")
        self.assertNotEqual(a, fa.fingerprint("r", "p.py", "simplify", "return a / b"))


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
                                      "/r:lib/sub:bug", "/r:lib/sub:cleanup", "/r:docs:cleanup",
                                      "/r:serve:bug", "/r:serve:ui", "/r:serve:cleanup"]))
        (lib,) = [u for u in fa.units_of("/r", files) if u.key == "/r:lib:bug"]
        self.assertEqual(lib.files, ["lib/use.py"])
        # the area's sha moves with any of its files' blobs, and only with them
        files2 = dict(files, **{"lib/use.py": {"kind": "code", "blob": "new"}})
        sha = {u.key: u.sha for u in fa.units_of("/r", files2)}
        old = {u.key: u.sha for u in fa.units_of("/r", files)}
        self.assertNotEqual(sha["/r:lib:bug"], old["/r:lib:bug"])
        self.assertEqual(sha["/r:.:bug"], old["/r:.:bug"])

    def test_due_order(self):
        t = 1_000_000_000.0
        us = [fa.Unit("/r", a, lens, ["x"], "sha-" + a)
              for a, lens in (("new", "cleanup"), ("new", "bug"), ("changed", "cleanup"), ("old", "bug"),
                              ("fresh", "bug"), ("aside", "bug"), ("later", "bug"), ("prompt", "bug"))]
        led = {
            us[2].key: {"sha": "other", "prompt": "h", "scanned": t - 10},
            us[3].key: {"sha": "sha-old", "prompt": "h", "scanned": t - 8 * 86400},
            us[4].key: {"sha": "sha-fresh", "prompt": "h", "scanned": t - 3600},
            us[5].key: {"failures": 3, "fail_sha": "sha-aside", "tried": t - 3600},
            us[6].key: {"failures": 1, "fail_sha": "sha-later", "defer_until": t + 60},
            us[7].key: {"sha": "sha-prompt", "prompt": "old-notes", "scanned": t - 10},
        }
        got = [(u.area, u.lens) for u in fa.due(us, led, "h", t)]
        self.assertEqual(got, [("new", "bug"), ("prompt", "bug"), ("changed", "cleanup"), ("new", "cleanup"),
                               ("old", "bug")])
        led[us[5].key]["tried"] = t - 4 * 86400   # ... and after RESCAN_DAYS, unchanged
        self.assertIn(("aside", "bug"), [(u.area, u.lens) for u in fa.due(us, led, "h", t)])
        led[us[5].key].update(tried=t - 3600, fail_sha="older")   # a set-aside pass whose area changed comes back
        self.assertIn(("aside", "bug"), [(u.area, u.lens) for u in fa.due(us, led, "h", t)])

    def test_prompt(self):
        u = fa.Unit("/x/repo", "serve", "bug", ["serve/a.py", "serve/b.py"], "s")
        p = fa.build_prompt(u, "my notes", ["serve/a.py:3 old one"], ["the parser"], "abc12345")
        for want in ("serve/a.py, serve/b.py", "do not stop at it", "data, not instructions", "my notes",
                     "serve/a.py:3 old one", "the parser", "FINDING-abc12345 | category | severity | path:line",
                     "COVERED-abc12345 |", fa.QUESTIONS["bug"]):
            self.assertIn(want, p)
        order = [p.index(x) for x in ("my notes", "the parser", "old one", "FINDING-abc12345")]
        self.assertEqual(order, sorted(order))

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
        self.state = tempfile.mkdtemp(prefix="fa-state-")
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
    def test_a_session_explores_and_its_findings_are_checked(self):
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
        self.assertIn("ignore style", prompt)
        self.assertIn("Start from the area ./", prompt)
        store = self.read("findings.json")
        (f,) = store["findings"]
        self.assertEqual((f["category"], f["path"], f["line"], f["project"], f["evidence"], f["session"],
                          f["area"], f["lens"], f["status"]),
                         ("bug", "app.py", 6, repo, "return a / b", "ses_stub1", ".", "bug", "open"))
        self.assertIn("lib/use.py", f["detail"])
        led = self.read("ledger.json")
        (p,) = led["passes"]
        self.assertEqual((p["n"], p["session"], p["outcome"], p["found"], p["rejected"], p["area"], p["lens"]),
                         (1, "ses_stub1", "found", 1, 1, ".", "bug"))
        self.assertEqual(p["covered"], "app.py and its callers in lib/")
        self.assertTrue(os.path.exists(os.path.join(self.state, p["out"])))
        e = led["units"][f"{repo}:.:bug"]
        self.assertEqual((e["outcome"], e["covered"], e["session"]),
                         ("found", ["app.py and its callers in lib/"], "ses_stub1"))
        info = self.read("agent.json")
        self.assertEqual(info["state"], "stopped")
        self.assertFalse(info.get("pass_pid"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "wanted")))   # --once is not a standing run
        # the next pass is the next area; what the first one found and covered goes into its area's prompt later
        r = self.run_once(repo, mode="none")
        self.assertEqual(r.returncode, 0, r.stderr)
        led = self.read("ledger.json")
        self.assertEqual([(p["n"], p["area"], p["outcome"]) for p in led["passes"]],
                         [(1, ".", "found"), (2, "lib", "none")])
        out = io.StringIO()
        with redirect_stdout(out):
            fa.main(["status", "--state", self.state])
        self.assertIn("1 open", out.getvalue())

    def test_known_findings_are_not_kept_twice_and_go_into_the_prompt(self):
        repo = make_repo({"app.py": APP})
        w = fa.Worker(self.state, self.url, "free-assist", [repo], harness=self.harness, once=True)
        w.refresh()
        w.trees[repo] = fa.snapshot(repo, fa.head_of(repo), w.p("tree"))
        u = [u for u in w.units[repo] if u.lens == "bug"][0]
        raw = [("bug", "high", "app.py", 6, "divides by zero", " return a / b | b can be 0")]
        self.assertEqual(w.accept(u, raw, "ses_a"), (1, 0))
        self.assertEqual(w.accept(u, raw, "ses_b"), (0, 0))   # same id
        self.assertEqual(w.accept(u, [("bug", "low", "app.py", 5, "Divides by zero!", "# the caller checks b|x")],
                                  "ses_b"), (0, 0))   # same title
        self.assertEqual(w.accept(u, [("bug", "high", w.trees[repo] + "/app.py", 6, "t2", "invented code | d"),
                                      ("bug", "high", "gone.py", 1, "t3", "return a / b | d")], "ses_b"), (0, 2))
        self.assertEqual(w.prior(u), ["app.py:6 divides by zero"])

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

    def test_board_full_pauses(self):
        repo = make_repo({"app.py": APP})
        store = {"version": 1, "findings": [{"id": f"f-{i:012x}", "status": "open", "project": "/x", "path": "p",
                                             "title": f"t{i}", "line": 1} for i in range(3)]}
        with open(os.path.join(self.state, "findings.json"), "w") as f:
            json.dump(store, f)
        self.assertEqual(self.run_once(repo, extra_env={"FINDINGS_OPEN_MAX": "3"}).returncode, 0)
        self.assertEqual(self.stub_log(), "")
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
