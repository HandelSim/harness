"""Tests for serve/pub.py against a live server on an ephemeral loopback port.

Run: python3 -m unittest serve/test_pub.py (tests/unit_serve_test.sh does).
"""

import base64
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pub  # noqa: E402
import vault_view  # noqa: E402


class LiveServer(unittest.TestCase):
    """A temp vault and pub servers on ephemeral loopback ports."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.vault = os.path.join(root, "vault")
        os.makedirs(os.path.join(self.vault, "pub", "notes"))
        os.makedirs(os.path.join(self.vault, "pub", ".git"))
        with open(os.path.join(self.vault, "pub", "old.html"), "w") as f:
            f.write("<html><head><title>Old &amp; page</title></head></html>")
        with open(os.path.join(self.vault, "pub", "notes", "new.html"), "w") as f:
            f.write("<title>New page</title><script>1</script>")
        with open(os.path.join(self.vault, "pub", ".git", "x.html"), "w") as f:
            f.write("<title>hidden</title>")
        with open(os.path.join(self.vault, "secret.txt"), "w") as f:
            f.write("outside pub")
        os.utime(os.path.join(self.vault, "pub", "old.html"), (1, 1))
        self.pwfile = os.path.join(root, "pw")
        with open(self.pwfile, "w") as f:
            f.write("s3cret\n")

    def tearDown(self):
        for srv in getattr(self, "servers", []):
            srv.shutdown()
            srv.server_close()
        self.tmp.cleanup()

    def start(self, vault=None, allow=(), **kw):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = srv.server_address[1]
        cfg = pub.Config(port, self.vault if vault is None else vault, "brain", port + 1, self.pwfile, allow, **kw)
        srv.RequestHandlerClass = pub.make_handler(cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers = getattr(self, "servers", []) + [srv]
        self.cfg = cfg
        return port

    def get(self, port, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body


class PubServerTest(LiveServer):
    def test_index_lists_pages_newest_first_and_links_opencode(self):
        port = self.start()
        r, body = self.get(port, "/")
        self.assertEqual(r.status, 200)
        text = body.decode()
        self.assertIn("New page", text)
        self.assertIn("Old &amp; page", text)
        self.assertLess(text.index("New page"), text.index("Old &amp; page"))
        self.assertNotIn(">hidden<", text)      # pub/.git/x.html is not a page
        self.assertNotIn("x.html", text)
        self.assertIn(f"href='http://opencode:s3cret@127.0.0.1:{port + 1}/'", text)
        self.assertNotIn("auth_token", text)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_wrong_host_is_refused(self):
        port = self.start()
        r, body = self.get(port, "/", host=f"evil.example:{port}")
        self.assertEqual(r.status, 421)
        self.assertNotIn(b"s3cret", body)

    def test_allow_host_accepts_a_reverse_proxy_name(self):
        port = self.start(allow=("pub.example.test",))
        r, _ = self.get(port, "/", host="pub.example.test")
        self.assertEqual(r.status, 200)

    def test_pub_pages_are_sandboxed(self):
        port = self.start()
        r, body = self.get(port, "/pub/notes/new.html")
        self.assertEqual(r.status, 200)
        self.assertIn(b"New page", body)
        csp = r.getheader("Content-Security-Policy")
        self.assertTrue(csp.startswith("sandbox"))
        self.assertNotIn("allow-same-origin", csp)

    def test_no_escape_from_pub(self):
        port = self.start()
        for path in ("/pub/../secret.txt", "/pub/%2e%2e/secret.txt", "/pub/.git/x.html", "/pub/", "/secret.txt"):
            r, _ = self.get(port, path)
            self.assertEqual(r.status, 404, path)

    def test_no_vault_still_serves(self):
        port = self.start(vault="")
        r, body = self.get(port, "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"vault <b>brain</b>: not cloned here yet", body)     # active, not cloned at state/vaults
        self.assertNotIn(b"no vault selected", body)
        self.assertIn(b"opencode web", body)
        r, _ = self.get(port, "/pub/old.html")
        self.assertEqual(r.status, 404)

    def test_healthz_says_which_state_and_whether_stale(self):
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(state)
        port = self.start(state_dir=state)
        self.assertEqual(pub.pub_health(port), (False, pub.state_id(state)))
        self.cfg.started_code, self.cfg._code_seen = 0, (0.0, False)     # its code changed on disk since; looked again
        stale, sid = pub.pub_health(port)
        self.assertTrue(stale)
        self.assertEqual(sid, pub.state_id(os.path.join(state, ".", "")))
        self.assertIsNone(pub.pub_health(free_port()))

    def test_healthz_gives_the_pid_and_the_port_is_its_own(self):
        port = self.start()
        self.assertEqual(pub.pub_pid(port), os.getpid())
        self.assertEqual(pub.pub_pid(free_port()), 0)
        # One page per port: on Windows SO_REUSEADDR let a second share it, and
        # each request went to either (an old page answered half of them).
        srv = pub.Server(("127.0.0.1", 0), pub.make_handler(self.cfg))
        try:
            with self.assertRaises(OSError):
                pub.Server(("127.0.0.1", srv.server_address[1]), pub.make_handler(self.cfg))
        finally:
            srv.server_close()

    def test_stop_opencode_port_leaves_another_program(self):
        port = free_port()
        other = subprocess.Popen([sys.executable, "-m", "http.server", "--bind", "127.0.0.1", str(port)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if pub.listening_pids(port):
                    break
                time.sleep(0.1)
            self.assertEqual(pub.listening_pids(port), {other.pid})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertFalse(pub.stop_opencode_port(port))
            self.assertIn("not opencode; left alone", err.getvalue())
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            other.wait()
        self.assertTrue(pub.stop_opencode_port(free_port()))

    def test_state_id_is_one_name_for_any_spelling(self):
        d = self.tmp.name
        self.assertEqual(pub.state_id(d), pub.state_id(d + "/"))
        self.assertEqual(pub.state_id(d), pub.state_id(os.path.join(d, "vault", "..")))
        self.assertNotEqual(pub.state_id(d), pub.state_id(self.vault))
        self.assertEqual(pub.state_id(""), "")


HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AccessCodeFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.codes = os.path.join(self.tmp.name, "serve", "access-codes")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "pub.py"), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_add_code_prints_once_and_stores_only_the_hash(self):
        r = self.cli("add-code", "--access-codes", self.codes, "alice")
        self.assertEqual(r.returncode, 0, r.stderr)
        code = r.stdout.decode().strip()
        self.assertGreaterEqual(len(code), 32)          # token_urlsafe(24): 192 bits
        with open(self.codes) as f:
            text = f.read()
        self.assertEqual(text, f"alice:{pub.code_hash(code)}\n")
        self.assertNotIn(code, text)
        self.assertNotIn(code, r.stderr.decode())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.codes).st_mode), 0o600)

    def test_add_code_rejects_bad_and_duplicate_names(self):
        self.assertEqual(self.cli("add-code", "--access-codes", self.codes, "alice").returncode, 0)
        for bad in ("alice", "has space", "a:b", "x" * 33, ""):
            r = self.cli("add-code", "--access-codes", self.codes, bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertEqual(r.stdout, b"", bad)
        with open(self.codes) as f:
            self.assertEqual(len(f.read().splitlines()), 1)

    def test_remove_code_keeps_other_lines(self):
        os.makedirs(os.path.dirname(self.codes))
        with open(self.codes, "w") as f:
            f.write("# team\n\n")
        pub.add_code(self.codes, "alice")
        pub.add_code(self.codes, "bob")
        r = self.cli("remove-code", "--access-codes", self.codes, "alice")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.codes) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[:2], ["# team", ""])
        self.assertEqual([l.split(":")[0] for l in lines[2:]], ["bob"])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.codes).st_mode), 0o600)
        self.assertNotEqual(self.cli("remove-code", "--access-codes", self.codes, "alice").returncode, 0)

    def test_parse_codes_skips_junk(self):
        good = "a" * 64
        got = pub.parse_codes(f"# c\n\nalice:{good}\nbad line\nbob:xyz\nc d:{good}\nEve : {good.upper()}\n")
        self.assertEqual(got, {("alice", good), ("Eve", good)})


class AccessGateTest(LiveServer):
    """The live server with --access-codes."""

    def setUp(self):
        super().setUp()
        self.codes = os.path.join(self.tmp.name, "codes")
        self.alice = pub.add_code(self.codes, "alice")
        self.bob = pub.add_code(self.codes, "bob")

    def gated(self, **kw):
        return self.start(access_codes=self.codes, **kw)

    def req(self, port, method, path, headers=None, body=None, host=None):
        h = {"Host": host or f"127.0.0.1:{port}"}
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def login(self, port, code, headers=None, host=None):
        h = {"Origin": f"http://{host or f'127.0.0.1:{port}'}",
             "Content-Type": "application/x-www-form-urlencoded"}
        h.update(headers or {})
        return self.req(port, "POST", "/login", h, urlencode({"code": code}), host=host)

    def set_cookies(self, r):
        """{cookie name: whole Set-Cookie value}."""
        return {c.split("=", 1)[0]: c for c in r.msg.get_all("Set-Cookie") or []}

    def cookie_of(self, r, name="harness_pub"):
        """The `name=value` pair a Cookie header would send back."""
        return self.set_cookies(r)[name].split(";")[0]

    def test_login_sets_strict_httponly_cookie(self):
        port = self.gated()
        r, _ = self.login(port, self.alice)
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), "/")
        self.assertEqual(set(self.set_cookies(r)), {"harness_pub", "harness_pub_view"})
        sc = self.set_cookies(r)["harness_pub"]
        attrs = [a.strip() for a in sc.split(";")[1:]]
        for want in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(want, attrs)
        self.assertNotIn("Secure", attrs)
        self.assertGreaterEqual(len(self.cookie_of(r).split("=", 1)[1]), 43)   # 32 random bytes
        r, body = self.req(port, "GET", "/", {"Cookie": self.cookie_of(r)})
        self.assertEqual(r.status, 200)
        self.assertIn(b"title='signed in as alice'", body)
        self.assertIn(b"New page", body)
        self.assertIn(b"opencode:s3cret@", body)
        self.assertIn("form-action 'self'", r.getheader("Content-Security-Policy"))

    def test_live_poll_signed_out_gets_401_json_not_the_login_page(self):
        port = self.gated()
        r, body = self.req(port, "GET", "/live", {"X-Harness-Live": "1"})
        self.assertEqual(r.status, 401)
        self.assertEqual(json.loads(body), {"auth": False})
        r, _ = self.req(port, "GET", "/runs/queue/live", {"X-Harness-Live": "1"})
        self.assertEqual(r.status, 401)
        r, _ = self.req(port, "GET", "/live")     # a browser's plain visit still goes to the sign-in page
        self.assertEqual(r.status, 303)
        r, _ = self.login(port, self.alice)
        r, body = self.req(port, "GET", "/live", {"Cookie": self.cookie_of(r), "X-Harness-Live": "1"})
        self.assertEqual(r.status, 200)
        self.assertIn("New page", json.loads(body)["regions"]["main"])

    def test_secure_cookie_uses_host_prefix(self):
        port = self.gated(secure_cookie=True)
        r, _ = self.login(port, self.alice)
        self.assertEqual(set(self.set_cookies(r)), {"__Host-harness_pub", "__Secure-harness_pub_view"})
        sc = self.set_cookies(r)["__Host-harness_pub"]
        self.assertIn("Secure", [a.strip() for a in sc.split(";")])
        self.assertNotIn("Domain", sc)
        cookie = self.cookie_of(r, "__Host-harness_pub")
        self.assertEqual(self.req(port, "GET", "/", {"Cookie": cookie})[0].status, 200)
        sid = cookie.split("=", 1)[1]
        r, _ = self.req(port, "GET", "/", {"Cookie": "harness_pub=" + sid})
        self.assertEqual(r.status, 401)     # the unprefixed name is not read

    def test_wrong_code_then_rate_limit(self):
        port = self.gated()
        r, body = self.login(port, "nope")
        self.assertEqual(r.status, 401)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertIn(b"not valid", body)
        for _ in range(pub.FAIL_PER_CLIENT - 1):
            self.assertEqual(self.login(port, "nope")[0].status, 401)
        r, _ = self.login(port, self.alice)            # even the right code waits
        self.assertEqual(r.status, 429)
        self.assertGreater(int(r.getheader("Retry-After")), 0)
        self.assertIsNone(r.getheader("Set-Cookie"))

    def test_rate_limit_per_client_and_global(self):
        port = self.gated(trust_forwarded_for=True)
        xff = lambda ip: {"X-Forwarded-For": f"203.0.113.9, {ip}"}
        for _ in range(pub.FAIL_PER_CLIENT):
            self.login(port, "nope", xff("198.51.100.1"))
        self.assertEqual(self.login(port, self.alice, xff("198.51.100.1"))[0].status, 429)
        self.assertEqual(self.login(port, self.alice, xff("198.51.100.2"))[0].status, 303)
        for i in range(pub.FAIL_GLOBAL):
            self.login(port, "nope", xff(f"192.0.2.{i}"))
        self.assertEqual(self.login(port, self.alice, xff("198.51.100.3"))[0].status, 429)

    def test_gated_routes_without_a_session(self):
        port = self.gated()
        r, body = self.req(port, "GET", "/")
        self.assertEqual(r.status, 401)
        self.assertIn(b"name='code'", body)
        for leak in (b"s3cret", b"New page", b"brain"):
            self.assertNotIn(leak, body)
        for path in ("/pub/notes/new.html", "/pub/missing.html", "/nowhere", "/?q=new"):
            r, body = self.req(port, "GET", path)
            if path.startswith("/?"):
                self.assertEqual(r.status, 401, path)
            else:
                self.assertEqual((r.status, r.getheader("Location")), (303, "/login"), path)
            self.assertNotIn(b"New page", body)
        r, body = self.req(port, "GET", "/login")
        self.assertEqual(r.status, 200)
        self.assertIn(b"action='/login'", body)
        r, body = self.req(port, "GET", "/healthz")
        self.assertEqual((r.status, body), (200, b"ok\n"))
        r, _ = self.req(port, "GET", "/", {"Cookie": "harness_pub=forged"})
        self.assertEqual(r.status, 401)
        cookie = self.cookie_of(self.login(port, self.alice)[0])
        r, body = self.req(port, "GET", "/pub/notes/new.html", {"Cookie": cookie})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Security-Policy").startswith("sandbox"))
        r, _ = self.req(port, "GET", "/login", {"Cookie": cookie})
        self.assertEqual((r.status, r.getheader("Location")), (303, "/"))

    def test_host_check_still_first(self):
        port = self.gated()
        for path in ("/login", "/_auth", "/healthz"):
            self.assertEqual(self.req(port, "GET", path, host="evil.example")[0].status, 421, path)
        r, _ = self.login(port, self.alice, host="evil.example")
        self.assertEqual(r.status, 421)

    def test_forward_auth_endpoint(self):
        port = self.gated(allow=("code.example.test",))
        r, _ = self.req(port, "GET", "/_auth", {"X-Harness-User": "alice"})
        self.assertEqual(r.status, 401)
        self.assertIsNone(r.getheader("X-Harness-User"))
        cookie = self.cookie_of(self.login(port, self.bob)[0])
        # Caddy's forward_auth keeps the original Host, here the opencode name.
        r, _ = self.req(port, "GET", "/_auth", {"Cookie": cookie, "X-Harness-User": "alice"},
                        host="code.example.test")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("X-Harness-User"), "bob")
        r, body = self.req(port, "GET", "/", {"Cookie": cookie, "X-Harness-User": "alice"})
        self.assertIn(b"title='signed in as bob'", body)

    def test_forward_auth_without_codes_is_open_and_names_nobody(self):
        port = self.start()
        r, _ = self.req(port, "GET", "/_auth", {"X-Harness-User": "alice"})
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.getheader("X-Harness-User"))
        self.assertEqual(self.req(port, "GET", "/login")[0].status, 404)

    def test_post_origin_checks(self):
        port = self.gated(allow=("pub.example.test",))
        body = urlencode({"code": self.alice})
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        refused = [{"Origin": "null"}, {"Origin": "http://evil.example"},
                   {"Origin": f"http://127.0.0.1:{port + 1}"},
                   {"Referer": f"http://127.0.0.1:{port}/login"},
                   {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}]
        for extra in refused:
            r, _ = self.req(port, "POST", "/login", dict(form, **extra), body)
            self.assertEqual(r.status, 403, extra)
            self.assertIsNone(r.getheader("Set-Cookie"))
        accepted = [{}, {"Sec-Fetch-Site": "same-origin"}, {"Origin": "https://pub.example.test"},
                    {"Origin": f"http://localhost:{port}"}]
        for extra in accepted:
            r, _ = self.req(port, "POST", "/login", dict(form, **extra), body)
            self.assertEqual(r.status, 303, extra)
        cookie = self.cookie_of(r)
        r, _ = self.req(port, "POST", "/logout", {"Cookie": cookie, "Origin": "null"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.req(port, "GET", "/", {"Cookie": cookie})[0].status, 200)

    def test_logout_ends_the_session(self):
        port = self.gated()
        cookie = self.cookie_of(self.login(port, self.alice)[0])
        r, _ = self.req(port, "POST", "/logout", {"Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
        cleared = self.set_cookies(r)
        self.assertEqual(set(cleared), {"harness_pub", "harness_pub_view"})
        for c in cleared.values():
            self.assertIn("Max-Age=0", c)
        self.assertEqual(self.req(port, "GET", "/", {"Cookie": cookie})[0].status, 401)

    def test_pub_view_cookie(self):
        # A sandboxed /pub/ page's own image and link loads are cross-site to
        # the browser, so only a SameSite=None cookie rides along (checked in
        # headless Chromium); it unlocks GET /pub/ and nothing else.
        port = self.gated()
        r, _ = self.login(port, self.alice)
        view = self.set_cookies(r)["harness_pub_view"]
        attrs = [a.strip() for a in view.split(";")[1:]]
        for want in ("Path=/pub/", "HttpOnly", "SameSite=None", "Secure"):
            self.assertIn(want, attrs)
        vc = view.split(";")[0]
        self.assertNotEqual(vc.split("=", 1)[1], self.cookie_of(r).split("=", 1)[1])
        r, body = self.req(port, "GET", "/pub/notes/new.html", {"Cookie": vc})
        self.assertEqual(r.status, 200)
        self.assertIn(b"New page", body)
        self.assertEqual(self.req(port, "GET", "/", {"Cookie": vc})[0].status, 401)
        self.assertEqual(self.req(port, "GET", "/_auth", {"Cookie": vc})[0].status, 401)
        self.assertEqual(self.req(port, "GET", "/pub/old.html", {"Cookie": vc.replace("_view", "")})[0].status, 303)
        pub.remove_code(self.codes, "alice")
        self.assertEqual(self.req(port, "GET", "/pub/old.html", {"Cookie": vc})[0].status, 303)

    def test_logout_ends_the_view_cookie_too(self):
        port = self.gated()
        r, _ = self.login(port, self.alice)
        cookie, vc = self.cookie_of(r), self.cookie_of(r, "harness_pub_view")
        self.req(port, "POST", "/logout", {"Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual(self.req(port, "GET", "/pub/old.html", {"Cookie": vc})[0].status, 303)

    def test_revoking_a_name_ends_its_sessions(self):
        port = self.gated()
        alice = self.cookie_of(self.login(port, self.alice)[0])
        bob = self.cookie_of(self.login(port, self.bob)[0])
        pub.remove_code(self.codes, "alice")
        self.assertEqual(self.req(port, "GET", "/", {"Cookie": alice})[0].status, 401)
        self.assertEqual(self.req(port, "GET", "/_auth", {"Cookie": alice})[0].status, 401)
        self.assertEqual(self.req(port, "GET", "/", {"Cookie": bob})[0].status, 200)
        self.assertEqual(self.login(port, self.alice)[0].status, 401)
        # Re-adding the name mints a new code; the old session stays dead.
        new = pub.add_code(self.codes, "alice")
        self.assertEqual(self.req(port, "GET", "/", {"Cookie": alice})[0].status, 401)
        self.assertEqual(self.login(port, new)[0].status, 303)

    def test_codes_added_live(self):
        port = self.gated()
        carol = pub.add_code(self.codes, "carol")
        self.assertEqual(self.login(port, carol)[0].status, 303)

    def test_missing_codes_file_fails_closed(self):
        port = self.start(access_codes=os.path.join(self.tmp.name, "nope"))
        self.assertEqual(self.req(port, "GET", "/")[0].status, 401)
        self.assertEqual(self.login(port, self.alice)[0].status, 401)


class CommandLineTest(LiveServer):
    def test_server_starts_with_every_flag(self):
        port = free_port()
        codes = os.path.join(self.tmp.name, "codes")
        code = pub.add_code(codes, "alice")
        ledger = os.path.join(self.tmp.name, "usage.jsonl")
        with open(ledger, "w") as f:
            f.write(json.dumps({"ts": time.time(), "model": "m", "prompt": 3, "completion": 4}) + "\n")
        args = [sys.executable, os.path.join(HERE, "pub.py"), "--port", str(port), "--vault", self.vault,
                "--vault-name", "brain", "--opencode-port", str(port + 1), "--password-file", self.pwfile,
                "--allow-host", "pub.example.test", "--access-codes", codes, "--secure-cookie",
                "--trust-forwarded-for", "--opencode-url", "https://code.example.test/",
                "--proxy-url", f"http://127.0.0.1:{free_port()}", "--mode", "slim=short tools",
                "--mode", "stock=as upstream", "--usage-ledger", ledger]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        for _ in range(100):
            try:
                r, body = self.get(port, "/healthz")
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("pub.py did not start")
        self.assertEqual(body, b"ok\n")
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/login", body=urlencode({"code": code}),
                  headers={"Host": "pub.example.test", "Origin": "https://pub.example.test",
                           "Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 303)
        cookie = [v for v in r.msg.get_all("Set-Cookie") if v.startswith("__Host-")][0].split(";")[0]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/", headers={"Host": "pub.example.test", "Cookie": cookie})
        text = c.getresponse().read().decode()
        c.close()
        for want in ("title='signed in as alice'", "href='https://code.example.test/'", "<code>slim</code>",
                     "Usage, last 24 h", "<span class='down'>down</span>"):
            self.assertIn(want, text)
        self.assertNotIn("s3cret", text)

    def test_bad_flags_are_rejected(self):
        for bad in (["--opencode-url", "javascript:alert(1)"], ["--proxy-url", "ftp://x"], ["--mode", "noequals"]):
            r = subprocess.run([sys.executable, os.path.join(HERE, "pub.py"), "--port", "1", *bad],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(r.returncode, 2, bad)


class IndexContentTest(LiveServer):
    def text(self, port, path="/"):
        r, body = self.get(port, path)
        self.assertEqual(r.status, 200)
        return body.decode()

    def test_opencode_url_has_no_token(self):
        port = self.start(opencode_url="https://code.example.test/")
        text = self.text(port)
        self.assertIn("href='https://code.example.test/'", text)
        self.assertNotIn("s3cret", text)
        self.assertIn("New terminal", text)

    def test_filter_and_cap(self):
        port = self.start()
        text = self.text(port, "/?q=NEW")
        self.assertIn("New page", text)
        self.assertNotIn("Old &amp; page", text)
        self.assertIn("1 of 2 pages match", text)
        text = self.text(port, "/?q=old.html")
        self.assertIn("Old &amp; page", text)
        self.assertNotIn("New page", text)
        text = self.text(port, "/?q=%3Cscript%3E")
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        old = pub.PAGE_CAP
        pub.PAGE_CAP = 1
        try:
            text = self.text(port, "/?q=page")
        finally:
            pub.PAGE_CAP = old
        self.assertIn("New page", text)
        self.assertIn("1 more not shown", text)

    def test_modes(self):
        port = self.start(modes=[("slim", "shorter tool descriptions <b>"), ("stock", "as upstream")])
        text = self.text(port)
        self.assertIn("<code>slim</code>", text)
        self.assertIn("shorter tool descriptions &lt;b&gt;", text)
        self.assertNotIn("Prompt modes", self.text(self.start()))

    def test_usage_from_ledger(self):
        ledger = os.path.join(self.tmp.name, "usage.jsonl")
        now = time.time()
        rows = [
            {"ts": now - 90000, "model": "a", "prompt": 1000, "completion": 1000,
             "remaining": {"credits-day": "1"}},
            {"ts": now - 7200, "model": "b", "prompt": 100, "completion": 20, "reasoning": 5, "cached": 50,
             "remaining": {}},
            "not json",
            {"ts": now - 60, "model": "a", "prompt": 10, "completion": 2, "reasoning": "x", "cached": None,
             "remaining": {"credits-day": "99", "tokens-minute": "5000"}},
            {"model": "no-ts", "prompt": 7},
        ]
        with open(ledger, "w") as f:
            for row in rows:
                f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
            f.write('{"ts": ')                       # a torn last line
        u = pub.read_ledger(ledger, now)
        self.assertEqual(u["day"], {"replies": 2, "prompt": 110, "completion": 22, "reasoning": 5,
                                    "cached": 50, "total": 132})
        self.assertEqual(u["hour"], {"replies": 1, "total": 12})
        self.assertEqual(u["models"], [("b", [1, 100, 20]), ("a", [1, 10, 2])])
        self.assertEqual(u["remaining"], [("credits-day", "99"), ("tokens-minute", "5000")])
        port = self.start(usage_ledger=ledger)
        text = self.text(port)
        self.assertIn("Usage, last 24 h", text)
        self.assertIn("<span class='v'>132</span>", text)
        self.assertIn("credits-day 99", text)
        empty = os.path.join(self.tmp.name, "empty.jsonl")
        open(empty, "w").close()
        self.assertIsNone(pub.read_ledger(empty))
        self.assertIsNone(pub.read_ledger(os.path.join(self.tmp.name, "missing")))
        self.assertNotIn("Usage", self.text(self.start(usage_ledger=empty)))

    def test_ledger_tail_only(self):
        ledger = os.path.join(self.tmp.name, "big.jsonl")
        now = time.time()
        line = json.dumps({"ts": now, "model": "m", "prompt": 1, "completion": 0}) + "\n"
        old = pub.LEDGER_TAIL
        pub.LEDGER_TAIL = len(line) * 3 + 5           # lands mid-line
        try:
            with open(ledger, "w") as f:
                f.write(line * 10)
            self.assertEqual(pub.read_ledger(ledger, now)["day"]["replies"], 3)
        finally:
            pub.LEDGER_TAIL = old

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_vault_activity(self):
        env = dict(os.environ, GIT_AUTHOR_NAME="Ann & Co", GIT_AUTHOR_EMAIL="a@x", GIT_COMMITTER_NAME="Ann",
                   GIT_COMMITTER_EMAIL="a@x", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        run = lambda *a: subprocess.run(["git", "-C", self.vault, *a], env=env, check=True,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port = self.start()
        self.assertNotIn("Vault activity", self.text(port))
        run("init", "-q")
        run("add", "-A")
        run("commit", "-q", "-m", "add <b>pages</b>")
        port = self.start()
        text = self.text(port)
        self.assertIn("Vault activity", text)
        self.assertIn("add &lt;b&gt;pages&lt;/b&gt;", text)
        self.assertIn("Ann &amp; Co", text)

    def test_proxy_status(self):
        port = self.start(proxy_url=f"http://127.0.0.1:{free_port()}")
        self.assertRegex(self.text(port), r"model proxy <span class='down'>down</span>")

        seen = []

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        fake = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        self.servers = getattr(self, "servers", []) + [fake]
        port = self.start(proxy_url=f"http://127.0.0.1:{fake.server_address[1]}/")
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Cookie": "harness_pub=x",
                                       "Authorization": "Basic eA=="})
        text = c.getresponse().read().decode()
        c.close()
        self.assertRegex(text, r"model proxy <span class='up'>up</span>")
        self.assertEqual(seen[0][0], "/health")
        sent = {k.lower() for k in seen[0][1]}
        self.assertFalse(sent & {"cookie", "authorization"}, sent)
        self.get(port, "/")
        self.assertEqual(len(seen), 1)                # cached


class HomeLiveTest(LiveServer):
    def test_home_is_live_and_its_regions_match_the_page(self):
        port = self.start()
        r, body = self.get(port, "/")
        text = body.decode()
        self.assertIn("id='live'", text)
        self.assertIn("data-live='/live'", text)
        self.assertIn("id='r-main'", text)
        self.assertIn("id='fresh'", text)
        self.assertIn("aria-current='page'>Home", text)
        nonce = re.search(r"script-src 'nonce-([^']+)'", r.getheader("Content-Security-Policy")).group(1)
        self.assertIn(f"<script nonce='{nonce}'>", text)
        sig = re.search(r"data-sig='([^']+)'", text).group(1)
        r, body = self.get(port, "/live")
        d = json.loads(body)
        self.assertEqual(d["sig"], sig)                  # nothing changed: the first poll swaps nothing
        self.assertIn(d["regions"]["main"], text)
        self.assertIsNone(r.getheader("X-Harness-Stale"))

    def test_filter_carries_into_the_poll(self):
        port = self.start()
        _, body = self.get(port, "/?q=new")
        self.assertIn("data-live='/live?q=new'", body.decode())
        _, body = self.get(port, "/live?q=new")
        main = json.loads(body)["regions"]["main"]
        self.assertIn("New page", main)
        self.assertNotIn("Old &amp; page", main)

    def test_vault_switch_shows_without_restart(self):
        state = os.path.join(self.tmp.name, "state")
        other = os.path.join(state, "vaults", "work")
        os.makedirs(os.path.join(other, "pub"))
        with open(os.path.join(other, "pub", "w.html"), "w") as f:
            f.write("<title>Work page</title>")
        port = self.start(state_dir=state)
        _, body = self.get(port, "/live")
        self.assertIn("New page", json.loads(body)["regions"]["main"])
        with open(os.path.join(state, "vault.active"), "w") as f:
            f.write("work\n")
        time.sleep(pub.VAULT_TTL + 0.2)
        _, body = self.get(port, "/live")
        d = json.loads(body)["regions"]
        self.assertIn("Work page", d["main"])
        self.assertIn("<b>work</b>", d["top"])

    def test_stale_header_once_serve_code_changes(self):
        cfg = pub.Config(0, self.vault, "brain", 0, self.pwfile, ())
        self.assertFalse(cfg.code_changed())
        cfg.started_code -= 100
        cfg._code_seen = (0.0, False)
        self.assertTrue(cfg.code_changed())


class VaultFixture(LiveServer):
    """A git vault under state/vaults with an ingest manifest, and serve on it."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp.name, "state")
        v = self.v = os.path.join(self.state, "vaults", "brain")
        files = {
            "wiki/code/api/server.md": "---\ntitle: API server\nrepo: api\n---\n# API server\n\n"
                                       "<!-- unit:code:api/server#1 sha:abc -->\nGoes to [[handlers]], "
                                       "[[cache|the cache]], [[nope]] and [p](../../notes/n.md).\n\n"
                                       "> [!warning] Careful\n> body\n\n<script>alert(1)</script>\n"
                                       "[x](javascript:alert(1)) ![i](pic.png)\n",
            "wiki/code/api/handlers.md": "# Handlers\n\nneedle in the handlers\n",
            "wiki/code/api/pic.png": "\x89PNG fake",
            "wiki/notes/n.md": "# Note\n",
            "wiki/cache.md": "# Cache\n",
            ".raw/sessions/s.md": "user: needle here\n",
            ".env": "TOKEN=needle-secret\n",
            "keys/id_rsa": "needle key\n",
        }
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(v, rel)), exist_ok=True)
            with open(os.path.join(v, rel), "w") as f:
                f.write(text)
        git = ["git", "-C", v, "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]
        subprocess.run(git[:3] + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A", "wiki", ".raw"], check=True)
        # Commit well in the past so the overview's git %ar fields read "N days ago"
        # (a stable granularity). A commit dated "now" reads "N seconds ago", which
        # ticks between the /vault render and the separate /vault/live poll and makes
        # their signatures disagree — a flake, not a real change.
        then = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 13 * 86400))
        env = dict(os.environ, GIT_AUTHOR_DATE=then, GIT_COMMITTER_DATE=then)
        subprocess.run(git + ["commit", "-q", "-m", "ingest: 3 written"], check=True, env=env)
        self.sha = subprocess.run(git[:3] + ["rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        os.makedirs(os.path.join(v, ".harness"))
        with open(os.path.join(v, ".harness", "ingest.json"), "w") as f:
            json.dump({"version": 1, "repos": {"api": v}, "units": {
                "code:api/server#1": {"page": "wiki/code/api/server.md", "status": "ok", "at": "2026-10-27T10:00:00Z"},
                "code:api/cache#1": {"page": "wiki/cache.md", "status": "failed", "attempts": 3,
                                     "at": "2026-10-27T10:00:00Z", "problems": ["no page <b>"]}}}, f)
        with open(os.path.join(self.state, "vaults.list"), "w") as f:
            f.write("brain\tlocal\nwork\thttps://u:tok@example.com/w.git\n")
        with open(os.path.join(self.state, "vault.active"), "w") as f:
            f.write("brain\n")
        self.port = self.start(vault="", state_dir=self.state)

    def page(self, path, code=200):
        r, body = self.get(self.port, path)
        self.assertEqual(r.status, code, path)
        return body.decode()


class VaultPagesTest(VaultFixture):
    """The Vault section: read-only pages."""

    def test_overview_shows_contents_ingest_and_vaults(self):
        text = self.page("/vault")
        self.assertIn("aria-current='page'>Vault", text)
        self.assertIn("wiki pages", text)
        self.assertIn("1 failed", text)
        self.assertIn("ingest: 3 written", text)
        self.assertIn("https://example.com/w.git", text)
        self.assertNotIn("tok@", text)                       # no credentials from a remote URL
        self.assertIn("img-src 'self'", self.get(self.port, "/vault")[0].getheader("Content-Security-Policy"))
        sig = re.search(r"data-sig='([^']+)'", text).group(1)
        self.assertEqual(json.loads(self.get(self.port, "/vault/live")[1])["sig"], sig)
        ing = self.page("/vault/ingest")
        self.assertIn("no page &lt;b&gt;", ing)
        self.assertIn("code:api/cache#1", ing)

    def test_page_renders_markdown_safely_and_resolves_links(self):
        text = self.page("/vault/browse/wiki/code/api/server.md")
        self.assertNotIn("<script>alert", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertNotIn('href="javascript:', text)
        self.assertIn('href="/vault/browse/wiki/code/api/handlers.md"', text)
        self.assertIn('href="/vault/browse/wiki/cache.md">the cache</a>', text)
        self.assertIn("class=\"missing\"", text)
        self.assertIn('href="/vault/browse/wiki/notes/n.md"', text)
        self.assertIn("src=\"/vault/file/wiki/code/api/pic.png\"", text)
        self.assertIn("callout-warning", text)
        self.assertNotIn("unit:code:api", text)                # generated markers are not shown
        self.assertIn("<dt>repo</dt><dd>api</dd>", text)
        self.assertIn(f"/vault/commit/{self.sha[:7]}", text)

    def test_paths_stay_inside_and_secrets_stay_hidden(self):
        for bad in ("/vault/browse/../../vaults.list", "/vault/browse/%2e%2e/%2e%2e/vaults.list",
                    "/vault/browse/.git/config", "/vault/browse/wiki/..%2f..%2f..%2fvaults.list",
                    "/vault/file/.env", "/vault/file/wiki/notes/n.md", "/vault/commit/HEAD",
                    "/vault/commit/--output=x"):
            self.assertNotIn("tok@", self.page(bad, 404), bad)
        os.symlink(self.state, os.path.join(self.v, "out"))
        self.page("/vault/browse/out/vaults.list", 404)
        self.assertIn("looks like a secret", self.page("/vault/browse/.env"))
        self.assertNotIn("TOKEN", self.page("/vault/browse/.env"))
        self.assertIn("looks like a secret", self.page("/vault/browse/keys/id_rsa"))
        r, body = self.get(self.port, "/vault/file/wiki/code/api/pic.png")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "image/png"))
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))

    def test_search_folder_and_commit(self):
        text = self.page("/vault/search?q=needle")
        self.assertIn("wiki/code/api/handlers.md", text)
        self.assertIn(".raw/sessions/s.md", text)
        self.assertNotIn("needle-secret", text)
        self.assertNotIn("id_rsa", text)
        self.assertLess(text.index("handlers.md"), text.index("sessions/s.md"))    # wiki first
        self.assertIn("Nothing matches", self.page("/vault/search?q=zzzqqq"))
        self.assertIn("&lt;img", self.page("/vault/search?q=%3Cimg"))
        listing = self.page("/vault/browse/wiki/code/api")
        self.assertIn("handlers.md", listing)
        self.assertIn("<small>Handlers</small>", listing)
        commit = self.page(f"/vault/commit/{self.sha[:10]}")
        self.assertIn("ingest: 3 written", commit)
        self.assertIn("needle in the handlers", commit)
        self.page("/vault/commit/0000000", 404)

    def test_use_switches_the_active_vault(self):
        os.makedirs(os.path.join(self.state, "vaults", "work", ".git"))
        h = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/x-www-form-urlencoded"}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/vault/use", urlencode({"name": "nosuch"}), {**h, "Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 409)
        c.request("POST", "/vault/use", urlencode({"name": "work"}), {**h, "Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 303)
        with open(os.path.join(self.state, "vault.active")) as f:
            self.assertEqual(f.read(), "work\n")
        self.assertIn("<b>work</b>", self.page("/vault").replace("<h2>work</h2>", "<b>work</b>"))

    def test_no_vault(self):
        os.remove(os.path.join(self.state, "vault.active"))
        time.sleep(pub.VAULT_TTL + 0.2)
        self.assertIn("No vault", self.page("/vault"))
        r, _ = self.get(self.port, "/vault/browse/wiki")
        self.assertEqual(r.status, 303)


class OpencodeLinkTest(unittest.TestCase):
    """The run viewer's "Open in opencode web": opencode's /server/<base64url origin>/session/<id>."""

    def cfg(self, **kw):
        c = pub.Config(8770, "", "", kw.pop("port", 0), "", (), **kw)
        c.opencode_password = lambda: "pw/1"
        return c

    def test_port_link_carries_the_login_and_only_the_hosts_sessions(self):
        link = pub.opencode_session_link(self.cfg(port=4096), "10.0.0.5")
        key = base64.urlsafe_b64encode(b"http://10.0.0.5:4096").decode().rstrip("=")
        self.assertEqual(link("host", "ses_1"), f"http://opencode:pw%2F1@10.0.0.5:4096/server/{key}/session/ses_1")
        self.assertEqual(link("container", "ses_1"), "")

    def test_url_link_keys_on_the_origin_without_login_or_default_port(self):
        link = pub.opencode_session_link(self.cfg(opencode_url="https://u:p@OC.example.com:443/"), "x")
        key = base64.urlsafe_b64encode(b"https://oc.example.com").decode().rstrip("=")
        self.assertEqual(link("host", "ses_2"), f"https://u:p@OC.example.com:443/server/{key}/session/ses_2")

    def test_no_opencode_no_link(self):
        self.assertIsNone(pub.opencode_session_link(self.cfg(), "x"))


if __name__ == "__main__":
    unittest.main()


FAKE_HARNESS = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$LOG"
case "$*" in
  "config list --tsv")
    printf 'PROXY_API_URL\thttps://llm.example\t0\tupstream URL\n'
    printf 'PROXY_API_KEY\t(set, 9 chars)\t1\tupstream key\n'
    printf 'DEFAULT_MODEL_NAME\tbig\t0\tdefault model\n'
    printf 'HTTPS_PROXY\thttp://al:pw9@proxy:3128\t0\tproxy\n' ;;
  "net list") printf 'allowlist (/x):\n  pypi.org            # pip\n  gitlab.example.com  [git-push]\n' ;;
  "mcp list --available") printf 'NAME STATE STATUS\nserena installed-enabled running\ngh available -\n' ;;
  doctor) printf '\033[1m[deps]\033[0m\n  \xe2\x9c\x93 git  git version 2\n  \xe2\x9c\x97 docker  not reachable <b>\n' ;;
  "config set DEFAULT_MODEL_NAME bad") echo "harness: refused" >&2; exit 1 ;;
  "net allow"*) echo "added ${3}" ;;
  "ingest --forget"*) echo "forgot ${3}: 2 pages removed" ;;
  ingest*) echo "ingesting"; exec sleep 30 ;;
  "net deny"*) echo "removed ${3}" ;;
  *) echo ok ;;
