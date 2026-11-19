"""Edge cases of the ticket queue (scripts/ticket_queue.py) that tests/queue_test.py
does not reach: signals and kills of the runner, a machine restart's leftovers,
two runners at once, files a crash or a hand edit leaves unreadable, and project
folders of other shapes. tests/QUEUE_WORKFLOWS.md maps each case to its test.

Docker-free, as queue_test.py: its fake harness (the agent of each stage), fake
proxy (the verifier) and throwaway git projects with a local bare remote.

  python3 tests/queue_edge_test.py      (tests/unit_queue_test.sh runs it)
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import queue_test as qt  # noqa: E402  (its classes are not collected here: only the module is bound)
from queue_test import cat, put, sh, tq  # noqa: E402


class Edge(qt.Base):
    run_bg, wait_for = qt.OwnerControlTest.run_bg, qt.OwnerControlTest.wait_for
    sleeping, status_json = qt.OwnerControlTest.sleeping, qt.OwnerControlTest.status_json

    def out(self):
        return cat(os.path.join(self.tmp, "run.out"))

    def run_json(self):
        return tq.run_file(os.path.join(self.state, "run.json")) or {}

    def innocent(self):
        """A process that is not the queue's: what a pid a dead run left may name by now."""
        p = subprocess.Popen(["sleep", "60"], start_new_session=True)   # its pid leads a group, as an agent's

        def end():
            if p.poll() is None:
                p.kill()
            p.wait()
        self.addCleanup(end)
        return p


class SignalTest(Edge):
    """The runner ended by a signal other than Ctrl-C: a shutdown's or kill's SIGTERM,
    a closed terminal's SIGHUP."""

    def agent_pid(self):
        self.wait_for(lambda: self.run_json().get("agent"), "the agent's pid in run.json")
        return self.run_json()["agent"]

    def check_ended_by(self, sig):
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1")
        self.add("ABC-1")
        p = self.run_bg()
        self.wait_for(self.sleeping, "the ABC-1 agent")
        agent = self.agent_pid()
        p.send_signal(sig)
        self.assertEqual(p.wait(timeout=60), 128 + sig, self.out())
        # the agent is ended with the run, not left working the ticket with no one watching it
        with self.assertRaises(ProcessLookupError):
            os.killpg(agent, 0)
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("queued", "plan"), self.out())
        self.assertEqual(st["history"][-1]["result"], "interrupted")
        self.assertNotIn("agent", st)
        self.assertIn("stop-launch " + st["launch_id"], cat(self.calls).splitlines())   # its container too
        # run.json stays, its beat stale already, as a run the machine took down leaves it:
        # harness serve starts such a run again at its start (RunsView.resume_runner),
        # even one started again within the 30 s a beat is fresh for
        info = self.run_json()
        self.assertEqual(info.get("pid"), p.pid)
        self.assertFalse(tq.beating(info), info)
        self.assertFalse(tq.runner(tq.Queue(self.state))["running"])
        # the next run carries on from the same stage
        os.environ.pop("FAKE_SLEEP")
        rc, out = self.run_queue()
        self.assertEqual((rc, self.status()["status"]), (0, "done"), out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "run.json")))

    def test_sigterm_stops_the_agent_and_puts_the_ticket_back(self):
        self.check_ended_by(signal.SIGTERM)

    def test_sighup_stops_the_agent_and_puts_the_ticket_back(self):
        self.check_ended_by(signal.SIGHUP)

    def test_sigterm_to_a_parallel_run_reaches_its_slots(self):
        os.environ.update(FAKE_SLEEP="60")
        self.add("ABC-1")
        other = os.path.join(self.tmp, "other")
        sh("git", "clone", "-q", self.project, other)
        rc, out = self.q("add", "ABC-2", "--project", other)
        self.assertEqual(rc, 0, out)
        p = self.run_bg("--parallel", "2")
        self.wait_for(lambda: self.sleeping("ABC-1") and self.sleeping("ABC-2"), "both agents", 60)
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=90), 128 + signal.SIGTERM, self.out())
        for key in ("ABC-1", "ABC-2"):
            st = self.status(key)
            self.assertEqual((st["status"], st["stage"]), ("queued", "plan"), key + "\n" + self.out())
            self.assertNotIn("agent", st)
        self.assertTrue(os.path.exists(os.path.join(self.state, "run.json")))   # serve resumes it, parallel
        self.assertEqual(self.run_json().get("parallel"), 2)
        self.assertFalse(tq.beating(self.run_json()))

    def test_a_closed_terminal_still_puts_the_ticket_back(self):
        # the run's terminal is gone when its SIGHUP comes: a write to it fails (EIO)
        import pty
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1")
        self.add("ABC-1")
        master, slave = pty.openpty()
        p = subprocess.Popen([sys.executable, tq.__file__, "--state", self.state, "run", "--vault", self.vault,
                              "--proxy", self.proxy, "--harness", self.harness, "--default-model", "free-withtools",
                              "--planners", "1", "--reviewers", "general"], stdin=slave, stdout=slave, stderr=slave,
                             start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        os.close(slave)
        self.wait_for(self.sleeping, "the ABC-1 agent")
        os.close(master)   # the terminal window closed
        p.send_signal(signal.SIGHUP)
        self.assertEqual(p.wait(timeout=60), 128 + signal.SIGHUP)
        st = self.status()
        self.assertEqual((st["status"], st["stage"]), ("queued", "plan"))
        self.assertNotIn("agent", st)
        self.assertIn("ended by signal", cat(os.path.join(self.state, "ABC-1", "log.md")))

    def test_a_nohup_run_is_not_ended_by_sighup(self):
        # nohup harness queue run: SIGHUP ignored before the run starts stays ignored
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1")
        self.add("ABC-1")
        out = open(os.path.join(self.tmp, "run.out"), "w")
        self.addCleanup(out.close)
        p = subprocess.Popen([sys.executable, tq.__file__, "--state", self.state, "run", "--vault", self.vault,
                              "--proxy", self.proxy, "--harness", self.harness, "--default-model", "free-withtools",
                              "--planners", "1", "--reviewers", "general"], stdout=out, stderr=subprocess.STDOUT,
                             preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.wait_for(self.sleeping, "the ABC-1 agent")
        p.send_signal(signal.SIGHUP)
        time.sleep(2)
        self.assertIsNone(p.poll(), self.out())
        self.assertEqual(self.status()["status"], "running")
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=60), 128 + signal.SIGTERM, self.out())

    def test_sigterm_while_watching_an_empty_queue(self):
        p = self.run_bg("--watch-queue", "1")
        self.wait_for(lambda: self.run_json().get("step") == "waiting for work", "the run to watch")
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=30), 128 + signal.SIGTERM, self.out())
        self.assertFalse(tq.beating(self.run_json()))   # kept, stale: serve's start runs it again
        self.assertIs(self.run_json().get("watch"), True)

    def test_sigkill_leaves_what_the_next_run_ends(self):
        # SIGKILL has no handler: the ticket stays running with its agent on record; the
        # next run ends that agent (an orphan) before it takes the ticket again
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1", FAKE_SLEEP_ONCE="1")
        self.add("ABC-1")
        p = self.run_bg()
        self.wait_for(self.sleeping, "the ABC-1 agent")
        agent = self.agent_pid()
        p.kill()
        p.wait(timeout=10)
        os.killpg(agent, 0)   # still going
        self.assertEqual(self.status()["status"], "running")
        rc, out = self.run_queue()
        self.assertEqual((rc, self.status()["status"]), (0, "done"), out)
        with self.assertRaises(ProcessLookupError):
            os.killpg(agent, 0)
        self.assertIn("ended the agent a run left running", cat(os.path.join(self.state, "ABC-1", "log.md")))


