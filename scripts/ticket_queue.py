#!/usr/bin/env python3
"""The ticket queue behind `harness queue`: works through tickets one at a time,
plan -> implement -> review (-> one fix) -> push, with no one at the keyboard.

  ticket_queue.py --state DIR add KEY... [--project DIR] [--base REF] [--stages LIST]
                  [--start REF|here] [--branch NAME | --no-branch] [--repo-branch REL[=BRANCH][:TARGET]]...
                  [--no-plan-approval] [--take-defaults] [--note TEXT]
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
or host mode) in the project folder itself, on the ticket's branch. A plan waits for
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
the upstream does not answer (one probe request), the ticket goes back at its
stage, uncounted, and the run waits for the upstream (QUEUE_OUTAGE_WAIT, then it
stops); so does a launch that dies at once having started nothing (three in a
row block the ticket). An agent with nothing new for QUEUE_IDLE_MIN is stopped
and its work checked; a verifier that gives no verdict blocks, its work kept.

A ticket can run a subset of the stages (--stages; fix still only after a
review with Must fix items). Its work starts at --start (a branch, tag, commit,
or <remote>/<branch>, fetched first; "here": the project's HEAD when the
ticket is added, and in place its uncommitted files too; default: here in
place, the base in a clone) and ends on --branch (default KEY). An
existing branch is only ever moved forward: one with commits the start lacks is
refused. The change reviewed is from the merge-base of the base and the start,
so existing work at the start is part of it.

The project can hold repositories of its own (each with its own .git: a
top-level repo with AGENTS.md and the build, and api/, web/ beside it). The agent
works from the top and may change any of them; nothing stops it. Each is either
on a branch (the top by default; one inside it with --repo-branch, ending on
BRANCH, default KEY, made from TARGET, default the branch it is on) where the
agent commits and the push pushes, or left uncommitted (the top with
--no-branch; every repository inside it not named by --repo-branch): the agent
changes files and commits nothing, and the change is the worktree against a
snapshot taken when the work starts (refs/harness-queue/base/KEY in that repo).
Review, the verifier, fix, push and the result see every repository's part.

The owner's side runs in other processes while a run holds a ticket in memory,
so it never writes what the run owns: notes go to KEY/notes.json (mirrored into
state.json by every save), a stop or halt is a file (KEY/stop, DIR/halt) the run
looks for every POLL seconds, and the run keeps DIR/run.json beating.

run --parallel N works N tickets at a time: the run (holding run.lock and
run.json) starts N slots, each a run of its own process (run --slot I, beating
DIR/run-I.json) that takes tickets one at a time like a lone run. A slot takes
no ticket another slot holds, nor one whose project folder another slot's
ticket works in (in place, the folder is on one ticket's branch at a time), and
the vault is committed by one slot at a time (DIR/vault.lock).

harness never runs git inside the clone once the agent has had it: it fetches
branch KEY into the project (refs/harness-queue/KEY), diffs there and pushes
from there, without force. Nothing is ever written to the tracker.
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vault_ingest import InfraError, call_model, resolve_model, try_lock, yq  # noqa: E402

PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue_prompts")
STAGE_TIMEOUT = 3 * 3600   # one agent launch; a cooperative turn alone can take 15 minutes
VERIFY_TIMEOUT = 600
SYNC_TIMEOUT = 600         # harness queue _sync: a session into the vault, and its commit
PULL_TIMEOUT = 300         # harness queue _pull: the ticket from the tracker
PUSH_TIMEOUT = 600
# The upstream does not answer: the ticket goes back to the queue and the run waits,
# looking again after each of these pauses (the last repeating), for QUEUE_OUTAGE_WAIT.
OUTAGE_PAUSES = (120, 300, 600, 1800)
OUTAGE_WAIT = 6 * 3600
IDLE_MIN = 20              # QUEUE_IDLE_MIN: an agent with nothing new for this long is stopped
LAUNCH_MIN = 60            # a launch that fails within this, having started nothing, is not the ticket's fault
LAUNCH_TRIES = 3           # ... until it has failed this many times in a row: then the ticket is blocked
IDLE = "idle"              # the rc of a launch the idle watchdog stopped
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
MAX_QUESTIONS = 8
# A plan's criteria: the ones from the ticket gate the change (verify_change);
# their edge cases, tagged (edge of ACn), are the review's to check, and the
# ticket's tests later. Up to MAX_GATED of the first, MAX_CRITERIA in all.
MAX_GATED = 15
MAX_CRITERIA = 40
PLAN_SECTIONS = ("Goal", "Steps", "Acceptance criteria", "Questions", "Assumptions")
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
    """The upstream (or the proxy) does not answer: the ticket goes back to the
    queue at its stage and the run waits for the upstream (wait_out)."""


class LaunchFailed(Infra):
    """The agent launch ended within LAUNCH_MIN having started nothing (docker not
    up, the proxy restarting): not the ticket's fault, so no attempt is counted."""


class VerifierBroke(Exception):
    """The verifier gave no verdict twice (no reply, or not the JSON asked for)."""


class Busy(RuntimeError):
    """A lock (queue.lock, notes.lock) stayed held past the wait."""


# The run's own code: when harness upgrade changes it, the run ends between
# tickets with RESTART and harness queue run starts it again on the new code.
RESTART = 75
CODE_FILES = ("ticket_queue.py", "vault_ingest.py")


def code_sig():
    h = hashlib.sha256()
    for name in CODE_FILES:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()


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
                raise Busy(f"{path} is held by another process")
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
    return r["running"] and key in r["tickets"] and run_going(q)


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
            "task": os.path.join(d, f"{key}-task.md"), "result": os.path.join(d, f"{key}-result.md"),
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
    gated = len(gated_criteria(criteria))
    if gated > MAX_GATED:
        problems.append(f"{gated} acceptance criteria from the ticket: at most {MAX_GATED}, merge the small ones "
                        "(edge cases tagged (edge of ACn) do not count)" if gated < len(criteria) else
                        f"{gated} acceptance criteria: at most {MAX_GATED}, merge the small ones")
    if len(criteria) > MAX_CRITERIA:
        problems.append(f"{len(criteria)} acceptance criteria: at most {MAX_CRITERIA} in all, merge the small ones")
    return problems, criteria, questions


def plan_questions(secs):
    """The Questions section's bullets, less the ways of saying there are none."""
    return [q for q in bullets(secs.get("questions", ""))
            if not re.match(r"(?i)\W*(none|no (open |further )?questions|n/?a)\b[^?]*$", q)]


MARK = re.compile(r"(?i)(?:\[|\(|\*\*|`)\s*(non[-\s]?)?blocking\s*(?:\]|\)|\*\*|`)")


def question_mark(q):
    """'blocking', 'non-blocking' or None: the marker in brackets, parentheses,
    bold or backticks, wherever it is ("Q1 [blocking] ...", "[blocking] Q1: ...",
    "Q1 - (blocking) ...", "Q1: Which? [blocking] ...")."""
    m = MARK.search(q)
    return None if not m else "non-blocking" if m.group(1) else "blocking"


def plan_notes(text):
    """What is off in a plan that passed check_plan, for the log."""
    secs = sections(text)
    notes = [f"no '## {s}' section" for s in PLAN_SECTIONS
             if s.lower() not in secs and not (s == "Steps" and "approach" in secs)]
    questions = plan_questions(secs)
    if len(questions) > MAX_QUESTIONS:
        notes.append(f"{len(questions)} questions (asked for at most {MAX_QUESTIONS})")
    notes += [f"question not marked [blocking] or [non-blocking] (taken as "
              f"{'blocking' if blocking([q]) else 'non-blocking'}): {q[:120]}"
              for q in questions if not question_mark(q)]
    return notes


def blocking(questions):
    """The questions that hold the ticket: marked [blocking], or an unmarked
    question (every question has a default now, so that tells nothing)."""
    return [q for q in questions if question(q, 0)["blocking"]]


# The plan's items, for the owner's page and a later tester: its numbered steps,
# its GIVEN/WHEN/THEN criteria (AC1...) and its questions (Q1...) with their
# options and default. The plan stays markdown the model writes; this is read
# from it, so an older plan (an Approach, plain bullets) still gives items.
ITEM_ID = re.compile(r"^[\s*_`]*(AC|Q)[-\s]?(\d{1,3})\b[\s*_`]*[:.)\-\u2013\u2014]?[\s*_`]*", re.I)
GWT_WORD = re.compile(r"(?i)[*_`]+(given|when|then|and)\b[*_`]*|\b(given|when|then)[*_`]+")
GWT = re.compile(r"(?is)^\W*given\b[\s,:]*(.*?)[\s,;]*\bwhen\b[\s,:]*(.*?)[\s,;]*\bthen\b[\s,:]*(.*)$")
SOURCE = re.compile(r"[(\[]\s*((?:from the )?(?:ticket|edge(?:[- ]case)?|source|from|owner|answer)\b[^\n]*?)"
                    r"\s*[)\]]\s*\.?\s*$", re.I)
PATH_RE = re.compile(r"`([^`\s]+\.[A-Za-z0-9]{1,8}|[^`\s]*/[^`\s]*)`")


def source_tag(body):
    """The tag that ends a criterion, as a match (or None): the last (ticket: ...)
    or (edge of ACn), else the last other one; a GIVEN can hold its own "(from ...)"
    before the tag, and a ticket quote can hold brackets."""
    found = [m for m in (SOURCE.match(body, i) for i, ch in enumerate(body) if ch in "([") if m]
    main = [m for m in found if re.match(r"(?i)(?:from the )?(?:ticket|edge)", m.group(1))]
    return (main or found or [None])[-1]


def criterion_ids(criteria):
    """Each criterion's AC number: its own id, unless an earlier one has it or it
    has none; those take the numbers after the highest (all plain: 1, 2, 3...)."""
    own = []
    for t in criteria:
        m = ITEM_ID.match(t)
        own.append(int(m.group(2)) if m and m.group(1).upper() == "AC" else None)
    top, seen, out = max([x for x in own if x is not None] or [0]), set(), []
    for x in own:
        if x is None or x in seen:
            top += 1
            x = top
        seen.add(x)
        out.append(x)
    return out


def item_id(text, kind, n):
    """(id, the text without it): "AC3: GIVEN..." -> ("AC3", "GIVEN...")."""
    m = ITEM_ID.match(text)
    if m and m.group(1).upper() == kind:
        return f"{kind}{int(m.group(2))}", text[m.end():].strip()
    return f"{kind}{n}", text.strip()


def criterion(text, n):
    """One acceptance criterion as {id, text, given, when, then, source}; given,
    when and then are "" when it is not written as GIVEN ... WHEN ... THEN ..."""
    cid, body = item_id(text, "AC", n)
    m = source_tag(body)
    source = m.group(1).strip() if m else ""
    if m:
        body = body[:m.start()].rstrip()
    plain = GWT_WORD.sub(lambda w: w.group(1) or w.group(2), body)
    g = GWT.match(plain)
    parts = [x.strip().rstrip(",;").strip() for x in g.groups()] if g else ["", "", ""]
    return dict(id=cid, text=text, given=parts[0], when=parts[1], then=parts[2], source=source)


def question(text, n):
    """One question as {id, text, blocking, options, default}."""
    mark = question_mark(text)
    qid, body = item_id(MARK.sub(" ", text, count=1).strip(), "Q", n)
    body = re.sub(r"^[\s*_`:.\-\u2013\u2014]+", "", body)
    default = ""
    m = re.search(r"(?i)\s*\bdefault\s*:\s*", body)
    if m:
        default, body = body[m.end():].strip(), body[:m.start()].rstrip()
    options = []
    m = re.search(r"(?i)\s*\boptions?\s*:\s*", body)
    if m:
        opts, body = body[m.end():], body[:m.start()].rstrip()
        options = [o.strip().rstrip(";,.").strip() for o in
                   re.split(r"(?:^|[\s;,]+)\(?[a-hA-H]\)\s+", " " + opts) if o.strip().rstrip(";,.").strip()]
        if len(options) < 2:
            options = [o.strip() for o in re.split(r"\s*(?:;|\s\|\s)\s*", opts) if o.strip()]
    blocks = mark == "blocking" or (mark is None and "?" in body)
    return dict(id=qid, text=text, ask=body.strip(), blocking=blocks, options=options[:8], default=default)


def plan_items(text):
    """{steps, criteria, questions} of a plan (see criterion and question)."""
    secs = sections(text)
    steps = bullets(secs.get("steps") or secs.get("approach") or "")
    return {"steps": [{"n": i, "text": t, "files": PATH_RE.findall(t)} for i, t in enumerate(steps, 1)],
            "criteria": plan_criteria(bullets(secs.get("acceptance criteria", ""))),
            "questions": [question(x, i) for i, x in enumerate(plan_questions(secs), 1)]}


def plan_criteria(criteria):
    """The criteria parsed, each with its id from criterion_ids (one id each)."""
    return [dict(criterion(t, n), id=f"AC{n}") for t, n in zip(criteria, criterion_ids(criteria))]


def is_edge(c):
    """True for a criterion tagged (edge of ACn): an edge case of another one."""
    return c["source"].lower().startswith("edge")


def gated_criteria(criteria):
    """The criteria (bullets) a change is held to: all but the edge cases, each
    without its id. A plan with no tags (an older one) gives all of them as written."""
    out = []
    for i, text in enumerate(criteria, 1):
        c = criterion(text, i)
        if not is_edge(c):
            out.append(item_id(text, "AC", i)[1])
    return out


def criteria_by_id(criteria):
    """(lines "ACn: ...", [n, ...]) for a verifier, n its AC number (criterion_ids)."""
    nums = criterion_ids(criteria)
    return "\n".join(f"AC{n}: {item_id(t, 'AC', n)[1]}" for n, t in zip(nums, criteria)), nums


def shape_problems(text):
    """What is off in how a plan's criteria are written (GIVEN/WHEN/THEN, one
    line each), naming them. Asked for again on a retry; a note on the last try."""
    body = sections(text).get("acceptance criteria", "")
    loose = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith((" ", "\t"))
             and not re.match(r"^\s*(?:[-*]|\d+[.)])\s", l) and not re.match(r"^(?:#|.*:\s*$)", l.strip())]
    out = []
    if loose:
        out.append(f"a criterion is broken over lines (one line each): {loose[0][:120]}")
    bad = [c["id"] for c in plan_criteria(bullets(body)) if not c["given"]]
    if bad:
        out.append(f"{', '.join(bad[:12])}: not written as one line '- ACn: GIVEN ... WHEN ... THEN ...' "
                   "ending with (ticket: \"...\") or (edge of ACn)")
    return out


def accepted_changed(old_plan, answers, notes, criteria):
    """The criteria the owner accepted by id in the feedback on the previous plan
    (the round of its sig) that the new plan dropped or reworded, one problem each;
    an id the owner wrote about again after that round, or in the notes, is free."""
    sig = (old_plan or {}).get("sig")
    if not sig:
        return []
    head = f"Feedback on the plan (round of {sig}):"
    at = answers.rfind(head)
    if at < 0:
        return []
    end = answers.find("\n<!-- ", at)
    block, later = (answers[at:], "") if end < 0 else (answers[at:end], answers[end:])
    ids = [i for m in re.finditer(r"(?m)^- ((?:AC\d+, )*AC\d+): accepted\s*$", block)
           for i in m.group(1).split(", ")]
    free = set(re.findall(r"\bAC\d+\b", later + "\n" + (notes or "")))
    old = {c.get("id"): item_id(c.get("text", ""), "AC", 0)[1] for c in old_plan.get("criteria") or []}
    new = {c["id"]: item_id(c["text"], "AC", 0)[1] for c in plan_criteria(criteria)}
    out = []
    for i in dict.fromkeys(ids):
        if i in free or i not in old or norm(new.get(i, "")) == norm(old[i]):
            continue
        out.append(f"{i} was accepted by the owner: keep it word for word, same id: \"- {i}: {old[i][:300]}\". "
                   "If an answer this round means it must change, keep it and ask about it as a Question.")
    return out


def step_notes(text, repo):
    """A Step's backticked path that is not in the working directory and is not
    marked (new): for the log, never a failure (the path can be another repo's)."""
    notes = []
    for st_ in plan_items(text)["steps"]:
        if re.search(r"(?i)\(new\b", st_["text"]):
            continue
        for f in st_["files"]:
            f = re.sub(r"(::|:\d|#L\d).*$", "", f).strip("/")
            if f and ".." not in f.split("/") and not os.path.exists(os.path.join(repo, f)):
                notes.append(f"step {st_['n']} names `{f}`, which is not in the working directory")
    return notes[:10]


def check_review(text):
    if not text.strip():
        return ["the review file was not written"], []
    secs = sections(text)
    problems = [f"the review has no '## {s}' section" for s in REVIEW_SECTIONS if s.lower() not in secs]
    return problems, bullets(secs.get("must fix", ""))


# --- git (the project only; never inside the agent's clone after it started) -----

# git's output is UTF-8 (paths, messages); Windows' default cp1252 decoding raises on "Ł" or "Ё".
TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def run_quiet(cmd, timeout, cwd=None, env=None):
    """subprocess.run with no pipes: output goes to temporary files, so a process the
    command leaves behind (ssh, a credential helper, git's fsmonitor daemon, a docker
    client) cannot hold the call open, which on Windows a pipe does past any timeout.
    On a timeout the whole tree is ended and returncode is 124; 127 when it could not
    start. Never raises for the command's sake."""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        try:
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, **NEW_GROUP)
        except OSError as e:
            return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: {e}")
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(p, grace=5)
            rc = 124
        except BaseException:   # Ctrl-C: it is in its own session and did not get it
            stop(p, grace=5)
            raise
        out = []
        for f in (fo, fe):   # as text=True reads a pipe: utf-8, universal newlines
            f.seek(0)
            out.append(f.read().decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n"))
    if rc == 124:
        out[1] += f"\n(no answer in {timeout}s; stopped)"
    return subprocess.CompletedProcess(cmd, rc, out[0], out[1])


def git(cwd, *args, check=True, timeout=300, env=None):
    env = dict(os.environ, **{"GIT_TERMINAL_PROMPT": "0", **(env or {})})
    # no fsmonitor daemon started by the queue's git: it would outlive the call holding its files;
    # no signing: a key on a smart card would ask for its PIN with nobody there
    r = run_quiet(["git", "-C", cwd, "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", *args],
                  timeout, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:400]}")
    return r


