"""Tests for serve/pub.py against a live server on an ephemeral loopback port.

Run: python3 -m unittest serve/test_pub.py (tests/unit_serve_test.sh does).
"""

import base64
import http.client
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pub  # noqa: E402


class PubServerTest(unittest.TestCase):
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

    def start(self, vault=None, allow=()):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = srv.server_address[1]
        cfg = pub.Config(port, self.vault if vault is None else vault, "brain", port + 1, self.pwfile, allow)
        srv.RequestHandlerClass = pub.make_handler(cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers = getattr(self, "servers", []) + [srv]
        return port

    def get(self, port, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_index_lists_pages_newest_first_and_links_opencode(self):
        port = self.start()
        r, body = self.get(port, "/")
        self.assertEqual(r.status, 200)
        text = body.decode()
        self.assertIn("New page", text)
        self.assertIn("Old &amp; page", text)
        self.assertLess(text.index("New page"), text.index("Old &amp; page"))
        self.assertNotIn("hidden", text)
        token = base64.b64encode(b"opencode:s3cret").decode()
        self.assertIn(f"http://127.0.0.1:{port + 1}/?auth_token=", text)
        self.assertIn(token.replace("=", "%3D"), text)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_wrong_host_is_refused(self):
        port = self.start()
        r, body = self.get(port, "/", host=f"evil.example:{port}")
        self.assertEqual(r.status, 421)
        self.assertNotIn(b"auth_token", body)

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
        self.assertIn(b"no vault selected", body)
        self.assertIn(b"opencode web", body)
        r, _ = self.get(port, "/pub/old.html")
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
