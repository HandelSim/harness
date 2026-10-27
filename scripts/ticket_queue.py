#!/usr/bin/env python3
"""The ticket queue behind `harness queue`: works through tickets one at a time,
plan -> implement -> review (-> one fix) -> push, with no one at the keyboard.

  ticket_queue.py --state DIR add KEY... [--project DIR] [--base REF] [--stages LIST]
                  [--start REF|here] [--branch NAME] [--clone] [--no-plan-approval] [--note TEXT]
  ticket_queue.py --state DIR list | status [--json] | remove KEY... | approve KEY
  ticket_queue.py --state DIR retry KEY [--stage S] | stop KEY | move KEY up|down|top|bottom
  ticket_queue.py --state DIR halt [--force --harness PATH]
  ticket_queue.py --state DIR note KEY [--text T | --stdin]
  ticket_queue.py --state DIR answer KEY --vault V [--text T] [--go]
  ticket_queue.py --state DIR findings --vault V
  ticket_queue.py --state DIR run --vault V --proxy URL --harness PATH
                  [--model M] [--default-model M] [--verify-model M] [--host]
                  [--single-message] [--retries N] [--push 0|1] [--remote R]
                  [--plan-review 0|1]
Each stage is one fresh, headless agent launch through the harness CLI
(`harness -p --yolo --ticket KEY -m MODEL PROMPT`: the firewalled container,
or host mode) in the project folder itself (or, --clone, the ticket's own clone
of the project, state/queue/KEY/repo), on the ticket's branch. A plan waits for
the owner's approval (plan-review) unless the ticket was added with
--no-plan-approval. The prompt (scripts/queue_prompts/<stage>.md) carries
everything the stage needs, the ticket, the plan, the answers and the last
attempt's problems, because the free models only read the last message.

After a stage, harness checks the result itself before an AI verifier sees it:
the plan has a goal and acceptance criteria; the branch has commits, a non-empty
diff, no .github/ change and no secret. The verifier (one plain request through
the proxy, no tools) must quote the ticket for a plan's criteria and the diff
for each one a change meets, and harness checks
every quote is really there. A failed stage runs again once (QUEUE_RETRIES) with
the problems listed, then the ticket is blocked. When the stage failed because
the upstream does not answer (one probe request), the whole run stops and the
ticket stays where it was.

A ticket can run a subset of the stages (--stages; fix still only after a
review with Must fix items). Its work starts at --start (a branch, tag, commit,
or <remote>/<branch>, fetched first; "here": the project's HEAD when the
ticket is added, and in place its uncommitted files too; default: here in
place, the base in a clone) and ends on --branch (default KEY). An
existing branch is only ever moved forward: one with commits the start lacks is
refused. The change reviewed is from the merge-base of the base and the start,
so existing work at the start is part of it.

The owner's side runs in other processes while a run holds a ticket in memory,
so it never writes what the run owns: notes go to KEY/notes.json (mirrored into
state.json by every save), a stop or halt is a file (KEY/stop, DIR/halt) the run
looks for every POLL seconds, and the run keeps DIR/run.json beating.

harness never runs git inside the clone once the agent has had it: it fetches
branch KEY into the project (refs/harness-queue/KEY), diffs there and pushes
from there, without force. Nothing is ever written to the tracker.
"""
import argparse
import contextlib
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vault_ingest import InfraError, call_model, resolve_model, try_lock, yq  # noqa: E402

PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue_prompts")
STAGE_TIMEOUT = 3 * 3600   # one agent launch; a cooperative turn alone can take 15 minutes
VERIFY_TIMEOUT = 600
TICKET_CAP = 30_000        # chars of each part embedded in an agent prompt, which
PLAN_CAP = 15_000          # goes on a command line (one argument is at most 128 KiB)
NOTE_CAP = 8_000           # feedback and must-fix lists in a prompt
PROMPT_MAX_BYTES = 120_000
# Windows: CreateProcess takes at most 32,767 characters for the whole command
# line; a prompt longer than this goes to the agent as a pointer to KEY-task.md.
WINDOWS = os.name == "nt"
WIN_PROMPT_MAX = 24_000
# An upstream error (not the request's own fault) is tried again after these pauses.
INFRA_RETRY_PAUSES = (20, 60)
DIFF_CAP_AGENT = 40_000
DIFF_CAP_VERIFY = 150_000
MAX_QUESTIONS = 3
PLAN_SECTIONS = ("Goal", "Acceptance criteria", "Approach", "Files", "Questions", "Assumptions")
PLAN_REQUIRED = ("Goal", "Acceptance criteria")   # the rest missing is noted, not failed
HISTORY_CAP = 30
REVIEW_SECTIONS = ("Must fix", "Notes")
SECRET_ENV = ("PROXY_API_KEY", "OAI_API_KEY", "CHATGPT_COOKIE_STRING", "TRACKER_TOKEN")
SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}|\bsk-[A-Za-z0-9_-]{32,}")
SECRET_PATH = re.compile(r"(^|/)(\.env(?!\.(example|sample|template)$)(\..*)?|.*\.pem|.*\.key|id_(rsa|ed25519|ecdsa|dsa)(\.pub)?|\.netrc)$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
ALL_STAGES = ("plan", "implement", "review", "fix", "push")
POLL = 2                   # seconds between looks at the agent, the stop and halt files
BEAT = 3                   # seconds between run.json beats
OWNER_NOTE_CAP = 4_000     # chars of one owner note
OWNER_NOTES_MAX = 50
NO_PLAN = "(no plan: this ticket's stages leave the plan out; work from the ticket and the owner's notes)"
NO_PLAN_CRITERION = "The change does what the ticket asks for"


class Infra(Exception):
    """The upstream (or the proxy) does not answer: stop the run, keep the ticket."""


class Interrupted(Exception):
    """The owner stopped the ticket ("stop", KEY/stop) or the run ("halt", DIR/halt)."""
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


# --- files and state ----------------------------------------------------------

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path, cap=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    if cap and len(text) > cap:
        text = text[:cap] + f"\n[... cut at {cap} characters ...]\n"
    return text


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for i in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows refuses to replace a file another process has open (the
            # run viewer reading state.json); it is closed again in a moment.
            if os.name != "nt" or i == 19:
                raise
            time.sleep(0.1)


class Queue:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def order(self):
        return [k for k in read(os.path.join(self.root, "queue.list")).split() if KEY_RE.match(k)]

    def set_order(self, keys):
        write_atomic(os.path.join(self.root, "queue.list"), "".join(k + "\n" for k in keys))

    def dir(self, key):
        return os.path.join(self.root, key)

    def load(self, key):
        try:
            st = json.loads(read(os.path.join(self.dir(key), "state.json")))
        except ValueError:
            return None
        if isinstance(st, dict) and os.path.exists(self.notes_path(key)):
            st["notes"] = self.notes(key)
        return st

    def save(self, st):
        """Write state.json with the owner's notes as they are in notes.json, which
        only 'note' writes: a run saving the state it holds cannot lose one."""
        st["updated"] = now_iso()
        if os.path.exists(self.notes_path(st["key"])):
            st["notes"] = self.notes(st["key"])
        write_atomic(os.path.join(self.dir(st["key"]), "state.json"), json.dumps(st, indent=2) + "\n")

    def notes_path(self, key):
        return os.path.join(self.dir(key), "notes.json")

    def notes(self, key):
        try:
            got = json.loads(read(self.notes_path(key)) or "[]")
        except ValueError:
            return []
        return [n for n in got if isinstance(n, dict)] if isinstance(got, list) else []

    def add_note(self, key, text):
        text = text.strip()
        if len(text) > OWNER_NOTE_CAP:
            text = text[:OWNER_NOTE_CAP] + f"\n[... cut at {OWNER_NOTE_CAP} characters ...]"
        with held(os.path.join(self.dir(key), "notes.lock")):
            notes = (self.notes(key) + [{"at": now_iso(), "text": text}])[-OWNER_NOTES_MAX:]
            write_atomic(self.notes_path(key), json.dumps(notes, indent=2) + "\n")

    def sentinel(self, key=None):
        """KEY/stop (the owner stopped the ticket) or, without a key, DIR/halt (the run)."""
        return os.path.join(self.dir(key), "stop") if key else os.path.join(self.root, "halt")

    def log(self, key, line, detail=""):
        """One line in the ticket's log and in run.log (what `harness queue watch` tails)."""
        stamp = f"{now_iso()} {key} {line}\n"
        for path in (os.path.join(self.dir(key), "log.md"), os.path.join(self.root, "run.log")):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(stamp)
                if detail and path.endswith("log.md"):
                    f.write(detail.rstrip() + "\n")
        print(stamp, end="", file=sys.stderr, flush=True)


@contextlib.contextmanager
def held(path, wait=10):
    """An exclusive lock on path for an owner command's short read-modify-write
    (queue.list, notes.json), against another owner command doing the same."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        end = time.time() + wait
        while not try_lock(f):
            if time.time() > end:
                raise RuntimeError(f"{path} is held by another process")
            time.sleep(0.05)
        try:
            yield
        finally:
            unlock(f)


def unlock(f):
    if os.name == "nt":   # flock goes with the close; msvcrt wants the byte unlocked first
        import msvcrt
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def run_going(q):
    """True when a 'run' holds run.lock. Takes the lock for a moment, so call it only
    from a command that needs to know (stop, halt), not from a poll."""
    with open(os.path.join(q.root, "run.lock"), "a") as f:
        if try_lock(f):
            unlock(f)
            return False
        return True


def run_holds(q, key):
    """True when a going run has the ticket in hand: run.json names it (the run
    names a ticket under queue.lock as it takes it) and beats. A ticket left running by
    a run that ended, or by one stuck elsewhere or frozen, is not in hand: the owner's commands
    change it themselves rather than leave it a request no run will read."""
    r = runner(q)
    return r["running"] and r.get("ticket") == key and run_going(q)


def stages_of(st):
    """The ticket's stages in their order; an older state file has none: all five."""
    want = st.get("stages") or ALL_STAGES
    return [s for s in ALL_STAGES if s in want]


def first_stage(st):
    return next(s for s in stages_of(st) + ["plan"] if s != "fix")


def next_stage(st, cur):
    """The listed stage after cur, fix left out (only a review with Must fix items
    asks for it); None when the list is done."""
    later = [s for s in ALL_STAGES[ALL_STAGES.index(cur) + 1:] if s in stages_of(st) and s != "fix"]
    return later[0] if later else None


def parse_stages(text):
    """--stages: (list in the canonical order, None) or (None, why not)."""
    got = [s.strip().lower() for s in (text or "").split(",") if s.strip()]
    bad = [s for s in got if s not in ALL_STAGES]
    if not got or bad:
        return None, f"--stages takes a comma list of {','.join(ALL_STAGES)}" + (f"; not a stage: {', '.join(bad)}" if bad else "")
    if "fix" in got and "review" not in got:
        return None, "--stages: fix runs only after a review asked for fixes; list review too"
    return [s for s in ALL_STAGES if s in got], None


def note_paths(vault, key):
    d = os.path.join(vault, "wiki", "notes", "tickets")
    return {"plan": os.path.join(d, f"{key}-plan.md"), "review": os.path.join(d, f"{key}-review.md"),
            "findings": os.path.join(d, f"{key}-findings.md"), "answers": os.path.join(d, f"{key}-answers.md"),
            "state": os.path.join(d, f"{key}-state.md"), "log": os.path.join(d, f"{key}-log.md"),
            "task": os.path.join(d, f"{key}-task.md"),
            "ticket": os.path.join(vault, ".raw", "tickets", f"{key}.md")}


# --- plan and review files ------------------------------------------------------

def sections(text):
    """{heading: body} for the '## ' headings of a markdown file."""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1).strip().rstrip(":").lower()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def bullets(body):
    items = []
    for line in body.splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)", line)
        if m:
            items.append(m.group(1))
        elif items and line.strip() and line.startswith((" ", "\t")):
            items[-1] += " " + line.strip()
    return [i for i in items if not re.fullmatch(r"(?i)\(?none\.?\)?|n/?a", i.strip())]


