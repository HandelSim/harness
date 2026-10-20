#!/usr/bin/env python3
"""Docker-free, network-free tests for the --thoughts probe runner.

Run: python3 -m unittest tests/benchmarks/thoughts/test_thoughts.py
(mock_upstream.py is not used: it speaks the cooperative ```json route, not
the native/thoughts one; a fake `harness` that writes proxy-log lines stands
in for the proxy instead.)
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run  # noqa: E402
import swe_env  # noqa: E402


def usage(rid, p, c, r, tools=3, mode="native/slim", chars=3000, left=None):
    tail = f" credits_day_left={left}" if left is not None else ""
    return (f"[{rid}] POST /v1/chat/completions model=m messages=4 tools={tools}\n"
            f"[{rid}] upstream shape: mode={mode} messages=4 chars={chars}\n"
            f"[{rid}] upstream OK; relayed native SSE\n"
            f"[{rid}] usage: model=m prompt={p} completion={c} reasoning={r} cached=0{tail}\n")


class Plan(unittest.TestCase):
    def test_interleave(self):
        p = run.build_plan(["a", "b", "c", "d"])
        self.assertEqual([x[2] for x in p], [["slim", "thoughts"], ["thoughts", "slim"]] * 2)
        self.assertEqual([x[1] for x in p], ["a", "b", "c", "d"])

    def test_task_order_and_fields(self):
        t = run.load_tasks()
        self.assertEqual(len(t), 38)
        self.assertEqual(t, sorted(t, key=lambda x: (-x["solve_rate"], x["id"])))
        self.assertEqual(set(t[0]), {"id", "repo", "difficulty", "solve_rate"})


class Guard(unittest.TestCase):
    def test_p75(self):
        self.assertEqual(run.p75([]), 0.0)
        self.assertEqual(run.p75([1, 2, 3, 4, 5]), 4.0)

    def test_cap_stands_in_until_two_trials(self):
        g = run.guard(0, 20_000_000, [100], 1_500_000)
        self.assertTrue(g["ok"])
        self.assertEqual(g["projected_pair"], 3_000_000)
        self.assertEqual(g["eff_cap"], 1_500_000)

    def test_stops_when_projection_exceeds_budget(self):
        g = run.guard(18_000_000, 20_000_000, [900_000, 1_100_000, 1_200_000], 1_500_000)
        self.assertFalse(g["ok"])  # 18M + 2*1.15M > 20M
        g = run.guard(17_000_000, 20_000_000, [900_000, 1_100_000, 1_200_000], 1_500_000)
        self.assertTrue(g["ok"])

    def test_eff_cap_shrinks_near_budget(self):
        g = run.guard(19_000_000, 20_000_000, [100_000, 200_000], 1_500_000)
        self.assertTrue(g["ok"])
        self.assertEqual(g["eff_cap"], 500_000)
        self.assertFalse(run.guard(20_000_000, 20_000_000, [1, 1], 10)["ok"])


class Meter(unittest.TestCase):
    def test_counts_usage_and_estimates(self):
        m = run.LogMeter()
        m.feed(usage("1", 1000, 50, 20) + usage("2", 2000, 10, None, tools=0, left=99))
        m.feed("[3] POST /v1/chat/completions model=m messages=5 tools=3\n"
               "[3] upstream shape: mode=native/slim/thoughts messages=5 chars=900\n")
        tok, est = m.tokens()
        self.assertEqual(tok, 1070 + 2010 + 300)
        self.assertTrue(est)
        m.feed("[3] usage: model=m prompt=1200 completion=40 reasoning=60 cached=0\n")
        tok, est = m.tokens()
        self.assertEqual((tok, est), (1070 + 2010 + 1300, False))
        s = m.summary()
        self.assertEqual(s["steps"], 2)
        self.assertEqual(s["title_requests"], 1)
        self.assertEqual(s["credits_day_left"], 99)
        self.assertEqual(s["shape_modes"], ["native/slim", "native/slim/thoughts"])

    def test_ignores_requests_from_before_the_window(self):
        m = run.LogMeter()
        m.feed("[9] usage: model=m prompt=99999 completion=1 reasoning=0 cached=0\n"
               "[9] upstream returned 500: x\n")
        self.assertEqual(m.tokens(), (0, False))
        self.assertEqual(m.summary()["errors"], 0)

    def test_errors_and_events(self):
        m = run.LogMeter()
        m.feed("[4] POST /v1/chat/completions model=m messages=5 tools=3\n"
               "[4] upstream shape: mode=native/slim/thoughts messages=5 chars=900 signatures_reattached=2\n"
               "[4] upstream returned 400: bad\n"
               "[4] WARN: retrying without the 2 reattached thought signature(s)\n"
               "[4] WARN: 'm' refuses reattached thought signatures; no longer reattaching them\n"
               "[4] upstream OK; relayed native JSON as SSE\n"
               "[4] usage: none reported by upstream credits_day_left=5\n")
        s = m.summary()
        self.assertEqual((s["errors"], s["status_counts"]), (1, {"400": 1}))
        self.assertEqual((s["ev_sig_retry"], s["ev_sig_off"], s["ev_usage_none"], s["ev_relay_json_sse"]),
                         (1, 1, 1, 1))
        self.assertEqual(m.tokens(), (0, False))  # errored, no usage: nothing billed

    def test_tail_restarts_on_truncation_and_pid_change(self):
        d = tempfile.mkdtemp()
        try:
            lp, pp = os.path.join(d, "proxy.log"), os.path.join(d, "proxy.pid")
            with open(pp, "w") as f:
                f.write("10")
            with open(lp, "w") as f:
                f.write("old line\n")
            t = run.Tail(lp, pp)
            self.assertEqual(t.read(), "")  # pre-existing content is skipped
            with open(lp, "a") as f:
                f.write("new\npart")
            self.assertEqual(t.read(), "new\n")
            with open(lp, "a") as f:
                f.write("ial\n")
            self.assertEqual(t.read(), "partial\n")
            with open(pp, "w") as f:
                f.write("11")
            with open(lp, "w") as f:
                f.write("restarted\n")
            self.assertEqual(t.read(), "restarted\n")
        finally:
            shutil.rmtree(d)


class Ledger(unittest.TestCase):
    def test_checks(self):
        self.assertFalse(run.ledger_check([], 100, 10)["stop"])
        e = [{"ts": 1, "prompt": 1000, "completion": 100, "reasoning": 0, "remaining": {"credits-day": "7"}}]
        self.assertFalse(run.ledger_check(e, 1100, 10)["stop"])
        self.assertTrue(run.ledger_check(e, 700, 10)["stop"])
        e[0]["remaining"] = {"tokens-day": "5000"}
        r = run.ledger_check(e, 1100, 6000)
        self.assertTrue(r["stop"])
        self.assertIn("tokens left", r["why"])

    def test_window(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "u.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"ts": 5}) + "\nnot json\n" + json.dumps({"ts": 50}) + "\n")
            self.assertEqual(len(run.ledger_window(p, 4, 10)), 1)
            self.assertEqual(run.ledger_window(os.path.join(d, "none"), 0, 1), [])
        finally:
            shutil.rmtree(d)


class SweEval(unittest.TestCase):
    def test_parsers_and_grade(self):
        st = swe_env.parse_sympy("test_a ok\ntest_b F\n___ sympy/x/tests/test_y.py:test_c ___\n")
        self.assertEqual(st, {"test_a": "PASSED", "test_b": "FAILED", "sympy/x/tests/test_y.py:test_c": "FAILED"})
        st = swe_env.parse_pytest("PASSED t.py::a\nFAILED t.py::b - boom\nXFAIL t.py::c\n")
        self.assertEqual(swe_env.grade(st, ["t.py::a", "t.py::c"], ["t.py::b"])["resolved"], False)
        self.assertTrue(swe_env.grade(st, ["t.py::a"], ["t.py::c"])["resolved"])
        self.assertFalse(swe_env.grade(st, ["t.py::missing"], [])["resolved"])
        st = swe_env.parse_django("test_x (m.T) ... ok\ntest_y (m.T) ... FAIL\ntest_z (m.T) ... skipped 'n'\n")
        self.assertEqual(st, {"test_x (m.T)": "PASSED", "test_y (m.T)": "FAILED", "test_z (m.T)": "SKIPPED"})
        st = swe_env.parse_pytest_options("PASSED a.py::t[/tmp/x/y.py]\n")
        self.assertEqual(st, {"a.py::t[/y.py]": "PASSED"})

    def test_directives(self):
        tp = "diff --git a/tests/forms/test_x.py b/tests/forms/test_x.py\ndiff --git a/tests/d.json b/tests/d.json\n"
        self.assertEqual(swe_env.test_directives({"repo": "django/django", "test_patch": tp}), ["forms.test_x"])
        self.assertEqual(swe_env.test_directives({"repo": "sympy/sympy", "test_patch": tp}), ["tests/forms/test_x.py"])

    def test_specs_cover_every_task(self):
        versions = {"django/django": ["3.0", "3.1", "4.1", "4.2", "5.0"], "sympy/sympy": ["1.0", "1.9"],
                    "pytest-dev/pytest": ["5.2", "5.4", "6.0"], "sphinx-doc/sphinx": ["3.2", "4.0", "5.2"],
                    "pylint-dev/pylint": ["2.14", "2.15"]}
        for t in run.load_tasks():
            self.assertIn(t["repo"], versions)
        self.assertEqual(swe_env.spec("django/django", "3.0")["python"], "3.6")
        self.assertNotIn("Jinja2<3.0", " ".join(swe_env.spec("sphinx-doc/sphinx", "5.2")["pre"]))
        self.assertIn("pluggy==0.13.1", swe_env.spec("pytest-dev/pytest", "5.4")["pip"])

    def test_load_rows_from_local_dump_no_network(self):
        d = tempfile.mkdtemp()
        try:
            dump = os.path.join(d, "rows0.json")
            with open(dump, "w") as f:
                json.dump({"rows": [{"row": {"instance_id": "x__y-1", "repo": "x/y"}}], "num_rows_total": 1}, f)
            rows = swe_env.load_rows(["x__y-1"], os.path.join(d, "data"), dump, fetch=False)
            self.assertEqual(rows["x__y-1"]["repo"], "x/y")
            rows = swe_env.load_rows(["x__y-1"], os.path.join(d, "data"), "", fetch=False)  # now from cache
            self.assertEqual(rows["x__y-1"]["repo"], "x/y")
            with self.assertRaises(SystemExit):
                swe_env.load_rows(["nope"], os.path.join(d, "data"), "", fetch=False)
        finally:
            shutil.rmtree(d)


def fake_row(n=1):
    return {"pair": n, "arm": "slim", "task": f"t{n}", "passed": True, "tokens": 1000, "steps": 4, "secs": 10,
            "secs_per_step": 2, "per_step": {"prompt": 200, "completion": 30, "reasoning": 0},
            "arm_ok": True, "patch_lines": 5, "infra": ""}


class ResumeReport(unittest.TestCase):
    def test_pairs_and_report(self):
        rows = []
        for n in range(3):
            a, b = fake_row(n), dict(fake_row(n), arm="thoughts", tokens=1500, passed=n != 0,
                                     per_step={"prompt": 200, "completion": 30, "reasoning": 90})
            rows += [a, b]
        rows.append(dict(fake_row(3), infra="upstream", tokens=777))
        self.assertEqual(sorted(run.pair_rows(rows)), [0, 1, 2])
        text = run.report(rows, {"budget": 20_000_000, "model": "m"})
        self.assertIn("guardrail, underpowered", text)
        self.assertIn("spent: 8,277 tokens", text)  # infra tokens count against the budget
        self.assertIn("slim-only 1, thoughts-only 0", text)
        self.assertIn("one-arm-passed pairs", text)
        self.assertIn("no complete pairs", run.report([fake_row(0)], {}))

    def test_lost_inflight_is_booked(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "inflight.json"), "w") as f:
                json.dump({"pair": 2, "task": "t", "arm": "thoughts", "tokens": 4242}, f)
            res = os.path.join(d, "results.jsonl")
            run.account_lost_inflight(d, res)
            r = run.read_results(res)
            self.assertEqual((r[0]["infra"], r[0]["tokens"]), ("lost", 4242))
            self.assertFalse(os.path.exists(os.path.join(d, "inflight.json")))
            self.assertEqual(run.pair_rows(r), {})
        finally:
            shutil.rmtree(d)

    def test_report_cli(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "results.jsonl"), "w") as f:
                for r in (fake_row(0), dict(fake_row(0), arm="thoughts")):
                    f.write(json.dumps(r) + "\n")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(["--report", d]), 0)
            self.assertTrue(os.path.isfile(os.path.join(d, "report.txt")))
        finally:
            shutil.rmtree(d)

    def test_dry_run_spends_nothing(self):
        d = tempfile.mkdtemp()
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = run.main(["--dry-run", "--cache-dir", d, "--harness-bin", os.path.join(d, "no-harness"),
                               "--install-root", d])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("thoughts-slim", out)
            self.assertIn("no tokens spent", out)
            self.assertEqual(len([ln for ln in out.splitlines() if "slim-thoughts" in ln or "thoughts-slim" in ln]), 38)
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                run.main(["--dry-run", "--budget", "100", "--trial-token-cap", "60"])
        finally:
            shutil.rmtree(d)



GOLD = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
TESTP = ("diff --git a/test_fix.py b/test_fix.py\nnew file mode 100644\n--- /dev/null\n+++ b/test_fix.py\n"
         "@@ -0,0 +1,2 @@\n+import a\n+print('PASSED test_fix.py::t' if a.x == 2 else 'FAILED test_fix.py::t')\n")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, check=True).stdout


@unittest.skipIf(os.name == "nt" or not shutil.which("git"), "needs bash and git")
class Checkout(unittest.TestCase):
    """Each task checkout is its own one-commit repo, and the gold/test
    patches only ever touch it inside evaluate(), which resets after."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        up = os.path.join(self.d, "upstream")
        os.makedirs(up)
        with open(os.path.join(up, "a.py"), "w") as f:
            f.write("x = 1\n")
        _git(up, "init", "-q")
        _git(up, *swe_env._GIT_ID, "add", "-A")
        _git(up, *swe_env._GIT_ID, "commit", "-qm", "c1")
        with open(os.path.join(up, "later.txt"), "w") as f:
            f.write("future history\n")
        _git(up, *swe_env._GIT_ID, "add", "-A")
        _git(up, *swe_env._GIT_ID, "commit", "-qm", "c2")
        self.base = _git(up, "rev-parse", "HEAD~1").strip()
        self.up = up
        self.src = os.path.join(self.d, "cache", "envs", "x__y-1", "src")
        swe_env.make_checkout(up, self.base, self.src)
        swe_env.commit_baseline(self.src)
        self.env = {"src": self.src, "venv": os.path.join(self.d, "venv")}
        self.row = {"repo": "x/y", "version": "1.0", "instance_id": "x__y-1", "patch": GOLD,
                    "test_patch": TESTP, "FAIL_TO_PASS": '["test_fix.py::t"]', "PASS_TO_PASS": "[]"}
        self._spec, swe_env.spec = swe_env.spec, lambda r, v: {"test_cmd": shutil.which("python3") or "python3"}
        swe_env.PARSERS["x/y"] = swe_env.parse_pytest

    def tearDown(self):
        swe_env.spec = self._spec
        swe_env.PARSERS.pop("x/y", None)
        shutil.rmtree(self.d)

    def assert_clean(self):
        self.assertEqual(_git(self.src, "status", "--porcelain"), "")
        self.assertFalse(os.path.exists(os.path.join(self.src, "test_fix.py")))
        with open(os.path.join(self.src, "a.py")) as f:
            self.assertEqual(f.read(), "x = 1\n")

    def test_own_repo_no_history_no_remote(self):
        self.assertEqual(os.path.realpath(_git(self.src, "rev-parse", "--show-toplevel").strip()),
                         os.path.realpath(self.src))
        self.assertEqual(_git(self.src, "rev-list", "--count", "HEAD").strip(), "1")
        self.assertEqual(_git(self.src, "remote").strip(), "")
        self.assertFalse(os.path.exists(os.path.join(self.src, "later.txt")))
        self.assert_clean()

    def test_patches_only_inside_grading(self):
        base = swe_env.evaluate(self.env, self.row, "")
        self.assertEqual((base["resolved"], base["f2p_pass"]), (False, 0))
        self.assert_clean()
        gold = swe_env.evaluate(self.env, self.row, GOLD)
        self.assertTrue(gold["resolved"])
        self.assert_clean()
        # The agent's edit is graded from `git diff pristine`, which never holds the test patch.
        with open(os.path.join(self.src, "a.py"), "w") as f:
            f.write("x = 2\n")
        diff = swe_env.capture_diff(self.env)
        self.assertIn("+x = 2", diff)
        self.assertNotIn("test_fix", diff)
        self.assertTrue(swe_env.evaluate(self.env, self.row, diff)["resolved"])
        self.assert_clean()
        bad = swe_env.evaluate(self.env, self.row, "diff --git a/nope b/nope\n--- a/nope\n+++ b/nope\n@@ -1 +1 @@\n-q\n+r\n")
        self.assertFalse(bad["applied"])
        self.assert_clean()

    def test_gate_artifacts_live_outside_the_env(self):
        p = swe_env.env_paths(os.path.join(self.d, "cache"), "x__y-1")
        self.assertFalse(p["gate"].startswith(p["root"] + os.sep))
        self.assertTrue(p["gate"].startswith(os.path.join(self.d, "cache", "data") + os.sep))


