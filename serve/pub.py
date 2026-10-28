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
  pub.py start --pid-file F [--log F] --port N [...]
                                                 serve in the background, unless a pub
                                                 page already answers on N
  pub.py stop --pid-file F                       end the one start started
"""

import argparse
import base64
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
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runs as runs_view
import settings_view  # noqa: E402
import vault_view  # noqa: E402
import diff_view  # noqa: E402

PUB_CSP = "sandbox allow-scripts allow-popups"
INDEX_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
# The run viewer's pages run one inline script each (polling), allowed by nonce.
RUNS_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; "
            "img-src 'self'; form-action 'self'; frame-ancestors 'none'")
MAX_SEND_FORM = 3 * runs_view.MAX_MESSAGE + 1024   # urlencoded, so up to 3 bytes a byte
TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

SESSION_TTL = 12 * 3600      # idle seconds before a session lapses (sliding)
MAX_SESSIONS = 10000
FAIL_WINDOW = 600            # failed-login window, seconds
FAIL_PER_CLIENT = 10
FAIL_GLOBAL = 60
CACHE_TTL = 5.0
VAULT_TTL = 2.0
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
                           stderr=subprocess.DEVNULL, timeout=3, env=env, **runs_view.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows = [tuple(line.split("\t", 3)) for line in r.stdout.decode("utf-8", "replace").splitlines()]
    return [row for row in rows if len(row) == 4] or None


def code_stamp():
    """The newest mtime of serve's own code: a later one than at start means harness
    was updated on disk and this server still runs the old code."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        return max(os.stat(os.path.join(here, n)).st_mtime for n in os.listdir(here) if n.endswith(".py"))
    except (OSError, ValueError):
        return 0


class ActiveVault:
    """The vault harness has active now: with state_dir, read from its vault.active on
    each use (cached VAULT_TTL s), so `harness vault use` shows without a restart; else
    the one this process was started with."""

    def __init__(self, state_dir="", path="", name=""):
        self.state_dir, self.fixed = state_dir, (path, name)
        self._at, self._val = 0.0, ("", "")

    def get(self):
        if not self.state_dir:
            return self.fixed
        now = time.monotonic()
        if now - self._at > VAULT_TTL:
            try:
                with open(os.path.join(self.state_dir, "vault.active"), encoding="utf-8") as f:
                    name = f.read().strip()
            except OSError:
                name = None
            path = os.path.join(self.state_dir, "vaults", name) if name else ""
            if name is None or (name and name == self.fixed[1] and self.fixed[0]):
                val = self.fixed            # as started (its path in the form harness gave it)
            elif path and os.path.isdir(path):
                val = (path, name)
            else:
                val = ("", name)
            self._val, self._at = val, now
        return self._val


class Config:
    def __init__(self, port, vault, vault_name, opencode_port, password_file, allow_hosts,
                 access_codes="", secure_cookie=False, opencode_url="", proxy_url="",
                 modes=(), usage_ledger="", trust_forwarded_for=False, agent_data=(), queue_dir="",
                 harness="", project_dir="", state_dir=""):
        self.port = port
        self.active = ActiveVault(state_dir, vault, vault_name)
        self.state_dir = state_dir
        self.harness = harness
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
        self.started_code = code_stamp()
        self._code_seen = (0.0, False)
        self.runs = runs_view.Runs(agent_data, queue_dir, self.active, harness, project_dir) if agent_data else None

    def code_changed(self):
        at, changed = self._code_seen
        if not changed and time.time() - at > 10:
            changed = code_stamp() > self.started_code + 1
            self._code_seen = (time.time(), changed)
        return changed

    @property
    def vault(self):
        return self.active.get()[0]

    @property
    def vault_name(self):
        return self.active.get()[1]

    @property
    def pub_dir(self):
        v = self.vault
        return os.path.join(v, "pub") if v else ""

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
/* Neutral greys, one indigo accent, and status colours that each mean one thing.
   A console for one host: the opencode launch panel up top, then pages and usage in the main
   column beside a rail of prompt modes and vault commits; one column on a phone. System fonts
   only (the index CSP loads nothing). */
:root {
  color-scheme: light;
  --bg: #f6f7f9; --panel: #ffffff; --raised: #f3f4f6; --sunk: #eef0f3; --hover: #f0f2f5; --field: #ffffff;
  --line: #e1e4e8; --line-2: #d0d4da; --line-3: #b5bbc4; --ink: #16181d; --ink-2: #3b4048; --muted: #5f6670;
  --faint: #8a919b; --accent: #4f56d6; --accent-ink: #ffffff; --accent-soft: rgba(79, 86, 214, .10);
  --run: #1f6feb; --run-soft: rgba(31, 111, 235, .10); --wait: #9a6700; --wait-soft: rgba(212, 167, 44, .16);
  --wait-ink: #ffffff; --bad: #cf222e; --bad-soft: rgba(207, 34, 46, .09); --ok: #1a7f37; --ok-soft: rgba(26, 127, 55, .10);
  --user: #1f6feb; --user-soft: rgba(31, 111, 235, .04); --user-line: rgba(31, 111, 235, .28); --tool: #6f42c1;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  --mono: ui-monospace, "SF Mono", "Cascadia Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  --r: 10px;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0e0f12; --panel: #15171b; --raised: #1c1f24; --sunk: #111317; --hover: #1a1d22; --field: #0f1114;
  --line: #262a31; --line-2: #343a43; --line-3: #4a515c; --ink: #e6e8eb; --ink-2: #c3c7cd; --muted: #9298a2;
  --faint: #6b717b; --accent: #8a90ff; --accent-ink: #0b0c1a; --accent-soft: rgba(138, 144, 255, .14);
  --run: #5aa9ff; --run-soft: rgba(90, 169, 255, .13); --wait: #e8a93b; --wait-soft: rgba(232, 169, 59, .13);
  --wait-ink: #1a1205; --bad: #f2685f; --bad-soft: rgba(242, 104, 95, .13); --ok: #3fbf7f; --ok-soft: rgba(63, 191, 127, .13);
  --user: #5aa9ff; --user-soft: rgba(90, 169, 255, .06); --user-line: rgba(90, 169, 255, .26); --tool: #c4a7ff;
  color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #0e0f12; --panel: #15171b; --raised: #1c1f24; --sunk: #111317; --hover: #1a1d22; --field: #0f1114;
  --line: #262a31; --line-2: #343a43; --line-3: #4a515c; --ink: #e6e8eb; --ink-2: #c3c7cd; --muted: #9298a2;
  --faint: #6b717b; --accent: #8a90ff; --accent-ink: #0b0c1a; --accent-soft: rgba(138, 144, 255, .14);
  --run: #5aa9ff; --run-soft: rgba(90, 169, 255, .13); --wait: #e8a93b; --wait-soft: rgba(232, 169, 59, .13);
  --wait-ink: #1a1205; --bad: #f2685f; --bad-soft: rgba(242, 104, 95, .13); --ok: #3fbf7f; --ok-soft: rgba(63, 191, 127, .13);
  --user: #5aa9ff; --user-soft: rgba(90, 169, 255, .06); --user-line: rgba(90, 169, 255, .26); --tool: #c4a7ff;
  color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 var(--sans); }