class HaltForceTest(Edge):
    def test_halt_force_spares_a_pid_a_stale_run_json_names(self):
        # A run that has just taken run.lock (it has not beaten yet) and the run.json a
        # run the machine took down left, naming a pid another process has by now.
        q = tq.Queue(self.state)
        lock = open(os.path.join(self.state, "run.lock"), "a")
        self.addCleanup(lock.close)
        self.assertTrue(tq.try_lock(lock))
        other = self.innocent()
        put(os.path.join(self.state, "run.json"), json.dumps({"pid": other.pid, "beat": int(time.time()) - 3600}))
        put(os.path.join(self.state, "run-1.json"), json.dumps({"pid": other.pid, "beat": int(time.time()) - 3600}))
        t0 = time.time()
        rc, out = self.q("halt", "--force")
        self.assertEqual(rc, 1, out)
        self.assertIsNone(other.poll(), "halt --force killed a process that is not the run")
        self.assertIn("has not beaten", out)
        self.assertLess(time.time() - t0, 15)
        self.assertTrue(tq.run_going(q))


    @unittest.skipUnless(os.path.isdir("/proc/self"), "the run's command line is read from /proc")
    def test_halt_force_ends_a_frozen_run_that_no_longer_beats(self):
        # a run frozen whole (its beat thread too): run.json stale, but its pid is the queue's
        os.makedirs(self.state, exist_ok=True)
        code = ("import fcntl, sys, time; f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); "
                "print('locked', flush=True); time.sleep(60)")
        p = subprocess.Popen([sys.executable, "-c", code, os.path.join(self.state, "run.lock"), "ticket_queue.py"],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait(), p.stdout.close()))
        self.assertEqual(p.stdout.readline().strip(), "locked")
        put(os.path.join(self.state, "run.json"), json.dumps({"pid": p.pid, "beat": int(time.time()) - 3600}))
        t0 = time.time()
        rc, out = self.q("halt", "--force")
        self.assertEqual(rc, 0, out)
        self.assertEqual(p.wait(timeout=10), -signal.SIGKILL)
        self.assertLess(time.time() - t0, 8)   # no wait for a beat that will not come
        self.assertFalse(os.path.exists(os.path.join(self.state, "run.json")))


