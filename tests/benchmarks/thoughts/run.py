#!/usr/bin/env python3
"""Paired, token-capped A/B probe of `harness --thoughts`, on SWE-bench Verified
(--suite swe, the default) or on the modes benchmark's six small local tasks
(--suite local: a few hundred thousand tokens a pair-set, fits in a day's
allowance).

Arm A = `harness host --yolo -p ...` (default slim mode); arm B = the same plus
`--thoughts`. Stock opencode in both arms; the flag is the only difference.
Pairs run interleaved AB, BA, AB, ... over tasks ordered by solve rate
(descending, then id). Docker-free: each task gets a no-history checkout + venv
(swe_env.py), a gold-patch gate excludes tasks whose env cannot reproduce
FAIL -> PASS, and the grader is the same SWE-bench eval run on the agent's diff.

Token guard (enforced in code, see guard()):
  * before each pair: continue only if spent + 2*p75(trial cost) <= --budget
    (p75 over finished trials; the per-trial cap stands in until there are 2);
  * each trial is killed once its live count reaches
    min(--trial-token-cap, (budget - spent) / 2) and scored as a fail (capped);
  * tokens = prompt + completion + reasoning from the proxy log's `usage:`
    lines; a request with no usage line (in flight at the kill, or "none
    reported") counts chars/3 of its prompt and flags the trial;
  * the proxy's usage ledger is cross-checked per trial: >10% disagreement, or
    a remaining-tokens-day header below the next pair's projected cost, stops.

Isolation, redaction and the private install root are the modes runner's
(tests/benchmarks/modes/run.py), imported and reused, not copied.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import swe_env  # noqa: E402

_spec = importlib.util.spec_from_file_location("bench_modes_run", os.path.join(HERE, "..", "modes", "run.py"))
modes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(modes)

REPO, RUNS_DIR, IS_WINDOWS, BASH, log = modes.REPO, modes.RUNS_DIR, modes.IS_WINDOWS, modes.BASH, modes.log
TASKS_JSON = os.path.join(HERE, "tasks.json")
ARMS = {"slim": [], "thoughts": ["--thoughts"]}
ARM_SHAPE = {"slim": "native/slim", "thoughts": "native/slim/thoughts"}
# Per suite: (budget, per-trial cap, per-trial timeout s). The local default
# leaves about 1M of a 5M daily allowance; the upstream refuses every model
# once the day's allowance is crossed.
DEFAULTS = {"swe": (20_000_000, 1_500_000, 2400), "local": (3_500_000, 250_000, 900)}
DEFAULT_BUDGET, DEFAULT_CAP = DEFAULTS["swe"][:2]
LEDGER_TOL = 0.10
PROMPT = (
    "You are working in a checkout of the {repo} repository (the current directory). A Python "
    "environment with the package installed in development mode is active (python, pip on PATH).\n"
    "Resolve the issue below by editing the source code. Do not add or change tests; hidden tests "
    "will check your fix. Work only inside the current directory. Stop when the fix is done.\n\n"
    "<issue>\n{problem}\n</issue>\n"
)


# --------------------------------------------------------------------------
# plan and guard
# --------------------------------------------------------------------------


def load_tasks(only: list[str] | None = None, path: str = TASKS_JSON) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        tasks = json.load(f)["tasks"]
    if only:
        by = {t["id"]: t for t in tasks}
        bad = [i for i in only if i not in by]
        if bad:
            raise SystemExit(f"unknown task id(s): {', '.join(bad)}")
        tasks = [by[i] for i in only]
    return sorted(tasks, key=lambda t: (-t["solve_rate"], t["id"]))


def build_plan(task_ids: list[str]) -> list[tuple[int, str, list[str]]]:
    """[(pair, task, [first arm, second arm])]: AB for even pairs, BA for odd,
    so arm order (warm caches, upstream drift) is balanced across the run."""
    return [(i, tid, ["slim", "thoughts"] if i % 2 == 0 else ["thoughts", "slim"])
            for i, tid in enumerate(task_ids)]


def build_local_plan(task_ids: list[str], repeats: int) -> list[tuple[int, str, list[str]]]:
    """[(pair, task, [first arm, second arm])] for --suite local: every task
    once per repeat, in a seeded shuffle; a task's arm order flips every
    repeat and half the tasks of a repeat start with each arm."""
    import random
    plan = []
    for r in range(repeats):
        ids = list(task_ids)
        random.Random(2000 + r).shuffle(ids)
        for tid in ids:
            ab = (task_ids.index(tid) + r) % 2 == 0
            plan.append((len(plan), tid, ["slim", "thoughts"] if ab else ["thoughts", "slim"]))
    return plan


def p75(xs: list[float]) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    k = 0.75 * (len(s) - 1)
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def guard(spent: int, budget: int, costs: list[int], cap: int) -> dict:
    """Go/no-go for the next pair, and the per-trial kill threshold for it."""
    per = p75(costs) if len(costs) >= 2 else float(cap)
    projected = int(2 * per)
    eff = min(cap, max(0, (budget - spent) // 2))
    ok = spent + projected <= budget and eff > 0
    return {"ok": ok, "spent": spent, "projected_pair": projected, "eff_cap": eff,
            "why": "" if ok else f"spent {spent:,} + projected pair {projected:,} > budget {budget:,}"}


# --------------------------------------------------------------------------
# proxy log: live token meter
# --------------------------------------------------------------------------

_RE_POST = re.compile(r"^\[(\w+)\] POST /\S*chat/completions .*?tools=(\d+)")
_RE_SHAPE = re.compile(r"^\[(\w+)\] upstream shape: mode=(\S+) messages=(\d+) chars=(\d+)")
_RE_USAGE = re.compile(r"^\[(\w+)\] usage: model=\S* prompt=(\w+) completion=(\w+) reasoning=(\w+)")
_RE_LEFT = re.compile(r"credits_day_left=(\d+)")
_RE_ERR = re.compile(r"^\[(\w+)\] upstream (?:returned (\d{3})|request failed|returned non-JSON|stream broke)")
_RE_ID = re.compile(r"^\[(\w+)\] ")
_EVENTS = {
    "sig_retry": "reattached thought signature(s)",
    "sig_off": "refuses reattached thought signatures",
    "budget_spent": "daily budget spent",
    "trimmed": "chars of old tool output",
    "usage_none": "usage: none reported",
    "relay_sse": "relayed native SSE",
    "relay_json_sse": "relayed native JSON as SSE",
}


def _n(v: str) -> int:
    return int(v) if v.isdigit() else 0


class LogMeter:
    """Feeds on proxy-log text; counts only requests whose POST it saw (a
    request left over from an earlier trial never scores against this one)."""

    def __init__(self):
        self.req: dict[str, dict] = {}
        self.text: list[str] = []
        self.ev = {k: 0 for k in _EVENTS}
        self.status: dict[str, int] = {}
        self.credits_left = None

    def feed(self, text: str) -> None:
        for line in text.splitlines():
            m = _RE_POST.match(line)
            if m:
                self.req[m.group(1)] = {"tools": int(m.group(2)), "t": time.time(), "usage": 0,
                                        "has_usage": False, "err": False, "chars": 0, "mode": "", "msgs": 0,
                                        "prompt": 0, "completion": 0, "reasoning": 0}
                self.text.append(line)
                continue
            m = _RE_ID.match(line)
            r = self.req.get(m.group(1)) if m else None
            if r is None:
                continue
            self.text.append(line)
            m = _RE_SHAPE.match(line)
            if m:
                r["mode"], r["msgs"], r["chars"] = m.group(2), int(m.group(3)), int(m.group(4))
                continue
            m = _RE_USAGE.match(line)
            if m:
                for k, g in (("prompt", 2), ("completion", 3), ("reasoning", 4)):
                    r[k] += _n(m.group(g))
                    r["usage"] += _n(m.group(g))
                r["has_usage"] = True
                r["t_done"] = time.time()
            m = _RE_ERR.match(line)
            if m:
                r["err"] = True
                if m.group(2):
                    self.status[m.group(2)] = self.status.get(m.group(2), 0) + 1
            for k, needle in _EVENTS.items():
                if needle in line:
                    self.ev[k] += 1
            m = _RE_LEFT.search(line)
            if m:
                self.credits_left = int(m.group(1))

    def tokens(self) -> tuple[int, bool]:
        """(tokens, estimated): usage where reported, chars/3 for a shaped
        request with neither usage nor an error (in flight, or none reported)."""
        tot, est = 0, False
        for r in self.req.values():
            if r["has_usage"]:
                tot += r["usage"]
            elif r["mode"] and not r["err"]:
                tot += r["chars"] // 3
                est = True
        return tot, est

    def summary(self) -> dict:
        c = modes.parse_proxy_log("\n".join(self.text))
        agent = [r for r in self.req.values() if r["tools"] > 0]
        tok, est = self.tokens()
        u = [r for r in agent if r["has_usage"]]
        c.update({
            "tokens": tok, "tokens_estimated": est, "steps": len(agent),
            "shape_modes": sorted({r["mode"] for r in agent if r["mode"]}),
            "status_counts": dict(self.status), "credits_day_left": self.credits_left,
            "usage_requests": len(u), **{f"ev_{k}": v for k, v in self.ev.items()},
        })
        return c


class Tail:
    """Incremental reader of the bench proxy's log. The proxy restarts (and
    truncates its log) when a trial is killed, so a pid change or a shrink
    restarts the read from 0."""

    def __init__(self, log_path: str, pid_path: str):
        self.path, self.pid_path = log_path, pid_path
        self.pid = modes._read_pid(pid_path)
        try:
            self.off = os.path.getsize(log_path) if self.pid else 0
        except OSError:
            self.off = 0
        self.part = ""

    def read(self) -> str:
        pid = modes._read_pid(self.pid_path)
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return ""
        if (pid and pid != self.pid) or size < self.off:
            self.off, self.part, self.pid = 0, "", pid
        if size == self.off:
            return ""
        with open(self.path, "rb") as f:
            f.seek(self.off)
            data = f.read()
        self.off += len(data)
        s = self.part + data.decode("utf-8", "replace")
        s, nl, self.part = s.rpartition("\n")
        return s + nl


# --------------------------------------------------------------------------
# ledger and opencode store
# --------------------------------------------------------------------------


def ledger_window(path: str, t0: float, t1: float) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if t0 <= float(e.get("ts", 0)) <= t1 + 5:
                    out.append(e)
    except OSError:
        pass
    return out


def ledger_check(entries: list[dict], counted: int, next_pair: int) -> dict:
    """Compare the ledger's own token sum for the trial window with the log
    count; read the remaining-day headers. stop=True ends the run."""
    if not entries:
        return {"ledger": "absent", "stop": False}
    led = sum(int(e.get(k) or 0) for e in entries for k in ("prompt", "completion", "reasoning"))
    out = {"ledger": "ok", "ledger_tokens": led, "stop": False}
    if counted and abs(led - counted) > LEDGER_TOL * max(led, counted):
        out.update(ledger="mismatch", stop=True,
                   why=f"ledger {led:,} vs log count {counted:,} tokens differ by >{LEDGER_TOL:.0%}")
    rem = entries[-1].get("remaining") or {}
    for key in ("tokens-day", "tokens"):
        if key in rem:
            try:
                left = int(float(rem[key]))
            except (TypeError, ValueError):
                continue
            out["tokens_day_left"] = left
            if left < next_pair:
                out.update(stop=True, why=f"upstream reports {left:,} tokens left today < next pair {next_pair:,}")
            break
    if "credits-day" in rem:
        out["credits_day_left"] = rem["credits-day"]
    return out


def contamination_needles(cache: str, dataset: str = "", run_dir: str = "") -> list[str]:
    """Strings that, in a tool input, mean the agent went looking at the
    answers: the cache (its basename, rows.json, the gate results), the
    dataset name and any --dataset file names, and this run's own trial dir
    (the other arm's patch and eval log)."""
    n = {os.path.basename(os.path.normpath(cache)), "bench-thoughts", "rows.json", "SWE-bench_Verified",
         "gate.json", "gate-gold.log", "gate-baseline.log"}
    if dataset:
        n.add(os.path.basename(os.path.normpath(dataset)))
        if os.path.isdir(dataset):
            n.update(x for x in os.listdir(dataset) if x.endswith(".json"))
    if run_dir:
        n.add(os.path.basename(os.path.normpath(run_dir)))
        n.add("benchmarks/runs")
    return sorted(x for x in n if len(x) >= 4)


def is_contaminated(text: str, needles: list[str], own_src: str) -> bool:
    """True if `text` mentions a needle once the agent's own checkout path
    (which itself sits under the cache) is taken out."""
    if own_src:
        text = text.replace(own_src, "<src>")
    return any(x in text for x in needles)


def _same_path(p: str) -> str:
    """A directory in one spelling, so opencode's record of it matches ours on
    Windows too (case, slashes, a Git Bash /c/... form)."""
    p = p.replace("\\", "/").rstrip("/")
    m = re.match(r"^/([a-zA-Z])(/|$)", p)
    if m and IS_WINDOWS:
        p = m.group(1) + ":/" + p[3:]
    return p.lower() if IS_WINDOWS else p


def opencode_metrics(db: str, directory: str, t0: float, t1: float, needles: list[str]) -> dict:
    """Tool-call / tool-error / reasoning-part / empty-reply counts for the
    trial's sessions (by directory and time), and a contamination count of
    tool inputs that reach for the answers (contamination_needles). Best
    effort: {"oc_error": ...} on error."""
    out = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(session)")}
            tc = "time_created" if "time_created" in cols else None
            q = "SELECT id, directory FROM session"
            args: list = []
            if tc:
                q += f" WHERE {tc} >= ? AND {tc} <= ?"
                args += [int(t0 * 1000) - 5000, int(t1 * 1000) + 5000]
            want = _same_path(directory)
            sids = [r[0] for r in con.execute(q, args) if _same_path(r[1] or "") == want]
            if "parent_id" in cols and sids:
                kids = [r[0] for r in con.execute(
                    f"SELECT id FROM session WHERE parent_id IN ({','.join('?' * len(sids))})", sids)]
                sids = list(dict.fromkeys(sids + kids))
            if not sids:
                return {"oc_sessions": 0}
            ph = ",".join("?" * len(sids))
            tools = errs = reasoning = contaminated = 0
            for (d,) in con.execute(f"SELECT data FROM part WHERE session_id IN ({ph})", sids):
                p = json.loads(d)
                if p.get("type") == "tool":
                    tools += 1
                    st = p.get("state") or {}
                    errs += st.get("status") == "error"
                    if needles and is_contaminated(json.dumps(st.get("input") or {}), needles, directory):
                        contaminated += 1
                elif p.get("type") == "reasoning":
                    reasoning += 1
            empty = msg_err = 0
            for (d,) in con.execute(f"SELECT data FROM message WHERE session_id IN ({ph})", sids):
                m = json.loads(d)
                if m.get("role") == "assistant":
                    msg_err += bool(m.get("error"))
                    empty += (m.get("tokens") or {}).get("output", 1) == 0
            out = {"oc_sessions": len(sids), "tool_calls": tools, "tool_errors": errs,
                   "reasoning_parts": reasoning, "empty_replies": empty, "message_errors": msg_err,
                   "contaminated": contaminated}
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        out = {"oc_error": type(e).__name__}
    return out


# --------------------------------------------------------------------------
# one trial
# --------------------------------------------------------------------------


def local_workspace(task: dict) -> dict:
    """A fresh scratch copy of a local task's seed files (outside any repo)."""
    scratch = tempfile.mkdtemp(prefix="hb-thoughts-")
    work = os.path.join(os.path.realpath(scratch), "w")
    shutil.copytree(os.path.join(task["dir"], "files"), work)
    keep = os.path.join(work, ".keep")
    if os.path.exists(keep):
        os.remove(keep)
    return {"src": work, "scratch": scratch}


def local_check(task: dict, work: str, log_path: str) -> dict:
    """The task's own check.py, run in the workdir: exit 0 is a pass."""
    try:
        chk = subprocess.run([sys.executable, os.path.join(task["dir"], "check.py")], cwd=work,
                             capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        ok, why = chk.returncode == 0, (chk.stdout + chk.stderr)
    except subprocess.TimeoutExpired:
        ok, why = False, "check timed out"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(why)
    return {"resolved": ok, "applied": True, "reason": (why.strip().splitlines() or [""])[0][:200]}


def run_trial(pair: int, pos: int, task: dict, row: dict, env: dict, arm: str, eff_cap: int, ctx: dict) -> dict:
    """One arm of one pair. --suite swe: `row` is the SWE-bench row and `env`
    its checkout + venv. --suite local: `row` is the local task and `env` is
    ignored (each trial gets a fresh scratch copy)."""
    local = ctx.get("suite") == "local"
    host = os.path.join(ctx["bench_root"], "state", "host")
    tail = Tail(os.path.join(host, "proxy.log"), os.path.join(host, "proxy.pid"))
    meter = LogMeter()
    if local:
        env = local_workspace(row)
    else:
        swe_env.reset(env)
    tid = f"p{pair:02d}-{arm}-{task['id']}"
    tdir = os.path.join(ctx["out"], "trials")
    out_path, err_path = os.path.join(tdir, tid + ".stdout"), os.path.join(tdir, tid + ".stderr")
    prompt = row["prompt"] if local else PROMPT.format(repo=row["repo"], problem=row["problem_statement"])
    cmd = [ctx["harness"], "host", "--yolo"] + ARMS[arm] + ["-p", prompt]
    if IS_WINDOWS:
        cmd = [BASH] + cmd
    cenv = modes.child_env(ctx["bench_root"], ctx["port"], False, first=ctx["first"])
    if not local:
        cenv["VIRTUAL_ENV"] = env["venv"]
        cenv["PATH"] = swe_env.venv_bin(env["venv"]) + os.pathsep + cenv.get("PATH", "")
    t0w, t0 = time.time(), time.monotonic()
    capped = timed_out = False
    inflight = os.path.join(ctx["out"], "inflight.json")
    with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
        proc = subprocess.Popen(cmd, cwd=env["src"], env=cenv, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, **kw)
        try:
            last = 0.0
            while proc.poll() is None:
                time.sleep(0.5)
                meter.feed(tail.read())
                tok, _ = meter.tokens()
                if time.monotonic() - last > 5:
                    last = time.monotonic()
                    with open(inflight, "w", encoding="utf-8") as f:
                        json.dump({"pair": pair, "task": task["id"], "arm": arm, "tokens": tok}, f)
                if tok >= eff_cap:
                    capped = True
                    modes._kill_tree(proc)
                elif time.monotonic() - t0 > ctx["timeout"]:
                    timed_out = True
                    modes._kill_tree(proc)
        except BaseException:
            modes._kill_tree(proc)
            raise
        finally:
            rest = tail.read() + tail.part
            tail.part = ""
            meter.feed(rest)
    rc = proc.returncode
    secs = time.monotonic() - t0
    c = meter.summary()
    led = ledger_check(ledger_window(ctx["ledger"], t0w, time.time()),
                       0 if c["tokens_estimated"] else c["tokens"], ctx["next_pair"])
    oc = opencode_metrics(ctx["oc_db"], env["src"], t0w, time.time(), ctx["needles"])
    if local:
        patch = ""
        ev = local_check(row, env["src"], os.path.join(tdir, tid + ".eval.log"))
        if not ctx.get("keep"):
            shutil.rmtree(env["scratch"], ignore_errors=True)
    else:
        patch = swe_env.capture_diff(env)
        with open(os.path.join(tdir, tid + ".patch"), "w", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            f.write(patch)
        ev = swe_env.evaluate(env, row, patch, log_path=os.path.join(tdir, tid + ".eval.log")) if patch.strip() \
            else {"resolved": False, "applied": True, "empty_patch": True}
    passed = bool(ev.get("resolved")) and not capped
    err_tail = modes._tail(err_path)
    infra = ""
    if c["auth_errors"] or re.search(r"(?i)unlock_url|key (?:is )?locked|key rejected", err_tail):
        infra = "auth"
    elif c["requests"] == 0:
        infra = "launch"
    elif not passed and c["transient_errors"] and (timed_out or c["transient_errors"] >= c["requests"]):
        infra = "upstream"
    for p in (out_path, err_path):
        with open(p, encoding="utf-8", errors="replace") as f:
            s = ctx["redact"](f.read()[-modes.LOG_TAIL_CHARS * 4:])
        with open(p, "w", encoding="utf-8") as f:
            f.write(s)
    agent = [r for r in meter.req.values() if r["tools"] > 0]
    steps = max(len(agent), 1)
    done = [r for r in agent if r.get("t_done")]
    billed = [r for r in agent if r["has_usage"]]
    chars = sum(r["chars"] for r in billed)
    return {
        "pair": pair, "pos": pos, "task": task["id"], "arm": arm, "passed": passed,
        "resolved_raw": bool(ev.get("resolved")), "f2p_pass": ev.get("f2p_pass"), "p2p_pass": ev.get("p2p_pass"),
        "check": ev.get("reason", ""),
        "patch_applied": ev.get("applied"), "patch_lines": patch.count("\n"),
        "capped": capped, "eff_cap": eff_cap, "timed_out": timed_out, "rc": rc, "secs": round(secs, 1),
        "infra": infra, "arm_ok": bool(c["steps"]) and c["shape_modes"] == [ARM_SHAPE[arm]],
        "per_step": {k: round(sum(r[k] for r in agent) / steps, 1) for k in ("prompt", "completion", "reasoning")},
        "secs_per_step": round(sum(r["t_done"] - r["t"] for r in done) / max(len(done), 1), 2),
        # Prompt tokens per 1k chars of message content the proxy sent: if the
        # thoughts that come back in the history (reasoning_content) were
        # billed, B would pay more per char than A.
        "prompt_per_kchar": round(1000 * sum(r["prompt"] for r in billed) / chars, 1) if chars else 0.0,
        "first_prompt": billed[0]["prompt"] if billed else 0,
        **c, **led, **oc,
    }


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def pair_rows(results: list[dict]) -> dict:
    """{pair: {arm: row}} using the last non-infra row per (pair, arm)."""
    out: dict[int, dict] = {}
    for r in results:
        if not r.get("infra"):
            out.setdefault(r["pair"], {})[r["arm"]] = r
    return {p: d for p, d in out.items() if len(d) == 2}


def report(results: list[dict], meta: dict) -> str:
    spent = sum(int(r.get("tokens") or 0) for r in results)
    pairs = pair_rows(results)
    suite = "local tasks" if meta.get("suite") == "local" else "SWE-bench Verified"
    L = [f"--thoughts probe on {suite}: slim (A) vs slim + --thoughts (B)",
         f"model: {meta.get('model', '?')}   budget: {meta.get('budget', 0):,}   spent: {spent:,} tokens "
         f"({len(results)} trials, {sum(1 for r in results if r.get('infra'))} infra)",
         f"complete pairs: {len(pairs)}"]
    if not pairs:
        return "\n".join(L + ["no complete pairs yet"]) + "\n"
    A = [pairs[p]["slim"] for p in sorted(pairs)]
    B = [pairs[p]["thoughts"] for p in sorted(pairs)]
    n = len(A)
    ka, kb = sum(r["passed"] for r in A), sum(r["passed"] for r in B)
    b = sum(1 for a, t in zip(A, B) if a["passed"] and not t["passed"])
    c = sum(1 for a, t in zip(A, B) if t["passed"] and not a["passed"])
    wa, wb = modes.wilson(ka, n), modes.wilson(kb, n)
    L += ["", "pass rate (guardrail, underpowered):",
          f"  slim      {ka}/{n} = {ka / n:.0%}  95% CI {wa[0]:.0%}-{wa[1]:.0%}",
          f"  thoughts  {kb}/{n} = {kb / n:.0%}  95% CI {wb[0]:.0%}-{wb[1]:.0%}",
          f"  discordant: slim-only {b}, thoughts-only {c}; exact McNemar p = {modes.mcnemar_exact(b, c):.3f}",
          "", "paired per-trial means (B - A, 95% CI):"]
    for key, label in (("steps", "steps"), ("tokens", "tokens/trial"), ("secs", "wall s/trial"),
                       ("secs_per_step", "s/step"), ("first_prompt", "step-1 prompt"),
                       ("prompt_per_kchar", "prompt/1k chars")):
        d = [float(t.get(key) or 0) - float(a.get(key) or 0) for a, t in zip(A, B)]
        m, h = modes.mean_ci(d)
        L.append(f"  {label:<16} A {sum(float(a.get(key) or 0) for a in A) / n:>10.1f}  "
                 f"B {sum(float(t.get(key) or 0) for t in B) / n:>10.1f}  diff {modes._fmt_ci(m, h)}")
    for key in ("prompt", "completion", "reasoning"):
        d = [t["per_step"][key] - a["per_step"][key] for a, t in zip(A, B)]
        m, h = modes.mean_ci(d)
        L.append(f"  {key + '/step':<16} A {sum(a['per_step'][key] for a in A) / n:>10.1f}  "
                 f"B {sum(t['per_step'][key] for t in B) / n:>10.1f}  diff {modes._fmt_ci(m, h)}")
    L += ["", "functional discrepancies (A | B, totals over complete pairs):"]
    counters = [("capped", "capped"), ("timed_out", "timeouts"), ("errors", "upstream errors"),
                ("ev_sig_retry", "signature 400 retries"), ("ev_sig_off", "signatures switched off"),
                ("malformed_retries", "malformed-call retries"), ("reasks", "require-tool re-asks"),
                ("fatal", "FATAL"), ("ev_usage_none", "usage: none reported"),
                ("tokens_estimated", "trials w/ estimated tokens"), ("ev_budget_spent", "daily budget spent"),
                ("empty_replies", "empty replies"), ("message_errors", "message errors"),
                ("tool_errors", "tool errors"), ("tool_calls", "tool calls"),
                ("reasoning_parts", "reasoning parts"), ("contaminated", "dataset-cache reads (contamination)")]
    for key, label in counters:
        L.append(f"  {label:<36} {sum(int(r.get(key) or 0) for r in A):>6} | {sum(int(r.get(key) or 0) for r in B):>6}")
    L.append(f"  {'arm-verification failures':<36} {sum(not r['arm_ok'] for r in A):>6} | "
             f"{sum(not r['arm_ok'] for r in B):>6}")
    # The tool and reasoning counters above come from opencode's database; a
    # trial whose session was not found there counts 0 for all of them.
    L.append(f"  {'opencode session not found':<36} {sum(not r.get('oc_sessions') for r in A):>6} | "
             f"{sum(not r.get('oc_sessions') for r in B):>6}")
    ta = sum(int(r.get("tool_errors") or 0) for r in A) / max(1, sum(int(r.get("tool_calls") or 0) for r in A))
    tb = sum(int(r.get("tool_errors") or 0) for r in B) / max(1, sum(int(r.get("tool_calls") or 0) for r in B))
    L.append(f"  {'tool-error rate':<36} {ta:>6.1%} | {tb:>6.1%}")
    one = [(a, t) for a, t in zip(A, B) if a["passed"] != t["passed"]]
    if one:
        L += ["", "one-arm-passed pairs (steps / tokens / patch lines, A vs B):"]
        for a, t in one:
            L.append(f"  {a['task']:<28} {'A' if a['passed'] else 'B'} passed   "
                     f"{a['steps']}/{a['tokens']:,}/{a.get('patch_lines', 0)}  vs  "
                     f"{t['steps']}/{t['tokens']:,}/{t.get('patch_lines', 0)}")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def read_results(path: str) -> list[dict]:
    return modes.read_results(path)


def account_lost_inflight(out: str, results_path: str) -> None:
    """A runner killed mid-trial left inflight.json: book its tokens as an
    infra row so the budget still counts them."""
    p = os.path.join(out, "inflight.json")
    if not os.path.isfile(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        row = {"pair": d["pair"], "task": d["task"], "arm": d["arm"], "passed": False,
               "infra": "lost", "tokens": int(d.get("tokens") or 0)}
        with open(results_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except (OSError, ValueError, KeyError):
        pass
    os.remove(p)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="tests/benchmarks/thoughts/run.py",
                                 description="Paired token-capped A/B of `harness --thoughts` on SWE-bench Verified "
                                             "or on six small local tasks.")
    ap.add_argument("--suite", choices=("swe", "local"), default="swe",
                    help="swe: 38 SWE-bench Verified tasks (default); local: the six tasks of "
                         "tests/benchmarks/modes/tasks, x --repeats, sized for one day's allowance")
    ap.add_argument("--repeats", type=int, default=4, help="--suite local: runs of each task per arm (default 4)")
    ap.add_argument("--task-ids", default="", help="comma list (default: every task of the suite)")
    ap.add_argument("--pairs", type=int, default=0, help="stop after N pairs (default: the whole plan)")
    ap.add_argument("--budget", type=int, default=None,
                    help="total upstream tokens, both arms (default: swe %s, local %s)"
                         % tuple(f"{DEFAULTS[k][0]:,}" for k in ("swe", "local")))
    ap.add_argument("--trial-token-cap", type=int, default=None,
                    help="kill a trial at this many tokens (default: swe %s, local %s)"
                         % tuple(f"{DEFAULTS[k][1]:,}" for k in ("swe", "local")))
    ap.add_argument("--timeout", type=int, default=None, help="seconds per trial (default: swe 2400, local 900)")
    ap.add_argument("--keep-workdirs", action="store_true", help="--suite local: keep each trial's scratch dir")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and budget math and check inputs; spends nothing")
    ap.add_argument("--gate-only", action="store_true", help="build envs and run the gold gate, no trials")
    ap.add_argument("--rebuild-envs", action="store_true", help="rebuild envs and rerun gates")
    ap.add_argument("--resume", default="", metavar="DIR", help="continue the run in DIR")
    ap.add_argument("--report", default="", metavar="DIR", help="only rebuild DIR/report.txt")
    ap.add_argument("--dataset", default="", metavar="PATH", help="local SWE-bench_Verified rows (file or dir) instead of HF")
    ap.add_argument("--cache-dir", default="", help="env/dataset cache (default <install root>/state/bench-thoughts)")
    ap.add_argument("--harness-bin", default="", help="harness script (default: $HARNESS_BIN or the repo's)")
    ap.add_argument("--install-root", default="", help="install root with your .env (default: $HARNESS_BENCH_INSTALL_ROOT or the repo)")
    a = ap.parse_args(argv)
    local = a.suite == "local"
    d_budget, d_cap, d_timeout = DEFAULTS[a.suite]
    a.budget = d_budget if a.budget is None else a.budget
    a.trial_token_cap = d_cap if a.trial_token_cap is None else a.trial_token_cap
    a.timeout = d_timeout if a.timeout is None else a.timeout
    if a.repeats <= 0:
        ap.error("need --repeats > 0")
    if a.budget <= 0 or a.trial_token_cap <= 0 or a.trial_token_cap * 2 > a.budget:
        ap.error("need budget > 0 and 0 < 2 * trial-token-cap <= budget")

    if a.report:
        meta = {}
        if os.path.isfile(os.path.join(a.report, "meta.json")):
            with open(os.path.join(a.report, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        text = report(read_results(os.path.join(a.report, "results.jsonl")), meta)
        with open(os.path.join(a.report, "report.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        print(text)
        return 0

    harness_bin = a.harness_bin or os.environ.get("HARNESS_BIN") or os.path.join(REPO, "harness")
    # abspath: harness hands Windows a C:/ root, and joins below add backslashes.
    real_root = os.path.abspath(a.install_root or os.environ.get("HARNESS_BENCH_INSTALL_ROOT") or REPO)
    cache = os.path.abspath(a.cache_dir or os.path.join(real_root, "state", "bench-thoughts"))
    data_dir = os.path.join(cache, "data")
    only = [x.strip() for x in a.task_ids.split(",") if x.strip()] or None
    if local:
        tasks = modes.load_tasks(only)
        plan = build_local_plan([t["id"] for t in tasks], a.repeats)[: a.pairs or None]
    else:
        tasks = load_tasks(only)
        if a.pairs:
            tasks = tasks[: a.pairs]
        plan = build_plan([t["id"] for t in tasks])

    if a.dry_run and local:
        print(f"{'pair':>4} {'order':<15} task")
        for i, tid, order in plan:
            print(f"{i:>4} {'-'.join(order):<15} {tid}")
        g0 = guard(0, a.budget, [], a.trial_token_cap)
        print(f"\nbudget {a.budget:,} tokens; per-trial cap {a.trial_token_cap:,}; timeout {a.timeout}s; "
              f"first pair projected {g0['projected_pair']:,} (cap stands in for p75 until 2 trials finish)")
        print(f"rule: before each pair, run only if spent + 2*p75(trial tokens) <= budget, and stop if the "
              f"upstream reports fewer tokens left today than the next pair needs. Worst case (every trial "
              f"capped): {a.budget // (2 * a.trial_token_cap)} of {len(plan)} pairs.")
        print(f"harness: {harness_bin} ({'found' if os.path.isfile(harness_bin) else 'MISSING'})")
        print("dry run: nothing started, no upstream call, no tokens spent")
        return 0

    if a.dry_run:
        rows = swe_env.load_rows([t["id"] for t in tasks], data_dir, a.dataset, fetch=False) \
            if a.dataset or os.path.isfile(os.path.join(data_dir, "rows.json")) else {}
        for t in tasks:
            if rows:
                r = rows[t["id"]]
                assert r["repo"] == t["repo"] and swe_env.test_directives(r), t["id"]
                swe_env.spec(r["repo"], r["version"])
                f2p, p2p = swe_env.f2p_p2p(r)
                assert f2p, t["id"]
        gates = {t["id"]: swe_env.env_paths(cache, t["id"])["gate"] for t in tasks}
        print(f"{'pair':>4} {'order':<15} {'task':<28} {'solve':>5}  {'difficulty':<16} gate")
        for (i, tid, order), t in zip(plan, tasks):
            g = "?"
            if os.path.isfile(gates[tid]):
                with open(gates[tid], encoding="utf-8") as f:
                    g = "ok" if json.load(f).get("ok") else "EXCLUDED"
            print(f"{i:>4} {'-'.join(order):<15} {tid:<28} {t['solve_rate']:>5.2f}  {t['difficulty']:<16} {g}")
        g0 = guard(0, a.budget, [], a.trial_token_cap)
        worst_pairs = a.budget // (2 * a.trial_token_cap)
        print(f"\nbudget {a.budget:,} tokens; per-trial cap {a.trial_token_cap:,}; first pair projected "
              f"{g0['projected_pair']:,} (cap stands in for p75 until 2 trials finish)")
        print(f"rule: before each pair, run only if spent + 2*p75(trial tokens) <= budget; each trial is killed at "
              f"min(cap, (budget - spent)/2). Worst case (every trial capped): {worst_pairs} of {len(plan)} pairs, "
              f"overshoot at most one in-flight request.")
        print(f"dataset rows: {'checked ' + str(len(rows)) if rows else 'not cached (fetched from HF at run time)'}; "
              f"harness: {harness_bin} ({'found' if os.path.isfile(harness_bin) else 'MISSING'}); cache: {cache}")
        print("dry run: nothing started, no upstream call, no tokens spent")
        return 0

    if local:
        if a.gate_only:
            log("--suite local has no gate (its checkers are unit-tested against their mock solutions)")
            return 0
        rows, gates = {t["id"]: t for t in tasks}, {}
    else:
        rows = swe_env.load_rows([t["id"] for t in tasks], data_dir, a.dataset)
        log(f"gold gate on {len(tasks)} task(s) (cached per task)")
        gates = {}
        for t in tasks:
            gates[t["id"]] = swe_env.gold_gate(rows[t["id"]], cache, a.rebuild_envs, logf=log)
            g = gates[t["id"]]
            log(f"  gate {t['id']}: {'ok' if g['ok'] else 'EXCLUDED - ' + g.get('why', '')}"
                + (f" ({'; '.join(g.get('notes') or [])})" if g.get("notes") else ""))
        if a.gate_only:
            ok = sum(g["ok"] for g in gates.values())
            log(f"{ok}/{len(gates)} task(s) pass the gold gate")
            return 0
        # Renumber after exclusions so AB/BA stays balanced (gates are cached, so
        # a resumed run gets the same numbering).
        plan = build_plan([p[1] for p in plan if gates[p[1]]["ok"]])

    if not os.path.isfile(os.path.join(real_root, ".env")):
        raise SystemExit(f"no .env in {real_root}: configure harness first")
    out = a.resume or os.path.join(RUNS_DIR, ("thoughts-local-" if local else "thoughts-")
                                   + datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(out, "trials"), exist_ok=True)
    results_path = os.path.join(out, "results.jsonl")
    account_lost_inflight(out, results_path)
    bench_root = os.path.join(real_root, "state", "bench-thoughts", "root")
    red = modes.make_redactor()
    port = modes.free_port()
    meta = {"suite": a.suite, "budget": a.budget, "trial_token_cap": a.trial_token_cap, "timeout_s": a.timeout,
            "repeats": a.repeats if local else None, "tasks": [p[1] for p in plan], "model": red(os.environ.get("DEFAULT_MODEL_NAME", "")) or "from .env",
            "host": f"{platform.system()} {platform.machine()}",
            "excluded": {k: g.get("why") for k, g in gates.items() if not g["ok"]}}
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    ctx = {"harness": harness_bin, "bench_root": bench_root, "port": port, "timeout": a.timeout, "redact": red,
           "out": out, "first": True, "suite": a.suite, "keep": a.keep_workdirs,
           "needles": (sorted({"benchmarks/modes", "check.py", "mock_solution", "task.json",
                               os.path.basename(out), "benchmarks/runs"}) if local
                       else contamination_needles(cache, a.dataset, out)),
           "ledger": os.path.join(bench_root, "state", "output", "usage.jsonl"),
           "oc_db": os.path.join(bench_root, "xdg", "data", "opencode", "opencode.db")}
    resume = f"python3 {os.path.join(HERE, 'run.py')} --resume {out} --budget {a.budget} " \
             f"--trial-token-cap {a.trial_token_cap}" + (f" --task-ids {a.task_ids}" if a.task_ids else "") \
             + (f" --pairs {a.pairs}" if a.pairs else "") \
             + (f" --suite local --repeats {a.repeats}" if local else "")

    def stop_proxy():
        subprocess.run(([BASH] if IS_WINDOWS else []) + [harness_bin, "host", "down"],
                       env=modes.child_env(bench_root, port, False, first=False),
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=60)

    aborted = ""
    try:
        modes.prepare_root(real_root, bench_root, None)
        stop_proxy()
        for stale in ("proxy.log", "proxy.pid"):
            try:
                os.remove(os.path.join(bench_root, "state", "host", stale))
            except OSError:
                pass
        aborted = modes.warm_up(harness_bin, modes.child_env(bench_root, port, False, first=False), red)
        for pair, tid, order in ([] if aborted else plan):
            results = read_results(results_path)
            done = {(r["pair"], r["arm"]) for r in results if not r.get("infra")}
            todo = [arm for arm in order if (pair, arm) not in done]
            if not todo:
                continue
            spent = sum(int(r.get("tokens") or 0) for r in results)
            costs = [int(r["tokens"]) for r in results if not r.get("infra") and r.get("tokens")]
            g = guard(spent, a.budget, costs, a.trial_token_cap)
            log(f"pair {pair} {tid}: spent {spent:,}/{a.budget:,}, projected {g['projected_pair']:,}, "
                f"trial cap {g['eff_cap']:,}")
            if not g["ok"]:
                aborted = "token budget: " + g["why"]
                break
            ctx["next_pair"] = g["projected_pair"]
            env = None if local else swe_env.build_env(rows[tid], cache, logf=log)
            for arm in todo:
                r = run_trial(pair, order.index(arm), {"id": tid}, rows[tid], env, arm, g["eff_cap"], ctx)
                with open(results_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(r) + "\n")
                try:
                    os.remove(os.path.join(out, "inflight.json"))
                except OSError:
                    pass
                log(f"  {arm:<8} {'PASS' if r['passed'] else ('INFRA ' + r['infra'] if r['infra'] else 'fail'):<12} "
                    f"{r['tokens']:>9,} tok {r['steps']:>3} steps {r['secs']:>6.0f}s"
                    + (" CAPPED" if r["capped"] else "") + (" TIMEOUT" if r["timed_out"] else "")
                    + ("" if r["arm_ok"] else " ARM-NOT-VERIFIED") + (" est" if r["tokens_estimated"] else ""))
                if r["infra"] == "auth":
                    aborted = "the upstream rejected the key"
                elif r.get("stop"):
                    aborted = "ledger: " + r.get("why", "")
                elif r["infra"] == "launch" and ctx["first"]:
                    aborted = "harness host did not start the agent"
                elif not r["infra"] and r["steps"] and ARM_SHAPE[arm] not in r["shape_modes"]:
                    # The run would be void: DEFAULT_MODEL_NAME is not on the
                    # native tool route, or the thoughts header did not arrive.
                    aborted = (f"arm {arm} did not take effect (request shapes {r['shape_modes']}, "
                               f"want {ARM_SHAPE[arm]}); DEFAULT_MODEL_NAME must be a native tool model")
                if aborted:
                    break
                if not r["infra"]:
                    ctx["first"] = False
                ctx["next_pair"] = max(ctx["next_pair"] - r["tokens"], 0)
            if aborted:
                break
    except KeyboardInterrupt:
        aborted = "interrupted"
    finally:
        try:
            stop_proxy()
        except Exception:  # noqa: BLE001
            pass
        account_lost_inflight(out, results_path)

    text = report(read_results(results_path), meta)
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print()
    print(text)
    if aborted:
        log(f"STOPPED: {aborted}. Continue with: {resume}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