main { max-width: 1100px; margin: 0 auto; padding-block: 20px 56px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: flex; flex-direction: column; gap: 20px; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
a:focus-visible, button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
code { font: 0.86em var(--mono); }
h1, h2 { margin: 0; text-wrap: balance; }
h2 { font-size: 12px; font-weight: 650; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); }
.muted { color: var(--muted); font-size: 13px; }
.err { color: var(--bad); margin: 0; }
.top { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.brand { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; min-width: 0; }
.brand h1 { font: 700 22px/1 var(--mono); letter-spacing: -.02em; }
.brand h1::before { content: "~$ "; color: var(--accent); }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
  background: var(--sunk); color: var(--muted); font-size: 13px; }
.chip b { color: var(--ink); font-weight: 600; }
.who { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px; }
.who b { color: var(--ink); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); padding: 18px; min-width: 0;
  display: flex; flex-direction: column; gap: 12px; }
.launch { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px 24px; align-items: center; }
.launch .lede { margin: 0; color: var(--muted); max-width: 62ch; }
.launch .lede b { color: var(--ink); }
.go { display: inline-flex; align-items: center; gap: 10px; padding: 10px 16px; border-radius: 8px;
  background: var(--accent); color: var(--accent-ink); font-weight: 650; white-space: nowrap; }
.go:hover { text-decoration: none; filter: brightness(1.08); }
.go span { font: 12px var(--mono); opacity: .8; }
.status { grid-column: 1 / -1; display: flex; gap: 8px 10px; align-items: center; flex-wrap: wrap;
  padding-top: 12px; border-top: 1px solid var(--line); font-size: 14px; min-width: 0; }
.status .muted { overflow-wrap: anywhere; }
.up, .down { display: inline-flex; align-items: center; gap: 6px; padding: 1px 9px; border-radius: 999px; font-size: 12px; font-weight: 650; }
.up { color: var(--ok); background: var(--ok-soft); } .down { color: var(--bad); background: var(--bad-soft); }
.up::before, .down::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 340px); gap: 20px; align-items: start; }
.stack { display: flex; flex-direction: column; gap: 20px; min-width: 0; }
ul { list-style: none; padding: 0; margin: 0; }
.list li { display: flex; justify-content: space-between; align-items: baseline; gap: 4px 16px; flex-wrap: wrap;
  padding: 9px 0; border-top: 1px solid var(--line); }
