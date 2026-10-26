"""The pub page's run viewer (/runs): every opencode session harness agents ran, read
from opencode's own databases, so it covers container agents, host mode, opencode
web and `harness queue` stages alike, finished or still working.

  /runs                       live runs, queue tickets by stage, then everything else
  /runs/<session>             the transcript: messages, thinking, every tool call with
                              its input and output, subagent sessions nested; follows a
                              working session as it goes, and takes a message for a
                              headless run that is still live
  /runs/<session>.json        the session, its messages and parts, as stored
  /runs/<session>/tail?from=N messages N.. as HTML, for the page's polling
  /runs/<session>/part/<id>   one tool call's full output, as text
  /runs/ticket/<KEY>          a queue ticket: its log.md, stage outputs and sessions
  POST /runs/<session>/send   a message for a live run (agents/live-run.sh's inbox)

Each source is a data directory holding opencode/opencode.db and harness/live/ (the
markers and inboxes agents/live-run.sh keeps). The databases are opened read-only.
"""

import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import quote

SID_RE = re.compile(r"^ses_[A-Za-z0-9]{1,64}$")
PART_RE = re.compile(r"^prt_[A-Za-z0-9]{1,64}$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
OUT_RE = re.compile(r"^[a-z]+-[0-9]+\.out$")
LIVE_FRESH = 10          # seconds since the relay last touched the marker
ACTIVE = 90              # an interactive session updated this recently is shown as active
LIST_CAP = 300
TEXT_CAP = 20000         # characters of one tool input/output shown inline
MAX_MESSAGE = 65536      # bytes of one message sent to a live run

esc = html.escape

STYLE = """
.crumbs { display: flex; gap: 6px; flex-wrap: wrap; align-items: baseline; font-size: 14px; }
.crumbs span { color: var(--muted); }
.runs li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 16px; padding: 9px 0;
  border-top: 1px solid var(--line); }
.runs li:first-child { border-top: 0; }
.runs a { font-weight: 550; overflow-wrap: anywhere; }
.runs .meta { grid-column: 1 / -1; font: 12px var(--mono); color: var(--muted); overflow-wrap: anywhere; }
.runs .when { font: 12px var(--mono); color: var(--muted); white-space: nowrap; }
.live, .active, .done { display: inline-flex; align-items: center; gap: 6px; padding: 1px 9px; border-radius: 999px;
  font-size: 12px; font-weight: 650; white-space: nowrap; }
.live { color: var(--ok); background: var(--ok-soft); }
.live::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.active { color: var(--accent); background: var(--accent-soft); }
.done { color: var(--muted); background: var(--sunk); }
.ticket { border-top: 1px solid var(--line); padding-top: 10px; }
.ticket:first-of-type { border-top: 0; padding-top: 0; }
.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 4px 14px; font-size: 13px; margin: 0; }
.facts dt { color: var(--muted); } .facts dd { margin: 0; font-family: var(--mono); overflow-wrap: anywhere; }
.msg { border-left: 3px solid var(--line); padding: 2px 0 2px 14px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.msg.user { border-left-color: var(--accent); }
.msg .who { font-size: 12px; color: var(--muted); font-family: var(--mono); }
.msg .who b { color: var(--ink); }
.txt { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; }
.msg.user .txt { background: var(--accent-soft); border-radius: 8px; padding: 8px 12px; }
details.tool, details.think { background: var(--sunk); border-radius: 8px; padding: 6px 10px; min-width: 0; }
details.think { background: transparent; border: 1px dashed var(--line); }
details summary { cursor: pointer; font: 13px var(--mono); overflow-wrap: anywhere; }
details summary .st { color: var(--muted); }
details summary .bad { color: var(--bad); }
pre.io { white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.45 var(--mono); margin: 8px 0 0;
  max-height: 480px; overflow: auto; background: var(--bg); border-radius: 6px; padding: 8px; }
.lbl { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-top: 8px; }
textarea { font: inherit; color: inherit; background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 8px 12px; width: 100%; min-height: 90px; resize: vertical; }
.send { display: flex; flex-direction: column; gap: 8px; margin: 0; }
.send button { align-self: flex-start; background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
.inbox li { font: 12px var(--mono); color: var(--muted); padding: 3px 0; overflow-wrap: anywhere; }
.inbox .bad { color: var(--bad); }
pre.qlog { white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.5 var(--mono); margin: 0; max-height: 640px; overflow: auto; }
"""


def _ms(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _when(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)) if ms else ""


def _ago(ms, now=None):
    s = max(0, int((now or time.time()) - ms / 1000))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _dur(ms):
    s = ms / 1000
    return f"{s:.1f}s" if s < 90 else f"{int(s // 60)}m{int(s % 60):02d}s"


def _loads(text):
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _norm(path):
    return os.path.normcase(os.path.normpath(path.replace("\\", "/"))) if path else ""


def _cap(text, limit=TEXT_CAP):
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _model_name(raw):
    m = _loads(raw) if isinstance(raw, str) else (raw or {})
    mid = m.get("modelID") or m.get("id") or ""
    prov = m.get("providerID") or ""
    return f"{prov}/{mid}" if prov and mid else mid


class Source:
    def __init__(self, label, data_dir):
        self.label = label
        self.db_path = os.path.join(data_dir, "opencode", "opencode.db")
        self.live_dir = os.path.join(data_dir, "harness", "live")

    def connect(self):
        if not os.path.isfile(self.db_path):
            return None
        uri = "file:" + quote(os.path.abspath(self.db_path).replace("\\", "/")) + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=3)
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.Error:
            return None


