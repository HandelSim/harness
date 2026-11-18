"""The queue page's findings board: every finding the queue's agents noted, in one list.

Two sources, both read on each poll (a parsed file is kept until it changes):
  - the tickets' findings notes in the vault (wiki/notes/tickets/KEY-findings.md):
    each top-level list item is one finding, with its indented continuation lines;
    headings and other text are skipped. The ticket page still shows the note itself.
  - the findings agent's <state>/findings/findings.json, when there is one.

What the owner does with them is kept in <state>/findings/board.json:
{"version": 1, "order": [id, ...], "archived": {id: ISO UTC}}. Ids are stable: a
ticket finding's is t- and a hash of its key and its text (whitespace folded, so a
re-wrap keeps it); the agent's findings carry their own.

  POST /runs/findings/archive   id=ID (or the ticked sel=ID fields): archive them
  POST /runs/findings/restore   id=ID: back on the board
  POST /runs/findings/move      key=ID, before=ID or empty (the end): the drag helper's

<state> is the queue folder's parent (harness passes --queue-dir <state>/queue).
"""

import calendar
import contextlib
import hashlib
import html
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

esc = html.escape   # runs imports this module too: nothing of it is used at import time

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")       # runs.KEY_RE
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ITEM_RE = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+(.*)$")  # a top-level list item
CHECK_RE = re.compile(r"^\[[ xX]\]\s+")                 # a task list's box
WHERE_RE = re.compile(r"^[*_]{0,2}`?(?P<path>[^\s`:*]+?)`?:(?P<line>\d+)(?:[-,]\d+)?`?[*_]{0,2}\s*[:\-]?\s*")
SEEN_RE = re.compile(r"\(seen while (planning|implementing|testing|reviewing|fixing)\b[^)]*\)[\s.]*$", re.I)
STAGE = {"planning": "plan", "implementing": "implement", "testing": "test", "reviewing": "review",
         "fixing": "fix"}
SEVERITY = {"high": "t-bad", "medium": "t-wait", "low": "t-off"}
ACTIONS = ("archive", "restore", "move")
OPEN_CAP = 300          # open findings the board lists; a count of the rest
ARCHIVED_CAP = 200      # and archived ones
GONE_KEEP = 1000        # archived ids whose finding is gone (its note edited, a vault away) still kept
NOTE_MAX = 512 * 1024   # bytes of one findings note read
AGENT_MAX = 8 * 1024 * 1024   # and of findings.json
TITLE_MAX = 300
TEXT_MAX = 4000
FORM_MAX = 64 * 1024
STEPS_MAX = 12        # a bug's steps to see it in the app
TRACE_MAX = 10        # and its code path
LOCATIONS_MAX = 20    # a rolling finding's item: the places it is
ITEMS_SHOWN = 50      # a rolling finding's items listed in its row
FORM_FIELDS = 2 * OPEN_CAP + 20

_CACHE = {}             # path: ((mtime_ns, size), parsed)
_CACHE_LOCK = threading.Lock()


# --- where things are ----------------------------------------------------------

def state_dir(runs):
    q = getattr(runs, "queue_dir", "") or ""
    return os.path.dirname(os.path.normpath(q)) if q else ""


def board_path(runs):
    d = state_dir(runs)
    return os.path.join(d, "findings", "board.json") if d else ""


def agent_path(runs):
    d = state_dir(runs)
    return os.path.join(d, "findings", "findings.json") if d else ""