esac
'''


class SettingsPageTest(LiveServer):
    """/settings drives the harness commands; a fake harness records what it was asked."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp.name, "inst", "state")
        os.makedirs(self.state)
        self.harness = os.path.join(self.tmp.name, "harness")
        with open(self.harness, "w") as f:
            f.write(FAKE_HARNESS)
        self.log = os.path.join(self.tmp.name, "log")
        os.environ["LOG"] = self.log
        self.addCleanup(os.environ.pop, "LOG", None)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def post(self, port, path, fields, cookie=""):
        h = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
             "Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            h["Cookie"] = cookie
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request("POST", path, urlencode(fields), h)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r, body

    def test_page_shows_every_part_and_no_secret(self):
        port = self.start(vault="", state_dir=self.state, harness=self.harness)
        r, body = self.get(port, "/settings")
        text = body.decode()
        self.assertEqual(r.status, 200)
        self.assertIn("aria-current='page'>Settings", text)
        # The page comes up without running harness; the slow parts come from /settings/panels.
        self.assertEqual([c for c in self.calls() if "list" in c], [])
        self.assertEqual(text.count("class='muted set-wait'"), 3)
        self.assertIn("Access codes are off", text)
        self.assertIn(f"<dt>state</dt><dd>{self.state}</dd>", text)
        self.assertIn("brain, not cloned here yet", text)
        r, body = self.get(port, "/settings/panels")
        self.assertEqual(r.status, 200)
        regions = json.loads(body)["regions"]
        self.assertEqual(set(regions), {"set-model", "set-network", "set-mcp"})
        self.assertNotIn("r-set-", body.decode())        # inner HTML: the page keeps its region div
        self.assertEqual(sorted(self.calls()), ["config list --tsv", "mcp list --available", "net list"])
        # Within GATHER_TTL the page shows them at once, without asking harness again.
        r, body = self.get(port, "/settings")
        text = body.decode()
        self.assertNotIn("class='muted set-wait'", text)
        self.assertEqual(len(self.calls()), 3)
        for part in regions.values():
            self.assertIn(part, text)
        self.assertIn("value='big'", text)
        self.assertIn("type='password' name='v_PROXY_API_KEY'", text)
        self.assertIn("type='password' name='v_HTTPS_PROXY'", text)     # a proxy URL with a password
        self.assertNotIn("pw9", text)
        self.assertIn("gitlab.example.com", text)
        self.assertIn("git push", text)
        self.assertIn("serena", text)
        self.assertIn(">Disable<", text)
        self.assertIn(">Install<", text)

    def test_a_change_here_forgets_what_was_read(self):
        port = self.start(vault="", state_dir=self.state, harness=self.harness)
        self.get(port, "/settings/panels")
        self.post(port, "/settings/net", {"act": "deny", "host": "pypi.org"})
        _, body = self.get(port, "/settings")
        self.assertIn("class='muted set-wait'", body.decode())

    def test_save_sets_only_what_changed(self):
        port = self.start(vault="", state_dir=self.state, harness=self.harness)
        r, _ = self.post(port, "/settings/config", {"v_PROXY_API_URL": "https://llm.example", "v_DEFAULT_MODEL_NAME": "other",
                                                    "v_PROXY_API_KEY": "", "v_HTTPS_PROXY": ""})
        self.assertEqual(r.status, 303)
        self.assertIn("done=Saved+DEFAULT_MODEL_NAME", r.getheader("Location"))
        sets = [c for c in self.calls() if c.startswith("config set")]
        self.assertEqual(sets, ["config set DEFAULT_MODEL_NAME other"])
        self.post(port, "/settings/config", {"v_PROXY_API_KEY": "sk-new", "clear_HTTPS_PROXY": "1"})
        sets = [c for c in self.calls() if c.startswith("config set")]
        self.assertEqual(sets[1:], ["config set PROXY_API_KEY sk-new", "config set HTTPS_PROXY "])
        r, _ = self.post(port, "/settings/config", {"v_DEFAULT_MODEL_NAME": "a\nb"})
        self.assertIn("bad=", r.getheader("Location"))
        r, _ = self.post(port, "/settings/config", {"v_DEFAULT_MODEL_NAME": "bad"})
        self.assertIn("refused", unquote(r.getheader("Location")).replace("+", " "))

    def test_net_mcp_and_doctor(self):
        port = self.start(vault="", state_dir=self.state, harness=self.harness)
        r, _ = self.post(port, "/settings/net", {"act": "allow", "host": "Git.Example.com", "push": "1"})
        self.assertIn("done=Added", r.getheader("Location"))
        r, _ = self.post(port, "/settings/net", {"act": "allow", "host": "a b;rm"})
        self.assertIn("bad=", r.getheader("Location"))
        self.post(port, "/settings/net", {"act": "deny", "host": "pypi.org"})
        r, _ = self.post(port, "/settings/mcp", {"act": "enable", "name": "gh"})
        self.assertEqual(r.status, 303)
        r, _ = self.post(port, "/settings/mcp", {"act": "uninstall", "name": "gh"})
        self.assertIn("bad=", r.getheader("Location"))
        r, body = self.post(port, "/settings/system", {"act": "doctor"})
        self.assertEqual(r.status, 200)
        self.assertIn("class='bad'", body)
        self.assertIn("not reachable &lt;b&gt;", body)
        self.assertNotIn("\x1b", body)
        calls = self.calls()
        self.assertIn("net allow git.example.com --git-push", calls)
        self.assertIn("net deny pypi.org", calls)
        self.assertIn("mcp enable gh", calls)
        self.assertNotIn("mcp uninstall gh", calls)
        self.assertFalse(any("rm" in c for c in calls))

    def test_access_codes_are_managed_here_when_on(self):
        codes = os.path.join(self.tmp.name, "codes")
        alice = pub.add_code(codes, "alice")
        port = self.start(vault="", state_dir=self.state, harness=self.harness, access_codes=codes)
        r, _ = self.post(port, "/settings/net", {"act": "deny", "host": "pypi.org"})
        self.assertEqual(r.status, 401)
        r, body = self.get(port, "/settings/panels")
        self.assertNotIn(b"big", body)
        self.assertNotIn("config list --tsv", self.calls())
        r, _ = self.post(port, "/login", {"code": alice})
        cookie = r.getheader("Set-Cookie").split(";")[0]
        r, body = self.post(port, "/settings/access", {"act": "add", "name": "bob"}, cookie)
        self.assertEqual(r.status, 200)
        code = re.search(r"class='newcode'>([^<]+)<", body).group(1)
        self.assertEqual(pub.parse_codes(open(codes).read()) >= {("bob", pub.code_hash(code))}, True)
        r, _ = self.post(port, "/settings/access", {"act": "remove", "name": "alice"}, cookie)
        self.assertIn("bad=", r.getheader("Location"))
        r, _ = self.post(port, "/settings/access", {"act": "remove", "name": "bob"}, cookie)
        self.assertIn("done=", r.getheader("Location"))
        self.assertEqual({n for n, _ in pub.parse_codes(open(codes).read())}, {"alice"})


class IngestControlsTest(VaultFixture):
    """/vault/ingest/start, stop and forget run harness ingest (a fake one here)."""

    def setUp(self):
        super().setUp()
        self.harness = os.path.join(self.tmp.name, "harness")
        with open(self.harness, "w") as f:
            f.write(FAKE_HARNESS)
        self.log = os.path.join(self.tmp.name, "log")
        os.environ["LOG"] = self.log
        self.addCleanup(os.environ.pop, "LOG", None)
        self.servers[-1].shutdown()
        self.port = self.start(vault="", state_dir=self.state, harness=self.harness)
        self.addCleanup(lambda: vault_view.stop_ingest(self.v))

    post = SettingsPageTest.post
    calls = SettingsPageTest.calls

    def wait_call(self, line):
        for _ in range(100):
            if line in self.calls():
                return
            time.sleep(0.05)
        self.assertIn(line, self.calls())

    def test_start_refuse_while_running_stop_and_forget(self):
        page = self.page("/vault/ingest")
        self.assertIn("Ingest what changed", page)
        self.assertIn("Retry failed sections too", page)
        self.assertIn(">Forget<", page)
        r, _ = self.post(self.port, "/vault/ingest/start", {"what": "all", "dir": "/no/such/dir"})
        self.assertIn("bad=", r.getheader("Location"))
        r, _ = self.post(self.port, "/vault/ingest/start", {"what": "retry", "max": "3"})
        self.assertIn("done=", r.getheader("Location"))
        self.wait_call("ingest --retry-failed --max 3")
        r, _ = self.post(self.port, "/vault/ingest/start", {"unit": "code:api/cache#1"})
        self.assertIn("already+running", r.getheader("Location"))
        for _ in range(50):
            page = self.page("/vault/ingest")
            if "ingesting" in page:
                break
            time.sleep(0.1)
        self.assertIn(">Stop<", page)
        self.assertIn("ingesting", page)                      # the output's tail
        r, _ = self.post(self.port, "/vault/ingest/stop", {})
        self.assertIn("done=", r.getheader("Location"))
        vault_view._started["proc"].wait(5)
        r, _ = self.post(self.port, "/vault/ingest/start", {"unit": "code:api/cache#1"})
        self.assertIn("done=", r.getheader("Location"))
        self.wait_call("ingest --unit code:api/cache#1")
        vault_view.stop_ingest(self.v)
        vault_view._started["proc"].wait(5)
        r, _ = self.post(self.port, "/vault/ingest/forget", {"name": "api"})
        self.assertIn("done=Forgot+api", r.getheader("Location"))
        r, _ = self.post(self.port, "/vault/ingest/forget", {"name": "../x"})
        self.assertIn("bad=", r.getheader("Location"))
