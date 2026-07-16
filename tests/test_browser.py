#!/usr/bin/env python3
"""
Tests for MemPalace Browser.

Runs the real server in demo mode, so nothing here needs MemPalace, a vector
database, or a palace. Stdlib only — `python3 -m unittest discover tests`.

These cover the things that would be genuinely bad to get wrong: the Host
allow-list (which is what makes "no auth because localhost" true rather than
wishful), HTML escaping of attacker-controlled input, and the rule that the
program never claims a check succeeded when it did not.
"""

import importlib.util
import os
import pathlib
import threading
import unittest
import urllib.error
import urllib.request

# Must be set before importing app: they are read at module scope.
os.environ["MPB_DEMO"] = "1"
os.environ["MPB_CHECK_UPDATES"] = "0"
os.environ["MPB_ALLOWED_HOSTS"] = "palace.test"

_APP = pathlib.Path(__file__).resolve().parent.parent / "app.py"
_spec = importlib.util.spec_from_file_location("mpb_app", _APP)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


def setUpModule():
    # The server's access log is correct behaviour but pure noise in CI.
    app.Handler.log_message = lambda *a, **k: None


def get(path, host=None, port=None):
    """Return (status, body). Never raises on 4xx/5xx."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if host is not None:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class ServerTestCase(unittest.TestCase):
    """Boots the real server on an ephemeral port for the whole class."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = app.Server(("127.0.0.1", 0), app.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, host=None):
        return get(path, host=host, port=self.port)


class TestVersionParsing(unittest.TestCase):
    def test_orders_releases(self):
        self.assertLess(app._parse_version("3.4.9"), app._parse_version("3.5.0"))
        self.assertLess(app._parse_version("3.5.0"), app._parse_version("3.6.0"))
        self.assertEqual(app._parse_version("3.5.0"), app._parse_version("3.5.0"))

    def test_stops_at_non_numeric(self):
        # "0.0.0-demo" must not explode, and must not outrank a real release.
        self.assertEqual(app._parse_version("0.0.0-demo"), (0, 0, 0))
        self.assertLess(app._parse_version("0.0.0-demo"), app._parse_version("3.5.0"))

    def test_survives_junk(self):
        for junk in ("", None, "not-a-version", "..."):
            self.assertIsInstance(app._parse_version(junk), tuple)


class TestVersionHonesty(unittest.TestCase):
    """The program must never imply a result it does not have."""

    def test_disabled_says_disabled_not_current(self):
        v = app.version_info()
        self.assertEqual(v["status"], "disabled")
        self.assertIsNone(v["latest"])
        self.assertNotEqual(v["status"], "current")

    def test_unreachable_pypi_is_unknown_not_current(self):
        orig_check, orig_url = app.CHECK_UPDATES, app.PYPI_URL
        try:
            app.CHECK_UPDATES = True
            app.PYPI_URL = "http://127.0.0.1:1/nope"  # nothing listens here
            app._pypi_cache.update(at=0.0, latest=None, error=None)
            v = app.version_info(force=True)
            self.assertEqual(v["status"], "unknown")
            self.assertNotEqual(v["status"], "current")
            self.assertIsNotNone(v["error"])
        finally:
            app.CHECK_UPDATES, app.PYPI_URL = orig_check, orig_url
            app._pypi_cache.update(at=0.0, latest=None, error=None)


class TestHostAllowList(ServerTestCase):
    """The DNS-rebinding defence. A bypass here defeats the whole security model."""

    ALLOWED = ["localhost", "localhost:8080", "127.0.0.1", "LOCALHOST",
               "[::1]", "[::1]:8080", "palace.test", "palace.test:8080",
               "  localhost  "]

    # Every one of these must fail closed.
    REJECTED = ["evil.example", "evil.example:8080", "localhost.",
                "localhost.evil.example", "127.0.0.1.evil.example",
                "palace.test.evil.example", "localhost:8080:evil",
                "localhost evil.example", "", "evil.example:8080:localhost"]

    def test_allowed_hosts_pass(self):
        for h in self.ALLOWED:
            with self.subTest(host=h):
                status, _ = self.get("/api/health", host=h)
                self.assertEqual(status, 200, f"legitimate host rejected: {h!r}")

    def test_rejected_hosts_fail_closed(self):
        for h in self.REJECTED:
            with self.subTest(host=h):
                status, _ = self.get("/api/data", host=h)
                self.assertEqual(status, 403, f"HOST BYPASS: {h!r} was allowed")

    def test_rejection_does_not_leak_drawer_data(self):
        _, body = self.get("/api/data", host="evil.example")
        self.assertNotIn("Orchard", body)
        self.assertNotIn("drawer_", body)

    def test_rejection_does_not_leak_allowed_host_list(self):
        _, body = self.get("/", host="evil.example")
        self.assertNotIn("palace.test", body)