.list li:first-child { border-top: 0; }
.list a { font-weight: 550; overflow-wrap: anywhere; }
.list .muted { font: 12px var(--mono); color: var(--muted); }
.modes li { display: flex; flex-direction: column; gap: 2px; padding: 8px 0; border-top: 1px solid var(--line); }
.modes li:first-child { border-top: 0; }
.modes code { font-weight: 650; color: var(--ink); }
.row { display: flex; gap: 8px; margin: 0; }
input, button { font: inherit; color: inherit; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px; }
input { background: var(--bg); }
button { cursor: pointer; background: var(--sunk); } button:hover { border-color: var(--muted); }
input[type=search], input[type=password] { flex: 1; min-width: 0; }
form.inline { display: inline; margin: 0; }
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.tile { background: var(--sunk); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.tile .k { color: var(--muted); font-size: 12px; }
.tile .v { font: 600 20px/1.2 var(--mono); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; padding-left: 0; } th:last-child, td:last-child { padding-right: 0; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
tr:last-child td { border-bottom: 0; }
.share { display: block; height: 3px; margin-top: 4px; border-radius: 2px; background: var(--accent); opacity: .55; }
.log li { display: flex; flex-direction: column; gap: 2px; padding: 8px 0; border-top: 1px solid var(--line); min-width: 0; }
.log li:first-child { border-top: 0; }
.log .s { overflow-wrap: anywhere; }
.log .muted { font-size: 12px; }
.log code { color: var(--accent); }
.signin { max-width: 400px; width: 100%; margin: 12vh auto 0; }
@media (max-width: 820px) { .cols { grid-template-columns: minmax(0, 1fr); } }
.cols.one { grid-template-columns: minmax(0, 1fr); } .stack:empty { display: none; }
.home-top { display: flex; flex-direction: column; gap: 14px; }
@media (max-width: 560px) { .launch { grid-template-columns: minmax(0, 1fr); } .go { justify-content: center; }
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } .list li { flex-direction: column; gap: 2px; } }
"""

esc = html.escape


def _head(title, bar=""):
    """The page's head; with bar (the run viewer's sticky nav), the runs layout."""
    return ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            f"<title>{esc(title)}</title><style>", STYLE, runs_view.STYLE, "</style></head><body>",
            bar + "<main class='rv'>" if bar else "<main>"]


def opencode_session_link(cfg, host):
    """f(source label, session id) -> the session in opencode web, or "": opencode's
    own route /server/<base64url of its origin>/session/<id>, which opens a session
    by id whatever folder it is in. Only the host's sessions: opencode web (opencode
    serve on this host) reads the host's database, not a container's."""
    if cfg.opencode_url:
        u = urlsplit(cfg.opencode_url)
        netloc = u.netloc.rpartition("@")[2].lower()
        default = {"http": ":80", "https": ":443"}.get(u.scheme, "")
        if default and netloc.endswith(default):
            netloc = netloc[:-len(default)]
        origin = f"{u.scheme}://{netloc}"
        base = f"{u.scheme}://{u.netloc}"      # with the login, if the URL carries one
    elif cfg.opencode_port:
        origin = f"http://{host}:{cfg.opencode_port}"
        pw = cfg.opencode_password()
        # the login in the userinfo, as on the index's link (see _services)
        base = f"http://opencode:{quote(pw, safe='')}@{host}:{cfg.opencode_port}" if pw else origin
    else:
        return None
    server = base64.urlsafe_b64encode(origin.encode()).decode().rstrip("=")

    def link(label, sid):
        return f"{base}/server/{server}/session/{quote(sid)}" if label == "host" else ""
    return link


def _n(v):
    return f"{int(v):,}"


def render_login(cfg, message=""):
    parts = _head("harness: sign in") + [
        "<section class='panel signin'><div class='brand'><h1>harness</h1></div><h2>Sign in</h2>"]
    if message:
        parts.append(f"<p class='err'>{esc(message)}</p>")
    parts.append("<form method='post' action='/login' class='row'>"
                 "<input type='password' id='code' name='code' placeholder='access code' "
                 "autocomplete='current-password' required autofocus>"
                 "<button type='submit'>Sign in</button></form>"
                 "<div class='muted'>Ask whoever runs this server for an access code.</div>"
                 "</section></main></body></html>")
    return "".join(parts).encode()


def _services(cfg, host):
    """The launch panel (opencode web and the model proxy's state) and the prompt modes,
    as two lists of parts: the panel opens the page, the modes go in the side column."""
    launch, modes = [], []
    status = []
    if cfg.proxy_url:
        up, detail = cfg.cache.get(("proxy", cfg.proxy_url), lambda: proxy_status(cfg.proxy_url))
        state = "<span class='up'>up</span>" if up else "<span class='down'>down</span>"
        status.append(f"<div class='status'><span>model proxy {state}</span>"
                      f"<span class='muted'>{esc(cfg.proxy_url)} ({esc(detail)})</span></div>")
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
        launch.append("<section class='panel launch'>"
                      "<p class='lede'>Chat with the agent in <b>opencode web</b>. For a shell, open a "
                      "<b>New terminal</b> tab there (<code>ctrl+`</code>): it runs on this host as the "
                      "user that runs <code>harness serve</code>.</p>"
                      f"<a class='go' href='{esc(url)}' rel='noreferrer'>Open opencode web"
                      f"<span>{esc(where)}</span></a>")
        launch += status
        launch.append("</section>")
    elif status:
        launch += ["<section class='panel launch'>"] + status + ["</section>"]
    if cfg.modes:
        modes.append("<section class='panel'><h2>Prompt modes</h2><div class='muted'>Each mode is its "
                     "own entry in opencode's model picker; pick the one you want for the session.</div>"
                     "<ul class='modes'>")
        for name, desc in cfg.modes:
            modes.append(f"<li><code>{esc(name)}</code><span class='muted'>{esc(desc)}</span></li>")
        modes.append("</ul></section>")
    return launch, modes


def _usage(cfg):
    if not cfg.usage_ledger:
        return []
    u = cfg.cache.get(("usage", cfg.usage_ledger), lambda: read_ledger(cfg.usage_ledger))
    if not u:
        return []
    d, h = u["day"], u["hour"]
    tiles = (("replies", d["replies"]), ("prompt tokens", d["prompt"]),
             ("completion tokens", d["completion"]), ("total tokens", d["total"]))
    parts = ["<section class='panel'><h2>Usage, last 24 h</h2><div class='tiles'>"]
    for k, v in tiles:
        parts.append(f"<div class='tile'><span class='k'>{k}</span><span class='v'>{_n(v)}</span></div>")
    hr = "reply" if h["replies"] == 1 else "replies"
    parts.append(f"</div><div class='muted'>Last hour: {_n(h['total'])} tokens in {_n(h['replies'])} {hr}. Reasoning "
                 f"{_n(d['reasoning'])} and cached prompt {_n(d['cached'])} tokens are counted inside "
                 "the totals above.</div>")
    if u["models"]:
        top = max(p + c for _, (n, p, c) in u["models"][:50]) or 1
        parts.append("<div class='scroll'><table><tr><th>model</th><th>replies</th><th>prompt</th>"
                     "<th>completion</th><th>total</th></tr>")
        for model, (n, p, c) in u["models"][:50]:
            pct = round(100 * (p + c) / top)
            parts.append(f"<tr><td><code>{esc(model)}</code><span class='share' style='width:{pct}%'></span>"
                         f"</td><td>{_n(n)}</td><td>{_n(p)}</td><td>{_n(c)}</td><td>{_n(p + c)}</td></tr>")
        parts.append("</table></div>")
    if u["remaining"]:
        left = ", ".join(f"{esc(k)} {esc(v)}" for k, v in u["remaining"])
        parts.append(f"<div class='muted'>upstream remaining (latest reply): {left}</div>")
    parts.append("</section>")
    return parts


def _pages(cfg, query):
    pages = cfg.cache.get(("pages", cfg.pub_dir), lambda: list_pages(cfg.pub_dir))
    parts = ["<section class='panel'><h2>Pages</h2>"]
    if not pages:
        parts.append("<div class='muted'>No pages yet. HTML files the agent writes under the vault's "
                     "pub/ folder show up here.</div></section>")
        return parts
    parts.append("<form method='get' action='/' class='row'>"
                 f"<input type='search' id='q' name='q' value='{esc(query)}' placeholder='filter by title or path'>"
                 "<button type='submit'>Filter</button></form>")
    shown = filter_pages(pages, query)
    if query:
        parts.append(f"<div class='muted'>{len(shown)} of {len(pages)} pages match "
                     f"&ldquo;{esc(query)}&rdquo; &middot; <a href='/'>clear</a></div>")
    if shown:
        parts.append("<ul class='list'>")
        for rel, title, mtime in shown[:PAGE_CAP]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            href = "/pub/" + quote(rel)
            parts.append(f"<li><a href='{esc(href)}'>{esc(title)}</a>"
                         f"<span class='muted'>{when}</span></li>")
        parts.append("</ul>")
        if len(shown) > PAGE_CAP:
            parts.append(f"<div class='muted'>{len(shown) - PAGE_CAP} more not shown; "
                         "narrow them with the filter</div>")
    parts.append("</section>")
    return parts


def _activity(cfg):
    rows = cfg.cache.get(("git", cfg.vault), lambda: vault_log(cfg.vault))
    if not rows:
        return []
    parts = ["<section class='panel'><h2>Vault activity</h2><ul class='log'>"]
    for sha, author, when, subject in rows:
        parts.append(f"<li><span class='s'>{esc(subject)}</span><span class='muted'><code>{esc(sha)}</code> "
                     f"&middot; {esc(author)} &middot; {esc(when)}</span></li>")
    parts.append("</ul></section>")
    return parts


def home_regions(cfg, request_host, query=""):
    """The home page's live parts: the vault and services at the top, then runs,
    pages and usage beside the prompt modes and vault activity."""
    host = request_host.rsplit(":", 1)[0] if request_host else "127.0.0.1"
    launch, modes = _services(cfg, host)
    if cfg.vault:
        vault = f"<span class='chip'>vault <b>{esc(cfg.vault_name or cfg.vault)}</b></span>"
    elif cfg.vault_name:
        vault = (f"<span class='chip'>vault <b>{esc(cfg.vault_name)}</b>: not cloned here yet "
                 "(a launch clones it)</span>")
    else:
        vault = "<span class='chip'>no vault selected (harness vault use &lt;name&gt;)</span>"
    main = (runs_view.live_summary(cfg.runs) if cfg.runs else []) + \
        (_pages(cfg, query) if cfg.vault else []) + _usage(cfg)
    side = modes + (_activity(cfg) if cfg.vault else [])
    regions = {"top": f"<div class='brand'>{vault}</div>" + "".join(launch),
               "main": "".join(main), "side": "".join(side), "nav": runs_view.nav(cfg.runs, "home")}
    return regions


def home_live(cfg, request_host, query=""):
    regions = home_regions(cfg, request_host, query)
    return {"sig": runs_view._sig(regions), "regions": regions}


def render_home(cfg, request_host, query=""):
    """The home page's body parts, for send_runs (which adds the bar and the scripts)."""
    regions = home_regions(cfg, request_host, query)
    live = "/live" + (f"?q={quote(query, safe='')}" if query else "")
    return [runs_view._live_tag(live, runs_view._sig(regions)),
            f"<div id='r-top' class='home-top'>{regions['top']}</div>",
            f"<div class='cols{'' if regions['side'] else ' one'}'><div id='r-main' class='stack'>{regions['main']}</div>"
            f"<div id='r-side' class='stack'>{regions['side']}</div></div>"]


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

        def send_runs(self, code, title, parts, script="", head=False, active="sessions"):
            nonce = secrets.token_urlsafe(16)
            bar = runs_view.nav_bar(cfg.runs, active, getattr(self, "user", None))
            body = _head(title, bar) + parts
            body.append(f"<script nonce='{nonce}'>{runs_view.COMMON_JS}{script}</script>")
            body.append("</main></body></html>")
            self.send(code, "".join(body).encode(), "text/html; charset=utf-8",
                      {"Cache-Control": "no-store", "Content-Security-Policy": RUNS_CSP.format(nonce=nonce),
                       "Referrer-Policy": "same-origin"}, head)

        def send_json(self, obj, head=False):
            extra = {"Cache-Control": "no-store"}
            if cfg.code_changed():
                extra["X-Harness-Stale"] = "1"
            self.send(200, json.dumps(obj).encode(), "application/json; charset=utf-8", extra, head)

        def oc_link(self):
            return opencode_session_link(cfg, (self.headers.get("Host") or "127.0.0.1").rsplit(":", 1)[0])

        def runs_get(self, path, query, head):
            r = cfg.runs
            if r is None:
                self.send(404, b"the run viewer is off (no --agent-data)\n", head=head)
                return
            rest = path[len("/runs"):].strip("/")
            q = parse_qs(query)
            text = {"Cache-Control": "no-store", "Content-Security-Policy": PUB_CSP}
            done = (q.get("done") or [""])[0][:500]
            if not rest or rest == "live":
                filt = (q.get("q") or [""])[0].strip()[:200]
                if rest:
                    self.send_json(runs_view.list_live(r, filt), head)
                else:
                    self.send_runs(200, "harness: sessions", runs_view.render_list(r, filt),
                                   runs_view.LIVE_JS, head)
                return
            bits = rest.split("/")
            if bits[0] == "queue" and len(bits) <= 2 and r.queue_dir:
                if len(bits) == 1:
                    self.send_runs(200, "harness: queue", runs_view.render_queue_page(r, done),
                                   runs_view.LIVE_JS, head, "queue")
                elif bits[1] == "live":
                    self.send_json(runs_view.queue_live(r), head)
                elif bits[1] == "branches":
                    self.send_json(runs_view.project_branches((q.get("project") or [""])[0][:1000]), head)
                elif bits[1] == "runner.out":
                    self.send(200, (r.runner_out(400) or "nothing yet").encode(), extra=text, head=head)
                else:
                    self.send(404, b"not found\n", head=head)
                return
            if bits[0] == "ticket" and len(bits) in (2, 3):
                if len(bits) == 2:
                    got = runs_view.render_ticket(r, bits[1], done)
                    if got:
                        self.send_runs(200, f"harness: {got[0]}", got[1], runs_view.LIVE_JS, head, "queue")
                        return
                elif bits[2] == "diff":
                    got = diff_view.page(r, bits[1], q)
                    if got:
                        self.send_runs(200, f"harness: {got[0]}", got[1], got[2], head, "queue")
                        return
                elif bits[2] == "live":
                    got = runs_view.ticket_live(r, bits[1])
                    self.send_json(got if got else {"gone": "/runs/queue"}, head)
                    return
                else:
                    data = runs_view.ticket_file(r, bits[1], bits[2])
                    if data is not None:
                        self.send(200, data, extra=text, head=head)
                        return
                self.send(404, b"not found\n", head=head)
                return
            if bits[0] in ("follow", "follow.json") and len(bits) == 1:
                since, queue, follow = runs_view.follow_query(q)
                sid = runs_view.follow_target(r, since, queue)
                if bits[0] == "follow.json":
                    self.send(200, json.dumps({"sid": sid}).encode(), "application/json; charset=utf-8",
                              {"Cache-Control": "no-store"}, head)
                elif sid:
                    self.redirect(f"/runs/{quote(sid)}" + (f"?{follow}" if queue else ""), head)
                else:
                    self.send_runs(200, "harness: waiting", [runs_view.render_follow(follow, queue)],
                                   runs_view.FOLLOW_JS, head, "queue" if queue else "sessions")
                return
            sid = bits[0]
            if len(bits) == 1 and sid.endswith(".json"):
                got = runs_view.export(r, sid[:-5])
                if got is not None:
                    self.send(200, json.dumps(got, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8", text, head)
                    return
            elif len(bits) == 1:
                _, queue, follow = runs_view.follow_query(q)
                got = runs_view.render_detail(r, sid, "", follow if queue else "", self.oc_link(), done)
                if got:
                    self.send_runs(200, f"harness: {got[0]}", got[1], got[2], head,
                                   "queue" if r.ticket_of_row(r.find(sid)[1] or {}) else "sessions")
                    return
            elif bits[1:] == ["tail"]:
                try:
                    start = int((q.get("from") or ["0"])[0])
                except ValueError:
                    start = 0
                got = runs_view.tail(r, sid, start, self.oc_link())
                if got is not None:
                    self.send_json(got, head)
                    return
            elif len(bits) == 3 and bits[1] == "part":
                got = runs_view.part_text(r, sid, bits[2])
                if got is not None:
                    self.send(200, got.encode(), extra=text, head=head)
                    return
            self.send(404, b"not found\n", head=head)

        def vault_get(self, path, query, head):
            """The Vault pages (vault_view): read-only views of the active vault."""
            vault, name = cfg.active.get()
            q = parse_qs(query)
            rest = path[len("/vault"):].strip("/")
            nav = {"nav": runs_view.nav(cfg.runs, "vault")}
            if rest in ("", "live"):
                vaults = vault_view.list_vaults(cfg.state_dir)
                if rest == "live":
                    self.send_json(vault_view.overview_live(vault, name, vaults, nav), head)
                else:
                    done = (q.get("done") or [""])[0][:500]
                    parts = vault_view.render_overview(vault, name, vaults, nav)
                    if done:
                        parts.insert(3, runs_view._flash(done))
                    self.send_runs(200, "harness: vault", parts, runs_view.LIVE_JS, head, "vault")
                return
            if not vault or not os.path.isdir(vault):
                self.redirect("/vault", head=head)
                return
            if rest in ("ingest", "ingest/live"):
                log = vault_view.ingest_log(cfg.state_dir)
                if rest == "ingest/live":
                    self.send_json(vault_view.ingest_live(vault, nav, log), head)
                else:
                    done, bad = (q.get("done") or [""])[0][:600], (q.get("bad") or [""])[0][:600]
                    self.send_runs(200, "harness: ingest", vault_view.render_ingest(vault, nav, log, done or bad,
                                                                                    bool(bad and not done)),
                                   runs_view.LIVE_JS, head, "vault")
                return
            if rest == "search":
                self.send_runs(200, "harness: vault search",
                               vault_view.render_search(vault, (q.get("q") or [""])[0]), "", head, "vault")
                return
            kind, _, arg = rest.partition("/")
            arg = unquote(arg)
            got = None
            if kind == "browse" or rest == "browse":
                got = vault_view.render_browse(vault, name, arg)
            elif kind == "commit":
                got = vault_view.render_commit(vault, arg, (q.get("path") or [""])[0])
            elif kind == "file":
                img = vault_view.file_bytes(vault, arg)
                if img:
                    self.send(200, img[0], img[1], {"Content-Security-Policy": "default-src 'none'; sandbox",
                                                    "X-Content-Type-Options": "nosniff",
                                                    "Cache-Control": "no-cache"}, head)
                    return
            if got is None:
                self.send_runs(404, "harness: not found", [
                    "<section class='panel'><h2>Not found</h2><p>Nothing by that name in the vault (or it is "
                    "not shown here).</p><a href='/vault'>Back to the vault</a></section>"], "", head, "vault")
                return
            self.send_runs(200, "harness: vault", got, "", head, "vault")

        def vault_post(self, path, user):
            if path not in ("/vault/use", "/vault/ingest/start", "/vault/ingest/stop", "/vault/ingest/forget"):
                self.send(404, b"not found\n")
                return
            form = self.read_form()
            if form is None:
                self.send(413, b"form too large\n")
                return
            if path.startswith("/vault/ingest/"):
                vault, _ = cfg.active.get()
                if not vault or not os.path.isdir(vault):
                    self.redirect("/vault")
                    return
                act = path.rsplit("/", 1)[1]
                if act == "start":
                    ok, msg = vault_view.start_ingest(self.settings_args(), cfg.state_dir, vault, form)
                elif act == "stop":
                    ok, msg = vault_view.stop_ingest(vault)
                else:
                    ok, msg = vault_view.forget_codebase(self.settings_args(), vault, form)
                sys.stderr.write(f"[pub] ingest {act} from {user or 'local'}: {msg}\n")
                self.redirect("/vault/ingest?" + urlencode({"done" if ok else "bad": msg}))
                return
            ok, msg = vault_view.use_vault(cfg.state_dir, (form.get("name") or [""])[0].strip())
            sys.stderr.write(f"[pub] vault use from {user or 'local'}: {msg}\n")
            if not ok:
                self.refused(msg, "/vault", "Back to the vault")
                return
            cfg.active._at = 0.0
            self.redirect(f"/vault?done={quote(msg)}")

        def settings_args(self):
            sd = cfg.state_dir
            root = os.path.dirname(os.path.abspath(sd)) if sd and os.path.basename(os.path.abspath(sd)) == "state" else ""
            return cfg.harness, root

        def send_settings(self, code, flash="", bad=False, new_code=None, result=None, head=False):
            h = self.settings_args()
            _, user = self.session() if cfg.codes else (None, None)
            info = settings_view.system_info(h)
            info["state"] = cfg.state_dir
            info["vault"] = (f"{cfg.vault_name or '?'} at {cfg.vault}" if cfg.vault else
                             f"{cfg.vault_name}, not cloned here yet (a launch clones it)" if cfg.vault_name else "none")
            if cfg.runs and cfg.runs.queue_dir:
                n = len(cfg.runs.ticket_keys())
                info["queue"] = f"{cfg.runs.queue_dir} ({n} ticket{'' if n == 1 else 's'})"
            parts = settings_view.render(h, cfg.codes, user, info, flash, bad, new_code, result)
            self.send_runs(code, "harness: settings", parts, settings_view.PANELS_JS, head, "settings")

        def settings_post(self, path, user):
            """Settings changes: each runs the harness command a terminal user would type."""
            form = self.read_form(fields=80)
            if form is None:
                self.send(413, b"form too large\n")
                return
            h, what = self.settings_args(), path[len("/settings/"):]
            new_code = result = None
            if what == "config":
                ok, msg = settings_view.save_config(h, form)
            elif what == "net":
                ok, msg = settings_view.net_change(h, form)
            elif what == "mcp":
                ok, msg = settings_view.mcp_change(h, form)
            elif what == "access" and cfg.codes:
                act, name = (form.get("act") or [""])[0], (form.get("name") or [""])[0].strip()
                try:
                    if act == "add":
                        new_code, ok, msg = (name, add_code(cfg.codes.path, name)), True, f"Made a code for {name}."
                    elif act == "remove" and name != user:
                        remove_code(cfg.codes.path, name)
                        ok, msg = True, f"Removed {name}'s code; they are signed out."
                    else:
                        ok, msg = False, "you cannot remove the code you are signed in with"
                except (ValueError, OSError) as e:
                    ok, msg = False, str(e)
            elif what == "system":
                act = (form.get("act") or [""])[0]
                ok, msg, result = settings_view.system_action(h, act)
            else:
                self.send(404, b"not found\n")
                return
            settings_view.forget()     # what the page read may have changed
            sys.stderr.write(f"[pub] settings {what} from {user or 'local'}: {msg}\n")
            if new_code or result:
                # shown once, on this answer: a code is never stored, a report is not worth keeping
                self.send_settings(200, msg, not ok, new_code, result)
                return
            self.redirect("/settings?" + urlencode({"done" if ok else "bad": msg}))

        def refused(self, msg, back, label):
            self.send_runs(409, "harness: not done", [
                f"<section class='panel'><h2>Not done</h2><p class='err'>{esc(msg)}</p>"
                f"<a href='{esc(back)}'>{esc(label)}</a></section>"])

        def runs_send(self, path, user):
            bits = path[len("/runs/"):].split("/")
            if cfg.runs is not None and len(bits) == 3 and bits[0] == "ticket":
                self.ticket_post(bits[1], bits[2], user)
                return
            if cfg.runs is not None and len(bits) == 2 and bits[0] == "queue":
                self.queue_post(bits[1], user)
                return
            if cfg.runs is not None and len(bits) == 2 and bits[1] == "stop":
                try:
                    form = self.read_form()
                except ValueError:
                    form = None
                if form is None:
                    self.send(413, b"form too large\n")
                    return
                ok, msg = runs_view.session_stop(cfg.runs, bits[0])
                sys.stderr.write(f"[pub] stop {bits[0]} from {user or 'local'}: {msg}\n")
                if not ok:
                    self.refused(msg, f"/runs/{quote(bits[0])}", "Back to the run")
                    return
                self.redirect(f"/runs/{quote(bits[0])}?done={quote(msg[:500])}")
                return
            if cfg.runs is None or len(bits) != 2 or bits[1] != "send":
                self.send(404, b"not found\n")
                return
            try:
                form = self.read_form(MAX_SEND_FORM)
            except ValueError:
                form = None
            if form is None:
                self.send(413, b"message too large\n")
                return
            text = (form.get("text") or [""])[0]
            ok, msg = cfg.runs.send(bits[0], text.replace("\r\n", "\n"), user or "local")
            sys.stderr.write(f"[pub] message to {bits[0]} from {user or 'local'}: {msg}\n")
            if not ok:
                self.send_runs(409, "harness: not sent", [
                    f"<section class='panel'><h2>Not sent</h2><p class='err'>{esc(msg)}</p>"
                    f"<a href='/runs/{quote(bits[0])}'>Back to the run</a></section>"])
                return
            self.redirect(f"/runs/{quote(bits[0])}#end")

        def queue_post(self, action, user):
            """The queue page's forms: add tickets, start the run (from a ticket's page
            too: back=KEY), halt it."""
            if action not in runs_view.QUEUE_ACTIONS or not cfg.runs.queue_dir:
                self.send(404, b"not found\n")
                return
            try:
                form = self.read_form(MAX_SEND_FORM)
            except ValueError:
                form = None
            if form is None:
                self.send(413, b"form too large\n")
                return
            ok, msg = runs_view.queue_action(cfg.runs, action, form)
            sys.stderr.write(f"[pub] queue {action} from {user or 'local'}: {msg}\n")
            if not ok and action == "add":   # the form again, with what was typed and why not
                self.send_runs(409, "harness: queue", runs_view.render_queue_page(cfg.runs, form=form, err=msg),
                               runs_view.LIVE_JS, active="queue")
                return
            key = (form.get("back") or [""])[0]   # started from a ticket's page: back there
            back = f"/runs/ticket/{quote(key)}" if runs_view.KEY_RE.match(key) else "/runs/queue"
            if not ok:
                self.refused(msg, back, "Back to the ticket" if back != "/runs/queue" else "Back to the queue")
                return
            self.redirect(f"{back}?done={quote(msg[:500])}")

        def ticket_post(self, key, action, user):
            """The ticket's buttons: answer the plan's questions, approve the plan,
            retry, stop, remove, move, add a note (harness queue's own commands)."""
            if action not in runs_view.TICKET_ACTIONS:
                self.send(404, b"not found\n")
                return
            try:
                form = self.read_form(MAX_SEND_FORM)
            except ValueError:
                form = None
            if form is None:
                self.send(413, b"answers too large\n")
                return
            ok, msg = runs_view.ticket_action(cfg.runs, key, action, form)
            sys.stderr.write(f"[pub] {action} {key} from {user or 'local'}: {msg}\n")
            queue = (form.get("back") or [""])[0] == "queue" or (ok and action == "remove")
            back = "/runs/queue" if queue else f"/runs/ticket/{quote(key)}"
            if not ok:
                self.refused(msg, back, "Back to the queue" if queue else "Back to the ticket")
                return
            self.redirect(f"{back}?done={quote(msg[:500])}")

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
                # for `pub.py start`: whether this server runs code that has changed since,
                # and which harness state it shows (hashed: /healthz needs no sign-in);
                # for `pub.py stop --port`: which process to end
                extra = {"Cache-Control": "no-store", "X-Harness-State": state_id(cfg.state_dir),
                         "X-Harness-Pid": str(os.getpid())}
                if cfg.code_changed():
                    extra["X-Harness-Stale"] = "1"
                self.send(200, b"ok\n", extra=extra, head=head)
                return
            # X-Harness-User is only ever set here, from the session; an
            # inbound one is never read.
            sid, user = self.session()
            self.user = user
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
                if self.headers.get("X-Harness-Live"):
                    # the live scripts' polls: say so, rather than redirect to a page they cannot show
                    self.send(401, b'{"auth": false}', "application/json; charset=utf-8",
                              {"Cache-Control": "no-store"}, head)
                elif path == "/":
                    self.send_page(401, render_login(cfg), head=head)
                else:
                    self.redirect("/login", head=head)
                return
            if path in ("/", "/live"):
                query = (parse_qs(url.query).get("q") or [""])[0].strip()[:200]
                host = self.headers.get("Host", "").lower()
                if path == "/live":
                    self.send_json(home_live(cfg, host, query), head)
                else:
                    self.send_runs(200, "harness", render_home(cfg, host, query), runs_view.LIVE_JS,
                                   head, "home")
                return
            if path == "/runs" or path.startswith("/runs/"):
                self.runs_get(path, url.query, head)
                return
            if path == "/vault" or path.startswith("/vault/"):
                self.vault_get(path, url.query, head)
                return
            if path == "/settings/panels":
                self.send_json({"regions": settings_view.slow_panels(settings_view.gather(self.settings_args()))},
                               head)
                return
            if path == "/settings":
                q = parse_qs(url.query)
                done, bad = (q.get("done") or [""])[0][:600], (q.get("bad") or [""])[0][:600]
                self.send_settings(200, done or bad, bool(bad and not done), head=head)
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

        def read_form(self, limit=MAX_FORM, fields=20):
            """The urlencoded body as a dict, or None if it is too large."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > limit:
                return None
            raw = self.rfile.read(n) if n else b""
            return parse_qs(raw.decode("utf-8", "replace"), max_num_fields=fields)

        def do_POST(self):
            if not self.host_ok(False):
                return
            if not origin_ok(self.headers, cfg.allowed):
                self.send(403, b"cross-origin request refused\n")
                return
            path = urlsplit(self.path).path
            if path.startswith(("/runs/", "/vault/", "/settings/")):
                _, user = self.session()
                if cfg.codes and not user:
                    self.send(401, b"not signed in\n")
                    return
                if path.startswith("/vault/"):
                    self.vault_post(path, user)
                elif path.startswith("/settings/"):
                    self.settings_post(path, user)
                else:
                    self.runs_send(path, user)
                return
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


def _labelled(value):
    label, sep, path = value.partition("=")
    if not sep or not label.strip() or not path.strip():
        raise argparse.ArgumentTypeError(f"--agent-data wants LABEL=DIR, got {value!r}")
    return label.strip(), path.strip()


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


def state_id(state_dir):
    """A short name for a harness state folder, the same for any spelling of it."""
    if not state_dir:
        return ""
    return hashlib.sha256(os.path.normcase(os.path.realpath(state_dir)).encode("utf-8", "replace")).hexdigest()[:16]


def pub_health(port):
    """The pub page on 127.0.0.1:port: None when none answers /healthz, else
    (stale: its code changed on disk since it started, its state_id)."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        ok = r.status == 200 and r.read(8) == b"ok\n"
        got = (r.getheader("X-Harness-Stale") == "1", r.getheader("X-Harness-State") or "")
        c.close()
        return got if ok else None
    except (OSError, http.client.HTTPException):
        return None


def pub_pid(port):
    """The pid a pub page on 127.0.0.1:port gives at /healthz (0: none, or one too old to say)."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        r.read(8)
        pid = r.getheader("X-Harness-Pid") or ""
        c.close()
        return int(pid) if r.status == 200 and pid.isdigit() else 0
    except (OSError, http.client.HTTPException):
        return 0


def listening_pids(port):
    """The pids the OS lists as listening on TCP port, for a pub page too old to
    give its own. netstat's state column is translated, so a listening row is
    known by its remote address, which is 0.0.0.0:0 or [::]:0."""
    if os.name == "nt":
        cmd = ["netstat", "-ano", "-p", "TCP"]
    elif shutil.which("lsof"):
        cmd = ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"]
    else:
        cmd = ["ss", "-Hltnp", f"sport = :{port}"]
    try:
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=10, **runs_view.NO_WINDOW).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return set()
    if cmd[0] == "lsof":
        return {int(x) for x in out.split() if x.isdigit()}
    if cmd[0] == "ss":
        return {int(x) for x in re.findall(r"pid=(\d+)", out)}
    pids = set()
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4 and f[1].rsplit(":", 1)[-1] == str(port) and f[2].endswith(":0") and f[-1].isdigit():
            pids.add(int(f[-1]))
    return pids


def end_process(pid, tree=False):
    """End pid; tree=True also ends what it started. Without it a queue run the page started goes on, as it
    does when the page stops any other way."""
    if os.name == "nt":
        # os.kill is TerminateProcess there, which leaves the children
        subprocess.run(["taskkill", "/F", *(["/T"] if tree else []), "/PID", str(pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, **runs_view.NO_WINDOW)
        return
    for p in (descendants(pid) if tree else []) + [pid]:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass


def descendants(pid):
    """pid's children, theirs and so on (POSIX: from ps), deepest last."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        return []
    kids = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) == 2 and f[0].isdigit() and f[1].isdigit():
            kids.setdefault(int(f[1]), []).append(int(f[0]))
    found, todo = [], [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            if k not in found:
                found.append(k)
                todo.append(k)
    return found


def process_name(pid):
    """The lower-cased program name of pid ("" when unknown)."""
    if os.name == "nt":
        cmd = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    else:
        try:
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip().lower()
        except OSError:
            cmd = ["ps", "-o", "comm=", "-p", str(pid)]
    try:
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=10, **runs_view.NO_WINDOW).stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if os.name == "nt":
        out = out.split(",")[0].strip('"') if out.startswith('"') else ""
    return os.path.basename(out).lower()


def _wait_free(port, secs=5):
    for _ in range(int(secs * 10)):
        if not listening_pids(port):
            return True
        time.sleep(0.1)
    return False


def stop_port(port):
    """End the pub pages answering on 127.0.0.1:port, whichever harness started
    them (a run viewer an older harness left up, a harness serve in another
    terminal or one whose Ctrl-C did not reach it): a new one is to have the
    port. Every process listening there goes, since on Windows an older page
    shared the port with a newer one. True when one was there."""
    if not pub_answers(port):
        return False
    pids = set()
    for _ in range(3):
        now = ({pub_pid(port)} | listening_pids(port)) - {0, os.getpid()}
        pids |= now
        for pid in now:
            try:
                end_process(pid)
            except (OSError, subprocess.SubprocessError):
                pass
        for _ in range(50):
            if not pub_answers(port):
                return True
            time.sleep(0.1)
    sys.stderr.write(f"harness: the pages on port {port} (pid {', '.join(map(str, sorted(pids))) or 'unknown'}) "
                     "did not stop; end that process\n")
    return True


# What harness serve's opencode runs as: the native binary, the npm wrapper (node)
# that starts it, or bun.
OPENCODE_NAMES = ("opencode", "node", "bun")


def stop_opencode_port(port):
    """End the opencode web listening on 127.0.0.1:port and everything it started:
    one a harness serve left behind (killing the npm wrapper leaves the binary
    up, with its agents) would keep the port from the next one. A program
    of another name there is left alone. True when the port is free."""
    pids = listening_pids(port) - {0, os.getpid()}
    if not pids:
        return True
    left = []
    for pid in pids:
        name = process_name(pid)
        if not any(n in name for n in OPENCODE_NAMES):
            left.append(f"{pid} {name or '?'}")
            continue
        try:
            end_process(pid, tree=True)
        except (OSError, subprocess.SubprocessError):
            pass
    if left:
        sys.stderr.write(f"harness: port {port} is held by {', '.join(left)}, not opencode; left alone\n")
        return False
    if not _wait_free(port):
        sys.stderr.write(f"harness: opencode on port {port} (pid {', '.join(map(str, sorted(pids)))}) "
                         "did not stop; end that process\n")
        return False
    return True


def pub_answers(port):
    """Whether a pub page answers /healthz on 127.0.0.1:port."""
    return pub_health(port) is not None


def stop_viewer(pid_file):
    """End the run viewer pid_file names, if it is up; True when one was."""
    try:
        with open(pid_file) as f:
            pid, port = (int(x) for x in f.read().split()[:2])
    except (OSError, ValueError):
        return False
    up = pub_answers(port)
    if up:  # not some later process that reused the pid
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        os.remove(pid_file)
    except OSError:
        pass
    for _ in range(30 if up else 0):   # the port is free before a new one takes it
        if not pub_answers(port):
            break
        time.sleep(0.1)
    return up


def viewer_port(pid_file):
    try:
        with open(pid_file) as f:
            return int(f.read().split()[1])
    except (OSError, ValueError, IndexError):
        return None


def background_main(argv):
    """start / stop: the run viewer harness brings up when an agent starts working,
    so the launch can open it. It outlives the launch (the history stays readable)
    and its Ctrl-C (its own session, or on Windows a detached process group)."""
    cmd = argv[0]
    ap = argparse.ArgumentParser(prog=f"pub.py {cmd}")
    ap.add_argument("--pid-file", required=True)
    if cmd == "stop":
        ap.add_argument("--port", type=int, action="append", default=[],
                        help="also end the pub page on this port, whoever started it (repeatable)")
        ap.add_argument("--opencode-port", type=int, action="append", default=[],
                        help="also end the opencode listening on this port, with what it started (repeatable)")
        a = ap.parse_args(argv[1:])
        stop_viewer(a.pid_file)
        for port in a.port:
            stop_port(port)
        for port in a.opencode_port:
            stop_opencode_port(port)
        return 0
    ap.add_argument("--log", default=os.devnull)
    a, rest = ap.parse_known_args(argv[1:])
    pp = argparse.ArgumentParser(add_help=False)
    pp.add_argument("--port", type=int, required=True)
    pp.add_argument("--state-dir", default="")
    known = pp.parse_known_args(rest)[0]
    port = known.port
    up = pub_health(port)
    if up and up[0] and viewer_port(a.pid_file) == port:
        # the viewer a launch left up runs code harness has updated since (harness
        # upgrade, harness update): start it again on the new code
        sys.stderr.write("harness: harness was updated since the run viewer started; restarting it\n")
        stop_viewer(a.pid_file)
        up = pub_health(port)
    if up:
        stale, state = up
        if stale:
            sys.stderr.write(f"harness: harness serve on port {port} runs the code from before harness was "
                             "updated; stop it (Ctrl-C) and run harness serve again\n")
        elif state and known.state_dir and state != state_id(known.state_dir):
            sys.stderr.write(f"harness: port {port} has the pages of another harness install (another state "
                             "folder): its vault and queue are not this one's\n")
        return 0
    if os.name == "nt":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: a console with no window that the
        # git and harness it runs share, where with none each of them opens one.
        flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | 0x00000200}  # | CREATE_NEW_PROCESS_GROUP
    else:
        flags = {"start_new_session": True}
    with open(a.log, "ab") as log:
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), *rest], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, close_fds=True, **flags)
    with open(a.pid_file, "w") as f:
        f.write(f"{proc.pid} {port}\n")
    for _ in range(50):
        if pub_answers(port):
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    sys.stderr.write(f"pub.py start: the page did not come up on 127.0.0.1:{port} (see {a.log})\n")
    return 1


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows lets a second SO_REUSEADDR socket bind a port another one listens on,
    # and then either may get a connection: a pub page an older harness left up
    # went on answering in front of the new one. There the port is one socket's.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("add-code", "remove-code"):
        return codes_main(argv)
    if argv and argv[0] in ("start", "stop"):
        return background_main(argv)
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
    ap.add_argument("--agent-data", type=_labelled, action="append", default=[], metavar="LABEL=DIR",
                    help="a data dir holding opencode/opencode.db and harness/live (repeatable); "
                         "turns on the run viewer at /runs")
    ap.add_argument("--queue-dir", default="", metavar="DIR",
                    help="harness queue's state/queue: the queue page, and its stage sessions by ticket")
    ap.add_argument("--harness", default="", metavar="PATH",
                    help="the harness script the queue page's Start runs 'queue run' with "
                         "(default: the one next to this serve/ folder)")
    ap.add_argument("--state-dir", default="", metavar="DIR",
                    help="harness's state folder: the active vault is read from its vault.active "
                         "on each request (--vault is then only the fallback when not given)")
    ap.add_argument("--project-dir", default="", metavar="DIR",
                    help="the folder harness serve was started in: the Add form's project folder "
                         "when it is in a git repository")
    a = ap.parse_args(argv)
    cfg = Config(a.port, a.vault, a.vault_name, a.opencode_port, a.password_file, a.allow_host,
                 access_codes=a.access_codes, secure_cookie=a.secure_cookie,
                 opencode_url=a.opencode_url, proxy_url=a.proxy_url, modes=a.mode,
                 usage_ledger=a.usage_ledger, trust_forwarded_for=a.trust_forwarded_for,
                 agent_data=a.agent_data, queue_dir=a.queue_dir, harness=a.harness,
                 project_dir=a.project_dir, state_dir=a.state_dir)
    if cfg.codes:
        n = len(cfg.codes.entries())
        if not n:
            sys.stderr.write(f"[pub] warning: no valid codes in {a.access_codes}; nobody can sign in "
                             "(pub.py add-code --access-codes FILE NAME)\n")
    srv = Server(("127.0.0.1", a.port), make_handler(cfg))
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