class Contamination(unittest.TestCase):
    def test_needles_and_match(self):
        d = tempfile.mkdtemp()
        try:
            ds = os.path.join(d, "swev")
            os.makedirs(ds)
            open(os.path.join(ds, "rows100.json"), "w").close()
            cache = os.path.join(d, "mycache")
            n = run.contamination_needles(cache, ds, os.path.join(d, "thoughts-20261020-1"))
            for x in ("mycache", "bench-thoughts", "rows.json", "rows100.json", "swev", "thoughts-20261020-1"):
                self.assertIn(x, n)
            src = os.path.join(cache, "envs", "t", "src")
            self.assertFalse(run.is_contaminated(json.dumps({"filePath": src + "/a.py", "cwd": src}), n, src))
            for bad in ("cat ../../../data/rows.json", "ls ~/x/mycache", "grep -r fix " + ds + "/rows100.json",
                        "find / -name 'bench-thoughts'", "cat ../../../data/gates/t/gate-gold.log",
                        "ls tests/benchmarks/runs"):
                self.assertTrue(run.is_contaminated(json.dumps({"command": bad}), n, src), bad)
        finally:
            shutil.rmtree(d)

    def test_opencode_metrics_counts_contaminated_tool_inputs(self):
        import sqlite3
        d = tempfile.mkdtemp()
        try:
            db, src = os.path.join(d, "oc.db"), "/w/bench-thoughts/envs/t/src"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, time_created INT)")
            con.execute("CREATE TABLE part (id TEXT, session_id TEXT, data TEXT)")
            con.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
            con.execute("INSERT INTO session VALUES ('s1', NULL, ?, 1000000)", (src,))
            con.execute("INSERT INTO session VALUES ('s2', 's1', ?, 1000000)", (src,))
            parts = [{"type": "tool", "state": {"status": "completed", "input": {"filePath": src + "/a.py"}}},
                     {"type": "tool", "state": {"status": "error", "input": {"command": "cat ../../../data/rows.json"}}},
                     {"type": "reasoning", "text": "hm"}]
            for i, p in enumerate(parts):
                con.execute("INSERT INTO part VALUES (?, ?, ?)", (str(i), "s2" if i else "s1", json.dumps(p)))
            con.execute("INSERT INTO message VALUES ('m', 's1', ?)",
                        (json.dumps({"role": "assistant", "tokens": {"output": 0}}),))
            con.commit()
            con.close()
            m = run.opencode_metrics(db, src, 999, 1001, run.contamination_needles("/w/bench-thoughts"))
            self.assertEqual((m["oc_sessions"], m["tool_calls"], m["tool_errors"], m["reasoning_parts"],
                              m["empty_replies"], m["contaminated"]), (2, 2, 1, 1, 1, 1))
        finally:
            shutil.rmtree(d)


