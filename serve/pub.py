#!/usr/bin/env python3
"""The `harness serve` pub page: an index of the active vault's pub/ HTML
pages plus links to the services `harness serve` brought up, and the pages
themselves under /pub/. Standard library only, loopback only.

Two guards, both needed because the index carries the opencode web login
link:
  - the Host header must be one of the allowed names (DNS rebinding would
    otherwise let any web page read the index);
  - every /pub/ response is sent with a CSP sandbox (no allow-same-origin),
    so a page in the vault, often agent-written, runs with an opaque origin
    and cannot read the index or call opencode as a localhost origin.

Optional access codes (--access-codes FILE) put a small shared-code gate in
front of everything but /login, /logout, /_auth and /healthz. It is a
doorman for a small team, not an identity system: whoever holds a code gets
what the page links to, which includes opencode web, a shell on this host.

  pub.py --port N [...]                          serve
  pub.py add-code --access-codes FILE NAME       mint a code, print it once
  pub.py remove-code --access-codes FILE NAME    revoke NAME
"""

import argparse
import hashlib
import hmac
import html
import http.client
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

PUB_CSP = "sandbox allow-scripts allow-popups"
INDEX_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

SESSION_TTL = 12 * 3600      # idle seconds before a session lapses (sliding)
MAX_SESSIONS = 10000
FAIL_WINDOW = 600            # failed-login window, seconds
FAIL_PER_CLIENT = 10
FAIL_GLOBAL = 60
CACHE_TTL = 5.0
PAGE_CAP = 200
LEDGER_TAIL = 5 * 1024 * 1024
MAX_FORM = 4096
MAX_CODE = 256


def page_title(path):
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return None
    m = TITLE_RE.search(head)
    if not m:
        return None
    title = " ".join(m.group(1).decode("utf-8", "replace").split())
    return html.unescape(title) or None


def list_pages(pub_dir):
    """(relative path, title, mtime) for every .html under pub_dir, newest first."""
    pages = []
    if not pub_dir or not os.path.isdir(pub_dir):
        return pages
    for root, dirs, files in os.walk(pub_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in files:
            if name.startswith(".") or not name.lower().endswith((".html", ".htm")):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, pub_dir).replace(os.sep, "/")
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            pages.append((rel, page_title(full) or rel, mtime))
    pages.sort(key=lambda p: (-p[2], p[0]))
    return pages


def filter_pages(pages, query):
    """Pages whose title or path contains query, case-insensitively."""
    q = query.casefold()
    if not q:
        return pages
    return [p for p in pages if q in p[1].casefold() or q in p[0].casefold()]


def resolve_pub(pub_dir, url_path):
    """Map /pub/<rel> to a file inside pub_dir, or None (missing, a dotfile, or outside)."""
    rel = unquote(url_path[len("/pub/"):])
    if not pub_dir or not rel or any(part.startswith(".") for part in rel.split("/")):
        return None
    base = os.path.realpath(pub_dir)
    target = os.path.realpath(os.path.join(base, rel))
    if os.path.commonpath([base, target]) != base or not os.path.isfile(target):
        return None
    return target


# --- access codes ----------------------------------------------------------
#
# The file holds `name:sha256hex` lines. A plain sha256 is enough because
# add-code mints 192-bit random codes; a hand-written weak code is only as
# strong as itself (the login rate limit is the only brake on guessing).