class ConcurrencyTest(Edge):
    def test_a_second_run_is_refused_while_one_goes(self):
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1")
        self.add("ABC-1")
        p = self.run_bg()
        self.wait_for(self.sleeping, "the ABC-1 agent")
        rc, out = self.run_queue()
        self.assertEqual(rc, 1, out)
        self.assertIn("another 'harness queue run' is running", out)
        self.assertEqual(self.status()["status"], "running")   # the second run touched nothing
        self.assertEqual(self.run_json().get("pid"), p.pid)
        rc, out = self.q("halt")
        self.assertEqual(p.wait(timeout=60), 0, self.out())


class UnreadableStateTest(Edge):
    def test_a_state_file_that_is_not_a_ticket_is_skipped_not_a_crash(self):
        self.add("ABC-1", "ABC-2")
        for text in ("[1]\n", '"x"\n', '{"key": "ABC-1", "sta'):   # a hand edit, a cut write
            put(os.path.join(self.state, "ABC-1", "state.json"), text)
            self.assertIsNone(tq.Queue(self.state).load("ABC-1"), text)
            for cmd in (("list",), ("status", "--json"), ("stats",)):
                rc, out = self.q(*cmd)
                self.assertEqual(rc, 0, f"{cmd} {text}: {out}")
                self.assertNotIn("Traceback", out)
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status("ABC-2")["status"], "done", out)   # the other tickets are worked
        rc, out = self.q("remove", "ABC-1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("ABC-1", tq.Queue(self.state).order())


class RestartTest(Edge):
    """The machine went down mid-run: no process of the run is left, but its files are,
    and their pids may be any other process's by now."""

    def test_the_run_after_a_restart_gets_past_what_the_dead_run_left(self):
        os.environ.update(FAKE_SLEEP="60", FAKE_SLEEP_KEY="ABC-1", FAKE_SLEEP_ONCE="1")
        self.add("ABC-1", "ABC-2")
        p = self.run_bg()
        self.wait_for(self.sleeping, "the ABC-1 agent")
        self.wait_for(lambda: self.run_json().get("agent"), "the agent's pid")
        agent = self.run_json()["agent"]
        os.killpg(agent, signal.SIGKILL)   # all of it at once, as a power cut
        p.kill()
        p.wait(timeout=10)
        # what the restarted machine has: every pid the files name is another process's
        other = self.innocent()
        old = int(time.time()) - 3600
        st = json.loads(cat(os.path.join(self.state, "ABC-1", "state.json")))
        self.assertEqual(st["status"], "running")
        st["agent"]["pid"] = other.pid
        put(os.path.join(self.state, "ABC-1", "state.json"), json.dumps(st))
        for name in ("run.json", "run-2.json"):
            put(os.path.join(self.state, name), json.dumps({"pid": other.pid, "beat": old, "ticket": "ABC-1",
                                                             "slot": 2, "agent": other.pid}))
        for left in ("ABC-1/state.json.999-1.tmp", "queue.list.999-1.tmp"):   # a write cut by the power
            put(os.path.join(self.state, left), "{")
        self.assertFalse(tq.runner(tq.Queue(self.state))["running"])   # no run is said to be going
        rc, out = self.q("list")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual(rc, 0, out)
        for key in ("ABC-1", "ABC-2"):
            self.assertEqual(self.status(key)["status"], "done", key + "\n" + out)
        self.assertIsNone(other.poll(), "the run killed a process that is not its agent")
        for name in ("run.json", "run-2.json"):
            self.assertFalse(os.path.exists(os.path.join(self.state, name)), name)
        self.assertEqual(tq.Queue(self.state).order(), ["ABC-1", "ABC-2"])


class ProjectShapeTest(Edge):
    def make(self, path, branch):
        os.makedirs(path)
        sh("git", "init", "-q", "-b", branch, path)
        put(os.path.join(path, "README"), "x\n")
        sh("git", "-C", path, "add", "README")
        sh("git", "-C", path, "commit", "-q", "-m", "init")
        return path

    def test_a_trunk_branch_and_a_path_with_spaces_and_unicode(self):
        project = self.make(os.path.join(self.tmp, "my proj \u00fc\u4e2d"), "trunk")
        remote = os.path.join(self.tmp, "r \u00fc.git")
        sh("git", "init", "-q", "--bare", remote)
        sh("git", "-C", project, "remote", "add", "origin", remote)
        rc, out = self.q("add", "ABC-1", "--project", project)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["base"], "trunk")
        rc, out = self.run_queue()
        st = self.status()
        self.assertEqual((rc, st["status"], st["push"]["result"]), (0, "done", "pushed"), out)
        self.assertIn("ABC-1", sh("git", "-C", remote, "branch"))

    def test_a_symlinked_project_folder_is_the_folder_it_names(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.project, link)
        rc, out = self.q("add", "ABC-1", "--project", link)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["project"], os.path.realpath(self.project))
        rc, out = self.q("add", "ABC-1", "--project", self.project)   # the same ticket, the same folder
        self.assertNotEqual(rc, 0, out)
        rc, out = self.run_queue()
        self.assertEqual((rc, self.status()["status"]), (0, "done"), out)

    def test_a_second_remote_is_pushed_to_when_named(self):
        up = os.path.join(self.tmp, "upstream.git")
        sh("git", "init", "-q", "--bare", up)
        self.git_p("remote", "add", "upstream", up)
        self.add("ABC-1")
        rc, out = self.run_queue("--remote", "upstream")
        st = self.status()
        self.assertEqual((rc, st["status"], st["push"]["result"]), (0, "done", "pushed"), out)
        self.assertIn("ABC-1", sh("git", "-C", up, "branch"))
        self.assertEqual(sh("git", "-C", self.remote, "branch"), "")   # not origin

    def test_a_base_branch_gone_since_the_add_blocks_with_why(self):
        self.add("ABC-1")
        self.git_p("checkout", "-q", "-b", "other")
        self.git_p("branch", "-D", "main")
        sh("git", "-C", self.remote, "branch", "-D", "main", cwd=None) if "main" in sh(
            "git", "-C", self.remote, "branch") else None
        rc, out = self.run_queue()
        st = self.status()
        self.assertNotIn("Traceback", out)
        self.assertEqual(st["status"], "blocked", out)
        self.assertIn("main", st["reason"])