def check_plan(text):
    """(problems, criteria, questions) of a plan file. Only what later stages
    cannot do without fails the attempt: a goal and the acceptance criteria.
    Format slips (a missing section, an unmarked question) are plan_notes."""
    if not text.strip():
        return ["the plan file was not written"], [], []
    secs = sections(text)
    problems = [f"the plan has no '## {s}' section" for s in PLAN_REQUIRED if s.lower() not in secs]
    criteria = bullets(secs.get("acceptance criteria", ""))
    questions = plan_questions(secs)
    if "acceptance criteria" in secs and not criteria:
        problems.append("'## Acceptance criteria' lists no criteria (one bullet each)")
    if len(criteria) > 15:
        problems.append(f"{len(criteria)} acceptance criteria: at most 15, merge the small ones")
    return problems, criteria, questions


def plan_questions(secs):
    """The Questions section's bullets, less the ways of saying there are none."""
    return [q for q in bullets(secs.get("questions", ""))
            if not re.match(r"(?i)\W*(none|no (open |further )?questions|n/?a)\b[^?]*$", q)]


def question_mark(q):
    """'blocking', 'non-blocking' or None: the marker, bold or in backticks too."""
    m = re.match(r"(?i)[\s*_`]*\[(non-)?blocking\]", q)
    return None if not m else "non-blocking" if m.group(1) else "blocking"


def plan_notes(text):
    """What is off in a plan that passed check_plan, for the log."""
    secs = sections(text)
    notes = [f"no '## {s}' section" for s in PLAN_SECTIONS if s.lower() not in secs]
    questions = plan_questions(secs)
    if len(questions) > MAX_QUESTIONS:
        notes.append(f"{len(questions)} questions (asked for at most {MAX_QUESTIONS})")
    notes += [f"question not marked [blocking] or [non-blocking] (taken as "
              f"{'blocking' if blocking([q]) else 'non-blocking'}): {q[:120]}"
              for q in questions if not question_mark(q)]
    return notes


def blocking(questions):
    """The questions that hold the ticket: marked [blocking], or an unmarked
    question with no default to go on."""
    return [q for q in questions if question_mark(q) == "blocking"
            or (question_mark(q) is None and "?" in q and not re.search(r"(?i)\bdefault:", q))]


def check_review(text):
    if not text.strip():
        return ["the review file was not written"], []
    secs = sections(text)
    problems = [f"the review has no '## {s}' section" for s in REVIEW_SECTIONS if s.lower() not in secs]
    return problems, bullets(secs.get("must fix", ""))


# --- git (the project only; never inside the agent's clone after it started) -----

# git's output is UTF-8 (paths, messages); Windows' default cp1252 decoding raises on "Ł" or "Ё".
TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def git(cwd, *args, check=True, timeout=300, env=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **(env or {}))
    # no fsmonitor daemon started by the queue's git: it would outlive the call holding its pipes
    r = subprocess.run(["git", "-C", cwd, "-c", "core.fsmonitor=false", *args], capture_output=True, env=env,
                       timeout=timeout, **TEXT)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def rmtree(path):
    """Remove a tree; None, or why it is still there. On Windows git's object
    files are read-only, which a plain rmtree cannot delete (WinError 5), and a
    file another program has open (a virus scan of a fresh clone) frees up
    within moments, so clear the bit and try again for a few seconds."""
    def writable(func, p, exc):
        if func not in (os.unlink, os.remove, os.rmdir):
            raise exc if isinstance(exc, BaseException) else exc[1]
        for x in (os.path.dirname(p), p):
            os.chmod(x, 0o700)
        func(p)
    err = None
    for i in range(10):
        if not os.path.lexists(path):
            return None
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=writable)
            else:
                shutil.rmtree(path, onerror=writable)
        except OSError as e:
            err = e
            if os.name != "nt":
                break
            time.sleep(0.5 * (i + 1))
    return None if not os.path.lexists(path) else f"could not remove {path}: {err}"


def clone_opts(container):
    """git clone -c options; they stay in the clone's config."""
    if os.name != "nt":
        return []
    # Git for Windows can enable a file system monitor daemon per repo; it keeps
    # the clone's directory open, so a later retry could not remove it. Deep
    # node_modules-style paths pass MAX_PATH. In a container the Linux git reads
    # the clone: files checked out with CRLF would all look modified there.
    opts = ["-c", "core.fsmonitor=false", "-c", "core.longpaths=true"]
    return opts + (["-c", "core.autocrlf=false"] if container else [])


def make_clone(q, st, container=False):
    """Clone the project into DIR/repo, on the ticket's branch at its start.
    Runs before any agent touches the clone; the clone has no remote, so the
    agent cannot push anywhere. Cloned in place, not renamed into place: Windows
    refuses to rename a directory while anything has a file in it open. A clone
    is complete once base_sha is saved; process() makes it again until then."""
    dest = os.path.join(q.dir(st["key"]), "repo")
    st.pop("base_sha", None)
    q.save(st)
    for d in (dest + ".new", dest):  # .new: left by versions that renamed
        err = rmtree(d)
        if err:
            raise RuntimeError(f"{err}. Close whatever has it open (an editor, a terminal "
                               f"in it, git's fsmonitor daemon) and run harness queue retry {st['key']}")
    start, base_sha = start_and_base(st)
    ends_on(q, st, start)
    r = subprocess.run(["git", "clone", "-q", "--no-checkout", *clone_opts(container), st["project"], dest],
                       capture_output=True, timeout=600, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), **TEXT)
    if r.returncode != 0:
        raise RuntimeError(f"git clone: {(r.stderr or r.stdout).strip()[:400]}")
    # A clone has the project's branches only; the start (a remote-tracking ref, a
    # bare sha) comes over by a name of its own, there for the fetch alone.
    tmp = f"refs/harness-queue-ref/{st['key']}"
    git(st["project"], "update-ref", tmp, start)
    try:
        git(dest, "fetch", "-q", "--no-tags", st["project"], f"+{tmp}:{tmp}")
    finally:
        git(st["project"], "update-ref", "-d", tmp, check=False)
    git(dest, "checkout", "-q", "--detach", start)
    git(dest, "update-ref", "-d", tmp, check=False)
    git(dest, "update-ref", f"refs/heads/{branch_of(st)}", start)   # the clone's own copy of the branch
    git(dest, "checkout", "-q", branch_of(st))
    git(dest, "remote", "remove", "origin")
    for k, fallback in (("user.name", "harness queue"), ("user.email", "harness-queue@localhost")):
        v = git(st["project"], "config", k, check=False).stdout.strip()
        git(dest, "config", k, v or fallback)
    st.update(start_sha=start, base_sha=base_sha)
    q.save(st)


def checked_out(project, branch):
    """True when branch is checked out in one of the project's worktrees."""
    return f"branch refs/heads/{branch}" in git(project, "worktree", "list", "--porcelain",
                                                 check=False).stdout.splitlines()


HERE = "here"   # --start here: where the project is (in place: its tree as it is, uncommitted changes too)


def branch_of(st):
    """The branch the work ends on: --branch, else the ticket key."""
    return st.get("branch") or st["key"]


def start_of(st):
    """What the work starts from: --start (--ref in older state files), else the base."""
    return st.get("start") or st.get("ref") or st["base"]


def workdir(q, st):
    """Where the agents work: the project itself (--in-place) or the ticket's clone."""
    return st["project"] if st.get("in_place") else os.path.join(q.dir(st["key"]), "repo")


def commit_of(project, ref):
    """The commit ref names now (a <remote>/<branch> is fetched first); HERE is HEAD."""
    return resolve_ref(project, "HEAD" if ref == HERE else ref)