def _cached(path, cap, parse):
    """parse(text) of the file, kept until its mtime or size changes; None when it cannot be read."""
    try:
        s = os.stat(path)
    except OSError:
        return None
    stamp = (s.st_mtime_ns, s.st_size)
    with _CACHE_LOCK:
        hit = _CACHE.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, "rb") as f:
            text = f.read(cap).decode("utf-8", "replace")
    except OSError:
        return None
    got = (s.st_mtime_ns // 1000000, parse(text))
    with _CACHE_LOCK:
        if len(_CACHE) > 2000:
            _CACHE.clear()
        _CACHE[path] = (stamp, got)
    return got


# --- the tickets' notes ----------------------------------------------------------

def parse_note(text):
    """The note's findings: [text of each top-level list item with its continuation lines]."""
    out, cur = [], None
    in_front = text.startswith("---\n")
    for n, line in enumerate(text.splitlines()):
        if in_front:   # front matter: up to its closing ---
            if n and line.strip() == "---":
                in_front = False
            continue
        m = ITEM_RE.match(line)
        if m:
            cur = [m.group(1)]
            out.append(cur)
        elif cur is not None and line[:1] in (" ", "\t") and line.strip():
            cur.append(line.strip())
        elif line.strip():
            cur = None   # a heading or prose: the item before it has ended
    return [t for t in (" ".join(" ".join(c).split()) for c in out) if t]


def _split(text):
    """(path, line, the rest, stage) of a finding's text: path:line at its start, the
    stage of its "(seen while reviewing KEY)" at its end."""
    rest, path, line = CHECK_RE.sub("", text), "", 0
    m = WHERE_RE.match(rest)
    if m and ("/" in m.group("path") or "." in m.group("path")):
        path, line, rest = m.group("path"), int(m.group("line")), rest[m.end():]
    s = SEEN_RE.search(rest)
    stage = STAGE.get(s.group(1).lower(), "") if s else ""
    if s:
        rest = rest[:s.start()].rstrip()
    return path, line, rest or text, stage


def ticket_id(key, text):
    return "t-" + hashlib.sha1((key + "\n" + " ".join(text.split())).encode("utf-8")).hexdigest()[:12]


def _keys(runs):
    """The queue's tickets (queue.list), then the archived ones (archived.list), then any
    other ticket folder: their findings notes stay on the board."""
    q, out = runs.queue_dir, []
    for name in ("queue.list", "archived.list"):
        try:
            with open(os.path.join(q, name), encoding="utf-8") as f:
                out += f.read().split()
        except OSError:
            pass
    try:
        out += sorted(os.listdir(q))
    except OSError:
        pass
    seen, keys = set(), []
    for k in out:
        if KEY_RE.match(k) and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _vault_of(runs, key):
    """The vault the queue ran the ticket with (runs.vault_of), else the active one."""
    try:
        with open(os.path.join(runs.queue_dir, key, "state.json"), encoding="utf-8") as f:
            v = json.load(f).get("vault")
        if isinstance(v, str) and os.path.isdir(v):
            return v
    except (OSError, ValueError, AttributeError):
        pass
    return runs.vault or ""


def _ticket_findings(runs, key):
    vault = _vault_of(runs, key)
    if not vault:
        return []
    got = _cached(os.path.join(vault, "wiki", "notes", "tickets", f"{key}-findings.md"), NOTE_MAX, parse_note)
    if not got:
        return []
    ms, texts = got
    out = []
    for pos, text in enumerate(texts):
        path, line, rest, stage = _split(text)
        md = f"- {text}" + ("" if key in text else f" ({key})")
        out.append({"id": ticket_id(key, text), "src": key, "key": key, "cat": "", "sev": "", "stage": stage,
                    "title": rest[:TITLE_MAX], "text": text[:TEXT_MAX], "body": rest[:TEXT_MAX], "evidence": "",
                    "path": path, "line": line, "ms": ms, "pos": pos, "md": md, "project": ""})
    return out


# --- the findings agent ------------------------------------------------------------

def iso_ms(text):
    """An ISO 8601 stamp as epoch ms (no zone: UTC), 0 if it is not one."""
    if not isinstance(text, str) or not text:
        return 0
    t = text.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        try:
            return calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")) * 1000
        except ValueError:
            return 0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _str(v, cap):
    return " ".join(v.split())[:cap] if isinstance(v, str) else ""


def _line(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else (
        int(v) if isinstance(v, str) and v.isdigit() else 0)


def _strs(v, cap, n):
    """A list of strings, each folded and cut to cap, the empty ones and the non-strings
    dropped, at most n of them; [] when it is not a list."""
    if not isinstance(v, list):
        return []
    return [x for x in (_str(s, cap) for s in v) if x][:n]


def _trace(v):
    """The code path, entry by entry: {path, line, code}; an entry of the wrong shape is dropped."""
    out = []
    for t in v if isinstance(v, list) else []:
        if not isinstance(t, dict) or not isinstance(t.get("path"), str) or not isinstance(t.get("code"), str):
            continue
        line = t.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 0:
            continue
        path = _str(t["path"], 500)
        if path:
            out.append({"path": path, "line": line, "code": _str(t["code"], 500)})
        if len(out) >= TRACE_MAX:
            break
    return out


def _at(path, line):
    return f"{path}:{line}" if path and line else path


def _items(v):
    """A rolling finding's open items, checked: {id, path, line, note, evidence, locations,
    section} (section: a unify item's, such as std, qt or a library's name; "" for others)."""
    out, seen = [], set()
    for i in v if isinstance(v, list) else []:
        if not isinstance(i, dict) or not isinstance(i.get("id"), str) or not ID_RE.match(i["id"]):
            continue
        if i.get("status") not in (None, "", "open") or i["id"] in seen:
            continue
        seen.add(i["id"])
        out.append({"id": i["id"], "path": _str(i.get("path"), 500), "line": _line(i.get("line")),
                    "note": _str(i.get("note"), TEXT_MAX), "evidence": _str(i.get("evidence"), 500),
                    "locations": _strs(i.get("locations"), 500, LOCATIONS_MAX),
                    "section": re.sub(r"[^a-z0-9]", "", _str(i.get("section"), 30).lower())})
    return out


def sections(items):
    """[(section, items)]: one ("", items) when no item has a section; else by section,
    "other" (and items with none) last."""
    if not any(i["section"] for i in items):
        return [("", items)]
    by = {}
    for i in items:
        by.setdefault(i["section"] or "other", []).append(i)
    return sorted(by.items(), key=lambda kv: (kv[0] == "other", kv[0]))


def rolled(f, items):
    """A rolling finding's row with these items: its title counts them, its md lists them."""
    n = len(items)
    md = f"- {f['rtitle']} ({n} place{'' if n == 1 else 's'}, findings agent)"
    for sec, its in sections(items):
        pad = "    " if sec else "  "
        if sec:
            md += f"\n  - {sec} ({len(its)})"
        md += "".join(f"\n{pad}- " + ": ".join(x for x in (_at(i["path"], i["line"]), i["note"] or i["evidence"])
                                              if x) for i in its)
    items = [i for _, its in sections(items) for i in its]   # in the order they are shown
    return dict(f, title=f"{f['rtitle']} ({n})", md=md, items=items, children=[i["id"] for i in items])


def parse_agent(text):
    """findings.json's findings, each checked: a hand edit or a newer writer must not
    take the page down. [] when it is not what it should be.

    Three shapes: a plain finding; a bug that also says how to see it in the app (ui,
    steps, expected, actual, trace, verdict); a rolling one (kind "rolling"): one row per
    theme with its open items, each of which can be archived on its own."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("findings") if isinstance(data, dict) else None
    out, seen = [], set()
    for pos, f in enumerate(items if isinstance(items, list) else []):
        if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not ID_RE.match(f["id"]):
            continue
        if f.get("status") not in (None, "", "open"):   # the agent's "stale": its evidence left the code
            continue
        fid = f["id"]
        title, detail = _str(f.get("title"), TITLE_MAX), _str(f.get("detail"), TEXT_MAX)
        cat, sev = _str(f.get("category"), 40).lower(), _str(f.get("severity"), 20).lower()
        project = _str(f.get("project"), 1000)
        if fid in seen:
            continue
        if f.get("kind") == "rolling":
            its = _items(f.get("items"))
            title = title or detail[:TITLE_MAX] or cat
            if not its or not title:
                continue
            seen.add(fid)
            out.append(rolled({"id": fid, "src": "agent", "key": "", "cat": cat, "sev": "", "stage": "",
                               "kind": "rolling", "rtitle": title,
                               "text": f"{title}: {detail}" if detail and detail != title else title,
                               "body": detail, "evidence": "", "path": "", "line": 0,
                               "ms": iso_ms(f.get("found_at")) or iso_ms(f.get("updated_at")), "pos": pos,
                               "project": project}, its))
            continue
        if not (title or detail):
            continue
        seen.add(fid)
        path = _str(f.get("path"), 500)
        line = _line(f.get("line"))
        where = (f"{path}:{line}: " if line else f"{path}: ") if path else ""
        tags = ", ".join(x for x in (cat, sev) if x)
        md = f"- {where}{title or detail}" + (f": {detail}" if title and detail and detail != title else "") + (
            f" ({tags}, findings agent)" if tags else " (findings agent)")
        steps, trace = _strs(f.get("steps"), 500, STEPS_MAX), _trace(f.get("trace"))
        expected, actual = _str(f.get("expected"), TEXT_MAX), _str(f.get("actual"), TEXT_MAX)
        md += "".join(x for x in (
            ("\n  Steps: " + " ".join(f"{n}. {s}" for n, s in enumerate(steps, 1))) if steps else "",
            f"\n  Expected: {expected}" if expected else "",
            f"\n  Actual: {actual}" if actual else "",
            ("\n  Trace: " + ", ".join(_at(t["path"], t["line"]) for t in trace)) if trace else ""))
        out.append({"id": fid, "src": "agent", "key": "", "cat": cat, "sev": sev, "stage": "",
                    "title": title or detail[:TITLE_MAX], "text": f"{title}: {detail}" if title and detail else title or detail,
                    "body": detail, "evidence": _str(f.get("evidence"), 500), "path": path, "line": line, "ms": iso_ms(f.get("found_at")), "pos": pos, "md": md,
                    "project": project, "ui": _str(f.get("ui"), TITLE_MAX), "steps": steps, "expected": expected,
                    "actual": actual, "trace": trace, "verdict": _str(f.get("verdict"), TEXT_MAX)})
    return out


def collect(runs):
    """Every finding now, tickets' (in queue order) then the agent's."""
    out = []
    if runs is not None and runs.queue_dir:
        for k in _keys(runs):
            out += _ticket_findings(runs, k)
    path = agent_path(runs) if runs is not None else ""
    got = _cached(path, AGENT_MAX, parse_agent) if path else None
    if got:
        out += got[1]
    seen, uniq = set(), []
    for f in out:   # the same text twice in a note: one finding
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    return uniq


# --- the owner's board ---------------------------------------------------------------

def read_board(path):
    b = {"version": 1, "order": [], "archived": {}}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return b
    if not isinstance(raw, dict):
        return b
    order = raw.get("order")
    if isinstance(order, list):
        seen = set()
        for i in order:
            if isinstance(i, str) and ID_RE.match(i) and i not in seen:
                seen.add(i)
                b["order"].append(i)
    arch = raw.get("archived")
    if isinstance(arch, dict):
        b["archived"] = {i: (at if isinstance(at, str) else "") for i, at in arch.items()
                         if isinstance(i, str) and ID_RE.match(i)}
    return b


def _write_atomic(path, text):
    """ticket_queue.write_atomic: written beside it, synced, moved over it."""
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for i in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:   # Windows: another process has it open for a moment
                if os.name != "nt" or i == 19:
                    raise
                time.sleep(0.1)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _try_lock(f):
    """vault_ingest.try_lock: fcntl on POSIX, msvcrt (one byte at offset 0) on Windows."""
    try:
        import fcntl
    except ImportError:
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def held(path, wait=5):
    """ticket_queue.held: an exclusive lock on path for a short read-modify-write;
    TimeoutError when another process keeps it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        end = time.time() + wait
        while not _try_lock(f):
            if time.time() > end:
                raise TimeoutError(f"{path} is held by another process")
            time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                with contextlib.suppress(OSError):
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def arrange(items, board):
    """(open findings in board order, archived ones newest archived first, ids not in order).
    Order: the board's order first, then the ones it does not have yet, newest first."""
    by = {f["id"]: f for f in items}
    placed = [by[i] for i in board["order"] if i in by]
    known = {f["id"] for f in placed}
    fresh = sorted((f for f in items if f["id"] not in known), key=lambda f: (f["ms"], f["pos"]), reverse=True)
    allf = placed + fresh
    arch = board["archived"]
    open_ = [f for f in allf if f["id"] not in arch]
    archived = sorted((f for f in allf if f["id"] in arch), key=lambda f: (arch[f["id"]], f["id"]), reverse=True)
    return open_, archived, {f["id"] for f in fresh}, allf


def _save(path, board, existing):
    """board.json with ids that no longer exist dropped from order; archived ones that
    are gone are kept (a note can come back, its vault be active again), the newest
    GONE_KEEP of them."""
    order = [i for i in board["order"] if i in existing]
    gone = sorted(((at, i) for i, at in board["archived"].items() if i not in existing), reverse=True)
    arch = {i: at for i, at in board["archived"].items() if i in existing}
    arch.update({i: at for at, i in gone[:GONE_KEEP]})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_atomic(path, json.dumps({"version": 1, "order": order, "archived": arch}, indent=1) + "\n")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ids(form, name):
    return [i for i in (form.get(name) or []) if ID_RE.match(i)]


def action(runs, act, form):
    """(ok, message) of an owner's archive, restore or move."""
    path = board_path(runs)
    if not path or act not in ACTIONS:
        return False, "no queue folder: the findings board has nowhere to keep its state"
    lock = os.path.join(os.path.dirname(path), "board.lock")
    try:
        with held(lock):
            items = collect(runs)
            rows = {f["id"] for f in items}
            # a rolling finding's items are archived (and restored) one by one too
            existing = rows | {c for f in items for c in f.get("children", ())}
            board = read_board(path)
            if act == "move":
                key = (form.get("key") or [""])[0]
                before = (form.get("before") or [""])[0]
                if key not in rows:
                    return False, "that finding is not on the board any more: reload the page"
                if before and before not in rows:
                    return False, "the finding it was dropped next to is gone: reload the page"
                order = [f["id"] for f in arrange(items, board)[3] if f["id"] != key]
                order.insert(order.index(before) if before and before != key else len(order), key)
                board["order"] = order
                _save(path, board, existing)
                return True, "Moved."
            ids = _ids(form, "id") or _ids(form, "sel")
            if not ids:
                return False, "no finding picked: tick one or more first"
            hit = [i for i in dict.fromkeys(ids) if i in existing]
            if not hit:
                return False, "that finding is not on the board any more: reload the page"
            if act == "archive":
                at = now_iso()
                for i in hit:
                    board["archived"].setdefault(i, at)
            else:
                for i in hit:
                    board["archived"].pop(i, None)
            _save(path, board, existing)
    except TimeoutError:
        return False, "the findings board is busy (another change is being saved): try again"
    except OSError as e:
        return False, f"could not save the findings board: {e}"
    n = len(hit)
    what = "1 finding" if n == 1 else f"{n} findings"
    return True, (f"Archived {what}." if act == "archive" else f"Restored {what}.")


# --- rendering ---------------------------------------------------------------------------

def _stamp(ms):
    """When it was noted, to the minute in this computer's time: fixed, so the poll's
    signature does not change with the clock."""
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000)) if ms else ""


