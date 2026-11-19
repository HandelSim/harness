"""serve/api.py: harness serve's JSON API under /api/v1, for the desktop app.

pub.py's handler calls handle() for every /api/ path (after its Host, Origin and
session checks) and deny() for an /api/ request with no session when access codes
are on. This module never imports pub (pub runs as __main__): pub hands its
helpers in as one namespace (API_HELPERS).

Every read is a plain function in READS (shared by the GET endpoints and /poll,
so they cannot drift); every action a function in the POST route table. Reads
answer {"ok": true, "sig", "data"} (or "same" when ?sig= matches); actions
{"ok": true, "msg"} or 409. architecture/harness-cli.md ("JSON API") is the
contract in prose. Python 3.9, stdlib only.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import types
from urllib.parse import parse_qs, unquote, urlsplit

import runs as runs_view
import settings_view
import vault_view
import diff_view
import findings_view
import findings_agent_view
import jira_view

API_VERSION = 1
PREFIX = "/api/v1"
MAX_TOPICS = 24
TOPIC_RE = re.compile(r"^[a-z-]+(:[A-Za-z0-9._-]+)?$")
OUT_CHUNK = 256 * 1024          # bytes of a stage output one call returns
LOG_CAP = 256 * 1024            # bytes of a ticket's log.md in Ticket (newest kept)
PART_CAP = 4000                 # characters of a tool part's output in /messages
MSG_LIMIT = 200                 # messages one /messages call returns by default
MSG_LIMIT_MAX = 1000
SESSIONS_MAX = 2000
TEXT_MAX = 1024 * 1024          # bytes of a vault file /vault/file returns as text
JSON_TYPE = "application/json; charset=utf-8"
MOVES = runs_view.MOVES

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_REFRESHING = set()   # cache keys a thread is getting the next value of


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        Exception.__init__(self, message)
        self.status, self.code, self.message = status, code, message


def _cached(key, ttl, fn):
    """fn()'s value, kept ttl seconds (proxy health, git version: /status at 1 Hz). Only
    the first call waits for fn; after that an expired value is served while one thread
    gets the next, so a slow proxy probe (2 s) never holds a poll (review A-6)."""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        if hit:
            if key not in _REFRESHING:
                _REFRESHING.add(key)
                threading.Thread(target=_refresh, args=(key, fn), daemon=True).start()
            return hit[1]
    val = fn()
    _store(key, val)
    return val


def _store(key, val):
    with _CACHE_LOCK:
        if len(_CACHE) > 200:
            _CACHE.clear()
        _CACHE[key] = (time.monotonic(), val)


def _refresh(key, fn):
    try:
        _store(key, fn())
    except Exception:   # the old value stays; the next expired read tries again
        traceback.print_exc(file=sys.stderr)
    finally:
        with _CACHE_LOCK:
            _REFRESHING.discard(key)


def sig_of(data):
    """The same construction as runs._sig: sha1 of the sorted JSON."""
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


# --- the wire ---------------------------------------------------------------------------

def _send(h, cfg, status, obj, head=False):
    body = json.dumps(obj, default=str).encode()
    extra = {"Cache-Control": "no-store", "X-Harness-Api": str(API_VERSION)}
    if cfg is not None and cfg.code_changed():
        extra["X-Harness-Stale"] = "1"
    h.send(status, body, JSON_TYPE, extra, head)


def _err(h, cfg, status, code, message, head=False):
    _send(h, cfg, status, {"ok": False, "error": {"code": code, "message": message}}, head)


def deny(h, head=False):
    """An /api/ request with no session while access codes are on."""
    _err(h, None, 401, "auth", "not signed in: POST /login with an access code", head)


def deny_origin(h):
    """An /api/ POST whose Origin (or Referer) is not this server's."""
    _err(h, None, 403, "origin", "cross-origin request refused")


# What pub.py hands in (API_HELPERS): proxy_status, read_ledger, vault_log, state_id,
# list_pages, add_code, remove_code, page_cap, max_form, max_send, max_ticket.
Helpers = types.SimpleNamespace


class Ctx:
    """One request: the handler, serve's Config, pub's helpers, who is signed in."""

    def __init__(self, h, cfg, helpers, user, query):
        self.h, self.cfg, self.helpers, self.user = h, cfg, helpers, user
        self.q = parse_qs(query or "", keep_blank_values=True)
        self._agents = None

    def arg(self, name, default=""):
        v = self.q.get(name)
        return v[0] if v else default

    def int_arg(self, name, default, lo=0, hi=None):
        v = self.arg(name, "")
        if v == "":
            return default
        if not re.match(r"^-?[0-9]{1,15}$", v):
            raise ApiError(400, "bad-request", f"{name} is not a whole number")
        n = int(v)
        if n < lo or (hi is not None and n > hi):
            raise ApiError(400, "bad-request", f"{name} is out of range")
        return n

    @property
    def runs(self):
        if self.cfg.runs is None:
            raise ApiError(503, "unavailable", "this serve has no run viewer (no agent data)")
        return self.cfg.runs

    @property
    def queue(self):
        r = self.runs
        if not r.queue_dir:
            raise ApiError(503, "unavailable", "this serve has no queue")
        return r

    def agents(self):
        if self._agents is None:
            try:
                self._agents = runs_view.agent_sessions(self.runs)
            except Exception:   # a broken debug or findings folder: no labels, not a failed read
                self._agents = {}
        return self._agents

    def host(self):
        return ((self.h.headers.get("Host") or "127.0.0.1").rsplit(":", 1)[0]) or "127.0.0.1"

    def settings_args(self):
        return self.h.settings_args()


