"""Tests for findings_agent_view.py: the findings agent's card and its actions.

  cd serve && python3 -m unittest -q test_findings_agent_view
"""
import json
import os
import shutil
import subprocess
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


class KnowledgeTest(Base):
    def write_notes(self):
        k = os.path.join(self.state, "knowledge", "repo-0123abcd")
        os.makedirs(k)
        with open(os.path.join(k, "_top.md"), "w") as f:
            f.write("# repo: ./\napp.py <does> things\n")
        with open(os.path.join(self.root, "secret.md"), "w") as f:
            f.write("not a note\n")
        notes = [{"repo": "/r/repo", "area": ".", "file": "repo-0123abcd/_top.md", "updated": "2026-11-04T10:00:00Z",
                  "session": "ses_1", "lens": "trace", "chars": 30, "summary": "app.py <does> things",
                  "changed": True},
                 {"repo": "/r/repo", "area": "x", "file": "../secret.md"},
                 {"repo": "/r/repo", "area": "y", "file": "repo-0123abcd/../../x.md"},
                 {"repo": "/r/repo", "area": "z", "file": 3}, "not a dict"]
        with open(os.path.join(self.state, "knowledge", "index.json"), "w") as f:
            json.dump({"version": 1, "notes": notes}, f)

    def test_index_and_text_checked(self):
        self.write_notes()
        (e,) = fav.knowledge_index(self.state)
        self.assertEqual((e["slug"], e["name"], e["area"], e["changed"], e["lens"]),
                         ("repo-0123abcd", "_top", ".", True, "trace"))
        self.assertIn("app.py <does>", fav.knowledge_text(self.state, "repo-0123abcd", "_top"))
        for slug, name in (("..", "secret"), ("repo-0123abcd", ".."), ("repo-0123abcd", "../../secret"),
                           (".", "secret"), ("repo-0123abcd", "nope"), ("", "_top")):
            self.assertIsNone(fav.knowledge_text(self.state, slug, name), (slug, name))
        self.assertEqual(fav.knowledge_index(os.path.join(self.root, "nowhere")), [])

    def test_card_links_the_notes(self):
        self.write_notes()
        self.status(todo=2, units=40, counts={"trace": {"calls": 2, "notes": 3}})
        h = fav.agent_card(self.state)
        self.assertIn("<a href='/runs/findings/notes'>What it has learned</a> <span class='muted'>1 area note</span>", h)
        self.assertIn("<dt>passes in the rotation</dt><dd>40</dd>", h)
        self.assertIn("<td>trace</td><td>2</td><td>0</td><td>0</td><td>0</td><td>3</td>", h)


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
        self.assertIn("<td>bug</td><td>3</td><td>1</td><td>2</td><td>0</td><td>0</td>", h)
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


class HarnessBase(Base):
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


class ActionTest(HarnessBase):
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