def _ev(label, text):
    return f"<p class='fev'><span class='muted'>{label}</span> {text}</p>"


def _bug(f):
    """A bug's way to see it in the app: where, the steps, expected and actual, the code
    path, what checking it found. "" for a finding that says none of it."""
    out = _ev("in the app", f"<code>{esc(f['ui'])}</code>") if f.get("ui") else ""
    if f.get("steps"):
        out += "<ol class='fsteps'>" + "".join(f"<li>{esc(s)}</li>" for s in f["steps"]) + "</ol>"
    if f.get("expected"):
        out += _ev("expected", esc(f["expected"]))
    if f.get("actual"):
        out += _ev("actual", esc(f["actual"]))
    if f.get("trace"):
        out += "<ul class='ftrace'>" + "".join(
            f"<li><code>{esc(_at(t['path'], t['line']))}</code>"
            + (f" <code>{esc(t['code'])}</code>" if t["code"] else "") + "</li>" for t in f["trace"]) + "</ul>"
    if f.get("verdict"):
        out += _ev("checked", esc(f["verdict"]))
    return out


def _item_list(f, archived):
    """A rolling finding's items, each with its own Not this one (the board's form posts
    it to archive, as the row's Archive does)."""
    items = f.get("items") or []
    if not items:
        return ""
    lis, last = [], None
    for i in items[:ITEMS_SHOWN]:
        sec = i["section"] or ("other" if any(x["section"] for x in items) else "")
        if sec and sec != last:
            n = sum(1 for x in items if (x["section"] or "other") == sec)
            lis.append(f"<li class='fsec'><b>{esc(sec)}</b> <span class='muted'>{n}</span></li>")
        last = sec
        where = _at(i["path"], i["line"])
        li = (f"<code>{esc(where)}</code>" if where else "") + (f" <span>{esc(i['note'])}</span>" if i["note"] else "")
        if len(i["locations"]) > 1:
            li += " " + "".join(f"<code>{esc(x)}</code>" for x in i["locations"][1:])
        if i["evidence"]:
            li += f" <code>{esc(i['evidence'])}</code>"
        if not archived:
            li += (f" <button type='submit' class='sm' name='id' value='{esc(i['id'])}' "
                   "title='not this one: take it off the list'>Not this one</button>")
        lis.append(f"<li>{li}</li>")
    if len(items) > ITEMS_SHOWN:
        lis.append(f"<li class='muted'>{len(items) - ITEMS_SHOWN} more</li>")
    return "<ul class='fitems'>" + "".join(lis) + "</ul>"