# --- /status ----------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_version(root):
    git = shutil.which("git")
    out = {"branch": "", "sha": "", "describe": ""}
    if not git:
        return out
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    for k in ("GIT_DIR", "GIT_WORK_TREE"):
        env.pop(k, None)

    def run(*a):
        try:
            r = subprocess.run([git, "-C", root] + list(a), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=5, env=env, **runs_view.NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    out["branch"] = run("rev-parse", "--abbrev-ref", "HEAD")
    out["sha"] = run("rev-parse", "HEAD")
    out["describe"] = run("describe", "--tags", "--always")
    return out


def _origin(url):
    """scheme://host[:port] of url, any login in it dropped, lowercased, a default port
    left out: the origin pub.opencode_session_link encodes (opencode keys its servers by it)."""
    u = urlsplit(url)
    netloc = u.netloc.rpartition("@")[2].lower()
    default = {"http": ":80", "https": ":443"}.get(u.scheme, "")
    if default and netloc.endswith(default):
        netloc = netloc[:-len(default)]
    return f"{u.scheme}://{netloc}"


def _opencode(ctx):
    cfg = ctx.cfg
    if cfg.opencode_url:
        u = urlsplit(cfg.opencode_url)
        return {"port": u.port or (443 if u.scheme == "https" else 80), "url": _origin(cfg.opencode_url)}
    if cfg.opencode_port:
        return {"port": cfg.opencode_port, "url": f"http://{ctx.host()}:{cfg.opencode_port}"}
    return None


def read_status(ctx, arg=None):
    cfg, hp = ctx.cfg, ctx.helpers
    proxy = None
    if cfg.proxy_url:
        up, detail = _cached(("proxy", cfg.proxy_url), 5, lambda: hp.proxy_status(cfg.proxy_url))
        proxy = {"url": cfg.proxy_url, "up": bool(up), "detail": detail}
    vault, name = cfg.active.get()
    return {
        "api": API_VERSION, "mode": "serve" if cfg.opencode_port or cfg.opencode_url else "viewer",
        "pid": os.getpid(), "state_id": hp.state_id(cfg.state_dir), "stale": bool(cfg.code_changed()),
        "root": ROOT, "state_dir": cfg.state_dir, "codes": bool(cfg.codes), "user": ctx.user,
        "runs": cfg.runs is not None, "opencode": _opencode(ctx), "proxy": proxy,
        "vault": {"name": name, "dir": vault} if (vault or name) else None,
        "modes": [{"name": n, "description": d} for n, d in cfg.modes],
        "version": _cached(("git", ROOT), 60, lambda: _git_version(ROOT)),
    }


def read_opencode(ctx, arg=None):
    oc = _opencode(ctx)
    if oc is None:
        raise ApiError(404, "not-found", "no opencode web on this serve")
    return dict(oc, user="opencode", password=ctx.cfg.opencode_password())


# --- the queue --------------------------------------------------------------------------

ACTION_NAMES = runs_view.TICKET_ACTIONS


def _gate(ok, reason=""):
    return {"ok": bool(ok), "reason": "" if ok else (reason or "not now")}


def gates(runs, key, st, archived):
    """{action: {ok, reason}}: the buttons the ticket's pages show, from the predicates
    and constants those pages use (runs.py); the queue script still has the last word."""
    status = st.get("status") or ""
    shown = status or "unreadable"
    stopping = bool(st.get("stopping"))
    if not status:
        why = "its state.json is missing or unreadable"
        return {a: _gate(a == "remove", why) for a in ACTION_NAMES}
    if archived:
        why = "archived: return it to the queue first"
        return {a: _gate(a in ("unarchive", "remove", "note"), why) for a in ACTION_NAMES}
    vault = runs.vault_of(st)
    g = {}
    if status not in runs_view.WAITING:
        g["answer"] = _gate(False, f"{key} is {shown}, not waiting on answers")
    elif not vault:
        g["answer"] = _gate(False, "no vault is set, so there is nowhere to keep the answers")
    else:
        g["answer"] = _gate(True)
    g["approve"] = _gate(status == "plan-review", f"{key} is {shown}, not a plan waiting for approval")
    g["retry"] = _gate(status in runs_view.RETRYABLE,
                       f"{key} is {shown}; only a blocked, stopped, done, waiting or interrupted ticket is retried")
    if status not in runs_view.CONTINUABLE:
        g["continue"] = _gate(False, f"{key} is {shown}; there is no stage to go on")
    else:
        ok = runs_view._continuable(runs, key, st)
        why = ""
        if not ok:
            why = runs.continue_refused(key) or (
                "it is stopping" if stopping else
                f"its stage ({st.get('stage', '')}) is not one an agent goes on with"
                if st.get("stage") not in runs_view.AGENT_STAGES else
                "queued to start over, not to go on" if status == "queued" else
                "stopped from the plan review" if st.get("stopped_from") == "plan-review" else "not now")
        g["continue"] = _gate(ok, why)
    g["stop"] = _gate(status in runs_view.STOPPABLE and not stopping,
                      "it is stopping" if stopping else f"{key} is {shown}; there is nothing to stop")
    g["remove"] = _gate(status != "running", f"{key} is running; stop it first")
    g["move"] = _gate(True)
    g["note"] = _gate(True)
    if status != "done":
        g["followup"] = _gate(False, f"{key} is {shown}, not done; a follow-up is for a done ticket")
    elif "plan" not in (st.get("stages") or runs_view.DEFAULT_STAGES):
        g["followup"] = _gate(False, "it has no plan stage to take a follow-up")
    elif not vault:
        g["followup"] = _gate(False, "no vault is set, so there is nowhere to keep the follow-up")
    else:
        why = runs.followup_refused(key)
        g["followup"] = _gate(not why, why or "")
    can, why = runs_view._edit_lock(st)
    g["edit"] = _gate(can != "none", why)
    if status not in runs_view.FIXABLE:
        g["fix"] = _gate(False, f"{key} is {shown}; review comments go to a done, blocked or stopped ticket")
    else:
        why = runs.fix_refused(key)
        g["fix"] = _gate(why == "", why if why else "the queue cannot say whether it takes review comments")
    g["archive"] = _gate(status != "running", f"{key} is running; stop it first")
    g["unarchive"] = _gate(False, f"{key} is in the queue, not archived")
    return g


def _ticket_st(runs, key):
    """ticket_state without the values that are an age (stop_wait: seconds since)."""
    st = dict(runs.ticket_state(key))
    st.pop("stop_wait", None)
    if st.get("stopping"):
        st["stuck"] = bool(runs_view._stuck(runs.ticket_state(key)))
    return st


def _summary(runs, key, archived):
    st = _ticket_st(runs, key)
    ses = st.get("session") or {}
    return {
        "key": key, "title": st.get("title", ""), "status": st.get("status", ""), "stage": st.get("stage", ""),
        "step": st.get("step", ""), "reason": st.get("reason", ""), "why": runs_view._why(st),
        "created": st.get("created", ""), "updated": st.get("updated", ""), "finished": st.get("finished", False),
        "attempt": st.get("attempt", 0), "project": st.get("project", ""), "branch": st.get("branch", ""),
        "stages": st.get("stages", []), "in_place": st.get("in_place", False),
        "plan_review": st.get("plan_review", False), "stopping": bool(st.get("stopping")),
        "stuck": bool(st.get("stuck")), "debug": bool(st.get("debug")),
        "session": {"stage": ses["stage"], "id": ses["id"]} if ses.get("id") else None,
        "actions": gates(runs, key, st, archived),
    }


def _runner(runs):
    """Runs.runner() with every key the app reads present: the strings "" and started,
    since None when run.json or this viewer has none; watch false when unknown."""
    r = dict(runs.runner())
    for k in ("ticket", "stage", "step", "model"):
        r.setdefault(k, "")
    r.setdefault("started", None)
    r.setdefault("since", None)
    r["watch"] = r.get("watch") is True
    # the run re-beats run.json every 3 s: a live beat would change the queue's sig on
    # every poll; "running" already carries what the beat means
    r["beat"] = None
    return r


def _start_gate(r, runs):
    if r.get("running"):
        return _gate(False, "the queue is running")
    if r.get("starting"):
        return _gate(False, "the queue is starting")
    return _gate(True)


def read_queue(ctx, arg=None):
    runs = ctx.queue
    r = _runner(runs)
    return {"runner": r, "tickets": [_summary(runs, k, False) for k in runs.ticket_keys()],
            "archived": [_summary(runs, k, True) for k in runs.archived_keys()],
            "start": _start_gate(r, runs), "modes": ["container", "host", "single"]}


def _check_key(runs, key):
    if not runs_view.KEY_RE.match(key or "") or key not in runs._ticket_dirs():
        raise ApiError(404, "not-found", "no such ticket")


def _log_tail(runs, key):
    path = os.path.join(runs.queue_dir, key, "log.md")

    def read():
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - LOG_CAP))
                data = f.read()
        except OSError:
            return ""
        if size > LOG_CAP:
            data = data.partition(b"\n")[2]
        return data.decode("utf-8", "replace")
    return runs.memo("api-log", path, (path,), read)


