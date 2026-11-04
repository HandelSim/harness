"""The findings agent's card on the queue page: what it does, Start / Stop, the owner's notes.

It reads <state>/findings/agent.json (scripts/findings_agent.py writes it) and
starts the agent the way the queue page starts a queue run: a detached
`bash harness findings start` with no terminal.

  POST /runs/findings/agent/start   start it: `harness findings start DIR...` on the folders ticked
                                    or typed (pick=1, dir=PATH..., more=one per line: checked, and
                                    saved when they change the choice), else on the saved ones, else
                                    as before any was saved (no DIR, in the project harness serve runs in)
  POST /runs/findings/agent/folders save the folders for the next start (act=reset: forget them)
  POST /runs/findings/agent/stop    stop it
  POST /runs/findings/agent/notes   notes=TEXT: the notes added to every pass (at most 4000 chars)

The chosen folders are kept in <state>/findings/folders.json ({"version": 1, "folders": [...]}).

The card lists the agent's last sessions (ledger.json "passes"), each linked to
its page on /runs, and links to the notes the passes wrote on each area
(knowledge/index.json, knowledge/<repo>/<area>.md): /runs/findings/notes. resume() is what harness serve calls at its start: the agent
the owner started and did not stop (findings/wanted) starts again: on the saved folders, or
on the repositories it last ran on when it was started on those after they were saved (as by
`harness findings start DIR` in a terminal).

The routes and the page region are wired in pub.py and runs.py; this module only
renders and acts.
"""

import html
import json
import os
import re
import shutil
import subprocess
import threading
import time

esc = html.escape

NOTES_MAX = 4000
STALE_BEAT = 300          # a heartbeat older than this, while not stopped: the agent is not answering
STATES = {"exploring": ("t-run", "exploring"), "scanning": ("t-run", "exploring"),
          "waiting": ("t-run", "waiting between passes"),
          "idle": ("t-wait", "idle"), "backoff": ("t-wait", "backing off"),
          "paused": ("t-wait", "paused"), "stopping": ("t-off", "stopping"), "stopped": ("t-off", "stopped")}
