"""Docker-free unit tests for the --test-modes runner (run.py) and its mock.

Run: python3 -m unittest tests/benchmarks/modes/test_modes.py
(tests/unit_bench_modes_test.sh runs this as part of `harness test unit`).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mock_upstream  # noqa: E402
import run  # noqa: E402


class TestStats(unittest.TestCase):
    def test_wilson(self):
        self.assertEqual(run.wilson(0, 0), (0.0, 1.0))
        lo, hi = run.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)
        lo, hi = run.wilson(10, 10)
        self.assertEqual(hi, 1.0)
        self.assertGreater(lo, 0.69)

    def test_mcnemar_exact(self):
        self.assertEqual(run.mcnemar_exact(0, 0), 1.0)
        self.assertEqual(run.mcnemar_exact(3, 3), 1.0)
        self.assertAlmostEqual(run.mcnemar_exact(0, 6), 2 / 64)
        self.assertAlmostEqual(run.mcnemar_exact(6, 0), 2 / 64)
        self.assertAlmostEqual(run.mcnemar_exact(1, 5), 2 * 7 / 64)

    def test_mean_ci(self):
        m, h = run.mean_ci([1.0, 2.0, 3.0])
        self.assertEqual(m, 2.0)
        self.assertAlmostEqual(h, 1.96 * 1.0 / 3 ** 0.5)
        m, h = run.mean_ci([4.0])
        self.assertEqual(m, 4.0)
        self.assertNotEqual(h, h)  # nan


class TestPlan(unittest.TestCase):
    def test_block_interleaved(self):
        tasks = [{"id": t} for t in ("a", "b", "c")]
        plan = run.build_plan(tasks, ["hybrid", "single"], 3)
        self.assertEqual(len(plan), 18)
        self.assertEqual(len(set(plan)), 18)
        # mode order alternates per repeat: AB, BA, AB
        firsts = [next(m for r, m, _ in plan if r == rep) for rep in range(3)]
        self.assertEqual(firsts, ["hybrid", "single", "hybrid"])
        # within a repeat, each mode is one contiguous block (one proxy restart)
        for rep in range(3):
            ms = [m for r, m, _ in plan if r == rep]
            switches = sum(1 for i in range(1, len(ms)) if ms[i] != ms[i - 1])
            self.assertEqual(switches, 1)

    def test_unknown_task_rejected(self):
        with self.assertRaises(SystemExit):
            run.load_tasks(["no-such-task"])


LOG = """\
[r1] POST /v1/chat/completions model=m messages=3 tools=0
[r1] upstream shape: mode=single messages=1 chars=900
[r2] POST /v1/chat/completions model=m messages=2 tools=9
[r2] catalog: tools=9 schema_tokens=7000
[r2] upstream shape: mode=single messages=1 chars=43000
[r2] in-proxy retry for malformed tool call (kind=x); attempt=1, recovered=yes
[r2] upstream OK; emitting OpenAI SSE (tool_calls=1)
[r3] POST /v1/chat/completions model=m messages=4 tools=9
[r3] upstream shape: mode=single messages=1 chars=45000
[r3] upstream returned 502: {"error": "upstream_error"}
[r4] POST /v1/chat/completions model=m messages=6 tools=9
[r4] upstream shape: mode=single messages=1 chars=47000
[r4] require-tool: rejected a tool-less message; recovered (tool_calls=1)
[r4] require-tool: consumed `finish`
"""


class TestProxyLog(unittest.TestCase):
    def test_counters(self):
        c = run.parse_proxy_log(LOG)
        self.assertEqual(c["requests"], 3)
        self.assertEqual(c["title_requests"], 1)
        self.assertEqual(c["errors"], 1)
        self.assertEqual(c["auth_errors"], 0)
        self.assertEqual(c["malformed_retries"], 1)
        self.assertEqual(c["reasks"], 1)
        self.assertEqual(c["finishes"], 1)
        self.assertEqual(c["shape_modes"], ["single"])
        self.assertEqual(c["max_messages"], 1)
        self.assertEqual(c["max_chars"], 47000)
        self.assertTrue(run.mode_verified("single", c))
        self.assertFalse(run.mode_verified("hybrid", c))

    def test_transient_errors_and_attribution(self):
        log = ("[old] upstream returned 401: late line from a killed trial\n"
               "[old] require-tool: consumed `finish`\n"
               "[a] POST /v1/chat/completions model=m messages=2 tools=9\n"
               "[a] upstream shape: mode=single messages=1 chars=100\n"
               "[a] upstream returned 503: busy\n"
               "[b] POST /v1/chat/completions model=m messages=3 tools=9\n"
               "[b] upstream shape: mode=single messages=1 chars=200\n"
               "[b] upstream returned 400: context too long\n"
               "[c] POST /v1/chat/completions model=m messages=4 tools=9\n"
               "[c] upstream request failed: timeout\n"
               "[c] retry upstream returned 429: slow down\n")
        c = run.parse_proxy_log(log)
        self.assertEqual(c["auth_errors"], 0)  # [old] is outside this window
        self.assertEqual(c["finishes"], 0)
        self.assertEqual(c["errors"], 3)  # 503, 400, request failed (retry lines are not _RE_ERR)
        self.assertEqual(c["transient_errors"], 2)  # 503 + request failed; the 400 is real

    def test_auth_error_and_hybrid(self):
        log = ("[a] POST /v1/chat/completions model=m messages=2 tools=9\n"
               "[a] upstream shape: mode=hybrid messages=3 chars=100\n"
               "[a] upstream returned 401: locked\n")
        c = run.parse_proxy_log(log)
        self.assertEqual(c["auth_errors"], 1)
        self.assertTrue(run.mode_verified("hybrid", c))
        # single must send exactly one message: a hybrid-shaped request fails it
        log2 = log.replace("mode=hybrid", "mode=single")
        self.assertFalse(run.mode_verified("single", run.parse_proxy_log(log2)))
        self.assertFalse(run.mode_verified("hybrid", run.parse_proxy_log("")))


class TestCheckers(unittest.TestCase):
    """Each task's check.py must fail on the seed files and pass on its
    mock_solution, or the benchmark scores noise."""

    def test_every_task(self):
        tasks = run.load_tasks(None)
        self.assertGreaterEqual(len(tasks), 6)
        for t in tasks:
            with self.subTest(task=t["id"]), tempfile.TemporaryDirectory() as d:
                w = os.path.join(d, "w")
                shutil.copytree(os.path.join(t["dir"], "files"), w)
                chk = [sys.executable, os.path.join(t["dir"], "check.py")]
                seed = subprocess.run(chk, cwd=w, capture_output=True, text=True)
                self.assertNotEqual(seed.returncode, 0, f"seed passes: {seed.stdout}")
                with open(os.path.join(t["dir"], "task.json"), encoding="utf-8") as f:
                    sol = json.load(f)["mock_solution"]
                subprocess.run(["bash", "-c", sol], cwd=w, capture_output=True, check=True)
                done = subprocess.run(chk, cwd=w, capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, f"solution fails: {done.stdout}")


class TestMock(unittest.TestCase):
    TASKS = [("Fix the bug in stats.py please", "sed -i x stats.py")]
    CAT = "<<<BEGIN_AGENT_TOOLS>>>\nbash: run a command\n<<<END_AGENT_TOOLS>>>\n"

    def test_title_request_gets_text(self):
        msgs = [{"role": "user", "content": "Generate a title for: Fix the bug in stats.py please"}]
        self.assertEqual(mock_upstream.reply_for(msgs, self.TASKS), "Benchmark task")

    def test_first_turn_runs_solution(self):
        msgs = [{"role": "user", "content": self.CAT + "Fix the bug in stats.py please"}]
        r = mock_upstream.reply_for(msgs, self.TASKS)
        self.assertIn('"bash"', r)
        self.assertIn("sed -i x stats.py", r)

    def test_tool_result_finishes(self):
        msgs = [{"role": "user", "content": self.CAT + "Fix the bug in stats.py please"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "<<<BEGIN_TOOL_RESULT>>> done <<<END_TOOL_RESULT>>>"}]
        self.assertIn('"finish"', mock_upstream.reply_for(msgs, self.TASKS))

    def test_single_fold_looks_at_current_turn_only(self):
        body = (self.CAT + "<conversation><turn n=\"1\" role=\"tool\"><<<BEGIN_TOOL_RESULT>>> old"
                "</turn></conversation>\n<current_turn>Fix the bug in stats.py please</current_turn>")
        r = mock_upstream.reply_for([{"role": "user", "content": body}], self.TASKS)
        self.assertIn('"bash"', r)
        body2 = body.replace("<current_turn>Fix", "<current_turn><<<BEGIN_TOOL_RESULT>>> Fix")
        self.assertIn('"finish"', mock_upstream.reply_for([{"role": "user", "content": body2}], self.TASKS))


class TestEnvAndRedaction(unittest.TestCase):
    def test_child_env(self):
        saved = dict(os.environ)
        try:
            os.environ.update({"OPENCODE_ENABLE_EXA": "1", "OPENCODE_CONFIG_DIR": "/x",
                               "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector", "PROXY_API_KEY": "sk-real",
                               "HARNESS_HOST_PORT": "1"})
            env = run.child_env("/b", 4321, mock=False, first=True)
            self.assertNotIn("OPENCODE_ENABLE_EXA", env)
            self.assertNotIn("OPENCODE_CONFIG_DIR", env)
            self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", env)
            self.assertEqual(env["HARNESS_HOST_PORT"], "4321")
            self.assertEqual(env["HARNESS_HOST_NO_WEB"], "1")
            self.assertEqual(env["HARNESS_INSTALL_ROOT"], "/b")
            self.assertEqual(env["OPENCODE_DISABLE_SHARE"], "1")
            self.assertEqual(env["OPENCODE_DISABLE_CLAUDE_CODE"], "1")
            self.assertTrue(env["XDG_CONFIG_HOME"].startswith("/b"))
            self.assertNotIn("HARNESS_SKIP_AUTH_PROBE", env)
            self.assertEqual(env["PROXY_API_KEY"], "sk-real")  # real mode: as normal
            menv = run.child_env("/b", 4321, mock=True, first=False)
            self.assertNotIn("PROXY_API_KEY", menv)  # the mock never sees the real key
            self.assertEqual(menv["HARNESS_SKIP_AUTH_PROBE"], "1")
            self.assertIn("127.0.0.1", menv["NO_PROXY"])
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_redactor(self):
        saved = dict(os.environ)
        try:
            os.environ["PROXY_API_KEY"] = "sk-abcdef0123456789xyz"
            os.environ["PROXY_API_URL"] = "https://gw.secretcorp.example.com"
            red = run.make_redactor()
            fwd = os.path.expanduser("~").replace("\\", "/")
            if len(fwd) > 1:
                self.assertNotIn(fwd, red(f"cd {fwd}/proj"))
            home = os.path.expanduser("~")
            s = red(f"key sk-abcdef0123456789xyz at https://gw.secretcorp.example.com/v1 in {home}/x")
            self.assertNotIn("sk-abcdef", s)
            self.assertNotIn("secretcorp", s)
            if len(home) > 1:
                self.assertNotIn(home, s)
        finally:
            os.environ.clear()
            os.environ.update(saved)


class TestSetup(unittest.TestCase):
    def test_warm_up_failure_is_reported_redacted(self):
        d = tempfile.mkdtemp()
        try:
            fake = os.path.join(d, "harness")
            with open(fake, "w") as f:
                f.write('echo "host mode: checksum mismatch for $HOME/x" >&2; return 3\n')
            msg = run.warm_up(fake, dict(os.environ), lambda s: s.replace(os.path.expanduser("~"), "~"))
            self.assertIn("failed (exit 1)", msg)
            self.assertIn("checksum mismatch for ~/x", msg)
            with open(fake, "w") as f:
                f.write("host_require_python3() { :; }; ensure_dirs() { :; }; host_ensure_toolchain() { :; }\n"
                        "host_preflight() { :; }; host_proxy_ensure_venv() { :; }\n")
            self.assertEqual(run.warm_up(fake, dict(os.environ), str), "")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_prepare_root_shares_toolchain(self):
        d = tempfile.mkdtemp()
        try:
            real, bench = os.path.join(d, "real"), os.path.join(d, "bench")
            os.makedirs(os.path.join(real, "state", "host", "toolchain", "bin"))
            with open(os.path.join(real, ".env"), "w") as f:
                f.write("X=1\n")
            run.prepare_root(real, bench, None)
            run.prepare_root(real, bench, None)  # idempotent
            self.assertTrue(os.path.isdir(os.path.join(bench, "state", "host", "toolchain", "bin")))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestReport(unittest.TestCase):
    def _r(self, task, mode, rep, passed, secs=10.0, req=3, infra=""):
        return {"task": task, "mode": mode, "repeat": rep, "passed": passed, "secs": secs,
                "requests": req, "errors": 0, "timed_out": False, "infra": infra, "mode_ok": True,
                "rc": 0, "shape_modes": [mode], "max_messages": 1}

    def test_report_pairs_and_excludes_infra(self):
        res = []
        for i in range(8):
            res.append(self._r(f"t{i}", "hybrid", 0, False, secs=20))
            res.append(self._r(f"t{i}", "single", 0, True, secs=10))
        res.append(self._r("t0", "single", 1, False, infra="auth"))
        txt = run.report(res, ["hybrid", "single"], {"repeats": 1})
        self.assertIn("only hybrid passed: 0   only single passed: 8", txt)
        self.assertIn("single passes significantly more often", txt)
        self.assertIn("1 infra failures excluded", txt)
        self.assertIn("single is faster", txt)

    def test_report_inconclusive(self):
        res = [self._r("a", "hybrid", 0, True), self._r("a", "single", 0, True)]
        txt = run.report(res, ["hybrid", "single"], {})
        self.assertIn("no significant pass-rate difference", txt)


if __name__ == "__main__":
    unittest.main()