FAKE_HARNESS = r"""#!/bin/bash
# Stand-in for `harness host`: a proxy that bills 1000 tokens a request.
h="$HARNESS_INSTALL_ROOT/state/host"; mkdir -p "$h"; echo $$ > "$h/proxy.pid"; : > "$h/proxy.log"
for i in $(seq 1 200); do
  id="r$i"
  printf '[%s] POST /v1/chat/completions model=m messages=3 tools=5\n[%s] upstream shape: mode=native/slim/thoughts messages=3 chars=30\n[%s] usage: model=m prompt=900 completion=60 reasoning=40 cached=0\n' "$id" "$id" "$id" >> "$h/proxy.log"
  sleep 0.1
done
"""


@unittest.skipIf(os.name == "nt" or not shutil.which("git"), "needs bash and git")
class CapKill(unittest.TestCase):
    def test_trial_is_killed_at_the_cap(self):
        d = tempfile.mkdtemp()
        try:
            hb = os.path.join(d, "harness")
            with open(hb, "w") as f:
                f.write(FAKE_HARNESS)
            os.chmod(hb, 0o755)
            src = os.path.join(d, "src")
            os.makedirs(src)
            g = ["git", "-C", src, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            subprocess.run(g[:3] + ["init", "-q"], check=True)
            with open(os.path.join(src, "a.py"), "w") as f:
                f.write("x = 1\n")
            subprocess.run(g + ["add", "-A"], check=True)
            subprocess.run(g + ["commit", "-qm", "p"], check=True)
            subprocess.run(g + ["tag", "pristine"], check=True)
            os.makedirs(os.path.join(d, "out", "trials"))
            ctx = {"harness": hb, "bench_root": os.path.join(d, "root"), "port": 1, "timeout": 60,
                   "redact": lambda s: s, "out": os.path.join(d, "out"), "first": False, "needles": ["rows.json"],
                   "ledger": os.path.join(d, "none.jsonl"), "oc_db": os.path.join(d, "none.db"), "next_pair": 0}
            env = {"src": src, "venv": os.path.join(d, "venv")}
            row = {"repo": "x/y", "problem_statement": "p", "version": "1.0"}
            r = run.run_trial(0, 1, {"id": "t"}, row, env, "thoughts", 5000, ctx)
            self.assertTrue(r["capped"])
            self.assertFalse(r["passed"])
            self.assertGreaterEqual(r["tokens"], 5000)
            self.assertLess(r["tokens"], 5000 + 4000)  # overshoot bounded by the poll interval
            self.assertTrue(r["arm_ok"])
            self.assertEqual(r["per_step"], {"prompt": 900, "completion": 60, "reasoning": 40})
            self.assertEqual(r["infra"], "")
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