def _ticket(ctx, key):
    runs = ctx.queue
    _check_key(runs, key)
    archived = key in runs.archived_keys()
    st = _ticket_st(runs, key)
    why = runs.retry_choices(key, later=True)
    status = st.get("status", "")
    choices = []
    if status in runs_view.RETRYABLE:
        why = why or dict.fromkeys((None,) + runs_view.STAGES, "")
        mine = st.get("stages") or runs_view.DEFAULT_STAGES
        if not why.get(None):
            choices.append({"value": "", "label": "where it was" if status != "done" else "the start"})
        choices += [{"value": s, "label": s} for s in runs_view.STAGES
                    if not why.get(s) and (s != "test" or s in mine)]
    live = runs.live()
    sessions = []
    try:
        agents = runs_view.ticket_agents(runs, key)
    except Exception:
        agents = []
    now = time.time()
    for a in agents:
        row = a["row"]
        label = a["stage"] + (f"-{a['n']}" if a.get("n") else "") + (f" {a['who']}" if a.get("who") else "")
        sessions.append({"sid": a["id"], "stage": a["stage"], "label": label.strip(),
                         "kind": "debug" if a["stage"] == "debug" else "queue",
                         "working": a["id"] in live or now - runs_view._ms(row.get("time_updated")) / 1000
                         <= runs_view.ACTIVE, "created": runs_view._ms(row.get("time_created"))})
    return {"key": key, "archived": archived, "state": st, "why": runs_view._why(st),
            "actions": gates(runs, key, st, archived), "retry_choices": choices,
            "retry_why": (why or {}).get(None) or "", "edit_lock": list(runs_view._edit_lock(st)),
            "continue_mode": runs_view._continue_mode(st), "sessions": sessions,
            "outs": [{"name": n, "mtime": m, "size": s} for n, m, s in runs.stage_outs(key)],
            "log": _log_tail(runs, key)}


def read_ticket(ctx, key):
    return _ticket(ctx, key)


def read_ticket_topic(ctx, key):
    try:
        return _ticket(ctx, key)
    except ApiError as e:
        if e.status == 404:
            return {"gone": True}
        raise


def read_out(ctx, key, name):
    runs = ctx.queue
    _check_key(runs, key)
    if not runs_view.OUT_RE.match(name or ""):
        raise ApiError(404, "not-found", "no such stage output")
    path = os.path.join(runs.queue_dir, key, name)
    start = ctx.int_arg("from", 0)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            reset = start > size
            if reset:
                start = 0
            f.seek(start)
            data = f.read(OUT_CHUNK)
    except OSError:
        raise ApiError(404, "not-found", "no such stage output")
    if len(data) == OUT_CHUNK or start + len(data) < size:
        # cut on a UTF-8 boundary: drop a partial character at the end (it comes next call)
        cut = len(data)
        i = cut - 1
        back = 0
        while i >= 0 and back < 4 and (data[i] & 0xC0) == 0x80:
            i -= 1
            back += 1
        if i >= 0 and data[i] >= 0xC0:
            need = 2 if data[i] < 0xE0 else 3 if data[i] < 0xF0 else 4
            if cut - i < need:
                cut = i
        data = data[:cut]
    return {"name": name, "from": start, "next": start + len(data), "size": size,
            "text": data.decode("utf-8", "replace"), "reset": reset}


def read_runner_log(ctx, arg=None):
    return {"text": ctx.queue.runner_out(lines=400)}


def read_branches(ctx, arg=None):
    return runs_view.project_branches(ctx.arg("project"))


def read_repos(ctx, arg=None):
    return runs_view.repo_tree(ctx.queue, ctx.arg("project"))


def _flat(form):
    return {k: (v[0] if isinstance(v, list) and len(v) == 1 else v) for k, v in form.items()}


def read_ticket_edit(ctx, key):
    runs = ctx.queue
    _check_key(runs, key)
    st = runs.ticket_state(key)
    if not st:
        raise ApiError(404, "not-found", "its state.json is missing or unreadable")
    can, why = runs_view._edit_lock(st)
    return {"form": _flat(runs_view._edit_values(key, st)), "can": can, "why": why}


def read_diff(ctx, key):
    runs = ctx.queue
    _check_key(runs, key)
    st = diff_view._state(runs, key)
    if st is None:
        raise ApiError(404, "not-found", "no such ticket")
    st.setdefault("key", key)
    choices = diff_view.repos(runs, st)
    repo = ctx.arg("repo", ".") or "."
    if repo not in dict(choices):
        raise ApiError(404, "not-found", f"not a repository of this ticket: {repo[:200]}")
    can = [v for v in diff_view.VIEWS if v != "work" or diff_view.has_work_view(st, repo)]
    view = ctx.arg("view")
    if view not in can:
        view = "work" if "work" in can and st.get("base_sha") else "uncommitted"
    res = diff_view.changes(runs, st, repo, view)
    return {"repos": [{"rel": v, "label": label} for v, label in choices], "views": can, "repo": repo,
            "view": view, "diff": res.get("diff", ""),
            "commits": [{"sha": c[0], "subject": c[1]} if isinstance(c, (list, tuple)) and len(c) == 2 else c
                        for c in res.get("commits") or []],
            "notes": res.get("notes"), "says": res.get("says"), "rev": res.get("rev", ""),
            "kind": res.get("kind", ""), "error": res.get("error", "")}


