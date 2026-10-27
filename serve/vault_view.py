"""harness serve's Vault pages: what the active vault holds, its pages rendered,
search, history, and what ingest has done and is doing.

Read-only: git runs without a fetch, without optional locks and without hooks, and
nothing here writes into the vault. Markdown is rendered on the server by the
vendored mistune (vendor/README.md) with raw HTML escaped; Obsidian's [[wikilinks]],
![[embeds]] and > [!callouts] are handled here."""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))
import mistune  # noqa: E402
import runs as runs_view  # noqa: E402

esc = runs_view.esc
WALK_CAP = 20000           # files looked at when listing a vault
SEARCH_CAP = 300           # matching lines shown
FILE_CAP = 1_000_000       # bytes of one file shown
DIFF_CAP = 400_000         # bytes of one commit's diff shown
RUN_STALE = 120            # s without an ingest heartbeat before a run counts as gone
SKIP_DIRS = {".git", "node_modules", ".trash"}
# Files that look like secrets are listed but never shown (the queue's SECRET_PATH, plus a few more).
SECRET = re.compile(r"(^|/)(\.env(?!\.(example|sample|template)$)(\..*)?|.*\.pem|.*\.key|.*\.p12|"
                    r"id_(rsa|ed25519|ecdsa|dsa)(\.pub)?|\.netrc|credentials(\.json)?)$", re.I)
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
          ".webp": "image/webp"}
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
COMMENT_LINE = re.compile(r"^[ \t]*<!--.*?-->[ \t]*$", re.M)
WIKI_RE = r"!?\[\[([^\[\]\n]{1,300})\]\]"
CALLOUT_RE = re.compile(r"<blockquote>\n<p>\[!([A-Za-z-]{1,20})\]([+-]?)[ \t]*([^\n<]*)")

STYLE = """
.vgrid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 340px); gap: 18px; align-items: start; }
@media (max-width: 1020px) { .vgrid { grid-template-columns: minmax(0, 1fr); } }
.vstats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
.vstats a, .vstats div { display: flex; flex-direction: column; gap: 2px; padding: 10px 12px; border: 1px solid var(--line);
  border-radius: 9px; background: var(--sunk); color: var(--ink); }
.vstats a:hover { text-decoration: none; border-color: var(--line-2); }
.vstats b { font-size: 20px; font-weight: 680; } .vstats span { font-size: 12.5px; color: var(--muted); }
.vlist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.vlist li { display: flex; gap: 6px 12px; align-items: baseline; justify-content: space-between; padding: 7px 2px;
  border-top: 1px solid var(--line); min-width: 0; flex-wrap: wrap; }
.vlist li:first-child { border-top: 0; }
.vlist .nm { min-width: 0; overflow-wrap: anywhere; } .vlist .nm small { color: var(--muted); margin-left: 6px; }
.vlist .meta { color: var(--muted); font-size: 12.5px; white-space: nowrap; }
.vlist .dir::before { content: "\\25B8 "; color: var(--faint); }
.vsearch { display: flex; gap: 8px; } .vsearch input { flex: 1; min-width: 0; }
.md { font-size: 15px; line-height: 1.65; overflow-wrap: anywhere; min-width: 0; }
.md h1 { font-size: 24px; margin: 0 0 12px; } .md h2 { font-size: 19px; margin: 22px 0 8px; }
.rv .md h2 { font-size: 19px; text-transform: none; letter-spacing: 0; display: block; }
.md h3 { font-size: 16px; margin: 18px 0 6px; } .md p, .md ul, .md ol { margin: 0 0 10px; }
.md pre { background: var(--sunk); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
  overflow: auto; font: 12.5px/1.5 var(--mono); }
.md code { background: var(--sunk); padding: 1px 4px; border-radius: 4px; } .md pre code { background: none; padding: 0; }
.md table { border-collapse: collapse; margin: 0 0 12px; display: block; overflow-x: auto; }
.md th, .md td { border: 1px solid var(--line); padding: 5px 9px; text-align: left; vertical-align: top; }
.md th { background: var(--raised); font: 600 14px/1.5 var(--sans); text-transform: none; letter-spacing: 0;
  color: var(--ink); } .md img { max-width: 100%; height: auto; }
.md blockquote { margin: 0 0 12px; padding: 4px 14px; border-left: 3px solid var(--line-2); color: var(--ink-2); }
.md blockquote.callout { border-left-color: var(--accent); background: var(--accent-soft); border-radius: 0 8px 8px 0; }
.md blockquote.callout-warning, .md blockquote.callout-caution { border-left-color: var(--wait); background: var(--wait-soft); }
.md blockquote.callout-danger, .md blockquote.callout-bug, .md blockquote.callout-failure {
  border-left-color: var(--bad); background: var(--bad-soft); }
.md .callout-t { display: block; font-weight: 650; }
.md a.missing { color: var(--faint); text-decoration: underline dotted; cursor: help; }
.md .task-list-item { list-style: none; } .md .task-list-item input { margin: 0 6px 0 0; }
.vhist li .s { overflow-wrap: anywhere; }
pre.vraw { white-space: pre-wrap; overflow-wrap: anywhere; font: 12.5px/1.55 var(--mono); margin: 0; }
.hits { list-style: none; margin: 0; padding: 0; } .hits > li { padding: 8px 0; border-top: 1px solid var(--line); }
.hits > li:first-child { border-top: 0; }
.hits pre { margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.5 var(--mono); color: var(--ink-2); }
.hits mark { background: var(--wait-soft); color: var(--ink); border-radius: 3px; }
.ing { display: flex; flex-direction: column; gap: 8px; }
.inl { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.ingf details { margin-top: 8px; } .ingf summary { cursor: pointer; color: var(--muted); font: 13px/1.4 var(--sans); }
.ingf .grid2 { margin-top: 8px; } .ingf label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
pre.out { white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.5 var(--mono); max-height: 320px; overflow: auto;
  background: var(--sunk); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin: 6px 0 0; }
.bar2 { height: 6px; border-radius: 3px; background: var(--sunk); overflow: hidden; display: flex; }
.bar2 i { display: block; height: 100%; } .bar2 .ok { background: var(--ok); } .bar2 .bad { background: var(--bad); }
.bar2 .pend { background: var(--wait); }
.probs { margin: 4px 0 0; padding-left: 18px; color: var(--ink-2); font-size: 13px; }
"""


