"""The queue page's Jira panel: the project's open sprint and its backlog, in
Jira rank order, each ticket added to the queue with one click.

panel_html(runs) is a placeholder that renders at once (no network); the page's
script (JS) asks /runs/queue/jira?list=sprint|backlog&mine=1 for panel_json,
which answers from a 60 s in-process cache and refreshes a stale entry in the
background, so neither the page nor a reload waits on Jira. Add (and Add
selected) opens the queue page's Add dialog with the ticket keys filled in and
their summaries shown (window.hAddOpen, in runs.py's script), so the owner sets
the stages, target branch and note before anything is queued, and the add is
checked exactly as one typed there.

The Jira URL and the way the token is sent are scripts/vault_ticket.py's
(tracker_url in the vault charter; TRACKER_TOKEN as Bearer, email:api-token as
Basic; https only, no redirect followed). harness serve passes TRACKER_TOKEN to
pub.py alone; this module takes it out of the environment when imported, so no
process serve starts inherits it. The token is never logged or shown.

Searches: Jira Cloud (*.atlassian.net) answers /rest/api/3/search/jql (its old
/rest/api/{2,3}/search answer 410); Server and Data Center answer
/rest/api/2/search. Each is tried first where it belongs and the other on 404
or 410, and the one that answered is remembered per Jira.
"""
import html
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse

_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import vault_ticket  # noqa: E402

TIMEOUT = 8          # seconds a Jira search may take
FRESH = 60           # seconds a result is served without asking Jira again
ERR_FRESH = 15       # seconds an error is kept before Jira is asked again
MAX_ROWS = 50
FIELDS = "summary,status,assignee,priority,issuetype"
LISTS = ("sprint", "backlog")
CLOUD = "/rest/api/3/search/jql"
SERVER = "/rest/api/2/search"
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")

_token = os.environ.pop("TRACKER_TOKEN", "").strip()
_cache = {}          # (url, projects, list, mine) -> {"at", "rows", "err", "busy"}
_lock = threading.Lock()
_endpoint = {}       # Jira url -> the search path that answered there

esc = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731


def token():
    return _token


def set_token(tok):
    """For tests."""
    global _token
    _token = (tok or "").strip()


# --- what to ask --------------------------------------------------------------

def _local_prefix():
    return vault_ticket.local_prefix()


def projects(runs, meta):
    """The Jira project keys the panel lists: the charter's ticket_pattern when it
    names one (ABC-[0-9]+), else the projects of the queue's tickets (archived
    too), else of the tickets pulled into the vault; most used first, at most 3."""
    m = re.match(r"^\^?([A-Z][A-Z0-9_]*)-", str(meta.get("ticket_pattern") or ""))
    if m:
        return [m.group(1)]
    local = _local_prefix()
    for keys in (lambda: runs.ticket_keys() + runs.archived_keys(), lambda: _vault_keys(runs)):
        try:
            got = keys()
        except Exception:
            got = []
        count = {}
        for k in got:
            p = k.rsplit("-", 1)[0]
            if KEY_RE.match(k) and p != local:
                count[p] = count.get(p, 0) + 1
        if count:
            return sorted(count, key=lambda p: (-count[p], p))[:3]
    return []


def _vault_keys(runs):
    d = os.path.join(runs.vault, ".raw", "tickets") if runs.vault else ""
    return [n[:-3] for n in (os.listdir(d) if d and os.path.isdir(d) else []) if n.endswith(".md")]


def jql(projs, which, mine):
    p = f'project = "{projs[0]}"' if len(projs) == 1 else "project in (" + ", ".join(f'"{x}"' for x in projs) + ")"
    if which == "sprint":
        q = f"{p} AND sprint in openSprints()"
    else:
        q = f"{p} AND (sprint is EMPTY OR sprint not in openSprints()) AND statusCategory != Done"
    return q + (" AND assignee = currentUser()" if mine else "") + " ORDER BY Rank ASC"


def is_cloud(url):
    return (urllib.parse.urlparse(url).hostname or "").lower().endswith(".atlassian.net")