def git_repo(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", path], check=True, capture_output=True)
    return path


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class FoldersTest(HarnessBase):
    """The folders the card chooses: checked, saved, passed as harness findings start DIR..."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.environ.pop, "FINDINGS_AUTORESUME", None)
        self.r1 = git_repo(os.path.join(self.root, "r1"))
        self.r2 = git_repo(os.path.join(self.root, "r2"))
        self.project = git_repo(os.path.join(self.root, "proj"))

    def saved(self):
        p = os.path.join(self.state, "folders.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)["folders"]

    def test_saved_folders_are_the_argv(self):
        ok, msg = fav.agent_action(self.state, self.harness, "folders",
                                   {"pick": ["1"], "dir": [self.r1], "more": [f"\r\n {self.r2}/ \r\n{self.r1}\n"]},
                                   project=self.project)
        self.assertTrue(ok, msg)
        self.assertEqual(self.saved(), [self.r1, self.r2])   # trimmed, normalised, de-duplicated
        self.assertFalse(os.path.exists(self.log))           # Save does not start it
        self.assertFalse([n for n in os.listdir(self.state) if n.endswith(".tmp")])
        ok, msg = fav.agent_action(self.state, self.harness, "start", {}, project=self.project)
        self.assertTrue(ok, msg)
        got = self.logged()
        self.assertEqual(got["args"], f"findings start {self.r1} {self.r2}")
        self.assertEqual(os.path.realpath(got["cwd"]), os.path.realpath(self.project))
        self.assertIn(self.r2, msg)

    def test_start_with_the_form_saves_what_is_ticked(self):
        ok, msg = fav.agent_action(self.state, self.harness, "start",
                                   {"pick": ["1"], "dir": [self.r2], "more": [""]}, project=self.project)
        self.assertTrue(ok, msg)
        self.assertEqual(self.logged()["args"], f"findings start {self.r2}")
        self.assertEqual(self.saved(), [self.r2])

    def test_default_with_nothing_saved_is_unchanged(self):
        # the card's Start with the default ticked (serve's project): as before, no DIR, in the project
        ok, msg = fav.agent_action(self.state, self.harness, "start",
                                   {"pick": ["1"], "dir": [self.project]}, project=self.project)
        self.assertTrue(ok, msg)
        got = self.logged()
        self.assertEqual(got["args"], "findings start")
        self.assertEqual(os.path.realpath(got["cwd"]), os.path.realpath(self.project))
        self.assertIsNone(self.saved())
        h = fav.agent_card(self.state, project=self.project, known=[self.r1])
        self.assertIn(f"value='{self.project}' checked>", h)
        self.assertIn(f"value='{self.r1}'><span><code>", h)    # offered, not ticked
        self.assertIn("(the default: the project harness serve runs in)", h)
        self.assertNotIn("Forget the choice", h)

    def test_reset_forgets_the_choice(self):
        fav.save_folders(self.state, [self.r1])
        h = fav.agent_card(self.state, project=self.project)
        self.assertIn(f"value='{self.r1}' checked>", h)
        self.assertIn(f"value='{self.project}'><span><code>", h)
        self.assertIn("Forget the choice", h)
        ok, msg = fav.agent_action(self.state, self.harness, "folders", {"act": ["reset"], "dir": [self.r1]},
                                   project=self.project)
        self.assertTrue(ok, msg)
        self.assertIsNone(self.saved())
        self.assertIn(self.project, msg)

    def test_bad_folders_refused_and_say_which(self):
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)
        afile = os.path.join(self.r1, "f.txt")
        open(afile, "w").close()
        many = []
        for i in range(fav.FOLDERS_MAX + 1):
            many.append(os.path.join(self.r1, f"d{i}"))
            os.makedirs(many[-1])
        cases = [({"dir": ["r1"]}, "not an absolute path: r1"),
                 ({"more": [os.path.join(self.root, "gone")]}, "no such folder: " + os.path.join(self.root, "gone")),
                 ({"dir": [afile]}, "not a folder: " + afile),
                 ({"dir": [self.r1], "more": [plain]}, "not in a git repository: " + plain),
                 ({"dir": ["/" + "x" * fav.FOLDER_LEN]}, f"at most {fav.FOLDER_LEN}"),
                 ({"dir": [self.r1 + "\x1b[31m"]}, "control character"),
                 ({"more": ["\n".join(many)]}, f"choose at most {fav.FOLDERS_MAX}"),
                 ({"dir": [], "more": ["  \n"]}, "no folder chosen")]
        for form, want in cases:
            for action in ("folders", "start"):
                with self.subTest(form=str(form)[:80], action=action):
                    ok, msg = fav.agent_action(self.state, self.harness, action, dict(form, pick=["1"]),
                                               project=self.project)
                    self.assertFalse(ok)
                    self.assertIn(want, msg)
        self.assertIsNone(self.saved())
        self.assertFalse(os.path.exists(self.log))
        # 20 distinct folders are fine, the same one twice counts once
        ok, msg = fav.agent_action(self.state, self.harness, "folders",
                                   {"more": ["\n".join(many[:fav.FOLDERS_MAX] + many[:2])]})
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.saved()), fav.FOLDERS_MAX)

    def test_start_with_the_form_refused_while_running(self):
        self.status()
        ok, msg = fav.agent_action(self.state, self.harness, "start", {"pick": ["1"], "dir": [self.r1]})
        self.assertFalse(ok)
        self.assertIn("already running", msg)
        self.assertIsNone(self.saved())

    def test_resume_uses_the_saved_folders(self):
        fav.save_folders(self.state, [self.r1, os.path.join(self.root, "gone-since"), self.r2])
        open(os.path.join(self.state, "wanted"), "w").close()
        self.status(pid=2 ** 22 + 12345, heartbeat_ts=time.time() - 3600, repos=[self.project])
        t = time.time()
        for name, age in (("agent.json", 300), ("wanted", 200), ("folders.json", 100)):
            os.utime(os.path.join(self.state, name), (t - age, t - age))
        ok, msg = fav.resume(self.state, self.harness)
        self.assertTrue(ok, msg)
        self.assertEqual(self.logged()["args"], f"findings start {self.r1} {self.r2}")   # the gone one left out

    def test_resume_keeps_a_later_start_on_other_folders(self):
        # saved on the card, then `harness findings start DIR` in a terminal: that run's repos go on
        fav.save_folders(self.state, [self.r1])
        open(os.path.join(self.state, "wanted"), "w").close()
        self.status(pid=2 ** 22 + 12345, heartbeat_ts=time.time() - 3600, repos=[self.r2])
        t = time.time()
        for name, age in (("folders.json", 300), ("wanted", 200), ("agent.json", 100)):
            os.utime(os.path.join(self.state, name), (t - age, t - age))
        ok, msg = fav.resume(self.state, self.harness)
        self.assertTrue(ok, msg)
        self.assertEqual(self.logged()["args"], f"findings start {self.r2}")

    def test_resume_with_nothing_saved_is_unchanged(self):
        open(os.path.join(self.state, "wanted"), "w").close()
        self.status(pid=2 ** 22 + 12345, heartbeat_ts=time.time() - 3600, repos=[self.r2])
        ok, msg = fav.resume(self.state, self.harness)
        self.assertTrue(ok, msg)
        self.assertEqual(self.logged()["args"], "findings start")

    def test_card_escapes_the_folders(self):
        odd = os.path.join(self.root, "a<b>'q\"&")
        os.makedirs(odd)
        fav.save_folders(self.state, [odd, "/gone/<script>x</script>"])
        self.status(repos=["/r/<i>'"])
        h = fav.agent_card(self.state, project="/p/<u>", known=["/k/<s>", "relative<x>"])
        for raw in ("<b>", "<script>", "<i>", "<u>", "<s>", "<x>", "q\"&"):
            self.assertNotIn(raw, h)
        self.assertIn(esc_attr(odd), h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("missing", h)   # a saved folder that is gone is still shown, ticked

    def test_card_running_shows_the_choice(self):
        fav.save_folders(self.state, [self.r1])
        self.status(repos=[self.r2])
        h = fav.agent_card(self.state, project=self.project)
        self.assertIn(f"It is exploring {self.r2}. A change applies at the next Start (Stop, then Start)", h)
        self.assertIn("Save for the next start", h)
        self.assertIn(f"value='{self.r1}' checked>", h)
        self.assertIn(f"Folders it explores: <span class='muted'>{self.r1}</span>", h)
        self.assertNotIn("formaction", h)
        self.assertIn("action='/runs/findings/agent/folders'", h)


def esc_attr(s):
    import html
    return html.escape(s)


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