LENSES = ("bug", "trace", "ui", "cleanup")
SLUG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,200}$")   # a knowledge/ folder or file name
NOTE_MAX = 64 * 1024      # bytes of one area's note read
INDEX_MAX = 4 * 1024 * 1024
SESSIONS_SHOWN = 8
FOLDERS_MAX = 20          # folders the agent is given at most
FOLDER_LEN = 1024         # characters in one folder's path
KNOWN_MAX = 20            # folders offered as checkboxes
FORM_MAX = 128 * 1024     # bytes of one of the card's forms (the folders' paths, the notes)
FORM_FIELDS = FOLDERS_MAX + KNOWN_MAX + 8
ACTIONS = ("start", "stop", "notes", "folders")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
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
.fagent .fa-dirs{margin:8px 0}
.fagent .fa-dirs summary{overflow-wrap:anywhere}
.fagent .fa-dirs fieldset{border:0;padding:0;margin:6px 0;min-width:0}
.fagent .fa-dirs label.fa-dir{display:flex;gap:6px;align-items:baseline;padding:2px 0;overflow-wrap:anywhere;min-width:0}
.fagent .fa-dirs label.fa-dir input{flex:none}
.fagent .fa-dirs label.fa-dir>span{min-width:0}
.fagent .fa-dirs label.fa-more{display:block;margin:6px 0}
.fagent .fa-dirs textarea{width:100%;box-sizing:border-box;display:block;margin-top:4px}
.fagent .fa-dirs p{margin:6px 0}
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
                    f"<td>{n('rejected')}</td><td>{n('failed')}</td><td>{n('notes')}</td></tr>")
    if not rows:
        return ""
    return ("<table class='fa-counts'><thead><tr><th>question</th><th>sessions</th><th>kept</th>"
            "<th>rejected</th><th>failed</th><th>notes written</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


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


def _folders_path(state_dir):
    return os.path.join(state_dir, "folders.json")


def _path_ok(p):
    return isinstance(p, str) and 0 < len(p) <= FOLDER_LEN and os.path.isabs(p) and not CTRL_RE.search(p)


def read_folders(state_dir):
    """The saved folders (folders.json), each an absolute path; [] when none are saved."""
    try:
        data = json.loads(_read(_folders_path(state_dir)) or "null")
    except ValueError:
        return []
    got = data.get("folders") if isinstance(data, dict) else None
    out = []
    for p in got if isinstance(got, list) else []:
        if _path_ok(p) and os.path.normpath(p) not in out:
            out.append(os.path.normpath(p))
    return out[:FOLDERS_MAX]


def save_folders(state_dir, folders):
    _write_atomic(_folders_path(state_dir), json.dumps(
        {"version": 1, "folders": list(folders), "saved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        indent=1) + "\n")


def last_repos(info):
    """The repositories the last run explored (agent.json "repos")."""
    got = info.get("repos")
    out = []
    for p in got if isinstance(got, list) else []:
        if _path_ok(p) and os.path.normpath(p) not in out:
            out.append(os.path.normpath(p))
    return out[:FOLDERS_MAX]


def default_folders(state_dir, project):
    """What a start explores with no folders saved, as before there were any: the
    project harness serve runs in, else (no DIR, not in a repository) the repos of
    the last run."""
    return [os.path.normpath(project)] if project else last_repos(read_status(state_dir))


def _git_top(git, path):
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    try:
        r = subprocess.run([git, "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL, **kw)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def check_folders(form):
    """The folders a form chose: the ticked ones (dir) and those typed one per line
    (more), de-duplicated. Returns (folders, "") or (None, why not)."""
    raw = [v for v in form.get("dir") or [] if isinstance(v, str)]
    for v in form.get("more") or []:
        if isinstance(v, str):
            raw += v.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for v in raw:
        p = v.strip()
        if not p:
            continue
        if len(p) > FOLDER_LEN:
            return None, f"a folder's path is {len(p)} characters long; at most {FOLDER_LEN}: {p[:120]}..."
        if CTRL_RE.search(p):
            return None, f"a folder's path has a control character in it: {p.encode('unicode_escape').decode()[:200]}"
        if not os.path.isabs(p):
            return None, f"not an absolute path: {p} (give the whole path, e.g. /home/me/src/project)"
        p = os.path.normpath(p)
        if p not in out:
            out.append(p)
        if len(out) > FOLDERS_MAX:
            return None, f"more than {FOLDERS_MAX} folders; choose at most {FOLDERS_MAX}"
    if not out:
        return None, "no folder chosen: tick one, or type one per line under Other folders"
    git = shutil.which("git")
    if not git:
        return None, "git is not on PATH, so the folders cannot be checked"
    for p in out:
        if not os.path.exists(p):
            return None, f"no such folder: {p}"
        if not os.path.isdir(p):
            return None, f"not a folder: {p}"
        if not _git_top(git, p):
            return None, f"not in a git repository: {p} (the agent explores a repository's committed code)"
    return out, ""


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def resume_folders(state_dir):
    """The folders resume starts the agent on: [] (none saved: as before, no DIR) or
    the saved ones; but the repos of the last run when that run started after they
    were saved (wanted is newer) and has written agent.json since (as `harness
    findings start DIR` in a terminal does). Folders that are gone are left out."""
    saved = read_folders(state_dir)
    if not saved:
        return []
    s = _mtime(_folders_path(state_dir))
    w = _mtime(os.path.join(state_dir, "wanted"))
    a = _mtime(os.path.join(state_dir, "agent.json"))
    last = [p for p in last_repos(read_status(state_dir)) if os.path.isdir(p)]
    pick = last if last and None not in (s, w, a) and s < w <= a else saved
    return [p for p in pick if os.path.isdir(p)]


def _known(state_dir, info, project, known):
    """The folders offered: (path, notes about it, ticked), the ticked ones first."""
    saved = read_folders(state_dir)
    last = last_repos(info)
    project = os.path.normpath(project) if project else ""
    chosen = saved or ([project] if project else last)
    tags = {}
    for p in last:
        tags.setdefault(p, []).append("explored last")
    if project:
        tags.setdefault(project, []).append("the project harness serve runs in")
    for p in known or ():
        if _path_ok(p) and os.path.normpath(p) != project:
            tags.setdefault(os.path.normpath(p), []).append("a ticket's project")
    out, seen = [], set()
    for p in chosen + last + ([project] if project else []) + [os.path.normpath(k) for k in known or ()
                                                                if _path_ok(k)]:
        if p in seen or (p not in chosen and (len(out) >= KNOWN_MAX or not os.path.isdir(p))):
            continue
        seen.add(p)
        note = list(tags.get(p, []))
        if p in chosen and not os.path.isdir(p):
            note.insert(0, "missing")
        out.append((p, note, p in chosen))
    return out, chosen, bool(saved)


def _folders_form(state_dir, info, live, project, known, act):
    """The folders it explores: checkboxes for the known ones, a box for others. Its
    action is the same running or not (the live poll keeps it while it is filled in);
    Start sends it to /start."""
    opts, chosen, saved = _known(state_dir, info, project, known)
    boxes = "".join(f"<label class='fa-dir'><input type='checkbox' name='dir' value='{esc(p)}'"
                    + (" checked" if on else "") + f"><span><code>{esc(p)}</code>"
                    + (f" <span class='muted'>{esc(', '.join(note))}</span>" if note else "") + "</span></label>"
                    for p, note, on in opts)
    which = (", ".join(chosen) if chosen else "none chosen")
    whence = "" if saved else (" (the default: the project harness serve runs in)" if project and chosen else
                               " (the default: the ones it explored last)" if chosen else "")
    now = last_repos(info)
    if live:
        hint = ("It is exploring " + (", ".join(now) if now else "the folders it was started on")
                + ". A change applies at the next Start (Stop, then Start), or when harness serve starts it "
                "again after a restart.")
        btns = "<button type='submit' class='btn sm'>Save for the next start</button>"
    else:
        hint = ("Start explores the ticked folders; Save keeps the choice without starting. When harness serve "
                "starts again it starts the agent on them, if you had not stopped it.")
        btns = "<button type='submit' class='btn sm'>Save</button>"
    if saved:
        btns += ("<button type='submit' class='btn sm' name='act' value='reset'>Forget the choice</button>"
                 "<span class='muted'>back to the default: "
                 + ("the project harness serve runs in" if project else "the ones it explored last") + "</span>")
    return ("<form method='post' action='/runs/findings/agent/folders' class='fa-folders'>"
            "<input type='hidden' name='pick' value='1'>"
            "<details class='fa-dirs' data-k='findings-agent-folders'>"
            f"<summary>Folders it explores: <span class='muted'>{esc(which)}{esc(whence)}</span></summary>"
            "<p class='muted'>Each must be a folder in a git repository; the agent explores that whole "
            f"repository's committed code. At most {FOLDERS_MAX}.</p>"
            f"<fieldset><legend class='muted'>Folders it knows</legend>{boxes or '<span class=muted>none yet</span>'}"
            "</fieldset>"
            "<label class='fa-more'>Other folders, one per line"
            f"<textarea name='more' rows='2' spellcheck='false' aria-label='other folders, one per line' "
            "placeholder='/home/me/src/other-project'></textarea></label>"
            f"<p class='muted'>{esc(hint)}</p>"
            f"<div class='acts'>{btns}</div></details>{act}</form>")


def agent_card(state_dir, flash="", project="", known=()):
    """The card's HTML. Everything from agent.json, folders.json and notes.md is
    escaped. project: the git project harness serve runs in; known: other project
    folders to offer (the queue's Add form's)."""
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
             "<span class='muted'>opencode sessions on a free model that keep exploring the committed code, an "
             "area at a time and round again, tracing it across files for bugs, UI problems and code to remove or "
             "simplify, and write down what each area does for the next agent; read-only</span></div>"]
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
    for key, name in (("open", "open findings"), ("todo", "changed areas first"), ("units", "passes in the rotation")):
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
    n = len(knowledge_index(state_dir)) if state_dir else 0
    parts.append(f"<p><a href='/runs/findings/notes'>What it has learned</a> <span class='muted'>"
                 f"{n} area note{'' if n == 1 else 's'}</span></p>")
    # FINDINGS_MODEL lives in .env; the Settings page edits it. The agent reads it when it starts.
    model_link = ("<a class='muted' href='/settings#model'>Change the model in Settings"
                  + (" (then Stop and Start)" if live else "") + "</a>")
    if live:
        parts.append("<div class='acts'><form method='post' action='/runs/findings/agent/stop' "
                     "data-confirm='Stop the findings agent? Its session in flight is ended and that pass runs "
                     "again later. It stays stopped, also after a restart, until you start it.'>"
                     f"<button type='submit' class='btn sm'>Stop</button></form>{model_link}</div>")
        parts.append(_folders_form(state_dir, info, True, project, known, ""))
    else:   # Start sends the folders' form, so what is ticked is what it explores
        parts.append(_folders_form(state_dir, info, False, project, known,
                                   "<div class='acts'><button type='submit' class='btn sm primary' "
                                   f"formaction='/runs/findings/agent/start'>Start</button>{model_link}</div>"))
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


def knowledge_index(state_dir):
    """The notes the passes wrote, from knowledge/index.json, each checked: [{repo, area,
    slug, name, updated, session, lens, chars, summary, changed}], by repository and area."""
    try:
        data = json.loads(_read(os.path.join(state_dir, "knowledge", "index.json"), INDEX_MAX) or "{}")
    except ValueError:
        return []
    items = data.get("notes") if isinstance(data, dict) else None
    out = []
    for e in items if isinstance(items, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("file"), str):
            continue
        slug, _, name = e["file"].partition("/")
        if not (SLUG_RE.match(slug) and name.endswith(".md") and SLUG_RE.match(name)):
            continue
        n = e.get("chars")
        out.append({"repo": _s(e.get("repo"), 1000), "area": _s(e.get("area"), 500) or ".", "slug": slug,
                    "name": name[:-3], "updated": _s(e.get("updated"), 40), "session": _s(e.get("session"), 80),
                    "lens": _s(e.get("lens"), 20), "summary": _s(e.get("summary"), 300),
                    "chars": n if isinstance(n, int) and not isinstance(n, bool) else 0,
                    "changed": e.get("changed") is True})
    out.sort(key=lambda e: (e["repo"], e["area"]))
    return out


def knowledge_text(state_dir, slug, name):
    """One area's note (knowledge/<slug>/<name>.md), or None: both names checked, the
    path kept under knowledge/."""
    if not (state_dir and SLUG_RE.match(slug or "") and SLUG_RE.match(name or "")):
        return None
    base = os.path.realpath(os.path.join(state_dir, "knowledge"))
    path = os.path.realpath(os.path.join(base, slug, name + ".md"))
    if os.path.dirname(os.path.dirname(path)) != base or not os.path.isfile(path):
        return None
    return _read(path, NOTE_MAX)


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"   # the server answers in threads
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _is_running(state_dir):
    held = _held(state_dir)
    return bool(held or (held is None and _running(read_status(state_dir), time.time())))


def _start(state_dir, harness_path, project, dirs=()):
    """`harness findings start DIR...` (no DIR: on the git project of cwd, which is
    project when given, else the repos of the last run)."""
    if not os.path.isfile(harness_path or ""):
        return False, f"no harness script at {harness_path} (pub.py --harness)"
    bash = shutil.which("bash")
    if not bash:
        return False, "bash is not on PATH, so harness cannot run"
    if _is_running(state_dir):
        return False, "the findings agent is already running"
    dirs = list(dirs)
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
                      f"harness findings start{''.join(' ' + d for d in dirs)} (in {cwd})\n"
                      .encode("utf-8", "replace"))
            out.flush()
            proc = subprocess.Popen([bash, harness_path, "findings", "start", *dirs], stdin=subprocess.DEVNULL,
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
    return True, "the findings agent started" + (f" on {', '.join(dirs)}" if dirs else "")


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


def _choose(state_dir, form, project, starting):
    """The folders form: checked, and saved when it changes the choice (only an
    explicit Save pins the default). Returns (ok, message)."""
    if (form.get("act") or [""])[0] == "reset" and not starting:
        try:
            os.remove(_folders_path(state_dir))
        except FileNotFoundError:
            pass
        except OSError as e:
            return False, f"cannot forget the folders: {e.strerror or e}"
        got = default_folders(state_dir, project)
        return True, "folders forgotten: the agent explores " + (", ".join(got) if got else "the default") + \
            " from its next start"
    picked, why = check_folders(form)
    if picked is None:
        return False, why
    saved = read_folders(state_dir)
    if not starting or set(picked) != set(saved or default_folders(state_dir, project)):
        try:
            os.makedirs(state_dir, exist_ok=True)
            save_folders(state_dir, picked)
        except OSError as e:
            return False, f"cannot save the folders: {e.strerror or e}"
    return True, "folders saved: the agent explores " + ", ".join(picked) + " from its next start" + \
        (" (Stop, then Start)" if _is_running(state_dir) else "")


def agent_action(state_dir, harness_path, action, form, project=""):
    """Do one of start, stop, notes, folders. Returns (ok, message). form maps names
    to lists of values (parse_qs); project is the folder harness serve runs in, when
    known."""
    if not state_dir:
        return False, "this viewer has no state folder (pub.py --queue-dir)"
    if action == "folders":
        return _choose(state_dir, form, project, False)
    if action == "start":
        if form.get("pick"):   # the card's form: what is ticked is what it explores
            if _is_running(state_dir):
                return False, "the findings agent is already running"
            ok, msg = _choose(state_dir, form, project, True)
            if not ok:
                return False, msg
        dirs = read_folders(state_dir)
        return _start(state_dir, harness_path, project, dirs)
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
    it is not running, the machine having gone down under it, on resume_folders
    (none saved: no DIR, as before). FINDINGS_AUTORESUME=0: never. Returns (ok,
    message), or None when there is nothing to do."""
    if os.environ.get("FINDINGS_AUTORESUME", "1").strip() == "0" or not state_dir:
        return None
    if not os.path.exists(os.path.join(state_dir, "wanted")) or _is_running(state_dir):
        return None
    return _start(state_dir, harness_path, project, resume_folders(state_dir))