class TestRefusalPage(ServerTestCase):
    def test_browser_gets_html_api_gets_json(self):
        _, page = self.get("/", host="evil.example")
        self.assertIn("<!doctype html>", page.lower())
        _, api = self.get("/api/data", host="evil.example")
        self.assertIn('"error"', api)
        self.assertNotIn("<!doctype", api.lower())

    def test_page_is_actionable(self):
        _, page = self.get("/", host="myhost.example")
        self.assertIn("MPB_ALLOWED_HOSTS=myhost.example", page)

    def test_host_header_is_escaped(self):
        _, page = self.get("/", host='x"><script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_placeholders_are_not_resubstituted(self):
        # Sending our own template markers must not re-enter substitution.
        _, page = self.get("/", host="{{NAME}}")
        self.assertIn("{{NAME}}", page)
        _, page = self.get("/", host="{{HOST}}")
        self.assertIn("{{HOST}}", page)


class TestEndpoints(ServerTestCase):
    def test_index_serves(self):
        status, body = self.get("/", host="localhost")
        self.assertEqual(status, 200)
        self.assertIn("MemPalace", body)

    def test_health_reports_real_state(self):
        status, body = self.get("/api/health", host="localhost")
        self.assertEqual(status, 200)
        self.assertIn('"ok": true', body)
        self.assertIn('"drawers"', body)

    def test_unknown_route_404s(self):
        status, _ = self.get("/nope", host="localhost")
        self.assertEqual(status, 404)

    def test_data_has_drawers(self):
        import json
        _, body = self.get("/api/data", host="localhost")
        d = json.loads(body)
        self.assertGreater(d["count"], 0)
        self.assertEqual(d["count"], len(d["drawers"]))
        for k in ("wing", "room", "filed_at", "content", "meta"):
            self.assertIn(k, d["drawers"][0])

    def test_refresh_query_is_parsed_not_substring_matched(self):
        import json
        _, a = self.get("/api/data", host="localhost")
        # A query merely CONTAINING "refresh=1" must not force a re-read.
        _, b = self.get("/api/data?x=notrefresh=1", host="localhost")
        self.assertEqual(json.loads(a)["read_at"], json.loads(b)["read_at"])


class TestStorageMessaging(unittest.TestCase):
    """Never print local disk numbers for drawers that live on another host."""

    def test_remote_backends_report_unavailable_not_zero(self):
        orig_demo, orig_backend = app.DEMO, app.BACKEND
        try:
            app.DEMO = False
            for backend in ("pgvector", "qdrant"):
                with self.subTest(backend=backend):
                    app.BACKEND = backend
                    s = app.storage_info()
                    self.assertFalse(s["available"])
                    self.assertIn("remotely", s["reason"])
                    self.assertNotIn("disk_total", s)
        finally:
            app.DEMO, app.BACKEND = orig_demo, orig_backend

    def test_demo_reports_synthetic_figures(self):
        # Demo storage is synthetic, like the demo drawers — a panel that only
        # ever reads "n/a" cannot demonstrate what it is for. The banner and the
        # palace path both say DEMO, so nothing here is claiming to be real.
        s = app.storage_info()
        self.assertTrue(s["available"])
        self.assertEqual(s["backend"], "demo")
        self.assertGreater(s["disk_total"], 0)

    def test_demo_never_touches_the_filesystem(self):
        # The point of demo mode: no palace is read. If this ever starts walking
        # a real directory, the CI machine (which has no palace) would notice.
        self.assertTrue(app.DEMO)
        self.assertEqual(app.DATA_PATH, "")


class TestReadOnly(unittest.TestCase):
    def test_no_create_true_anywhere_in_source(self):
        """create=True would let a wrong path silently manufacture a palace."""
        src = _APP.read_text(encoding="utf-8")
        self.assertNotIn("create=True", src)
        self.assertIn("create=False", src)

    def test_no_write_methods_are_served(self):
        self.assertFalse(hasattr(app.Handler, "do_POST"))
        self.assertFalse(hasattr(app.Handler, "do_PUT"))
        self.assertFalse(hasattr(app.Handler, "do_DELETE"))
        self.assertFalse(hasattr(app.Handler, "do_PATCH"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