def code_hash(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def parse_codes(text):
    """{(name, sha256hex)} from a codes file; comments, blanks and malformed lines are skipped."""
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, digest = line.partition(":")
        name, digest = name.strip(), digest.strip().lower()
        if sep and NAME_RE.match(name) and HASH_RE.match(digest):
            out.add((name, digest))
    return frozenset(out)


def _line_name(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    return line.partition(":")[0].strip()


def _private(path):
    if os.name == "posix":
        os.chmod(path, 0o600)


def add_code(path, name):
    """Append a new random code for name to the codes file; return the code."""
    if not NAME_RE.match(name):
        raise ValueError("name must match [A-Za-z0-9_.-]{1,32}")
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    if any(_line_name(line) == name for line in existing.splitlines()):
        raise ValueError(f"name '{name}' already has a code (remove-code it first)")
    code = secrets.token_urlsafe(24)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{name}:{code_hash(code)}\n")
    _private(path)
    return code


def remove_code(path, name):
    """Rewrite the codes file without name's line(s); raise ValueError if absent."""
    if not NAME_RE.match(name):
        raise ValueError("name must match [A-Za-z0-9_.-]{1,32}")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        raise ValueError(f"no codes file at {path}")
    kept = [line for line in lines if _line_name(line) != name]
    if len(kept) == len(lines):
        raise ValueError(f"no code for '{name}'")
    # A fresh file swapped in (new inode), so a running server sees the change
    # even when mtime and size happen to match.
    fd, tmp = tempfile.mkstemp(prefix=".codes-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(line + "\n" for line in kept))
        _private(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class AccessCodes:
    """The codes file, re-read whenever its mtime, size or inode changes."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.sig = object()
        self.current = frozenset()

    def entries(self):
        try:
            st = os.stat(self.path)
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            sig = None
        with self.lock:
            if sig != self.sig:
                fresh, self.sig = frozenset(), sig
                if sig is not None:
                    try:
                        with open(self.path, encoding="utf-8", errors="replace") as f:
                            fresh = parse_codes(f.read())
                    except OSError:
                        self.sig = object()   # retry next time; fail closed meanwhile
                if fresh != self.current:
                    sys.stderr.write(f"[pub] access codes: {len(fresh)} active\n")
                self.current = fresh
            return self.current

    def match(self, code):
        """(name, digest) whose hash equals sha256(code), or None; compares every entry."""
        digest = code_hash(code).encode()
        found = None
        for name, h in sorted(self.entries()):
            if hmac.compare_digest(digest, h.encode()) and found is None:
                found = (name, h)
        return found


class Sessions:
    """Server-side sessions: random id -> [name, code digest, idle deadline,
    view id]. The view id is a second random id that only unlocks GET /pub/
    (see Config.view_cookie)."""

    def __init__(self, codes, ttl=SESSION_TTL):
        self.codes = codes
        self.ttl = ttl
        self.lock = threading.Lock()
        self.data = {}
        self.views = {}

    def create(self, name, digest):
        """(session id, view id) for a new session."""
        sid, vid = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.monotonic()
        with self.lock:
            for k in [k for k, s in self.data.items() if s[2] < now]:
                self._del(k)
            while len(self.data) >= MAX_SESSIONS:
                self._del(min(self.data, key=lambda k: self.data[k][2]))
            self.data[sid] = [name, digest, now + self.ttl, vid]
            self.views[vid] = sid
        return sid, vid

    def _del(self, sid):
        s = self.data.pop(sid, None)
        if s is not None:
            self.views.pop(s[3], None)

    def lookup_view(self, vid):
        """The name behind a view id, under the same rules as lookup."""
        with self.lock:
            sid = self.views.get(vid) if vid else None
        return self.lookup(sid)

    def lookup(self, sid):
        """The name behind sid if it is live and its code is still in the file; slides the deadline."""
        if not sid:
            return None
        live = self.codes.entries()
        now = time.monotonic()
        with self.lock:
            s = self.data.get(sid)
            if s is None:
                return None
            if s[2] < now or (s[0], s[1]) not in live:
                self._del(sid)
                return None
            s[2] = now + self.ttl
            return s[0]

    def drop(self, sid):
        with self.lock:
            self._del(sid)


class FailLimiter:
    """Failed logins per client and overall, in a sliding window."""

    def __init__(self, per_client=FAIL_PER_CLIENT, total=FAIL_GLOBAL, window=FAIL_WINDOW):
        self.per_client, self.total, self.window = per_client, total, window
        self.lock = threading.Lock()
        self.all = deque()
        self.by_client = {}

    def _trim(self, q, now):
        while q and q[0] <= now - self.window:
            q.popleft()

    def retry_after(self, client):
        """Seconds until client may try again; 0 if not blocked."""
        now = time.monotonic()
        with self.lock:
            self._trim(self.all, now)
            q = self.by_client.get(client)
            if q is not None:
                self._trim(q, now)
            wait = 0.0
            if len(self.all) >= self.total:
                wait = self.all[len(self.all) - self.total] + self.window - now
            if q is not None and len(q) >= self.per_client:
                wait = max(wait, q[len(q) - self.per_client] + self.window - now)
            return int(wait) + 1 if wait > 0 else 0

    def fail(self, client):
        now = time.monotonic()
        with self.lock:
            self.all.append(now)
            self.by_client.setdefault(client, deque()).append(now)
            if len(self.by_client) > 256:
                for k in list(self.by_client):
                    self._trim(self.by_client[k], now)
                    if not self.by_client[k]:
                        del self.by_client[k]


def origin_ok(headers, allowed):
    """CSRF guard for anything but GET/HEAD.

    A browser sends Origin on every cross-site POST, so: Origin present ->
    its host:port must be one of ours ("null", from sandboxed pages, file://
    and some redirects, is never ours). Origin absent -> only a non-browser
    client or an old browser on a same-origin form; accept it when nothing
    else says cross-site (Sec-Fetch-Site absent or same-origin) and there is
    no Referer either (our pages send none: Referrer-Policy no-referrer).
    SameSite=Strict on the session cookie is the second layer.
    """
    origin = headers.get("Origin")
    if origin is not None:
        origin = origin.strip()
        if origin.lower() == "null":
            return False
        parts = urlsplit(origin)
        return parts.scheme in ("http", "https") and parts.netloc.lower() in allowed
    site = headers.get("Sec-Fetch-Site")
    return site in (None, "same-origin") and headers.get("Referer") is None


def cookie_values(headers, name):
    """Every value the request sent for cookie `name` (Cookie headers may repeat it)."""
    out = []
    for raw in headers.get_all("Cookie") or []:
        for part in raw.split(";"):
            k, sep, v = part.strip().partition("=")
            if sep and k == name:
                out.append(v.strip().strip('"'))
    return out


# --- status sources (each cached for CACHE_TTL) ----------------------------

class TTLCache:
    def __init__(self, ttl=CACHE_TTL):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.data = {}

    def get(self, key, fn):
        now = time.monotonic()
        with self.lock:
            hit = self.data.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1]
        value = fn()
        with self.lock:
            self.data[key] = (time.monotonic(), value)
        return value


def proxy_status(proxy_url):
    """(up, detail) for GET <proxy_url>/health. Plain http.client: no client
    headers, no environment proxy, no redirects followed, 2 s timeout."""
    u = urlsplit(proxy_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False, "bad --proxy-url"
    https = u.scheme == "https"
    cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    try:
        port = u.port or (443 if https else 80)
    except ValueError:
        return False, "bad --proxy-url"
    try:
        c = cls(u.hostname, port, timeout=2)
        try:
            c.request("GET", u.path.rstrip("/") + "/health", headers={"User-Agent": "harness-pub"})
            r = c.getresponse()
            r.read(4096)
        finally:
            c.close()
    except (OSError, http.client.HTTPException) as e:
        return False, type(e).__name__
    return 200 <= r.status < 300, f"HTTP {r.status}"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def read_ledger(path, now=None):
    """Last-24 h usage from the proxy's JSONL ledger (its last LEDGER_TAIL
    bytes), or None when the file is missing or empty. Bad lines are skipped."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            start = max(0, size - LEDGER_TAIL)
            f.seek(start)
            data = f.read()
    except OSError:
        return None
    if start:
        data = data.partition(b"\n")[2]
    now = time.time() if now is None else now
    day = {"replies": 0, "prompt": 0, "completion": 0, "reasoning": 0, "cached": 0}
    hour = {"replies": 0, "total": 0}
    models = {}
    remaining, remaining_ts = {}, float("-inf")
    for line in data.splitlines():
        try:
            e = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(e, dict):
            continue
        ts = _num(e.get("ts"))
        if not ts:
            continue
        rem = e.get("remaining")
        if isinstance(rem, dict) and rem and ts >= remaining_ts:
            remaining, remaining_ts = rem, ts
        if ts < now - 86400:
            continue
        p, c = int(_num(e.get("prompt"))), int(_num(e.get("completion")))
        day["replies"] += 1
        day["prompt"] += p
        day["completion"] += c
        day["reasoning"] += int(_num(e.get("reasoning")))
        day["cached"] += int(_num(e.get("cached")))
        m = models.setdefault(str(e.get("model") or "?")[:120], [0, 0, 0])
        m[0] += 1
        m[1] += p
        m[2] += c
        if ts >= now - 3600:
            hour["replies"] += 1
            hour["total"] += p + c
    day["total"] = day["prompt"] + day["completion"]
    rows = sorted(models.items(), key=lambda kv: (-(kv[1][1] + kv[1][2]), kv[0]))
    rem_items = sorted((str(k)[:80], str(v)[:80]) for k, v in remaining.items())
    return {"day": day, "hour": hour, "models": rows, "remaining": rem_items}


def vault_log(vault):
    """[(hash, author, when, subject)] for the vault's last 15 commits, or None."""
    git = shutil.which("git")
    if not vault or not git or not os.path.isdir(vault):
        return None
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    for k in ("GIT_DIR", "GIT_WORK_TREE"):
        env.pop(k, None)
    cmd = [git, "--no-pager", "-C", vault, "-c", "log.showSignature=false", "log", "-15",
           "--no-color", "--format=%h%x09%an%x09%ar%x09%s"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=3, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows = [tuple(line.split("\t", 3)) for line in r.stdout.decode("utf-8", "replace").splitlines()]
    return [row for row in rows if len(row) == 4] or None


class Config:
    def __init__(self, port, vault, vault_name, opencode_port, password_file, allow_hosts,
                 access_codes="", secure_cookie=False, opencode_url="", proxy_url="",
                 modes=(), usage_ledger="", trust_forwarded_for=False):
        self.port = port
        self.vault = vault
        self.vault_name = vault_name
        self.pub_dir = os.path.join(vault, "pub") if vault else ""
        self.opencode_port = opencode_port
        self.password_file = password_file
        self.allowed = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
        self.allowed.update(h.lower() for h in allow_hosts)
        self.codes = AccessCodes(access_codes) if access_codes else None
        self.sessions = Sessions(self.codes) if self.codes else None
        self.limiter = FailLimiter()
        self.secure_cookie = secure_cookie
        self.cookie_name = "__Host-harness_pub" if secure_cookie else "harness_pub"
        self.view_cookie_name = "__Secure-harness_pub_view" if secure_cookie else "harness_pub_view"
        self.opencode_url = opencode_url
        self.proxy_url = proxy_url
        self.modes = list(modes)
        self.usage_ledger = usage_ledger
        self.trust_forwarded_for = trust_forwarded_for
        self.cache = TTLCache()

    def opencode_password(self):
        if not self.password_file:
            return ""
        try:
            with open(self.password_file) as f:
                return f.read().strip()
        except OSError:
            return ""

    def cookie(self, value, clear=False):
        attrs = "; Path=/; HttpOnly; SameSite=Strict"
        if clear:
            attrs += "; Max-Age=0"
        if self.secure_cookie:
            attrs += "; Secure"
        return f"{self.cookie_name}={value}{attrs}"

    def view_cookie(self, value, clear=False):
        """The /pub/-only cookie. A /pub/ page runs sandboxed with an opaque
        origin, so browsers treat its image/style/script loads and its links
        to other /pub/ pages as cross-site and withhold SameSite=Strict (and,
        for subresources, Lax) cookies. This one is SameSite=None so those
        loads still authenticate; it is scoped to Path=/pub/ and accepted only
        for GET/HEAD /pub/, which changes nothing and whose responses another
        site cannot read (no CORS). None requires Secure; browsers accept a
        Secure cookie over plain http only on 127.0.0.1/localhost."""
        attrs = "; Path=/pub/; HttpOnly; SameSite=None; Secure"
        if clear:
            attrs += "; Max-Age=0"
        return f"{self.view_cookie_name}={value}{attrs}"


STYLE = """
:root { color-scheme: light dark; --bg: #fafafa; --fg: #1d1d1f; --muted: #6b6b70; --line: #e2e2e6; --link: #0b62d6; --ok: #18794e; --bad: #c4321c; --field: #fff; }
@media (prefers-color-scheme: dark) { :root { --bg: #151517; --fg: #ececf0; --muted: #9a9aa2; --line: #2c2c31; --link: #6aa8ff; --ok: #4cc38a; --bad: #ff6b57; --field: #1f1f23; } }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 28px 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
ul { list-style: none; padding: 0; margin: 0; } li { padding: 8px 0; border-bottom: 1px solid var(--line); display: flex; gap: 12px; justify-content: space-between; flex-wrap: wrap; }
a { color: var(--link); text-decoration: none; } a:hover { text-decoration: underline; }
.muted { color: var(--muted); font-size: 13px; } .up { color: var(--ok); } .down { color: var(--bad); }
.bar { display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
form.inline { display: inline; margin: 0; }
input, button { font: inherit; color: inherit; background: var(--field); border: 1px solid var(--line); border-radius: 6px; padding: 5px 10px; }
button { cursor: pointer; } input[type=search], input[type=password] { flex: 1; min-width: 0; }
.row { display: flex; gap: 8px; margin: 0 0 8px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 4px 6px; border-bottom: 1px solid var(--line); } th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; font-size: 13px; }
code { font: 13px ui-monospace, monospace; } .log td { text-align: left; vertical-align: top; } .err { color: var(--bad); }
"""

esc = html.escape


def _head(title):
    return ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            f"<title>{esc(title)}</title><style>", STYLE, "</style></head><body><main>"]


def _n(v):
    return f"{int(v):,}"


def render_login(cfg, message=""):
    parts = _head("harness: sign in") + ["<h1>harness</h1><h2>Sign in</h2>"]
    if message:
        parts.append(f"<p class='err'>{esc(message)}</p>")
    parts.append("<form method='post' action='/login' class='row'>"
                 "<input type='password' name='code' placeholder='access code' "
                 "autocomplete='current-password' required autofocus>"
                 "<button type='submit'>Sign in</button></form>"
                 "<div class='muted'>Ask whoever runs this server for an access code.</div>"
                 "</main></body></html>")
    return "".join(parts).encode()


def _services(cfg, host):
    parts = ["<h2>Services</h2><ul>"]
    if cfg.opencode_url or cfg.opencode_port:
        if cfg.opencode_url:
            url, where = cfg.opencode_url, urlsplit(cfg.opencode_url).netloc
        else:
            # The login rides in the URL's userinfo, not opencode's ?auth_token=: that
            # token covers only the HTML, so its scripts and styles draw a Basic prompt.
            # A userinfo URL draws a 401 the browser answers itself, then caches the
            # login for the page's later requests (adding ?auth_token= would skip that
            # 401, so the cache stays empty).
            pw = cfg.opencode_password()
            login = f"opencode:{quote(pw, safe='')}@" if pw else ""
            url = f"http://{login}{host}:{cfg.opencode_port}/"
            where = f"port {cfg.opencode_port}"
        parts.append(f"<li><a href='{esc(url)}' rel='noreferrer'>opencode web</a>"
                     f"<span class='muted'>{esc(where)}</span></li>")
        parts.append("<li><span>Terminal: inside opencode web, open a <b>New terminal</b> tab "
                     "(<code>ctrl+`</code>). It is a shell on this host, running as the user "
                     "that runs <code>harness serve</code>.</span></li>")
    if cfg.proxy_url:
        up, detail = cfg.cache.get(("proxy", cfg.proxy_url), lambda: proxy_status(cfg.proxy_url))
        state = "<span class='up'>up</span>" if up else "<span class='down'>down</span>"
        parts.append(f"<li><span>model proxy {state}</span>"
                     f"<span class='muted'>{esc(cfg.proxy_url)} ({esc(detail)})</span></li>")
    parts.append("</ul>")
    if cfg.modes:
        parts.append("<h2>Prompt modes</h2><div class='muted'>Each mode is its own entry in "
                     "opencode's model picker; pick the one you want for the session.</div><ul>")
        for name, desc in cfg.modes:
            parts.append(f"<li><code>{esc(name)}</code><span class='muted'>{esc(desc)}</span></li>")
        parts.append("</ul>")
    return parts


def _usage(cfg):
    if not cfg.usage_ledger:
        return []
    u = cfg.cache.get(("usage", cfg.usage_ledger), lambda: read_ledger(cfg.usage_ledger))
    if not u:
        return []
    d, h = u["day"], u["hour"]
    parts = ["<h2>Usage, last 24 h</h2><table><tr><th>replies</th><th>prompt</th>"
             "<th>completion</th><th>total</th><th>last hour</th></tr>",
             f"<tr><td>{_n(d['replies'])}</td><td>{_n(d['prompt'])}</td><td>{_n(d['completion'])}</td>"
             f"<td>{_n(d['total'])}</td><td>{_n(h['total'])} ({_n(h['replies'])} replies)</td></tr></table>",
             f"<div class='muted'>reasoning {_n(d['reasoning'])}, cached prompt {_n(d['cached'])} "
             "(both counted inside the columns above)</div>"]
    if u["models"]:
        parts.append("<table><tr><th>model</th><th>replies</th><th>prompt</th><th>completion</th>"
                     "<th>total</th></tr>")
        for model, (n, p, c) in u["models"][:50]:
            parts.append(f"<tr><td><code>{esc(model)}</code></td><td>{_n(n)}</td><td>{_n(p)}</td>"
                         f"<td>{_n(c)}</td><td>{_n(p + c)}</td></tr>")
        parts.append("</table>")
    if u["remaining"]:
        left = ", ".join(f"{esc(k)} {esc(v)}" for k, v in u["remaining"])
        parts.append(f"<div class='muted'>upstream remaining (latest reply): {left}</div>")
    return parts


def _pages(cfg, query):
    pages = cfg.cache.get(("pages", cfg.pub_dir), lambda: list_pages(cfg.pub_dir))
    parts = ["<h2>Pages</h2>"]
    if not pages:
        parts.append("<div class='muted'>no HTML pages in the vault's pub/ folder</div>")
        return parts
    parts.append("<form method='get' action='/' class='row'>"
                 f"<input type='search' name='q' value='{esc(query)}' placeholder='filter by title or path'>"
                 "<button type='submit'>Filter</button></form>")
    shown = filter_pages(pages, query)
    if query:
        parts.append(f"<div class='muted'>{len(shown)} of {len(pages)} pages match "
                     f"&ldquo;{esc(query)}&rdquo; &middot; <a href='/'>clear</a></div>")
    if not shown:
        return parts
    parts.append("<ul>")
    for rel, title, mtime in shown[:PAGE_CAP]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        href = "/pub/" + quote(rel)
        parts.append(f"<li><a href='{esc(href)}'>{esc(title)}</a>"
                     f"<span class='muted'>{when}</span></li>")
    parts.append("</ul>")
    if len(shown) > PAGE_CAP:
        parts.append(f"<div class='muted'>{len(shown) - PAGE_CAP} more not shown; "
                     "narrow them with the filter</div>")
    return parts


def _activity(cfg):
    rows = cfg.cache.get(("git", cfg.vault), lambda: vault_log(cfg.vault))
    if not rows:
        return []
    parts = ["<h2>Vault activity</h2><table class='log'>"]
    for sha, author, when, subject in rows:
        parts.append(f"<tr><td><code>{esc(sha)}</code></td><td>{esc(subject)}</td>"
                     f"<td class='muted'>{esc(author)}</td><td class='muted'>{esc(when)}</td></tr>")
    parts.append("</table>")
    return parts


def render_index(cfg, request_host, user=None, query=""):
    host = request_host.rsplit(":", 1)[0] if request_host else "127.0.0.1"
    parts = _head("harness")
    parts.append("<div class='bar'><h1>harness</h1>")
    if user:
        parts.append(f"<span class='muted'>signed in as <b>{esc(user)}</b> "
                     "<form method='post' action='/logout' class='inline'>"
                     "<button type='submit'>Log out</button></form></span>")
    parts.append("</div>")
    if cfg.vault:
        parts.append(f"<div class='muted'>vault: {esc(cfg.vault_name or cfg.vault)}</div>")
    else:
        parts.append("<div class='muted'>no vault selected (harness vault use &lt;name&gt;)</div>")
    parts += _services(cfg, host)
    parts += _usage(cfg)
    if cfg.vault:
        parts += _pages(cfg, query)
        parts += _activity(cfg)
    parts.append("</main></body></html>")
    return "".join(parts).encode()


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        server_version = "harness-pub"
        sys_version = ""

        def log_message(self, fmt, *args):
            sys.stderr.write("[pub] %s %s\n" % (self.address_string(), fmt % args))

        def send(self, code, body, ctype="text/plain; charset=utf-8", extra=None, head=False,
                 cookies=()):
            hdrs = {"Content-Type": ctype, "Content-Length": str(len(body)),
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
            hdrs.update(extra or {})
            self.send_response(code)
            for k, v in hdrs.items():
                self.send_header(k, v)
            for c in cookies:
                self.send_header("Set-Cookie", c)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def send_page(self, code, body, extra=None, head=False):
            # same-origin, not no-referrer, on the pages with forms: under
            # no-referrer browsers send `Origin: null` on a same-origin POST,
            # which origin_ok refuses. Cross-origin requests still get no Referer.
            hdrs = {"Cache-Control": "no-store", "Content-Security-Policy": INDEX_CSP,
                    "Referrer-Policy": "same-origin"}
            hdrs.update(extra or {})
            self.send(code, body, "text/html; charset=utf-8", hdrs, head)

        def redirect(self, location, head=False, cookies=()):
            self.send(303, b"", extra={"Location": location, "Cache-Control": "no-store"},
                      head=head, cookies=cookies)

        def host_ok(self, head):
            if (self.headers.get("Host") or "").lower() in cfg.allowed:
                return True
            self.send(421, b"unexpected Host\n", head=head)
            return False

        def client(self):
            if cfg.trust_forwarded_for:
                xff = ",".join(self.headers.get_all("X-Forwarded-For") or [])
                last = xff.split(",")[-1].strip()
                if last:
                    return last
            return self.client_address[0]

        def session(self):
            """(sid, name) of the request's live session, or (None, None)."""
            if not cfg.codes:
                return None, None
            for sid in cookie_values(self.headers, cfg.cookie_name):
                name = cfg.sessions.lookup(sid)
                if name:
                    return sid, name
            return None, None

        def handle_get(self, head):
            if not self.host_ok(head):
                return
            url = urlsplit(self.path)
            path = url.path
            if path == "/healthz":
                self.send(200, b"ok\n", extra={"Cache-Control": "no-store"}, head=head)
                return
            # X-Harness-User is only ever set here, from the session; an
            # inbound one is never read.
            sid, user = self.session()
            if cfg.codes and not user and path.startswith("/pub/"):
                for vid in cookie_values(self.headers, cfg.view_cookie_name):
                    user = cfg.sessions.lookup_view(vid)
                    if user:
                        break
            if path == "/_auth":
                if not cfg.codes:
                    self.send(200, b"ok\n", extra={"Cache-Control": "no-store"}, head=head)
                elif user:
                    self.send(200, b"ok\n", extra={"Cache-Control": "no-store",
                                                    "X-Harness-User": user}, head=head)
                else:
                    self.send_page(401, b"<!doctype html><title>harness</title>"
                                   b"<p>Not signed in. <a href='/login'>Sign in</a></p>", head=head)
                return
            if path in ("/login", "/logout"):
                if not cfg.codes:
                    self.send(404, b"access codes are not enabled\n", head=head)
                elif path == "/logout":
                    self.send(405, b"use POST\n", extra={"Allow": "POST"}, head=head)
                elif user:
                    self.redirect("/", head=head)
                else:
                    self.send_page(200, render_login(cfg), head=head)
                return
            if cfg.codes and not user:
                if path == "/":
                    self.send_page(401, render_login(cfg), head=head)
                else:
                    self.redirect("/login", head=head)
                return
            if path == "/":
                query = (parse_qs(url.query).get("q") or [""])[0].strip()[:200]
                self.send_page(200, render_index(cfg, self.headers.get("Host", "").lower(), user, query),
                               head=head)
                return
            if path.startswith("/pub/"):
                target = resolve_pub(cfg.pub_dir, path)
                if not target:
                    self.send(404, b"not found\n", head=head)
                    return
                ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
                if ctype.startswith("text/"):
                    ctype += "; charset=utf-8"
                with open(target, "rb") as f:
                    body = f.read()
                self.send(200, body, ctype,
                          {"Content-Security-Policy": PUB_CSP, "Cache-Control": "no-cache"}, head=head)
                return
            self.send(404, b"not found\n", head=head)

        def read_form(self):
            """The urlencoded body as a dict, or None if it is too large."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > MAX_FORM:
                return None
            raw = self.rfile.read(n) if n else b""
            return parse_qs(raw.decode("utf-8", "replace"), max_num_fields=20)

        def do_POST(self):
            if not self.host_ok(False):
                return
            if not origin_ok(self.headers, cfg.allowed):
                self.send(403, b"cross-origin request refused\n")
                return
            path = urlsplit(self.path).path
            if not cfg.codes or path not in ("/login", "/logout"):
                self.send(404, b"not found\n")
                return
            if path == "/logout":
                for sid in cookie_values(self.headers, cfg.cookie_name):
                    cfg.sessions.drop(sid)
                self.redirect("/login", cookies=(cfg.cookie("", clear=True),
                                                 cfg.view_cookie("", clear=True)))
                return
            client = self.client()
            wait = cfg.limiter.retry_after(client)
            if wait:
                self.send_page(429, render_login(cfg, "Too many failed sign-ins. Try again later."),
                               {"Retry-After": str(wait)})
                return
            try:
                form = self.read_form()
            except ValueError:
                form = None
            if form is None:
                self.send(413, b"form too large\n")
                return
            code = (form.get("code") or [""])[0].strip()
            if not code:
                self.send_page(401, render_login(cfg, "Enter an access code."))
                return
            hit = cfg.codes.match(code) if len(code) <= MAX_CODE else None
            if not hit:
                cfg.limiter.fail(client)
                sys.stderr.write(f"[pub] sign-in failed from {client}\n")
                self.send_page(401, render_login(cfg, "That code is not valid."))
                return
            sid, vid = cfg.sessions.create(*hit)
            sys.stderr.write(f"[pub] signed in: {hit[0]} from {client}\n")
            self.redirect("/", cookies=(cfg.cookie(sid), cfg.view_cookie(vid)))

        def do_GET(self):
            self.handle_get(False)

        def do_HEAD(self):
            self.handle_get(True)

    return Handler


def _http_url(value):
    if not value:
        return ""
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise argparse.ArgumentTypeError(f"not an http(s) URL: {value!r}")
    return value


def _mode(value):
    name, sep, desc = value.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"--mode wants NAME=DESCRIPTION, got {value!r}")
    return name.strip(), desc.strip()


def codes_main(argv):
    """add-code / remove-code: manage the access codes file."""
    cmd = argv[0]
    ap = argparse.ArgumentParser(prog=f"pub.py {cmd}")
    ap.add_argument("--access-codes", required=True, metavar="FILE")
    ap.add_argument("name")
    a = ap.parse_args(argv[1:])
    try:
        if cmd == "add-code":
            code = add_code(a.access_codes, a.name)
            print(code)
            sys.stderr.write(f"added a code for '{a.name}'. It is shown once and not stored; "
                             "send it to them privately.\n")
        else:
            remove_code(a.access_codes, a.name)
            sys.stderr.write(f"removed '{a.name}'; their sessions end on their next request.\n")
    except (ValueError, OSError) as e:
        sys.stderr.write(f"pub.py {cmd}: {e}\n")
        return 1
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("add-code", "remove-code"):
        return codes_main(argv)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--vault", default="")
    ap.add_argument("--vault-name", default="")
    ap.add_argument("--opencode-port", type=int, default=0)
    ap.add_argument("--password-file", default="")
    ap.add_argument("--allow-host", action="append", default=[],
                    help="extra Host header value to accept, e.g. a reverse proxy's name")
    ap.add_argument("--access-codes", default="", metavar="FILE",
                    help="name:sha256 lines; when given, everything but /login, /logout, "
                         "/_auth and /healthz needs a signed-in session")
    ap.add_argument("--secure-cookie", action="store_true",
                    help="behind TLS: Secure, __Host- prefixed session cookie")
    ap.add_argument("--trust-forwarded-for", action="store_true",
                    help="rate-limit sign-ins by the last X-Forwarded-For entry (set only "
                         "behind a reverse proxy that overwrites that header)")
    ap.add_argument("--opencode-url", type=_http_url, default="",
                    help="link opencode web here, with no login in the link (the proxy logs in)")
    ap.add_argument("--proxy-url", type=_http_url, default="",
                    help="model proxy base URL; the index shows GET <url>/health")
    ap.add_argument("--mode", type=_mode, action="append", default=[], metavar="NAME=DESCRIPTION",
                    help="a prompt mode / model alias to list (repeatable)")
    ap.add_argument("--usage-ledger", default="", metavar="FILE",
                    help="the proxy's usage JSONL; the index shows last-24 h totals")
    a = ap.parse_args(argv)
    cfg = Config(a.port, a.vault, a.vault_name, a.opencode_port, a.password_file, a.allow_host,
                 access_codes=a.access_codes, secure_cookie=a.secure_cookie,
                 opencode_url=a.opencode_url, proxy_url=a.proxy_url, modes=a.mode,
                 usage_ledger=a.usage_ledger, trust_forwarded_for=a.trust_forwarded_for)
    if cfg.codes:
        n = len(cfg.codes.entries())
        if not n:
            sys.stderr.write(f"[pub] warning: no valid codes in {a.access_codes}; nobody can sign in "
                             "(pub.py add-code --access-codes FILE NAME)\n")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(cfg))
    srv.daemon_threads = True
    sys.stderr.write(f"[pub] serving http://127.0.0.1:{a.port}/\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