def _row(f, archived=False, new=False):
    fid = esc(f["id"])
    tags = ""
    if f["cat"]:
        tags += f"<span class='tag'>{esc(f['cat'])}</span>"
    if f["sev"]:
        tags += f"<span class='pill {SEVERITY.get(f['sev'], 't-off')}'>{esc(f['sev'])}</span>"
    if new:
        tags += "<span class='pill t-acc' title='not placed yet: it came after you last reordered'>new</span>"
    if f["key"]:
        src = (f"<a href='/runs/ticket/{quote(f['key'])}'>{esc(f['key'])}</a>"
               + (f" &middot; {esc(f['stage'])}" if f["stage"] else ""))
    else:
        proj = os.path.basename(f["project"].rstrip("/\\"))
        src = "findings agent" + (f" &middot; <span title='{esc(f['project'])}'>{esc(proj)}</span>" if proj else "")
    where = (f"<code>{esc(f['path'])}" + (f":{f['line']}" if f["line"] else "") + "</code>") if f["path"] else ""
    when = _stamp(f["ms"])
    meta = "".join(x for x in (where, f"<span>{src}</span>",
                               f"<span title='noted at'>{when}</span>" if when else "") if x)
    if archived:
        lead = "<span></span><span></span>"
        acts = (f"<button type='submit' class='sm' name='id' value='{fid}' formaction='/runs/findings/restore' "
                "title='back on the board'>Restore</button>")
    else:
        lead = ("<span class='grip' title='drag to reorder'>&#8942;&#8942;</span>"
                f"<input type='checkbox' class='fsel' name='sel' value='{fid}' aria-label='select this finding'>")
        acts = (f"<button type='submit' class='sm' name='id' value='{fid}' "
                "title='off the board, into Archived below'>Archive</button>")
    # the whole finding, opened in place (the poll keeps it open: data-k)
    body = f.get("body") or ""
    full = (f"<p class='fbx'>{esc(body)}</p>" if body and body != f["title"] else "") + (
        f"<p class='fev'><span class='muted'>evidence</span> <code>{esc(f['evidence'])}</code></p>"
        if f.get("evidence") else "") + _bug(f) + _item_list(f, archived)
    return (f"<div class='fd' data-sort-key='{fid}' data-src='{esc(f['src'])}' data-cat='{esc(f['cat'])}' "
            f"data-md='{esc(f['md'])}'>{lead}<div class='fmain'><details class='fx' data-k='fx-{fid}'>"
            f"<summary class='ft' title='show the whole finding'>{tags}"
            f"<span class='tl'>{esc(f['title'])}</span></summary>{full}</details>"
            f"<div class='fm'>{meta}</div></div>"
            f"<div class='ra'><button type='button' class='sm' data-fcopy='1' title='copy it as a markdown line'>Copy"
            f"</button>{acts}</div></div>")


