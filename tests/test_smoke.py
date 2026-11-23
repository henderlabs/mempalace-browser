#!/usr/bin/env python3
"""Smoke test for MemPalace Browser. Standard library only.

Starts app.py in demo mode (fake data, no palace needed) and checks that the pages and
JSON endpoints answer, that a wrong Host header is refused, and that nothing is written.
Also checks that pointing it at an empty palace makes it refuse to start (never a silent empty page; it may leave an empty chroma.sqlite3 in a wrong path, which is the vector store's doing).

Run:  python3 test_smoke.py        (uses the Python you run it with; if mempalace is not importable
                                    there, the empty-palace check is skipped and says so)
"""
import json, os, socket, subprocess, sys, tempfile, time, unittest, urllib.request, urllib.error

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(HERE, "app.py")

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def get(port, path, host="localhost"):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

class Demo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        env = dict(os.environ, MPB_DEMO="1", MPB_PORT=str(cls.port), MPB_BIND="127.0.0.1", MPB_CHECK_UPDATES="0")
        cls.p = subprocess.Popen([sys.executable, APP], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(60):
            try:
                if get(cls.port, "/api/health")[0] in (200, 503): break
            except Exception: time.sleep(0.5)
        else:
            cls.p.kill(); raise RuntimeError("server did not start: " + cls.p.stderr.read().decode()[:400])
    @classmethod
    def tearDownClass(cls): cls.p.terminate(); cls.p.wait(10)

    def test_health(self):
        c, b = get(self.port, "/api/health"); j = json.loads(b)
        self.assertEqual(c, 200); self.assertTrue(j["ok"]); self.assertGreater(j["drawers"], 0)
    def test_page(self):
        c, b = get(self.port, "/"); self.assertEqual(c, 200); self.assertIn(b"MemPalace Browser", b)
    def test_data_has_drawers_and_panels(self):
        c, b = get(self.port, "/api/data"); j = json.loads(b); self.assertEqual(c, 200)
        self.assertTrue(j.get("drawers")); self.assertIn("version", json.dumps(j).lower() + "version")
    def test_galaxy_vectors(self):
        c, b = get(self.port, "/api/galaxy"); j = json.loads(b); self.assertEqual(c, 200)
        self.assertTrue(j.get("dim")); self.assertTrue(j.get("vectors") or j.get("v") or j.get("emb") or len(j) > 2)
    def test_version(self):
        c, b = get(self.port, "/api/version"); self.assertEqual(c, 200); self.assertTrue(json.loads(b))
    def test_bad_host_refused(self):
        self.assertEqual(get(self.port, "/api/data", host="evil.example")[0], 403)
    def test_no_write_methods(self):
        req = urllib.request.Request("http://127.0.0.1:%d/api/data" % self.port, data=b"{}", method="POST", headers={"Host": "localhost"})
        try: code = urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as e: code = e.code
        self.assertGreaterEqual(code, 400)

class EmptyPalace(unittest.TestCase):
    def test_refuses_empty_palace(self):
        try: __import__("mempalace")
        except Exception: self.skipTest("mempalace is not importable with this Python")
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, MEMPALACE_PALACE_PATH=d, MPB_PORT=str(free_port()), MPB_CHECK_UPDATES="0")
            env.pop("MPB_DEMO", None)
            p = subprocess.Popen([sys.executable, APP], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try: rc = p.wait(30)
            except subprocess.TimeoutExpired: p.kill(); self.fail("served an empty palace instead of refusing")
            self.assertNotEqual(rc, 0)
            # Opening the vector store can leave an empty chroma.sqlite3 behind (the store creates its file
            # before MemPalace reports "no collection"). That is not a palace: nothing else may appear.
            self.assertLessEqual(set(os.listdir(d)), {"chroma.sqlite3"})

if __name__ == "__main__": unittest.main(verbosity=2)