def read_lines(ctx, key):
    """diff_view.lines: (status, {"lines"...} or {"error"}) for Show more lines."""
    runs = ctx.queue
    _check_key(runs, key)
    code, got = diff_view.lines(runs, key, ctx.q)
    if code != 200:
        raise ApiError(404 if code == 404 else 400, "not-found" if code == 404 else "bad-request",
                       str((got or {}).get("error") or "refused"))
    return got


# --- sessions ---------------------------------------------------------------------------

def _web_link(ctx):
    """f(source label, sid) -> the session in opencode web, with no login in it (the
    app answers opencode's Basic auth itself), or None."""
    oc = _opencode(ctx)
    if oc is None:
        return lambda label, sid: None
    origin = oc["url"]
    server = base64.urlsafe_b64encode(origin.encode()).decode().rstrip("=")
    return lambda label, sid: f"{origin}/server/{server}/session/{sid}" if label == "host" else None


def _session_row(ctx, r, live, now, web):
    runs = ctx.runs
    src = r.get("source")
    label = getattr(src, "label", str(src or ""))
    sid = r["id"]
    ticket = ""
    try:
        ticket = runs.ticket_of_row(r)
    except Exception:
        ticket = ""
    agent = ctx.agents().get(sid) or {}
    kind = "queue" if ticket else agent.get("kind") if agent.get("kind") in ("debug", "findings") else "session"
    updated = runs_view._ms(r.get("time_updated"))
    return {"id": sid, "parent": r.get("parent_id"), "source": label, "directory": r.get("directory") or "",
            "title": r.get("title") or "", "agent": r.get("agent") or "", "model": runs_view._model_name(r.get("model")),
            "created": runs_view._ms(r.get("time_created")), "updated": updated,
            "tokens_in": r.get("tokens_input") or 0, "tokens_out": r.get("tokens_output") or 0,
            "cost": r.get("cost") or 0, "kids": r.get("kids") or 0,
            "working": sid in live or now - updated / 1000 <= runs_view.ACTIVE, "live": sid in live,
            "ticket": ticket or None, "kind": kind, "label": agent.get("label") or "", "web": web(label, sid)}


def read_sessions(ctx, arg=None):
    runs = ctx.runs
    limit = ctx.int_arg("limit", runs_view.LIST_CAP, 1, SESSIONS_MAX)
    q = ctx.arg("q").strip().lower()[:200]
    rows = runs.sessions(roots_only=ctx.arg("all") != "1", limit=limit)
    live, now, web = runs.live(), time.time(), _web_link(ctx)
    out = [_session_row(ctx, r, live, now, web) for r in rows]
    if q:
        out = [r for r in out if q in r["title"].lower() or q in r["directory"].lower() or q in r["id"].lower()
               or q in (r["ticket"] or "").lower()]
    return out


def _find(ctx, sid):
    runs = ctx.runs
    src, row = runs.find(sid)
    if not row:
        raise ApiError(404, "not-found", "no such session")
    return runs, src, row


def _head(ctx, sid):
    runs, src, row = _find(ctx, sid)
    live, now = runs.live(), time.time()
    row = dict(row, source=src, kids=len(runs.children(src, sid)))
    r = _session_row(ctx, row, live, now, _web_link(ctx))
    n = runs._query(src, "select count(*) as n from message where session_id = ?", (sid,))
    count = n[0]["n"] if n else 0
    kids = [{"id": c["id"], "title": c.get("title") or "", "agent": c.get("agent") or "",
             "created": runs_view._ms(c.get("time_created")), "updated": runs_view._ms(c.get("time_updated"))}
            for c in runs.children(src, sid)]
    inbox = [{"at": at, "sender": s, "state": state, "text": text} for at, s, state, text in runs.inbox(src, sid)]
    root = runs.root_of(src, row) or row
    stoppable = bool(runs.ticket_of_row(root)) if root else False
    return {"row": r, "count": count, "children": kids, "inbox": inbox,
            "send": _gate(r["live"], "this run is not live (finished, or not a headless run)"),
            "stop": _gate(stoppable, "this is not a queue stage; stop a harness -p run from its terminal, "
                                     "and an interactive one from opencode")}


def read_session(ctx, sid):
    return _head(ctx, sid)


def read_session_topic(ctx, sid):
    try:
        return _head(ctx, sid)
    except ApiError as e:
        if e.status == 404:
            return {"gone": True}
        raise


def _cut_part(p):
    st = p.get("state")
    if p.get("type") == "tool" and isinstance(st, dict):
        for k in ("output", "error"):
            v = st.get(k)
            if isinstance(v, str) and len(v) > PART_CAP:
                st = dict(st)
                st[k] = v[:PART_CAP]
                p = dict(p, state=st, truncated=True)
    return p


def read_messages(ctx, sid):
    runs, src, row = _find(ctx, sid)
    start = ctx.int_arg("from", 0)
    limit = ctx.int_arg("limit", MSG_LIMIT, 1, MSG_LIMIT_MAX)
    count, msgs = runs.messages_page(src, sid, start, limit)   # the page alone is read (A-3)
    live = runs.live()
    start = min(start, count)
    page = [{"id": mid, "index": start + i, "created": t, "role": (info or {}).get("role", ""), "info": info,
             "parts": [_cut_part(p) for p in plist]}
            for i, (mid, info, t, plist) in enumerate(msgs)]
    working = sid in live or time.time() - runs_view._ms(row.get("time_updated")) / 1000 <= runs_view.ACTIVE
    return {"from": start, "count": count, "messages": page, "working": working, "live": sid in live}


def read_part(ctx, sid, pid):
    text = runs_view.part_text(ctx.runs, sid, pid)
    if text is None:
        raise ApiError(404, "not-found", "no such part")
    return {"text": text}


def read_export(ctx, sid):
    got = runs_view.export(ctx.runs, sid)
    if got is None:
        raise ApiError(404, "not-found", "no such session")
    return got


def read_follow(ctx, arg=None):
    since = ctx.int_arg("since", 0)
    queue = "findings" if ctx.arg("findings") == "1" else ctx.arg("queue") == "1"
    return {"sid": runs_view.follow_target(ctx.runs, since, queue)}


# --- home -------------------------------------------------------------------------------