class StateDirTest(Edge):
    def test_a_full_disk_is_said_not_a_traceback(self):
        import errno
        from unittest import mock
        self.add("ABC-1")
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(tq, "write_atomic", side_effect=full):
            for cmd in (("add", "ABC-2", "--project", self.project), ("note", "ABC-1", "--text", "hi"),
                        ("move", "ABC-1", "bottom")):
                rc, out = self.q(*cmd)
                self.assertEqual(rc, 1, f"{cmd}: {out}")
                self.assertIn("the disk may be full", out)
        self.assertEqual(tq.Queue(self.state).order(), ["ABC-1"])   # as the last whole write had it
        self.assertEqual(self.status()["status"], "queued")

    def test_a_disk_that_fills_mid_run_leaves_the_ticket_for_the_next_run(self):
        import errno
        from unittest import mock
        self.add("ABC-1")
        real, n = tq.write_atomic, [0]

        def filling(path, *a, **k):   # the take's write goes through; the disk is full after it
            if path.endswith("state.json"):
                n[0] += 1
                if n[0] > 1:
                    raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, *a, **k)
        with mock.patch.object(tq, "write_atomic", side_effect=filling):
            rc, out = self.run_queue()
        # one ticket's failure does not stop the queue (run_tickets): the run goes on, the
        # error in the ticket's log; the ticket keeps its last whole write, no run holding it
        self.assertEqual(rc, 0, out)
        self.assertIn("No space left on device", cat(os.path.join(self.state, "ABC-1", "log.md")))
        self.assertEqual(self.status()["status"], "running")
        self.assertFalse(tq.run_holds(tq.Queue(self.state), "ABC-1"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "run.json")))
        rc, out = self.run_queue()   # space freed
        self.assertEqual((rc, self.status()["status"]), (0, "done"), out)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "root writes anywhere")
    def test_an_unwritable_state_folder_is_a_clear_error(self):
        self.add("ABC-1")
        os.chmod(self.state, 0o555)
        self.addCleanup(os.chmod, self.state, 0o755)
        rc, out = self.q("add", "ABC-2", "--project", self.project)
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)
        rc, out = self.q("list")
        self.assertEqual(rc, 0, out)
        self.assertIn("ABC-1", out)


if __name__ == "__main__":
    unittest.main()