def findings_region(runs):
    """The findings panel's HTML (the queue page's r-findings region): one form, so the
    poll leaves it alone while a finding is ticked or a filter is set (hSwap)."""
    path = board_path(runs)
    board = read_board(path) if path else {"version": 1, "order": [], "archived": {}}
    items = []
    for f in collect(runs):   # a rolling finding without the items archived one by one; none left: not shown
        if f.get("children"):
            left = [i for i in f["items"] if i["id"] not in board["archived"]]
            if not left:
                continue
            if len(left) < len(f["items"]):
                f = rolled(f, left)
        items.append(f)
    open_, archived, fresh, _ = arrange(items, board)
    mark = fresh if board["order"] else set()
    keys = list(dict.fromkeys(f["key"] for f in items if f["key"]))
    cats = sorted({f["cat"] for f in items if f["cat"]})
    has_agent = any(f["src"] == "agent" for f in items)
    head = (f"<div class='hd' id='findings'><h2>Findings <span class='muted'>{len(open_)} open"
            + (f" &middot; {len(archived)} archived" if archived else "") + "</span></h2>"
            "<span class='muted fhint'>what the queue&#39;s agents noticed outside their tickets</span></div>")
    if not items:
        return (f"<form method='post' action='/runs/findings/archive' id='fboard'>{head}"
                "<div class='empty'>No findings yet. A stage agent that notices a problem outside its ticket "
                "notes it here (and in the ticket&#39;s findings note).</div></form>")
    srcs = "<option value=''>every source</option>" + (
        "<option value='agent'>findings agent</option>" if has_agent else "") + "".join(
        f"<option value='{esc(k)}'>{esc(k)}</option>" for k in keys)
    catsel = ("<select data-ff='cat' aria-label='category'><option value=''>every category</option>"
              + "".join(f"<option value='{esc(c)}'>{esc(c)}</option>" for c in cats) + "</select>") if cats else ""
    bar = ("<div class='fbar'><label class='fall' title='tick every finding shown'><input type='checkbox' "
           "data-fall='1' aria-label='select every finding shown'></label>"
           "<input type='search' data-ff='q' placeholder='Filter findings' aria-label='filter findings'>"
           f"<select data-ff='src' aria-label='source'>{srcs}</select>{catsel}"
           "<span class='fgo'><span class='muted' data-fn='1'></span>"
           "<button type='button' class='sm' data-fsel='copy' disabled>Copy selected</button>"
           "<button type='button' class='sm' data-fsel='ticket' disabled title='fill the Add form below with a "
           "ticket of your own that lists them'>New ticket from selected</button>"
           "<button type='submit' class='sm' data-fsel='archive' disabled>Archive selected</button></span></div>")
    shown = open_[:OPEN_CAP]
    rows = "".join(_row(f, new=f["id"] in mark) for f in shown)
    more = (f"<div class='empty'>{len(open_) - len(shown)} more not shown: archive some to see them.</div>"
            if len(open_) > len(shown) else "")
    body = (f"<div class='fds' data-sort-url='/runs/findings/move'>{rows}</div>{more}" if shown else
            "<div class='empty'>Nothing open: every finding is archived.</div>")
    arch = ""
    if archived:
        a_rows = "".join(_row(f, archived=True) for f in archived[:ARCHIVED_CAP])
        a_more = (f"<div class='empty'>{len(archived) - ARCHIVED_CAP} older not shown.</div>"
                  if len(archived) > ARCHIVED_CAP else "")
        arch = (f"<details class='farch' data-k='findings-archived'><summary>Archived "
                f"<span class='muted'>{len(archived)}</span></summary><div class='fds'>{a_rows}</div>{a_more}</details>")
    return (f"<form method='post' action='/runs/findings/archive' id='fboard'>"
            "<input type='hidden' name='back' value='queue'>"
            f"{head}{bar}{body}{arch}<div class='fnone empty' hidden>No finding matches the filter.</div></form>")


