#!/usr/bin/env python3
"""The ticket queue behind `harness queue`: works through tickets one at a time,
plan -> implement -> review (-> one fix) -> push, with no one at the keyboard.

  ticket_queue.py --state DIR add KEY... [--project DIR] [--base REF]
  ticket_queue.py --state DIR list | remove KEY... | approve KEY | retry KEY
  ticket_queue.py --state DIR answer KEY --vault V [--text T]
  ticket_queue.py --state DIR findings --vault V
  ticket_queue.py --state DIR run --vault V --proxy URL --harness PATH
                  [--model M] [--default-model M] [--verify-model M] [--host]
                  [--single-message] [--retries N] [--push 0|1] [--remote R]
                  [--plan-review 0|1]

Each stage is one fresh, headless agent launch through the harness CLI
(`harness -p --yolo --ticket KEY -m MODEL PROMPT`: the firewalled container,
or host mode) in the ticket's own clone of the project, state/queue/KEY/repo,
on branch KEY. The prompt (scripts/queue_prompts/<stage>.md) carries
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

harness never runs git inside the clone once the agent has had it: it fetches
branch KEY into the project (refs/harness-queue/KEY), diffs there and pushes
from there, without force. Nothing is ever written to the tracker.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
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


class Infra(Exception):
    """The upstream (or the proxy) does not answer: stop the run, keep the ticket."""


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
            return json.loads(read(os.path.join(self.dir(key), "state.json")))
        except ValueError:
            return None

    def save(self, st):
        st["updated"] = now_iso()
        write_atomic(os.path.join(self.dir(st["key"]), "state.json"), json.dumps(st, indent=2) + "\n")

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


def note_paths(vault, key):
    d = os.path.join(vault, "wiki", "notes", "tickets")
    return {"plan": os.path.join(d, f"{key}-plan.md"), "review": os.path.join(d, f"{key}-review.md"),
            "findings": os.path.join(d, f"{key}-findings.md"), "answers": os.path.join(d, f"{key}-answers.md"),
            "state": os.path.join(d, f"{key}-state.md"), "log": os.path.join(d, f"{key}-log.md"),
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

def git(cwd, *args, check=True, timeout=300):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=env, timeout=timeout)
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


def make_clone(q, st):
    """Clone the project at its base into DIR/repo, on a new branch KEY (the ticket key).
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
    base_sha = git(st["project"], "rev-parse", "--verify", st["base"] + "^{commit}").stdout.strip()
    # Git for Windows can enable a file system monitor daemon per repo; it keeps
    # the clone's directory open, so a later retry could not remove it.
    opts = ["-c", "core.fsmonitor=false"] if os.name == "nt" else []
    r = subprocess.run(["git", "clone", "-q", "--no-checkout", *opts, st["project"], dest],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"git clone: {(r.stderr or r.stdout).strip()[:400]}")
    git(dest, "checkout", "-q", "-b", st["key"], base_sha)
    git(dest, "remote", "remove", "origin")
    for k, fallback in (("user.name", "harness queue"), ("user.email", "harness-queue@localhost")):
        v = git(st["project"], "config", k, check=False).stdout.strip()
        git(dest, "config", k, v or fallback)
    st["base_sha"] = base_sha
    q.save(st)


def fetch_branch(q, st):
    """Fetch the clone's branch KEY into the project; (sha, diff, files, commits)
    or None when the branch has no commits of its own."""
    key, project = st["key"], st["project"]
    ref = f"refs/harness-queue/{key}"
    r = git(project, "fetch", "-q", "--no-tags", os.path.join(q.dir(key), "repo"),
            f"+refs/heads/{key}:{ref}", check=False)
    if r.returncode != 0:
        return None
    sha = git(project, "rev-parse", ref).stdout.strip()
    commits = int(git(project, "rev-list", "--count", f"{st['base_sha']}..{sha}").stdout.strip() or 0)
    if not commits:
        return None
    diff = git(project, "diff", "--no-color", "--no-ext-diff", st["base_sha"], sha).stdout
    files = [f for f in git(project, "diff", "--name-only", "--no-ext-diff", st["base_sha"], sha).stdout.splitlines() if f]
    return sha, diff, files, commits


def check_change(st, change, plan_text):
    """Hard gates on a branch, and the warnings that are not gates."""
    if change is None:
        return [f"no commits on branch {st['key']}: commit your change on that branch (git add, git commit)"], []
    _, diff, files, _ = change
    problems, warnings = [], []
    if not diff.strip():
        problems.append("the commits change nothing (the diff is empty)")
    for f in files:
        if f.startswith(".github/"):
            problems.append(f"{f}: changes under .github/ (CI) are not made by the queue; leave CI to the owner")
        if SECRET_PATH.search(f):
            problems.append(f"{f}: a secret or key file must not be committed")
    notes = tuple(f"{st['key']}-{k}.md".lower() for k in ("plan", "review", "answers", "findings"))
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
    if outside:
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