def start_and_base(st):
    """(start, base_sha): the commit the work starts from, and where the change
    being made begins (the merge-base of the base and the start: with existing
    work at the start, that work is part of the change reviewed)."""
    project = st["project"]
    start = commit_of(project, start_of(st))
    if not start:
        raise RuntimeError(f"{start_of(st)} is not a commit in {project}")
    base = commit_of(project, st["base"]) if st["base"] != start_of(st) else start
    if not base:
        raise RuntimeError(f"{st['base']} is not a commit in {project}")
    r = git(project, "merge-base", base, start, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{start_of(st)} and {st['base']} have no history in common")
    return start, r.stdout.strip()


def tips(project, branch):
    """[(name, sha)]: the branch here and on each remote (fetched first), where it exists."""
    out = []
    local = git(project, "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", check=False).stdout.strip()
    if local:
        out.append((branch, local))
    for remote in git(project, "remote", check=False).stdout.split():
        sha = resolve_ref(project, f"{remote}/{branch}")
        if sha:
            out.append((f"{remote}/{branch}", sha))
    return out


def lost_by(project, branch, sha):
    """The tips of branch that sha does not contain: ending the work on branch at
    sha would throw their commits away. [] for a new branch, or sha on top of it."""
    return [(name, tip) for name, tip in tips(project, branch)
            if git(project, "merge-base", "--is-ancestor", tip, sha, check=False).returncode != 0]


def ends_on(q, st, start):
    """Raise unless the ticket's branch can end up at start without losing commits:
    an existing branch must be behind the start, so the work goes on from its tip.
    The project's own branch, left by an earlier go of this ticket, is kept at
    refs/harness-queue/prev/KEY and started again; a pushed one is never moved."""
    project, branch = st["project"], branch_of(st)
    lost = lost_by(project, branch, start)
    if lost and lost == [(branch, lost[0][1])] and st.get("made_branch"):
        prev = f"refs/harness-queue/prev/{st['key']}"
        git(project, "update-ref", prev, lost[0][1])
        q.log(st["key"], f"branch {branch} from the earlier go ({lost[0][1][:12]}) kept at {prev}")
        if not st.get("in_place"):   # in place, enter_in_place moves it with the checkout
            if checked_out(project, branch):
                raise RuntimeError(f"branch {branch} from the earlier go is checked out; switch away from it")
            git(project, "update-ref", f"refs/heads/{branch}", start, lost[0][1])
        return
    if lost:
        names = ", ".join(f"{n} at {t[:12]}" for n, t in lost)
        earlier = " (an earlier go of this ticket?)" if branch == st["key"] else ""
        raise RuntimeError(f"branch {branch} has commits that {start_of(st)} does not ({names}){earlier}; "
                           f"ending the work there would throw them away. Start from the branch "
                           f"(--start {lost[0][0]}), end on another branch (--branch), or delete it")


def resolve_ref(project, ref):
    """The commit ref names in project, or "". A remote-tracking ref (origin/X) is
    fetched from its remote first, best effort, so it is the work as it is now."""
    head, _, rest = ref.partition("/")
    if rest and head in git(project, "remote", check=False).stdout.split():
        try:
            git(project, "fetch", "-q", "--no-tags", head, f"+refs/heads/{rest}:refs/remotes/{head}/{rest}",
                check=False, timeout=120)
        except subprocess.SubprocessError:
            pass
    r = git(project, "rev-parse", "-q", "--verify", ref + "^{commit}", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def uncommitted(project):
    """{"untracked": [...], "changed": [...]}: what git status shows in project.
    A nested repository is one untracked "dir/" entry."""
    out = {"untracked": [], "changed": []}
    raw = git(project, "status", "--porcelain=v1", "-z", "-uall", "--ignore-submodules=none").stdout.split("\0")
    i = 0
    while i < len(raw):
        e = raw[i]
        i += 1
        if len(e) < 4:
            continue
        out["untracked" if e[:2] == "??" else "changed"].append(e[3:])
        if e[0] in "RC":   # a rename's old path follows
            i += 1
    return out


def owner_files(q, st):
    try:
        with open(os.path.join(q.dir(st["key"]), "owner.json"), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


EXCLUDE_MARK = "# harness queue: the owner's uncommitted files (removed when the stage ends)"


def exclude_block(project, paths):
    """Keep the owner's untracked files out of git add -A in place (.git/info/exclude,
    between marks); paths=None takes the block out again."""
    path = git(project, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    path = path if os.path.isabs(path) else os.path.join(project, path)
    text = read(path)
    end = EXCLUDE_MARK.replace("(removed", "(end; removed")
    if EXCLUDE_MARK in text:
        a = text.index(EXCLUDE_MARK)
        b = text.find(end, a)
        text = text[:a].rstrip("\n") + ("\n" + text[b + len(end):].lstrip("\n") if b != -1 else "\n")
        text = text if text.strip() else ""
    if paths:
        lines = ["/" + p.replace("[", "\\[").replace("*", "\\*").replace("?", "\\?") for p in paths]
        text = (text.rstrip("\n") + "\n" if text.strip() else "") + "\n".join([EXCLUDE_MARK] + lines + [end]) + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_atomic(path, text)


def enter_in_place(q, st):
    """Put the project itself on the ticket's branch at its start, keeping the
    owner's uncommitted changes (git switch carries them, and refuses rather than
    overwrite one). What is uncommitted now is the owner's: saved to DIR/owner.json
    for the gates. Never resets, cleans or stashes."""
    project, branch, key = st["project"], branch_of(st), st["key"]
    st.pop("base_sha", None)
    q.save(st)
    trouble = in_place_trouble(project, None)
    if trouble:
        raise RuntimeError(trouble)
    start, base_sha = start_and_base(st)
    if start_of(st) == HERE:   # frozen at the first go, as a clone's is at add: going again starts there too
        st["start"] = start
    ends_on(q, st, start)
    for wt in git(project, "worktree", "list", "--porcelain", check=False).stdout.split("\n\n")[1:]:
        if f"branch refs/heads/{branch}" in wt.splitlines():
            raise RuntimeError(f"branch {branch} is checked out in another worktree ({wt.splitlines()[0][9:]})")
    owner = uncommitted(project)
    write_atomic(os.path.join(q.dir(key), "owner.json"), json.dumps(owner))
    head = git(project, "rev-parse", "HEAD").stdout.strip()
    on = git(project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    if on != branch or head != start:
        if head != start:
            git(project, "switch", "-q", "--detach", start)
        old = git(project, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False).stdout.strip()
        if old != start:
            git(project, "update-ref", f"refs/heads/{branch}", start, *([old] if old else []))
            if not old:
                st["made_branch"] = True
        git(project, "switch", "-q", branch)
    for k, fallback in (("user.name", "harness queue"), ("user.email", "harness-queue@localhost")):
        if not git(project, "config", k, check=False).stdout.strip():
            raise RuntimeError(f"git {k} is not set in {project}; the agent's commits need it (git config {k} ...)")
    st.update(start_sha=start, base_sha=base_sha)
    q.save(st)
    n = len(owner["untracked"]) + len(owner["changed"])
    return f"{n} uncommitted file(s) of yours kept out of the commits" if n else ""


def in_place_trouble(project, branch):
    """Why the project is not fit for an in-place stage (a rebase or merge going,
    git's index.lock left, not on the ticket's branch), or ""."""
    for name, what in (("rebase-merge", "a rebase"), ("rebase-apply", "a rebase or git am"),
                       ("MERGE_HEAD", "a merge"), ("CHERRY_PICK_HEAD", "a cherry-pick"), ("index.lock", "index.lock")):
        p = git(project, "rev-parse", "--git-path", name, check=False).stdout.strip()
        if p and os.path.exists(p if os.path.isabs(p) else os.path.join(project, p)):
            return (f"{what} is left in {project}" if name == "index.lock" else f"{what} is in progress in {project}") \
                + "; finish or abort it there, then harness queue retry"
    if branch:
        on = git(project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
        if on != branch:
            return f"{project} is on {on or 'a detached HEAD'}, not the ticket's branch {branch}; " \
                   f"git switch {branch} there (your changes on the way stay), then harness queue retry"
    return ""


def in_place_branches(q, project):
    """The branches of the in-place tickets set up in project: the queue switches
    the project between them, never from a branch the owner put it on."""
    return {branch_of(s) for s in map(q.load, q.order())
            if s and s.get("in_place") and s.get("project") == project and s.get("start_sha")}


def back_on_branch(q, st):
    """In place, before a stage: the project back on the ticket's branch when
    another in-place ticket left it on its own, with nothing uncommitted but what
    the owner had when this ticket started, so no other ticket's leftovers come
    along. "" when it is on the branch, else why not."""
    project, branch, key = st["project"], branch_of(st), st["key"]
    trouble = in_place_trouble(project, None)
    if trouble:
        return trouble
    on = git(project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    if on == branch:
        return ""
    fix = f"git switch {branch} there (your uncommitted changes come along), then harness queue retry {key}"
    if not on or on not in in_place_branches(q, project):
        return f"{project} is on {on or 'a detached HEAD'}, not the ticket's branch {branch}; {fix}"
    owner = owner_files(q, st)
    mine = set(owner.get("untracked", [])) | set(owner.get("changed", []))
    now = uncommitted(project)
    new = sorted(p for p in now["untracked"] + now["changed"] if p not in mine)
    if new:
        return (f"{project} is on {on}, not the ticket's branch {branch}, and has uncommitted changes that were "
                f"not there when {key} started ({', '.join(new[:10])}); commit, stash or remove them, or {fix}")
    r = git(project, "switch", "-q", branch, check=False)
    if r.returncode != 0:
        return f"could not switch {project} from {on} to {branch}: {(r.stderr or r.stdout).strip()[:300]}; {fix}"
    q.log(key, f"switched {project} from {on} back to {branch}")
    return ""


def snapshot(q, st, stage):
    """In place, before each stage: the project's whole tree as it is (uncommitted
    and untracked files too; not ignored ones, nor nested repos' contents) as a
    commit at refs/harness-queue/snap/KEY, each one on the one before. If an agent
    throws away work: git checkout <snap> -- <path> takes it back."""
    project, key = st["project"], st["key"]
    ref = f"refs/harness-queue/snap/{key}"
    index = os.path.join(q.dir(key), "snap.index")
    real = git(project, "rev-parse", "--git-path", "index").stdout.strip()
    real = real if os.path.isabs(real) else os.path.join(project, real)
    who = {"GIT_INDEX_FILE": index, "GIT_AUTHOR_NAME": "harness queue", "GIT_AUTHOR_EMAIL": "harness-queue@localhost",
           "GIT_COMMITTER_NAME": "harness queue", "GIT_COMMITTER_EMAIL": "harness-queue@localhost"}
    try:
        if os.path.exists(real):   # git's stat cache: only what changed is read again
            shutil.copyfile(real, index)
        git(project, "-c", "advice.addEmbeddedRepo=false", "add", "-A", env=who, timeout=600)
        tree = git(project, "write-tree", env=who).stdout.strip()
        prev = git(project, "rev-parse", "-q", "--verify", ref, check=False).stdout.strip()
        sha = git(project, "commit-tree", tree, *(["-p", prev] if prev else []), "-m",
                  f"{key}: the project before {stage}", env=who).stdout.strip()
        git(project, "update-ref", ref, sha)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        q.log(key, f"warning: no snapshot of the project before {stage}: {e}")
    finally:
        drop(index)


def nested_changes(project):
    """["dir (n)"]: repositories inside the project (3 levels down) with changes; the
    queue commits in the project's own repository only."""
    out = []
    for root, dirs, files in os.walk(project):
        if root != project and (".git" in dirs or ".git" in files):
            dirs[:] = []
            n = len(git(root, "status", "--porcelain", check=False).stdout.splitlines())
            if n:
                out.append(f"{os.path.relpath(root, project)} ({n})")
            continue
        depth = os.path.relpath(root, project).count(os.sep)
        dirs[:] = [] if depth >= 2 else [d for d in dirs if d not in (".git", "node_modules")]
    return out


def fetch_branch(q, st):
    """The ticket's branch, fetched from the clone into the project (in place it is
    the project's); (sha, diff, files, commits) or None when it has no commits of its own."""
    key, project = st["key"], st["project"]
    ref = f"refs/heads/{branch_of(st)}"
    if not st.get("in_place"):   # in place the branch is the project's own
        r = git(project, "fetch", "-q", "--no-tags", workdir(q, st), f"+{ref}:refs/harness-queue/{key}", check=False)
        if r.returncode != 0:
            return None
        ref = f"refs/harness-queue/{key}"
    r = git(project, "rev-parse", "-q", "--verify", ref, check=False)
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    commits = int(git(project, "rev-list", "--count", f"{st['base_sha']}..{sha}").stdout.strip() or 0)
    if not commits:
        return None
    diff = git(project, "diff", "--no-color", "--no-ext-diff", st["base_sha"], sha).stdout
    files = [f for f in git(project, "diff", "--name-only", "--no-ext-diff", st["base_sha"], sha).stdout.splitlines() if f]
    return sha, diff, files, commits


def check_change(st, change, plan_text):
    """Hard gates on a branch, and the warnings that are not gates."""
    if change is None:
        return [f"no commits on branch {branch_of(st)}: commit your change on that branch (git add, git commit)"], []
    _, diff, files, _ = change
    problems, warnings = [], []
    if not diff.strip():
        problems.append("the commits change nothing (the diff is empty)")
    for f in files:
        if f.startswith(".github/"):
            problems.append(f"{f}: changes under .github/ (CI) are not made by the queue; leave CI to the owner")
        if SECRET_PATH.search(f):
            problems.append(f"{f}: a secret or key file must not be committed")
    notes = tuple(f"{st['key']}-{k}.md".lower() for k in ("plan", "review", "answers", "findings", "task"))
    for f in files:
        if f.lower().endswith(notes):
            problems.append(f"{f}: the queue's notes live in the vault, not the branch; git rm it")
    added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    if SECRET_RE.search(added):
        problems.append("the diff adds what looks like a key or token; remove it")
    for name in SECRET_ENV:
        v = os.environ.get(name, "")
        if len(v) >= 8 and v in diff:
            problems.append(f"the diff contains the value of {name}; remove it")
    planned = " ".join(bullets(sections(plan_text).get("files", "")))
    outside = [f for f in files if f not in planned and os.path.basename(f) not in planned]
    if outside and plan_text.strip():
        warnings.append("changed but not in the plan's Files: " + ", ".join(outside[:20]))
    return problems, warnings


# --- the verifier -----------------------------------------------------------------

def norm(text, diff=False):
    """Letters and digits only, lower-cased, so a quote matches whatever the
    spacing and punctuation; a diff loses its +/-/space column first."""
    lines = text.replace("\r\n", "\n").splitlines()
    if diff:
        lines = [l[1:] if l[:1] in ("+", "-", " ") else l for l in lines]
    return "".join(c for c in "\n".join(lines).lower() if c.isalnum())


def json_object(text):
    """The first balanced {...} in a reply, parsed."""
    try:
        return json.loads(text.strip(), strict=False)
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1], strict=False)
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def transient(e):
    """An upstream error worth trying again: no answer, 408, 429 or 5xx; not a
    4xx about the request itself (too long, malformed), which would fail again."""
    m = re.match(r"HTTP (\d{3})\b", str(e))
    return not m or m.group(1) in ("408", "429") or m.group(1).startswith("5")


def pause(ctx, secs):
    """Sleep, looking for a stop or halt every POLL seconds."""
    end = time.monotonic() + secs
    while time.monotonic() < end:
        time.sleep(max(0, min(POLL, end - time.monotonic())))
        if hasattr(ctx, "q"):
            checkpoint(ctx)


def model_call(ctx, messages, timeout, what="asking the verifier"):
    """call_model, tried again after INFRA_RETRY_PAUSES when the upstream fails
    (the error does not carry Retry-After, so the pauses are fixed)."""
    for n in range(len(INFRA_RETRY_PAUSES) + 1):
        step(ctx, what + (f" (try {n + 1})" if n else ""))
        try:
            return wait_call(ctx, call_model, ctx.proxy, ctx.verify_model, messages, timeout)
        except InfraError as e:
            if n == len(INFRA_RETRY_PAUSES) or not transient(e):
                raise
            if getattr(ctx, "key", ""):
                ctx.q.log(ctx.key, f"the verifier request failed ({str(e)[:120]}); trying again in "
                                   f"{INFRA_RETRY_PAUSES[n]}s")
            step(ctx, f"the verifier request failed; trying again in {INFRA_RETRY_PAUSES[n]}s")
            pause(ctx, INFRA_RETRY_PAUSES[n])


def ask_verifier(ctx, prompt, key, stage):
    """The verifier's JSON verdict; three tries with the whole prompt each time
    (the upstream reads only the last message). Every reply is kept for review."""
    reply = ""
    ctx.q.log(key, f"asking the verifier ({ctx.verify_model}) about the {stage}")
    for n in range(3):
        try:
            reply = model_call(ctx, [{"role": "user", "content": prompt}], VERIFY_TIMEOUT)
        except InfraError as e:
            probe(ctx)  # raises Infra when the upstream is down; else this request was the problem
            return {"verdict": "FAIL", "criteria": [], "problems": [f"the verifier request failed: {str(e)[:200]}"],
                    "unreadable": True}
        path = os.path.join(ctx.q.dir(key), f"verify-{stage}-{int(time.time())}-{n}.txt")
        write_atomic(path, reply)
        obj = json_object(reply)
        if isinstance(obj, dict) and isinstance(obj.get("criteria"), list):
            return obj
    return {"verdict": "FAIL", "criteria": [], "problems": ["the verifier's reply was not the JSON asked for (3 tries)"],
            "unreadable": True}


REMOVAL = re.compile(r"(?i)\b(remov|delet|drop|strip|get rid|no longer|stop|deprecat|eliminat)")


def diff_sides(diff):
    """(added, removed) lines of a diff, each normalized; context lines are in neither."""
    lines = diff.replace("\r\n", "\n").splitlines()
    added = [l for l in lines if l.startswith("+") and not l.startswith("+++ ")]
    removed = [l for l in lines if l.startswith("-") and not l.startswith("--- ")]
    return norm("\n".join(added), diff=True), norm("\n".join(removed), diff=True)


def judge(obj, criteria, source, quote_field, what, notes="", soft=None):
    """Problems with a verdict: every criterion answered, met (for a change),
    and every quote found in <source>. For a diff a quote must be from an added
    line (a removed one when the criterion is about removing; never an unchanged
    one); a reviewer's "(review)" item may instead quote <notes>, the commit
    messages saying why it is wrong. A ticket quote the verifier got slightly
    wrong goes to <soft> instead (a list, logged): one found quote is enough
    to show the plan is about the ticket."""
    soft = [] if soft is None else soft
    if obj.get("unreadable"):
        return [str(p) for p in obj["problems"]]
    problems = []
    if what == "diff":
        added, removed = diff_sides(source)
    else:
        src = norm(source)
    got = {}
    for c in obj.get("criteria", []):
        if isinstance(c, dict):
            try:
                got[int(c.get("n"))] = c
            except (TypeError, ValueError):
                pass
    found = 0
    for i, crit in enumerate(criteria, 1):
        c = got.get(i)
        if c is None:
            problems.append(f"criterion {i} was not checked: {crit[:100]}")
            continue
        if c.get("met") is False or c.get("ok") is False:
            problems.append(f"criterion {i} failed the check: {crit[:100]} ({str(c.get('why', ''))[:200]})")
            continue
        quote = norm(str(c.get(quote_field) or ""))
        if not quote:
            if what == "diff":
                problems.append(f"criterion {i}: no quote from the diff shows it is met")
            continue
        if what == "diff":
            ok = quote in added or (REMOVAL.search(crit) and quote in removed) \
                or (crit.startswith("(review) ") and quote in norm(notes))
        else:
            ok = quote in src
        if len(quote) >= 8 and ok:
            found += 1
        else:
            (soft if what == "ticket" else problems).append(
                f"criterion {i}: the quoted {what} text is not in the {what}: {quote[:120]}")
    if what == "ticket" and criteria and not found:
        problems.append("no criterion quotes the ticket: the plan may not be about this ticket")
    if str(obj.get("verdict", "")).upper() != "PASS":
        problems.extend(str(p)[:300] for p in obj.get("problems", []) if str(p).strip())
        if not problems:
            problems.append("the verifier said FAIL without a reason")
    return problems


# --- prompts and agent launches -------------------------------------------------------

def render(name, **values):
    """Fill {{NAME}}s in one pass: ticket text that says {{PLAN}} stays as it is."""
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))),
                  read(os.path.join(PROMPTS, name + ".md")))


def run_agent(ctx, st, stage, prompt):
    """One headless agent launch in the clone; its output goes to DIR/<stage>-N.out."""
    key = st["key"]
    model = ctx.model if "/" in ctx.model else "harness/" + ctx.model
    cmd = [BASH, ctx.harness]
    if ctx.host or ctx.single:   # --single-message is a host-mode launch flag
        cmd.append("host")
    if ctx.single:
        cmd.append("--single-message")
    # The whole prompt is also in the vault while the stage runs: a no-tools model
    # loses the prompt after its first tool call, and the proxy points it there.
    task = note_paths(ctx.vault, key)["task"]
    arg = prompt
    if WINDOWS and len(prompt) > WIN_PROMPT_MAX:
        arg = (f"You are working on ticket {key}, stage {stage}, in {'the project' if st.get('in_place') else 'a clone of the project'} "
               f"on branch {branch_of(st)}.\n\n"
               f"Your full instructions for the {stage} stage of ticket {key} are in {agent_path(ctx, task)}. "
               f"Read it first and follow it exactly.\n")
    elif len(prompt.encode()) > PROMPT_MAX_BYTES:
        raise RuntimeError(f"the {stage} prompt is {len(prompt.encode())} bytes, over one command-line argument's limit")
    cmd += ["-p", "--yolo", "--ticket", key, "-m", model, arg]
    out = os.path.join(ctx.q.dir(key), f"{stage}-{st['attempt']}.out")
    since = int(time.time() * 1000)
    write_atomic(task, prompt)
    if st.get("in_place"):
        exclude_block(st["project"], None)   # the snapshot takes the owner's files too
        snapshot(ctx.q, st, stage)
        exclude_block(st["project"], owner_files(ctx.q, st).get("untracked"))
        # when the stage ran, for the run viewer: in place its session's folder is the project's
        st["launches"] = (st.get("launches") or [])[-(HISTORY_CAP - 1):] + [[since, 0]]
        ctx.q.save(st)
    try:
        rc = launch(ctx, st, cmd, out, since)
    finally:   # gone before the vault is committed: it is the prompt, not a note
        drop(task)
        if st.get("in_place"):
            st["launches"][-1][1] = int(time.time() * 1000)
            ctx.q.save(st)
            with contextlib.suppress(RuntimeError, OSError, subprocess.SubprocessError):
                exclude_block(st["project"], None)
    if st.get("in_place"):
        nested = nested_changes(st["project"])
        if nested:
            ctx.q.log(key, "changes in repositories inside the project, which the queue does not commit: "
                           + ", ".join(nested[:20]))
    sync_vault(ctx, key, since)
    return rc, out


def drop(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:   # Windows: something has it open for a moment
        time.sleep(1)
        with contextlib.suppress(OSError):
            os.remove(path)


INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "CONTEXT.md")   # what opencode looks for, in its order


def copy_instructions(project, repo):
    """The project's own AGENTS.md (or CLAUDE.md, CONTEXT.md) into the clone when
    the clone has none: a clone has only what is committed, so an instruction file
    kept uncommitted in the project would never reach the agent. Listed in the
    clone's .git/info/exclude, so it is never committed or pushed. Before every
    stage, so a file added since the clone is picked up."""
    for name in INSTRUCTION_FILES:
        src, dst = os.path.join(project, name), os.path.join(repo, name)
        if not os.path.isfile(src) or os.path.lexists(dst):
            continue
        try:
            shutil.copyfile(src, dst)
            exclude = os.path.join(repo, ".git", "info", "exclude")
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            with open(exclude, "a+", encoding="utf-8") as f:
                f.seek(0)
                if f"/{name}" not in f.read().splitlines():
                    f.write(f"\n/{name}\n")
        except OSError as e:
            print(f"[queue] warning: could not copy {src} into the clone: {e}", file=sys.stderr)


def launch(ctx, st, cmd, out, since):
    """Run the agent to its end, a timeout (rc 124), or the owner's stop or halt (Interrupted)."""
    key = st["key"]
    # HARNESS_QUEUE_LAUNCH: harness labels the container with it (what stop_containers
    # stops) and does not remember the ticket for the folder (in place, the owner's)
    st["launch_id"] = f"{key}-{os.getpid()}-{int(time.time() * 1000)}"
    env = dict(os.environ, HARNESS_HOST_CONFIRM="1", HARNESS_QUEUE_LAUNCH=st["launch_id"])
    end = time.monotonic() + ctx.stage_timeout
    notes_at = mtime(ctx.q.notes_path(key))
    if not st.get("in_place"):
        copy_instructions(st["project"], workdir(ctx.q, st))
    step(ctx, "the agent is working")
    with open(out, "w", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, cwd=workdir(ctx.q, st), stdin=subprocess.DEVNULL,
                             stdout=f, stderr=subprocess.STDOUT, env=env, **NEW_GROUP)
        ctx.child = p
        beat(ctx, agent=p.pid, launch=st["launch_id"])   # halt --force ends it (and its container) with the run
        rc = None
        try:
            while rc is None:
                try:
                    rc = p.wait(timeout=max(0.1, min(POLL, end - time.monotonic())))
                except subprocess.TimeoutExpired:
                    checkpoint(ctx)
                    if mtime(ctx.q.notes_path(key)) != notes_at:   # a note: into state.json for the viewer
                        notes_at = mtime(ctx.q.notes_path(key))
                        ctx.q.save(st)
                    if time.monotonic() >= end:
                        stop(p)
                        stop_containers(ctx, st)
                        rc = 124
        except Interrupted:
            stop(p)
            stop_containers(ctx, st)
            drop(note_paths(ctx.vault, key)["task"])
            sync_vault(ctx, key, since)
            raise
        except BaseException:  # Ctrl-C: the agent is in its own session and did not get it
            stop(p)
            raise
        finally:
            ctx.child = None
            beat(ctx, agent=0, launch="")
            step(ctx, "the agent ended; checking its work")
    return rc


def stop_containers(ctx, st):
    """Stopping the docker client leaves its container running: stop the agent
    container of this ticket's launch (harness queue _stop_agents, by the folder
    it mounts and the launch's label)."""
    if ctx.host or ctx.single:
        return
    key = st["key"]
    try:
        r = subprocess.run([BASH, ctx.harness, "queue", "_stop_agents", workdir(ctx.q, st), st.get("launch_id", "")],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False, **TEXT)
        n = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        n = ""
    if n.isdigit():
        if int(n):
            ctx.q.log(key, f"stopped {n} agent container(s)")
    else:
        ctx.q.log(key, "warning: the agent's container may still be running; check 'harness list' "
                       "(harness stop NAME) before 'harness queue retry'")


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def checkpoint(ctx):
    """Raise Interrupted when the owner asked the run (DIR/halt) or this ticket (KEY/stop) to stop."""
    if os.path.exists(ctx.q.sentinel()):
        raise Interrupted("halt")
    key = getattr(ctx, "key", "")
    if key and os.path.exists(ctx.q.sentinel(key)):
        raise Interrupted("stop")


def wait_call(ctx, fn, *args):
    """fn(*args) in a thread, looking for a stop or halt every POLL seconds while it
    is out (a verifier request can take minutes); a call given up on is left to end
    by itself."""
    box = {}

    def go():
        try:
            box["r"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            box["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    while t.is_alive():
        t.join(POLL)
        if t.is_alive() and hasattr(ctx, "q"):
            checkpoint(ctx)
    if "e" in box:
        raise box.pop("e")
    return box["r"]


def sync_vault(ctx, key, since):
    """Copy the ticket's queue log (stages, checks, verifier verdicts) to KEY-log.md in the
    vault, then capture the session and commit the vault (best effort; never fails a stage)."""
    log = read(os.path.join(ctx.q.dir(key), "log.md"))
    if log.strip():
        fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", log)), default=0))
        write_atomic(note_paths(ctx.vault, key)["log"],
                     f"---\ntype: note\nticket: {key}\nsummary: {yq(f'harness queue log of {key}')}\n---\n\n"
                     f"# {key}: queue log\n\nWritten by `harness queue`; each line is a stage, a check or a verdict.\n\n"
                     f"{fence}text\n{log.rstrip()}\n{fence}\n")
    st = ctx.q.load(key) or {"key": key, "project": ""}
    # no pipes: on Windows a timed-out run() waits for its pipes to close, which a
    # process the call left behind (a server, git's fsmonitor daemon) never does
    subprocess.run([BASH, ctx.harness, "queue", "_sync", key, workdir(ctx.q, st), str(since)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=600, check=False)


# On Windows a bare "bash" can resolve to WSL's System32\bash.exe before PATH is searched;
# shutil.which walks PATH, where Git Bash puts its own bash first.
BASH = shutil.which("bash") or "bash"

# The agent gets its own session (process group on Windows) so Ctrl-C reaches only the queue,
# which then stops the agent's whole tree itself.
NEW_GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})


def stop(p):
    if os.name == "nt":  # no killpg or SIGKILL: taskkill /T ends bash and everything it started
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], stdin=subprocess.DEVNULL,
                       capture_output=True, check=False)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        return
    for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
        try:
            os.killpg(p.pid, sig)
            p.wait(timeout=wait)
            return
        except (ProcessLookupError, subprocess.TimeoutExpired):
            continue


