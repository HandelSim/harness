#!/usr/bin/env python3
"""A/B benchmark of the proxy's prompt modes: hybrid (normal) vs single-message.

Entry point: `harness benchmark --test-modes [options]` (which sets HARNESS_BIN
and HARNESS_BENCH_INSTALL_ROOT and execs this). Docker-free: every trial is one
`harness host --yolo [--single-message] -p "<task prompt>"` run in a fresh
scratch copy of a task's seed files, scored by the task's own check.py.

Isolation and egress (what leaves the machine):
  * Trials run under a private install root, state/bench-modes/root/, whose .env
    is a symlink to yours, so the benchmark proxy has its own pid/log/port and
    never touches a `harness host` proxy you have running.
  * opencode gets a private XDG config/data/cache/state dir there too, so your
    global opencode config, plugins, MCP servers and sessions are not used.
  * HARNESS_HOST_NO_WEB=1: no webfetch/websearch tools, no Exa, sharing off.
    OPENCODE_DISABLE_MODELS_FETCH / _AUTOUPDATE / _SHARE / _LSP_DOWNLOAD /
    _CLAUDE_CODE / _DEFAULT_PLUGINS are set (no ~/.claude instructions, no
    auth-plugin installs) and OTEL_* is cleared.
  * So the task traffic goes only to the upstream API (PROXY_API_URL). The
    one-time public downloads (Node/opencode/jq/pip deps if missing, ripgrep
    if absent) fetch public packages and send no task or user data.
  * Results hold task ids, modes, pass/fail, timings and counts. The per-trial
    logs (agent output and launcher stderr tails) pass through the probe's
    redactor (key, URL, host, IPs, emails) plus home dir / user / hostname.

--mock swaps the upstream for tests/benchmarks/modes/mock_upstream.py on
loopback (no key, no network): a full dry run of the pipeline in both modes.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import math
import os
import platform
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TASKS_DIR = os.path.join(HERE, "tasks")
RUNS_DIR = os.path.join(REPO, "tests", "benchmarks", "runs")
IS_WINDOWS = os.name == "nt"
# Resolved via PATH: a bare "bash" on Windows can hit System32's WSL launcher
# before Git Bash.
BASH = shutil.which("bash") or "bash"

KNOWN_MODES = ("hybrid", "single", "user_front", "passthrough")
LOG_TAIL_CHARS = 4000


def log(msg: str) -> None:
    print(f"[test-modes] {msg}", flush=True)


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------


def load_tasks(only: list[str] | None) -> list[dict]:
    tasks = []
    for name in sorted(os.listdir(TASKS_DIR)):
        d = os.path.join(TASKS_DIR, name)
        if not os.path.isfile(os.path.join(d, "task.json")):
            continue
        with open(os.path.join(d, "task.json"), encoding="utf-8") as f:
            t = json.load(f)
        tasks.append({"id": name, "dir": d, "prompt": t["prompt"]})
    if only:
        known = {t["id"] for t in tasks}
        bad = [x for x in only if x not in known]
        if bad:
            raise SystemExit(f"unknown task id(s): {', '.join(bad)} (known: {', '.join(sorted(known))})")
        tasks = [t for t in tasks if t["id"] in only]
    return tasks


def build_plan(tasks: list[dict], modes: list[str], repeats: int) -> list[tuple[int, str, str]]:
    """Block-interleaved: each repeat runs every task in every mode, with the
    mode order alternating (AB, BA, ...) so drift over the night (upstream load,
    key age) does not land on one mode. Task order is shuffled per repeat with a
    fixed seed. One proxy restart per mode block, not per trial."""
    plan = []
    for r in range(repeats):
        order = list(modes) if r % 2 == 0 else list(reversed(modes))
        ids = [t["id"] for t in tasks]
        random.Random(1000 + r).shuffle(ids)
        for m in order:
            for tid in ids:
                plan.append((r, m, tid))
    return plan


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value on the discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def mean_ci(xs: list[float]) -> tuple[float, float]:
    """(mean, half-width of a normal-approx 95% CI)."""
    if not xs:
        return (float("nan"), float("nan"))
    m = sum(xs) / len(xs)
    if len(xs) < 2:
        return (m, float("nan"))
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return (m, 1.96 * sd / math.sqrt(len(xs)))


# --------------------------------------------------------------------------
# proxy log counters
# --------------------------------------------------------------------------

_RE_POST = re.compile(r"^\[(\w+)\] POST /\S*chat/completions .*?messages=(\d+) tools=(\d+)")
_RE_SHAPE = re.compile(r"^\[(\w+)\] upstream shape: mode=(\w+) messages=(\d+) chars=(\d+)")
_RE_ERR = re.compile(r"^\[\w+\] upstream (?:returned (\d{3})|request failed|returned non-JSON)")
_RE_ANY_STATUS = re.compile(r"upstream returned (\d{3})")
_RE_ID = re.compile(r"^\[(\w+)\] ")


def parse_proxy_log(text: str) -> dict:
    agent_ids, title_ids = set(), set()
    shapes = {}
    c = {"errors": 0, "transient_errors": 0, "auth_errors": 0, "malformed_retries": 0,
         "reasks": 0, "finishes": 0, "fatal": 0}
    for line in text.splitlines():
        m = _RE_POST.match(line)
        if m:
            (agent_ids if int(m.group(3)) > 0 else title_ids).add(m.group(1))
            continue
        m = _RE_SHAPE.match(line)
        if m:
            shapes[m.group(1)] = (m.group(2), int(m.group(3)), int(m.group(4)))
            continue
        # Count a request's later lines only if its POST is in this window: a
        # request left over from a killed trial must not score against the next.
        m = _RE_ID.match(line)
        if m and m.group(1) not in agent_ids and m.group(1) not in title_ids:
            continue
        m = _RE_ERR.match(line)
        if m:
            c["errors"] += 1
            code = m.group(1)
            # 5xx / 429 / network / garbled: the upstream's trouble, not the
            # prompt's. Other 4xx (e.g. a too-long single message) stay real.
            if code is None or code == "429" or code.startswith("5"):
                c["transient_errors"] += 1
        m = _RE_ANY_STATUS.search(line)
        if m and m.group(1) in ("401", "403"):
            c["auth_errors"] += 1
        if "in-proxy retry for malformed tool call" in line:
            c["malformed_retries"] += 1
        if "require-tool: rejected a tool-less" in line or "require-tool: could not obtain" in line:
            c["reasks"] += 1
        if "require-tool: consumed `finish`" in line:
            c["finishes"] += 1
        if "] FATAL:" in line:
            c["fatal"] += 1
    agent_shapes = [shapes[i] for i in agent_ids if i in shapes]
    c["requests"] = len(agent_ids)
    c["title_requests"] = len(title_ids)
    c["shape_modes"] = sorted({s[0] for s in agent_shapes})
    c["max_messages"] = max((s[1] for s in agent_shapes), default=0)
    c["min_messages"] = min((s[1] for s in agent_shapes), default=0)
    c["max_chars"] = max((s[2] for s in agent_shapes), default=0)
    c["total_chars"] = sum(s[2] for s in agent_shapes)
    return c


def mode_verified(mode: str, c: dict) -> bool:
    """Did the proxy really run this mode? Every agent request must report the
    mode, and single must send exactly one message each time."""
    if c["requests"] == 0:
        return False
    if c["shape_modes"] != [mode]:
        return False
    if mode == "single":
        return c["max_messages"] == 1
    return True


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


def make_redactor():
    base = None
    try:
        spec = importlib.util.spec_from_file_location(
            "_probe_upstream", os.path.join(REPO, "scripts", "probe_upstream.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        base = mod.Redactor(os.environ.get("PROXY_API_KEY", ""), os.environ.get("PROXY_API_URL", ""), "")
    except Exception as e:  # never run unredacted: fall back to a strict local one
        log(f"note: probe redactor unavailable ({type(e).__name__}); using the built-in one")
    literals = []
    home = os.path.expanduser("~")
    if len(home) > 1:
        literals.append((home, "~"))
        fwd = home.replace("\\", "/")  # Windows: C:/Users/x, and Git Bash's /c/Users/x
        literals.append((fwd, "~"))
        if re.match(r"^[A-Za-z]:/", fwd):
            literals.append(("/" + fwd[0].lower() + fwd[2:], "~"))
        if len(os.path.basename(home)) >= 3:
            literals.append((os.path.basename(home), "<user>"))
    for v, rep in ((_safe(getpass.getuser), "<user>"), (_safe(socket.gethostname), "<host>")):
        if v and len(v) >= 3:
            literals.append((v, rep))
    for k in ("PROXY_API_KEY", "CHATGPT_COOKIE_STRING", "PROXY_API_URL", "CHATGPT_BASE_URL"):
        v = os.environ.get(k, "")
        if len(v) >= 6:
            literals.append((v, f"<{k.lower()}>"))
    literals.sort(key=lambda p: -len(p[0]))
    url_re = re.compile(r"(?i)\b(?:https?|wss?)://(?!127\.0\.0\.1|localhost)[^\s\"'<>)\]}]+")

    def red(s: str) -> str:
        s = s or ""
        for lit, rep in literals:
            s = s.replace(lit, rep)
        if base is not None:
            s = base(s)
        return url_re.sub("<url>", s)
    return red


def _safe(fn):
    try:
        return fn()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _link_or_copy(src: str, dst: str, copy_ok: bool) -> None:
    aside = ""
    if os.path.lexists(dst):
        try:
            if os.path.samefile(src, dst):
                return  # already shared (symlink or Windows junction)
        except OSError:
            pass
        if os.path.islink(dst) or os.path.isfile(dst):
            os.remove(dst)
        else:
            # A real directory the bench provisioned itself (an earlier run
            # whose link failed). Move it aside, never delete it, and share yours.
            aside = f"{dst}.bench-local-{int(time.time())}"
            os.rename(dst, aside)
    try:
        os.symlink(src, dst)
    except OSError:
        if copy_ok and os.path.isfile(src):
            shutil.copy2(src, dst)
        elif IS_WINDOWS and os.path.isdir(src):
            # Windows symlinks need Developer Mode or admin; a directory junction
            # needs neither. Without one the bench root gets an empty toolchain
            # and re-downloads (and re-verifies) everything.
            try:
                import _winapi
                _winapi.CreateJunction(os.path.abspath(src), dst)
            except (ImportError, AttributeError, OSError):
                subprocess.run(["cmd", "/c", "mklink", "/J", dst, os.path.abspath(src)],
                               capture_output=True, stdin=subprocess.DEVNULL)
    if os.path.isdir(src) and not os.path.isdir(dst):
        if aside:
            os.rename(aside, dst)
        log(f"warning: could not link {src} into the bench root; the bench provisions its own copy")


def prepare_root(real_root: str, bench_root: str, mock_env: str | None) -> None:
    """Private install root: .env (yours, or the mock's), your reminder files,
    and the shared toolchain + proxy venv so nothing is downloaded twice."""
    host = os.path.join(bench_root, "state", "host")
    os.makedirs(host, exist_ok=True)
    env_dst = os.path.join(bench_root, ".env")
    if mock_env is not None:
        if os.path.islink(env_dst):
            os.remove(env_dst)
        with open(env_dst, "w", encoding="utf-8") as f:
            f.write(mock_env)
    else:
        _link_or_copy(os.path.join(real_root, ".env"), env_dst, copy_ok=True)
    for name in ("reminder.md", "tool-guidance.json"):
        src = os.path.join(real_root, name)
        if os.path.isfile(src):
            _link_or_copy(src, os.path.join(bench_root, name), copy_ok=True)
    real_host = os.path.join(real_root, "state", "host")
    tool = os.path.join(real_host, "toolchain")
    os.makedirs(tool, exist_ok=True)
    _link_or_copy(tool, os.path.join(host, "toolchain"), copy_ok=False)
    venv = os.path.join(real_host, "venv")
    if os.path.isdir(venv):
        _link_or_copy(venv, os.path.join(host, "venv"), copy_ok=False)


def write_python3_shim(shim_dir: str, exe: str) -> str:
    """A `python3` that runs `exe`. Git Bash on Windows usually resolves
    `python3` to the Microsoft Store stub, which fails, so agents (and the mock's
    scripted solutions) burn turns finding `python`; trials get this on PATH."""
    os.makedirs(shim_dir, exist_ok=True)
    with open(os.path.join(shim_dir, "python3"), "w", encoding="utf-8", newline="\n") as f:
        f.write('#!/bin/sh\nexec "%s" "$@"\n' % exe.replace("\\", "/"))
    return shim_dir


def child_env(bench_root: str, port: int, mock: bool, first: bool) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENCODE_", "OTEL_", "HARNESS_HOST_"))}
    if mock:
        for k in list(env):
            if k.startswith(("PROXY_", "CHATGPT_")) or k == "DEFAULT_MODEL_NAME":
                del env[k]
        np = env.get("NO_PROXY", "")
        env["NO_PROXY"] = env["no_proxy"] = (np + "," if np else "") + "127.0.0.1,localhost"
    xdg = os.path.join(bench_root, "xdg")
    env.update({
        # Forward slashes: valid for Windows and Git Bash alike (bash reads it).
        "HARNESS_INSTALL_ROOT": bench_root.replace("\\", "/") if IS_WINDOWS else bench_root,
        "HARNESS_HOST_PORT": str(port),
        "HARNESS_HOST_NO_WEB": "1",
        "HARNESS_HOST_CONFIRM": "1",
        "XDG_CONFIG_HOME": os.path.join(xdg, "config"),
        "XDG_DATA_HOME": os.path.join(xdg, "data"),
        "XDG_CACHE_HOME": os.path.join(xdg, "cache"),
        "XDG_STATE_HOME": os.path.join(xdg, "state"),
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_SHARE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    })
    if IS_WINDOWS:
        shim = write_python3_shim(os.path.join(bench_root, "bin"), sys.executable)
        env["PATH"] = shim + os.pathsep + env.get("PATH", "")
    if not first:
        env["HARNESS_SKIP_AUTH_PROBE"] = "1"
    env.pop("CI", None)
    return env


# Provision the host toolchain (jq/Node/opencode) and the proxy venv up front,
# untimed, so a first-ever host-mode run does not charge those downloads to the
# first trial's mode. No model API call: these only fetch public packages.
WARM_UP = (
    'HARNESS_SOURCE_ONLY=1 source "$0" >/dev/null || exit 1; '
    "host_require_python3 && ensure_dirs && host_ensure_toolchain && host_preflight "
    "&& host_proxy_ensure_venv"
)


def warm_up(harness_bin: str, env: dict, redact) -> str:
    """Return "" when ready, else why not (redacted). A failure here is the
    same failure every trial would hit, so the caller stops before trial 1."""
    log("preparing the host toolchain and proxy venv (one-time downloads only if missing)")
    try:
        # $0 = the harness script: it locates its clone (and scripts/lib) from $0.
        r = subprocess.run([BASH, "-c", WARM_UP, harness_bin], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "the toolchain/venv setup timed out after 30 min (slow or blocked downloads)"
    if r.returncode == 0:
        return ""
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()][-12:]
    return (f"the toolchain/venv setup failed (exit {r.returncode}), so no trial could start:\n"
            + "\n".join("    " + redact(ln) for ln in lines))


def inside_repo(path: str) -> str | None:
    p = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


# --------------------------------------------------------------------------
# one trial
# --------------------------------------------------------------------------


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _read_pid(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _tail(path: str, n: int = LOG_TAIL_CHARS) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            s = f.read()
        return s[-n:]
    except OSError:
        return ""


def run_trial(task: dict, mode: str, repeat: int, ctx: dict) -> dict:
    harness_bin, bench_root = ctx["harness"], ctx["bench_root"]
    proxy_log = os.path.join(bench_root, "state", "host", "proxy.log")
    pidfile = os.path.join(bench_root, "state", "host", "proxy.pid")
    pid_before = _read_pid(pidfile)
    try:
        offset = os.path.getsize(proxy_log)
    except OSError:
        offset = 0

    scratch = tempfile.mkdtemp(prefix="hb-modes-")
    work = os.path.join(scratch, "w")
    shutil.copytree(os.path.join(task["dir"], "files"), work)
    keep = os.path.join(work, ".keep")
    if os.path.exists(keep):
        os.remove(keep)
    out_path, err_path = os.path.join(scratch, "stdout"), os.path.join(scratch, "stderr")

    cmd = [harness_bin, "host", "--yolo"]
    if mode == "single":
        cmd.append("--single-message")
    elif mode != "hybrid":
        cmd += ["--prompt-mode", mode]
    cmd += ["-p", task["prompt"]]
    if IS_WINDOWS:
        cmd = [BASH] + cmd

    env = child_env(bench_root, ctx["port"], ctx["mock"], first=ctx["first"])
    t0 = time.monotonic()
    timed_out = False
    with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
        proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, **kw)
        try:
            rc = proc.wait(timeout=ctx["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            rc = proc.returncode if proc.returncode is not None else -9
        except BaseException:  # Ctrl-C: the child is in its own session, so stop it here
            _kill_tree(proc)
            shutil.rmtree(scratch, ignore_errors=True)
            raise
    secs = time.monotonic() - t0

    # A restarted proxy (mode switch, or the kill above) truncates its log.
    pid_after = _read_pid(pidfile)
    try:
        with open(proxy_log, encoding="utf-8", errors="replace") as f:
            if pid_before and pid_before == pid_after and os.path.getsize(proxy_log) >= offset:
                f.seek(offset)
            text = f.read()
    except OSError:
        text = ""
    counters = parse_proxy_log(text)

    try:
        chk = subprocess.run([sys.executable, os.path.join(task["dir"], "check.py")], cwd=work,
                             capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        passed = chk.returncode == 0
        reason = (chk.stdout.strip().splitlines() or [""])[0][:200]
    except subprocess.TimeoutExpired:
        passed, reason = False, "check timed out"

    infra = ""
    err_tail = _tail(err_path)
    if counters["auth_errors"] or re.search(
            r"(?i)unlock_url|key (?:is )?locked|key rejected|rejected the API key", err_tail):
        infra = "auth"
    elif counters["requests"] == 0:
        # Nothing reached the proxy (even if it then timed out, e.g. a hung
        # auth probe), so the prompt mode cannot be what failed.
        infra = "launch"
    elif not passed and counters["transient_errors"] and (
            timed_out or counters["transient_errors"] >= counters["requests"]):
        # An upstream outage (5xx/429/network) sank this trial; whichever mode
        # happened to be running would have failed. Excluded and retried.
        infra = "upstream"

    red = ctx["redact"]
    trial_id = f"r{repeat}-{mode}-{task['id']}"
    with open(os.path.join(ctx["out"], "trials", trial_id + ".log"), "w", encoding="utf-8") as f:
        f.write(f"# {trial_id}  passed={passed}  rc={rc}  timed_out={timed_out}  secs={secs:.1f}\n")
        f.write(f"# check: {reason}\n# counters: {json.dumps(counters)}\n\n")
        f.write("## agent output (tail, redacted)\n")
        f.write(red(_tail(out_path)) + "\n\n## launcher stderr (tail, redacted)\n")
        f.write(red(err_tail) + "\n")

    if not ctx["keep"]:
        shutil.rmtree(scratch, ignore_errors=True)
    return {
        "task": task["id"], "mode": mode, "repeat": repeat, "passed": passed, "reason": reason,
        "secs": round(secs, 1), "rc": rc, "timed_out": timed_out, "infra": infra,
        "mode_ok": mode_verified(mode, counters), **counters,
    }


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _fmt_ci(m: float, h: float, unit: str = "") -> str:
    if math.isnan(m):
        return "n/a"
    return f"{m:.1f}{unit}" + ("" if math.isnan(h) else f" +/- {h:.1f}")


def report(results: list[dict], modes: list[str], meta: dict) -> str:
    good = [r for r in results if not r["infra"]]
    infra = [r for r in results if r["infra"]]
    L = []
    L.append("harness benchmark --test-modes report")
    L.append(f"generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    for k in ("upstream", "model", "repeats", "timeout_s", "tasks"):
        if k in meta:
            L.append(f"{k}: {meta[k]}")
    L.append(f"trials: {len(results)} ({len(infra)} infra failures excluded below)")
    L.append("")
    L.append("Per mode (95% CI; time and requests are per trial)")
    L.append(f"  {'mode':<12} {'pass':>7} {'rate':>6} {'95% CI':>13} {'time s':>16} {'requests':>14} {'errors':>6} {'timeouts':>8} {'mode ok':>8}")
    by = {m: [r for r in good if r["mode"] == m] for m in modes}
    for m in modes:
        rs = by[m]
        k, n = sum(r["passed"] for r in rs), len(rs)
        lo, hi = wilson(k, n)
        t = mean_ci([r["secs"] for r in rs])
        q = mean_ci([r["requests"] for r in rs])
        rate = f"{100 * k / n:.0f}%" if n else "n/a"
        frac, ci = f"{k}/{n}", f"{100 * lo:.0f}-{100 * hi:.0f}%"
        errs = sum(r["errors"] for r in rs)
        touts = sum(r["timed_out"] for r in rs)
        mok = f"{sum(r['mode_ok'] for r in rs)}/{n}"
        L.append(f"  {m:<12} {frac:>7} {rate:>6} {ci:>13} {_fmt_ci(*t):>16} {_fmt_ci(*q):>14} "
                 f"{errs:>6} {touts:>8} {mok:>8}")
    L.append("")

    base = modes[0]
    verdicts = []
    for other in modes[1:]:
        pairs = {}
        for r in good:
            if r["mode"] in (base, other):
                pairs.setdefault((r["task"], r["repeat"]), {})[r["mode"]] = r
        both = [p for p in pairs.values() if base in p and other in p]
        b = sum(1 for p in both if p[base]["passed"] and not p[other]["passed"])
        c = sum(1 for p in both if not p[base]["passed"] and p[other]["passed"])
        pv = mcnemar_exact(b, c)
        dt = mean_ci([p[other]["secs"] - p[base]["secs"] for p in both])
        dq = mean_ci([p[other]["requests"] - p[base]["requests"] for p in both])
        L.append(f"Paired: {other} vs {base} over {len(both)} (task, repeat) pairs")
        L.append(f"  only {base} passed: {b}   only {other} passed: {c}   exact McNemar p = {pv:.3f}")
        L.append(f"  time difference ({other} - {base}): {_fmt_ci(*dt, unit=' s')}")
        L.append(f"  request difference ({other} - {base}): {_fmt_ci(*dq)}")
        L.append("")
        verdicts.append(_verdict(base, other, b, c, pv, dt, dq, len(both)))

    L.append("Per task (passes / trials, mean seconds)")
    hdr = f"  {'task':<20}" + "".join(f" {m:>18}" for m in modes)
    L.append(hdr)
    for tid in sorted({r["task"] for r in results}):
        row = f"  {tid:<20}"
        for m in modes:
            rs = [r for r in good if r["task"] == tid and r["mode"] == m]
            if rs:
                cell = f"{sum(r['passed'] for r in rs)}/{len(rs)}, {sum(r['secs'] for r in rs) / len(rs):.0f}s"
                row += f" {cell:>18}"
            else:
                row += f" {'-':>18}"
        L.append(row)
    L.append("")

    bad_mode = [r for r in good if not r["mode_ok"]]
    if bad_mode:
        L.append("WARNING: these trials did not show the expected mode in the proxy log, so")
        L.append("they may not measure what they claim (see trials/<id>.log):")
        for r in bad_mode:
            L.append(f"  r{r['repeat']}-{r['mode']}-{r['task']}: modes={r['shape_modes']} max_messages={r['max_messages']}")
        L.append("")
    if infra:
        L.append("Infra failures (not scored):")
        for r in infra:
            L.append(f"  r{r['repeat']}-{r['mode']}-{r['task']}: {r['infra']} (rc={r['rc']})")
        L.append("")
    L.append("Verdict")
    for v in verdicts:
        L.append("  " + v)
    return "\n".join(L) + "\n"


def _verdict(base, other, b, c, pv, dt, dq, n) -> str:
    if n == 0:
        return f"{other} vs {base}: no complete pairs, nothing to compare."
    if pv < 0.05:
        better = other if c > b else base
        s = f"{better} passes significantly more often (p={pv:.3f}, {b}+{c} discordant pairs of {n})."
    else:
        s = (f"no significant pass-rate difference between {other} and {base} (p={pv:.3f}; "
             f"{b + c} of {n} pairs disagreed). With this few pairs only a large gap would show; "
             f"add --repeats to tighten it.")
    extra = []
    m, h = dt
    if not math.isnan(h) and abs(m) > h:
        extra.append(f"{other} is {'slower' if m > 0 else 'faster'} ({m:+.1f} +/- {h:.1f} s per trial)")
    m, h = dq
    if not math.isnan(h) and abs(m) > h:
        extra.append(f"{other} uses {'more' if m > 0 else 'fewer'} requests ({m:+.1f} +/- {h:.1f} per trial)")
    if extra:
        s += " Significant: " + "; ".join(extra) + "."
    return s


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def read_results(path: str) -> list[dict]:
    out = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="harness benchmark --test-modes",
                                 description="A/B the proxy's normal (hybrid) mode against --single-message.")
    ap.add_argument("--repeats", type=int, default=3, help="runs of every task in every mode (default 3)")
    ap.add_argument("--modes", default="hybrid,single", help="comma list; the first is the baseline (default hybrid,single)")
    ap.add_argument("--task-ids", default="", help="comma list of task ids (default: all)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per trial before it is killed and failed (default 900)")
    ap.add_argument("--mock", action="store_true", help="dry run against a scripted local mock upstream (no key, no network)")
    ap.add_argument("--resume", default="", metavar="DIR", help="continue an interrupted run in DIR, skipping finished trials")
    ap.add_argument("--report", default="", metavar="DIR", help="only rebuild DIR/report.txt from DIR/results.jsonl")
    ap.add_argument("--list", action="store_true", help="print the tasks and the plan, run nothing")
    ap.add_argument("--keep-workdirs", action="store_true", help="keep each trial's scratch directory")
    a = ap.parse_args(argv)

    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    if len(modes) < 2 or len(set(modes)) != len(modes) or any(m not in KNOWN_MODES for m in modes):
        ap.error(f"--modes needs two or more distinct modes from {', '.join(KNOWN_MODES)}")
    if a.repeats < 1 or a.timeout < 30:
        ap.error("--repeats must be >= 1 and --timeout >= 30")

    if a.report:
        meta = {}
        mp = os.path.join(a.report, "meta.json")
        if os.path.isfile(mp):
            with open(mp, encoding="utf-8") as f:
                meta = json.load(f)
            modes = meta.get("modes", modes)
        text = report(read_results(os.path.join(a.report, "results.jsonl")), modes, meta)
        with open(os.path.join(a.report, "report.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        print(text)
        return 0

    tasks = load_tasks([x.strip() for x in a.task_ids.split(",") if x.strip()] or None)
    plan = build_plan(tasks, modes, a.repeats)
    est_lo, est_hi = len(plan) * 2, len(plan) * 6
    if a.list:
        for t in tasks:
            print(f"{t['id']:<20} {t['prompt'][:100]}")
        print(f"\n{len(plan)} trials ({len(tasks)} tasks x {len(modes)} modes x {a.repeats} repeats); "
              f"roughly {est_lo // 60}h{est_lo % 60:02d}m-{est_hi // 60}h{est_hi % 60:02d}m against the real upstream")
        return 0

    harness_bin = os.environ.get("HARNESS_BIN") or os.path.join(REPO, "harness")
    real_root = os.environ.get("HARNESS_BENCH_INSTALL_ROOT") or REPO
    if not a.mock and not os.path.isfile(os.path.join(real_root, ".env")):
        raise SystemExit(f"no .env in {real_root}: configure harness first (harness install / harness config)")

    probe_dir = tempfile.mkdtemp(prefix="hb-modes-probe-")
    repo_hit = inside_repo(probe_dir)
    shutil.rmtree(probe_dir, ignore_errors=True)
    if repo_hit:
        raise SystemExit(f"the temp dir is inside a git repo ({repo_hit}); opencode would load that repo's "
                         f"AGENTS.md into every trial. Point TMPDIR somewhere else.")

    out = a.resume or os.path.join(RUNS_DIR, ("modes-mock-" if a.mock else "modes-")
                                   + datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(out, "trials"), exist_ok=True)
    results_path = os.path.join(out, "results.jsonl")
    done = {(r["task"], r["mode"], r["repeat"]) for r in read_results(results_path) if not r.get("infra")}
    if a.resume:
        # A resumed run keeps only the scored trials; infra failures are retried.
        kept = [r for r in read_results(results_path) if not r.get("infra")]
        with open(results_path, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r) + "\n")

    red = make_redactor()
    mock_proc = None
    state_root = os.path.join(real_root, "state")
    bench_root = os.path.join(state_root, "bench-modes", "mock-root" if a.mock else "root")
    port = free_port()
    meta = {"modes": modes, "repeats": a.repeats, "timeout_s": a.timeout,
            "tasks": ",".join(t["id"] for t in tasks),
            "model": red(os.environ.get("DEFAULT_MODEL_NAME", "")) if not a.mock else "mock-model",
            "upstream": "mock (loopback)" if a.mock else "PROXY_API_URL from your .env (redacted)",
            "host": f"{platform.system()} {platform.machine()}"}
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)

    ctx = {"harness": harness_bin, "bench_root": bench_root, "port": port, "mock": a.mock,
           "timeout": a.timeout, "redact": red, "out": out, "keep": a.keep_workdirs, "first": True}

    def stop_proxy():
        subprocess.run(([] if not IS_WINDOWS else [BASH]) + [harness_bin, "host", "down"],
                       env=child_env(bench_root, port, a.mock, first=False),
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=60)

    todo = [p for p in plan if (p[2], p[1], p[0]) not in done]
    log(f"{len(plan)} trials planned, {len(todo)} to run; output -> {out}")
    log(f"estimate: {len(todo) * 2 // 60}h{len(todo) * 2 % 60:02d}m-{len(todo) * 6 // 60}h{len(todo) * 6 % 60:02d}m"
        + (" (mock runs are much faster)" if a.mock else ""))
    log("egress: task traffic goes only to the upstream API via the local proxy; opencode web tools, "
        "sharing, model-list fetch, autoupdate and telemetry are off")
    by_id = {t["id"]: t for t in tasks}
    aborted = ""
    # --resume rebuilds the plan from the options, so the hint repeats them.
    resume = (f"harness benchmark --test-modes --resume {out} --repeats {a.repeats} --modes {','.join(modes)}"
              + (f" --task-ids {a.task_ids}" if a.task_ids else "")
              + (f" --timeout {a.timeout}" if a.timeout != 900 else "")
              + (" --mock" if a.mock else ""))
    upstream_streak = 0
    try:
        if a.mock:
            mport = free_port()
            mock_proc = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "mock_upstream.py"), "--port", str(mport),
                 "--tasks-dir", TASKS_DIR, "--log", os.path.join(out, "mock.log"),
                 "--delay", os.environ.get("HARNESS_BENCH_MOCK_DELAY", "0"),
                 "--status", os.environ.get("HARNESS_BENCH_MOCK_STATUS", "0")],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", mport), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            mock_env = (f"PROXY_API_URL=http://127.0.0.1:{mport}\nPROXY_API_KEY=mock-key\n"
                        f"DEFAULT_MODEL_NAME=mock-model\n")
            prepare_root(real_root, bench_root, mock_env)
        else:
            prepare_root(real_root, bench_root, None)
        # Restart from a clean proxy so a stale one from an earlier run (other
        # port, other mode) is never reused.
        stop_proxy()
        # Drop the stopped proxy's log too: a trial that dies before its own
        # proxy starts would otherwise count an earlier run's requests.
        for stale in ("proxy.log", "proxy.pid"):
            try:
                os.remove(os.path.join(bench_root, "state", "host", stale))
            except OSError:
                pass
        aborted = warm_up(harness_bin, child_env(bench_root, port, a.mock, first=False), red)
        if aborted:
            aborted += f"\n  Fix that, then continue with: {resume}"

        for i, (rep, mode, tid) in enumerate([] if aborted else todo, 1):
            r = run_trial(by_id[tid], mode, rep, ctx)
            with open(results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(r) + "\n")
            mark = "PASS" if r["passed"] else ("INFRA " + r["infra"] if r["infra"] else "fail")
            log(f"[{i}/{len(todo)}] r{rep} {mode:<7} {tid:<18} {mark:<12} {r['secs']:>6.0f}s "
                f"req={r['requests']} err={r['errors']}" + ("" if r["mode_ok"] else " MODE-NOT-VERIFIED")
                + (" TIMEOUT" if r["timed_out"] else ""))
            if r["infra"] == "auth":
                aborted = ("the upstream rejected the key (locked or invalid). Unlock it (harness doctor shows "
                           f"the unlock URL), then continue with: {resume}")
                break
            if r["infra"] == "launch" and i <= 2:
                aborted = (f"harness host did not start the agent; see {os.path.join(out, 'trials')} "
                           f"for the redacted launcher output. Fix that, then continue with: {resume}")
                break
            upstream_streak = upstream_streak + 1 if r["infra"] == "upstream" else 0
            if upstream_streak >= 3:
                aborted = ("3 trials in a row lost to upstream errors (5xx/429/network); the upstream looks "
                           f"down. Continue later with: {resume}")
                break
            if not r["infra"]:
                ctx["first"] = False
    except KeyboardInterrupt:
        aborted = f"interrupted. Continue with: {resume}"
    finally:
        try:
            stop_proxy()
        except Exception:
            pass
        if mock_proc is not None:
            mock_proc.terminate()
            try:
                mock_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                mock_proc.kill()

    text = report(read_results(results_path), modes, meta)
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print()
    print(text)
    log(f"report: {os.path.join(out, 'report.txt')}")
    if aborted:
        log("STOPPED: " + aborted)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