# --- git -------------------------------------------------------------------

def git(vault, *args, timeout=8, binary=False):
    """Read-only git in the vault: stdout, or None when git fails."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    cmd = ["git", "--no-pager", "-C", vault, "-c", "core.fsmonitor=false", "-c", "log.showSignature=false",
           "-c", "color.ui=false", "-c", "core.quotepath=false", *args]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode not in (0, 1) or (r.returncode == 1 and args[:1] != ("grep",)):
        return None
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def remote_shown(url):
    """A remote URL without any user or token in it."""
    if "://" in url:
        u = urlsplit(url)
        host = u.netloc.rpartition("@")[2]
        return f"{u.scheme}://{host}{u.path}"
    return url.rpartition("@")[2] if re.match(r"^[^/]*@[^/]*:", url) else url


def repo_facts(vault):
    """Where the vault syncs and how it stands, from local refs only (no fetch)."""
    out = {"branch": (git(vault, "symbolic-ref", "-q", "--short", "HEAD") or "").strip(),
           "head": (git(vault, "log", "-1", "--format=%h%x09%ar%x09%s") or "").strip().split("\t"),
           "remote": remote_shown((git(vault, "remote", "get-url", "origin") or "").strip())}
    ab = (git(vault, "rev-list", "--left-right", "--count", "HEAD...@{upstream}") or "").split()
    out["ahead"], out["behind"] = (int(ab[0]), int(ab[1])) if len(ab) == 2 else (None, None)
    st = git(vault, "status", "--porcelain=v1", "-z", "--untracked-files=normal", timeout=10)
    out["dirty"] = [e[3:] for e in (st or "").split("\0") if len(e) > 3][:50] if st is not None else None
    return out


# --- files -----------------------------------------------------------------

def resolve(vault, rel):
    """(absolute path, clean relative path) inside the vault, or None: no .., no .git,
    nothing a symlink takes outside it."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if any(p in (".", "..", ".git") or "\0" in p for p in parts):
        return None
    root = os.path.realpath(vault)
    full = os.path.realpath(os.path.join(root, *parts))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full, "/".join(parts)


_PAGES = {}


def pages(vault):
    """{relative path: mtime} of every file in the vault (cached 10 s)."""
    hit = _PAGES.get(vault)
    if hit and time.monotonic() - hit[0] < 10:
        return hit[1]
    found = {}
    for top, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel = os.path.relpath(top, vault).replace(os.sep, "/")
        for f in files:
            p = f if rel == "." else f"{rel}/{f}"
            try:
                found[p] = os.stat(os.path.join(top, f)).st_mtime
            except OSError:
                continue
            if len(found) >= WALK_CAP:
                break
        if len(found) >= WALK_CAP:
            break
    _PAGES[vault] = (time.monotonic(), found)
    if len(_PAGES) > 8:
        _PAGES.pop(next(iter(_PAGES)))
    return found


def link_index(vault):
    """Obsidian's lookup for [[name]]: the full path (without .md) or the file name,
    the shortest path winning."""
    full, base = {}, {}
    for p in sorted(pages(vault), key=lambda p: (p.count("/"), p)):
        key = p[:-3] if p.lower().endswith(".md") else p
        full.setdefault(key.lower(), p)
        base.setdefault(key.rsplit("/", 1)[-1].lower(), p)
    return full, base


def read_text(path, cap=FILE_CAP):
    try:
        with open(path, "rb") as f:
            data = f.read(cap + 1)
    except OSError:
        return None, False
    if b"\0" in data[:8000]:
        return None, False
    return data[:cap].decode("utf-8", "replace"), len(data) > cap


def frontmatter(text):
    """(fields, body): the simple key: value lines of a leading --- block."""
    m = FRONT_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k and not k.startswith((" ", "\t", "-", "#")):
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, text[m.end():]


def title_of(vault, rel):
    """A page's title: front matter title, else its first # heading, else its name."""
    text, _ = read_text(os.path.join(vault, rel), 4000)
    name = rel.rsplit("/", 1)[-1]
    name = name[:-3] if name.lower().endswith(".md") else name
    if not text:
        return name, {}
    meta, body = frontmatter(text)
    if meta.get("title"):
        return meta["title"], meta
    m = re.search(r"^#[ \t]+(.+)$", body, re.M)
    return (m.group(1).strip() if m else name), meta


# --- markdown --------------------------------------------------------------

def href_of(rel):
    return "/vault/browse/" + quote(rel)