def ask_verifier(ctx, prompt, key, stage):
    """The verifier's JSON verdict; three tries with the whole prompt each time
    (the upstream reads only the last message). Every reply is kept for review."""
    reply = ""
    for n in range(3):
        try:
            reply = call_model(ctx.proxy, ctx.verify_model, [{"role": "user", "content": prompt}], VERIFY_TIMEOUT)
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
    if len(prompt.encode()) > PROMPT_MAX_BYTES:
        raise RuntimeError(f"the {stage} prompt is {len(prompt.encode())} bytes, over one command-line argument's limit")
    cmd += ["-p", "--yolo", "--ticket", key, "-m", model, prompt]
    out = os.path.join(ctx.q.dir(key), f"{stage}-{st['attempt']}.out")
    env = dict(os.environ, HARNESS_HOST_CONFIRM="1")
    since = int(time.time() * 1000)
    with open(out, "w", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, cwd=os.path.join(ctx.q.dir(key), "repo"), stdin=subprocess.DEVNULL,
                             stdout=f, stderr=subprocess.STDOUT, env=env, **NEW_GROUP)
        ctx.child = p
        try:
            rc = p.wait(timeout=ctx.stage_timeout)
        except subprocess.TimeoutExpired:
            stop(p)
            rc = 124
        except BaseException:  # Ctrl-C: the agent is in its own session and did not get it
            stop(p)
            raise
        finally:
            ctx.child = None
    sync_vault(ctx, key, since)
    return rc, out


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
    subprocess.run([BASH, ctx.harness, "queue", "_sync", key, os.path.join(ctx.q.dir(key), "repo"), str(since)],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=600, check=False)


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
        call_model(ctx.proxy, ctx.verify_model, [{"role": "user", "content": "Reply with the single word OK."}], 180)
    except InfraError as e:
        raise Infra(str(e)) from None


# --- stages -------------------------------------------------------------------------------

_AGENT_PATHS = {}


def agent_path(ctx, path):
    """A host path as the agent sees it. A container launch on Windows mounts the
    vault at its Git Bash spelling (/c/Users/...); C:\\Users\\... means nothing in
    Linux, and an agent told to write its plan there never does."""
    if os.name != "nt" or ctx.host or ctx.single:
        return path
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
    return dict(KEY=st["key"], BRANCH=st["key"], BASE_SHA=st.get("base_sha", ""),
                REPO=os.path.join(ctx.q.dir(st["key"]), "repo"), TICKET_FILE=ap["ticket"],
                TICKET=read(p["ticket"], TICKET_CAP) or "(the ticket file is missing; read the path above)",
                PLAN_FILE=ap["plan"], PLAN=plan or read(p["plan"], PLAN_CAP) or "(none yet)",
                REVIEW_FILE=ap["review"], FINDINGS_FILE=ap["findings"], ANSWERS_FILE=ap["answers"],
                ANSWERS=answers or "(none)",
                FEEDBACK=cap(st.get("feedback") or "(first attempt)", NOTE_CAP))


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
    repo = os.path.join(ctx.q.dir(key), "repo")
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
        text, where = read(found[1]), f"the clone ({os.path.relpath(found[1], repo)})"
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
    advance(ctx, st, "implement")
    if ctx.plan_review and not st.get("plan_approved"):
        st.update(status="plan-review", reason=f"plan ready to read: {p['plan']}")
        ctx.q.save(st)
        ctx.q.log(st["key"], f"waiting for the plan to be read: harness queue approve {st['key']}")


def numbered(items):
    return "\n".join(f"{i}. {c}" for i, c in enumerate(items, 1))


def verify_change(ctx, st, rc, stage):
    """Gates then the verifier on the branch; True when it passes."""
    p = note_paths(ctx.vault, st["key"])
    plan = read(plan_copy(ctx.q, st["key"]))
    _, criteria, _ = check_plan(plan)
    if stage == "fix":
        criteria = criteria + [f"(review) {m}" for m in st.get("must_fix", [])]
    change = fetch_branch(ctx.q, st)
    problems, warnings = check_change(st, change, plan)
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


def cap(text, n):
    return text if len(text) <= n else text[:n] + f"\n[... cut at {n} characters ...]\n"


def stage_implement(ctx, st):
    rc, _ = run_agent(ctx, st, "implement", render("implement", **common(ctx, st)))
    if verify_change(ctx, st, rc, "implement"):
        advance(ctx, st, "review")


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
    advance(ctx, st, "fix" if must else "push")