def read_home(ctx, arg=None):
    cfg, hp = ctx.cfg, ctx.helpers
    live_rows = []
    if cfg.runs is not None:
        runs = cfg.runs
        live, now, web = runs.live(), time.time(), _web_link(ctx)
        live_rows = [_session_row(ctx, r, live, now, web) for r in runs.sessions(limit=100) if r["id"] in live]
    usage = cfg.cache.get(("usage", cfg.usage_ledger), lambda: hp.read_ledger(cfg.usage_ledger)) \
        if cfg.usage_ledger else None
    pages = cfg.cache.get(("pages", cfg.pub_dir), lambda: hp.list_pages(cfg.pub_dir)) if cfg.vault else []
    rows = cfg.cache.get(("git", cfg.vault), lambda: hp.vault_log(cfg.vault)) if cfg.vault else None
    return {"live": live_rows, "usage": usage,
            "pages": [{"rel": rel, "title": title, "mtime": mtime, "url": "/pub/" + rel}
                      for rel, title, mtime in (pages or [])[:hp.page_cap]],
            "activity": [{"sha": a, "author": b, "when": c, "subject": d} for a, b, c, d in (rows or [])]}


# --- findings ---------------------------------------------------------------------------

def read_findings(ctx, arg=None):
    runs = ctx.queue
    items = findings_view.collect(runs)
    board = findings_view.read_board(findings_view.board_path(runs))
    got = findings_view.arrange(items, board)
    open_, arch = got[0], got[1]
    out = [dict(f, archived=False) for f in open_] + [dict(f, archived=True) for f in arch]
    return {"items": out, "order": list(board.get("order") or [])}


def _fa_dir(runs):
    d = findings_view.state_dir(runs)
    if not d:
        raise ApiError(503, "unavailable", "no state folder for the findings agent")
    return os.path.join(d, "findings")


def read_findings_agent(ctx, arg=None):
    d = _fa_dir(ctx.queue)
    info = findings_agent_view.read_status(d)
    notes = ""
    try:
        with open(os.path.join(d, "notes.md"), encoding="utf-8", errors="replace") as f:
            notes = f.read(findings_agent_view.NOTES_MAX * 4)[:findings_agent_view.NOTES_MAX]
    except OSError:
        pass
    status = {k: v for k, v in info.items() if k not in ("heartbeat_ts",)} if isinstance(info, dict) else {}
    return {"running": bool(findings_agent_view._is_running(d)), "status": status,
            "heartbeat": info.get("heartbeat_ts") if isinstance(info, dict) else None,
            "passes": findings_agent_view.passes(d, 20), "folders": findings_agent_view.read_folders(d),
            "last_repos": findings_agent_view.last_repos(info), "notes": notes,
            "knowledge": findings_agent_view.knowledge_index(d)}


def read_findings_note(ctx, slug, name):
    text = findings_agent_view.knowledge_text(_fa_dir(ctx.queue), slug, name)
    if text is None:
        raise ApiError(404, "not-found", "no such note")
    return {"text": text}


# --- vault ------------------------------------------------------------------------------

def read_vaults(ctx, arg=None):
    vault, name = ctx.cfg.active.get()
    return {"active": name, "dir": vault, "vaults": vault_view.list_vaults(ctx.cfg.state_dir)}


def _vault(ctx):
    vault, name = ctx.cfg.active.get()
    if not vault or not os.path.isdir(vault):
        raise ApiError(503, "unavailable", "no vault is active here")
    return vault, name


def read_ingest(ctx, arg=None):
    vault, _ = _vault(ctx)
    run = vault_view.ingest_run(vault)
    if isinstance(run, dict):
        run = {k: v for k, v in run.items() if k != "age"}
    log = vault_view.ingest_log(ctx.cfg.state_dir)
    tail = ""
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            tail = f.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        tail = ""
    return {"run": run, "summary": vault_view.ingest_summary(vault), "out_tail": tail}


def read_vault(ctx, arg=None):
    vault, name = _vault(ctx)
    found = vault_view.pages(vault)   # {rel: mtime}
    pages = [{"rel": rel, "title": os.path.splitext(os.path.basename(rel))[0], "mtime": found[rel]}
             for rel in sorted(found) if rel.lower().endswith(".md")]
    return {"name": name, "dir": vault, "facts": vault_view.repo_facts(vault), "pages": pages,
            "ingest": read_ingest(ctx), "log": [{"sha": a, "author": b, "when": c, "subject": d} for a, b, c, d in
                                                (ctx.cfg.cache.get(("git", vault),
                                                                   lambda: ctx.helpers.vault_log(vault)) or [])]}


def _vault_path(vault, rel):
    """rel inside the vault (no .git, no escape), or None: the HTML side's own check
    (vault_view.resolve, which also reads a backslash as a separator, as Windows does)."""
    got = vault_view.resolve(vault, rel or "")
    return got[0] if got else None


def read_vault_tree(ctx, arg=None):
    vault, _ = _vault(ctx)
    rel = ctx.arg("path").strip("/")
    path = _vault_path(vault, rel)
    if path is None or not os.path.isdir(path):
        raise ApiError(404, "not-found", "no such folder in the vault")
    out = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        names = []
    for n in names[:2000]:
        if n == ".git":   # only git's own folder: .gitignore, .github are the vault's files
            continue
        p = os.path.join(path, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": n, "path": (rel + "/" + n).strip("/"), "dir": os.path.isdir(p),
                    "size": 0 if os.path.isdir(p) else st.st_size})
    return {"entries": out}


def read_vault_file(ctx, arg=None):
    vault, _ = _vault(ctx)
    rel = ctx.arg("path").strip("/")
    path = _vault_path(vault, rel)
    if path is None or not os.path.isfile(path):
        raise ApiError(404, "not-found", "no such file in the vault")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        return {"path": rel, "kind": "image", "text": "", "url": "/vault/file/" + rel}
    try:
        with open(path, "rb") as f:
            data = f.read(TEXT_MAX + 1)
    except OSError:
        raise ApiError(404, "not-found", "cannot read that file")
    return {"path": rel, "kind": "markdown" if ext in (".md", ".markdown") else "text",
            "text": data[:TEXT_MAX].decode("utf-8", "replace"), "truncated": len(data) > TEXT_MAX}


# --- settings ---------------------------------------------------------------------------

def _config_rows(rows):
    out = []
    for r in rows or []:
        r = dict(r)
        if r.get("secret") and r.get("value"):
            v = str(r["value"])
            # read_config may already give "(set, N chars)"; never a raw secret either way
            keep = re.match(r"^\(set, \d+ chars?\)$", v) or "://***@" in v   # already masked by read_config
            r["value"] = v if keep else f"(set, {len(v)} chars)"
        r.setdefault("group", "")
        out.append(r)
    return out


