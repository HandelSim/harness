"""The findings agent's card on the queue page: what it does, Start / Stop, the owner's notes.

It reads <state>/findings/agent.json (scripts/findings_agent.py writes it) and
starts the agent the way the queue page starts a queue run: a detached
`bash harness findings start` with no terminal.

  POST /runs/findings/agent/start   start it (in the project harness serve runs in, when given)
  POST /runs/findings/agent/stop    stop it
  POST /runs/findings/agent/notes   notes=TEXT: the notes added to every pass (at most 4000 chars)

The card lists the agent's last sessions (ledger.json "passes"), each linked to
its page on /runs. resume() is what harness serve calls at its start: the agent
the owner started and did not stop (findings/wanted) starts again.

The routes and the page region are wired in pub.py and runs.py; this module only
renders and acts.
"""

import html
import json
import os
import shutil
import subprocess
import time

esc = html.escape

NOTES_MAX = 4000
STALE_BEAT = 300          # a heartbeat older than this, while not stopped: the agent is not answering
STATES = {"exploring": ("t-run", "exploring"), "scanning": ("t-run", "exploring"),
          "waiting": ("t-run", "waiting between passes"),
          "idle": ("t-ok", "idle: everything is scanned"), "backoff": ("t-wait", "backing off"),
          "paused": ("t-wait", "paused"), "stopping": ("t-off", "stopping"), "stopped": ("t-off", "stopped")}
LENSES = ("bug", "ui", "cleanup")
SESSIONS_SHOWN = 8
OUTCOME_TONE = {"found": "t-ok", "none": "t-off", "running": "t-run", "failed": "t-bad", "stopped": "t-off"}
# the card's own rules; the page adds them to its stylesheet (no inline styles: strict CSP)
AGENT_CSS = """
.fagent .fa-info{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:8px 0}
.fagent .fa-info dt{color:var(--muted,#777)}
.fagent .fa-info dd{margin:0;overflow-wrap:anywhere}
.fagent .fa-counts{border-collapse:collapse;margin:6px 0}
.fagent .fa-counts th,.fagent .fa-counts td{padding:2px 10px 2px 0;text-align:left}
.fagent .fa-notes textarea{width:100%;box-sizing:border-box}
.fagent .fa-passes{list-style:none;padding:0;margin:6px 0}
.fagent .fa-passes li{padding:2px 0;overflow-wrap:anywhere}
"""


def _read(path, cap=256 * 1024):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except OSError:
        return ""