def probe(ctx):
    """Raise Infra when the upstream does not answer one tiny request."""
    try:
        model_call(ctx, [{"role": "user", "content": "Reply with the single word OK."}], 180,
                   "checking that the upstream answers")
    except InfraError as e:
        raise Infra(str(e)) from None


# --- stages -------------------------------------------------------------------------------

_AGENT_PATHS = {}


def agent_path(ctx, path):
    """A host path as the agent sees it. A container launch on Windows mounts the
    vault at its Git Bash spelling (/c/Users/...); C:\\Users\\... means nothing in
    Linux, and an agent told to write its plan there never does."""
    if os.name != "nt":
        return path
    if ctx.host or ctx.single:   # C:/...: opencode takes it, and a no-tools model's JSON has no \\ to escape
        return path.replace("\\", "/")
    if path not in _AGENT_PATHS:
        try:
            got = subprocess.run(["cygpath", "-u", path], capture_output=True, text=True, timeout=10,
                                 stdin=subprocess.DEVNULL).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            got = ""
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
        _AGENT_PATHS[path] = got or (f"/{m.group(1).lower()}/{m.group(2)}" if m else path).replace("\\", "/")
    return _AGENT_PATHS[path]


def common(ctx, st):
    p = note_paths(ctx.vault, st["key"])
    ap = {k: agent_path(ctx, v) for k, v in p.items()}
    answers = read(p["answers"])
    if len(answers) > PLAN_CAP:
        answers = "[... earlier answers cut ...]\n" + answers[-PLAN_CAP:]
    plan = read(plan_copy(ctx.q, st["key"]), PLAN_CAP) if st.get("stage", "plan") != "plan" else ""
    if not plan:   # the vault's copy is unchecked: only the plan stage itself reads it
        plan = NO_PLAN if "plan" not in stages_of(st) else read(p["plan"], PLAN_CAP) or "(none yet)"
    return dict(KEY=st["key"], BRANCH=branch_of(st), BASE_SHA=st.get("base_sha", ""),
                REPO=workdir(ctx.q, st), WHERE=IN_PLACE if st.get("in_place") else IN_CLONE,
                TICKET_FILE=ap["ticket"],
                TICKET=read(p["ticket"], TICKET_CAP) or "(the ticket file is missing; read the path above)",
                PLAN_FILE=ap["plan"], PLAN=plan,
                REVIEW_FILE=ap["review"], FINDINGS_FILE=ap["findings"], ANSWERS_FILE=ap["answers"],
                ANSWERS=answers or "(none)", NOTES=owner_notes(ctx.q.notes(st["key"])),
                FEEDBACK=cap(st.get("feedback") or "(first attempt)", NOTE_CAP))