def read_settings(ctx, arg=None):
    h = ctx.settings_args()
    g = settings_view.gather(h)

    def part(name):
        got = g.get(name)
        return got if isinstance(got, tuple) and len(got) == 2 else (got, "")
    config, cerr = part("config")
    net, nerr = part("net")
    mcp, merr = part("mcp")
    cfg = ctx.cfg
    names = []
    if cfg.codes:
        try:
            names = sorted(e[0] if isinstance(e, (list, tuple)) else str(e) for e in cfg.codes.entries())
        except Exception:
            names = []
    info = settings_view.system_info(h)
    info = dict(info) if isinstance(info, dict) else {}
    info["state"] = cfg.state_dir
    return {"config": _config_rows(config), "config_error": cerr if config is None else "",
            "allowlist": net or [], "allowlist_error": nerr if net is None else "",
            "mcp": mcp or [], "mcp_error": merr if mcp is None else "",
            "access": {"enabled": bool(cfg.codes), "names": names, "me": ctx.user}, "system": info}


def read_update(ctx, arg=None):
    v = _cached(("git", ROOT), 60, lambda: _git_version(ROOT))
    remote, checked = None, None
    path = os.path.join(ctx.cfg.state_dir or os.path.join(ROOT, "state"), ".harness-update-check")
    try:
        with open(path, encoding="utf-8") as f:
            bits = f.read().split()
        checked = int(os.path.getmtime(path))
        if len(bits) >= 2 and bits[0] == v["branch"]:
            remote = bits[1]
    except OSError:
        pass
    behind = None if remote is None or not v["sha"] else remote != v["sha"]
    return {"branch": v["branch"], "local": v["sha"], "remote": remote, "behind": behind, "checked_at": checked}


# --- jira, debug (P2) -------------------------------------------------------------------

def read_jira(ctx, arg=None):
    runs = ctx.queue
    which = ctx.arg("list") if ctx.arg("list") in jira_view.LISTS else "sprint"
    mine = ctx.arg("mine") == "1"
    url, projs, why = jira_view._problem(runs)
    if why:
        return {"rows": [], "error": why, "note": "", "more": False, "projects": []}
    q = jira_view.jql(projs, which, mine)
    e = jira_view.lookup((url, tuple(projs), which, mine), url, jira_view.token(), q)
    rows = e.get("rows")
    if rows is None:
        return {"rows": [], "error": e.get("err") or "Jira gave nothing.", "note": "", "more": False,
                "projects": list(projs)}
    queued = {}
    for k in runs.ticket_keys() + runs.archived_keys():
        queued[k] = runs.ticket_state(k).get("status") or None
    out = [dict(r, queued=queued.get(r.get("key"))) for r in rows]
    return {"rows": out, "error": "", "note": f"Showing an earlier list. {e['err']}" if e.get("err") else "",
            "more": len(rows) >= jira_view.MAX_ROWS, "projects": list(projs)}


def read_debug(ctx, arg=None):
    return runs_view.debug_reports(ctx.queue)


def read_debug_report(ctx, kind, name):
    if kind not in ("runs", "reports"):
        raise ApiError(404, "not-found", "no such report")
    text = runs_view._debug_text(ctx.queue, kind, name)
    if text is None:
        raise ApiError(404, "not-found", "no such report")
    return {"text": text}


def unavailable(ctx, *a):
    raise ApiError(501, "unavailable", "not built on this harness: use the web page")


# --- registries -------------------------------------------------------------------------

READS = {   # topic -> reader(ctx, arg); /poll and the GET endpoints both use these
    "status": read_status, "home": read_home, "queue": read_queue, "sessions": read_sessions,
    "findings": read_findings, "findings-agent": read_findings_agent, "vault": read_vault,
    "ingest": read_ingest, "debug": read_debug, "ticket": read_ticket_topic, "session": read_session_topic,
}
ARG_TOPICS = ("ticket", "session")

SEG = r"([^/]+)"
GETS = [(re.compile("^" + p + "$"), fn) for p, fn in (
    (r"/status", read_status), (r"/opencode", read_opencode), (r"/home", read_home),
    (r"/queue", read_queue), (r"/queue/runner-log", read_runner_log),
    (r"/queue/branches", read_branches), (r"/queue/repos", read_repos),
    (r"/tickets/" + SEG, read_ticket), (r"/tickets/" + SEG + r"/out/" + SEG, read_out),
    (r"/tickets/" + SEG + r"/edit", read_ticket_edit), (r"/tickets/" + SEG + r"/diff", read_diff),
    (r"/tickets/" + SEG + r"/lines", read_lines),
    (r"/sessions", read_sessions), (r"/sessions/" + SEG, read_session),
    (r"/sessions/" + SEG + r"/messages", read_messages), (r"/sessions/" + SEG + r"/parts/" + SEG, read_part),
    (r"/sessions/" + SEG + r"/export", read_export), (r"/follow", read_follow),
    (r"/findings", read_findings), (r"/findings/agent", read_findings_agent),
    (r"/findings/notes/" + SEG + "/" + SEG, read_findings_note),
    (r"/vaults", read_vaults), (r"/vault", read_vault), (r"/vault/ingest", read_ingest),
    (r"/vault/tree", read_vault_tree), (r"/vault/file", read_vault_file),
    (r"/vault/search", unavailable), (r"/vault/commit/" + SEG, unavailable),
    (r"/settings", read_settings), (r"/update", read_update),
    (r"/jira", read_jira), (r"/debug", read_debug), (r"/debug/" + SEG + "/" + SEG, read_debug_report),
)]


# --- actions ----------------------------------------------------------------------------

def _form(body):
    """The JSON body as the {name: [value]} dict the form handlers take (INV 4 names)."""
    form = {}
    for k, v in body.items():
        if not isinstance(k, str):
            continue
        if k == "stages" and isinstance(v, list):
            for s in v:
                form["stage_" + str(s)] = ["1"]
            continue
        if k == "repo_modes" and isinstance(v, dict):
            for rel, mode in v.items():
                form["repo_mode:" + str(rel)] = [str(mode)]
            continue
        if k == "comments" and isinstance(v, list):
            form["comments"] = [json.dumps(v)]
            continue
        if v is None or v is False:
            continue
        if v is True:
            form[k] = ["1"]
        elif isinstance(v, list):
            vals = [x for x in v if x is not None and x is not False]
            form[k] = ["1" if x is True else x if isinstance(x, str) else json.dumps(x)
                       if isinstance(x, (dict, list)) else str(x) for x in vals]
        elif isinstance(v, (int, float)):
            form[k] = [str(v)]
        elif isinstance(v, str):
            form[k] = [v]
        else:
            raise ApiError(400, "bad-request", f"{k}: not a string, number, true, list or one of the typed fields")
    return form