class Runs:
    def __init__(self, sources, queue_dir=""):
        """sources: (label, data dir) pairs; queue_dir: harness queue's state/queue."""
        self.sources = [Source(label, d) for label, d in sources]
        self.queue_dir = queue_dir

    # --- reading -------------------------------------------------------------

    def _query(self, src, sql, args=()):
        con = src.connect()
        if con is None:
            return []
        try:
            return con.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def live(self, now=None):
        """{session id: (source, marker age seconds)} of runs whose relay is alive."""
        now = now or time.time()
        out = {}
        for src in self.sources:
            try:
                names = os.listdir(src.live_dir)
            except OSError:
                continue
            for name in names:
                sid = name[:-5]
                if not name.endswith(".json") or not SID_RE.match(sid):
                    continue
                try:
                    age = now - os.path.getmtime(os.path.join(src.live_dir, name))
                except OSError:
                    continue
                if age <= LIVE_FRESH:
                    out[sid] = (src, age)
        return out

    def sessions(self, roots_only=True, limit=LIST_CAP):
        rows = []
        where = "where s.parent_id is null and s.time_archived is null" if roots_only else "where s.time_archived is null"
        for src in self.sources:
            for r in self._query(src, "select s.id, s.parent_id, s.directory, s.title, s.agent, s.model, "
                                      "s.time_created, s.time_updated, s.tokens_input, s.tokens_output, s.cost, "
                                      "(select count(*) from session c where c.parent_id = s.id) as kids "
                                      f"from session s {where} order by s.time_updated desc limit ?", (limit,)):
                d = dict(r)
                d["source"] = src
                rows.append(d)
        rows.sort(key=lambda d: _ms(d["time_updated"]), reverse=True)
        return rows[:limit]

    def find(self, sid):
        """(source, session row as dict) or (None, None)."""
        if not SID_RE.match(sid or ""):
            return None, None
        for src in self.sources:
            rows = self._query(src, "select * from session where id = ?", (sid,))
            if rows:
                return src, dict(rows[0])
        return None, None

    def children(self, src, sid):
        return [dict(r) for r in self._query(
            src, "select id, title, agent, time_created, time_updated from session where parent_id = ? "
                 "order by time_created", (sid,))]

    def messages(self, src, sid):
        """[(message id, info dict, time_created, [part dicts])] in order."""
        msgs = [(r["id"], _loads(r["data"]), _ms(r["time_created"]), [])
                for r in self._query(src, "select id, data, time_created from message where session_id = ? "
                                          "order by time_created, id", (sid,))]
        index = {m[0]: m[3] for m in msgs}
        for r in self._query(src, "select id, message_id, data from part where session_id = ? order by id", (sid,)):
            if r["message_id"] in index:
                p = _loads(r["data"])
                p["id"] = r["id"]
                index[r["message_id"]].append(p)
        return msgs

    def part(self, src, sid, pid):
        rows = self._query(src, "select data from part where session_id = ? and id = ?", (sid, pid))
        return _loads(rows[0]["data"]) if rows else None

    def root_of(self, src, row):
        """The top session of row's subagent chain."""
        seen = set()
        while row and row.get("parent_id") and row["id"] not in seen:
            seen.add(row["id"])
            rows = self._query(src, "select * from session where id = ?", (row["parent_id"],))
            if not rows:
                break
            row = dict(rows[0])
        return row

    def chain(self, src, row):
        """The parent sessions above row, top first."""
        out, seen = [], set()
        while row and row.get("parent_id") and row["id"] not in seen:
            seen.add(row["id"])
            rows = self._query(src, "select id, parent_id, title from session where id = ?", (row["parent_id"],))
            if not rows:
                break
            row = dict(rows[0])
            out.append(row)
        return out[::-1]

    # --- the queue -----------------------------------------------------------

    def ticket_of(self, directory):
        """The queue ticket KEY whose clone (state/queue/KEY/repo) directory is in, or ""."""
        if not self.queue_dir or not directory:
            return ""
        q, d = _norm(self.queue_dir), _norm(directory)
        prefix = q.rstrip("/\\") + os.sep
        if not d.startswith(os.path.normcase(prefix)):
            return ""
        rest = d[len(prefix):].replace("\\", "/").split("/")
        if len(rest) >= 2 and rest[1] == "repo":
            # the KEY as the queue spelled it, not normcase'd
            for name in self._ticket_dirs():
                if os.path.normcase(name) == rest[0]:
                    return name
        return ""

    def _ticket_dirs(self):
        try:
            return [n for n in os.listdir(self.queue_dir) if KEY_RE.match(n)]
        except OSError:
            return []

    def stage_outs(self, key):
        """[(name, mtime, size)] of the ticket's stage outputs, oldest first."""
        d = os.path.join(self.queue_dir, key)
        out = []
        try:
            for n in os.listdir(d):
                if OUT_RE.match(n):
                    st = os.stat(os.path.join(d, n))
                    out.append((n, st.st_mtime, st.st_size))
        except OSError:
            return []
        return sorted(out, key=lambda t: t[1])

    def stage_of(self, key, created_ms, outs=None):
        """The stage (e.g. plan-1) a ticket session belongs to: the first stage output
        written at or after the session started. A running stage has no output yet
        finished, so the newest session past every output is the stage in progress."""
        for name, mtime, _ in (self.stage_outs(key) if outs is None else outs):
            if mtime * 1000 >= created_ms:
                return name[:-4]
        return ""

    # --- sending -------------------------------------------------------------

    def send(self, sid, text, sender="local"):
        """Drop a message in the live run's inbox. Returns (ok, message)."""
        if not text.strip():
            return False, "the message is empty"
        if len(text.encode()) > MAX_MESSAGE:
            return False, f"the message is over {MAX_MESSAGE} bytes"
        live = self.live().get(sid)
        if not live:
            return False, "this run is not live (finished, or not a headless run)"
        src = live[0]
        body = {"parts": [{"type": "text", "text": text}]}
        # The session's own agent and model: without them opencode answers with its
        # default agent ("build"), not the run's (e.g. yolo).
        msgs = self._query(src, "select data from message where session_id = ? order by time_created desc, id desc",
                           (sid,))
        for r in msgs:
            info = _loads(r["data"])
            if info.get("role") == "user":
                if info.get("agent"):
                    body["agent"] = info["agent"]
                m = info.get("model") or {}
                if m.get("providerID") and m.get("modelID"):
                    body["model"] = {"providerID": m["providerID"], "modelID": m["modelID"]}
                break
        inbox = os.path.join(src.live_dir, sid + ".inbox")
        name = f"{int(time.time() * 1000)}-{sender}-{secrets.token_hex(3)}"
        tmp = os.path.join(inbox, name + ".tmp")
        try:
            os.makedirs(inbox, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(body, f)
            os.replace(tmp, os.path.join(inbox, name + ".json"))
        except OSError as e:
            return False, f"cannot write the run's inbox: {e.strerror or e}"
        return True, "queued"

    def inbox(self, src, sid):
        """[(sent ms, sender, state, text)] newest first; state: waiting, sent, failed-N, late."""
        d = os.path.join(src.live_dir, sid + ".inbox")
        out = []
        try:
            names = os.listdir(d)
        except OSError:
            return []
        for n in names:
            base, _, state = n.rpartition(".")
            if state == "tmp" or not base:
                continue
            ts, _, rest = base.partition("-")
            sender = rest.rsplit("-", 1)[0]
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    body = json.load(f)
                text = "".join(p.get("text", "") for p in body.get("parts", []) if isinstance(p, dict))
            except (OSError, ValueError, AttributeError):
                text = ""
            out.append((_ms(ts), sender, "waiting" if state == "json" else state, text))
        return sorted(out, reverse=True)


# --- rendering ---------------------------------------------------------------

def _status(runs, row, live, now=None):
    now = now or time.time()
    if row["id"] in live:
        return "<span class='live'>live</span>"
    if now - _ms(row["time_updated"]) / 1000 <= ACTIVE:
        return "<span class='active'>active</span>"
    return "<span class='done'>idle</span>"


def _row(runs, row, live, extra="", now=None):
    sid = row["id"]
    title = row.get("title") or sid
    kids = row.get("kids") or 0
    meta = [esc(row["source"].label)]
    if row.get("agent"):
        meta.append("agent " + esc(row["agent"]))
    model = _model_name(row.get("model"))
    if model:
        meta.append(esc(model))
    if kids:
        meta.append(f"{kids} subagent session{'s' if kids != 1 else ''}")
    tok = _ms(row.get("tokens_input")) + _ms(row.get("tokens_output"))
    if tok:
        meta.append(f"{tok:,} tokens")
    meta.append(esc(row.get("directory") or ""))
    return (f"<li><span>{extra}<a href='/runs/{quote(sid)}'>{esc(title)}</a> {_status(runs, row, live, now)}</span>"
            f"<span class='when'>{_ago(_ms(row['time_updated']), now)}</span>"
            f"<span class='meta'>{' &middot; '.join(meta)}</span></li>")


def render_list(runs, query=""):
    now = time.time()
    live = runs.live(now)
    rows = runs.sessions()
    q = query.lower()
    if q:
        rows = [r for r in rows if q in (r.get("title") or "").lower() or q in (r.get("directory") or "").lower()
                or q in r["id"].lower()]
    parts = ["<header class='top'><div class='crumbs'><a href='/'>harness</a><span>/</span><b>runs</b></div>"
             f"<span class='chip'>{len(live)} live</span></header>"]
    parts.append("<section class='panel'><form method='get' action='/runs' class='row'>"
                 f"<input type='search' name='q' value='{esc(query)}' placeholder='filter by title, folder or id'>"
                 "<button type='submit'>Filter</button></form>"
                 "<div class='muted'>Every opencode session on this host: container agents, host mode, opencode "
                 "web and queue stages. <b>live</b> is a headless run you can message; <b>active</b> was "
                 "updated in the last 90 s. This page refreshes itself.</div></section>")
    live_rows = [r for r in rows if r["id"] in live]
    if live_rows:
        parts.append("<section class='panel'><h2>Live now</h2><ul class='runs'>")
        parts += [_row(runs, r, live, now=now) for r in live_rows]
        parts.append("</ul></section>")
    tickets, other = {}, []
    for r in rows:
        key = runs.ticket_of(r.get("directory") or "")
        if key:
            tickets.setdefault(key, []).append(r)
        else:
            other.append(r)
    if tickets:
        parts.append("<section class='panel'><h2>Queue tickets</h2>")
        for key in sorted(tickets, key=lambda k: -max(_ms(r["time_updated"]) for r in tickets[k])):
            outs = runs.stage_outs(key)
            parts.append(f"<div class='ticket'><a href='/runs/ticket/{quote(key)}'><b>{esc(key)}</b></a> "
                         "<span class='muted'>queue log and stage outputs</span><ul class='runs'>")
            for r in sorted(tickets[key], key=lambda r: _ms(r["time_created"])):
                stage = runs.stage_of(key, _ms(r["time_created"]), outs) or "running"
                parts.append(_row(runs, r, live, f"<code>{esc(stage)}</code> ", now))
            parts.append("</ul></div>")
        parts.append("</section>")
    parts.append("<section class='panel'><h2>Sessions</h2>")
    if other:
        parts.append("<ul class='runs'>")
        parts += [_row(runs, r, live, now=now) for r in other]
        parts.append("</ul>")
    else:
        parts.append("<div class='muted'>No sessions" + (" match." if q else " yet.") + "</div>")
    parts.append("</section>")
    return parts


def _tool(p, sid):
    st = p.get("state") or {}
    status = st.get("status") or "?"
    name = p.get("tool") or "tool"
    title = st.get("title") or ""
    t = st.get("time") or {}
    took = _dur(_ms(t.get("end")) - _ms(t.get("start"))) if t.get("end") and t.get("start") else ""
    cls = "bad" if status == "error" else "st"
    head = (f"<summary><b>{esc(name)}</b> {esc(title)} <span class='{cls}'>{esc(status)}"
            f"{' ' + esc(took) if took else ''}</span></summary>")
    body = []
    meta = st.get("metadata") or {}
    child = meta.get("sessionId") or meta.get("sessionID")
    if isinstance(child, str) and SID_RE.match(child):
        body.append(f"<div><a href='/runs/{quote(child)}'>open the subagent session &rarr;</a></div>")
    inp = st.get("input")
    if inp:
        text, cut = _cap(json.dumps(inp, indent=2, ensure_ascii=False))
        body.append(f"<div class='lbl'>input</div><pre class='io'>{esc(text)}{' &hellip;' if cut else ''}</pre>")
    out = st.get("output") if status != "error" else st.get("error")
    if out:
        out = out if isinstance(out, str) else json.dumps(out, indent=2, ensure_ascii=False)
        text, cut = _cap(out)
        body.append(f"<div class='lbl'>{'error' if status == 'error' else 'output'}</div>"
                    f"<pre class='io'>{esc(text)}</pre>")
        if cut:
            body.append(f"<div class='muted'>{len(out):,} characters; "
                        f"<a href='/runs/{quote(sid)}/part/{quote(p['id'])}' target='_blank' rel='noopener'>"
                        "the whole output</a></div>")
    return f"<details class='tool'>{head}{''.join(body)}</details>"


def _part(p, sid):
    kind = p.get("type")
    if kind == "text":
        if p.get("synthetic") or not (p.get("text") or "").strip():
            return ""
        return f"<div class='txt'>{esc(p['text'])}</div>"
    if kind == "reasoning":
        text = (p.get("text") or "").strip()
        return f"<details class='think'><summary>thinking</summary><div class='txt'>{esc(text)}</div></details>" if text else ""
    if kind == "tool":
        return _tool(p, sid)
    if kind == "file":
        return f"<div class='muted'>file: {esc(p.get('filename') or p.get('url') or '')} {esc(p.get('mime') or '')}</div>"
    if kind == "patch":
        files = p.get("files") or []
        return f"<div class='muted'>changed: {esc(', '.join(str(f) for f in files[:20]))}</div>" if files else ""
    if kind == "subtask":
        return (f"<div class='muted'>subtask for <b>{esc(p.get('agent') or '')}</b>: "
                f"{esc(p.get('description') or '')}</div>")
    if kind == "agent":
        return f"<div class='muted'>agent: {esc(p.get('name') or '')}</div>"
    if kind == "compaction":
        return "<div class='muted'>(context compacted)</div>"
    if kind == "retry":
        return f"<div class='muted'>retry: {esc(str((p.get('error') or {}).get('data', {}).get('message', '')))}</div>"
    return ""


def render_messages(msgs, sid, start=0):
    parts = []
    for i, (mid, info, created, plist) in enumerate(msgs[start:], start):
        role = info.get("role") or "?"
        who = [f"<b>{esc(role)}</b>"]
        if info.get("agent"):
            who.append(esc(info["agent"]))
        model = _model_name(info.get("model")) or _model_name({"providerID": info.get("providerID"),
                                                                 "modelID": info.get("modelID")})
        if model:
            who.append(esc(model))
        who.append(esc(_when(created)))
        tok = info.get("tokens") or {}
        if role == "assistant" and (tok.get("input") or tok.get("output")):
            who.append(f"{_ms(tok.get('input')):,} in / {_ms(tok.get('output')):,} out")
        body = "".join(_part(p, sid) for p in plist)
        err = info.get("error")
        if err:
            msg = (err.get("data") or {}).get("message") or err.get("name") or ""
            body += f"<div class='err'>{esc(str(msg))}</div>"
        if role == "assistant" and not body and not info.get("time", {}).get("completed"):
            body = "<div class='muted'>working&hellip;</div>"
        parts.append(f"<div class='msg {esc(role)}' data-i='{i}'><div class='who'>{' &middot; '.join(who)}</div>"
                     f"{body}</div>")
    return "".join(parts)


def render_side(runs, src, row, live, sent=""):
    sid = row["id"]
    now = time.time()
    root = runs.root_of(src, row)
    parts = ["<dl class='facts'>"]
    facts = [("status", None), ("source", src.label), ("folder", row.get("directory")),
             ("agent", row.get("agent")), ("model", _model_name(row.get("model"))),
             ("started", _when(_ms(row.get("time_created")))),
             ("updated", f"{_when(_ms(row.get('time_updated')))} ({_ago(_ms(row.get('time_updated')), now)})"),
             ("tokens", f"{_ms(row.get('tokens_input')):,} in / {_ms(row.get('tokens_output')):,} out"),
             ("session", sid)]
    for k, v in facts:
        if k == "status":
            parts.append(f"<dt>status</dt><dd>{_status(runs, row, live, now)}</dd>")
        elif v:
            parts.append(f"<dt>{k}</dt><dd>{esc(str(v))}</dd>")
    parts.append("</dl>")
    key = runs.ticket_of(row.get("directory") or "")
    if key:
        stage = runs.stage_of(key, _ms(root.get("time_created"))) or "running"
        parts.append(f"<div>Queue ticket <a href='/runs/ticket/{quote(key)}'><b>{esc(key)}</b></a>, "
                     f"stage <code>{esc(stage)}</code></div>")
    kids = runs.children(src, sid)
    if kids:
        parts.append("<h2>Subagent sessions</h2><ul class='runs'>")
        for k in kids:
            parts.append(f"<li><span><a href='/runs/{quote(k['id'])}'>{esc(k.get('title') or k['id'])}</a></span>"
                         f"<span class='when'>{_ago(_ms(k['time_updated']), now)}</span>"
                         f"<span class='meta'>{esc(k.get('agent') or '')}</span></li>")
        parts.append("</ul>")
    parts.append("<h2>Message the agent</h2>")
    if sid in live:
        parts.append(f"<form method='post' action='/runs/{quote(sid)}/send' class='send'>"
                     "<textarea name='text' required placeholder='Sent to the running session; the agent "
                     "reads it at its next step.'></textarea><button type='submit'>Send</button></form>")
        if sent:
            parts.append(f"<div class='muted'>{esc(sent)}</div>")
    elif root and root["id"] != sid and root["id"] in live:
        parts.append(f"<div class='muted'>This is a subagent of a live run; message the "
                     f"<a href='/runs/{quote(root['id'])}'>main session</a>.</div>")
    else:
        parts.append("<div class='muted'>Read-only: this run has finished, or it is an interactive "
                     "session (type in its terminal or opencode web). Headless runs (<code>harness -p</code>, "
                     "<code>harness queue</code>) take messages while they work.</div>")
    items = runs.inbox(src, sid)
    if items:
        parts.append("<ul class='inbox'>")
        for ts, sender, state, text in items[:20]:
            cls = " class='bad'" if state.startswith("failed") or state == "late" else ""
            short = text if len(text) <= 160 else text[:160] + "…"
            parts.append(f"<li{cls}>{esc(_when(ts))} {esc(sender)} <b>{esc(state)}</b>: {esc(short)}</li>")
        parts.append("</ul>")
    return "".join(parts)


POLL_JS = """
(function () {
  var box = document.getElementById('msgs'), side = document.getElementById('side');
  if (!box) return;
  var url = box.getAttribute('data-tail'), idle = 0, last = '';
  function nearBottom() { return window.innerHeight + window.scrollY >= document.body.scrollHeight - 160; }
  function tick() {
    if (document.hidden) { setTimeout(tick, 3000); return; }
    var n = box.querySelectorAll('.msg').length, from = Math.max(0, n - 1);
    fetch(url + '?from=' + from, {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function (d) {
      var stick = nearBottom();
      if (d.sig !== last) {
        last = d.sig;
        box.querySelectorAll('.msg').forEach(function (m) {
          if (+m.getAttribute('data-i') >= d.from) m.remove();
        });
        box.insertAdjacentHTML('beforeend', d.html);
        if (stick) window.scrollTo(0, document.body.scrollHeight);
      }
      var ta = side.querySelector('textarea');
      if (!ta || (!ta.value && document.activeElement !== ta)) side.innerHTML = d.side;
      idle = d.working ? 0 : idle + 1;
      setTimeout(tick, idle > 20 ? 15000 : 2500);
    }).catch(function () { setTimeout(tick, 10000); });
  }
  setTimeout(tick, 2500);
})();
"""

LIST_JS = """
(function () {
  function again() {
    var a = document.activeElement;
    if (document.hidden || (a && a.tagName === 'INPUT')) setTimeout(again, 5000); else location.reload();
  }
  setTimeout(again, 10000);
})();
"""


def render_detail(runs, sid, sent=""):
    """(title, body parts, script) or None."""
    src, row = runs.find(sid)
    if not row:
        return None
    live = runs.live()
    msgs = runs.messages(src, sid)
    crumbs = ["<a href='/'>harness</a><span>/</span><a href='/runs'>runs</a>"]
    key = runs.ticket_of(row.get("directory") or "")
    if key:
        crumbs.append(f"<span>/</span><a href='/runs/ticket/{quote(key)}'>{esc(key)}</a>")
    for p in runs.chain(src, row):
        crumbs.append(f"<span>/</span><a href='/runs/{quote(p['id'])}'>{esc(p.get('title') or p['id'])}</a>")
    title = row.get("title") or sid
    parts = [f"<header class='top'><div class='crumbs'>{''.join(crumbs)}<span>/</span><b>{esc(title)}</b></div>"
             f"<a class='chip' href='/runs/{quote(sid)}.json' target='_blank' rel='noopener'>JSON</a></header>",
             "<div class='cols'><div class='stack'><section class='panel'>"
             f"<div id='msgs' data-tail='/runs/{quote(sid)}/tail'>{render_messages(msgs, sid)}</div>"
             + ("" if msgs else "<div class='muted'>No messages yet.</div>") +
             "<span id='end'></span></section></div>",
             f"<div class='stack'><section class='panel' id='side'>{render_side(runs, src, row, live, sent)}"
             "</section></div></div>"]
    return title, parts, POLL_JS


def tail(runs, sid, start):
    src, row = runs.find(sid)
    if not row:
        return None
    live = runs.live()
    msgs = runs.messages(src, sid)
    start = max(0, min(start, len(msgs)))
    working = sid in live or time.time() - _ms(row.get("time_updated")) / 1000 <= ACTIVE
    body = render_messages(msgs, sid, start)
    side = render_side(runs, src, row, live)
    sig = hashlib.sha1(f"{start}:{len(msgs)}:{body}:{side}".encode()).hexdigest()
    return {"from": start, "count": len(msgs), "html": body, "sig": sig, "working": working, "side": side}


def export(runs, sid):
    src, row = runs.find(sid)
    if not row:
        return None
    return {"info": {k: v for k, v in row.items()}, "source": src.label,
            "children": [k["id"] for k in runs.children(src, sid)],
            "messages": [{"id": mid, "info": info, "time_created": t, "parts": plist}
                         for mid, info, t, plist in runs.messages(src, sid)]}


def part_text(runs, sid, pid):
    if not PART_RE.match(pid or ""):
        return None
    src, row = runs.find(sid)
    if not row:
        return None
    p = runs.part(src, sid, pid)
    if p is None:
        return None
    st = p.get("state") or {}
    out = st.get("output") if st.get("status") != "error" else st.get("error")
    if out is None:
        out = p.get("text", "")
    return out if isinstance(out, str) else json.dumps(out, indent=2, ensure_ascii=False)


def render_ticket(runs, key):
    if not KEY_RE.match(key or "") or key not in runs._ticket_dirs():
        return None
    d = os.path.join(runs.queue_dir, key)
    try:
        with open(os.path.join(d, "log.md"), encoding="utf-8", errors="replace") as f:
            log = f.read()
    except OSError:
        log = ""
    live = runs.live()
    now = time.time()
    outs = runs.stage_outs(key)
    rows = [r for r in runs.sessions(limit=2000) if runs.ticket_of(r.get("directory") or "") == key]
    parts = ["<header class='top'><div class='crumbs'><a href='/'>harness</a><span>/</span><a href='/runs'>runs</a>"
             f"<span>/</span><b>{esc(key)}</b></div></header><div class='cols'><div class='stack'>"
             "<section class='panel'><h2>Stage sessions</h2>"]
    if rows:
        parts.append("<ul class='runs'>")
        for r in sorted(rows, key=lambda r: _ms(r["time_created"])):
            stage = runs.stage_of(key, _ms(r["time_created"]), outs) or "running"
            parts.append(_row(runs, r, live, f"<code>{esc(stage)}</code> ", now))
        parts.append("</ul>")
    else:
        parts.append("<div class='muted'>No sessions for this ticket in the opencode databases.</div>")
    parts.append("</section><section class='panel'><h2>Queue log</h2>"
                 f"<pre class='qlog'>{esc(log) if log.strip() else 'no log yet'}</pre></section></div>"
                 "<div class='stack'><section class='panel'><h2>Stage outputs</h2>")
    if outs:
        parts.append("<ul class='runs'>")
        for name, mtime, size in outs:
            parts.append(f"<li><span><a href='/runs/ticket/{quote(key)}/{quote(name)}' target='_blank' "
                         f"rel='noopener'>{esc(name)}</a></span><span class='when'>{_ago(mtime * 1000, now)}</span>"
                         f"<span class='meta'>{size:,} bytes</span></li>")
        parts.append("</ul>")
    else:
        parts.append("<div class='muted'>None yet.</div>")
    parts.append("</section></div></div>")
    return key, parts


def ticket_file(runs, key, name):
    if not KEY_RE.match(key or "") or not OUT_RE.match(name or "") or key not in runs._ticket_dirs():
        return None
    try:
        with open(os.path.join(runs.queue_dir, key, name), "rb") as f:
            return f.read()
    except OSError:
        return None


def live_summary(runs):
    """The index's panel: live runs, linked."""
    live = runs.live()
    rows = [r for r in runs.sessions(limit=100) if r["id"] in live]
    parts = ["<section class='panel'><h2>Agent runs</h2>"]
    if rows:
        parts.append("<ul class='runs'>")
        now = time.time()
        parts += [_row(runs, r, live, now=now) for r in rows]
        parts.append("</ul>")
    else:
        parts.append("<div class='muted'>No headless run is working right now.</div>")
    parts.append("<a href='/runs'>All sessions, transcripts and queue stages &rarr;</a></section>")
    return parts