FINDINGS_CSS = """
/* the queue page's findings board */
#fboard { margin: 0; }
#fboard > .hd { display: flex; align-items: center; justify-content: space-between; gap: 8px 12px; flex-wrap: wrap;
  padding: 14px 18px; border-bottom: 1px solid var(--line); scroll-margin-top: 68px; }
.fhint { font-size: 12.5px; }
.fbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 18px; border-bottom: 1px solid var(--line); }
.fbar input[type=search] { flex: 1 1 180px; width: auto; height: 30px; }
.fbar select { height: 30px; font-size: 13px; }
.fbar .fall { display: inline-flex; align-items: center; padding: 0 2px; }
.fbar .fgo { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-left: auto; font-size: 12.5px; }
.fds { display: flex; flex-direction: column; }
.fd { display: grid; grid-template-columns: 14px 16px minmax(0, 1fr) auto; gap: 4px 10px; align-items: center;
  padding: 8px 18px; border-top: 1px solid var(--line); min-width: 0; }
.fds > .fd:first-child { border-top: 0; }
.fd:hover { background: var(--hover); }
.fd[hidden] { display: none; }
.fd .grip { cursor: grab; color: var(--faint); user-select: none; font-size: 12px; letter-spacing: -4px; line-height: 1; }
.fd input[type=checkbox], .fbar input[type=checkbox] { accent-color: var(--accent); margin: 0; }
.fd .fmain { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.fd .ft { display: flex; align-items: center; gap: 6px; min-width: 0; }
.fd .ft .tl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13.5px; min-width: 0; }
.fd .fx > summary { cursor: pointer; list-style: none; }
.fd .fx > summary::-webkit-details-marker { display: none; }
.fd .fx > summary::after { content: "\25B8"; color: var(--faint); font-size: 11px; flex: none; }
.fd .fx[open] > summary::after { content: "\25BE"; }
.fd .fx[open] .tl { white-space: normal; overflow-wrap: anywhere; }
.fd .fx .fbx, .fd .fx .fev { margin: 6px 0 2px; font-size: 13px; white-space: pre-wrap; overflow-wrap: anywhere; }
.fd .fx .fev code { font: 12px var(--mono); }
.fd .fx .fsteps, .fd .fx .ftrace, .fd .fx .fitems { margin: 6px 0 2px; padding-left: 20px; font-size: 13px; }
.fd .fx .ftrace, .fd .fx .fitems { list-style: none; padding-left: 0; }
.fd .fx li { margin: 3px 0; overflow-wrap: anywhere; }
.fd .fx .fitems li.fsec { margin-top: 6px; }
.fd .fx .fitems li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
.fd .fx .ftrace code, .fd .fx .fitems code { font: 12px var(--mono); color: var(--ink-2); overflow-wrap: anywhere; }
.fd .fm { display: flex; flex-wrap: wrap; gap: 2px 14px; font-size: 12px; color: var(--muted); min-width: 0; }
.fd .fm code { font: 11.5px var(--mono); color: var(--ink-2); overflow-wrap: anywhere; }
.fd .pill { height: 18px; padding: 0 7px; font-size: 11px; }
details.farch { border-top: 1px solid var(--line); }
details.farch > summary { padding: 10px 18px; font: 600 13px var(--sans); }
details.farch .fd { opacity: .75; }
@media (max-width: 560px) {
  .fd { grid-template-columns: 14px 16px minmax(0, 1fr); padding: 8px 14px; }
  .fd .ra { grid-column: 3; justify-content: flex-start; }
  .fbar, #fboard > .hd { padding-left: 14px; padding-right: 14px; }
  .fbar .fgo { margin-left: 0; }
}
"""