class _Renderer(mistune.HTMLRenderer):
    """Links inside the vault open in the vault browser; [[wikilinks]] resolve as Obsidian does."""

    def __init__(self, vault, rel):
        super().__init__(escape=True)
        self.vault, self.dir = vault, rel.rsplit("/", 1)[0] if "/" in rel else ""
        self._index = None

    def index(self):
        if self._index is None:
            self._index = link_index(self.vault)
        return self._index

    def local(self, url):
        """A relative link's target in the vault, or None."""
        if not url or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:|^//|^#", url):
            return None
        path, _, frag = url.partition("#")
        path = path.split("?", 1)[0]
        from urllib.parse import unquote
        joined = os.path.normpath(os.path.join(self.dir, unquote(path))).replace(os.sep, "/").lstrip("/")
        if joined.startswith(".."):
            return None
        return joined, frag

    def link(self, text, url, title=None):
        got = self.local(url)
        if got:
            target = got[0]
            if target not in pages(self.vault) and target + ".md" in pages(self.vault):
                target += ".md"
            url = href_of(target) + (f"#{got[1]}" if got[1] else "")
            return f'<a href="{esc(url)}">{text}</a>'
        s = super().link(text, url, title)
        return s.replace("<a ", '<a rel="noopener noreferrer" target="_blank" ', 1) if "://" in url else s

    def image(self, text, url, title=None):
        got = self.local(url)
        if got:
            return f'<img src="{esc("/vault/file/" + quote(got[0]))}" alt="{esc(text)}">'
        return f'<span class="muted">[image: {esc(text or url)}]</span>'

    def wikilink(self, raw):
        embed = raw.startswith("!")
        inner = raw[3:-2] if embed else raw[2:-2]
        target, _, alias = inner.partition("|")
        target, _, frag = target.partition("#")
        label = alias.strip() or (target.strip() + (f" › {frag.strip()}" if frag.strip() and not target.strip()
                                                     else ""))
        label = label or frag.strip()
        full, base = self.index()
        t = target.strip().lower()
        hit = full.get(t) or base.get(t) or full.get(t[:-3] if t.endswith(".md") else "") if t else None
        if not t:
            return f'<a href="#{esc(frag.strip())}">{esc(label)}</a>'
        if not hit:
            return f'<a class="missing" title="no page named {esc(target.strip())} in the vault">{esc(label)}</a>'
        if embed and os.path.splitext(hit)[1].lower() in IMAGES:
            return f'<img src="{esc("/vault/file/" + quote(hit))}" alt="{esc(label)}">'
        anchor = "#" + quote(frag.strip().lower().replace(" ", "-")) if frag.strip() else ""
        return f'<a href="{esc(href_of(hit) + anchor)}">{"&#8618; " if embed else ""}{esc(label)}</a>'


def _wikilinks(md):
    def parse(inline, m, state):
        state.append_token({"type": "wikilink", "attrs": {"raw": m.group(0)}})
        return m.end()
    md.inline.register("wikilink", WIKI_RE, parse, before="link")
    md.renderer.register("wikilink", lambda renderer, raw: renderer.wikilink(raw))


def render_markdown(vault, rel, text):
    """A page's body as HTML: raw HTML escaped, generated markers dropped, callouts styled."""
    body = COMMENT_LINE.sub("", text)
    md = mistune.create_markdown(renderer=_Renderer(vault, rel),
                                 plugins=["table", "strikethrough", "footnotes", "task_lists", "url", _wikilinks])
    out = md(body)

    def callout(m):
        kind, title = m.group(1).lower(), m.group(3).strip() or m.group(1).capitalize()
        return (f"<blockquote class='callout callout-{esc(kind)}'>\n<p><span class='callout-t'>{title}</span>")
    return CALLOUT_RE.sub(callout, out)


# --- ingest ----------------------------------------------------------------

def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return True       # the heartbeat age decides on Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def ingest_run(vault):
    """The ingest running on this vault now (its run file), with alive: False when
    the process is gone or its heartbeat stopped; None when none ran."""
    try:
        with open(os.path.join(vault, ".git", "harness-ingest.run.json"), encoding="utf-8") as f:
            run = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(run, dict):
        return None
    beat = runs_view._iso_ms(str(run.get("beat") or run.get("started") or ""))
    age = (time.time() * 1000 - beat) / 1000 if beat else None
    run["alive"] = bool(_pid_alive(run.get("pid")) and age is not None and age < RUN_STALE)
    run["age"] = age
    return run