def _answer_form(runs, key, body):
    """answer: {answers: {question id: text}, picks?: {id: option index or "other"},
    criteria?: {id: {reject?, comment?}}, assumptions?: {id: text}, feedback?, defaults?/go?, sig}
    onto the field names ticket_action reads: the per-item form (qt.ID, q.ID, ac.ID,
    acc.ID, as.ID, note) on a plan with criteria, else the plain one (q<i>, a<i>, note)."""
    st = runs.ticket_state(key)
    rest = {k: v for k, v in body.items() if k not in ("answers", "picks", "criteria", "assumptions", "feedback",
                                                        "go", "defaults")}
    form = _form(rest)
    answers = body.get("answers") or {}
    picks = body.get("picks") or {}
    crit = body.get("criteria") or {}
    assumed = body.get("assumptions") or {}
    for name, v in (("answers", answers), ("picks", picks), ("criteria", crit), ("assumptions", assumed)):
        if not isinstance(v, dict):
            raise ApiError(400, "bad-request", f"{name} must be an object keyed by item id")
    if body.get("defaults") or body.get("go"):
        form["defaults"] = ["1"]
    feedback = body.get("feedback")
    if isinstance(feedback, str) and feedback:
        form["note"] = [feedback]
    items = bool(st.get("criteria")) and st.get("status") in ("needs-answers", "plan-review")
    if items or picks or crit or assumed:
        for qid, text in answers.items():
            if isinstance(text, str) and text:
                form["qt." + str(qid)] = [text]
        for qid, pick in picks.items():
            if pick is not None and pick != "":
                form["q." + str(qid)] = [str(pick)]
        for cid, c in crit.items():
            c = c if isinstance(c, dict) else {}
            if c.get("reject"):
                form["ac." + str(cid)] = ["reject"]
            if isinstance(c.get("comment"), str) and c["comment"]:
                form["acc." + str(cid)] = [c["comment"]]
        for aid, text in assumed.items():
            if isinstance(text, str) and text:
                form["as." + str(aid)] = [text]
        return form
    qs = st.get("questions") or []
    extra = []
    by_id = {q["id"]: q for q in qs}
    for qid, text in answers.items():
        if not isinstance(text, str) or not text.strip():
            continue
        q = by_id.get(qid)
        i = qs.index(q) if q is not None else -1
        if 0 <= i < runs_view.FORM_QUESTIONS:
            form[f"q{i}"] = [q["text"]]
            form[f"a{i}"] = [text]
        else:
            extra.append(f"{qid}: {q['text'] if q else ''}\n{text}".strip())
    if extra:
        form["note"] = ["\n\n".join(extra + (form.get("note") or []))]
    return form


def _changed(ok, msg):
    if not ok and msg == runs_view.CHANGED:
        raise ApiError(409, "changed", msg)
    return ok, msg


def act_send(ctx, body, sid):
    text = body.get("text")
    if not isinstance(text, str):
        raise ApiError(400, "bad-request", "text must be a string")
    runs = ctx.runs
    src, row = runs.find(sid)
    if not row:
        raise ApiError(404, "not-found", "no such session")
    ok, msg = runs.send(sid, text.replace("\r\n", "\n"), ctx.user or "desktop")
    sys.stderr.write(f"[pub] api message to {sid} from {ctx.user or 'desktop'}: {msg}\n")
    return ok, msg


def act_session_stop(ctx, body, sid):
    _find(ctx, sid)
    return runs_view.session_stop(ctx.runs, sid)


def act_queue(ctx, body, action):
    runs = ctx.queue
    if action not in runs_view.QUEUE_ACTIONS:
        raise ApiError(400, "bad-request", "no such queue action")
    if action == "start" and body.get("confirm_host"):
        body = dict(body, confirm_host="1")
    return runs_view.queue_action(runs, action, _form(body))


def act_ticket(ctx, body, key, action):
    runs = ctx.queue
    if action not in runs_view.TICKET_ACTIONS:
        raise ApiError(400, "bad-request", "no such ticket action")
    _check_key(runs, key)
    form = _answer_form(runs, key, body) if action in ("answer", "approve") else _form(body)
    if action == "move" and "before" in body and body["before"] in (None, ""):
        form["before"] = [""]   # to the bottom
    return _changed(*runs_view.ticket_action(runs, key, action, form))


def act_findings(ctx, body, action):
    runs = ctx.queue
    if action not in findings_view.ACTIONS:
        raise ApiError(400, "bad-request", "no such findings action")
    return findings_view.action(runs, action, _form(body))


def act_findings_agent(ctx, body, action):
    runs = ctx.queue
    if action not in findings_agent_view.ACTIONS:
        raise ApiError(400, "bad-request", "no such findings agent action")
    form = _form(body)
    if isinstance(body.get("more"), list):
        form["more"] = ["\n".join(str(x) for x in body["more"])]
    return findings_agent_view.agent_action(_fa_dir(runs), runs.harness, action, form,
                                            project=runs_view._serve_project(runs))


def act_vault(ctx, body, what):
    cfg = ctx.cfg
    form = _form(body)
    if what == "use":
        ok, msg = vault_view.use_vault(cfg.state_dir, (form.get("name") or [""])[0].strip())
        if ok:
            cfg.active._at = 0.0
        return ok, msg
    vault, _ = _vault(ctx)
    if what == "ingest/start":
        return vault_view.start_ingest(ctx.settings_args(), cfg.state_dir, vault, form)
    if what == "ingest/stop":
        return vault_view.stop_ingest(vault)
    if what == "ingest/forget":
        return vault_view.forget_codebase(ctx.settings_args(), vault, form)
    raise ApiError(404, "not-found", "no such vault action")