def read_status(state_dir):
    """agent.json as a dict ({} when there is none or it does not parse)."""
    try:
        data = json.loads(_read(os.path.join(state_dir, "agent.json")) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return True   # no cheap check; the heartbeat says the rest
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _age(info, t):
    beat = info.get("heartbeat_ts")
    return int(t - beat) if isinstance(beat, (int, float)) and not isinstance(beat, bool) else None


def _running(info, t):
    """Live: not stopped, its pid alive and its heartbeat fresh."""
    age = _age(info, t)
    return (info.get("state") not in (None, "", "stopped") and _alive(info.get("pid"))
            and age is not None and age <= STALE_BEAT)


def _held(state_dir):
    """True while a findings agent holds state_dir/agent.lock (its flock: what
    says it runs, whatever agent.json says after a restart); None where flock is not,
    or no agent has made the lock yet (agent.json is asked then)."""
    try:
        import fcntl
    except ImportError:
        return None
    path = os.path.join(state_dir, "agent.lock")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return None
    return False


def _ago(secs):
    if secs is None:
        return "never"
    if secs < 90:
        return f"{max(0, secs)} s ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


def _s(v, cap=300):
    return " ".join(v.split())[:cap] if isinstance(v, str) else ""


def _counts(info):
    c = info.get("counts")
    if not isinstance(c, dict):
        return ""
    rows = []
    for lens in sorted(c, key=lambda k: (LENSES.index(k) if k in LENSES else 9, k)):
        v = c[lens]
        if not isinstance(v, dict):
            continue

        def n(k):
            x = v.get(k, 0)
            return x if isinstance(x, int) and not isinstance(x, bool) else 0
        rows.append(f"<tr><td>{esc(_s(lens, 20))}</td><td>{n('calls')}</td><td>{n('accepted')}</td>"
                    f"<td>{n('rejected')}</td><td>{n('failed')}</td></tr>")
    if not rows:
        return ""
    return ("<table class='fa-counts'><thead><tr><th>question</th><th>sessions</th><th>kept</th>"
            "<th>rejected</th><th>failed</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def passes(state_dir, n=SESSIONS_SHOWN):
    """The agent's last sessions, newest first: ledger.json "passes" entries that parse."""
    try:
        data = json.loads(_read(os.path.join(state_dir, "ledger.json"), 4 * 1024 * 1024) or "{}")
    except ValueError:
        return []
    got = data.get("passes") if isinstance(data, dict) else None
    if not isinstance(got, list):
        return []
    return [p for p in reversed(got) if isinstance(p, dict)][:n]


def _passes(state_dir):
    rows = []
    for p in passes(state_dir):
        sid = _s(p.get("session"), 80)
        outcome = _s(p.get("outcome"), 20)
        label = f"{os.path.basename(_s(p.get('repo'), 300))}: {_s(p.get('area'), 200)}/ ({_s(p.get('lens'), 20)})"
        what = esc(label)
        if sid.startswith("ses_") and all(c.isalnum() or c == "_" for c in sid):
            what = f"<a href='/runs/{esc(sid)}'>{what}</a>"
        n = p.get("found")
        tail = f" {n} new" if isinstance(n, int) and not isinstance(n, bool) and n else ""
        cov = _s(p.get("covered"), 300)
        rows.append(f"<li><span class='tag {OUTCOME_TONE.get(outcome, 't-off')}'>{esc(outcome or '?')}</span> "
                    f"{what}<span class='muted'>{esc(tail)} {esc(_s(p.get('started'), 30))}"
                    + (f" &middot; {esc(cov)}" if cov else "") + "</span></li>")
    if not rows:
        return ""
    return ("<details class='fa-sessions' data-k='findings-agent-sessions' open><summary>Its last sessions</summary>"
            "<ul class='fa-passes'>" + "".join(rows) + "</ul></details>")


def agent_card(state_dir, flash=""):
    """The card's HTML. Everything from agent.json and notes.md is escaped."""
    t = time.time()
    info = read_status(state_dir) if state_dir else {}
    live = _running(info, t)
    age = _age(info, t)
    state = _s(info.get("state"), 20)
    if not info:
        cls, label = "t-off", "never started"
    elif live:
        cls, label = STATES.get(state, ("t-off", state or "unknown"))
    elif state in ("", "stopped"):
        cls, label = "t-off", "stopped"
    else:   # it says it works but its pid is gone or it stopped beating
        cls, label = "t-bad", f"not answering (last: {state or 'unknown'}, heartbeat {_ago(age)})"
    parts = ["<section class='panel fagent' id='findings-agent'><div class='hd'><h2>Findings agent "
             f"<span class='tag {cls}'>{esc(label)}</span></h2>"
             "<span class='muted'>opencode sessions on a free model that explore the committed code, an area "
             "at a time, and follow it across files for bugs, UI problems and code to remove or simplify; "
             "read-only</span></div>"]
    if flash:
        parts.append(f"<div class='flash'>{esc(flash)}</div>")
    rows = []
    model = _s(info.get("model"), 120)
    if model:
        rows.append(("model", model))
    if info:
        rows.append(("heartbeat", _ago(age) + (" (stale)" if age is not None and age > STALE_BEAT
                                               and state not in ("", "stopped") else "")))
    unit = _s(info.get("unit"), 400)
    if live and unit:
        rows.append(("on", unit))
    for key, name in (("reason", "why"), ("next_at", "next")):
        v = _s(info.get(key), 400)
        if live and v:
            rows.append((name, v))
    repos = info.get("repos")
    if isinstance(repos, list) and repos:
        rows.append(("repos", ", ".join(_s(r, 300) for r in repos[:10] if isinstance(r, str))))
    sid = _s(info.get("session"), 80)
    if live and sid.startswith("ses_"):
        rows.append(("session", sid))
    for key, name in (("open", "open findings"), ("todo", "passes to do")):
        v = info.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            rows.append((name, str(v)))
    err = _s(info.get("last_error"), 600)
    if err:
        rows.append(("last error", err))
    if rows:
        parts.append("<dl class='fa-info'>" + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
                     + "</dl>")
    parts.append(_counts(info))
    parts.append(_passes(state_dir) if state_dir else "")
    if live:
        act = ("<form method='post' action='/runs/findings/agent/stop' data-confirm='Stop the findings agent? "
               "Its session in flight is ended and that pass runs again later. It stays stopped, "
               "also after a restart, until you start it.'>"
               "<button type='submit' class='btn sm'>Stop</button></form>")
    else:
        act = ("<form method='post' action='/runs/findings/agent/start'>"
               "<button type='submit' class='btn sm primary'>Start</button></form>")
    # FINDINGS_MODEL lives in .env; the Settings page edits it. The agent reads it when it starts.
    model_link = ("<a class='muted' href='/settings#model'>Change the model in Settings"
                  + (" (then Stop and Start)" if live else "") + "</a>")
    parts.append(f"<div class='acts'>{act}{model_link}</div>")
    notes = _read(os.path.join(state_dir, "notes.md"), NOTES_MAX * 4)[:NOTES_MAX] if state_dir else ""
    parts.append("<details class='fa-notes' data-k='findings-agent-notes'><summary>Notes for every pass"
                 + (f" <span class='muted'>{len(notes)} characters</span>" if notes else "") + "</summary>"
                 "<form method='post' action='/runs/findings/agent/notes'>"
                 f"<textarea name='notes' rows='6' maxlength='{NOTES_MAX}' aria-label='notes for the findings "
                 "agent' placeholder='What to look for or leave alone, e.g. &quot;tests/ fixtures are meant to be "
                 f"broken&quot;'>{esc(notes)}</textarea>"
                 "<div class='acts'><button type='submit' class='btn sm'>Save notes</button>"
                 "<span class='muted'>a change asks about every part again, with the new notes</span></div>"
                 "</form></details>")
    parts.append("</section>")
    return "".join(parts)


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _start(state_dir, harness_path, project):
    if not os.path.isfile(harness_path or ""):
        return False, f"no harness script at {harness_path} (pub.py --harness)"
    bash = shutil.which("bash")
    if not bash:
        return False, "bash is not on PATH, so harness cannot run"
    held = _held(state_dir)
    if held or (held is None and _running(read_status(state_dir), time.time())):
        return False, "the findings agent is already running"
    env = dict(os.environ)
    env["HARNESS_OPEN"] = "0"
    parent = os.path.dirname(os.path.abspath(state_dir))
    if (not env.get("HARNESS_INSTALL_ROOT") and os.path.basename(os.path.normpath(state_dir)) == "findings"
            and os.path.basename(parent) == "state"):
        env["HARNESS_INSTALL_ROOT"] = os.path.dirname(parent)   # the agent this page shows: <root>/state/findings
    # `harness findings start` scans the git project it is started in, else the ones it scanned last.
    cwd = project if project and os.path.isdir(project) else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = parent
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kw["start_new_session"] = True
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "agent.out"), "ab") as out:
            out.write(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} started from the queue page: "
                      f"harness findings start (in {cwd})\n".encode())
            out.flush()
            proc = subprocess.Popen([bash, harness_path, "findings", "start"], stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, cwd=cwd, env=env, **kw)
    except OSError as e:
        return False, f"cannot start harness: {e.strerror or e}"
    # `findings start` returns within seconds: it starts the agent detached and checks it is alive.
    try:
        rc = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        return True, "the findings agent is starting"
    if rc != 0:
        tail = [x for x in _read(os.path.join(state_dir, "agent.out")).splitlines() if x.strip()][-1:]
        return False, f"harness findings start failed (status {rc})" + (f": {tail[0][:300]}" if tail else "")
    return True, "the findings agent started"