# Client side, all on the page's own data: filters, ticking, copy, and a new ticket of
# your own from the ticked ones (the Add form is filled in, not sent). Listeners sit on
# the document, so they keep working after the poll replaces the panel.
FINDINGS_JS = """
(function () {
  function board() { return document.getElementById('fboard'); }
  function rows(b) { return b.querySelectorAll('.fds > .fd'); }
  function picked(b) {
    var out = [];
    b.querySelectorAll('input.fsel:checked').forEach(function (c) { out.push(c.closest('.fd')); });
    return out;
  }
  function sync() {
    var b = board();
    if (!b) return;
    var n = picked(b).length;
    b.querySelectorAll('[data-fsel]').forEach(function (x) { x.disabled = !n; });
    var t = b.querySelector('[data-fn]');
    if (t) t.textContent = n ? n + ' selected' : '';
  }
  function filter() {
    var b = board();
    if (!b) return;
    function v(k) { var e = b.querySelector('[data-ff=' + k + ']'); return e ? e.value : ''; }
    var q = v('q').toLowerCase().trim(), src = v('src'), cat = v('cat'), shown = 0, any = 0;
    rows(b).forEach(function (r) {
      var ok = (!src || r.getAttribute('data-src') === src) && (!cat || r.getAttribute('data-cat') === cat) &&
        (!q || (r.getAttribute('data-md') || '').toLowerCase().indexOf(q) >= 0);
      r.hidden = !ok;
      any++;
      if (ok) shown++;
    });
    var none = b.querySelector('.fnone');
    if (none) none.hidden = !any || shown > 0;
  }
  function copy(text, btn) {
    function done() {
      if (!btn) return;
      var was = btn.textContent;
      btn.textContent = 'Copied';
      setTimeout(function () { btn.textContent = was; }, 1200);
    }
    function old() {   // http on a LAN host is not a secure context: no navigator.clipboard
      var t = document.createElement('textarea');
      t.value = text;
      t.setAttribute('readonly', '');
      t.style.position = 'fixed'; t.style.top = '-1000px'; t.style.opacity = '0';
      document.body.appendChild(t);
      t.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(t);
      if (ok) done(); else window.prompt('Copy the findings:', text);
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, old);
    else old();
  }
  function md(list) { return list.map(function (r) { return r.getAttribute('data-md'); }).join('\\n'); }
  function newTicket(list) {
    var add = document.getElementById('addform'), kind = add && add.querySelector('input[name=kind][value=local]');
    if (!kind || kind.disabled) {
      window.alert('A ticket of your own needs an active vault to be saved in (harness vault use NAME).');
      return;
    }
    kind.checked = true;
    kind.dispatchEvent(new Event('change', {bubbles: true}));
    var title = add.querySelector('input[name=title]'), desc = add.querySelector('textarea[name=desc]');
    var one = list.length === 1 ? (list[0].querySelector('.tl') || {}).textContent || '' : '';
    var max = +(title.getAttribute('maxlength') || 120);
    title.value = one ? ('Fix: ' + one).slice(0, max) : 'Fix ' + list.length + ' findings';
    var text = 'Fix ' + (list.length === 1 ? 'this finding' : 'these findings') + ':\\n\\n', dmax = +(desc.getAttribute('maxlength') || 20000), left = 0;
    list.forEach(function (r) {
      var line = r.getAttribute('data-md') + '\\n';
      if (text.length + line.length < dmax - 80) text += line; else left++;
    });
    if (left) text += '\\n(' + left + ' more selected did not fit here)\\n';
    desc.value = text;
    title.dispatchEvent(new Event('input', {bubbles: true}));
    desc.dispatchEvent(new Event('input', {bubbles: true}));
    var p = document.getElementById('add');
    p.scrollIntoView({behavior: 'smooth', block: 'start'});
    title.focus({preventScroll: true});
    p.classList.add('hl');
    setTimeout(function () { p.classList.remove('hl'); }, 1500);
  }
  document.addEventListener('change', function (e) {
    var t = e.target, b = board();
    if (!b || !b.contains(t)) return;
    if (t.hasAttribute('data-fall')) {
      rows(b).forEach(function (r) { var c = r.querySelector('input.fsel'); if (c && !r.hidden) c.checked = t.checked; });
    }
    if (t.hasAttribute('data-ff')) filter();
    sync();
  });
  document.addEventListener('input', function (e) {
    var b = board();
    if (b && b.contains(e.target) && e.target.hasAttribute('data-ff')) filter();
  });
  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('button'), b = board();
    if (!t || !b || !b.contains(t)) return;
    if (t.hasAttribute('data-fcopy')) { copy(t.closest('.fd').getAttribute('data-md') + '\\n', t); return; }
    var w = t.getAttribute('data-fsel'), list = picked(b);
    if (w === 'copy' && list.length) copy(md(list) + '\\n', t);
    else if (w === 'ticket' && list.length) newTicket(list);
  });
})();
"""