def stage_fix(ctx, st):
    v = common(ctx, st)
    v["MUST_FIX"] = cap("\n".join("- " + m for m in st.get("must_fix", [])), NOTE_CAP)
    rc, _ = run_agent(ctx, st, "fix", render("fix", **v))
    if verify_change(ctx, st, rc, "fix"):
        advance(ctx, st, "push")


def stage_push(ctx, st):
    key, project = st["key"], st["project"]
    change = fetch_branch(ctx.q, st)
    if change is None:
        return block(ctx, st, "the branch is gone from the clone")
    sha = change[0]
    branch = f"refs/heads/{key}"
    old = git(project, "rev-parse", "-q", "--verify", branch, check=False).stdout.strip()
    if not old:
        git(project, "update-ref", branch, sha)
    elif old != sha:
        if git(project, "merge-base", "--is-ancestor", old, sha, check=False).returncode == 0:
            git(project, "update-ref", branch, sha, old)
        else:
            ctx.q.log(key, f"warning: the project's branch {key} is not behind the queue's branch; left as it is")
    if ctx.push:
        r = git(project, "push", "-q", ctx.remote, f"{sha}:{branch}", check=False, timeout=600)
        if r.returncode != 0:
            return block(ctx, st, f"push to {ctx.remote} refused: {(r.stderr or r.stdout).strip()[:300]}")
        ctx.q.log(key, f"pushed branch {key} to {ctx.remote} ({sha[:12]})")
    else:
        ctx.q.log(key, f"branch {key} ready in the project ({sha[:12]}); not pushed (QUEUE_PUSH=0)")
    record(st, "passed", [f"branch {key} at {sha[:12]}"])
    st.update(status="done", stage="done", attempt=0, feedback="", reason="", sha=sha)
    ctx.q.save(st)


STAGES = {"plan": stage_plan, "implement": stage_implement, "review": stage_review, "fix": stage_fix,
          "push": stage_push}


def record(st, result, details=()):
    """One entry in the ticket's history (state.json), which the run viewer shows:
    each stage attempt's result and, for a failed one, why."""
    h = st.setdefault("history", [])
    h.append({"at": now_iso(), "stage": st.get("stage", ""), "attempt": st.get("attempt", 0) + 1,
              "result": result, "details": [str(d)[:400] for d in list(details)[:20]]})
    del h[:-HISTORY_CAP]


def advance(ctx, st, stage):
    record(st, "passed")
    st.update(stage=stage, attempt=0, feedback="", status="running")
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
    # running (and so not answerable) before the clone, which can take minutes;
    # the vault is kept for the run viewer, which may have started with another
    st.update(status="running", vault=ctx.vault)
    ctx.q.save(st)
    if not st.get("base_sha") or not os.path.isdir(os.path.join(ctx.q.dir(key), "repo")):
        if st["stage"] != "plan":
            return block(ctx, st, "its clone is missing; harness queue retry starts it again from the plan")
        try:
            make_clone(ctx.q, st)
        except (RuntimeError, subprocess.SubprocessError) as e:
            return block(ctx, st, f"could not clone {st['project']} at {st['base']}: {e}")
        ctx.q.log(key, f"cloned {st['project']} at {st['base']} ({st['base_sha'][:12]}) on branch {key}")
    subprocess.run([BASH, ctx.harness, "queue", "_pull", key], stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=300, check=False)
    while st["status"] == "running":
        ctx.q.log(key, f"{st['stage']} (attempt {st['attempt'] + 1}, {ctx.model})")
        STAGES[st["stage"]](ctx, st)