def ingest_summary(vault):
    """What the manifest says ingest has done, per source: codebases by name, .raw
    material as one."""
    try:
        with open(os.path.join(vault, ".harness", "ingest.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        man = {}
    repos = man.get("repos") if isinstance(man.get("repos"), dict) else {}
    units = man.get("units") if isinstance(man.get("units"), dict) else {}
    srcs = {name: {"name": name, "path": str(path), "exists": os.path.isdir(str(path)), "ok": 0, "failed": 0,
                   "pages": set(), "at": ""} for name, path in repos.items()}
    raw = {"name": ".raw", "path": ".raw", "exists": True, "ok": 0, "failed": 0, "pages": set(), "at": ""}
    failed = []
    for uid, e in units.items():
        if not isinstance(e, dict):
            continue
        if uid.startswith("code:"):
            name = uid[5:].split("/", 1)[0]
            s = srcs.setdefault(name, {"name": name, "path": "", "exists": False, "ok": 0, "failed": 0,
                                       "pages": set(), "at": ""})
        else:
            s = raw
        ok = e.get("status") == "ok"
        s["ok" if ok else "failed"] += 1
        if e.get("page"):
            s["pages"].add(e["page"])
        s["at"] = max(s["at"], str(e.get("at") or ""))
        if not ok:
            failed.append({"id": uid, "page": e.get("page") or "", "attempts": e.get("attempts") or 0,
                           "at": e.get("at") or "", "problems": [str(p)[:300] for p in (e.get("problems") or [])][:3]})
    out = sorted(srcs.values(), key=lambda s: s["name"].lower())
    if raw["ok"] or raw["failed"]:
        out.append(raw)
    for s in out:
        s["pages"] = len(s["pages"])
    failed.sort(key=lambda f: f["at"], reverse=True)
    last = ""
    log, _ = read_text(os.path.join(vault, "wiki", "log.md"), 400_000)
    for line in reversed((log or "").splitlines()):
        if " ingest: " in line:
            last = line.lstrip("- ").strip()
            break
    return {"sources": out, "failed": failed[:200], "nfailed": len(failed), "last": last, "has": bool(man)}


# --- pages -----------------------------------------------------------------

def _crumbs(rel, last_link=False):
    out = ["<a href='/vault'>Vault</a>"]
    parts = [p for p in rel.split("/") if p]
    for i, p in enumerate(parts):
        out.append("<span>/</span>")
        if i < len(parts) - 1 or last_link:
            out.append(f"<a href='{esc(href_of('/'.join(parts[:i + 1])))}'>{esc(p)}</a>")
        else:
            out.append(f"<b>{esc(p)}</b>")
    return f"<div class='crumbs'>{''.join(out)}</div>"


def _obsidian(name, rel=""):
    """An Open in Obsidian link (the vault folder's name is its Obsidian name)."""
    if not name:
        return ""
    q = f"vault={quote(name, safe='')}" + (f"&file={quote(rel, safe='')}" if rel else "")
    return f"<a class='btn ghost' href='obsidian://open?{q}' title='needs Obsidian with this vault open'>Open in Obsidian</a>"


def _search_box(q=""):
    return ("<form method='get' action='/vault/search' class='vsearch' role='search'>"
            f"<input type='search' name='q' value='{esc(q)}' placeholder='Search the vault' "
            "aria-label='search the vault' maxlength='200'><button type='submit'>Search</button></form>")


def _ago_iso(text):
    ms = runs_view._iso_ms(text) if text else 0
    return runs_view._ago(ms) if ms else ""


def _ingest_controls(run, s, log):
    """Start, retry and add forms when nothing runs, Stop while something does, and the
    last ingest started from here (its output's tail)."""
    btn = runs_view._btn
    parts = []
    proc = _started["proc"]
    if (run and run.get("alive")) or (proc is not None and proc.poll() is None):
        parts.append("<div class='inl'>" + btn("/vault/ingest/stop", "Stop", "ghost", (),
                     confirm="Stop this ingest? Sections already written are kept; the rest wait for the next one.")
                     + "</div>")
    else:
        parts.append(
            "<form method='post' action='/vault/ingest/start' class='ingf'>"
            "<div class='inl'><button type='submit' name='what' value='all' class='primary'>Ingest what changed</button>"
            + ("<button type='submit' name='what' value='retry'>Retry failed sections too</button>" if s["nfailed"] else "")
            + "</div><details><summary>Add a codebase, or limit the run</summary><div class='grid2'>"
            "<label>Codebase folder<input type='text' name='dir' placeholder='e.g. C:/src/api or ~/src/api' "
            "spellcheck='false' autocapitalize='off'></label>"
            "<label>At most this many sections<input type='text' name='max' inputmode='numeric' placeholder='all'></label>"
            "<label>Model<input type='text' name='model' placeholder='the default model' spellcheck='false'></label>"
            "</div><p class='muted'>A folder is remembered, so later runs check it too. Try a few sections first "
            "and read the pages.</p></details></form>")
    if log:
        tail = _log_tail(log)
        if tail:
            parts.append(f"<details{' open' if run and run.get('alive') else ''}><summary>Output of the last ingest "
                         f"started here</summary><pre class='out'>{esc(tail)}</pre></details>")
    return "".join(parts)


def _log_tail(path, limit=6000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - limit))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    if n > limit:
        text = text.split("\n", 1)[-1]
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r\n", "\n").strip()


def _ingest_panel(vault, full=False, log=""):
    run = ingest_run(vault)
    s = ingest_summary(vault)
    parts = ["<section class='panel ing' id='ingest'><div class='hd'><h2>Ingest</h2>"
             + ("" if full else "<a href='/vault/ingest'>details and controls</a>") + "</div>"]
    if run and run.get("alive"):
        n = int(run.get("done") or 0) + int(run.get("failed") or 0)
        left = int(run.get("left") or 0)
        parts.append(f"<p><span class='pill t-run dot pulse'>running</span> {esc(str(run.get('step') or 'ingesting'))}"
                     f" &middot; {n} of {n + left} sections &middot; {int(run.get('failed') or 0)} failed"
                     + (f" &middot; with {esc(str(run.get('model')))}" if run.get("model") else "")
                     + (f"<br><span class='muted'>now: <code>{esc(str(run.get('unit')))}</code></span>"
                        if run.get("unit") else "") + "</p>")
    elif full and _started["proc"] is not None and _started["proc"].poll() is None:
        parts.append("<p><span class='pill t-run dot pulse'>starting</span> harness is getting ready (the first "
                     "run on a computer can install tools first); the output is below.</p>")
    elif run:
        parts.append("<p class='muted'>An ingest stopped without finishing (its process is gone); the next "
                     "ingest takes over.</p>")
    else:
        m = re.match(r"^(\S+) ingest: (.*)$", s["last"] or "")
        last = f"last run {_ago_iso(m.group(1))}: {m.group(2)}" if m else s["last"]
        parts.append("<p class='muted'>Not running. " + (f"{esc(last[:1].upper() + last[1:])}" if last else
                                                          "Nothing ingested yet.") + "</p>")
    if full:
        parts.append(_ingest_controls(run, s, log))
    if s["sources"]:
        parts.append("<ul class='vlist'>")
        for src in s["sources"]:
            tot = src["ok"] + src["failed"] or 1
            where = "wiki/sources" if src["name"] == ".raw" else f"wiki/code/{src['name']}"
            note = "" if src["exists"] else " <span class='pill t-bad'>folder not on this host</span>"
            parts.append(
                f"<li><span class='nm'><a href='{esc(href_of(where))}'>{esc(src['name'])}</a>{note}"
                f"<small>{src['pages']} page{'' if src['pages'] == 1 else 's'}</small></span>"
                f"<span class='meta'>{src['ok']} ok" + (f" &middot; <b class='t-bad'>{src['failed']} failed</b>"
                                                           if src["failed"] else "")
                + (f" &middot; {esc(_ago_iso(src['at']))}" if src["at"] else "") + "</span>"
                + (runs_view._btn("/vault/ingest/forget", "Forget", "ghost sm", (("name", src["name"]),),
                                  confirm=f"Forget codebase {src['name']}? Its generated pages under "
                                          f"wiki/code/{src['name']}/ are deleted (pages you wrote stay) and "
                                          "the change is committed and pushed.")
                   if full and src["name"] != ".raw" and not (run and run.get("alive")) else "")
                + f"<div class='bar2' style='flex-basis:100%'><i class='ok' style='width:{100 * src['ok'] / tot:.1f}%'></i>"
                f"<i class='bad' style='width:{100 * src['failed'] / tot:.1f}%'></i></div></li>")
        parts.append("</ul>")
    if full and s["failed"]:
        parts.append(f"<h3>Failed sections <span class='muted'>{s['nfailed']}</span></h3><ul class='vlist'>")
        for f in s["failed"][:100]:
            probs = "".join(f"<li>{esc(p)}</li>" for p in f["problems"])
            parts.append(f"<li><span class='nm'><code>{esc(f['id'])}</code>"
                         + (f" &rarr; <a href='{esc(href_of(f['page']))}'>{esc(f['page'])}</a>" if f["page"] else "")
                         + f"<ul class='probs'>{probs}</ul></span><span class='meta'>{f['attempts']} tries"
                         + (f" &middot; {esc(_ago_iso(f['at']))}" if f["at"] else "") + "</span>"
                         + ("" if run and run.get("alive") else
                            runs_view._btn("/vault/ingest/start", "Retry", "ghost sm", (("unit", f["id"]),)))
                         + "</li>")
        parts.append("</ul>")
    parts.append("</section>")
    return "".join(parts)


def _recent(vault, n=12):
    """The pages changed in the last commits, newest first."""
    out = git(vault, "log", f"-{n * 3}", "--name-only", "--format=%x00%ar", "--", "wiki", ".raw", "pub")
    seen, rows, when = set(), [], ""
    for line in (out or "").splitlines():
        if line.startswith("\0"):
            when = line[1:]
        elif line and line not in seen and not line.endswith(".gitkeep"):
            seen.add(line)
            if line in pages(vault):
                rows.append((line, when))
        if len(rows) >= n:
            break
    return rows


def _history(vault, rel="", n=20):
    args = ["log", f"-{n}", "--format=%h%x09%an%x09%ar%x09%s"]
    if rel:
        args += ["--follow", "--", rel]
    rows = [r.split("\t", 3) for r in (git(vault, *args) or "").splitlines()]
    return [r for r in rows if len(r) == 4]


def _hist_list(rows, rel=""):
    if not rows:
        return "<p class='muted'>No commits yet.</p>"
    q = f"?path={quote(rel, safe='')}" if rel else ""
    return "<ul class='vlist vhist'>" + "".join(
        f"<li><span class='nm s'><a href='/vault/commit/{esc(h)}{esc(q)}'>{esc(s)}</a></span>"
        f"<span class='meta'><code>{esc(h)}</code> &middot; {esc(a)} &middot; {esc(w)}</span></li>"
        for h, a, w, s in rows) + "</ul>"


def overview_regions(vault, name, vaults=()):
    if not vault or not os.path.isdir(vault):
        msg = ("<section class='panel'><h2>No vault</h2><p>harness has no active vault"
               + (f" (<code>{esc(name)}</code> is not cloned here yet: a launch clones it)" if name else "")
               + ". Pick one below, or make one with <code>harness vault new NAME</code> or "
               "<code>harness vault add NAME URL</code>.</p></section>")
        return {"main": msg, "side": _vaults_panel(vaults, name)}
    facts = repo_facts(vault)
    all_pages = pages(vault)
    wiki = [p for p in all_pages if p.startswith("wiki/") and p.endswith(".md")]
    groups = {}
    for p in wiki:
        top = p.split("/")[1] if p.count("/") > 1 else ""
        groups[top] = groups.get(top, 0) + 1
    raw = sum(1 for p in all_pages if p.startswith(".raw/") and not p.endswith(".gitkeep"))
    stats = [f"<a href='{esc(href_of('wiki'))}'><b>{len(wiki)}</b><span>wiki pages</span></a>"]
    for top in sorted(g for g in groups if g):
        stats.append(f"<a href='{esc(href_of('wiki/' + top))}'><b>{groups[top]}</b><span>{esc(top)}</span></a>")
    stats.append(f"<a href='{esc(href_of('.raw'))}'><b>{raw}</b><span>raw files (.raw)</span></a>")
    head = facts["head"]
    sync = []
    if facts["remote"]:
        sync.append(f"<dt>remote</dt><dd>{esc(facts['remote'])}</dd>")
    else:
        sync.append("<dt>remote</dt><dd>none (this vault is only on this host)</dd>")
    if facts["branch"]:
        sync.append(f"<dt>branch</dt><dd>{esc(facts['branch'])}</dd>")
    if facts["ahead"] is not None:
        state = ("in step" if not facts["ahead"] and not facts["behind"] else
                 ", ".join(x for x in (f"{facts['ahead']} to push" if facts["ahead"] else "",
                                       f"{facts['behind']} to pull" if facts["behind"] else "") if x))
        sync.append(f"<dt>with the remote</dt><dd>{esc(state)} <span class='muted'>(as of the last sync)</span></dd>")
    if facts["dirty"] is not None:
        d = facts["dirty"]
        sync.append("<dt>uncommitted</dt><dd>" + ("none" if not d else
                    f"{len(d)} file{'s' if len(d) != 1 else ''}: " + esc(", ".join(d[:6])) + (" &hellip;" if len(d) > 6 else ""))
                    + "</dd>")
    if len(head) == 3:
        sync.append(f"<dt>last commit</dt><dd>{esc(head[2])} <span class='muted'>{esc(head[1])}</span></dd>")
    recent = _recent(vault)
    rec = "<ul class='vlist'>" + "".join(
        f"<li><span class='nm'><a href='{esc(href_of(p))}'>{esc(p)}</a></span><span class='meta'>{esc(w)}</span></li>"
        for p, w in recent) + "</ul>" if recent else "<p class='muted'>Nothing changed yet.</p>"
    main = (f"<section class='panel'><div class='hd'><h2>{esc(name or os.path.basename(vault))}</h2>"
            f"{_obsidian(os.path.basename(vault))}</div>{_search_box()}<div class='vstats'>{''.join(stats)}</div>"
            f"<dl class='facts'><dt>folder</dt><dd>{esc(vault)}</dd>{''.join(sync)}</dl></section>"
            f"<section class='panel'><div class='hd'><h2>Recently changed</h2>"
            f"<a href='{esc(href_of(''))}'>browse all files</a></div>{rec}</section>")
    side = (_ingest_panel(vault) + _vaults_panel(vaults, name)
            + f"<section class='panel'><div class='hd'><h2>History</h2></div>{_hist_list(_history(vault, n=12))}</section>")
    return {"main": main, "side": side}


def _vaults_panel(vaults, active):
    if not vaults:
        return ""
    rows = []
    for v in vaults:
        on = v["name"] == active
        act = ("<span class='pill t-ok'>active</span>" if on else
               runs_view._btn("/vault/use", "Use", "", (("name", v["name"]),),
                              confirm=f"Make {v['name']} the active vault? New launches and the queue use it."))
        rows.append(f"<li><span class='nm'>{esc(v['name'])}<small>{esc(remote_shown(v.get('url') or ''))}</small>"
                    + ("" if v.get("cloned") else " <small>(not cloned yet)</small>") + f"</span>{act}</li>")
    return ("<section class='panel'><div class='hd'><h2>Vaults</h2></div><ul class='vlist'>" + "".join(rows)
            + "</ul><p class='muted'>Add one with <code>harness vault add NAME URL</code> or "
            "<code>harness vault new NAME</code>.</p></section>")


def list_vaults(state_dir):
    """[{name, url, cloned}] from harness's registry (state/vaults.list: name TAB url;
    url "local" for a vault made here, shown as its clone's origin)."""
    out = []
    if not state_dir:
        return out
    try:
        with open(os.path.join(state_dir, "vaults.list"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        name, _, url = line.partition("\t")
        if not name.strip():
            continue
        dir_ = os.path.join(state_dir, "vaults", name)
        if url == "local":
            url = (git(dir_, "remote", "get-url", "origin") or "").strip() or "local, no remote"
        out.append({"name": name, "url": url, "cloned": os.path.isdir(os.path.join(dir_, ".git"))})
    return out


def use_vault(state_dir, name):
    """`harness vault use NAME`: (ok, message)."""
    if not state_dir:
        return False, "serve was started without --state-dir, so it cannot change the vault"
    if name not in {v["name"] for v in list_vaults(state_dir)}:
        return False, f"no vault named {name!r}"
    path = os.path.join(state_dir, "vault.active")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(name + "\n")
        os.replace(tmp, path)
    except OSError as e:
        return False, f"could not write {path}: {e}"
    return True, f"{name} is the active vault; new launches and the queue's next stage use it"


def render_overview(vault, name, vaults=(), extra=None):
    regions = dict(overview_regions(vault, name, vaults), **(extra or {}))
    return [f"<style>{STYLE}</style>", runs_view._live_tag("/vault/live", runs_view._sig(regions)),
            "<div class='ph'><h1>Vault</h1><div class='sub'>Everything the agents know: the wiki ingest writes, "
            "the raw material it reads, and their history.</div></div>",
            f"<div class='vgrid'><div id='r-main' class='stack'>{regions['main']}</div>"
            f"<div id='r-side' class='stack'>{regions['side']}</div></div>"]


def overview_live(vault, name, vaults=(), extra=None):
    regions = dict(overview_regions(vault, name, vaults), **(extra or {}))
    return {"sig": runs_view._sig(regions), "regions": regions}


def render_ingest(vault, extra=None, log="", flash="", bad=False):
    regions = dict({"ing": _ingest_panel(vault, full=True, log=log)}, **(extra or {}))
    return [f"<style>{STYLE}</style>", runs_view._live_tag("/vault/ingest/live", runs_view._sig(regions)),
            "<div class='ph'><div class='crumbs'><a href='/vault'>Vault</a><span>/</span><b>Ingest</b></div>"
            "<h1>Ingest</h1><div class='sub'>Ingest turns codebases and .raw material into wiki pages, one "
            "section at a time, and keeps a hash of each so only what changed is sent again.</div></div>",
            runs_view._flash(flash, bad), f"<div id='r-ing' class='stack'>{regions['ing']}</div>"]


def ingest_live(vault, extra=None, log=""):
    regions = dict({"ing": _ingest_panel(vault, full=True, log=log)}, **(extra or {}))
    return {"sig": runs_view._sig(regions), "regions": regions}


def render_browse(vault, name, rel):
    """A folder's listing or a file shown: markdown rendered, text as is. None: not found."""
    got = resolve(vault, rel)
    if not got:
        return None
    full, rel = got
    if os.path.isdir(full):
        return _folder(vault, full, rel)
    if not os.path.isfile(full):
        return None
    title = rel.rsplit("/", 1)[-1]
    side = (f"<section class='panel'><div class='hd'><h2>History</h2></div>{_hist_list(_history(vault, rel, 15), rel)}"
            "</section>")
    acts = _obsidian(os.path.basename(vault), rel) if rel.lower().endswith(".md") else ""
    if SECRET.search(rel):
        body = "<p class='muted'>Not shown: the name looks like a secret or key file.</p>"
    else:
        ext = os.path.splitext(rel)[1].lower()
        if ext in IMAGES:
            body = f"<img src='{esc('/vault/file/' + quote(rel))}' alt='{esc(title)}' style='max-width:100%'>"
        else:
            text, cut = read_text(full)
            if text is None:
                body = f"<p class='muted'>A binary file, {os.path.getsize(full)} bytes.</p>"
            elif ext in (".md", ".markdown"):
                meta, md_body = frontmatter(text)
                facts = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in list(meta.items())[:30] if v)
                if facts:
                    side = f"<section class='panel'><h2>Properties</h2><dl class='facts'>{facts}</dl></section>" + side
                t, _ = title_of(vault, rel)
                title = t or title
                html_ = render_markdown(vault, rel, md_body)
                first = re.match(r"\s*<h1[^>]*>(.*?)</h1>\n?", html_, re.S)
                if first and re.sub(r"<[^>]+>", "", first.group(1)).strip() == esc(title):
                    html_ = html_[first.end():]          # the page heading already says it
                body = f"<article class='md'>{html_}</article>"
            else:
                body = f"<pre class='vraw'>{esc(text)}</pre>"
            if cut:
                body += f"<p class='muted'>Only the first {FILE_CAP // 1000} kB is shown.</p>"
    return [f"<style>{STYLE}</style>",
            f"<div class='ph'>{_crumbs(rel)}<h1>{esc(title)}</h1>{acts}</div>",
            f"<div class='vgrid'><section class='panel'>{body}</section><div class='stack'>{side}</div></div>"]


def _folder(vault, full, rel):
    all_pages = pages(vault)
    prefix = rel + "/" if rel else ""
    dirs, files = {}, []
    for p, mtime in all_pages.items():
        if not p.startswith(prefix):
            continue
        rest = p[len(prefix):]
        if "/" in rest:
            d = rest.split("/", 1)[0]
            n, m = dirs.get(d, (0, 0))
            dirs[d] = (n + 1, max(m, mtime))
        else:
            files.append((rest, mtime))
    rows = []
    for d in sorted(dirs, key=str.lower):
        n, m = dirs[d]
        rows.append(f"<li><span class='nm dir'><a href='{esc(href_of(prefix + d))}'>{esc(d)}/</a>"
                    f"<small>{n} file{'s' if n != 1 else ''}</small></span>"
                    f"<span class='meta'>{esc(runs_view._ago(m * 1000))}</span></li>")
    for f, m in sorted(files, key=lambda x: x[0].lower()):
        label = ""
        if f.lower().endswith(".md"):
            t, meta = title_of(vault, prefix + f)
            if t and t != f[:-3]:
                label = f"<small>{esc(t)}</small>"
            if meta.get("summary"):
                label += f"<small>{esc(meta['summary'][:140])}</small>"
        rows.append(f"<li><span class='nm'><a href='{esc(href_of(prefix + f))}'>{esc(f)}</a>{label}</span>"
                    f"<span class='meta'>{esc(runs_view._ago(m * 1000))}</span></li>")
    listing = "<ul class='vlist'>" + "".join(rows) + "</ul>" if rows else "<p class='muted'>Empty.</p>"
    title = rel.rsplit("/", 1)[-1] if rel else os.path.basename(vault)
    return [f"<style>{STYLE}</style>",
            f"<div class='ph'>{_crumbs(rel)}<h1>{esc(title)}/</h1></div>",
            f"<section class='panel'>{_search_box()}{listing}</section>"]


def search(vault, q):
    """[(path, [(line number, text)])] for q: every word, case-insensitive, in any
    order on a line; files whose path matches come first with no lines."""
    words = [w for w in q.split() if w][:6]
    if not words:
        return []
    args = ["grep", "-n", "-I", "-i", "--fixed-strings", "--untracked", "--full-name", "--no-color",
            "--max-count=20"]
    for i, w in enumerate(words):
        args += (["--and"] if i else []) + ["-e", w]
    out = git(vault, *args, "--", ".", ":!.obsidian", ":!.harness", ":!.gitignore", ":!.gitattributes",
              timeout=10) or ""
    hits, order = {}, []
    for line in out.splitlines():
        path, _, rest = line.partition(":")
        num, _, text = rest.partition(":")
        if SECRET.search(path) or not num.isdigit():
            continue
        if path not in hits:
            hits[path] = []
            order.append(path)
        hits[path].append((int(num), text[:400]))
        if sum(len(v) for v in hits.values()) >= SEARCH_CAP:
            break
    low = [w.lower() for w in words]
    named = [p for p in pages(vault) if all(w in p.lower() for w in low) and p not in hits and not SECRET.search(p)]
    rank = lambda p: (0 if p.startswith("wiki/") else 1 if p.startswith(".raw/") else 2, p)  # noqa: E731
    return [(p, []) for p in sorted(named, key=rank)[:50]] + [(p, hits[p]) for p in sorted(order, key=rank)]


def _mark(text, words):
    out = esc(text)
    for w in sorted(set(words), key=len, reverse=True):
        out = re.sub(re.escape(esc(w)), lambda m: f"<mark>{m.group(0)}</mark>", out, flags=re.I)
    return out


def render_search(vault, q):
    q = q.strip()[:200]
    res = search(vault, q) if q else []
    words = q.split()
    if not q:
        body = "<p class='muted'>Type words to find; every word must be on the same line.</p>"
    elif not res:
        body = f"<p class='muted'>Nothing matches &ldquo;{esc(q)}&rdquo;.</p>"
    else:
        n = sum(len(v) for _, v in res)
        items = []
        for p, lines in res:
            pre = "\n".join(f"{num}: {_mark(t, words)}" for num, t in lines[:8])
            more = f"<span class='muted'> and {len(lines) - 8} more lines</span>" if len(lines) > 8 else ""
            items.append(f"<li><a href='{esc(href_of(p))}'>{esc(p)}</a>{more}" + (f"<pre>{pre}</pre>" if pre else "")
                         + "</li>")
        body = (f"<p class='muted'>{n} matching line{'s' if n != 1 else ''} in {len(res)} file"
                f"{'s' if len(res) != 1 else ''}" + (" (only the first ones)" if n >= SEARCH_CAP else "") + "</p>"
                f"<ul class='hits'>{''.join(items)}</ul>")
    return [f"<style>{STYLE}</style>",
            "<div class='ph'><div class='crumbs'><a href='/vault'>Vault</a><span>/</span><b>Search</b></div>"
            "<h1>Search</h1></div>",
            f"<section class='panel'>{_search_box(q)}{body}</section>"]


def render_commit(vault, sha, rel=""):
    if not SHA_RE.match(sha or ""):
        return None
    head = git(vault, "show", "-s", "--format=%H%x09%an%x09%ar%x09%aI%x09%s%n%b", sha)
    if not head:
        return None
    first, _, msg = head.partition("\n")
    bits = first.split("\t", 4)
    if len(bits) != 5:
        return None
    full, author, ago, when, subject = bits
    paths = ["--", rel] if rel and resolve(vault, rel) else []
    stat = git(vault, "show", "--format=", "--stat=120", sha, *paths) or ""
    diff = git(vault, "show", "--format=", "--no-ext-diff", "-M", sha, *paths, binary=True) or b""
    cut = len(diff) > DIFF_CAP
    diff = diff[:DIFF_CAP].decode("utf-8", "replace")
    scope = (f"<p class='muted'>Only {esc(rel)}. <a href='/vault/commit/{esc(sha)}'>The whole commit</a></p>"
             if paths else "")
    return [f"<style>{STYLE}</style>",
            "<div class='ph'><div class='crumbs'><a href='/vault'>Vault</a><span>/</span><b>Commit</b></div>"
            f"<h1>{esc(subject)}</h1><div class='sub'><code>{esc(full[:12])}</code> &middot; {esc(author)} &middot; "
            f"<span title='{esc(when)}'>{esc(ago)}</span></div></div>",
            "<section class='panel'>" + (f"<pre class='vraw'>{esc(msg.strip())}</pre>" if msg.strip() else "")
            + scope + f"<pre class='vraw muted'>{esc(stat)}</pre>{runs_view._diff(diff)}"
            + ("<p class='muted'>The diff is cut short here.</p>" if cut else "") + "</section>"]


def file_bytes(vault, rel):
    """(bytes, content type) of an image in the vault, or None."""
    got = resolve(vault, rel)
    if not got or not os.path.isfile(got[0]) or SECRET.search(got[1]):
        return None
    ctype = IMAGES.get(os.path.splitext(got[1])[1].lower())
    if not ctype:
        return None
    try:
        with open(got[0], "rb") as f:
            data = f.read(20_000_001)
    except OSError:
        return None
    return (data, ctype) if len(data) <= 20_000_000 else None


# --- ingest controls ------------------------------------------------------------------

UNIT_RE = re.compile(r"^[^\s\x00-\x1f]{1,400}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
_started = {"proc": None}


def ingest_log(state_dir):
    return os.path.join(state_dir, "serve", "ingest.out") if state_dir else ""


def start_ingest(h, state_dir, vault, form):
    """Start `harness ingest` in the background as the form asks: (ok, message)."""
    import settings_view
    run = ingest_run(vault)
    proc = _started["proc"]
    if (run and run.get("alive")) or (proc is not None and proc.poll() is None):
        return False, "an ingest is already running on this vault; stop it first or wait for it"
    get = lambda k: (form.get(k) or [""])[0].strip()   # noqa: E731
    args = ["ingest"]
    unit, folder, mx, model = get("unit"), get("dir"), get("max"), get("model")
    if unit:
        if not UNIT_RE.match(unit):
            return False, "not a section id"
        args += ["--unit", unit]
    else:
        if folder:
            if folder.startswith("-") or "\n" in folder or len(folder) > 1000:
                return False, "give the codebase's folder path"
            path = os.path.expanduser(folder)
            if not os.path.isdir(path):
                return False, f"no folder {folder} on the computer harness serve runs on"
            args.append(path)
        if get("what") == "retry":
            args.append("--retry-failed")
    if mx:
        if not mx.isdigit() or not 0 < int(mx) < 100000:
            return False, "at most: a whole number of sections"
        args += ["--max", mx]
    if model:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,200}$", model):
            return False, "not a model id"
        args += ["--model", model]
    proc, why = settings_view.spawn_harness(*h, args, ingest_log(state_dir))
    if proc is None:
        return False, why
    _started["proc"] = proc
    return True, "Ingest started. This page follows it; the output is under the controls."


def stop_ingest(vault):
    """SIGTERM the running ingest (it saves its progress and removes its run file) and the
    harness started from here with it, which may still be getting ready (no run file yet)."""
    import signal
    run, proc, stopped = ingest_run(vault), _started["proc"], False
    if run and run.get("alive"):
        try:
            os.kill(int(run.get("pid")), signal.SIGTERM)
            stopped = True
        except (TypeError, ValueError, OSError):
            pass
    if proc is not None and proc.poll() is None:
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)    # its own session: harness and what it started
            stopped = True
        except OSError:
            pass
    if not stopped:
        return False, "no ingest is running"
    return True, "Asked the ingest to stop; sections already written are kept."


def forget_codebase(h, vault, form):
    import settings_view
    name = (form.get("name") or [""])[0].strip()
    if not NAME_RE.match(name):
        return False, "not a codebase name"
    run = ingest_run(vault)
    if run and run.get("alive"):
        return False, "an ingest is running; stop it first"
    rc, out = settings_view.run_harness(*h, "ingest", "--forget", name, timeout=180)
    lines = [x.strip() for x in (out or "").splitlines() if x.strip()]
    if rc != 0:
        return False, (lines[-1] if lines else "harness ingest --forget failed")
    return True, f"Forgot {name}: " + (lines[-1] if lines else "done")