IN_CLONE = "a clone of the project made for this ticket"
IN_PLACE = ("the owner's own project folder, not a clone. It has the owner's uncommitted files: leave every file you "
            "do not change for the ticket alone; commit only files you changed, by name (git add <file>; never "
            "git add -A, git add . or git commit -a); never run git checkout/restore of files you did not change, "
            "git reset --hard, git clean, git stash or git switch. Repositories inside it are not committed by harness: "
            "change them only when the ticket needs it and say so in your commit message")


def owner_notes(notes):
    """The owner's notes for a prompt, as written, newest kept when they are long."""
    text = "\n\n".join(f"[{n.get('at', '')}]\n{str(n.get('text', '')).strip()}" for n in notes)
    if len(text) > NOTE_CAP:
        text = "[... earlier notes cut ...]\n" + text[-NOTE_CAP:]
    return text or "(none)"


def ticket_criteria(text):
    """The bullets under the ticket's own 'Acceptance criteria' heading, if it has one."""
    m = re.search(r"(?im)^[#*\s]*acceptance criteria\b.*$", text)
    if not m:
        return []
    return bullets(re.split(r"(?m)^#{1,6}\s", text[m.end():], maxsplit=1)[0])[:15]


def change_criteria(ctx, st, plan):
    """What a change is held to: the plan's acceptance criteria; with no plan (the
    stage is not in the ticket's stages) the ticket's own, else the ticket as a whole."""
    if plan.strip():
        return check_plan(plan)[1]
    crit = ticket_criteria(read(note_paths(ctx.vault, st["key"])["ticket"]))
    ctx.q.log(st["key"], "no plan: the change is checked against " +
              (f"the ticket's {len(crit)} acceptance criteria" if crit else "the ticket as a whole"))
    return crit or [NO_PLAN_CRITERION]


def plan_copy(q, key):
    """The plan as the verifier passed it, which later stages are held to
    (an agent can write to the vault's copy)."""
    return os.path.join(q.dir(key), "plan.md")


def fresh(path, started):
    """The file's text if it was written since started, else ""."""
    return read(path) if os.path.exists(path) and os.path.getmtime(path) >= started - 1 else ""


# A line of each prompt's template: output holding it is the prompt echoed, not the agent's note.
NOTE_TEMPLATE = {"plan": "One or two sentences: what changes for the user of this code",
                 "review": "Only real defects that would make the change wrong, unsafe or not meet"}
NOTE_HEADS = {"plan": r"^##\s+Goal\b", "review": r"^##\s+Must fix\b"}


def recover_note(ctx, st, kind, started, out):
    """The plan or review the agent wrote somewhere else, put where it belongs
    (KEY-plan.md / KEY-review.md in the vault); its text, or "". An agent that
    cannot write the vault path writes the file into its clone, or prints it."""
    key = st["key"]
    dest = note_paths(ctx.vault, key)[kind]
    repo = workdir(ctx.q, st)
    want = f"{key}-{kind}.md".lower()
    found = None
    for root, dirs, files in os.walk(repo):
        depth = os.path.relpath(root, repo).count(os.sep)
        dirs[:] = [] if depth >= 3 else [d for d in dirs if d not in (".git", "node_modules")]
        for f in files:
            # endswith: the name can be a whole Windows path the agent took for a file name
            if f.lower().endswith(want):
                path = os.path.join(root, f)
                try:
                    m = os.path.getmtime(path)
                except OSError:
                    continue
                if m >= started - 1 and (found is None or m > found[0]):
                    found = (m, path)
    text, where = "", ""
    if found:
        text, where = read(found[1]), f"the agent's folder ({os.path.relpath(found[1], repo)})"
        try:
            os.remove(found[1])   # not the agent's change to commit
        except OSError:
            pass
    else:
        printed = read(out)
        heads = list(re.finditer(NOTE_HEADS[kind], printed, re.M | re.I))
        if heads and NOTE_TEMPLATE[kind] not in printed[heads[-1].start():]:
            text, where = printed[heads[-1].start():], "the agent's output"
    if not text.strip():
        return ""
    write_atomic(dest, text if text.endswith("\n") else text + "\n")
    ctx.q.log(key, f"the {kind} was not at {dest}; taken from {where}")
    return text


def stage_plan(ctx, st):
    v = common(ctx, st)
    started = time.time()
    rc, out = run_agent(ctx, st, "plan", render("plan", **v))
    p = note_paths(ctx.vault, st["key"])
    text = fresh(p["plan"], started) or recover_note(ctx, st, "plan", started, out)
    problems, criteria, questions = check_plan(text)
    if not text.strip():
        problems = [f"the plan file was not written: write it to {v['PLAN_FILE']}"]
    if not problems:
        notes = plan_notes(text)
        obj = ask_verifier(ctx, render("verify_plan", TICKET=read(p["ticket"], DIFF_CAP_VERIFY), PLAN=text,
                                       CRITERIA=numbered(criteria)), st["key"], "plan")
        problems = judge(obj, criteria, read(p["ticket"]), "ticket_quote", "ticket", soft=notes)
        ctx.q.log(st["key"], f"plan verifier: {obj.get('verdict')} ({len(problems)} problem(s))")
        if notes:
            ctx.q.log(st["key"], "plan notes (not failures)", "\n".join("- " + n for n in notes))
    if problems:
        return fail(ctx, st, rc, problems)
    st["questions"] = [{"text": x, "blocking": bool(blocking([x]))} for x in questions]
    open_q = blocking(questions)
    if open_q:
        record(st, "questions", open_q)
        st.update(status="needs-answers", stage="plan", attempt=0, feedback="",
                  reason=f"{len(open_q)} blocking question(s) in the plan")
        ctx.q.save(st)
        ctx.q.log(st["key"], f"needs answers: harness queue answer {st['key']} (or the ticket's page in harness serve)",
                  "\n".join("- " + x for x in open_q))
        return
    write_atomic(plan_copy(ctx.q, st["key"]), text)
    advance(ctx, st, next_stage(st, "plan"))
    held = ctx.plan_review or st.get("plan_review") or st.get("plan_hold")
    if held and not st.get("plan_approved") and st["status"] == "running":
        st.update(status="plan-review", reason=f"plan ready to read: {p['plan']}")
        ctx.q.save(st)
        ctx.q.log(st["key"], f"waiting for the plan to be read: harness queue approve {st['key']}")


def numbered(items):
    return "\n".join(f"{i}. {c}" for i, c in enumerate(items, 1))


def verify_change(ctx, st, rc, stage):
    """Gates then the verifier on the branch; True when it passes."""
    p = note_paths(ctx.vault, st["key"])
    plan = read(plan_copy(ctx.q, st["key"]))
    criteria = change_criteria(ctx, st, plan)
    if stage == "fix":
        criteria = criteria + [f"(review) {m}" for m in st.get("must_fix", [])]
    change = fetch_branch(ctx.q, st)
    problems, warnings = check_change(st, change, plan)
    if st.get("in_place"):
        problems += in_place_problems(ctx, st)
    if not problems and stage == "implement" and change[0] == st.get("start_sha"):
        problems.append(f"no new commit on branch {branch_of(st)}: commit your change (git add <files>, git commit)")
    if not problems and stage == "fix" and change[0] == st.get("review_sha"):
        problems.append("no new commit since the review: fix the Must fix items and commit")
    if not problems and rc != 0:
        problems.append("the agent did not finish; check the work is complete and committed")
    for w in warnings:
        ctx.q.log(st["key"], f"warning: {w}")
    if not problems:
        _, diff, files, commits = change
        ctx.q.log(st["key"], f"{stage}: {commits} commit(s), {len(files)} file(s) changed")
        notes = ""
        if stage == "fix" and st.get("review_sha"):
            notes = git(st["project"], "log", "--format=%B", f"{st['review_sha']}..{change[0]}",
                        check=False).stdout.strip()
        obj = ask_verifier(ctx, render("verify_change", TICKET=read(p["ticket"], TICKET_CAP),
                                       CRITERIA=numbered(criteria), DIFF=cap(diff, DIFF_CAP_VERIFY),
                                       COMMITS=cap(notes, NOTE_CAP) or "(none)"),
                           st["key"], stage)
        problems = judge(obj, criteria, diff, "quote", "diff", notes)
        ctx.q.log(st["key"], f"{stage} verifier: {obj.get('verdict')} ({len(problems)} problem(s))")
        st["warnings"] = warnings
    if problems:
        fail(ctx, st, rc, problems)
        return False
    return True


def in_place_problems(ctx, st):
    """In place: the project left on the ticket's branch with nothing half done, and
    no commit since the start touching a file the owner had uncommitted when the
    ticket started (each commit, not the sum: a file added then removed was pushed)."""
    project = st["project"]
    trouble = in_place_trouble(project, branch_of(st))
    if trouble:
        return [trouble]
    owner = owner_files(ctx.q, st)
    mine = set(owner.get("untracked", [])) | set(owner.get("changed", []))
    dirs = tuple(p for p in mine if p.endswith("/"))
    touched = git(project, "log", "--format=", "--name-only", "--no-renames",
                  f"{st['start_sha']}..refs/heads/{branch_of(st)}", check=False).stdout.split("\n")
    hit = sorted({f for f in touched if f and (f in mine or f + "/" in mine or f.startswith(dirs))})
    if not hit:
        return []
    return [f"the commits touch {', '.join(hit[:10])}, which the owner had uncommitted in the project when the ticket "
            f"started (theirs, not the ticket's). Take them out of every commit since {st['start_sha'][:12]}: "
            f"git reset --soft {st['start_sha'][:12]}, git restore --staged {' '.join(hit[:10])}, then commit "
            "again with only your files. Never delete or edit those files"]


def cap(text, n):
    return text if len(text) <= n else text[:n] + f"\n[... cut at {n} characters ...]\n"