def act_settings(ctx, body, what):
    h = ctx.settings_args()
    cfg = ctx.cfg
    data = None
    if what == "config":
        form = {}
        sets, clears = body.get("set") or {}, body.get("clear") or []
        if not isinstance(sets, dict) or not isinstance(clears, list):
            raise ApiError(400, "bad-request", "set is an object of KEY: value, clear a list of KEYs")
        for k, v in sets.items():
            form["v_" + str(k)] = [str(v)]
        for k in clears:
            form["clear_" + str(k)] = ["1"]
        ok, msg = settings_view.save_config(h, form)
    elif what == "net":
        ok, msg = settings_view.net_change(h, _form(body))
    elif what == "mcp":
        ok, msg = settings_view.mcp_change(h, _form(body))
    elif what == "system":
        ok, msg, result = settings_view.system_action(h, str(body.get("act") or ""))
        data = {"output": result}
    elif what == "access":
        if not cfg.codes:
            raise ApiError(409, "refused", "access codes are not enabled on this serve")
        act, name = str(body.get("act") or ""), str(body.get("name") or "").strip()
        try:
            if act == "add":
                code = ctx.helpers.add_code(cfg.codes.path, name)
                ok, msg, data = True, f"Made a code for {name}.", {"name": name, "code": code}
            elif act == "remove" and name != ctx.user:
                ctx.helpers.remove_code(cfg.codes.path, name)
                ok, msg = True, f"Removed {name}'s code; they are signed out."
            elif act == "remove":
                ok, msg = False, "you cannot remove the code you are signed in with"
            else:
                raise ApiError(400, "bad-request", "act is add or remove")
        except (ValueError, OSError) as e:
            ok, msg = False, str(e)
    else:
        raise ApiError(404, "not-found", "no such settings action")
    settings_view.forget()
    sys.stderr.write(f"[pub] api settings {what} from {ctx.user or 'desktop'}: {msg}\n")
    return ok, msg, data


POSTS = [(re.compile("^" + p + "$"), fn, limit) for p, fn, limit in (
    (r"/sessions/" + SEG + r"/send", act_send, "send"), (r"/sessions/" + SEG + r"/stop", act_session_stop, "form"),
    (r"/queue/(add|start|halt|end)", act_queue, "ticket"),
    (r"/tickets/" + SEG + r"/([a-z]+)", act_ticket, "ticket"),
    (r"/findings/(archive|restore|move)", act_findings, "findings"),
    (r"/findings/agent/(start|stop|notes|folders)", act_findings_agent, "findings-agent"),
    (r"/vault/(use|ingest/start|ingest/stop|ingest/forget)", act_vault, "form"),
    (r"/settings/(config|net|mcp|system|access)", act_settings, "settings"),
)]


def _limit(helpers, which):
    return {"send": helpers.max_send, "ticket": helpers.max_ticket, "findings": findings_view.FORM_MAX,
            "findings-agent": findings_agent_view.FORM_MAX}.get(which, helpers.max_form * 4)


# --- dispatch ---------------------------------------------------------------------------

def _match(table, path):
    for row in table:
        m = row[0].match(path)
        if m:
            return row, [unquote(g) for g in m.groups()]
    return None, None


def _read_one(ctx, fn, args):
    data = fn(ctx, *args) if args else fn(ctx)
    return data, sig_of(data)


def _poll(ctx):
    ts = ctx.q.get("t") or []
    if len(ts) > MAX_TOPICS:
        raise ApiError(400, "bad-request", f"at most {MAX_TOPICS} topics")
    want = []
    for t in ts:
        topic, _, sig = t.partition("~")
        if not TOPIC_RE.match(topic):
            raise ApiError(400, "bad-request", f"bad topic: {topic[:80]}")
        want.append((topic, sig))
    out = {}
    for topic, sig in want:
        name, _, arg = topic.partition(":")
        fn = READS.get(name)
        try:
            if fn is None or (name in ARG_TOPICS) != bool(arg):
                raise ApiError(400, "bad-request", f"unknown topic: {topic}")
            data, s = _read_one(ctx, fn, [arg] if arg else [])
            out[topic] = {"sig": s, "same": True} if sig and sig == s else {"sig": s, "data": data}
        except ApiError as e:
            out[topic] = {"error": {"code": e.code, "message": e.message}}
        except Exception as e:   # one failing topic never fails the others
            traceback.print_exc(file=sys.stderr)
            out[topic] = {"error": {"code": "internal", "message": f"{type(e).__name__} (details in serve's log)"}}
    return {"ok": True, "now": int(time.time()), "stale": bool(ctx.cfg.code_changed()), "topics": out}


def _body(h, limit):
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise ApiError(415, "content-type", "send Content-Type: application/json")
    n = h.headers.get("Content-Length")
    if n is None or not re.match(r"^[0-9]{1,12}$", n.strip()):
        raise ApiError(400, "bad-request", "Content-Length is required")
    n = int(n)
    if n > limit:
        raise ApiError(413, "too-large", f"the body is over {limit} bytes")
    raw = h.rfile.read(n) if n else b""
    try:
        body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ApiError(400, "bad-request", "the body is not JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "bad-request", "the body must be one JSON object")
    return body


def handle(h, cfg, helpers, method, path, query, user, head=False):
    """Answer one /api/ request; pub.py has checked Host, Origin (POST) and the session."""
    try:
        if not (path == PREFIX or path.startswith(PREFIX + "/")):
            raise ApiError(404, "not-found", "unknown API version or path")
        sub = path[len(PREFIX):] or "/"
        ctx = Ctx(h, cfg, helpers, user, query)
        if method in ("GET", "HEAD"):
            if sub == "/poll":
                _send(h, cfg, 200, _poll(ctx), head)
                return
            row, args = _match(GETS, sub)
            if row is None:
                if _match(POSTS, sub)[0] is not None:
                    raise ApiError(405, "method", "use POST")
                raise ApiError(404, "not-found", "no such endpoint")
            data, s = _read_one(ctx, row[1], args)
            want = ctx.arg("sig")
            _send(h, cfg, 200, {"ok": True, "sig": s, "same": True} if want and want == s
                  else {"ok": True, "sig": s, "data": data}, head)
            return
        if method != "POST":
            raise ApiError(405, "method", "GET, HEAD or POST")
        row, args = _match(POSTS, sub)
        if row is None:
            if sub == "/poll" or _match(GETS, sub)[0] is not None:
                raise ApiError(405, "method", "use GET")
            raise ApiError(404, "not-found", "no such endpoint")
        body = _body(h, _limit(helpers, row[2]))
        got = row[1](ctx, body, *args)
        ok, msg = got[0], got[1]
        data = got[2] if len(got) > 2 else None
        if not ok:
            raise ApiError(409, "refused", str(msg))
        out = {"ok": True, "msg": str(msg)}
        if data is not None:
            out["data"] = data
        _send(h, cfg, 200, out)
    except ApiError as e:
        _err(h, cfg, e.status, e.code, e.message, head)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        _err(h, cfg, 500, "internal", f"{type(e).__name__} (details in serve's log)", head)   # no paths out