def _paths(url):
    known = _endpoint.get(url)
    order = [CLOUD, SERVER] if is_cloud(url) else [SERVER, CLOUD]
    return [known] + [p for p in order if p != known] if known else order


# --- asking Jira --------------------------------------------------------------

def _jira_message(e):
    """The first error Jira gave in an error answer ({"errorMessages": [...], "errors": {...}}), or ""."""
    try:
        d = json.loads(e.read(4000).decode("utf-8", "replace"))
    except Exception:
        return ""
    msgs = [str(m) for m in (d.get("errorMessages") or []) if m] if isinstance(d, dict) else []
    if isinstance(d, dict) and isinstance(d.get("errors"), dict):
        msgs += [str(v) for v in d["errors"].values() if v]
    return " ".join(msgs[0].split())[:200] if msgs else ""


def search(url, tok, q):
    """(issues, "") or (None, one short line why not)."""
    params = urllib.parse.urlencode({"jql": q, "fields": FIELDS, "maxResults": MAX_ROWS})
    last = ""
    for path in _paths(url):
        try:
            d = vault_ticket.get_json(f"{url}{path}?{params}", tok, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                last = f"Jira has no search at {path} (HTTP {e.code})"
                continue
            if e.code in (401, 403):
                return None, f"Jira refused the token (HTTP {e.code}). Run harness ticket login, then restart serve."
            why = _jira_message(e)
            return None, f"Jira answered HTTP {e.code}" + (f": {why}" if why else "")
        except vault_ticket.Redirected as e:
            return None, f"Jira {e.reason}"
        except (socket.timeout, TimeoutError):
            return None, f"Jira did not answer in {TIMEOUT} s."
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return None, f"Jira did not answer in {TIMEOUT} s."
            why = getattr(reason, "strerror", None) or str(reason)   # "Connection refused", not "[Errno 111] ..."
            return None, f"Jira is not reachable: {why[:120]}."
        except ValueError:
            return None, "Jira's answer is not JSON."
        if not isinstance(d, dict) or not isinstance(d.get("issues"), list):
            return None, "Jira's answer has no issues list."
        _endpoint[url] = path
        return d["issues"][:MAX_ROWS], ""
    return None, last or "Jira has no search endpoint."


def row(issue):
    f = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    st = f.get("status") if isinstance(f.get("status"), dict) else {}
    cat = st.get("statusCategory") if isinstance(st.get("statusCategory"), dict) else {}
    return {"key": str(issue.get("key") or ""), "summary": vault_ticket._value(f.get("summary")),
            "status": vault_ticket._value(st), "cat": str(cat.get("key") or ""),
            "assignee": vault_ticket._value(f.get("assignee")), "type": vault_ticket._value(f.get("issuetype")),
            "priority": vault_ticket._value(f.get("priority"))}


def _fetch(ck, url, tok, q):
    issues, err = search(url, tok, q)
    with _lock:
        e = _cache.setdefault(ck, {"rows": None})
        e["busy"] = False
        e["at"] = time.time()
        e["err"] = err
        if issues is not None:
            e["rows"] = [r for r in (row(i) for i in issues if isinstance(i, dict)) if KEY_RE.match(r["key"])]


def lookup(ck, url, tok, q, now=None):
    """The cache entry for ck: fresh as it is; stale refreshed in the background
    (what it had is served meanwhile); none fetched now (the first ask waits)."""
    now = now or time.time()
    with _lock:
        e = _cache.get(ck)
        if e is not None and e.get("at") is not None:
            age = now - e["at"]
            if age >= (ERR_FRESH if e.get("err") else FRESH) and not e.get("busy"):
                e["busy"] = True
                threading.Thread(target=_fetch, args=(ck, url, tok, q), daemon=True).start()
            return dict(e)
    _fetch(ck, url, tok, q)
    with _lock:
        return dict(_cache[ck])


# --- the panel ----------------------------------------------------------------

def _problem(runs):
    """(url, projects, "") or ("", [], one line why the panel lists nothing)."""
    if not runs.vault:
        return "", [], "No active vault, so no Jira URL (harness vault use NAME)."
    meta = vault_ticket.charter(runs.vault)
    url = str(meta.get("tracker_url") or "").strip().rstrip("/")
    if not url:
        return "", [], "No Jira URL in the vault charter. Run harness ticket login."
    bad = vault_ticket.url_problem(url)
    if bad:
        return "", [], bad[0].upper() + bad[1:] + "."
    if not token():
        return url, [], "Serve has no Jira token. Run harness ticket login, then restart harness serve."
    projs = projects(runs, meta)
    if not projs:
        return url, [], "No Jira project known yet: add one of its tickets by key below and its project shows here."
    return url, projs, ""


def _queued(runs):
    """{KEY: status} of the queue's tickets ("archived" for one put away)."""
    out = {}
    try:
        for k in runs.archived_keys():
            out[k] = "archived"
        for k in runs.ticket_keys():
            out[k] = runs.ticket_state(k).get("status", "") or "queued"
    except Exception:
        pass
    return out


def _rows_html(url, rows, queued):
    import runs as runs_view   # the queue page's own pill, as the tickets list shows it
    out = []
    for r in rows:
        k = r["key"]
        st = queued.get(k)
        if not st and r["cat"] == "done":   # finished in Jira: nothing to queue
            act, box = "<span class='muted'>done</span>", "<span class='jcb'></span>"
        elif st:
            act = (f"<a class='jq' href='/runs/ticket/{urllib.parse.quote(k)}' title='in the queue'>"
                   f"{runs_view._pill(st)}</a>")
            box = "<span class='jcb'></span>"
        else:
            act = f"<button type='button' class='sm' data-add='{esc(k)}'>Add</button>"
            box = f"<input type='checkbox' class='jcb' value='{esc(k)}' aria-label='select {esc(k)}'>"
        meta = " &middot; ".join(esc(x) for x in (r["type"], r["priority"], r["assignee"] or "unassigned") if x)
        cat = {"done": "t-ok", "indeterminate": "t-run"}.get(r["cat"], "t-off")
        out.append(f"<div class='jr{' in' if st else ''}' data-key='{esc(k)}'>{box}"
                   f"<a class='jk' href='{esc(url)}/browse/{esc(urllib.parse.quote(k))}' target='_blank' "
                   f"rel='noopener'>{esc(k)}</a>"
                   f"<div class='js'><div class='jt' title='{esc(r['summary'])}'>{esc(r['summary'])}</div>"
                   f"<div class='jm'><span class='jst {cat}'>{esc(r['status'])}</span> {meta}</div></div>"
                   f"<div class='ja'>{act}</div></div>")
    return "".join(out)


def _q(query, name):
    if isinstance(query, str):
        query = urllib.parse.parse_qs(query)
    v = (query or {}).get(name) or [""]
    return v[0] if isinstance(v, list) else str(v)


def panel_json(runs, query):
    """{"html", "note", "error", "count", "more"} for one list (query: list=sprint|backlog, mine=1)."""
    which = _q(query, "list") if _q(query, "list") in LISTS else "sprint"
    mine = _q(query, "mine") == "1"
    try:
        url, projs, why = _problem(runs)
        if why:
            return {"error": why, "html": "", "count": 0}
        q = jql(projs, which, mine)
        e = lookup((url, tuple(projs), which, mine), url, token(), q)
        rows = e.get("rows")
        if rows is None:
            return {"error": e.get("err") or "Jira gave nothing.", "html": "", "count": 0}
        note = f"Showing an earlier list. {e['err']}" if e.get("err") else ""
        more = f"{url}/issues/?jql={urllib.parse.quote(q)}"
        empty = ("<div class='empty'>" + ("Nothing assigned to you here." if mine else
                 "No open sprint." if which == "sprint" else "The backlog is empty.") + "</div>")
        return {"html": _rows_html(url, rows, _queued(runs)) or empty, "note": note, "error": "",
                "count": len(rows), "full": len(rows) >= MAX_ROWS, "more": more, "projects": projs}
    except Exception as ex:   # never a broken page
        return {"error": f"The Jira panel failed: {type(ex).__name__}.", "html": "", "count": 0}


STYLE = """
#jira > summary { flex-wrap: wrap; }
#jira .jbar { display: flex; align-items: center; gap: 8px 12px; flex-wrap: wrap; flex: 1; min-width: 0; }
#jira .jtabs { display: inline-flex; gap: 2px; padding: 2px; border-radius: 8px; background: var(--sunk); }
#jira .jtabs button { height: 26px; border: 0; background: transparent; color: var(--muted); font-size: 12.5px; }
#jira .jtabs button[aria-selected=true] { background: var(--raised); color: var(--ink); box-shadow: 0 1px 2px rgba(0,0,0,.12); }
#jira .jtools { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-left: auto; font-size: 12.5px; }
#jira .jtools label { display: inline-flex; align-items: center; gap: 5px; color: var(--muted); cursor: pointer; }
#jira .jlist { max-height: 340px; overflow-y: auto; }
#jira .jtools, #jira .jproj { font-family: var(--sans); }
#jira.jerr .jtools, #jira.jerr .jfoot { display: none; }
#jira .jline button { margin-left: 10px; }
#jira .jr { display: grid; grid-template-columns: 18px 96px minmax(0, 1fr) auto; gap: 4px 10px; align-items: center;
  padding: 7px 18px; border-top: 1px solid var(--line); }
#jira .jr:first-child { border-top: 0; }
#jira .jr:hover { background: var(--hover); }
#jira .jr.in .jk { color: var(--muted); }
#jira .jk { font: 600 13px var(--mono); color: var(--ink); white-space: nowrap; }
#jira .js { min-width: 0; }
#jira .jt { font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#jira .jm { font-size: 11.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#jira .jst { padding: 0 5px; border-radius: 4px; font-weight: 600; }
#jira .ja { display: flex; justify-content: flex-end; }
#jira .jline { padding: 12px 18px; font-size: 13px; color: var(--muted); }
#jira .jline.err { color: var(--bad); }
#jira .jfoot { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 8px 18px;
  border-top: 1px solid var(--line); font-size: 12.5px; color: var(--muted); }
#jira .jfoot .jmsg.err { color: var(--bad); }
#jira .jfoot a { margin-left: auto; }
#jira .jfoot:has(.jmsg:empty):has(.jmore[hidden]) { display: none; }
@media (max-width: 640px) {
  #jira .jr { grid-template-columns: 18px minmax(0, 1fr) auto; padding: 8px 14px; }
  #jira .jk { grid-column: 2; }
  #jira .js { grid-column: 2 / 4; grid-row: 2; }
  #jira .jt { white-space: normal; }
  #jira .ja { grid-column: 3; grid-row: 1; }
  #jira .jtools { margin-left: 0; }
}
"""


def panel_html(runs):
    """The placeholder the page's script fills (JS): nothing read from Jira here."""
    return (f"<style>{STYLE}</style>"
            "<details class='panel flush arch' id='jira' data-k='jira' data-url='/runs/queue/jira' data-nomem open>"
            "<summary class='hd'><h2>Jira <span class='muted jproj'></span></h2><div class='jbar'>"
            "<div class='jtabs' role='tablist'>"
            "<button type='button' role='tab' data-list='sprint' aria-selected='true'>Sprint</button>"
            "<button type='button' role='tab' data-list='backlog' aria-selected='false'>Backlog</button></div>"
            "<div class='jtools'><label><input type='checkbox' class='jmine'> Mine only</label>"
            "<button type='button' class='sm primary jaddsel' disabled>Add selected</button></div></div></summary>"
            "<div class='jlist' aria-live='polite'><div class='jline'>Loading&hellip;</div></div>"
            "<div class='jfoot'><span class='jmsg' aria-live='polite'>Add opens the ticket's settings first: stages, "
            "target branch, a note for the agents.</span><a class='jmore' target='_blank' rel='noopener' hidden>Open in Jira</a></div>"
            "</details>")


# The panel's script, in the queue page's one script (pub.py). Add opens the Add
# dialog (window.hAddOpen) with kind=jira, the keys and each one's summary.
JS = r"""
(function () {
  var box = document.getElementById('jira');
  if (!box) return;
  var list = box.querySelector('.jlist'), msg = box.querySelector('.jmsg'), more = box.querySelector('.jmore'),
    mine = box.querySelector('.jmine'), proj = box.querySelector('.jproj'), addSel = box.querySelector('.jaddsel'), base = box.getAttribute('data-url'),
    which = 'sprint', seq = 0, hint = msg.textContent;
  try { which = localStorage.getItem('hjira:list') || 'sprint'; mine.checked = localStorage.getItem('hjira:mine') === '1'; } catch (e) {}
  function say(t, bad) { msg.textContent = t; msg.title = ''; msg.className = 'jmsg' + (bad ? ' err' : ''); }
  function line(t, bad, again) {
    list.innerHTML = ''; var d = document.createElement('div'); d.className = 'jline' + (bad ? ' err' : ''); d.textContent = t;
    if (again) { var b = document.createElement('button'); b.type = 'button'; b.className = 'sm'; b.setAttribute('data-retry', ''); b.textContent = 'Try again'; d.appendChild(b); }
    list.appendChild(d);
  }
  function picked() { return Array.prototype.map.call(list.querySelectorAll('input.jcb:checked'), function (x) { return x.value; }); }
  function sync() { var n = picked().length; addSel.disabled = !n; addSel.textContent = n ? 'Add selected (' + n + ')' : 'Add selected'; }
  function load(quiet) {
    var n = ++seq;
    box.querySelectorAll('.jtabs button').forEach(function (b) { b.setAttribute('aria-selected', b.getAttribute('data-list') === which ? 'true' : 'false'); });
    if (!quiet) line('Loading…');
    fetch(base + '?list=' + which + (mine.checked ? '&mine=1' : ''), {credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { if (!r.ok) throw r.status; return r.json(); })
      .then(function (d) {
        if (n !== seq) return;
        if (d.projects) proj.textContent = d.projects.join(', ');
        box.classList.toggle('jerr', !!d.error);
        if (d.error) { line(d.error, true, true); more.hidden = true; if (msg.textContent === hint) say(''); sync(); return; }
        var keep = picked();
        list.innerHTML = d.html;
        keep.forEach(function (k) { var x = list.querySelector('input.jcb[value="' + k + '"]'); if (x) x.checked = true; });
        if (d.more) { more.href = d.more; more.hidden = false; more.textContent = d.full ? 'First ' + d.count + ', all in Jira' : 'Open in Jira'; }
        if (d.note) say(d.note, true);
        sync();
      }).catch(function () { if (n === seq) { box.classList.add('jerr'); line('The Jira list did not load.', true, true); } });
  }
  function add(keys) {
    if (!keys.length) return;
    if (!window.hAddOpen) { say('The Add form is not on this page.', true); return; }
    window.hAddOpen(keys, keys.map(function (k) {
      var t = list.querySelector('.jr[data-key="' + k + '"] .jt');
      return t ? t.textContent : '';
    }));
  }
  box.addEventListener('click', function (ev) {
    // the tabs and tools sit in the summary (their own clicks do not open or close it): nor does a gap between them
    if (ev.target.closest('.jbar') && !ev.target.closest('button, label, input')) ev.preventDefault();
    var t = ev.target.closest('button');
    if (!t || !box.contains(t)) return;
    if (t.hasAttribute('data-list')) { which = t.getAttribute('data-list'); try { localStorage.setItem('hjira:list', which); } catch (e) {} say(hint); load(); }
    else if (t.hasAttribute('data-add')) add([t.getAttribute('data-add')]);
    else if (t === addSel) add(picked());
    else if (t.hasAttribute('data-retry')) load();
  });
  box.addEventListener('change', function (ev) {
    if (ev.target === mine) { try { localStorage.setItem('hjira:mine', mine.checked ? '1' : '0'); } catch (e) {} load(); }
    else sync();
  });
  function first() { if (box.open && !seq) load(); }
  box.addEventListener('toggle', first);
  first();
})();
"""