def stage_implement(ctx, st):
    rc, _ = run_agent(ctx, st, "implement", render("implement", **common(ctx, st)))
    if verify_change(ctx, st, rc, "implement"):
        advance(ctx, st, next_stage(st, "implement"))


def stage_review(ctx, st):
    change = fetch_branch(ctx.q, st)
    v = common(ctx, st)
    v["DIFF"] = cap(change[1], DIFF_CAP_AGENT) if change else "(no diff)"
    started = time.time()
    rc, out = run_agent(ctx, st, "review", render("review", **v))
    path = note_paths(ctx.vault, st["key"])["review"]
    text = fresh(path, started) or recover_note(ctx, st, "review", started, out)
    problems, must = check_review(text)
    if not text.strip():
        problems = [f"the review file was not written: write it to {v['REVIEW_FILE']}"]
    if problems:
        return fail(ctx, st, rc, problems)
    ctx.q.log(st["key"], f"review: {len(must)} must-fix item(s)", "\n".join("- " + m for m in must))
    st.update(must_fix=must[:15], review_sha=change[0] if change else "")
    after = next_stage(st, "review")
    if not must or "fix" in stages_of(st):
        return advance(ctx, st, "fix" if must else after)
    found = f"the review found {len(must)} must-fix item(s) ({path}); fix is not in this ticket's stages"
    if after:   # nothing after a review that wants fixes, without them
        return block(ctx, st, f"{found}, so {after} did not run: harness queue retry {st['key']} --stage fix "
                              f"(or --stage {after} to go on as it is)")
    advance(ctx, st, None, reason=found)


def stage_fix(ctx, st):
    v = common(ctx, st)
    v["MUST_FIX"] = cap("\n".join("- " + m for m in st.get("must_fix", [])), NOTE_CAP)
    rc, _ = run_agent(ctx, st, "fix", render("fix", **v))
    if verify_change(ctx, st, rc, "fix"):
        advance(ctx, st, next_stage(st, "fix"))


def stage_push(ctx, st):
    key, project = st["key"], st["project"]
    change = fetch_branch(ctx.q, st)
    if change is None:
        return block(ctx, st, "the branch is gone from the clone")
    sha, name = change[0], branch_of(st)
    branch = f"refs/heads/{name}"
    old = git(project, "rev-parse", "-q", "--verify", branch, check=False).stdout.strip()
    if st.get("in_place"):
        pass   # the agents committed on the project's branch itself
    elif checked_out(project, name):
        # Moving a checked-out branch leaves its index and files behind: the
        # checkout would then show the queue's commits as changes undone.
        ctx.q.log(key, f"warning: branch {name} is checked out in the project; not moved to {sha[:12]} "
                       f"(git merge --ff-only {sha[:12]} there takes it)")
    elif not old:
        git(project, "update-ref", branch, sha)
        st["made_branch"] = True
    elif old != sha:
        if git(project, "merge-base", "--is-ancestor", old, sha, check=False).returncode == 0:
            git(project, "update-ref", branch, sha, old)
        else:
            ctx.q.log(key, f"warning: the project's branch {name} is not behind the queue's branch; left as it is")
    if ctx.push:
        checkpoint(ctx)   # a stop asked during the stage: nothing pushed
        r = git(project, "-c", "credential.interactive=false", "push", "-q", ctx.remote, f"{sha}:{branch}",
                check=False, timeout=600, env=push_env(project))
        if r.returncode != 0:
            return block(ctx, st, f"push to {ctx.remote} refused: {(r.stderr or r.stdout).strip()[:300]}")
        ctx.q.log(key, f"pushed branch {name} to {ctx.remote} ({sha[:12]})")
    else:
        ctx.q.log(key, f"branch {name} ready in the project ({sha[:12]}); not pushed (QUEUE_PUSH=0)")
    record(st, "passed", [f"branch {name} at {sha[:12]}"])
    st.update(status="done", stage="done", attempt=0, feedback="", reason="", sha=sha)
    ctx.q.save(st)


def push_env(project):
    """No credential prompt can hold a push (nobody is watching): Git Credential
    Manager and ssh fail instead of asking. An ssh the owner chose (GIT_SSH,
    GIT_SSH_COMMAND, core.sshCommand: plink, a key, the Windows agent) is kept."""
    env = {"GCM_INTERACTIVE": "never"}
    if not (os.environ.get("GIT_SSH") or os.environ.get("GIT_SSH_COMMAND")
            or git(project, "config", "core.sshCommand", check=False).stdout.strip()):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


STAGES = {"plan": stage_plan, "implement": stage_implement, "review": stage_review, "fix": stage_fix,
          "push": stage_push}


def record(st, result, details=()):
    """One entry in the ticket's history (state.json), which the run viewer shows:
    each stage attempt's result and, for a failed one, why."""
    h = st.setdefault("history", [])
    h.append({"at": now_iso(), "stage": st.get("stage", ""), "attempt": st.get("attempt", 0) + 1,
              "result": result, "details": [str(d)[:400] for d in list(details)[:20]]})
    del h[:-HISTORY_CAP]


def advance(ctx, st, stage, reason=""):
    """On to stage; None: the ticket's stages are done (push marks done itself)."""
    record(st, "passed")
    if stage:
        st.update(stage=stage, attempt=0, feedback="", status="running")
    else:
        if not reason and "push" not in stages_of(st):
            reason = f"its stages are done ({','.join(stages_of(st))}); nothing pushed"
        st.update(stage="done", attempt=0, feedback="", status="done", reason=reason)
        ctx.q.log(st["key"], f"done: {reason}" if reason else "done")
    ctx.q.save(st)


def block(ctx, st, reason):
    record(st, "blocked", [reason])
    st.update(status="blocked", reason=reason)
    ctx.q.save(st)
    ctx.q.log(st["key"], f"blocked at {st['stage']}: {reason}")


def fail(ctx, st, rc, problems):
    """A stage did not pass: stop the run if the upstream is down, else retry
    with the problems listed, then block."""
    if rc == 124:
        return block(ctx, st, f"the {st['stage']} agent ran past {ctx.stage_timeout}s and was stopped; a container "
                              "launch can outlive that, so check 'harness list' before 'harness queue retry'")
    if rc not in (0, None):
        problems = [f"the agent launch exited with status {rc}"] + problems
    probe(ctx)  # raises Infra: the ticket stays at this stage, the attempt is not counted
    text = "\n".join("- " + p for p in problems)
    ctx.q.log(st["key"], f"{st['stage']} attempt {st['attempt'] + 1} failed", text)
    record(st, "failed", problems)
    if st["attempt"] < ctx.retries:
        st.update(attempt=st["attempt"] + 1, feedback=text)
        ctx.q.save(st)
    else:
        block(ctx, st, f"{st['stage']} failed {st['attempt'] + 1} time(s): {problems[0][:200]}")


def process(ctx, st):
    key = st["key"]
    checkpoint(ctx)
    # running (and so not answerable) before the clone, which can take minutes;
    # the vault is kept for the run viewer, which may have started with another
    st.update(status="running", vault=ctx.vault, reason="")
    ctx.q.save(st)
    if st.get("in_place") and os.name == "nt" and not (ctx.host or ctx.single):
        # the container's Linux git would see every file of a CRLF checkout as changed
        return block(ctx, st, "working in the project folder on Windows needs the queue run on the host "
                              "(harness queue run --host, or Start the queue with host); or remove the ticket and "
                              "add it again to work in a separate copy (--clone)")
    if st.get("in_place") and st.get("base_sha"):
        trouble = back_on_branch(ctx.q, st)
        if trouble:
            return block(ctx, st, trouble)
    elif not st.get("base_sha") or not os.path.isdir(workdir(ctx.q, st)):
        if st.get("base_sha") and st["stage"] != first_stage(st):
            return block(ctx, st, f"its clone is missing; harness queue retry starts it again from {first_stage(st)}")
        step(ctx, f"setting up {branch_of(st)} in {st['project']}" if st.get("in_place") else f"cloning {st['project']}")
        try:
            kept = enter_in_place(ctx.q, st) if st.get("in_place") else make_clone(ctx.q, st, container=not (ctx.host or ctx.single))
        except (RuntimeError, subprocess.SubprocessError) as e:
            return block(ctx, st, f"could not {'set up' if st.get('in_place') else 'clone'} {st['project']} "
                                  f"at {start_of(st)}: {e}")
        where = f"in place in {st['project']}" if st.get("in_place") else f"cloned {st['project']}"
        ctx.q.log(key, f"{where}: branch {branch_of(st)} from {start_of(st)} ({st['start_sha'][:12]}); the change is "
                       f"since {st['base_sha'][:12]} (base {st['base']})" + (f"; {kept}" if kept else ""))
    subprocess.run([BASH, ctx.harness, "queue", "_pull", key], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=False)
    while st["status"] == "running":
        checkpoint(ctx)
        beat(ctx, ticket=key, stage=st["stage"], step="")
        ctx.q.log(key, f"{st['stage']} (attempt {st['attempt'] + 1}, {ctx.model})")
        STAGES[st["stage"]](ctx, st)


def beat(ctx, **change):
    """DIR/run.json, what the run is doing and when it last said so (the viewer
    takes a beat older than 30 s for a run that is gone)."""
    with ctx.beat_lock:
        if ctx.beating.is_set():   # the run has ended and run.json is gone
            return
        ctx.run_info.update(change, beat=int(time.time()))
        try:
            write_atomic(os.path.join(ctx.q.root, "run.json"), json.dumps(ctx.run_info) + "\n")
        except OSError:
            pass


def step(ctx, text):
    """What the run is doing in its ticket's stage, for the viewer (run.json "step")."""
    if hasattr(ctx, "run_info"):
        beat(ctx, step=text)


def put_back(ctx, st, why):
    """The run ends with this ticket taken (the upstream does not answer, Ctrl-C):
    it goes back to the queue at its stage, the attempt not counted, saying why."""
    record(st, "interrupted", [why])
    st.update(status="queued", reason=why)
    ctx.q.save(st)
    ctx.q.log(st["key"], f"{why}; back in the queue at {st['stage']}")


def interrupted(ctx, st, e):
    """The owner's stop (the ticket is held, the run goes on) or halt (the ticket
    goes back to the queue at its stage and the run ends); the attempt is not counted."""
    key = st["key"]
    if e.kind == "halt":
        record(st, "halted", ["the run was halted by the owner; the attempt does not count"])
        st.update(status="queued")
        ctx.q.log(key, f"halted by the owner: back in the queue at {st['stage']}")
    else:
        record(st, "stopped", ["stopped by the owner"])
        st.update(status="stopped", reason="stopped by the owner")
        ctx.q.log(key, f"stopped by the owner at {st['stage']}; harness queue retry {key} resumes it")
    ctx.q.save(st)
    if e.kind == "stop":
        try:
            os.remove(ctx.q.sentinel(key))
        except OSError:
            pass


def cmd_run(q, a):
    lock = open(os.path.join(q.root, "run.lock"), "a")
    if not try_lock(lock):
        lock.close()
        print("[queue] another 'harness queue run' is running", file=sys.stderr)
        return 1
    locked_at = time.time()
    try:   # released here, not whenever the file object is collected (an exception's frames can hold it)
        return run_locked(q, a, locked_at)
    finally:
        unlock(lock)
        lock.close()


def run_locked(q, a, locked_at):
    ctx = argparse.Namespace(q=q, vault=os.path.abspath(a.vault), proxy=a.proxy, harness=a.harness, host=a.host,
                             single=a.single_message, retries=a.retries, push=a.push == 1, remote=a.remote,
                             plan_review=a.plan_review == 1, stage_timeout=a.stage_timeout, child=None, key="",
                             beat_lock=threading.Lock(), beating=threading.Event())
    ctx.model = a.model or resolve_model(a.proxy, a.default_model)
    if not ctx.model:
        print("[queue] no model: set QUEUE_MODEL or DEFAULT_MODEL_NAME", file=sys.stderr)
        return 1
    ctx.verify_model = (a.verify_model or ctx.model).split("/", 1)[-1]
    at = mtime(q.sentinel())
    if at is not None and at < locked_at:   # a halt asked of a run that had already ended,
        drop(q.sentinel())                  # not one asked of this run as it started
    keep_tasks_out(q, ctx.vault)
    ctx.run_info = {"pid": os.getpid(), "started": now_iso(), "beat": 0, "host": bool(ctx.host or ctx.single),
                    "single": bool(ctx.single), "model": ctx.model, "ticket": "", "stage": "", "step": ""}
    beat(ctx)

    def beats():
        while not ctx.beating.wait(BEAT):
            beat(ctx)
    threading.Thread(target=beats, daemon=True).start()
    try:
        return run_tickets(q, ctx)
    finally:
        ctx.beating.set()
        with ctx.beat_lock:
            try:
                os.remove(os.path.join(q.root, "run.json"))
            except OSError:
                pass


