"""Tests for findings_agent_view.py: the findings agent's card and its actions.

  cd serve && python3 -m unittest -q test_findings_agent_view
"""
import json
import os
import shutil
import tempfile
import time
import unittest

import findings_agent_view as fav

FAKE_HARNESS = """#!/usr/bin/env bash
{ printf 'args=%s\\n' "$*"; printf 'cwd=%s\\n' "$PWD"; printf 'root=%s\\n' "${HARNESS_INSTALL_ROOT:-}";
  printf 'open=%s\\n' "${HARNESS_OPEN:-}"; } >"$LOG"
[[ "${FAIL:-}" == 1 ]] && { echo "no upstream login"; exit 1; }
echo "[findings] done"
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fav-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = os.path.join(self.root, "state", "findings")
        os.makedirs(self.state)

    def status(self, **kw):
        info = {"pid": os.getpid(), "state": "scanning", "heartbeat_ts": time.time(), "model": "m-assist"}
        info.update(kw)
        with open(os.path.join(self.state, "agent.json"), "w") as f:
            json.dump(info, f)


class CardTest(Base):
    def test_never_started(self):
        h = fav.agent_card(self.state)
        self.assertIn("never started", h)
        self.assertIn("action='/runs/findings/agent/start'", h)
        self.assertIn("name='notes'", h)
        self.assertIn("maxlength='4000'", h)
        self.assertIn("href='/settings#model'>Change the model in Settings</a>", h)

    def test_running_escapes_everything(self):
        self.status(unit="<b>x.py</b>", last_error="<script>alert(1)</script>", repos=["/r/<i>"],
                    counts={"bug": {"calls": 3, "accepted": 1, "rejected": 2, "failed": 0},
                            "<x>": {"calls": "7"}}, model="<m>", open=4)
        with open(os.path.join(self.state, "notes.md"), "w") as f:
            f.write("</textarea><script>bad()</script>")
        h = fav.agent_card(self.state, flash="<saved>")
        self.assertNotIn("<script>", h)
        self.assertNotIn("<b>x.py", h)
        self.assertNotIn("<i>", h)
        self.assertNotIn("<x>", h)
        self.assertIn("&lt;script&gt;alert(1)", h)
        self.assertIn("&lt;/textarea&gt;", h)
        self.assertIn("<div class='flash'>&lt;saved&gt;</div>", h)
        self.assertIn("action='/runs/findings/agent/stop'", h)
        self.assertNotIn("/runs/findings/agent/start", h)
        self.assertIn("<td>bug</td><td>3</td><td>1</td><td>2</td><td>0</td>", h)
        self.assertIn("exploring", h)   # an old agent.json's "scanning" is shown as exploring
        self.assertIn("Change the model in Settings (then Stop and Start)</a>", h)

    def test_stale_heartbeat(self):
        self.status(heartbeat_ts=time.time() - 900)
        h = fav.agent_card(self.state)
        self.assertIn("not answering", h)
        self.assertIn("(stale)", h)
        self.assertIn("/runs/findings/agent/start", h)

    def test_dead_pid_and_stopped(self):
        self.status(pid=2 ** 22 + 12345)
        self.assertIn("not answering", fav.agent_card(self.state))
        self.status(state="stopped", heartbeat_ts=time.time() - 9000)
        h = fav.agent_card(self.state)
        self.assertIn(">stopped<", h)
        self.assertNotIn("(stale)", h)

    def test_bad_json(self):
        with open(os.path.join(self.state, "agent.json"), "w") as f:
            f.write("{nope")
        self.assertIn("never started", fav.agent_card(self.state))


class ActionTest(Base):
    def setUp(self):
        super().setUp()
        self.harness = os.path.join(self.root, "harness")
        with open(self.harness, "w") as f:
            f.write(FAKE_HARNESS)
        self.log = os.path.join(self.root, "log")
        os.environ["LOG"] = self.log
        self.addCleanup(os.environ.pop, "LOG", None)
        self.addCleanup(os.environ.pop, "FAIL", None)
        os.environ.pop("HARNESS_INSTALL_ROOT", None)

    def logged(self):
        with open(self.log) as f:
            return dict(line.split("=", 1) for line in f.read().splitlines())

    def test_notes(self):
        ok, msg = fav.agent_action(self.state, self.harness, "notes", {"notes": ["skip tests/\r\nand docs\n"]})
        self.assertTrue(ok, msg)
        with open(os.path.join(self.state, "notes.md")) as f:
            self.assertEqual(f.read(), "skip tests/\nand docs\n")
        ok, msg = fav.agent_action(self.state, self.harness, "notes", {"notes": ["x" * 4001]})
        self.assertFalse(ok)
        self.assertIn("4000", msg)
        self.assertEqual(os.listdir(self.state), ["notes.md"])   # no temp file left

    def test_start(self):
        project = os.path.join(self.root, "proj")
        os.makedirs(project)
        ok, msg = fav.agent_action(self.state, self.harness, "start", {}, project=project)
        self.assertTrue(ok, msg)
        got = self.logged()
        self.assertEqual(got["args"], "findings start")
        self.assertEqual(os.path.realpath(got["cwd"]), os.path.realpath(project))
        self.assertEqual(got["root"], self.root)
        self.assertEqual(got["open"], "0")
        with open(os.path.join(self.state, "agent.out")) as f:
            self.assertIn("started from the queue page", f.read())

    def test_start_failure_says_why(self):
        os.environ["FAIL"] = "1"
        ok, msg = fav.agent_action(self.state, self.harness, "start", {})
        self.assertFalse(ok)
        self.assertIn("no upstream login", msg)

    def test_start_refused_while_running(self):
        self.status()
        ok, msg = fav.agent_action(self.state, self.harness, "start", {})
        self.assertFalse(ok)
        self.assertIn("already running", msg)
        self.assertFalse(os.path.exists(self.log))

    def test_stop(self):
        ok, msg = fav.agent_action(self.state, self.harness, "stop", {})
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "[findings] done")
        self.assertEqual(self.logged()["args"], "findings stop")
        self.assertEqual(self.logged()["root"], self.root)

    def test_bad_inputs(self):
        self.assertFalse(fav.agent_action(self.state, self.harness, "nuke", {})[0])
        self.assertFalse(fav.agent_action("", self.harness, "start", {})[0])
        self.assertFalse(fav.agent_action(self.state, os.path.join(self.root, "none"), "start", {})[0])


class SessionsTest(Base):
    def ledger(self, passes):
        with open(os.path.join(self.state, "ledger.json"), "w") as f:
            json.dump({"version": 2, "passes": passes}, f)

    def test_sessions_listed_newest_first_and_linked(self):
        self.status()
        self.ledger([{"n": 1, "session": "ses_old1", "repo": "/r/harness", "area": "serve", "lens": "bug",
                      "outcome": "ok", "found": 0},
                     {"n": 2, "session": "ses_new2", "repo": "/r/harness", "area": "scripts", "lens": "ui",
                      "outcome": "ok", "found": 2, "covered": "<looked> at it"},
                     {"n": 3, "session": "ses_x'><script>", "repo": "/r/h", "area": "a", "lens": "bug",
                      "outcome": "failed"}, "junk"])
        got = [p["n"] for p in fav.passes(self.state)]
        self.assertEqual(got, [3, 2, 1])
        h = fav.agent_card(self.state)
        self.assertIn("href='/runs/ses_new2'", h)
        self.assertIn("href='/runs/ses_old1'", h)
        self.assertNotIn("<script>", h)
        self.assertNotIn("<looked>", h)
        self.assertLess(h.index("ses_new2"), h.index("ses_old1"))

    def test_no_ledger(self):
        self.assertEqual(fav.passes(self.state), [])


class ResumeTest(Base):
    def setUp(self):
        super().setUp()
        self.harness = os.path.join(self.root, "harness")
        with open(self.harness, "w") as f:
            f.write(FAKE_HARNESS)
        self.log = os.path.join(self.root, "log")
        os.environ["LOG"] = self.log
        self.addCleanup(os.environ.pop, "LOG", None)
        self.addCleanup(os.environ.pop, "FINDINGS_AUTORESUME", None)

    def test_not_wanted_is_left_alone(self):
        self.assertIsNone(fav.resume(self.state, self.harness))
        self.assertFalse(os.path.exists(self.log))

    def test_wanted_and_not_running_starts(self):
        open(os.path.join(self.state, "wanted"), "w").close()
        # a dead agent's agent.json and its lock file, as a restart leaves them
        self.status(pid=2 ** 22 + 12345, heartbeat_ts=time.time() - 3600)
        open(os.path.join(self.state, "agent.lock"), "w").close()
        ok, msg = fav.resume(self.state, self.harness)
        self.assertTrue(ok, msg)
        with open(self.log) as f:
            self.assertIn("args=findings start", f.read())

    def test_wanted_but_off(self):
        open(os.path.join(self.state, "wanted"), "w").close()
        os.environ["FINDINGS_AUTORESUME"] = "0"
        self.assertIsNone(fav.resume(self.state, self.harness))

    @unittest.skipIf(os.name == "nt", "flock")
    def test_held_lock_means_running(self):
        import fcntl
        open(os.path.join(self.state, "wanted"), "w").close()
        f = open(os.path.join(self.state, "agent.lock"), "a")
        self.addCleanup(f.close)
        self.assertFalse(fav._held(self.state))
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(fav._held(self.state))
        self.assertIsNone(fav.resume(self.state, self.harness))
        ok, msg = fav.agent_action(self.state, self.harness, "start", {})
        self.assertFalse(ok)
        self.assertIn("already running", msg)


if __name__ == "__main__":
    unittest.main()