def cmd_run(q, a):
    lock = open(os.path.join(q.root, "run.lock"), "a")
    if not try_lock(lock):
        print("[queue] another 'harness queue run' is running", file=sys.stderr)
        return 1
    ctx =argparse.Namespace(q=q, vault=os.path.abspath(a.vault), proxy=a.proxy, harness=a.harness, host=a.host,
                             single=a.single_message, retries=a.retries, push=a.push == 1, remote=a.remote,
                             plan_review=a.plan_review == 1, stage_timeout=a.stage_timeout, child=None)
    ctx.model = a.model or resolve_model(a.proxy, a.default_model)
    if not ctx.model:
        print("[queue] no model: set QUEUE_MODEL or DEFAULT_MODEL_NAME", file=sys.stderr)
        return 1
    ctx.verify_model = (a.verify_model or ctx.model).split("/", 1)[-1]
    done = {}   # KEY: its requeued stamp when this run took it; answered or retried since, it is taken again

    def pending(k):
        s = q.load(k) or {}
        return s.get("status") in ("queued", "running") and done.get(k) != s.get("requeued", "")
    try:
        while True:
            todo = [k for k in q.order() if pending(k)]
            if not todo:
                break
            st = q.load(todo[0])
            done[st["key"]] = st.get("requeued", "")
            try:
                process(ctx, st)
            except (Infra, KeyboardInterrupt):
                raise
            except Exception as e:  # noqa: BLE001 - one ticket's failure must not stop the queue
                import traceback
                q.log(st["key"], "error", traceback.format_exc())
                block(ctx, st, f"{type(e).__name__}: {str(e)[:250]}")
            sync_vault(ctx, st["key"], int(time.time() * 1000))   # the outcome, into the vault
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
    r = subprocess.run(["git", "-C", project, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[queue] {project} is not in a git repository (use --dir)", file=sys.stderr)
        return 1
    project = r.stdout.strip()
    base = a.base or git(project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() \
        or git(project, "rev-parse", "HEAD").stdout.strip()
    if git(project, "rev-parse", "-q", "--verify", base + "^{commit}", check=False).returncode != 0:
        print(f"[queue] {base} is not a commit in {project}", file=sys.stderr)
        return 1
    order = q.order()
    rc = 0
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
        q.save({"key": key, "project": project, "base": base, "status": "queued", "stage": "plan", "attempt": 0,
                "feedback": "", "reason": "", "created": now_iso()})
        order.append(key)
        print(f"[queue] {key}: queued ({project} at {base})")
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
    order = q.order()
    for raw in a.keys:
        key = raw.upper()
        if key not in order and not q.load(key):
            print(f"[queue] {key} is not in the queue", file=sys.stderr)
            continue
        order = [k for k in order if k != key]
        err = rmtree(q.dir(key))
        if err:
            print(f"[queue] {key}: {err}; remove it by hand", file=sys.stderr)
        print(f"[queue] {key} removed (its branch, if any, stays in the project as refs/harness-queue/{key})")
    q.set_order(order)
    return 0


def requeue(q, key, want, stage=None, **extra):
    st = q.load(key.upper())
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return None
    if st["status"] not in want:
        print(f"[queue] {st['key']} is {st['status']}, not {' or '.join(want)}", file=sys.stderr)
        return None
    st.update(status="queued", attempt=0, feedback="", reason="", requeued=now_iso(), **extra)
    if stage:
        st["stage"] = stage
    if st["stage"] == "plan":  # planned again: start over from the base, not from earlier commits
        st.pop("questions", None)
        rmtree(os.path.join(q.dir(st["key"]), "repo"))  # make_clone tries again, and says so
        for f in (plan_copy(q, st["key"]),):
            if os.path.exists(f):
                os.remove(f)
        st.pop("base_sha", None)
    q.save(st)
    return st


def cmd_approve(q, a):
    st = requeue(q, a.key, ("plan-review",), plan_approved=True)
    if st:
        print(f"[queue] {st['key']}: plan approved; 'harness queue run' implements it")
    return 0 if st else 1


def cmd_retry(q, a):
    st = q.load(a.key.upper())
    stage = "plan" if st and not os.path.isdir(os.path.join(q.dir(st["key"]), "repo")) else None
    st = requeue(q, a.key, ("blocked", "needs-answers"), stage=stage)
    if st:
        print(f"[queue] {st['key']}: queued again at {st['stage']}")
    return 0 if st else 1


def cmd_answer(q, a):
    key = a.key.upper()
    st = q.load(key)
    if not st:
        print(f"[queue] {key} is not in the queue", file=sys.stderr)
        return 1
    allowed = ("needs-answers", "plan-review", "blocked", "queued")
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
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        # $EDITOR may carry arguments ("code -w"), so the shell splits it.
        if subprocess.call(["sh", "-c", editor + ' "$1"', "sh", tmp]):
            print("[queue] the editor failed; nothing saved", file=sys.stderr)
            return 1
        text = read(tmp)
        if text.strip() == tmpl.strip():
            print("[queue] no answers written; nothing saved", file=sys.stderr)
            return 1
    stamp = f"\n<!-- {now_iso()} -->\n"
    old = read(p["answers"])
    write_atomic(p["answers"], (old.rstrip() + "\n" if old.strip() else "") + stamp + text.strip() + "\n")
    st = requeue(q, key, allowed, stage="plan", plan_approved=False)
    if st:
        print(f"[queue] {key}: answers saved to {p['answers']}; the plan is made again on the next run")
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
    sub.add_parser("list")
    s = sub.add_parser("remove"); s.add_argument("keys", nargs="+")
    s = sub.add_parser("approve"); s.add_argument("key")
    s = sub.add_parser("retry"); s.add_argument("key")
    s = sub.add_parser("answer"); s.add_argument("key"); s.add_argument("--vault", required=True); s.add_argument("--text")
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
    return {"add": cmd_add, "list": cmd_list, "remove": cmd_remove, "approve": cmd_approve, "retry": cmd_retry,
            "answer": cmd_answer, "findings": cmd_findings, "run": cmd_run}[a.cmd](q, a)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