# Network git (fetch, push): nobody is watching, so no prompt may hold it (a
# terminal, Git Credential Manager's window, ssh asking for a passphrase or about
# a host key), and a connection that stalls is given up on, not waited on for hours.
NET_OPTS = ("-c", "credential.interactive=false", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60")
SSH_BATCH = "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"


def net_env(project):
    """The environment of network git. An ssh the owner chose (GIT_SSH, GIT_SSH_COMMAND,
    core.sshCommand: plink, a key, the Windows agent) is kept."""
    env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    if not (os.environ.get("GIT_SSH") or os.environ.get("GIT_SSH_COMMAND")
            or git(project, "config", "core.sshCommand", check=False).stdout.strip()):
        env["GIT_SSH_COMMAND"] = SSH_BATCH
    return env


def net_git(project, *args, timeout=300):
    """git fetch or push in project, with NET_OPTS and net_env; never raises."""
    return git(project, *NET_OPTS, *args, check=False, timeout=timeout, env=net_env(project))


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
    r = run_quiet(["git", "clone", "-q", "--no-checkout", *clone_opts(container), st["project"], dest],
                  600, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
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
    st.update(start_sha=start, base_sha=base_sha, clone_mode="container" if container else "host")
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


def commit_of(project, ref, fetch=True):
    """The commit ref names now (a <remote>/<branch> is fetched first, unless not
    fetch); HERE is HEAD."""
    return resolve_ref(project, "HEAD" if ref == HERE else ref, fetch)


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


def tips(project, branch, fetch=True):
    """[(name, sha)]: the branch here and on each remote (fetched first, unless not
    fetch: as last fetched), where it exists."""
    out = []
    local = git(project, "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", check=False).stdout.strip()
    if local:
        out.append((branch, local))
    for remote in git(project, "remote", check=False).stdout.split():
        sha = resolve_ref(project, f"{remote}/{branch}", fetch)
        if sha:
            out.append((f"{remote}/{branch}", sha))
    return out


def lost_by(project, branch, sha, fetch=True):
    """The tips of branch that sha does not contain: ending the work on branch at
    sha would throw their commits away. [] for a new branch, or sha on top of it."""
    return [(name, tip) for name, tip in tips(project, branch, fetch)
            if git(project, "merge-base", "--is-ancestor", tip, sha, check=False).returncode != 0]


def own_branch(st, lost):
    """True when lost (lost_by the ticket's branch) is the project's branch alone,
    made by an earlier go of this ticket: ends_on keeps it and starts it again."""
    return bool(lost) and lost == [(branch_of(st), lost[0][1])] and bool(st.get("made_branch"))


def ends_on(q, st, start):
    """Raise unless the ticket's branch can end up at start without losing commits:
    an existing branch must be behind the start, so the work goes on from its tip.
    The project's own branch, left by an earlier go of this ticket, is kept at
    refs/harness-queue/prev/KEY and started again; a pushed one is never moved."""
    project, branch = st["project"], branch_of(st)
    lost = lost_by(project, branch, start)
    if own_branch(st, lost):
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


def resolve_ref(project, ref, fetch=True):
    """The commit ref names in project, or "". A remote-tracking ref (origin/X) is
    fetched from its remote first, best effort, so it is the work as it is now
    (not fetch: as last fetched)."""
    head, _, rest = ref.partition("/")
    if fetch and rest and head in git(project, "remote", check=False).stdout.split():
        net_git(project, "fetch", "-q", "--no-tags", head, f"+refs/heads/{rest}:refs/remotes/{head}/{rest}",
                timeout=120)
    r = git(project, "rev-parse", "-q", "--verify", ref + "^{commit}", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def status_entries(project, env=None):
    """[(XY, path)]: what git status shows in project, untracked files one by one."""
    raw = git(project, "status", "--porcelain=v1", "-z", "-uall", "--ignore-submodules=none",
              env=env).stdout.split("\0")
    out, i = [], 0
    while i < len(raw):
        e = raw[i]
        i += 1
        if len(e) < 4:
            continue
        out.append((e[:2], e[3:]))
        if e[0] in "RC":   # a rename's old path follows
            i += 1
    return out


def uncommitted(project):
    """{"untracked": [...], "changed": [...]}: what git status shows in project.
    A nested repository is one untracked "dir/" entry."""
    out = {"untracked": [], "changed": []}
    for xy, path in status_entries(project):
        out["untracked" if xy == "??" else "changed"].append(path)
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
    if paths is None and EXCLUDE_MARK not in text:
        return
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
    st.pop("baselines", None)   # set up again: what the uncommitted repositories hold is looked at again
    q.save(st)
    trouble = in_place_trouble(project, None)
    if trouble:
        raise RuntimeError(trouble)
    if st.get("no_branch"):   # the top stays as it is, on the owner's branch: its change is left uncommitted
        exclude_block(project, None)
        owner = uncommitted(project)
        write_atomic(os.path.join(q.dir(key), "owner.json"), json.dumps(owner))
        head = git(project, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).stdout.strip()
        if not head:
            raise RuntimeError(f"{project} has no commit yet; commit something there first")
        st.setdefault("orig_branch", "")
        enter_nested(q, st)
        st.update(start_sha=head, base_sha=head)
        q.save(st)
        return ""
    start, base_sha = start_and_base(st)
    if start_of(st) == HERE:   # frozen at the first go, as a clone's is at add: going again starts there too
        st["start"] = start
    ends_on(q, st, start)
    # Not the first entry: the project can be a linked worktree itself (a bare
    # repository with its worktrees beside it), and the main one is then another.
    here = os.path.normcase(os.path.realpath(project))
    for wt in git(project, "worktree", "list", "--porcelain", check=False).stdout.split("\n\n"):
        lines = wt.splitlines()
        if lines and f"branch refs/heads/{branch}" in lines \
                and os.path.normcase(os.path.realpath(lines[0][9:])) != here:
            raise RuntimeError(f"branch {branch} is checked out in another worktree ({lines[0][9:]})")
    exclude_block(project, None)   # a run that died mid-stage left it: its files would not show as uncommitted
    owner = uncommitted(project)
    write_atomic(os.path.join(q.dir(key), "owner.json"), json.dumps(owner))
    head = git(project, "rev-parse", "HEAD").stdout.strip()
    on = git(project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    # the branch the folder goes back to when the ticket stops for the owner: theirs,
    # not another queue ticket's (that one's own), not this ticket's
    orig = on
    for s in map(q.load, q.order()):
        if s and s.get("key") != key and s.get("in_place") and s.get("project") == project \
                and s.get("start_sha") and branch_of(s) == on:
            orig = s.get("orig_branch", "")
            break
    if orig == branch:
        orig = ""
    if orig or "orig_branch" not in st:
        st["orig_branch"] = orig
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
    enter_nested(q, st)
    st.update(start_sha=start, base_sha=base_sha)
    q.save(st)
    n = len(owner["untracked"]) + len(owner["changed"])
    return f"{n} uncommitted file(s) of yours kept out of the commits" if n else ""


def enter_nested(q, st):
    """Each repository inside the project given a branch (--repo-branch) put on it, as
    enter_in_place puts the top: an existing branch goes on from its tip, a new one
    starts at its target (the branch the repository was on when the ticket was added),
    the owner's uncommitted changes there coming along. st["nested"][REL]: its branch,
    start and base (the change there is since the merge-base of the target and the
    start), the branch it was on (park puts it back) and what was uncommitted there."""
    project, key = st["project"], st["key"]
    nested = st.setdefault("nested", {})
    for rel, want in sorted((st.get("repos") or {}).items()):
        d = repo_dir(st, rel)
        top = git(d, "rev-parse", "--show-toplevel", check=False).stdout.strip() if os.path.isdir(d) else ""
        if not top or os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(d)):
            raise RuntimeError(f"{rel} is not a repository inside {project} (it was given a branch: --repo-branch)")
        trouble = in_place_trouble(d, None)
        if trouble:
            raise RuntimeError(trouble)
        branch = want.get("branch") or branch_of(st)
        target = commit_of(d, want.get("base") or "HEAD")
        if not target:
            raise RuntimeError(f"{want.get('base')} is not a commit in {rel}")
        on = git(d, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
        old = git(d, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False).stdout.strip()
        start = old or target
        r = git(d, "merge-base", target, start, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"{rel}: branch {branch} and {want.get('base')} have no history in common")
        prev = nested.get(rel) or {}
        n = {"branch": branch, "start_sha": start, "base_sha": r.stdout.strip(),
             "orig_branch": prev.get("orig_branch", "") if on == branch else on,
             "made_branch": prev.get("made_branch", not old),
             "owner": prev.get("owner") if "owner" in prev else
             sorted(p for v in uncommitted(d).values() for p in v)}
        if on != branch:
            git(d, "switch", "-q", *([branch] if old else ["-c", branch, target]))
        for k in ("user.name", "user.email"):
            if not git(d, "config", k, check=False).stdout.strip():
                raise RuntimeError(f"git {k} is not set in {d}; the agent's commits need it (git config {k} ...)")
        nested[rel] = n
        q.save(st)
        q.log(key, f"{rel}: on branch {branch} from {start[:12]}; the change there is since {n['base_sha'][:12]}")


def nested_back(q, st):
    """Before a stage: each repository inside the project with a branch of the ticket's
    back on it (another ticket, or the owner, may have switched it); "" or why not."""
    key = st["key"]
    for rel, n in sorted((st.get("nested") or {}).items()):
        d = repo_dir(st, rel)
        if not os.path.isdir(d):
            return f"{rel} is gone from {st['project']}; put it back, then harness queue retry {key}"
        trouble = in_place_trouble(d, None)
        if trouble:
            return trouble
        on = git(d, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
        if on == n["branch"]:
            continue
        r = git(d, "switch", "-q", n["branch"], check=False)
        if r.returncode != 0:
            return (f"could not switch {rel} from {on or 'a detached HEAD'} to {n['branch']}: "
                    f"{(r.stderr or r.stdout).strip()[:300]}; git switch {n['branch']} there, then harness queue retry {key}")
        q.log(key, f"switched {rel} from {on or 'a detached HEAD'} back to {n['branch']}")
    return ""


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
    """In place, before a stage: the top on the ticket's branch (top_back_on_branch;
    with --no-branch it stays where it is) and each repository inside it with a branch
    of the ticket's on that (nested_back). "" when they are, else why not."""
    if st.get("no_branch"):
        return in_place_trouble(st["project"], None) or nested_back(q, st)
    return top_back_on_branch(q, st) or nested_back(q, st)


def top_back_on_branch(q, st):
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
    owners = bool(on) and on == st.get("orig_branch")   # the queue put it back there when the ticket stopped
    if not on or (on not in in_place_branches(q, project) and not owners):
        return f"{project} is on {on or 'a detached HEAD'}, not the ticket's branch {branch}; {fix}"
    exclude_block(project, None)
    owner = owner_files(q, st)
    mine = set(owner.get("untracked", [])) | set(owner.get("changed", []))
    now = uncommitted(project)
    new = sorted(p for p in now["untracked"] + now["changed"] if p not in mine)
    if new and owners:   # on the owner's own branch what is uncommitted is the owner's: it comes along, theirs
        owner = {k: sorted(set(owner.get(k, [])) | set(now[k])) for k in ("untracked", "changed")}
        write_atomic(os.path.join(q.dir(key), "owner.json"), json.dumps(owner))
        new = []
    if new:
        return (f"{project} is on {on}, not the ticket's branch {branch}, and has uncommitted changes that were "
                f"not there when {key} started ({', '.join(new[:10])}); commit, stash or remove them, or {fix}")
    r = git(project, "switch", "-q", branch, check=False)
    if r.returncode != 0:
        return f"could not switch {project} from {on} to {branch}: {(r.stderr or r.stdout).strip()[:300]}; {fix}"
    q.log(key, f"switched {project} from {on} back to {branch}")
    return ""


def park(q, st):
    """In place, the ticket stopped for the owner (a plan to read, questions, blocked,
    stopped, done): the folder back on the branch it was on before the ticket
    (orig_branch) when nothing is uncommitted that was not there then, so the owner
    finds it as they left it. Else it stays, and the ticket's reason says why.
    st["folder_branch_now"]: where the folder is."""
    project, branch, key = st["project"], branch_of(st), st["key"]
    if not os.path.isdir(project):
        return
    on = git(project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    orig = st.get("orig_branch", "")
    why = ""
    if on == branch and orig and orig != branch:
        exclude_block(project, None)
        owner = owner_files(q, st)
        mine = set(owner.get("untracked", [])) | set(owner.get("changed", []))
        now = uncommitted(project)
        new = sorted(p for p in now["untracked"] + now["changed"] if p not in mine)
        trouble = in_place_trouble(project, None)
        if trouble:
            why = trouble
        elif new:
            why = f"uncommitted changes the ticket's work left ({', '.join(new[:5])}); git switch {orig} after them"
        else:
            r = git(project, "switch", "-q", orig, check=False)
            if r.returncode == 0:
                q.log(key, f"switched {project} back to {orig}, the branch it was on before {key}")
                on = orig
            else:
                why = f"git switch {orig}: {(r.stderr or r.stdout).strip()[:200]}"
    st["folder_branch_now"] = on
    if why:
        q.log(key, f"{project} stays on {on}: {why}")
        st["reason"] = (st.get("reason", "") + "; " if st.get("reason") else "") + f"the folder stays on {on}: {why}"
    for rel, n in sorted((st.get("nested") or {}).items()):   # each repository with a branch, as the top
        d, orig = repo_dir(st, rel), n.get("orig_branch")
        if not orig or not os.path.isdir(d) or \
                git(d, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() != n["branch"]:
            continue
        new = sorted(p for v in uncommitted(d).values() for p in v if p not in set(n.get("owner") or []))
        why = in_place_trouble(d, None) or (f"uncommitted changes the ticket's work left ({', '.join(new[:5])}); "
                                             f"git switch {orig} there after them" if new else "")
        if not why:
            r = git(d, "switch", "-q", orig, check=False)
            if r.returncode == 0:
                q.log(key, f"switched {rel} back to {orig}, the branch it was on before {key}")
                continue
            why = f"git switch {orig}: {(r.stderr or r.stdout).strip()[:200]}"
        q.log(key, f"{rel} stays on {n['branch']}: {why}")
        st["reason"] = (st.get("reason", "") + "; " if st.get("reason") else "") + f"{rel} stays on {n['branch']}: {why}"
    with held(os.path.join(q.root, "queue.lock")):
        # the owner may have approved, answered or stopped it since it stopped for them: theirs stands
        cur = q.load(key) or {}
        if (cur.get("status"), cur.get("requeued")) != (st.get("status"), st.get("requeued")):
            st = dict(cur, folder_branch_now=on)
        q.save(st)


def put_away(q, st):
    """In place, the ticket stopped for the owner or the run ended (halt, Ctrl-C):
    the folder back on the owner's branch (park); the run that takes it again
    switches it back. Never raises: the folder is the owner's to sort out."""
    if not st.get("in_place"):
        return
    try:
        park(q, st)
    except Exception as e:  # noqa: BLE001
        q.log(st["key"], f"warning: the folder was not put back: {type(e).__name__}: {str(e)[:200]}")


SNAP_WHO = {"GIT_AUTHOR_NAME": "harness queue", "GIT_AUTHOR_EMAIL": "harness-queue@localhost",
            "GIT_COMMITTER_NAME": "harness queue", "GIT_COMMITTER_EMAIL": "harness-queue@localhost"}


def worktree_tree(q, st, repo):
    """The tree of repo's files as they are (uncommitted and untracked files too; not
    ignored ones, nor the repositories inside it: one with no commit would fail git
    add), written through an index of its own: the owner's is not touched."""
    index = os.path.join(q.dir(st["key"]), f"tree-{os.getpid()}-{threading.get_ident()}.index")
    real = git(repo, "rev-parse", "--git-path", "index").stdout.strip()
    real = real if os.path.isabs(real) else os.path.join(repo, real)
    who = dict(SNAP_WHO, GIT_INDEX_FILE=index)
    try:
        if os.path.exists(real):   # git's stat cache: only what changed is read again
            shutil.copyfile(real, index)
        # literal: a repository named l* would take the owner's untracked lx/ with it
        specs = [".", *(f":(exclude,literal){r}/" for r in untracked_repos(repo))]
        if sum(len(x) + 1 for x in specs) > SNAP_ARGS_MAX:   # Windows: 32k characters on one command line
            with open(index + ".specs", "w", encoding="utf-8", newline="") as f:
                f.write("\0".join(specs))
            specs = ["--pathspec-from-file=" + index + ".specs", "--pathspec-file-nul"]
        else:
            specs = ["--", *specs]
        git(repo, "-c", "advice.addEmbeddedRepo=false", "add", "-A", *specs, env=who, timeout=600)
        return git(repo, "write-tree", env=who).stdout.strip()
    finally:
        drop(index)
        drop(index + ".specs")


def snapshot(q, st, stage):
    """In place, before each stage: the project's whole tree as it is (worktree_tree)
    as a commit at refs/harness-queue/snap/KEY, each one on the one before. If an
    agent throws away work: git checkout <snap> -- <path> takes it back."""
    project, key = st["project"], st["key"]
    ref = f"refs/harness-queue/snap/{key}"
    try:
        tree = worktree_tree(q, st, project)
        prev = git(project, "rev-parse", "-q", "--verify", ref, check=False).stdout.strip()
        sha = git(project, "commit-tree", tree, *(["-p", prev] if prev else []), "-m",
                  f"{key}: the project before {stage}", env=SNAP_WHO).stdout.strip()
        git(project, "update-ref", ref, sha)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        q.log(key, f"warning: no snapshot of the project before {stage}: {e}")


SNAP_ARGS_MAX = 8000


def untracked_repos(project):
    """The repositories inside the project that git add -A would meet (each an
    untracked "dir/" in git ls-files, at any depth): relative paths."""
    raw = git(project, "ls-files", "-z", "-o", "--exclude-standard", timeout=600).stdout.split("\0")
    return [e[:-1] for e in raw if e.endswith("/")]


# Folders of tools' own checkouts (a virtualenv's pip -e git+, terraform's
# modules): a build step the agent runs moves their HEAD, and they are not the owner's
NESTED_SKIP = ("node_modules", ".venv", "venv", ".tox", ".terraform")


def nested_repos(project, skip=()):
    """The repositories inside the project (their own .git): 3 levels down, ignored
    ones too, and any deeper one git add -A would meet; not the project's own
    worktrees, nor one that is or holds a folder in skip (the vault, the queue's
    state), nor one inside a tool's folder (NESTED_SKIP), nor a submodule. Relative paths, sorted."""
    found = {r for r in untracked_repos(project) if not set(r.split("/")) & set(NESTED_SKIP)}
    for root, dirs, files in os.walk(project):
        if root != project and (".git" in dirs or ".git" in files):
            dirs[:] = []
            found.add(os.path.relpath(root, project).replace(os.sep, "/"))
            continue
        depth = os.path.relpath(root, project).count(os.sep)
        dirs[:] = [] if depth >= 2 else [d for d in dirs if d != ".git" and d not in NESTED_SKIP]

    # submodules (gitlinks in the index, or named in .gitmodules) are the project's own
    staged = git(project, "ls-files", "-s", "-z", check=False).stdout.split("\0")
    subs = {e.split("\t", 1)[1] for e in staged if e.startswith("160000 ") and "\t" in e}
    named = git(project, "config", "-f", os.path.join(project, ".gitmodules"), "--get-regexp",
                r"^submodule\..*\.path$", check=False).stdout.splitlines()
    subs |= {l.split(" ", 1)[1].strip("/") for l in named if " " in l}
    found -= subs

    def rev_parse(d, what):   # no --path-format (git 2.31): relative to d, as git prints it
        out = git(d, "rev-parse", what, check=False).stdout.strip()
        return os.path.realpath(os.path.join(d, out)) if out else ""
    mine = rev_parse(project, "--git-common-dir")
    skip = [os.path.realpath(s) for s in skip if s]
    out = []
    for rel in sorted(found):
        d = os.path.realpath(os.path.join(project, rel))
        if any(s == d or s.startswith(d + os.sep) for s in skip):
            continue
        # a .git that is not a repository's (git there is the project's), or the project's own worktree;
        # one git will not read (another owner's) stays, said so when it is looked at
        top = rev_parse(d, "--show-toplevel")
        if (top and top != d) or rev_parse(d, "--git-common-dir") == mine:
            continue
        out.append(rel)
    return out


def repo_dir(st, rel):
    """A repository of the ticket's: "." is the project (the top), else one inside it."""
    return st["project"] if rel == "." else os.path.join(st["project"], rel)


def uncommitted_repos(st, skip=()):
    """The repositories whose part of the change is left uncommitted: the top ("."),
    with --no-branch, and each one inside the project not given a branch (--repo-branch)."""
    named = set(st.get("repos") or {})
    return (["."] if st.get("no_branch") else []) + [r for r in nested_repos(st["project"], skip) if r not in named]


def take_baselines(q, st, stage, skip=()):
    """Before the first stage after the plan: what each uncommitted repository holds
    (uncommitted_repos), as a commit at refs/harness-queue/base/KEY there. Its part of
    the change is its files against that. Before implement: its files as they are (the
    owner's uncommitted work is not the ticket's); a ticket with no implement (review
    first): its HEAD, so what is uncommitted there is the change. Kept until the work
    is set up again (enter_in_place)."""
    if stage == "plan" or isinstance(st.get("baselines"), dict):
        return
    key, out = st["key"], {}
    try:
        repos = uncommitted_repos(st, skip)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        q.log(key, f"warning: repositories inside the project not looked for before {stage}: {e}")
        repos = ["."] if st.get("no_branch") else []
    for rel in repos:
        d = repo_dir(st, rel)
        try:
            head = git(d, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).stdout.strip()
            if stage == "implement":
                tree = worktree_tree(q, st, d)
            elif head:
                tree = git(d, "rev-parse", f"{head}^{{tree}}").stdout.strip()
            else:   # no commit yet: everything there is the change
                tree = git(d, "hash-object", "-t", "tree", "-w", os.devnull).stdout.strip()
            sha = git(d, "commit-tree", tree, "-m", f"{key}: {rel} before the ticket's work",
                      env=SNAP_WHO).stdout.strip()
            git(d, "update-ref", f"refs/harness-queue/base/{key}", sha)
            out[rel] = {"snap": sha, "head": head}
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            q.log(key, f"warning: {rel} not looked at before {stage}: {e}; a change there is not part of the ticket's")
    st["baselines"] = out
    q.save(st)
    if out:
        q.log(key, f"left uncommitted (the change there is the files against how they are now): {', '.join(out)}")


def repo_diff(repo, rel, a, b):
    """(diff, files): a..b in the repository, its paths as the project's (under rel/)."""
    pre = "" if rel == "." else rel.strip("/") + "/"
    diff = git(repo, "diff", "--no-color", "--no-ext-diff", f"--src-prefix=a/{pre}", f"--dst-prefix=b/{pre}",
               a, b).stdout
    files = [pre + f for f in git(repo, "diff", "--name-only", "--no-ext-diff", a, b).stdout.splitlines() if f]
    return diff, files


def ticket_change(q, st):
    """All the ticket's work, in every repository: (sha, diff, files, commits, parts),
    or None when there is none. sha: the top's branch tip (None: the top has no branch,
    or no commits of its own); parts: one dict per repository with a part, its "repo",
    "kind" (branch or uncommitted), "branch", "sha", "files" and "commits"."""
    parts, diffs = [], []
    top = None if st.get("no_branch") else fetch_branch(q, st)
    if top:
        parts.append({"repo": ".", "kind": "branch", "branch": branch_of(st), "sha": top[0], "files": top[2],
                      "commits": top[3]})
        diffs.append(top[1])
    for rel, n in sorted((st.get("nested") or {}).items()):
        d = repo_dir(st, rel)
        sha = git(d, "rev-parse", "-q", "--verify", f"refs/heads/{n['branch']}", check=False).stdout.strip()
        commits = int(git(d, "rev-list", "--count", f"{n['base_sha']}..{sha}", check=False).stdout.strip() or 0) \
            if sha and n.get("base_sha") else 0
        if commits:
            diff, files = repo_diff(d, rel, n["base_sha"], sha)
            parts.append({"repo": rel, "kind": "branch", "branch": n["branch"], "sha": sha, "files": files,
                          "commits": commits})
            diffs.append(diff)
    for rel, b in sorted((st.get("baselines") or {}).items()):
        d = repo_dir(st, rel)
        if not os.path.isdir(d):
            continue
        diff, files = repo_diff(d, rel, b["snap"], worktree_tree(q, st, d))
        if files:
            parts.append({"repo": rel, "kind": "uncommitted", "branch": "", "sha": "", "files": files, "commits": 0})
            diffs.append(diff)
    if not parts:
        return None
    return (top[0] if top else None, "".join(diffs), [f for p in parts for f in p["files"]],
            sum(p["commits"] for p in parts), parts)


def no_change(st):
    """What a ticket with no change (ticket_change None) lacks, for its problem."""
    if not st.get("no_branch") and not st.get("nested") and not st.get("baselines"):
        return f"no commits on branch {branch_of(st)}: commit your change on that branch (git add, git commit)"
    ways = [] if st.get("no_branch") else [f"no commits on branch {branch_of(st)} in the project's top"]
    ways += [f"no commits on branch {n['branch']} in {rel}" for rel, n in sorted((st.get("nested") or {}).items())]
    if st.get("baselines"):
        ways.append(f"no file changed in {', '.join(sorted(st['baselines']))} (left uncommitted)")
    return "the ticket changed nothing: " + "; ".join(ways or ["no repository to look at"])


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


def added_gitlinks(project, since, sha):
    """Repositories committed as bare pointers (gitlinks) since since, added or in a
    tracked file's place: not the ones .gitmodules at sha names (submodules, the
    owner's or the ticket's)."""
    raw = git(project, "diff", "--raw", "-z", "--no-renames", "--ignore-submodules=none", "--diff-filter=AT", since,
              sha, check=False).stdout.split("\0")   # -z: a path as it is, not quoted
    links = [raw[i + 1] for i in range(0, len(raw) - 1, 2) if raw[i].startswith(":") and raw[i].split()[1] == "160000"]
    if not links:
        return []
    named = git(project, "config", "--blob", f"{sha}:.gitmodules", "--get-regexp", r"^submodule\..*\.path$",
                check=False).stdout.splitlines()
    named = {l.split(" ", 1)[1] for l in named if " " in l}
    return [f for f in links if f not in named]


def check_change(st, change, plan_text):
    """Hard gates on the ticket's change (ticket_change), and the warnings that are not gates."""
    if change is None:
        return [no_change(st)], []
    sha, diff, files = change[:3]
    problems, warnings = [], []
    if not diff.strip():
        problems.append("the commits change nothing (the diff is empty)")
    since = st.get("start_sha") or st.get("base_sha")
    if st.get("project") and since and sha:   # a repository made inside it, added by git add -A or .
        for f in added_gitlinks(st["project"], since, sha)[:10]:
            problems.append(f"{f}: a repository inside the project is committed as a bare pointer (a gitlink), which "
                            f"would be pushed without its commits: git rm --cached {f}, and leave it out of commits")
    for f in files:
        if f.startswith(".github/"):
            problems.append(f"{f}: changes under .github/ (CI) are not made by the queue; leave CI to the owner")
        if SECRET_PATH.search(f):
            problems.append(f"{f}: a secret or key file must not be committed")
    notes = tuple(f"{st['key']}-{k}.md".lower() for k in ("plan", "review", "answers", "findings", "task", "result"))
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
            reply = wait_call(ctx, call_model, ctx.proxy, ctx.verify_model, messages, timeout, deadline=timeout + 60)
            if getattr(ctx, "outage", None):
                ctx.outage.clear()   # it answers: an outage the run waited out is over
            return reply
        except InfraError as e:
            if n == len(INFRA_RETRY_PAUSES) or not transient(e):
                raise
            if getattr(ctx, "key", ""):
                ctx.q.log(ctx.key, f"the verifier request failed ({str(e)[:120]}); trying again in "
                                   f"{INFRA_RETRY_PAUSES[n]}s")
            step(ctx, f"the verifier request failed; trying again in {INFRA_RETRY_PAUSES[n]}s")
            pause(ctx, INFRA_RETRY_PAUSES[n])


def ask_verifier(ctx, prompt, key, stage):
    """The verifier's JSON verdict; two tries with the whole prompt each time
    (the upstream reads only the last message). Every reply is kept for review.
    No verdict is VerifierBroke, never a failed attempt: the agent's work was not judged."""
    ctx.q.log(key, f"asking the verifier ({ctx.verify_model}) about the {stage}")
    for n in range(2):
        try:
            reply = model_call(ctx, [{"role": "user", "content": prompt}], VERIFY_TIMEOUT)
        except InfraError as e:
            probe(ctx)  # raises Infra when the upstream is down; else this request was the problem
            raise VerifierBroke(f"the verifier request failed: {str(e)[:200]}") from None
        path = os.path.join(ctx.q.dir(key), f"verify-{stage}-{int(time.time())}-{n}.txt")
        write_atomic(path, reply)
        obj = json_object(reply)
        if isinstance(obj, dict) and isinstance(obj.get("criteria"), list):
            return obj
        ctx.q.log(key, f"the verifier's reply was not the JSON asked for ({path})")
    raise VerifierBroke(f"the verifier's reply was not the JSON asked for (2 tries; kept in {ctx.q.dir(key)})")


def verdict(ctx, st, stage, prompt_for):
    """ask_verifier on prompt_for(cap); with no verdict, once more with a quarter of
    the diff or ticket (a long prompt is what a free model most often chokes on).
    VerifierBroke after that: process() blocks the ticket with its work kept."""
    try:
        return ask_verifier(ctx, prompt_for(DIFF_CAP_VERIFY), st["key"], stage)
    except VerifierBroke as e:
        ctx.q.log(st["key"], f"{e}; asking once more with a shorter prompt")
        return ask_verifier(ctx, prompt_for(DIFF_CAP_VERIFY // 4), st["key"], stage)


REMOVAL = re.compile(r"(?i)\b(remov|delet|drop|strip|get rid|no longer|stop|deprecat|eliminat)")


def diff_sides(diff):
    """(added, removed) lines of a diff, each normalized; context lines are in neither."""
    lines = diff.replace("\r\n", "\n").splitlines()
    added = [l for l in lines if l.startswith("+") and not l.startswith("+++ ")]
    removed = [l for l in lines if l.startswith("-") and not l.startswith("--- ")]
    return norm("\n".join(added), diff=True), norm("\n".join(removed), diff=True)


def judge(obj, criteria, source, quote_field, what, notes="", soft=None, nums=None):
    """Problems with a verdict: every criterion answered, met (for a change),
    and every quote found in <source>. For a diff a quote must be from an added
    line (a removed one when the criterion is about removing; never an unchanged
    one); a reviewer's "(review)" item may instead quote <notes>, the commit
    messages saying why it is wrong. A ticket quote the verifier got slightly
    wrong goes to <soft> instead (a list, logged): one found quote is enough
    to show the plan is about the ticket. <nums> are the numbers the verifier
    was told for the criteria (their AC numbers), 1, 2, 3... by default."""
    soft = [] if soft is None else soft
    problems = []
    if what == "diff":
        added, removed = diff_sides(source)
    else:
        src = norm(source)
    got = {}
    for c in obj.get("criteria", []):
        if isinstance(c, dict):
            try:
                got[int(re.sub(r"(?i)^\s*AC", "", str(c.get("n"))))] = c
            except (TypeError, ValueError):
                pass
    found = 0
    for i, crit in zip(nums or range(1, len(criteria) + 1), criteria):
        c = got.get(i)
        if c is None:
            problems.append(f"criterion {i} was not checked: {crit[:100]}")
            continue
        if c.get("met") is False or c.get("ok") is False:
            problems.append(f"criterion {i} failed the check: {crit[:100]} ({str(c.get('why', ''))[:200]})")
            continue
        if what == "diff" and not (c.get("met") is True or str(c.get("met")).lower() in ("true", "yes")):
            problems.append(f"criterion {i}: the verifier did not say it is met (\"met\": "
                            f"{json.dumps(c.get('met'))[:40]}): {crit[:100]}")
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


# agents/live-run.sh says which opencode session the launch is: the session a stage
# that was cut off goes on in (harness queue continue; an outage, Ctrl-C or a halt)
SESSION_LINE = re.compile(r"^\[harness\] session: (ses_\w+)", re.M)
ERROR_LINE = re.compile(r"^\[harness\] opencode error: (.*)$", re.M)   # agents/live-run.sh: what ended the session
RESUMABLE = ("plan", "implement", "review", "fix")   # the stages an agent works in


def session_in(text):
    """The last opencode session a launch's output names, or ""."""
    found = SESSION_LINE.findall(text)
    return found[-1] if found else ""


def resuming(ctx, st, stage):
    """The session stage goes on in when it is to go on (st["resume"]), or "": none
    known, or one of the other mode's (a container agent's sessions are not where a
    host one looks, nor the other way round)."""
    r, s = st.get("resume") or {}, st.get("session") or {}
    if r.get("stage") != stage or s.get("stage") != stage or not s.get("id"):
        return ""
    return s["id"] if bool(s.get("container")) == (not (ctx.host or ctx.single)) else ""


def note_session(ctx, st, out, since):
    """st["session"], the opencode session the launch (started at since, ms) names, when it names
    one (kept: the stage, attempt and mode, and when the session started: a note
    written before it was cut off counts). True when it does."""
    got, before = session_in(read(out)), st.get("session") or {}
    if not got:
        return False
    same = before.get("id") == got and before.get("stage") == st["stage"]
    st["session"] = {"stage": st["stage"], "attempt": st.get("attempt", 0), "id": got,
                     "container": not (ctx.host or ctx.single),
                     "started": before["started"] if same and before.get("started") else since / 1000}
    if same and before.get("crashed"):
        st["session"]["crashed"] = before["crashed"]
    return True


def go_on_later(st, why):
    """This attempt's agent was cut off (an outage, Ctrl-C, a halt): the next launch
    of the stage goes on in its session (or, with none known, a new agent is told to
    finish its work) instead of starting the stage over."""
    s = st.get("session") or {}
    if st.get("stage") in RESUMABLE and s.get("stage") == st["stage"] and s.get("attempt") == st.get("attempt", 0):
        st["resume"] = {"stage": st["stage"], "why": why, "message": ""}


def crash_of(st, rc):
    """Why this attempt's agent stopped before it finished, when it stopped on an
    error (opencode's: a compaction that went wrong, say) or its launch failed;
    else ""."""
    kept = st.get("verify_only") or {}
    err = ERROR_LINE.findall(read(kept["out"])) if kept.get("out") else []
    if err:
        return f"its agent stopped on an error: {err[-1][:300]}"
    return f"its agent's launch exited with status {rc}" if rc not in (0, None, IDLE) else ""


def go_on_after_crash(ctx, st, why):
    """The agent stopped (crash_of): True when the stage goes on in its session
    instead of failing the attempt. Once per attempt, which is not counted; a
    second stop fails it as usual."""
    s = st.get("session") or {}
    if st.get("stage") not in RESUMABLE or s.get("stage") != st["stage"] \
            or s.get("attempt") != st.get("attempt", 0) or s.get("crashed") or not s.get("id"):
        return False
    s["crashed"] = True
    st.pop("verify_only", None)
    st["resume"] = {"stage": st["stage"], "why": why, "message": ""}
    ctx.q.log(st["key"], f"{st['stage']}: {why}; going on in its session {s['id']}")
    record(st, "crashed", [why])
    ctx.q.save(st)
    return True


CARRY_ON = ("\n\n<<<BEGIN_GOING_ON (an earlier agent on this stage was cut off before it finished: {why})>>>\n"
            "Its work so far is in your working directory. Check what is already done (git status, git log, the "
            "files the stage writes) and finish it instead of starting over.\n{message}<<<END_GOING_ON>>>\n")


def run_agent(ctx, st, stage, prompt):
    """One headless agent launch in the clone; its output goes to DIR/<stage>-N.out.
    A stage to go on (st["resume"]) goes on in its agent's session, told so with the
    owner's message (the full prompt is in the task file); with no session to go on
    in, a new agent gets the prompt and is told to finish the work there."""
    key = st["key"]
    model = ctx.model if "/" in ctx.model else "harness/" + ctx.model
    sid = resuming(ctx, st, stage)
    resume = st.pop("resume", None)
    resume = resume if isinstance(resume, dict) and resume.get("stage") == stage else None
    cmd = [BASH, ctx.harness]
    if ctx.host or ctx.single:   # --single-message is a host-mode launch flag
        cmd.append("host")
    if ctx.single:
        cmd.append("--single-message")
    # The whole prompt is also in the vault while the stage runs: a no-tools model
    # loses the prompt after its first tool call, and the proxy points it there.
    task = note_paths(ctx.vault, key)["task"]
    if resume and not sid:
        msg = (resume.get("message") or "").strip()
        prompt += CARRY_ON.format(why=resume.get("why") or "it was stopped",
                                  message=f"The owner's message (it wins over everything above):\n{msg}\n" if msg else "")
    arg = prompt
    if sid:
        arg = render("continue", KEY=key, STAGE=stage, TASK_FILE=agent_path(ctx, task),
                     WHY=resume.get("why") or "it was stopped",
                     MESSAGE=(resume.get("message") or "").strip() or "(none: carry on)")
        ctx.q.log(key, f"{stage}: going on in the agent's session {sid}")
    elif WINDOWS and len(prompt) > WIN_PROMPT_MAX:
        arg = (f"You are working on ticket {key}, stage {stage}, in {'the project' if st.get('in_place') else 'a clone of the project'} "
               f"on branch {branch_of(st)}.\n\n"
               f"Your full instructions for the {stage} stage of ticket {key} are in {agent_path(ctx, task)}. "
               f"Read it first and follow it exactly.\n")
    elif len(prompt.encode()) > PROMPT_MAX_BYTES:
        raise RuntimeError(f"the {stage} prompt is {len(prompt.encode())} bytes, over one command-line argument's limit")
    cmd += ["-p", "--yolo", "--ticket", key, "-m", model] + (["--session", sid] if sid else []) + [arg]
    out = os.path.join(ctx.q.dir(key), f"{stage}-{st['attempt']}.out")
    if sid and os.path.exists(out):   # what the agent did before it was cut off stays readable
        with contextlib.suppress(OSError):
            os.replace(out, out + ".before")
    since = int(time.time() * 1000)
    if not sid:   # a new agent: its session, once it says, is the one a cut-off stage goes on in
        st.pop("session", None)
    write_atomic(task, prompt)
    if st.get("in_place"):
        exclude_block(st["project"], None)   # the snapshot takes the owner's files too
        snapshot(ctx.q, st, stage)
        exclude_block(st["project"], owner_files(ctx.q, st).get("untracked"))
        # when the stage ran, for the run viewer: in place its session's folder is the project's
        st["launches"] = (st.get("launches") or [])[-(HISTORY_CAP - 1):] + [[since, 0]]
        ctx.q.save(st)
    ended = gone = False
    try:
        rc = launch(ctx, st, cmd, out, since, task_card(ctx, st, stage))
        ended = True
    except LaunchFailed:
        if resume:   # the session would not go on (gone, say): the next launch is a new agent, still told
            st.pop("session", None)
            st["resume"] = resume
            gone = True
        raise
    finally:   # gone before the vault is committed: it is the prompt, not a note
        drop(task)
        if not gone:
            note_session(ctx, st, out, since)   # the plain opencode run says it only at its end
        ctx.q.save(st)
        if st.get("in_place"):
            st["launches"][-1][1] = int(time.time() * 1000)
            ctx.q.save(st)
            with contextlib.suppress(RuntimeError, OSError, subprocess.SubprocessError):
                exclude_block(st["project"], None)
    # The agent's part of the stage is over: saved at once, so a run that dies from
    # here on (or an outage, a halt, a verifier with no verdict) goes on from its
    # result instead of launching it again. fail/advance/block let it go.
    st["verify_only"] = {"stage": stage, "rc": rc, "out": out, "started": since / 1000}
    ctx.q.save(st)
    sync_vault(ctx, key, since)
    return rc, out


def kept_result(st):
    """The agent of the ticket's stage ended and its result is kept (verify_only):
    going on checks that result rather than launching the agent again."""
    kept = st.get("verify_only")
    return isinstance(kept, dict) and kept.get("stage") == st.get("stage") and bool(kept.get("out"))


def agent_or_kept(ctx, st, stage, prompt):
    """(rc, out, started): run_agent on prompt(), or the result its agent left when
    that is kept (verify_only: the verifier gave no verdict, the run died, the
    upstream went away after the agent ended); the agent is not launched again."""
    kept = st.get("verify_only")
    if isinstance(kept, dict) and kept.get("stage") == stage and kept.get("out"):
        ctx.q.log(st["key"], f"{stage}: the agent's result from before is kept; checking it without a new launch")
        st.pop("resume", None)
        return kept.get("rc", 0), kept["out"], kept.get("started", 0)
    st.pop("verify_only", None)
    # going on in a session: the notes it wrote before it was cut off count
    started = (st["session"].get("started") or time.time()) if resuming(ctx, st, stage) else time.time()
    rc, out = run_agent(ctx, st, stage, prompt())
    return rc, out, started


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


def launch(ctx, st, cmd, out, since, card=None):
    """Run the agent to its end, a timeout (rc 124), or the owner's stop or halt (Interrupted)."""
    key = st["key"]
    # HARNESS_QUEUE_LAUNCH: harness labels the container with it (what stop_containers
    # stops) and does not remember the ticket for the folder (in place, the owner's)
    st["launch_id"] = f"{key}-{os.getpid()}-{int(time.time() * 1000)}"
    env = dict(os.environ, HARNESS_HOST_CONFIRM="1", HARNESS_QUEUE_LAUNCH=st["launch_id"])
    env.pop("HARNESS_TASK_CARD", None)
    if card:
        env["HARNESS_TASK_CARD"] = json.dumps(card)
    # nobody is at the keyboard: no editor (git commit, rebase), password or host-key prompt may hold the agent
    env.update(net_env(st["project"]), GIT_EDITOR="true")
    t0 = time.monotonic()
    end = t0 + ctx.stage_timeout
    notes_at = mtime(ctx.q.notes_path(key))
    if not st.get("in_place"):
        copy_instructions(st["project"], workdir(ctx.q, st))
    gitdir = git_dir(ctx.q, st)
    idle = max(0.0, float(getattr(ctx, "idle_min", 0) or 0)) * 60
    look = min(60.0, idle / 4) if idle else 0
    step(ctx, "the agent is working")
    with open(out, "w", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, cwd=workdir(ctx.q, st), stdin=subprocess.DEVNULL,
                             stdout=f, stderr=subprocess.STDOUT, env=env, **NEW_GROUP)
        ctx.child = p
        beat(ctx, agent=p.pid, launch=st["launch_id"])   # halt --force ends it (and its container) with the run
        # a run that dies leaves this behind: the next run (or retry, remove, stop) ends that agent
        st["agent"] = {"pid": p.pid, "launch": st["launch_id"], "host": socket.gethostname(),
                       "container": not (ctx.host or ctx.single), "workdir": workdir(ctx.q, st)}
        ctx.q.save(st)
        rc = None
        keep = False   # the agent not known to be ended: st["agent"] stays for the next run, stop, retry or remove
        seen, quiet_since, next_look = activity(ctx, st, out, gitdir), t0, t0 + look
        named = False   # the agent's session saved: a run that dies from here on leaves it to go on in
        try:
            while rc is None:
                try:
                    rc = p.wait(timeout=max(0.1, min(POLL, end - time.monotonic())))
                except subprocess.TimeoutExpired:
                    if not named and note_session(ctx, st, out, since):
                        named = True
                        ctx.q.save(st)
                    checkpoint(ctx)
                    if mtime(ctx.q.notes_path(key)) != notes_at:   # a note: into state.json for the viewer
                        notes_at = mtime(ctx.q.notes_path(key))
                        ctx.q.save(st)
                    now = time.monotonic()
                    if now >= end:
                        stop(p)
                        stop_containers(ctx, st)
                        rc = 124
                    elif look and now >= next_look:
                        next_look = now + look
                        got = activity(ctx, st, out, gitdir)
                        if got != seen:
                            seen, quiet_since = got, now
                        elif now - quiet_since >= idle:
                            rc = idle_out(ctx, st, p, (now - quiet_since) / 60)
        except Interrupted:
            stop(p)
            keep = not stop_containers(ctx, st)
            drop(note_paths(ctx.vault, key)["task"])
            sync_vault(ctx, key, since)
            raise
        except BaseException:  # Ctrl-C: the agent is in its own session and did not get it
            stop(p)
            keep = True   # a second Ctrl-C while its container is stopped
            keep = not stop_containers(ctx, st)
            raise
        finally:
            ctx.child = None
            if not keep:
                st.pop("agent", None)
            with contextlib.suppress(OSError):
                ctx.q.save(st)
            beat(ctx, agent=0, launch="")
            step(ctx, "the agent ended; checking its work")
    took = time.monotonic() - t0
    if rc not in (0, 124, IDLE) and took < LAUNCH_MIN and not started_work(ctx, st, since, gitdir):
        tail = read(out)[-400:].strip()
        raise LaunchFailed(f"the agent launch ended after {took:.0f}s with status {rc}, having started nothing"
                           + (f": {tail}" if tail else ""))
    st.pop("launch_fails", None)
    if hasattr(ctx, "outage"):
        ctx.outage.clear()   # a launch that ran: whatever kept the run waiting is over
    return rc


SCAN_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", "target", ".tox",
             ".mypy_cache", ".pytest_cache", ".next", ".gradle"}
SCAN_CAP = 20_000


def git_dir(q, st):
    """The git directory of the folder the agent works in (a clone's is ours: no git run in it)."""
    if not st.get("in_place"):
        return os.path.join(workdir(q, st), ".git")
    return git(st["project"], "rev-parse", "--absolute-git-dir", check=False).stdout.strip()


def newest(root):
    """The newest mtime (ns) of a file or folder under root, build and dependency folders left
    out, at most SCAN_CAP entries looked at."""
    best, n = 0, 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SCAN_SKIP]
        for name in [""] + files:
            n += 1
            if n > SCAN_CAP:
                return best
            with contextlib.suppress(OSError):
                best = max(best, os.stat(os.path.join(r, name) if name else r).st_mtime_ns)
    return best


def activity(ctx, st, out, gitdir):
    """What changes while an agent works: its output, its session database (--watch),
    the ticket's notes, the folder's git (commits, the index) and its newest file."""
    paths = [out] + list(getattr(ctx, "watch", []) or [])
    paths += [w + "-wal" for w in paths[1:]]
    paths += [note_paths(ctx.vault, st["key"])[k] for k in ("plan", "review", "findings")]
    if gitdir:
        paths += [os.path.join(gitdir, "index"), os.path.join(gitdir, "logs", "HEAD")]
    sig = []
    for path in paths:
        try:
            s = os.stat(path)
            sig.append((s.st_mtime_ns, s.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig) + (newest(workdir(ctx.q, st)),)


def started_work(ctx, st, since, gitdir):
    """True when the launch did something: a note of the ticket's, a commit on its
    branch, a write to the agent's session database (--watch)."""
    t = since / 1000   # no slack: the clone's own reflog is written just before the launch
    watch = list(getattr(ctx, "watch", []) or [])
    paths = [note_paths(ctx.vault, st["key"])[k] for k in ("plan", "review")] + watch + [w + "-wal" for w in watch]
    if gitdir:
        paths.append(os.path.join(gitdir, "logs", "refs", "heads", *branch_of(st).split("/")))
    return any((mtime(path) or 0) >= t for path in paths)


def idle_out(ctx, st, p, mins):
    """The agent has shown nothing new for mins: when the upstream does not answer it is
    waiting on it (Infra: the ticket goes back, the run waits); else it is stuck, or done
    and not exiting. Either way it is stopped, and its work so far is checked (IDLE)."""
    key = st["key"]
    ctx.q.log(key, f"the agent made no progress for {mins:.0f} min; checking that the upstream answers")
    step(ctx, f"the agent made no progress for {mins:.0f} min; checking that the upstream answers")
    up = upstream_ok(ctx)
    stop(p)
    stop_containers(ctx, st)
    if not up:
        raise Infra(f"the agent made no progress for {mins:.0f} min and the upstream does not answer")
    ctx.q.log(key, f"the agent made no progress for {mins:.0f} min and was stopped; its work is checked as it is")
    return IDLE


def stop_containers(ctx, st):
    """Stopping the docker client leaves its container running: stop the agent
    container of this ticket's launch (harness queue _stop_agents, by the folder
    it mounts and the launch's label). False: it may still be running."""
    if ctx.host or ctx.single:
        return True
    key = st["key"]
    n = launch_containers_stopped(ctx.harness, workdir(ctx.q, st), st.get("launch_id", ""))
    if n.isdigit():
        if int(n):
            ctx.q.log(key, f"stopped {n} agent container(s)")
        return True
    ctx.q.log(key, "warning: the agent's container may still be running; check 'harness list' "
                   "(harness stop NAME) before 'harness queue retry'")
    return False


def launch_containers_stopped(harness, folder, launch_id):
    """harness queue _stop_agents FOLDER LAUNCH: how many agent containers it
    stopped, as text; "" when that is not known."""
    r = run_quiet([BASH, harness, "queue", "_stop_agents", folder, launch_id], 120)
    return r.stdout.strip() if r.returncode == 0 else ""


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def checkpoint(ctx):
    """Raise Interrupted when the owner asked the run (DIR/halt) or this ticket (KEY/stop) to stop."""
    if os.path.exists(ctx.q.sentinel()):
        raise Interrupted("halt")
    parent = getattr(ctx, "parent", 0)
    if parent and os.name != "nt" and os.getppid() != parent:   # a slot whose run was killed
        raise Interrupted("halt")
    key = getattr(ctx, "key", "")
    if key and os.path.exists(ctx.q.sentinel(key)):
        raise Interrupted("stop")


def wait_call(ctx, fn, *args, deadline=None):
    """fn(*args) in a thread, looking for a stop or halt every POLL seconds while it
    is out (a verifier request can take minutes); a call given up on (a stop, or
    no answer in deadline seconds: InfraError) is left to end by itself."""
    box = {}

    def go():
        try:
            box["r"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            box["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    end = time.monotonic() + deadline if deadline else None
    while t.is_alive():
        t.join(POLL)
        if t.is_alive() and hasattr(ctx, "q"):
            checkpoint(ctx)
        if t.is_alive() and end and time.monotonic() >= end:
            raise InfraError(f"no answer in {deadline:g}s")
    if "e" in box:
        raise box.pop("e")
    return box["r"]


def sync_vault(ctx, key, since):
    """Copy the ticket's queue log (stages, checks, verifier verdicts) to KEY-log.md in the
    vault, then capture the session and commit the vault (best effort; never fails a stage)."""
    log = read(os.path.join(ctx.q.dir(key), "log.md"))
    try:
        if log.strip():
            fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", log)), default=0))
            write_atomic(note_paths(ctx.vault, key)["log"],
                         f"---\ntype: note\nticket: {key}\nsummary: {yq(f'harness queue log of {key}')}\n---\n\n"
                         f"# {key}: queue log\n\nWritten by `harness queue`; each line is a stage, a check or a "
                         f"verdict.\n\n{fence}text\n{log.rstrip()}\n{fence}\n")
    except OSError as e:
        ctx.q.log(key, f"warning: could not write the log into the vault: {e}")
    st = ctx.q.load(key) or {"key": key, "project": ""}
    step(ctx, "saving the session into the vault")
    try:   # the slots of a parallel run commit the vault one at a time
        with held(os.path.join(ctx.q.root, "vault.lock"), wait=SYNC_TIMEOUT):
            r = run_quiet([BASH, ctx.harness, "queue", "_sync", key, workdir(ctx.q, st), str(since)], SYNC_TIMEOUT)
    except Busy:
        ctx.q.log(key, "warning: the session was not saved into the vault (another slot held the vault for "
                       f"{SYNC_TIMEOUT}s); the ticket goes on")
        return
    if r.returncode != 0:
        ctx.q.log(key, f"warning: the session was not saved into the vault (harness queue _sync: "
                       f"{(r.stderr or r.stdout).strip()[-300:] or 'exit ' + str(r.returncode)}); the ticket goes on")


# On Windows a bare "bash" can resolve to WSL's System32\bash.exe before PATH is searched;
# shutil.which walks PATH, where Git Bash puts its own bash first.
BASH = shutil.which("bash") or "bash"

# The agent gets its own session (process group on Windows) so Ctrl-C reaches only the queue,
# which then stops the agent's whole tree itself.
NEW_GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})


def stop(p, grace=30):
    """End p and everything it started (it runs in its own session, NEW_GROUP)."""
    if os.name == "nt":  # no killpg or SIGKILL: taskkill /T ends bash and everything it started
        kill_tree(p.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(timeout=30)
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 10)):
        try:
            os.killpg(p.pid, sig)
            p.wait(timeout=wait)
            break
        except ProcessLookupError:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            continue
    # what the leader left behind in its group (a docker client, ssh): the group lives
    # on while any of it does, so its id is not anyone else's yet
    with contextlib.suppress(OSError):
        os.killpg(p.pid, signal.SIGKILL)


def kill_tree(pid):
    """End pid's process tree (POSIX: its process group, as NEW_GROUP made it); best
    effort, bounded: taskkill itself is given a minute."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=60)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            return
        end = time.time() + 10
        while time.time() < end:
            try:
                os.killpg(pid, 0)
            except OSError:
                return
            time.sleep(0.1)


def probe(ctx):
    """Raise Infra when the upstream does not answer one tiny request."""
    try:
        model_call(ctx, [{"role": "user", "content": "Reply with the single word OK."}], 180,
                   "checking that the upstream answers")
    except InfraError as e:
        raise Infra(str(e)) from None


def upstream_ok(ctx):
    """One tiny request, no retries: does the upstream answer now?"""
    try:
        wait_call(ctx, call_model, ctx.proxy, ctx.verify_model,
                  [{"role": "user", "content": "Reply with the single word OK."}], 120, deadline=180)
        return True
    except InfraError:
        return False


def hhmm(t):
    return time.strftime("%H:%M", time.localtime(t))


def wait_out(ctx, e):
    """The ticket went back because the upstream does not answer (or a launch failed
    having started nothing): wait, looking again after 2, 5, 10, then every 30 minutes,
    a halt still ending the run, until the upstream answers (a failed launch: until the
    pause is over). After ctx.outage_wait seconds of it the run ends: Infra."""
    o = ctx.outage
    o.setdefault("since", time.time())
    what = "a launch failed having started nothing" if isinstance(e, LaunchFailed) else "the upstream does not answer"
    while True:
        waited = time.time() - o["since"]
        left = ctx.outage_wait - waited
        if left <= 0:
            raise Infra(f"{str(e)[:200]}; waited {waited / 60:.0f} min") from None
        n = o.get("n", 0)
        secs = min(OUTAGE_PAUSES[min(n, len(OUTAGE_PAUSES) - 1)], left)
        o["n"] = n + 1
        now = time.time()
        text = (f"waiting: {what} since {hhmm(o['since'])}; looking again at {hhmm(now + secs)}, "
                f"giving up at {hhmm(o['since'] + ctx.outage_wait)}")
        beat(ctx, ticket="", stage="", step=text,
             waiting={"since": int(o["since"]), "next": int(now + secs), "until": int(o["since"] + ctx.outage_wait),
                      "why": str(e)[:300]})
        print(f"[queue] {text} ({str(e)[:200]})", file=sys.stderr)
        try:
            pause(ctx, secs)
        finally:
            beat(ctx, waiting=None)
        if isinstance(e, LaunchFailed) or upstream_ok(ctx):
            return


def is_agent(pid, launch, key):
    """True when pid still leads the agent launch `launch` of ticket key (a pid
    alone may be someone else's by now)."""
    if not pid:
        return False
    if os.name == "nt":
        r = run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                       f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"], 60)
        return r.returncode == 0 and f"--ticket {key}" in r.stdout
    if os.path.isdir("/proc"):
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                if os.getpgid(int(p)) != pid:
                    continue
                with open(f"/proc/{p}/environ", "rb") as f:
                    if f"HARNESS_QUEUE_LAUNCH={launch}".encode() in f.read().split(b"\0"):
                        return True
            except OSError:
                continue
        return False
    r = run_quiet(["ps", "-A", "-o", "pgid=,command="], 60)
    return any(line.split(None, 1)[0] == str(pid) and f"--ticket {key}" in line
               for line in r.stdout.splitlines() if line.strip())


def reap(q, st, harness, why):
    """End the agent a run that is gone left on st (state.json "agent"): its process
    tree when it is still that launch on this machine, and its container. The caller
    drops st["agent"] and saves."""
    a = st.get("agent") or {}
    if not a:
        return
    key = st["key"]
    if a.get("host") == socket.gethostname() and is_agent(a.get("pid"), a.get("launch", ""), key):
        kill_tree(a["pid"])
        q.log(key, f"ended the agent a run left running (pid {a['pid']}): {why}")
    if a.get("container") and harness:
        n = launch_containers_stopped(harness, a.get("workdir") or workdir(q, st), a.get("launch", ""))
        if n.isdigit() and int(n):
            q.log(key, f"stopped {n} agent container(s) a run left running: {why}")
    elif a.get("container"):
        q.log(key, f"warning: the agent's container may still be running (launch {a.get('launch', '')}); "
                   "check 'harness list' (harness stop NAME)")


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
                **landing(st, lands(ctx, st)),
                TICKET_FILE=ap["ticket"],
                TICKET=read(p["ticket"], TICKET_CAP) or "(the ticket file is missing; read the path above)",
                PLAN_FILE=ap["plan"], PLAN=plan,
                REVIEW_FILE=ap["review"], FINDINGS_FILE=ap["findings"], ANSWERS_FILE=ap["answers"],
                ANSWERS=answers or "(none)", NOTES=owner_notes(ctx.q.notes(st["key"])),
                FEEDBACK=cap(st.get("feedback") or "(first attempt)", NOTE_CAP))


IN_CLONE = "a clone of the project made for this ticket"
IN_PLACE = ("the owner's own project folder, not a clone. It has the owner's uncommitted files: leave every file you "
            "do not change for the ticket alone; where you commit, commit only files you changed, by name (git add "
            "<file>; never git add -A, git add . or git commit -a); never run git checkout/restore of files you did "
            "not change, git reset --hard, git clean, git stash or git switch, here or in a repository inside it")


def lands(ctx, st):
    """(branches, left): where the ticket's change is committed, [(rel, branch)], the
    top's first, and the repositories where it is left uncommitted, [rel]."""
    branches = [] if st.get("no_branch") else [(".", branch_of(st))]
    branches += [(rel, v.get("branch") or branch_of(st)) for rel, v in sorted((st.get("repos") or {}).items())]
    if isinstance(st.get("baselines"), dict):
        left = sorted(st["baselines"])
    else:
        try:
            left = uncommitted_repos(st, (ctx.vault, ctx.q.root)) if st.get("in_place") else []
        except (RuntimeError, OSError, subprocess.SubprocessError):
            left = ["."] if st.get("no_branch") else []
    return branches, left


def landing(st, how):
    """The prompts' words for where the change lands (lands): LAYOUT, COMMIT_STEP,
    CHECK_STEP, FIX_COMMIT_STEP, CHANGE (the review's) and CHANGE_SO_FAR (the fix's)."""
    branches, left = how
    key, base = st["key"], st.get("base_sha", "")
    name = lambda rel: "this folder (the project's top)" if rel == "." else f"{rel}/"   # noqa: E731
    if branches == [(".", branch_of(st))] and not left:   # one repository on one branch
        b = branch_of(st)
        return dict(
            LAYOUT=f"You are on branch {b}. Stay on that branch.",
            COMMIT_STEP=f"Commit on {b}: `git add <the files you changed>` then `git commit -m \"{key}: <what changed>\"`. "
                        "Do not push; harness pushes.",
            CHECK_STEP=f"Check `git status` is clean and `git log --oneline {base}..HEAD` shows your commit(s).",
            FIX_COMMIT_STEP=f"Commit on {b}: `git add <files>` then `git commit -m \"{key}: review fixes\" -m \"<why any "
                            "item was left as it is>\"`. When every item was wrong and nothing changed, still commit: "
                            "`git commit --allow-empty` with the same messages. Do not push, do not rewrite earlier commits.",
            CHANGE=f"`git diff {base}..HEAD`", CHANGE_SO_FAR=f"`git diff {base}..HEAD`", ON_BRANCH=f", on branch {b}")
    parts = [f"{name(rel)}: on branch {b}; commit what you change there" for rel, b in branches]
    if left:
        parts.append(f"{', '.join(name(r) for r in left)}: leave what you change there uncommitted (no git add, "
                     "commit, stash or switch there; harness takes the change from the files)")
    layout = ("The project is more than one git repository: this folder and the repositories inside it (each its own "
              ".git). Work in whichever of them the ticket needs: the ticket, the plan, AGENTS.md and the notes say "
              "which; nothing is off limits. How your change lands in each: " + "; ".join(parts) + ".") \
        if len(branches) + len(left) > 1 or (left and left != ["."]) else \
        "Leave what you change uncommitted: no git add, commit, stash or switch; harness takes the change from the files."
    on = [f"`git -C {'.' if rel == '.' else rel} commit` on {b}" for rel, b in branches]
    commit = (f"In each repository on a branch of the ticket's, commit the files you changed there by name "
              f"(`git -C <repository> add <files>` then `git -C <repository> commit -m \"{key}: <what changed>\"`: "
              f"{', '.join(on)}). " if branches else "") + \
        ("Leave every other change uncommitted. " if branches and left else
         "Leave your changes uncommitted; commit nothing. " if left else "") + "Do not push; harness pushes."
    fix = (f"Commit what you changed in a repository on a branch of the ticket's ({', '.join(on)}) with "
           f"`-m \"{key}: review fixes\" -m \"<why any item was left as it is>\"`; leave the rest uncommitted. "
           if branches else "Leave your changes uncommitted; commit nothing. ") + \
        "An item that is wrong (the code already right) stays as it is. Do not push, do not rewrite earlier commits."
    on_branch = (". The project is more than one git repository: this folder and the repositories inside it (each "
                 "its own .git): " + "; ".join([f"{name(rel)} on branch {b}" for rel, b in branches] + (
                     [f"{', '.join(name(r) for r in left)} with the change left uncommitted"] if left else []))) \
        if len(branches) + len(left) > 1 or (left and left != ["."]) else \
        (f", on branch {branches[0][1]}" if branches else ", where the change is left uncommitted")
    return dict(LAYOUT=layout, COMMIT_STEP=commit, ON_BRANCH=on_branch,
                CHECK_STEP="Check `git status` in each repository you changed: your changes are committed where "
                           "the ticket has a branch and uncommitted everywhere else, and nothing else of yours is left.",
                FIX_COMMIT_STEP=fix,
                CHANGE="the diff below (every repository's part, its paths from this folder; where the change is "
                       "left uncommitted, against the files as they were when the work started)",
                CHANGE_SO_FAR="its commits on the ticket's branches and what `git diff` shows uncommitted in the "
                              "repositories where it is left uncommitted")


# The task card: a few lines a model on the cooperative route (no native tool
# calls) sees at the end of every turn, where the stage's prompt is long gone
# after its first tool call. harness sends it to the proxy (x-harness-task,
# with the ticket's title); the proxy points at KEY-task.md for the rest. Done
# says what the stage's checks look for, in their words.
CARD_WHERE_IN_PLACE = ("the owner's own project folder, not a clone. Where you commit, commit only files you changed, "
                       "by name (never git add -A, git add . or git commit -a); leave every other file alone (no git "
                       "reset --hard, clean, stash, or checkout of files you did not change)")
CARD_DONE = {
    "plan": "no code changed, the plan written to {PLAN_FILE}: # {KEY} plan, ## Goal, ## Steps (1. `file` "
            "`function()`: the change (AC1)), ## Acceptance criteria (one line each: - AC1: GIVEN .. WHEN .. THEN .. "
            "(ticket: \"exact words\") or (edge of AC1)), ## Questions (- Q1 [blocking] ..? Options: (a) .. (b) .. "
            "Default: (a), because ..), ## Assumptions (- A1: ..); then each Step's code re-read and the plan fixed.",
    "implement": "every acceptance criterion of the plan is met and committed on {BRANCH} (git add <files>, git commit "
                 "-m \"{KEY}: <what changed>\"), and git status is clean. Do not push.",
    "implement-repos": "every acceptance criterion of the plan is met. {COMMIT_STEP}",
    "review": "the review is written to {REVIEW_FILE}: # {KEY} review, then ## Must fix (path:line: the problem and "
              "what to do, or - None) and ## Notes. Change no code in this stage.",
    "fix": "every Must fix item is fixed, or said in the commit message to be wrong, and committed on {BRANCH} in a "
           "new commit (git commit --allow-empty when nothing changed). Do not push.",
    "fix-repos": "every Must fix item is fixed. {FIX_COMMIT_STEP}",
}


def task_card(ctx, st, stage):
    if stage not in CARD_DONE:
        return None
    p = {k: agent_path(ctx, v) for k, v in note_paths(ctx.vault, st["key"]).items()}
    how = lands(ctx, st)
    words = landing(st, how)
    done = CARD_DONE[stage + "-repos"] if stage + "-repos" in CARD_DONE and "COMMIT_STEP" in words \
        and how != ([(".", branch_of(st))], []) else CARD_DONE[stage]
    card = {"stage": stage, "attempt": st.get("attempt", 0) + 1,
            "branch": "" if st.get("no_branch") else branch_of(st),
            "where": CARD_WHERE_IN_PLACE if st.get("in_place") else IN_CLONE,
            "done": done.format(KEY=st["key"], BRANCH=branch_of(st), PLAN_FILE=p["plan"], REVIEW_FILE=p["review"],
                                **words)}
    if stage == "fix" and st.get("must_fix"):
        card["must_fix"] = len(st["must_fix"])
    do = stage_guidance(ctx.q, st["key"]).get(stage)
    if do:
        card["do"] = [line.replace("{KEY}", st["key"]) for line in do]
    return card


STAGE_GUIDANCE = os.path.join(PROMPTS, "stage-guidance.json")


def stage_guidance(q=None, key=""):
    """{stage: [lines]} from queue_prompts/stage-guidance.json, the few lines
    a stage's agent sees on its task card every turn; {} when the file is
    missing or not that shape (logged), so a hand edit never stops a run."""
    try:
        with open(STAGE_GUIDANCE, encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        if q is not None and key and os.path.exists(STAGE_GUIDANCE):
            q.log(key, f"{STAGE_GUIDANCE} is not used: {str(e)[:200]}")
        return {}
    return {k: [x for x in v if isinstance(x, str) and x.strip()] for k, v in obj.items()
            if not k.startswith("_") and isinstance(v, list)}


def owner_notes(notes):
    """The owner's notes for a prompt, as written, newest kept when they are long."""
    text = "\n\n".join(f"[{n.get('at', '')}]\n{str(n.get('text', '')).strip()}" for n in notes)
    if len(text) > NOTE_CAP:
        text = "[... earlier notes cut ...]\n" + text[-NOTE_CAP:]
    return text or "(none)"


def ticket_criteria(text):
    """The bullets under the ticket's own 'Acceptance criteria' heading, if it has one:
    a markdown heading first (the tracker's criteria field, as harness writes it),
    else a line that starts with the words (a description's own)."""
    m = re.search(r"(?im)^#{1,6}\s*acceptance criteria\s*$", text) \
        or re.search(r"(?im)^[#*\s]*acceptance criteria\b.*$", text)
    if not m:
        return []
    return bullets(re.split(r"(?m)^#{1,6}\s", text[m.end():], maxsplit=1)[0])[:15]


def change_criteria(ctx, st, plan):
    """What a change is held to: the plan's acceptance criteria; with no plan (the
    stage is not in the ticket's stages) the ticket's own, else the ticket as a whole."""
    if plan.strip():
        return gated_criteria(check_plan(plan)[1])
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
    rc, out, started = agent_or_kept(ctx, st, "plan", lambda: render("plan", **v))
    p = note_paths(ctx.vault, st["key"])
    text = fresh(p["plan"], started) or recover_note(ctx, st, "plan", started, out)
    problems, criteria, questions = check_plan(text)
    if not text.strip():
        problems = [f"the plan file was not written: write it to {v['PLAN_FILE']}"]
    if not problems:
        try:
            last = json.loads(read(os.path.join(ctx.q.dir(st["key"]), "plan.json")) or "null")
        except ValueError:
            last = None
        shape = shape_problems(text) + accepted_changed(last if isinstance(last, dict) else None, read(p["answers"]),
                                                        owner_notes(ctx.q.notes(st["key"])), criteria)
        if st["attempt"] < ctx.retries:   # asked for once more; on the last try only noted
            problems = shape
    if not problems:
        notes = plan_notes(text) + shape + step_notes(text, workdir(ctx.q, st))
        listed, nums = criteria_by_id(criteria)
        obj = verdict(ctx, st, "plan", lambda c: render("verify_plan", TICKET=read(p["ticket"], c), PLAN=text,
                                                         ANSWERS=v["ANSWERS"], CRITERIA=listed))
        problems = judge(obj, criteria, read(p["ticket"]), "ticket_quote", "ticket", soft=notes, nums=nums)
        ctx.q.log(st["key"], f"plan verifier: {verifier_line(obj, problems)}")
        if notes:
            ctx.q.log(st["key"], "plan notes (not failures)", "\n".join("- " + n for n in notes))
    if problems:
        return fail(ctx, st, rc, problems)
    items = plan_items(text)
    st["questions"] = [dict(q, blocking=bool(blocking([q["text"]]))) for q in items["questions"]]
    st["criteria"] = [dict(c, edge=is_edge(c)) for c in items["criteria"]]
    st["plan_sig"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    write_atomic(os.path.join(ctx.q.dir(st["key"]), "plan.json"),
                 json.dumps(dict(items, sig=st["plan_sig"]), indent=1, ensure_ascii=False) + "\n")
    open_q = blocking(questions)
    if open_q and st.get("take_defaults"):
        open_q = take_defaults(ctx, st, p, open_q)
    if open_q:
        record(st, "questions", open_q)
        st.pop("verify_only", None)   # answered, the plan is written again
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


def default_said(q):
    """A question's default with the option it names written out: "(a), because
    ..." -> "(a) Postgres, because ..."."""
    m = re.match(r"\(?([a-h])\)", q["default"])
    i = ord(m.group(1)) - 97 if m else -1
    if 0 <= i < len(q["options"]):
        return f"({m.group(1)}) {q['options'][i]}{q['default'][m.end():]}"
    return q["default"]


def take_defaults(ctx, st, p, open_q):
    """The ticket goes on with its questions' defaults (its after-plan setting):
    each blocking question with a Default is taken at it, written to the
    answers file for the stages after; the ones with no Default, still open."""
    taken, left = [], []
    for x in open_q:
        q = question(x, 0)
        (taken if q["default"] else left).append(x)
    if taken:
        lines = "\n".join(f"- {question(x, 0)['ask'][:300]} -> the default: {default_said(question(x, 0))[:300]}"
                          for x in taken)
        old = read(p["answers"])
        write_atomic(p["answers"], (old.rstrip() + "\n" if old.strip() else "") + f"\n<!-- {now_iso()} -->\n"
                     "Taken at their defaults (the ticket is set to go on without waiting for answers):\n"
                     + lines + "\n")
        ctx.q.log(st["key"], f"{len(taken)} blocking question(s) taken at their default (the ticket goes on "
                             "without waiting for answers)", lines)
    return left


def verifier_line(obj, problems):
    """The verifier's verdict for the log, saying whether it stands: a PASS with
    problems found in its answer (an unquoted criterion, say) is a fail."""
    v = obj.get("verdict") or "no verdict"
    if not problems:
        return f"{v}, no problems: passed"
    return f"{v}, but {len(problems)} problem(s) found: failed" if str(v).upper() == "PASS" \
        else f"{v}, {len(problems)} problem(s): failed"


def numbered(items):
    return "\n".join(f"{i}. {c}" for i, c in enumerate(items, 1))


def verify_change(ctx, st, rc, stage):
    """Gates then the verifier on the branch; True when it passes."""
    p = note_paths(ctx.vault, st["key"])
    plan = read(plan_copy(ctx.q, st["key"]))
    criteria = change_criteria(ctx, st, plan)
    if stage == "fix":
        criteria = criteria + [f"(review) {m}" for m in st.get("must_fix", [])]
    change = ticket_change(ctx.q, st)
    problems, warnings = check_change(st, change, plan)
    if st.get("in_place"):
        problems += in_place_problems(ctx, st)
        warnings += head_moved(st)
    if not problems and stage == "implement" and not moved_since(st, change, "start"):
        problems.append(f"no new commit on branch {branch_of(st)}: commit your change (git add <files>, git commit)"
                        if not st.get("no_branch") and not st.get("baselines") and not st.get("nested")
                        else "nothing changed since the work started: make the change " + HOW_SHORT)
    if not problems and stage == "fix" and st.get("review_digest") and not moved_since(st, change, "review"):
        problems.append("no new commit since the review: fix the Must fix items and commit"
                        if not st.get("no_branch") else "nothing changed since the review: fix the Must fix items")
    if not problems and rc not in (0, IDLE):   # IDLE: it went quiet; the work is judged as it is
        problems.append("the agent did not finish; check the work is complete and committed")
    for w in warnings:
        ctx.q.log(st["key"], f"warning: {w}")
    if not problems:
        _, diff, files, commits, parts = change
        ctx.q.log(st["key"], f"{stage}: {commits} commit(s), {len(files)} file(s) changed"
                  + (f" ({'; '.join(part_line(p) for p in parts)})" if len(parts) > 1 or st.get("no_branch") else ""))
        notes = ""
        if stage == "fix" and st.get("review_sha") and change[0]:
            notes = git(st["project"], "log", "--format=%B", f"{st['review_sha']}..{change[0]}",
                        check=False).stdout.strip()
        obj = verdict(ctx, st, stage, lambda c: render(
            "verify_change", TICKET=read(p["ticket"], min(TICKET_CAP, c)), CRITERIA=numbered(criteria),
            DIFF=cap(diff, c), COMMITS=cap(notes, NOTE_CAP) or "(none)"))
        problems = judge(obj, criteria, diff, "quote", "diff", notes)
        ctx.q.log(st["key"], f"{stage} verifier: {verifier_line(obj, problems)}")
        st["warnings"] = warnings
    if problems:
        fail(ctx, st, rc, problems)
        return False
    st["verified_sha"] = change[0]   # what the push may push: a branch moved since is not
    st["verified_repos"] = {p["repo"]: p["sha"] for p in change[4] if p["kind"] == "branch" and p["repo"] != "."}
    return True


HOW_SHORT = "(where the ticket says how it lands: committed on its branch, or left uncommitted)"


def head_moved(st):
    """Warnings: a repository whose change is left uncommitted that has commits since the
    work started (the agent's, on the owner's branch; or the owner's own meanwhile)."""
    out = []
    for rel, b in sorted((st.get("baselines") or {}).items()):
        head = git(repo_dir(st, rel), "rev-parse", "-q", "--verify", "HEAD", check=False).stdout.strip()
        if b.get("head") and head != b["head"]:
            out.append(f"{'the project' if rel == '.' else rel} has commits since the work started "
                       f"({b['head'][:12]}..{head[:12]}), where the ticket's change is left uncommitted")
    return out


def in_place_problems(ctx, st):
    """In place: the project left on the ticket's branch with nothing half done, and
    no commit since the start touching a file the owner had uncommitted when the
    ticket started (each commit, not the sum: a file added then removed was pushed)."""
    project = st["project"]
    trouble = in_place_trouble(project, None if st.get("no_branch") else branch_of(st))
    if trouble:
        return [trouble]
    out = []
    if not st.get("no_branch"):
        owner = owner_files(ctx.q, st)
        out += owners_touched(project, ".", st["start_sha"], branch_of(st),
                              set(owner.get("untracked", [])) | set(owner.get("changed", [])))
    for rel, n in sorted((st.get("nested") or {}).items()):
        trouble = in_place_trouble(repo_dir(st, rel), n["branch"])
        out += [trouble] if trouble else \
            owners_touched(repo_dir(st, rel), rel, n["start_sha"], n["branch"], set(n.get("owner") or []))
    return out


def owners_touched(repo, rel, since, branch, mine):
    """[problem] when a commit since since on branch touches a file the owner had
    uncommitted in the repository when the ticket started (each commit, not the sum:
    a file added then removed was pushed); [] when none does."""
    dirs = tuple(p for p in mine if p.endswith("/"))
    touched = git(repo, "log", "--format=", "--name-only", "--no-renames",
                  f"{since}..refs/heads/{branch}", check=False).stdout.split("\n")
    hit = sorted({f for f in touched if f and (f in mine or f + "/" in mine or f.startswith(dirs))})
    if not hit:
        return []
    where = "in the project" if rel == "." else f"in {rel} (run these there)"
    return [f"the commits touch {', '.join(hit[:10])}, which the owner had uncommitted {where} when the ticket "
            f"started (theirs, not the ticket's). Take them out of every commit since {since[:12]}: "
            f"git reset --soft {since[:12]}, git restore --staged {' '.join(hit[:10])}, then commit "
            "again with only your files. Never delete or edit those files"]


def moved_since(st, change, what):
    """True when the change has work since what ("start": the set-up; "review": the
    review): a commit on a branch of the ticket's, or a file left uncommitted changed."""
    if change is None:
        return False
    if what == "review":
        return (change[0] or "") != st.get("review_sha", "") or digest(change[1]) != st.get("review_digest")
    starts = {rel: n.get("start_sha") for rel, n in (st.get("nested") or {}).items()}
    starts["."] = st.get("start_sha")
    return any(p["kind"] == "uncommitted" or p["sha"] != starts.get(p["repo"]) for p in change[4])


def digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def cap(text, n):
    return text if len(text) <= n else text[:n] + f"\n[... cut at {n} characters ...]\n"


def stage_implement(ctx, st):
    rc, _, _ = agent_or_kept(ctx, st, "implement", lambda: render("implement", **common(ctx, st)))
    if verify_change(ctx, st, rc, "implement"):
        advance(ctx, st, next_stage(st, "implement"))


def part_line(p):
    """One repository's part of the change, said in a line."""
    where = "the project" if p["repo"] == "." else p["repo"]
    how = f"{p['commits']} commit(s) on {p['branch']}" if p["kind"] == "branch" else "left uncommitted"
    return f"{where}: {len(p['files'])} file(s), {how}"


def stage_review(ctx, st):
    change = ticket_change(ctx.q, st)
    v = common(ctx, st)
    v["DIFF"] = cap(change[1], DIFF_CAP_AGENT) if change else "(no diff)"
    rc, out, started = agent_or_kept(ctx, st, "review", lambda: render("review", **v))
    path = note_paths(ctx.vault, st["key"])["review"]
    text = fresh(path, started) or recover_note(ctx, st, "review", started, out)
    problems, must = check_review(text)
    if not text.strip():
        problems = [f"the review file was not written: write it to {v['REVIEW_FILE']}"]
    if problems:
        return fail(ctx, st, rc, problems)
    ctx.q.log(st["key"], f"review: {len(must)} must-fix item(s)", "\n".join("- " + m for m in must))
    st.update(must_fix=must[:15], review_sha=(change[0] or "") if change else "",
              review_digest=digest(change[1]) if change else "")
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
    rc, _, _ = agent_or_kept(ctx, st, "fix", lambda: render("fix", **v))
    if verify_change(ctx, st, rc, "fix"):
        advance(ctx, st, next_stage(st, "fix"))


def stage_push(ctx, st):
    key, project = st["key"], st["project"]
    change = ticket_change(ctx.q, st)
    if change is None:
        if not st.get("no_branch") and not st.get("nested") and not st.get("baselines"):
            where = "the project" if st.get("in_place") else "the ticket's clone"
            return block(ctx, st, f"nothing to push: branch {branch_of(st)} in {where} has no commits on its start "
                                  f"(or is gone); harness queue retry {key} --stage implement makes the change again")
        return block(ctx, st, f"nothing to push: {no_change(st)}; harness queue retry {key} --stage implement "
                              "makes the change again")
    sha, name, parts = change[0], branch_of(st), change[4]
    if not st.get("no_branch") and st.get("verified_sha") and st["verified_sha"] != sha:
        return block(ctx, st, f"branch {name} moved since it was checked ({st['verified_sha'][:12]}, now "
                              f"{(sha or 'no commits')[:12]}): look at what was added; harness queue retry {key} "
                              "--stage push pushes it as it is")
    for p in parts:
        was = (st.get("verified_repos") or {}).get(p["repo"])
        if p["repo"] != "." and p["kind"] == "branch" and "verified_repos" in st and was != p["sha"]:
            return block(ctx, st, f"branch {p['branch']} in {p['repo']} moved since it was checked "
                                  f"({(was or 'not there')[:12]}, now {p['sha'][:12]}): look at what was added; "
                                  f"harness queue retry {key} --stage push pushes it as it is")
    warnings = list(st.get("warnings") or [])
    if sha:
        branch = f"refs/heads/{name}"
        old = git(project, "rev-parse", "-q", "--verify", branch, check=False).stdout.strip()
        if st.get("in_place"):
            pass   # the agents committed on the project's branch itself
        elif checked_out(project, name):
            # Moving a checked-out branch leaves its index and files behind: the
            # checkout would then show the queue's commits as changes undone.
            warnings.append(f"branch {name} is checked out in the project; not moved to {sha[:12]} "
                            f"(git merge --ff-only {sha[:12]} there takes it)")
        elif not old:
            git(project, "update-ref", branch, sha)
            st["made_branch"] = True
        elif old != sha:
            if git(project, "merge-base", "--is-ancestor", old, sha, check=False).returncode == 0:
                git(project, "update-ref", branch, sha, old)
            else:
                warnings.append(f"the project's branch {name} is not behind the queue's branch; left as it is")
    for w in warnings[len(st.get("warnings") or []):]:
        ctx.q.log(key, f"warning: {w}")
    st["warnings"] = warnings
    # each branch with commits pushed from its own repository: the top's first, then the ones inside it
    pushes, reasons = [], []
    for p in [p for p in parts if p["kind"] == "branch"]:
        d, where = repo_dir(st, p["repo"]), "the project" if p["repo"] == "." else p["repo"]
        of = "" if p["repo"] == "." else f" of {where}"
        push = {"remote": ctx.remote, "url": remote_url(d, ctx.remote), "branch": p["branch"], "sha": p["sha"],
                "result": "local", "error": "", "repo": p["repo"]}
        pushes.append(push)
        if not ctx.push:
            reasons.append(f"kept local (QUEUE_PUSH=0): branch {p['branch']} at {p['sha'][:12]} in {where}")
        elif not git(d, "remote", "get-url", ctx.remote, check=False).stdout.strip():
            reasons.append(f"kept local: {where} has no remote {ctx.remote}; branch {p['branch']} at "
                           f"{p['sha'][:12]} in {where}")
        else:
            checkpoint(ctx)   # a stop asked during the stage: nothing (more) pushed
            r = net_git(d, "push", "-q", ctx.remote, f"{p['sha']}:refs/heads/{p['branch']}", timeout=PUSH_TIMEOUT)
            if r.returncode != 0:
                push.update(result="rejected", error=(r.stderr or r.stdout).strip()[:300])
                results(ctx, st, change, pushes)
                return block(ctx, st, f"push{of} to {ctx.remote} refused: {push['error']}. Fix that, then "
                                      f"harness queue retry {key} --stage push (branch {p['branch']} is at "
                                      f"{p['sha'][:12]} in {where})")
            push["result"] = "pushed"
            ctx.q.log(key, f"pushed branch {p['branch']}{of} to {ctx.remote} ({p['sha'][:12]})")
    left = [p["repo"] for p in parts if p["kind"] == "uncommitted"]
    if left:
        reasons.append("changes left uncommitted in " + ", ".join("the project" if r == "." else r for r in left))
    reason = "; ".join(reasons)
    if reason:
        ctx.q.log(key, reason)
    results(ctx, st, change, pushes)
    record(st, "passed", [part_line(p) for p in parts])
    st.update(status="done", stage="done", attempt=0, feedback="", reason=reason, sha=sha or "")
    ctx.q.save(st)


def remote_url(project, remote):
    """The remote's url without a user or password in it; "" when there is no such remote."""
    url = git(project, "remote", "get-url", remote, check=False).stdout.strip()
    return re.sub(r"(?i)^([a-z][a-z0-9+.-]*://)[^/@]*@", r"\1", url)


def compare_url(url, base, branch):
    """The page that opens a pull request of branch onto base: GitHub's compare view,
    GitLab's new merge request; "" for another host or a url that is not a host's."""
    m = re.match(r"(?i)^(?:(?:https?|ssh|git)://)?(?:[^@/]+@)?([a-z0-9.-]+)(?::\d+)?[:/]([^:]+?)(?:\.git)?/*$", url)
    if not m or os.path.isabs(url) or re.match(r"^[A-Za-z]:[\\/]", url) or url.startswith("file:"):
        return ""
    host, path = m.group(1).lower(), m.group(2).strip("/")
    quote = lambda s: urllib.parse.quote(s, safe="/")   # noqa: E731
    if "github" in host:
        return f"https://{host}/{path}/compare/{quote(base)}...{quote(branch)}?expand=1"
    if "gitlab" in host:
        return f"https://{host}/{path}/-/merge_requests/new?merge_request[source_branch]={quote(branch)}"
    return ""


def results(ctx, st, change, pushes):
    """What the ticket ended with, into state.json (the run viewer shows it) and
    KEY-result.md in the vault; best effort: the outcome stands without them.
    pushes: one push (dict) per branch, the top's first; st["push"] is the top's (or,
    with none, the first, or one saying nothing was pushed), st["pushes"] all of them."""
    key, project = st["key"], st["project"]
    try:
        commits, stat = [], {"files": 0, "insertions": 0, "deletions": 0}
        if change:
            for p in change[4]:
                if p["kind"] != "branch":
                    continue
                d, since = repo_dir(st, p["repo"]), st["base_sha"] if p["repo"] == "." else \
                    (st.get("nested") or {}).get(p["repo"], {}).get("base_sha", "")
                pre = "" if p["repo"] == "." else f"{p['repo']}: "
                log = git(d, "log", "-n", "50", "--format=%H%x09%s", f"{since}..{p['sha']}", check=False)
                commits += [{"sha": c.split("\t", 1)[0], "subject": pre + c.split("\t", 1)[-1]}
                            for c in log.stdout.splitlines() if c]
            stat["files"] = len(change[2])
            for line in change[1].splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    stat["insertions"] += 1
                elif line.startswith("-") and not line.startswith("---"):
                    stat["deletions"] += 1
        push = next((p for p in pushes if p.get("repo", ".") == "."), pushes[0] if pushes else None) or \
            {"remote": "", "url": "", "branch": "", "sha": "", "result": "local", "error": "", "repo": "."}
        base = st.get("base", "")
        base = st.get("orig_branch", "") if base == HERE else re.sub(rf"^{re.escape(push['remote'])}/", "", base)
        if push.get("repo", ".") != ".":
            base = (st.get("repos") or {}).get(push["repo"], {}).get("base", "")
            base = re.sub(rf"^{re.escape(push['remote'])}/", "", base) if push["remote"] else base
        link = compare_url(push["url"], base, push["branch"]) if push["result"] == "pushed" and base else ""
        now = git(project, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        left = [p["repo"] for p in (change[4] if change else []) if p["kind"] == "uncommitted"]
        st.update(push=push, pushes=pushes, compare_url=link, commits=commits, diffstat=stat, folder_branch_now=now,
                  left_uncommitted=left)
        st.setdefault("orig_branch", "")
        st.setdefault("warnings", [])
        ctx.q.save(st)
        result_note(ctx, st)
    except Exception as e:  # noqa: BLE001 - the stage's outcome does not hang on its summary
        ctx.q.log(key, f"warning: the ticket's results were not all written: {type(e).__name__}: {str(e)[:200]}")


def where_of(push):
    return {"pushed": f"pushed to {push['remote']}", "local": "kept local (not pushed)",
            "rejected": f"push to {push['remote']} refused: {push['error']}", "skipped": "no push stage"}[push["result"]]


def result_note(ctx, st):
    """KEY-result.md: what the ticket set out to do and what it ended with."""
    key, p = st["key"], note_paths(ctx.vault, st["key"])
    plan = read(plan_copy(ctx.q, key)) or read(p["plan"])
    goal = sections(plan).get("goal", "").strip()
    ticket = read(p["ticket"])
    if not goal:
        m = re.search(r"(?m)^#\s+(.+)$", ticket)
        goal = m.group(1).strip() if m else ""
    criteria = check_plan(plan)[1] if plan.strip() else ticket_criteria(ticket)
    push, stat = st["push"], st["diffstat"]
    where = where_of(push)
    lines = ["---", "type: note", f"ticket: {key}", f"summary: {yq(f'harness queue result of {key}: {where}')}", "---",
             "", f"# {key}: result", "", "## Goal", "", goal or "(not written)", "", "## Acceptance criteria", ""]
    lines += [f"- {c}" for c in criteria] or ["(none)"]
    lines += ["", "## Branch", ""]
    for x in st.get("pushes") or ([push] if push["branch"] else []):
        r = "" if x.get("repo", ".") == "." else f" in {x['repo']}"
        lines += [f"- branch: `{x['branch']}`{r}" + (f" at `{x['sha'][:12]}`" if x["sha"] else ""), f"- {where_of(x)}"]
        if x["url"]:
            lines.append(f"- remote: {x['url']}")
    for rel in st.get("left_uncommitted") or []:
        lines.append(f"- left uncommitted in {'the project' if rel == '.' else rel}")
    if st.get("compare_url"):
        lines.append(f"- open a pull request: {st['compare_url']}")
    if st.get("orig_branch") or st.get("folder_branch_now"):
        lines.append(f"- the project folder is on `{st.get('folder_branch_now') or '?'}`"
                     + (f" (it was on `{st['orig_branch']}`)" if st.get("orig_branch") else ""))
    lines += ["", "## Commits", ""]
    lines += [f"- `{c['sha'][:12]}` {c['subject']}" for c in st["commits"]] or ["(none)"]
    lines += ["", "## Diffstat", "", f"{stat['files']} file(s), +{stat['insertions']} -{stat['deletions']}",
              "", "## Review notes", ""]
    notes = [f"- must fix (asked by the review): {m}" for m in st.get("must_fix") or []]
    notes += [f"- warning: {w}" for w in st.get("warnings") or []]
    if os.path.exists(p["review"]):
        notes.append(f"- the review: [[{key}-review]]")
    lines += notes or ["(none)"]
    write_atomic(p["result"], "\n".join(lines) + "\n")


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
    st.pop("verify_only", None)
    st.pop("session", None)   # the next stage's agent is a new one
    st.pop("resume", None)
    if stage:
        st.update(stage=stage, attempt=0, feedback="", status="running")
    else:
        if not reason and "push" not in stages_of(st):
            reason = f"its stages are done ({','.join(stages_of(st))}); nothing pushed"
        st.update(stage="done", attempt=0, feedback="", status="done", reason=reason)
        ctx.q.log(st["key"], f"done: {reason}" if reason else "done")
        if st.get("base_sha"):   # what the stages left, with nothing pushed
            change = None
            with contextlib.suppress(Exception):
                change = ticket_change(ctx.q, st)
            pushes = [{"remote": "", "url": "", "branch": p["branch"], "sha": p["sha"], "result": "skipped",
                       "error": "", "repo": p["repo"]} for p in (change[4] if change else []) if p["kind"] == "branch"]
            if not st.get("no_branch") and not any(p["repo"] == "." for p in pushes):
                pushes.insert(0, {"remote": "", "url": "", "branch": branch_of(st), "sha": "", "result": "skipped",
                                  "error": "", "repo": "."})
            results(ctx, st, change, pushes or [{"remote": "", "url": "", "branch": "", "sha": "",
                                                 "result": "skipped", "error": "", "repo": "."}])
    ctx.q.save(st)


def block(ctx, st, reason, keep=False):
    """Blocked for the owner; keep: the agent's result stays to be checked again
    (verify_only) by a retry without --stage."""
    if not keep:
        st.pop("verify_only", None)
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
    if rc == IDLE:
        problems = [f"the agent made no progress for {getattr(ctx, 'idle_min', 0):g} min and was stopped"] + problems
    elif rc not in (0, None):
        problems = [f"the agent launch exited with status {rc}"] + problems
    try:
        probe(ctx)  # raises Infra: the ticket stays at this stage, the attempt is not counted
    except Infra:   # the agent's work may be what the outage cut short: it runs again after
        st.pop("verify_only", None)
        ctx.q.save(st)
        raise
    crash = crash_of(st, rc)
    if crash and go_on_after_crash(ctx, st, crash):
        return
    st.pop("verify_only", None)
    text = "\n".join("- " + p for p in problems)
    ctx.q.log(st["key"], f"{st['stage']} attempt {st['attempt'] + 1} failed", text)
    record(st, "failed", problems)
    if st["attempt"] < ctx.retries:
        st.update(attempt=st["attempt"] + 1, feedback=text)
        if crash and st["stage"] in RESUMABLE:   # its session stopped again: a new agent, told to finish the work
            st.pop("session", None)
            st["resume"] = {"stage": st["stage"], "why": crash, "message": ""}
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
    if not os.path.isdir(st["project"]):
        return block(ctx, st, f"the project folder {st['project']} is not there (moved, renamed, or a drive not "
                              f"mounted); put it back, then harness queue retry {key}")
    step(ctx, "fetching the ticket")
    r = run_quiet([BASH, ctx.harness, "queue", "_pull", key], PULL_TIMEOUT)
    if r.returncode != 0:
        ctx.q.log(key, f"warning: the ticket was not fetched from the tracker (harness queue _pull: "
                       f"{(r.stderr or r.stdout).strip()[-300:] or 'exit ' + str(r.returncode)}); the vault's copy is used")
    ticket = note_paths(ctx.vault, key)["ticket"]
    if not read(ticket).strip():
        return block(ctx, st, f"there is no ticket text for {key}: the tracker did not give it (no tracker set up, "
                              f"or no access: harness ticket login, then harness ticket pull {key}) and {ticket} "
                              f"is empty or missing. Pull it, or write the ticket into that file yourself, then "
                              f"harness queue retry {key}")
    mode = "container" if not (ctx.host or ctx.single) else "host"
    if WINDOWS and not st.get("in_place") and st.get("base_sha") and st.get("clone_mode", mode) != mode:
        # a clone made for the other mode has the other line endings: Linux git in the
        # container would see every file of a CRLF checkout as changed
        if st["stage"] != first_stage(st) or st.get("verify_only"):
            return block(ctx, st, f"its clone was made for a {st['clone_mode']} run and this run is a {mode} one: "
                                  f"run the queue {'with' if st['clone_mode'] == 'host' else 'without'} --host, "
                                  f"or harness queue retry {key} --stage {first_stage(st)} to start it again here")
        st.pop("base_sha", None)   # nothing done in it yet: cloned again for this mode
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
    while st["status"] == "running":
        checkpoint(ctx)
        beat(ctx, ticket=key, stage=st["stage"], step="")
        ctx.q.log(key, f"{st['stage']} (attempt {st['attempt'] + 1}, {ctx.model})")
        try:
            if st.get("in_place"):   # what the uncommitted repositories hold before the work: their change is since
                take_baselines(ctx.q, st, st["stage"], (ctx.vault, ctx.q.root))
            STAGES[st["stage"]](ctx, st)
        except VerifierBroke as e:   # the work was not judged: kept for a retry that asks again
            stage = st["stage"]
            block(ctx, st, f"verifier failed at {stage}: {e}. The agent's work is kept: harness queue retry {key} "
                           f"asks the verifier again without running the agent (--stage {stage} runs it again)",
                  keep=True)


def beat(ctx, **change):
    """DIR/run.json, what the run is doing and when it last said so (the viewer
    takes a beat older than 30 s for a run that is gone)."""
    with ctx.beat_lock:
        if ctx.beating.is_set():   # the run has ended and run.json is gone
            return
        ctx.run_info.update(change, beat=int(time.time()))
        try:
            write_atomic(ctx.run_file, json.dumps(ctx.run_info) + "\n")
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
        go_on_later(st, "the queue run was halted")
        st.update(status="queued")
        ctx.q.log(key, f"halted by the owner: back in the queue at {st['stage']}")
    else:
        record(st, "stopped", ["stopped by the owner"])
        st.update(status="stopped", reason="stopped by the owner")
        ctx.q.log(key, f"stopped by the owner at {st['stage']}; harness queue continue {key} \"message\" goes on "
                       f"in the agent's session, harness queue retry {key} starts the stage again")
    ctx.q.save(st)
    if e.kind == "stop":
        try:
            os.remove(ctx.q.sentinel(key))
        except OSError:
            pass


def cmd_run(q, a):
    if a.slot:   # a slot of a parallel run: the run that started it holds run.lock
        return run_locked(q, a, time.time())
    lock = open(os.path.join(q.root, "run.lock"), "a")
    if not try_lock(lock):
        lock.close()
        print("[queue] another 'harness queue run' is running", file=sys.stderr)
        return 1
    locked_at = time.time()
    try:   # released here, not whenever the file object is collected (an exception's frames can hold it)
        return (run_parallel if a.parallel > 1 else run_locked)(q, a, locked_at)
    finally:
        unlock(lock)
        lock.close()


def run_locked(q, a, locked_at):
    ctx = argparse.Namespace(q=q, vault=os.path.abspath(a.vault), proxy=a.proxy, harness=a.harness, host=a.host,
                             single=a.single_message, retries=a.retries, push=a.push == 1, remote=a.remote,
                             plan_review=a.plan_review == 1, stage_timeout=a.stage_timeout, child=None, key="",
                             beat_lock=threading.Lock(), beating=threading.Event(), outage_wait=max(0, a.outage_wait),
                             idle_min=max(0.0, a.idle_min), watch=[w for w in a.watch if w], outage={},
                             slot=a.slot, parent=os.getppid() if a.slot else 0,
                             run_file=os.path.join(q.root, f"run-{a.slot}.json" if a.slot else "run.json"))
    ctx.code = code_sig()
    ctx.model = a.model or resolve_model(a.proxy, a.default_model)
    if not ctx.model:
        print("[queue] no model: set QUEUE_MODEL or DEFAULT_MODEL_NAME", file=sys.stderr)
        return 1
    ctx.verify_model = (a.verify_model or ctx.model).split("/", 1)[-1]
    if not a.slot:   # a parallel run did these for its slots
        new_run(q, ctx.vault, locked_at)
    ctx.run_info = {"pid": os.getpid(), "started": now_iso(), "beat": 0, "host": bool(ctx.host or ctx.single),
                    "single": bool(ctx.single), "model": ctx.model, "ticket": "", "stage": "", "step": ""}
    with beating_run(ctx):
        return run_tickets(q, ctx)


def new_run(q, vault, locked_at):
    at = mtime(q.sentinel())
    if at is not None and at < locked_at:   # a halt asked of a run that had already ended,
        drop(q.sentinel())                  # not one asked of this run as it started
    keep_tasks_out(q, vault)
    for info in slot_runs(q, all_files=True):   # a killed parallel run's
        drop(os.path.join(q.root, f"run-{info['slot']}.json"))


@contextlib.contextmanager
def beating_run(ctx):
    """ctx.run_file beating every BEAT seconds while the run goes, and gone after."""
    beat(ctx)

    def beats():
        while not ctx.beating.wait(BEAT):
            beat(ctx)
    threading.Thread(target=beats, daemon=True).start()
    try:
        yield
    finally:
        ctx.beating.set()
        with ctx.beat_lock:
            try:
                os.remove(ctx.run_file)
            except OSError:
                pass


def run_parallel(q, a, locked_at):
    """--parallel N: N slots, each a run in a process of its own (run --slot I) that
    takes the queue's tickets one at a time; this process holds run.lock and beats
    run.json for them. It ends when they all have: Ctrl-C reaches them too (each puts
    its ticket back), a halt ends each at its next check, and after a harness upgrade
    each ends after its ticket, and the run starts again on the new code (RESTART)."""
    model = a.model or resolve_model(a.proxy, a.default_model)
    if not model:
        print("[queue] no model: set QUEUE_MODEL or DEFAULT_MODEL_NAME", file=sys.stderr)
        return 1
    new_run(q, os.path.abspath(a.vault), locked_at)
    tidy(q)
    ctx = argparse.Namespace(q=q, beat_lock=threading.Lock(), beating=threading.Event(),
                             run_file=os.path.join(q.root, "run.json"))
    ctx.run_info = {"pid": os.getpid(), "started": now_iso(), "beat": 0, "host": bool(a.host or a.single_message),
                    "single": bool(a.single_message), "model": model, "ticket": "", "stage": "", "step": "",
                    "parallel": a.parallel}
    argv = [sys.executable, os.path.abspath(__file__), *a.argv[1:], "--model", model]
    kids = []
    with beating_run(ctx):
        try:
            for i in range(1, a.parallel + 1):
                kids.append(subprocess.Popen([*argv, "--slot", str(i)]))
            rcs = [k.wait() for k in kids]
            drop(q.sentinel())   # a halt, seen by every slot
        except KeyboardInterrupt:   # the slots have it too: wait for each to put its ticket back
            for k in kids:
                with contextlib.suppress(KeyboardInterrupt):
                    k.wait()
            print("\n[queue] interrupted; 'harness queue run' carries on from the same stage", file=sys.stderr)
            return 130
    return next((rc for rc in (130, RESTART, 3) if rc in rcs), max(rcs, default=0))


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


PARKED = ("plan-review", "needs-answers", "blocked", "stopped", "done")


def take(ctx, key, pending, done):
    """Under queue.lock: the ticket in hand (run_holds) and running. (st, orphan):
    orphan, a ticket some run left running with its agent maybe still going.
    st None: not this one (taken, removed, or one this run cannot do)."""
    q = ctx.q
    with held(os.path.join(q.root, "queue.lock")):   # owner commands change a ticket under it
        if not pending(key) or key not in q.order():
            return None, False
        st = q.load(key)
        why = elsewhere(ctx, st)
        if why:   # not this slot's to take; it waits for the other to be done
            ctx.waiting = why
            return None, False
        if WINDOWS and st.get("in_place") and not (ctx.host or ctx.single):
            # the container's Linux git would see every file of a CRLF checkout as changed
            done[key] = st.get("requeued", "")
            why = ("waits for a run on the host: working in the project folder on Windows needs it (harness queue "
                   "run --host, or Start the queue with host); or remove it and add it again with --clone")
            if st.get("reason") != why or st.get("status") != "queued":
                st.update(status="queued", reason=why)
                q.save(st)
                q.log(key, why)
            return None, False
        orphan = bool(st.get("agent"))   # only a launch sets it; a run that is gone left it
        ctx.key = key
        beat(ctx, ticket=key, stage=st["stage"], step="")   # in hand (run_holds) before it is running
        st.update(status="running", vault=ctx.vault)
        q.save(st)
    done[key] = st.get("requeued", "")
    return st, orphan


def elsewhere(ctx, st):
    """A slot of a parallel run: why another slot keeps it off this ticket (that slot
    has it, or a ticket that works in the same folder: in place, the project folder
    is on one ticket's branch at a time), or ""."""
    if not getattr(ctx, "slot", 0):
        return ""
    for r in slot_runs(ctx.q):
        other = r.get("ticket")
        if r["slot"] == ctx.slot or not other:
            continue
        if other == st["key"]:
            return f"{other} is on slot {r['slot']}"
        o = ctx.q.load(other)
        if o and o.get("project") and overlaps(workdir(ctx.q, st), workdir(ctx.q, o)):
            return f"waiting for {other} (slot {r['slot']}): it works in {workdir(ctx.q, o)}"
    return ""


def overlaps(a, b):
    """The two folders are one, or one is inside the other."""
    a, b = (os.path.normcase(os.path.realpath(x)) for x in (a, b))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)


def tidy(q):
    """At the start of a run: what a run or a remove that died left behind (a
    removed ticket's folder, the in-place exclude block of a stage cut short)."""
    for name in os.listdir(q.root):
        if name.startswith(".trash-"):
            rmtree(os.path.join(q.root, name))
    for project in {s["project"] for s in map(q.load, q.order()) if s and s.get("in_place") and s.get("project")}:
        if os.path.isdir(project) and not any(run_holds(q, s["key"]) for s in map(q.load, q.order()) if s):
            with contextlib.suppress(RuntimeError, OSError, subprocess.SubprocessError):
                exclude_block(project, None)


def run_tickets(q, ctx):
    done = {}   # KEY: its requeued stamp when this run took it; answered or retried since, it is taken again

    def pending(k):
        s = q.load(k) or {}
        return s.get("status") in ("queued", "running") and done.get(k) != s.get("requeued", "")
    if not ctx.slot:   # a parallel run tidied for its slots
        tidy(q)
    try:
        while True:
            ctx.key = ""
            beat(ctx, ticket="", stage="", step="")
            checkpoint(ctx)
            if getattr(ctx, "code", "") and code_sig() != ctx.code:
                print("[queue] harness was updated; the run starts again on the new code", file=sys.stderr)
                return RESTART
            todo = [k for k in q.order() if pending(k)]
            if not todo:
                break
            ctx.waiting = ""
            try:
                for k in todo:
                    st, orphan = take(ctx, k, pending, done)
                    if st is not None:
                        break
            except Busy:   # an owner command holds it; it lets go within seconds
                step(ctx, "waiting for the queue's lock")
                pause(ctx, POLL)
                continue
            if st is None:
                if ctx.waiting:   # what is left is another slot's, or in its folder
                    step(ctx, ctx.waiting)
                    pause(ctx, POLL)
                continue
            key = st["key"]
            if orphan:   # a run that is gone left its agent: ended before this one starts
                reap(q, st, ctx.harness, "the ticket is taken again")
                st.pop("agent", None)
                go_on_later(st, "the queue run working on it ended while the agent worked")
                q.save(st)
            outage = None
            try:
                process(ctx, st)
            except Interrupted as e:
                interrupted(ctx, st, e)
                if e.kind == "halt":
                    put_away(q, st)
                    sync_vault(ctx, key, int(time.time() * 1000))
                    raise
            except Infra as e:   # the ticket goes back; the run waits for the upstream and carries on
                n = st.get("launch_fails", 0) + 1 if isinstance(e, LaunchFailed) else 0
                if n >= LAUNCH_TRIES:   # not passing: something about this ticket's launch
                    st.pop("launch_fails", None)
                    block(ctx, st, f"{str(e)[:300]} ({n} times in a row; check that 'harness -p' runs in "
                                   f"{st['project']}, then harness queue retry {key})")
                else:
                    if n:
                        st["launch_fails"] = n
                    else:   # the outage may have cut the agent short: the stage goes on in its session
                        go_on_later(st, "the upstream stopped answering")
                    put_back(ctx, st, f"the upstream does not answer ({str(e)[:200]}); the run waits and carries on"
                             if not n else f"{str(e)[:300]}; the run waits and carries on")
                    done.pop(key, None)
                    outage = e
            except KeyboardInterrupt:
                go_on_later(st, "the queue run was interrupted (Ctrl-C)")
                put_back(ctx, st, "the run was interrupted (Ctrl-C); start the queue again to carry on")
                put_away(q, st)
                raise
            except Exception as e:  # noqa: BLE001 - one ticket's failure must not stop the queue
                import traceback
                try:
                    q.log(key, "error", traceback.format_exc())
                    block(ctx, st, f"the queue hit an error of its own ({type(e).__name__}: {str(e)[:200]}); the "
                                   f"details are in harness queue log {key}; harness queue retry {key} tries again")
                except Exception:  # noqa: BLE001 - nor its handling
                    traceback.print_exc()
            finally:   # a stop asked too late for this go is not for a later one
                drop(q.sentinel(key))
            if st.get("status") in PARKED:
                put_away(q, st)
                # done with it: an owner's continue, retry or remove need not wait out the vault save
                beat(ctx, ticket="", stage="", step="")
            sync_vault(ctx, key, int(time.time() * 1000))   # the outcome, into the vault
            if outage is not None:
                ctx.key = ""
                wait_out(ctx, outage)
    except Interrupted:   # halt: between tickets, or the ticket above went back to the queue
        if not ctx.slot:   # the other slots are to see it too: their run drops it
            drop(q.sentinel())
        print("[queue] halted by the owner; 'harness queue run' carries on from the same stage", file=sys.stderr)
        return 0
    except Infra as e:
        print(f"[queue] stopped: {str(e)[:300]}. The ticket keeps its place; check 'harness unlock' and "
              "'harness usage', then run 'harness queue run' again.", file=sys.stderr)
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
    if a.no_branch and (a.branch or a.start or a.ref or a.base or a.clone):
        print("[queue] --no-branch leaves the change in the project uncommitted: --branch, --start, --base and "
              "--clone do not go with it", file=sys.stderr)
        return 1
    repos, why = repo_branches(project, a.repo_branch, a.allow_branch)
    if why:
        print(f"[queue] {why}", file=sys.stderr)
        return 1
    if repos and not in_place:
        print("[queue] --repo-branch works in place, not in a clone", file=sys.stderr)
        return 1
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
        start_sha = sha
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
    if a.branch and len({k.upper() for k in a.keys}) > 1:
        print(f"[queue] --branch {a.branch} with {len(a.keys)} tickets: each would end on (and push) the same "
              "branch. Add them one at a time, or leave --branch out (each gets its own)", file=sys.stderr)
        return 1
    shared = shared_branches(project, base)
    if a.branch and a.branch in shared and not a.allow_branch:
        print(f"[queue] --branch {a.branch} is {shared[a.branch]}: the queue would commit (and push) there "
              "directly. Pick a branch of its own, or give --allow-branch to mean it", file=sys.stderr)
        return 1
    if a.note is not None and not a.note.strip():
        print("[queue] --note is empty", file=sys.stderr)
        return 1
    rc, lost = 0, {}
    if start:   # the branch's tips here and on each remote: fetched, so not under the lock
        for key in {k.upper() for k in a.keys if KEY_RE.match(k.upper())}:
            lost[key] = lost_by(project, a.branch or key, start_sha)
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
            st.update({k: v for k, v in (("start", "" if a.no_branch else start), ("branch", a.branch),
                                         ("in_place", in_place), ("plan_review", not a.no_plan_approval),
                                         ("no_branch", a.no_branch), ("repos", repos),
                                         ("take_defaults", a.take_defaults)) if v})
            if lost.get(key) and not a.no_branch:
                print(f"[queue] {key}: branch {branch_of(st)} has commits that {start_of(st)} does not "
                      f"({', '.join(n for n, _ in lost[key])}): the work would end over them. Start from it "
                      f"(--start {lost[key][0][0]}) or pick another --branch", file=sys.stderr)
                rc = 1
                continue
            if a.note:
                q.add_note(key, a.note)
            q.save(st)
            order.append(key)
            frm = start_of(st)
            frm = frm[:12] if re.fullmatch(r"[0-9a-f]{40}", frm) else frm[:40]
            how = (f"in {project} itself" if in_place else f"in a clone of {project}") \
                + (", its change left uncommitted" if a.no_branch else f", from {frm} on branch {branch_of(st)}") \
                + "".join(f"; {r} on branch {v['branch'] or key} (from {v['base']})" for r, v in sorted(repos.items()))
            plan = "" if "plan" not in stages else "; it is done once the plan is written" if stages == ["plan"] \
                else f"; the plan waits for you to approve it (harness queue approve {key})" if not a.no_plan_approval \
                else f"; {stages[1]} starts after the plan without asking"
            print(f"[queue] {key}: queued ({how}; stages {','.join(stages)}{plan})")
        q.set_order(order)
    return rc


def repo_branches(project, specs, allow_branch=False):
    """({REL: {"branch", "base"}}, why not): --repo-branch REL[=BRANCH][:TARGET], each a
    repository inside the project to end on a branch (empty: the ticket's key) made from
    TARGET (default: the branch it is on now, so the work starts where it is)."""
    out = {}
    for spec in specs:
        m = re.fullmatch(r"([^=:]+?)/?(?:=([^:]*))?(?::(.+))?", spec.strip())
        if not m:
            return {}, f"--repo-branch {spec}: give REL[=BRANCH][:TARGET]"
        rel, branch, target = m.group(1).replace("\\", "/").strip("/"), (m.group(2) or "").strip(), \
            (m.group(3) or "").strip()
        d = os.path.join(project, rel)
        if rel in ("", ".") or rel.startswith("../") or "/../" in f"/{rel}/" or os.path.isabs(rel):
            return {}, f"--repo-branch {spec}: {rel or '(empty)'} is not a folder inside {project}"
        top = git(d, "rev-parse", "--show-toplevel", check=False).stdout.strip() if os.path.isdir(d) else ""
        if not top or os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(d)):
            return {}, f"--repo-branch {spec}: {d} is not a repository of its own (its own .git)"
        if branch and git(d, "check-ref-format", "--branch", branch, check=False).returncode != 0:
            return {}, f"--repo-branch {spec}: not a branch name: {branch}"
        if not target:
            target = git(d, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() or \
                git(d, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout.strip()
        if not target or not resolve_ref(d, target):
            return {}, f"--repo-branch {spec}: {target or 'HEAD'} is not a commit in {rel} (git fetch it first?)"
        if branch and not allow_branch and branch in ("main", "master", target, re.sub(r"^[^/]+/", "", target)):
            return {}, (f"--repo-branch {spec}: the queue would commit (and push) on {branch} in {rel} directly. "
                        "Pick a branch of its own, or give --allow-branch to mean it")
        if rel in out:
            return {}, f"--repo-branch {rel} given twice"
        out[rel] = {"branch": branch, "base": target}
    return out, ""


def shared_branches(project, base):
    """{branch: what it is}: the branches a ticket's work does not end on unless
    the owner means it (--allow-branch): the base, main, master, each remote's default."""
    out = {"main": "a main branch", "master": "a main branch"}
    remotes = git(project, "remote", check=False).stdout.split()
    for r in remotes:
        d = git(project, "symbolic-ref", "-q", "--short", f"refs/remotes/{r}/HEAD", check=False).stdout.strip()
        if d.startswith(r + "/"):
            out[d[len(r) + 1:]] = f"{r}'s default branch"
    names = [base] + [base[len(r) + 1:] for r in remotes if base.startswith(r + "/")]
    for n in names:
        out[n] = "the base the work starts from"
    return out


NEXT = {"needs-answers": "harness queue answer {k}   (or --go: take the plan's defaults)",
        "plan-review": "harness queue approve {k}   (or harness queue answer {k} \"feedback\": plan again)",
        "blocked": "read why above (harness queue log {k}), then harness queue continue {k} \"what to do\"   "
                   "(or harness queue retry {k}: the stage again)",
        "stopped": "harness queue continue {k}   goes on in the agent's session   (harness queue retry {k}: the "
                   "stage again)"}


def print_list(q, hints=True):
    keys = q.order()
    if not keys:
        print("the queue is empty (harness queue add KEY...)")
        return
    print(f"{'TICKET':<14} {'STATUS':<14} {'STAGE':<10} NOTE")
    sts = [q.load(k) or {"key": k} for k in keys]
    for st in sts:
        note = st.get("reason") or (f"attempt {st.get('attempt', 0) + 1}" if st.get("status") == "running" else "")
        print(f"{st['key']:<14} {st.get('status', '?'):<14} {st.get('stage', '?'):<10} {note}")
    if not hints:
        return
    waits = [(st["key"], NEXT[st["status"]]) for st in sts if st.get("status") in NEXT]
    if waits:
        print("\nwaiting on you:")
        for k, what in waits:
            print(f"  {k:<12} " + what.format(k=k))
    if any(st.get("status") in ("queued", "running") for st in sts) and not run_going(q):
        print("\nno queue run is going: harness queue run (or Start the queue in harness serve) works the queued tickets")


def cmd_list(q, a):
    print_list(q)
    return 0


def reap_stranded(q, key, harness):
    """Before an owner command changes a ticket a run left running (no run holds
    it): its agent, if still going, is ended. Outside queue.lock (it can take a
    minute); the command then drops st["agent"] under the lock."""
    st = q.load(key) or {}
    if st.get("agent") and not run_holds(q, key):
        reap(q, st, harness, "the owner changed the ticket")


FINISHING = "the run is finishing up its last stage; try again in a few seconds"


def cmd_remove(q, a):
    rc = 0
    for raw in a.keys:
        reap_stranded(q, raw.upper(), getattr(a, "harness", ""))
    trash = []
    with held(os.path.join(q.root, "queue.lock")):
        order = q.order()
        for raw in a.keys:
            key = raw.upper()
            if key not in order and not q.load(key):
                print(f"[queue] {key} is not in the queue", file=sys.stderr)
                rc = 1
                continue
            st = q.load(key) or {}
            if run_holds(q, key):   # running, or just stopped for the owner and the run still putting it away
                print(f"[queue] {key}: " + (f"is running; stop it first (harness queue stop {key})"
                                            if st.get("status") == "running" else FINISHING), file=sys.stderr)
                rc = 1
                continue
            order = [k for k in order if k != key]
            # out of the way at once; the slow delete (a clone's files) after the lock
            gone = os.path.join(q.root, f".trash-{key}-{int(time.time() * 1000)}")
            try:
                if os.path.isdir(q.dir(key)):
                    os.rename(q.dir(key), gone)
                    trash.append((key, gone))
            except OSError as e:
                print(f"[queue] {key}: {e}; remove {q.dir(key)} by hand", file=sys.stderr)
            print(f"[queue] {key} removed" + removed_note(q, st, order))
        q.set_order(order)
    for key, gone in trash:
        err = rmtree(gone)
        if err:
            print(f"[queue] {key}: {err}; the next run removes it, or remove {gone} by hand", file=sys.stderr)
    return rc


def removed_note(q, st, order):
    """What a removed ticket leaves in the project. In place: its branch, and the
    folder still on it when a run that died left it there; the uncommitted-files
    block in .git/info/exclude goes, unless another ticket of the folder is mid-stage."""
    project = st.get("project", "")
    if not st.get("in_place"):
        return f" (its work, if any, stays in the project as refs/harness-queue/{st.get('key', '')})"
    if not project or not os.path.isdir(project):
        return ""
    branch = branch_of(st)
    if not any(run_holds(q, k) for k in order if (q.load(k) or {}).get("project") == project):
        with contextlib.suppress(RuntimeError, OSError, subprocess.SubprocessError):
            exclude_block(project, None)
    on = git(project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    if on == branch:
        back = st.get("orig_branch") or "your branch"
        return f"; {project} is still on its branch {branch}: git switch {back} to go back"
    return f" (its branch {branch}, if any, stays in {project})"


RESULTS = ("push", "compare_url", "commits", "diffstat", "folder_branch_now", "launch_fails", "agent")


def requeue(q, key, want, stage=None, fresh=False, go_on=False, **extra):
    """Under queue.lock: the ticket queued again (at stage), counters and the last
    go's outcome cleared; fresh (or from the plan): its work too, made again by the run.
    go_on (harness queue continue): its stage goes on from the work as it is."""
    st = q.load(key.upper())
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return None
    if st["status"] not in want:
        print(f"[queue] {st['key']} is {st['status']}, not {' or '.join(want)}", file=sys.stderr)
        return None
    st.update(status="queued", attempt=0, feedback="", reason="", requeued=now_iso(), **extra)
    st.pop("stopped_from", None)
    st.pop("resume", None)   # a cut-off stage goes on only when continued (the caller sets it again)
    for k in RESULTS:
        st.pop(k, None)
    if st.get("vault"):   # the last go's summary is not this one's
        drop(note_paths(st["vault"], st["key"])["result"])
    try:
        os.remove(q.sentinel(st["key"]))   # a stop asked before is not for this go
    except OSError:
        pass
    kept = st.get("verify_only")   # a plan the verifier gave no verdict on is checked again, not made again
    kept = kept if (not stage and not fresh and isinstance(kept, dict) and kept.get("stage") == st["stage"] == "plan"
                    and kept.get("out")) else None
    if stage:
        st["stage"] = stage
    if stage or fresh or go_on:   # the agent runs again; only a plain retry checks the kept result again
        st.pop("verify_only", None)
        st.pop("verified_sha", None)
        st.pop("verified_repos", None)
    if not go_on and starts_over(q, st, fresh=fresh):  # from the start, not earlier commits
        for k in ("sha", "review_sha", "review_digest", "must_fix", "warnings", "verify_only", "verified_sha",
                  "verified_repos"):
            st.pop(k, None)
        if st["stage"] == "plan":
            for k in ("questions", "criteria", "plan_sig"):
                st.pop(k, None)
            if os.path.exists(plan_copy(q, st["key"])):
                os.remove(plan_copy(q, st["key"]))
        st.pop("base_sha", None)   # the run clones (or sets up) again; no delete under the lock
        if kept:
            st["verify_only"] = kept
    q.save(st)
    return st


def starts_over(q, st, stage=None, fresh=False):
    """True when going again at stage (None: where it is) sets the work up again
    from its start (the run clones, or sets up in place, again): fresh, from the
    plan, or its work is gone."""
    return fresh or (stage or st["stage"]) == "plan" or not has_work(q, st)


def has_work(q, st):
    """The ticket's work so far is there: its clone, or (in place) the project set up."""
    return bool(st.get("base_sha")) and (bool(st.get("in_place")) or os.path.isdir(os.path.join(q.dir(st["key"]), "repo")))


def commits_since_base(q, st):
    """How many commits the ticket's branch has on its base (where the agents
    commit: the clone, or the project in place); 0 when not known."""
    r = git(workdir(q, st), "rev-list", "--count", f"{st['base_sha']}..refs/heads/{branch_of(st)}", check=False)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0


def cmd_approve(q, a):
    with held(os.path.join(q.root, "queue.lock")):
        st = requeue(q, a.key, ("plan-review",), plan_approved=True, plan_hold=False)
    if st:
        print(f"[queue] {st['key']}: plan approved; the queue run implements it when it gets to it")
    return 0 if st else 1


RETRYABLE = ("blocked", "needs-answers", "stopped", "done")


def plain_retry(q, st):
    """(stage, fresh): where a retry without --stage queues the ticket. Where it was
    (None); a plan the verifier gave no verdict on is kept and checked again; done,
    or its work gone, from the start on a fresh clone."""
    if st["status"] != "done" and st.get("stage") == "plan" and (st.get("verify_only") or {}).get("stage") == "plan":
        return None, False
    if st["status"] == "done" or st.get("stage") not in ALL_STAGES or not has_work(q, st):
        return first_stage(st), True
    return None, False


def lost_over(q, st, fetch=True, seen=None):
    """The tips of the ticket's branch that setting its work up again from its start
    would throw away, so ends_on refuses it (a pushed branch is never moved back);
    [] when none, or when the project or start is not there to tell (setup says why).
    seen: a dict kept across calls for one ticket, so the branch is looked at once."""
    if seen is not None and "lost" in seen:
        return seen["lost"]
    project, lost = st.get("project") or "", []
    if os.path.isdir(project) and not st.get("no_branch"):
        start = commit_of(project, start_of(st), fetch)
        if start:
            lost = lost_by(project, branch_of(st), start, fetch)
            lost = [] if own_branch(st, lost) else lost
    if seen is not None:
        seen["lost"] = lost
    return lost


def retry_refused(q, st, stage=None, fetch=True, seen=None):
    """Why the ticket cannot go again at stage (--stage; None: a plain retry), or "".
    Not fetch: the remotes' branches as last fetched (the run's page asks often);
    seen: as lost_over's, for asking of several stages."""
    key = st["key"]
    seen = {} if seen is None else seen
    held_plan = st["status"] == "plan-review" or (st["status"] == "stopped" and st.get("stopped_from") == "plan-review")
    fresh, how = False, f"--stage {stage}"
    if stage is None:
        if st["status"] == "queued":
            return f"{key} is queued already; 'harness queue run' takes it (--stage S sends it to another stage)"
        if st["status"] == "plan-review":
            return f"{key}'s plan waits to be read: harness queue approve {key} goes on, --stage plan makes it again"
        if held_plan:
            return ""   # back to plan-review: nothing is set up again
        (stage, fresh), how = plain_retry(q, st), "a retry"
    elif held_plan and stage != "plan":
        return f"{key}'s plan waits to be read: harness queue approve {key} goes on, --stage plan makes it again"
    elif stage not in (first_stage(st), "plan"):
        stages = stages_of(st)
        if "plan" in stages and ALL_STAGES.index(stage) > 0 and not os.path.exists(plan_copy(q, key)):
            return f"{key} has no checked plan yet, so {stage} has nothing to work from: --stage plan"
        if stage == "fix" and not st.get("must_fix"):
            return f"{key}'s review has no Must fix items, so fix has nothing to do: --stage review"
        if stage in ("review", "fix", "push"):
            if not has_work(q, st):
                return f"{key}'s work is gone (or was never made), so {stage} has nothing to take: --stage {first_stage(st)}"
            many = st.get("no_branch") or st.get("nested") or st.get("baselines")
            if "commits" not in seen:
                try:
                    seen["commits"] = bool(ticket_change(q, st)) if many else commits_since_base(q, st)
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    seen["commits"] = True   # not known: the stage itself says what is missing
            if not seen["commits"] and many:
                return f"{key} has changed nothing yet, so {stage} has nothing to take: --stage implement"
            if not seen["commits"]:
                return f"{key}'s branch {branch_of(st)} has no commits yet, so {stage} has nothing to take: --stage implement"
    if not starts_over(q, st, stage, fresh):
        return ""   # goes on from the work: its branch only moves forward
    lost = lost_over(q, st, fetch, seen)
    if not lost:
        return ""
    names = ", ".join(f"{n} at {t[:12]}" for n, t in lost)
    then = ("--stage implement (or a later stage) goes on from the work instead" if has_work(q, st) else
            f"delete it (here and on the remote, then git fetch --prune), or remove {key} and add it again "
            f"with --start {lost[0][0]} or another --branch")
    return (f"{key}'s branch {branch_of(st)} has commits that {start_of(st)} does not ({names}); {how} starts "
            f"the work over from {start_of(st)}, and the queue never ends it over them: {then}")


def retry_choices(q, st, fetch=False):
    """{stage: why not, or ""} for a retry at each stage, None the plain retry: what
    the run's page offers. Not fetched by default: the page asks on every refresh."""
    seen = {}
    return {s: retry_refused(q, st, s, fetch, seen) for s in (None,) + ALL_STAGES}


def cmd_retry(q, a):
    key = a.key.upper()
    st = q.load(key)
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return 1
    reap_stranded(q, key, getattr(a, "harness", ""))
    st = q.load(key) or st
    why = retry_refused(q, st, a.stage)   # fetched, as setup will be: not under the lock
    if why:
        print(f"[queue] {why}", file=sys.stderr)
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
        with held(os.path.join(q.root, "queue.lock")):
            st = q.load(key)
            if st.get("status") != "stopped":
                print(f"[queue] {key} is {st.get('status')} now; nothing done", file=sys.stderr)
                return 1
            st.update(status="plan-review", reason="plan ready to read; stopped and resumed", requeued=now_iso())
            st.pop("stopped_from", None)
            q.save(st)
        print(f"[queue] {key}: back to plan-review; harness queue approve {key} goes on")
        return 0
    else:
        stage, fresh = plain_retry(q, st)   # done, or its work is gone: from the start, on a fresh clone
        if fresh:
            extra["fresh"] = True
    # also a ticket left running by a run that ended (or is stuck elsewhere), and a
    # queued one sent back to an earlier stage; under the lock the run takes tickets under
    with held(os.path.join(q.root, "queue.lock")):
        if run_holds(q, key):
            print(f"[queue] {key}: " + (f"is running; stop it first (harness queue stop {key})"
                                        if (q.load(key) or {}).get("status") == "running" else FINISHING),
                  file=sys.stderr)
            return 1
        st = requeue(q, key, RETRYABLE + ("running",) + (("queued", "plan-review") if a.stage else ()),
                     stage=stage, **extra)
    if st:
        kept = isinstance(st.get("verify_only"), dict) and st["verify_only"].get("stage") == st["stage"]
        print(f"[queue] {st['key']}: queued again at {st['stage']}"
              + ("; the agent's kept work is checked again, not made again" if kept else ""))
    return 0 if st else 1


CONTINUABLE = ("blocked", "stopped", "queued", "running")


def continue_refused(q, st):
    """Why the ticket's stage cannot go on (harness queue continue), or ""."""
    key, stage = st["key"], st.get("stage")
    if st["status"] not in CONTINUABLE:
        how = {"needs-answers": f"harness queue answer {key}", "plan-review": f"harness queue approve {key}",
               "done": f"harness queue retry {key} starts it over"}.get(st["status"])
        return f"{key} is {st['status']}: there is no stage to go on" + (f" ({how})" if how else "")
    if st["status"] == "stopped" and st.get("stopped_from") == "plan-review":
        return f"{key} was stopped while its plan waited to be read: harness queue retry {key} puts it back"
    if stage not in RESUMABLE:
        return f"{key} is at {stage}, where no agent works: harness queue retry {key}"
    if not has_work(q, st):
        return f"{key}'s work is gone (or was never set up): harness queue retry {key} starts it again"
    return ""


def cut_off_why(st):
    """Why the stage stopped, for the agent going on: the reason and what the stage's
    last results (failed, blocked, stopped...) said."""
    said = [st.get("reason") or ""]
    for h in reversed((st.get("history") or [])[-3:]):
        if h.get("stage") != st.get("stage") or h.get("result") == "passed":
            break
        said += [d for d in h.get("details") or [] if d not in said]
    text = "; ".join(d for d in said if d)
    return cap(text, 1500) if text else "it was stopped"


def cmd_continue(q, a):
    """The stage that was cut off (blocked, stopped, or left by a run that ended)
    goes on in its agent's session with the owner's message, its work as it is;
    checked as usual when the agent ends."""
    key = a.key.upper()
    st = q.load(key)
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return 1
    if a.stdin:
        text = sys.stdin.buffer.read().decode("utf-8", "replace")
    else:
        text = a.text if a.text is not None else a.message or ""
    if len(text) > OWNER_NOTE_CAP:
        text = text[:OWNER_NOTE_CAP] + f"\n[... cut at {OWNER_NOTE_CAP} characters ...]"
    reap_stranded(q, key, getattr(a, "harness", ""))
    with held(os.path.join(q.root, "queue.lock")):
        if run_holds(q, key):
            print(f"[queue] {key}: " + (f"is running; stop it first (harness queue stop {key})"
                                        if (q.load(key) or {}).get("status") == "running" else FINISHING),
                  file=sys.stderr)
            return 1
        st = q.load(key) or st
        why = continue_refused(q, st)
        if why:
            print(f"[queue] {why}", file=sys.stderr)
            return 1
        cut = cut_off_why(st)
        # its agent had ended (stopped while its work was being saved or checked): with
        # nothing new to tell it, its work is checked as it is, not made again
        kept = st["verify_only"] if kept_result(st) and not text.strip() else None
        st = requeue(q, key, CONTINUABLE, go_on=True)
        if not st:
            return 1
        if kept:
            st["verify_only"] = kept
            st["reason"] = f"continued by the owner: {st['stage']}'s finished work is checked"
        else:
            st["resume"] = {"stage": st["stage"], "why": cut, "message": text.strip()}
            st["reason"] = f"continued by the owner: {st['stage']} goes on"
        q.save(st)
    s = st.get("session") or {}
    how = ("; its agent had finished, so its work is checked as it is, with no new launch (a message sends the "
           "agent back to it first)" if kept else
           f" in its agent's session {s['id']}" if s.get("stage") == st["stage"] and s.get("id") else
           " with a new agent told to finish the work there (no session of the stage's agent is known)")
    print(f"[queue] {key}: queued to go on at {st['stage']}{how}"
          + ("" if run_going(q) else "; harness queue run takes it"))
    return 0


def cmd_stop(q, a):
    reap_stranded(q, a.key.upper(), getattr(a, "harness", ""))
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
    st.pop("agent", None)
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
        # the run first: it would take its agent's end for a failed launch and go on
        # (a parallel run: each slot is a run, then the run that started them)
        for s in [*slot_runs(q, all_files=True), r]:
            if s is not r and not (isinstance(s.get("pid"), int) and s["pid"] > 0):
                continue
            if os.name == "nt":
                kill_tree(s["pid"])
            else:
                with contextlib.suppress(OSError):
                    os.kill(s["pid"], signal.SIGKILL)
            if isinstance(s.get("agent"), int) and s["agent"] > 0:
                kill_tree(s["agent"])   # the agent's session (Windows: its tree: bash, the docker client)
        end = time.time() + 20
        while run_going(q) and time.time() < end:
            time.sleep(0.2)
        if run_going(q):
            print(f"[queue] the run (process {pid}) still holds the queue; end it by hand", file=sys.stderr)
            return 1
        print(f"[queue] ended the run (process {pid})")
    else:
        print("[queue] no queue run is going")
    for path in (q.sentinel(), os.path.join(q.root, "run.json"),
                 *(os.path.join(q.root, f"run-{s['slot']}.json") for s in slot_runs(q, all_files=True))):
        drop(path)
    left = []
    with held(os.path.join(q.root, "queue.lock")):
        for key in q.order():
            st = q.load(key)
            if not st or st.get("status") != "running":
                continue
            stopped = os.path.exists(q.sentinel(key))
            drop(q.sentinel(key))
            why = "the run was ended by the owner (halt --force)"
            record(st, "stopped" if stopped else "interrupted", [why + "; the attempt does not count"])
            if st.get("agent"):
                left.append(dict(st))
            st.pop("agent", None)
            st.update(status="stopped" if stopped else "queued", reason=why,
                      **({"stopped_from": "queued"} if stopped else {}))
            q.save(st)
            q.log(key, f"{why}: {'stopped' if stopped else 'back in the queue'} at {st['stage']}")
            print(f"[queue] {key}: {'stopped' if stopped else 'back in the queue'} at {st['stage']}")
    for st in left:   # its agent (if it outlived the kill) and its container; slow, so after the lock
        reap(q, st, a.harness, "the run was ended by the owner")
    tidy(q)   # the in-place exclude block of the stage cut short
    for key in q.order():
        st = q.load(key) or {}
        if st.get("in_place") and st.get("status") in ("queued", "stopped") and os.path.isdir(st.get("project", "")):
            on = git(st["project"], "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
            if on == branch_of(st) and st.get("orig_branch") not in ("", None, on):
                print(f"[queue] {key}: {st['project']} is left on its branch {on} (the next run carries on there; "
                      f"git switch {st['orig_branch']} to go back meanwhile)")
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
    """run.json with "running": a beat within the last 30 s; "slots": a parallel
    run's slots that beat (run-I.json, "slot": I); "tickets": the tickets in hand."""
    info = run_file(os.path.join(q.root, "run.json"))
    if info is None:
        return {"running": False, "slots": [], "tickets": []}
    info["running"] = beating(info)
    info["slots"] = slot_runs(q) if info["running"] and info.get("parallel") else []
    info["tickets"] = [t for t in [info.get("ticket")] + [s.get("ticket") for s in info["slots"]] if t]
    return info


def run_file(path):
    try:
        info = json.loads(read(path) or "null")
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def beating(info):
    beat_at = info.get("beat")
    return isinstance(beat_at, (int, float)) and time.time() - beat_at < 30


SLOT_FILE = re.compile(r"run-(\d+)\.json")


def slot_runs(q, all_files=False):
    """The slots of a parallel run that beat (all_files: every run-I.json there is), by slot."""
    out = []
    for name in os.listdir(q.root) if os.path.isdir(q.root) else ():
        m = SLOT_FILE.fullmatch(name)
        info = run_file(os.path.join(q.root, name)) if m else None
        if info is not None and (all_files or beating(info)):
            info["slot"] = int(m.group(1))
            out.append(info)
    return sorted(out, key=lambda i: i["slot"])


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
    if r["running"] and r.get("parallel"):
        print(f"run: going (pid {r.get('pid')}), {r['parallel']} tickets at a time")
        for s in r["slots"]:
            doing = f"{s.get('ticket') or 'between tickets'} {s.get('stage') or ''}".rstrip()
            print(f"  slot {s['slot']} (pid {s.get('pid')}): {doing}" + (f": {s['step']}" if s.get("step") else ""))
    elif r["running"]:
        doing = f"{r.get('ticket') or 'between tickets'} {r.get('stage') or ''}".rstrip()
        print(f"run: going (pid {r.get('pid')}): {doing}" + (f": {r['step']}" if r.get("step") else ""))
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
    was = st["stage"]
    # Answers or feedback: the plan made with them comes back to be agreed before
    # anything is built (--go: the owner took the defaults and wants it to go on: approved).
    with held(os.path.join(q.root, "queue.lock")):   # the run takes a queued ticket under it
        st = requeue(q, key, allowed, stage="plan" if planned else None, plan_approved=planned and bool(a.go),
                     plan_hold=planned and not a.go)
    if st:
        if not planned:
            then = f"the queue run goes on at {st['stage']} with them"
        elif a.go:
            then = "the queue run makes the plan again with them and goes straight on to implementing"
        else:
            then = "the queue run makes the plan again with them, and it comes back to you to approve"
        if planned and was not in ("plan", "done"):
            then += f" (the ticket was at {was}: it starts again from the plan)"
        print(f"[queue] {key}: answers saved; {then}")
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
    s.add_argument("--clone", action="store_true", help=argparse.SUPPRESS)   # gone from harness; kept for old tickets' tests
    s.add_argument("--no-branch", action="store_true")   # the top's change is left uncommitted
    s.add_argument("--repo-branch", action="append", default=[])   # REL[=BRANCH][:TARGET]: a repository inside on a branch
    s.add_argument("--allow-branch", action="store_true")   # --branch may be the base, main or master
    s.add_argument("--review-plan", action="store_true")   # the default
    s.add_argument("--no-plan-approval", action="store_true")
    s.add_argument("--take-defaults", action="store_true")   # blocking questions go on at their Default
    sub.add_parser("list")
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("remove"); s.add_argument("keys", nargs="+"); s.add_argument("--harness", default="")
    s = sub.add_parser("approve"); s.add_argument("key")
    s = sub.add_parser("retry"); s.add_argument("key"); s.add_argument("--stage", choices=ALL_STAGES)
    s.add_argument("--harness", default="")
    s = sub.add_parser("continue"); s.add_argument("key"); s.add_argument("message", nargs="?")
    s.add_argument("--text"); s.add_argument("--stdin", action="store_true"); s.add_argument("--harness", default="")
    s = sub.add_parser("stop"); s.add_argument("key"); s.add_argument("--harness", default="")
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
    s.add_argument("--outage-wait", type=int, default=OUTAGE_WAIT)   # seconds; 0: an outage ends the run
    s.add_argument("--idle-min", type=float, default=IDLE_MIN)       # 0: no idle watchdog
    s.add_argument("--watch", action="append", default=[])           # files the agent writes as it works
    s.add_argument("--parallel", type=int, default=1)                # tickets at a time
    s.add_argument("--slot", type=int, default=0, help=argparse.SUPPRESS)   # one of a parallel run's slots
    a = p.parse_args(argv[1:])
    a.argv = argv
    q = Queue(a.state)
    try:
        return {"add": cmd_add, "list": cmd_list, "status": cmd_status, "remove": cmd_remove, "approve": cmd_approve,
            "retry": cmd_retry, "continue": cmd_continue, "stop": cmd_stop, "halt": cmd_halt, "move": cmd_move, "note": cmd_note,
            "answer": cmd_answer, "findings": cmd_findings, "run": cmd_run}[a.cmd](q, a)
    except Busy as e:
        print(f"[queue] {e}; try again", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