def _stop(state_dir, harness_path):
    if not os.path.isfile(harness_path or ""):
        return False, f"no harness script at {harness_path} (pub.py --harness)"
    bash = shutil.which("bash")
    if not bash:
        return False, "bash is not on PATH, so harness cannot run"
    env = dict(os.environ)
    parent = os.path.dirname(os.path.abspath(state_dir))
    if not env.get("HARNESS_INSTALL_ROOT") and os.path.basename(parent) == "state":
        env["HARNESS_INSTALL_ROOT"] = os.path.dirname(parent)
    try:
        r = subprocess.run([bash, harness_path, "findings", "stop"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=45, env=env)
    except subprocess.TimeoutExpired:
        return True, "asked the findings agent to stop; it ends within a minute"
    except OSError as e:
        return False, f"cannot run harness: {e.strerror or e}"
    last = [x for x in (r.stdout + r.stderr).splitlines() if x.strip()][-1:]
    return r.returncode == 0, (last[0][:300] if last else f"harness findings stop: status {r.returncode}")


def agent_action(state_dir, harness_path, action, form, project=""):
    """Do one of start, stop, notes. Returns (ok, message). form maps names to lists
    of values (parse_qs); project is the folder harness serve runs in, when known."""
    if not state_dir:
        return False, "this viewer has no state folder (pub.py --queue-dir)"
    if action == "start":
        return _start(state_dir, harness_path, project)
    if action == "stop":
        return _stop(state_dir, harness_path)
    if action == "notes":
        vals = form.get("notes") or [""]
        text = vals[0] if isinstance(vals, list) else str(vals)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if len(text) > NOTES_MAX:
            return False, f"the notes are {len(text)} characters; at most {NOTES_MAX}"
        try:
            _write_atomic(os.path.join(state_dir, "notes.md"), text.strip() + ("\n" if text.strip() else ""))
        except OSError as e:
            return False, f"cannot save the notes: {e.strerror or e}"
        return True, "notes saved: the agent uses them from its next pass"
    return False, f"unknown action: {action}"


def resume(state_dir, harness_path, project=""):
    """At harness serve's start: the agent the owner started and has not stopped
    (state_dir/wanted, which only an owner's stop takes away) starts again when
    it is not running, the machine having gone down under it. FINDINGS_AUTORESUME=0:
    never. Returns (ok, message), or None when there is nothing to do."""
    if os.environ.get("FINDINGS_AUTORESUME", "1").strip() == "0" or not state_dir:
        return None
    held = _held(state_dir)
    if not os.path.exists(os.path.join(state_dir, "wanted")) or held or (
            held is None and _running(read_status(state_dir), time.time())):
        return None
    return _start(state_dir, harness_path, project)
