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
"""

import argparse
import base64
import html
import mimetypes
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

PUB_CSP = "sandbox allow-scripts allow-popups"
TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


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


class Config:
    def __init__(self, port, vault, vault_name, opencode_port, password_file, allow_hosts):
        self.port = port
        self.vault = vault
        self.vault_name = vault_name
        self.pub_dir = os.path.join(vault, "pub") if vault else ""
        self.opencode_port = opencode_port
        self.password_file = password_file
        self.allowed = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
        self.allowed.update(h.lower() for h in allow_hosts)

    def opencode_token(self):
        if not self.password_file:
            return ""
        try:
            with open(self.password_file) as f:
                pw = f.read().strip()
        except OSError:
            return ""
        return base64.b64encode(f"opencode:{pw}".encode()).decode()


STYLE = """
:root { color-scheme: light dark; --bg: #fafafa; --fg: #1d1d1f; --muted: #6b6b70; --line: #e2e2e6; --link: #0b62d6; }
@media (prefers-color-scheme: dark) { :root { --bg: #151517; --fg: #ececf0; --muted: #9a9aa2; --line: #2c2c31; --link: #6aa8ff; } }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 28px 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
ul { list-style: none; padding: 0; margin: 0; } li { padding: 8px 0; border-bottom: 1px solid var(--line); display: flex; gap: 12px; justify-content: space-between; flex-wrap: wrap; }
a { color: var(--link); text-decoration: none; } a:hover { text-decoration: underline; }
.muted { color: var(--muted); font-size: 13px; }
"""


def render_index(cfg, request_host):
    host = request_host.rsplit(":", 1)[0] if request_host else "127.0.0.1"
    parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width, initial-scale=1'>",
             "<title>harness</title><style>", STYLE, "</style></head><body><main>",
             "<h1>harness</h1>"]
    if cfg.vault:
        parts.append(f"<div class='muted'>vault: {html.escape(cfg.vault_name or cfg.vault)}</div>")
    else:
        parts.append("<div class='muted'>no vault selected (harness vault use &lt;name&gt;)</div>")

    parts.append("<h2>Services</h2><ul>")
    if cfg.opencode_port:
        token = cfg.opencode_token()
        url = f"http://{host}:{cfg.opencode_port}/"
        if token:
            url += "?auth_token=" + quote(token, safe="")
        parts.append(f"<li><a href='{html.escape(url)}' rel='noreferrer'>opencode web</a>"
                     f"<span class='muted'>port {cfg.opencode_port}</span></li>")
    parts.append("</ul>")

    if cfg.vault:
        pages = list_pages(cfg.pub_dir)
        parts.append("<h2>Pages</h2>")
        if not pages:
            parts.append("<div class='muted'>no HTML pages in the vault's pub/ folder</div>")
        else:
            parts.append("<ul>")
            for rel, title, mtime in pages:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
                href = "/pub/" + quote(rel)
                parts.append(f"<li><a href='{html.escape(href)}'>{html.escape(title)}</a>"
                             f"<span class='muted'>{when}</span></li>")
            parts.append("</ul>")
    parts.append("</main></body></html>")
    return "".join(parts).encode()


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        server_version = "harness-pub"
        sys_version = ""

        def log_message(self, fmt, *args):
            sys.stderr.write("[pub] %s %s\n" % (self.address_string(), fmt % args))

        def send(self, code, body, ctype="text/plain; charset=utf-8", extra=None, head=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def handle_get(self, head):
            host = (self.headers.get("Host") or "").lower()
            if host not in cfg.allowed:
                self.send(421, b"unexpected Host\n", head=head)
                return
            path = urlsplit(self.path).path
            if path == "/":
                self.send(200, render_index(cfg, host), "text/html; charset=utf-8",
                          {"Cache-Control": "no-store",
                           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"},
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

        def do_GET(self):
            self.handle_get(False)

        def do_HEAD(self):
            self.handle_get(True)

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--vault", default="")
    ap.add_argument("--vault-name", default="")
    ap.add_argument("--opencode-port", type=int, default=0)
    ap.add_argument("--password-file", default="")
    ap.add_argument("--allow-host", action="append", default=[],
                    help="extra Host header value to accept, e.g. a reverse proxy's name")
    a = ap.parse_args(argv)
    cfg = Config(a.port, a.vault, a.vault_name, a.opencode_port, a.password_file, a.allow_host)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(cfg))
    srv.daemon_threads = True
    sys.stderr.write(f"[pub] serving http://127.0.0.1:{a.port}/\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