TASK_IGNORE = "wiki/notes/tickets/*-task.md"


def keep_tasks_out(q, vault):
    """KEY-task.md is a stage's prompt, not a note: the vault's git ignores it (any
    vault commit can run while a stage does), and one a killed run left is removed."""
    path = os.path.join(vault, ".gitignore")
    try:
        text = read(path) if os.path.exists(path) else ""
        if TASK_IGNORE not in text.splitlines():
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(("" if not text or text.endswith("\n") else "\n")
                        + "# harness queue: a stage's prompt while it runs\n" + TASK_IGNORE + "\n")
    except OSError as e:
        print(f"[queue] warning: could not add {TASK_IGNORE} to {path}: {e}", file=sys.stderr)
    for key in os.listdir(q.root):
        if os.path.isfile(os.path.join(q.root, key, "state.json")):
            drop(note_paths(vault, key)["task"])


def run_tickets(q, ctx):
    done = {}   # KEY: its requeued stamp when this run took it; answered or retried since, it is taken again

    def pending(k):
        s = q.load(k) or {}
        return s.get("status") in ("queued", "running") and done.get(k) != s.get("requeued", "")
    try:
        while True:
            ctx.key = ""
            beat(ctx, ticket="", stage="", step="")
            checkpoint(ctx)
            todo = [k for k in q.order() if pending(k)]
            if not todo:
                break
            with held(os.path.join(q.root, "queue.lock")):   # owner commands change a ticket under it
                if not pending(todo[0]) or todo[0] not in q.order():
                    continue
                st = q.load(todo[0])
                ctx.key = st["key"]
                beat(ctx, ticket=st["key"], stage=st["stage"], step="")   # in hand (run_holds) before it is running
                st.update(status="running", vault=ctx.vault)
                q.save(st)
            done[st["key"]] = st.get("requeued", "")
            ctx.key = st["key"]
            try:
                process(ctx, st)
            except Interrupted as e:
                interrupted(ctx, st, e)
                if e.kind == "halt":
                    sync_vault(ctx, st["key"], int(time.time() * 1000))
                    raise
            except Infra as e:
                put_back(ctx, st, f"the run stopped: the upstream does not answer ({str(e)[:200]}); "
                                 "start the queue again to carry on")
                raise
            except KeyboardInterrupt:
                put_back(ctx, st, "the run was interrupted (Ctrl-C); start the queue again to carry on")
                raise
            except Exception as e:  # noqa: BLE001 - one ticket's failure must not stop the queue
                import traceback
                q.log(st["key"], "error", traceback.format_exc())
                block(ctx, st, f"{type(e).__name__}: {str(e)[:250]}")
            finally:   # a stop asked too late for this go is not for a later one
                drop(q.sentinel(st["key"]))
            sync_vault(ctx, st["key"], int(time.time() * 1000))   # the outcome, into the vault
    except Interrupted:   # halt: between tickets, or the ticket above went back to the queue
        try:
            os.remove(q.sentinel())
        except OSError:
            pass
        print("[queue] halted by the owner; 'harness queue run' carries on from the same stage", file=sys.stderr)
        return 0
    except Infra as e:
        print(f"[queue] stopped: the upstream does not answer ({str(e)[:300]}). The ticket keeps its place; "
              "check 'harness unlock' and 'harness usage', then run 'harness queue run' again.", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        if ctx.child is not None:
            stop(ctx.child)
        print("\n[queue] interrupted; 'harness queue run' carries on from the same stage", file=sys.stderr)
        return 130
    print_list(q)
    return 0


# --- the owner's commands --------------------------------------------------------------

def cmd_add(q, a):
    project = a.project or os.getcwd()
    r = subprocess.run(["git", "-C", project, "rev-parse", "--show-toplevel"], capture_output=True, **TEXT)
    if r.returncode != 0:
        print(f"[queue] {project} is not in a git repository (use --dir)", file=sys.stderr)
        return 1
    project = r.stdout.strip()
    if a.in_place and a.clone:
        print("[queue] --in-place and --clone: give one of them (in place is the default)", file=sys.stderr)
        return 1
    if a.review_plan and a.no_plan_approval:
        print("[queue] --review-plan and --no-plan-approval: give one of them (approval is the default)",
              file=sys.stderr)
        return 1
    in_place = not a.clone
    on = git(project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    # the folder on an in-place ticket's branch (the queue switched it): new work starts where that ticket's did
    queued = next((s for s in map(q.load, q.order()) if s and s.get("in_place") and s.get("project") == project
                   and s.get("start_sha") and on and branch_of(s) == on), None)
    base = a.base or (queued["base"] if queued else on) or git(project, "rev-parse", "HEAD").stdout.strip()
    if git(project, "rev-parse", "-q", "--verify", base + "^{commit}", check=False).returncode != 0:
        print(f"[queue] {base} is not a commit in {project}", file=sys.stderr)
        return 1
    stages, why = parse_stages(a.stages) if a.stages is not None else (list(ALL_STAGES), None)
    if why:
        print(f"[queue] {why}", file=sys.stderr)
        return 1
    if a.start and a.ref and a.start != a.ref:
        print("[queue] --ref is the old name of --start; give one of them", file=sys.stderr)
        return 1
    start = a.start or a.ref or ((queued["start_sha"] if queued else HERE) if in_place else "")
    if queued and not (a.start or a.ref):
        print(f"[queue] note: {project} is on {queued['key']}'s branch {on}; the work starts where "
              f"{queued['key']}'s did ({start[:12]}), on base {base}", file=sys.stderr)
    if start:
        sha = commit_of(project, start)
        if not sha:
            print(f"[queue] {start} is not a commit in {project} (git fetch it first?)", file=sys.stderr)
            return 1
        if git(project, "merge-base", base, sha, check=False).returncode != 0:
            print(f"[queue] {start} and {base} have no history in common", file=sys.stderr)
            return 1
        if start == HERE:   # the commit the project is at now (in place, its uncommitted files come along)
            changes = uncommitted(project)
            if not in_place and (changes["untracked"] or changes["changed"]):
                print(f"[queue] note: the clone starts at {project}'s HEAD ({sha[:12]}); its "
                      f"{len(changes['untracked']) + len(changes['changed'])} uncommitted file(s) are not in it "
                      "(working in place, the default, works on them)", file=sys.stderr)
            start = sha
    if a.branch and git(project, "check-ref-format", "--branch", a.branch, check=False).returncode != 0:
        print(f"[queue] not a branch name: {a.branch}", file=sys.stderr)
        return 1
    if a.note is not None and not a.note.strip():
        print("[queue] --note is empty", file=sys.stderr)
        return 1
    rc = 0
    with held(os.path.join(q.root, "queue.lock")):
        order = q.order()
        for raw in a.keys:
            key = raw.upper()
            if not KEY_RE.match(key) or len(key) > 40:
                print(f"[queue] not a ticket key: {raw}", file=sys.stderr)
                rc = 1
                continue
            if key in order or q.load(key):
                print(f"[queue] {key} is already in the queue ('harness queue remove {key}' first)", file=sys.stderr)
                rc = 1
                continue
            st = {"key": key, "project": project, "base": base, "status": "queued", "stage": "plan", "attempt": 0,
                  "feedback": "", "reason": "", "created": now_iso(), "stages": stages, "notes": []}
            st["stage"] = first_stage(st)
            st.update({k: v for k, v in (("start", start), ("branch", a.branch), ("in_place", in_place),
                                         ("plan_review", not a.no_plan_approval)) if v})
            if start:
                lost = lost_by(project, branch_of(st), commit_of(project, start_of(st)))
                if lost:
                    print(f"[queue] {key}: branch {branch_of(st)} has commits that {start_of(st)} does not "
                          f"({', '.join(n for n, _ in lost)}): the work would end over them. Start from it "
                          f"(--start {lost[0][0]}) or pick another --branch", file=sys.stderr)
                    rc = 1
                    continue
            if a.note:
                q.add_note(key, a.note)
            q.save(st)
            order.append(key)
            how = (f"in {project} itself" if in_place else f"in a clone of {project}") \
                + f", from {start_of(st)[:40]} on branch {branch_of(st)}"
            plan = "" if "plan" not in stages else "; the plan waits for approve" if not a.no_plan_approval \
                else "; implement starts after the plan without asking"
            print(f"[queue] {key}: queued ({how}; stages {','.join(stages)}{plan})")
        q.set_order(order)
    return rc


def print_list(q):
    keys = q.order()
    if not keys:
        print("the queue is empty (harness queue add KEY...)")
        return
    print(f"{'TICKET':<14} {'STATUS':<14} {'STAGE':<10} NOTE")
    for k in keys:
        st = q.load(k) or {}
        note = st.get("reason") or (f"attempt {st.get('attempt', 0) + 1}" if st.get("status") == "running" else "")
        print(f"{k:<14} {st.get('status', '?'):<14} {st.get('stage', '?'):<10} {note}")


def cmd_list(q, a):
    print_list(q)
    return 0


def cmd_remove(q, a):
    rc = 0
    with held(os.path.join(q.root, "queue.lock")):
        order = q.order()
        for raw in a.keys:
            key = raw.upper()
            if key not in order and not q.load(key):
                print(f"[queue] {key} is not in the queue", file=sys.stderr)
                continue
            st = q.load(key) or {}
            if st.get("status") == "running" and run_holds(q, key):
                print(f"[queue] {key} is running; stop it first (harness queue stop {key})", file=sys.stderr)
                rc = 1
                continue
            order = [k for k in order if k != key]
            err = rmtree(q.dir(key))
            if err:
                print(f"[queue] {key}: {err}; remove it by hand", file=sys.stderr)
            print(f"[queue] {key} removed (its branch, if any, stays in the project as refs/harness-queue/{key})")
        q.set_order(order)
    return rc


def requeue(q, key, want, stage=None, fresh=False, **extra):
    st = q.load(key.upper())
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return None
    if st["status"] not in want:
        print(f"[queue] {st['key']} is {st['status']}, not {' or '.join(want)}", file=sys.stderr)
        return None
    st.update(status="queued", attempt=0, feedback="", reason="", requeued=now_iso(), **extra)
    st.pop("stopped_from", None)
    try:
        os.remove(q.sentinel(st["key"]))   # a stop asked before is not for this go
    except OSError:
        pass
    if stage:
        st["stage"] = stage
    repo = os.path.join(q.dir(st["key"]), "repo")
    if fresh or st["stage"] == "plan" or not has_work(q, st):  # from the start, not earlier commits
        for k in ("sha", "review_sha", "must_fix", "warnings"):
            st.pop(k, None)
        if st["stage"] == "plan":
            st.pop("questions", None)
            if os.path.exists(plan_copy(q, st["key"])):
                os.remove(plan_copy(q, st["key"]))
        rmtree(repo)  # make_clone tries again, and says so
        st.pop("base_sha", None)
    q.save(st)
    return st


def has_work(q, st):
    """The ticket's work so far is there: its clone, or (in place) the project set up."""
    return bool(st.get("base_sha")) if st.get("in_place") else os.path.isdir(os.path.join(q.dir(st["key"]), "repo"))


def cmd_approve(q, a):
    st = requeue(q, a.key, ("plan-review",), plan_approved=True, plan_hold=False)
    if st:
        print(f"[queue] {st['key']}: plan approved; 'harness queue run' implements it")
    return 0 if st else 1


RETRYABLE = ("blocked", "needs-answers", "stopped", "done")


def cmd_retry(q, a):
    key = a.key.upper()
    st = q.load(key)
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return 1
    extra = {}
    stage = a.stage
    if stage:
        if stage not in stages_of(st):
            extra["stages"] = [s for s in ALL_STAGES if s in stages_of(st) or s == stage]
        if stage == "plan":
            extra["plan_approved"] = False
    elif st["status"] == "stopped" and st.get("stopped_from") == "plan-review":
        # held again for reading: a stop is no way round plan review
        st.update(status="plan-review", reason="plan ready to read; stopped and resumed", requeued=now_iso())
        st.pop("stopped_from")
        q.save(st)
        print(f"[queue] {key}: back to plan-review; harness queue approve {key} goes on")
        return 0
    elif st["status"] == "done" or st.get("stage") not in ALL_STAGES or not has_work(q, st):
        stage = first_stage(st)   # done, or its work is gone: from the start, on a fresh clone
        extra["fresh"] = True
    # also a ticket left running by a run that ended (or is stuck elsewhere), and a
    # queued one sent back to an earlier stage; under the lock the run takes tickets under
    with held(os.path.join(q.root, "queue.lock")):
        if run_holds(q, key):
            print(f"[queue] {key} is running; stop it first (harness queue stop {key})", file=sys.stderr)
            return 1
        st = requeue(q, key, RETRYABLE + ("running",) + (("queued",) if a.stage else ()), stage=stage, **extra)
    if st:
        print(f"[queue] {st['key']}: queued again at {st['stage']}")
    return 0 if st else 1


def cmd_stop(q, a):
    with held(os.path.join(q.root, "queue.lock")):   # the run takes a ticket under it: no stop is missed
        return stop_locked(q, a)


def stop_locked(q, a):
    key = a.key.upper()
    st = q.load(key)
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return 1
    if st["status"] == "stopped":
        print(f"[queue] {key} is already stopped")
        return 0
    if st["status"] not in ("queued", "needs-answers", "plan-review", "blocked", "running"):
        print(f"[queue] {key} is {st['status']}; there is nothing to stop", file=sys.stderr)
        return 1
    if st["status"] == "running" and run_holds(q, key):
        # The run holds a running ticket in memory and would write over a status
        # set here: it looks for this file, stops the agent and sets it itself.
        write_atomic(q.sentinel(key), now_iso() + "\n")
        print(f"[queue] {key}: asked the run to stop it; it stops within seconds "
              "(a run that does not: harness queue halt --force)")
        return 0
    drop(q.sentinel(key))   # asked of a run that never got to it
    st.update(status="stopped", reason="stopped by the owner",
              stopped_from="queued" if st["status"] == "running" else st["status"])
    q.save(st)
    print(f"[queue] {key}: stopped; harness queue retry {key} resumes it")
    return 0


def cmd_halt(q, a):
    if a.force:
        return end_run(q, a)
    if not run_going(q):
        stale = os.path.exists(q.sentinel())
        if stale:
            os.remove(q.sentinel())
        print("[queue] no queue run is going" + ("; removed an old halt request" if stale else ""))
        return 0
    write_atomic(q.sentinel(), now_iso() + "\n")
    print("[queue] asked the run to halt; its ticket goes back to the queue at the same stage")
    return 0


def end_run(q, a):
    """halt --force: a run that does not halt (stuck in a call that never comes
    back) is killed with its agent, and its ticket goes back to the queue at its
    stage (stopped, when the owner had asked that)."""
    r = runner(q)
    pid, agent = r.get("pid"), r.get("agent")
    if run_going(q):
        if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
            print("[queue] a run holds the queue but run.json does not say which process it is; "
                  "end the python process running ticket_queue.py run by hand", file=sys.stderr)
            return 1
        for victim in (agent, pid):
            if not isinstance(victim, int) or victim <= 0:
                continue
            if os.name == "nt":   # the whole tree: the run's agent, its docker client, bash
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(victim)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=60)
                continue
            with contextlib.suppress(OSError):
                (os.killpg if victim == agent else os.kill)(victim, signal.SIGKILL)
        end = time.time() + 20
        while run_going(q) and time.time() < end:
            time.sleep(0.2)
        if run_going(q):
            print(f"[queue] the run (process {pid}) still holds the queue; end it by hand", file=sys.stderr)
            return 1
        print(f"[queue] ended the run (process {pid})")
    else:
        print("[queue] no queue run is going")
    for path in (q.sentinel(), os.path.join(q.root, "run.json")):
        drop(path)
    with held(os.path.join(q.root, "queue.lock")):
        for key in q.order():
            st = q.load(key)
            if not st or st.get("status") != "running":
                continue
            stopped = os.path.exists(q.sentinel(key))
            drop(q.sentinel(key))
            why = "the run was ended by the owner (halt --force)"
            record(st, "stopped" if stopped else "interrupted", [why + "; the attempt does not count"])
            st.update(status="stopped" if stopped else "queued", reason=why,
                      **({"stopped_from": "queued"} if stopped else {}))
            q.save(st)
            q.log(key, f"{why}: {'stopped' if stopped else 'back in the queue'} at {st['stage']}")
            launch = r.get("launch") if r.get("ticket") == key else ""
            if launch and a.harness and not r.get("host"):   # its agent's container
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run([BASH, a.harness, "queue", "_stop_agents", workdir(q, st), launch],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=120, check=False)
            print(f"[queue] {key}: {'stopped' if stopped else 'back in the queue'} at {st['stage']}")
    return 0


def cmd_move(q, a):
    key = a.key.upper()
    with held(os.path.join(q.root, "queue.lock")):
        order = q.order()
        if key not in order:
            print(f"[queue] {key} is not in the queue", file=sys.stderr)
            return 1
        i = order.index(key)
        order.pop(i)
        j = {"up": max(0, i - 1), "down": min(len(order), i + 1), "top": 0, "bottom": len(order)}[a.where]
        order.insert(j, key)
        q.set_order(order)
    print(f"[queue] {key}: {j + 1} of {len(order)}")
    return 0


def cmd_note(q, a):
    key = a.key.upper()
    if not q.load(key):
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return 1
    if a.stdin:
        text = sys.stdin.buffer.read().decode("utf-8", "replace")
    elif a.text is not None:
        text = a.text
    else:
        tmp = os.path.join(q.dir(key), "note.edit.md")
        write_atomic(tmp, "")
        if edit(tmp):
            print("[queue] the editor failed; nothing saved", file=sys.stderr)
            return 1
        text = read(tmp)
    if not text.strip():
        print("[queue] no note given; nothing saved", file=sys.stderr)
        return 1
    q.add_note(key, text)
    with held(os.path.join(q.root, "queue.lock")):   # not while the run takes the ticket
        st = q.load(key)
        if st and st.get("status") != "running":   # a running ticket's run mirrors it within POLL seconds
            q.save(st)
    print(f"[queue] {key}: note saved; every stage from now on gets it")
    return 0


def edit(path):
    """Open path in the owner's editor; non-zero when that failed."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    # $EDITOR may carry arguments ("code -w"), so the shell splits it.
    return subprocess.call(["sh", "-c", editor + ' "$1"', "sh", path])


def runner(q):
    """run.json with "running": a beat within the last 30 s."""
    try:
        info = json.loads(read(os.path.join(q.root, "run.json")) or "null")
    except ValueError:
        info = None
    if not isinstance(info, dict):
        return {"running": False}
    beat_at = info.get("beat")
    info["running"] = isinstance(beat_at, (int, float)) and time.time() - beat_at < 30
    return info


def cmd_status(q, a):
    r = runner(q)
    if a.json:
        tickets = []
        for k in q.order():
            st = q.load(k)
            if isinstance(st, dict):
                st.setdefault("stages", list(ALL_STAGES))
                st.setdefault("notes", [])
                tickets.append(st)
        print(json.dumps({"runner": r, "tickets": tickets}, indent=2))
        return 0
    if r["running"]:
        print(f"run: going (pid {r.get('pid')}): {r.get('ticket') or 'between tickets'} {r.get('stage') or ''}".rstrip())
    else:
        print("run: not going")
    print_list(q)
    return 0


def cmd_answer(q, a):
    key = a.key.upper()
    st = q.load(key)
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return 1
    allowed = ("needs-answers", "plan-review", "blocked", "queued", "stopped")
    if st["status"] not in allowed:
        print(f"[queue] {key} is {st['status']}; answers are for a ticket that is {', '.join(allowed)}", file=sys.stderr)
        return 1
    p = note_paths(a.vault, key)
    _, _, questions = check_plan(read(p["plan"]))
    if a.stdin:
        text = sys.stdin.buffer.read().decode("utf-8", "replace")
        if not text.strip():
            print("[queue] no answers given; nothing saved", file=sys.stderr)
            return 1
    elif a.text:
        text = a.text
    else:
        tmpl = (f"# Answers for {key}\n\n<!-- Answer under each question; delete nothing else. Save and quit. -->\n\n"
                + "".join(f"## {qq}\n\n\n" for qq in questions))
        tmp = os.path.join(q.dir(key), "answers.edit.md")
        write_atomic(tmp, tmpl)
        if edit(tmp):
            print("[queue] the editor failed; nothing saved", file=sys.stderr)
            return 1
        text = read(tmp)
        if text.strip() == tmpl.strip():
            print("[queue] no answers written; nothing saved", file=sys.stderr)
            return 1
    stamp = f"\n<!-- {now_iso()} -->\n"
    old = read(p["answers"])
    write_atomic(p["answers"], (old.rstrip() + "\n" if old.strip() else "") + stamp + text.strip() + "\n")
    planned = "plan" in stages_of(st)   # without a plan stage the answers reach the next stage's prompt
    # Answers or feedback: the plan made with them comes back to be agreed before
    # anything is built (--go: the owner took the defaults and wants it to go on).
    st = requeue(q, key, allowed, stage="plan" if planned else None, plan_approved=False,
                 plan_hold=planned and not a.go)
    if st:
        print(f"[queue] {key}: answers saved to {p['answers']}; "
              + ("the plan is made again on the next run" if planned else f"the next run goes on at {st['stage']}"))
    return 0 if st else 1


def cmd_findings(q, a):
    any_ = False
    for k in q.order():
        text = read(note_paths(a.vault, k)["findings"]).strip()
        if text:
            any_ = True
            print(f"== {k} ({note_paths(a.vault, k)['findings']})\n{text}\n")
    if not any_:
        print("no findings yet")
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="ticket_queue.py")
    p.add_argument("--state", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add"); s.add_argument("keys", nargs="+"); s.add_argument("--project"); s.add_argument("--base")
    s.add_argument("--stages"); s.add_argument("--ref"); s.add_argument("--note"); s.add_argument("--start")
    s.add_argument("--branch"); s.add_argument("--in-place", action="store_true")   # the default
    s.add_argument("--clone", action="store_true")
    s.add_argument("--review-plan", action="store_true")   # the default
    s.add_argument("--no-plan-approval", action="store_true")
    sub.add_parser("list")
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("remove"); s.add_argument("keys", nargs="+")
    s = sub.add_parser("approve"); s.add_argument("key")
    s = sub.add_parser("retry"); s.add_argument("key"); s.add_argument("--stage", choices=ALL_STAGES)
    s = sub.add_parser("stop"); s.add_argument("key")
    s = sub.add_parser("halt"); s.add_argument("--force", action="store_true"); s.add_argument("--harness", default="")
    s = sub.add_parser("move"); s.add_argument("key"); s.add_argument("where", choices=("up", "down", "top", "bottom"))
    s = sub.add_parser("note"); s.add_argument("key"); s.add_argument("--text"); s.add_argument("--stdin", action="store_true")
    s = sub.add_parser("answer"); s.add_argument("key"); s.add_argument("--vault", required=True); s.add_argument("--text")
    s.add_argument("--go", action="store_true")
    s.add_argument("--stdin", action="store_true")
    s = sub.add_parser("findings"); s.add_argument("--vault", required=True)
    s = sub.add_parser("run")
    s.add_argument("--vault", required=True)
    s.add_argument("--proxy", required=True)
    s.add_argument("--harness", required=True)
    s.add_argument("--model", default="")
    s.add_argument("--default-model", default="")
    s.add_argument("--verify-model", default="")
    s.add_argument("--host", action="store_true")
    s.add_argument("--single-message", action="store_true")
    s.add_argument("--retries", type=int, default=1)
    s.add_argument("--push", type=int, default=1)
    s.add_argument("--remote", default="origin")
    s.add_argument("--plan-review", type=int, default=0)
    s.add_argument("--stage-timeout", type=int, default=STAGE_TIMEOUT)
    a = p.parse_args(argv[1:])
    q = Queue(a.state)
    return {"add": cmd_add, "list": cmd_list, "status": cmd_status, "remove": cmd_remove, "approve": cmd_approve,
            "retry": cmd_retry, "stop": cmd_stop, "halt": cmd_halt, "move": cmd_move, "note": cmd_note,
            "answer": cmd_answer, "findings": cmd_findings, "run": cmd_run}[a.cmd](q, a)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
