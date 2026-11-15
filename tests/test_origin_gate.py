"""The origin and host gate on Studio's server (server-origin-host-gate).

    python3 -m unittest tests/test_origin_gate.py

Unit tests of origin_gate's two checks, then one end-to-end run: a real
serve_media.py on a free port, pointed at a temporary project
(STUDIO_PROJECT) and a temporary port file (STUDIO_PORT_FILE), so it never
touches the live server's studio-port.json or the open project.
"""
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from origin_gate import host_allowed, origin_allowed  # noqa: E402

EVIL = "https://evil.example"


class OriginAllowed(unittest.TestCase):
    def test_loopback_origins_on_any_port(self):
        for o in ("http://127.0.0.1:5173", "http://127.0.0.1:65433", "http://localhost:3000",
                  "http://[::1]:8080", "http://127.0.0.1"):
            self.assertTrue(origin_allowed(o), o)

    def test_bram_pane(self):
        self.assertTrue(origin_allowed("bramapp://localhost"))

    def test_foreign_and_malformed(self):
        for o in (EVIL, "http://evil.example", "http://127.0.0.1.evil.example",
                  "http://localhost.evil.example:8765", "https://127.0.0.1:8765",
                  "http://user@127.0.0.1:8765", "http://127.0.0.1:8765/path",
                  "http://127.0.0.1:notaport", "null", "", "file://", "bramapp://evil"):
            self.assertFalse(origin_allowed(o), o)


class HostAllowed(unittest.TestCase):
    def test_loopback_on_this_port(self):
        for h in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            self.assertTrue(host_allowed(h, 8765), h)

    def test_missing_host(self):
        self.assertTrue(host_allowed(None, 8765))
        self.assertTrue(host_allowed("", 8765))

    def test_wrong_port_or_name(self):
        for h in ("127.0.0.1:8766", "127.0.0.1", "evil.example:8765",
                  "127.0.0.1.evil.example:8765", "x@127.0.0.1:8765", "127.0.0.1:bad"):
            self.assertFalse(host_allowed(h, 8765), h)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        project = os.path.join(cls.tmp.name, "project")
        os.makedirs(os.path.join(project, "media"))
        os.makedirs(os.path.join(project, "work"))
        with open(os.path.join(project, "project.json"), "w") as f:
            json.dump({"name": "gate test", "genre": "screencast", "sources": []}, f)
        # As create_project_folder makes it: a studio.db from schema.sql.
        with sqlite3.connect(os.path.join(project, "studio.db")) as db, \
                open(os.path.join(REPO, "schema.sql")) as f:
            db.executescript(f.read())
        cls.port_file = os.path.join(cls.tmp.name, "studio-port.json")
        cls.port = free_port()
        env = {**os.environ, "STUDIO_PROJECT": project, "STUDIO_PORT_FILE": cls.port_file}
        env.pop("STUDIO_PORT", None)
        cls.log = open(os.path.join(cls.tmp.name, "server.log"), "w")
        cls.proc = subprocess.Popen([sys.executable, os.path.join(REPO, "serve_media.py"), str(cls.port)],
                                    cwd=REPO, env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while not os.path.exists(cls.port_file):
            if cls.proc.poll() is not None or time.time() > deadline:
                cls.tearDownClass()
                raise RuntimeError("test server didn't start; see its log")
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log.close()
        cls.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}
        finally:
            conn.close()

    def test_port_file_is_the_temporary_one(self):
        with open(self.port_file) as f:
            self.assertEqual(json.load(f)["port"], self.port)

    def test_no_origin_passes_without_cors(self):
        status, h = self.request("GET", "/projects")
        self.assertEqual(status, 200)
        self.assertNotIn("access-control-allow-origin", h)

    def test_loopback_origin_is_echoed(self):
        status, h = self.request("GET", "/projects", {"Origin": "http://127.0.0.1:5173"})
        self.assertEqual(status, 200)
        self.assertEqual(h.get("access-control-allow-origin"), "http://127.0.0.1:5173")
        self.assertIn("Origin", h.get("vary", ""))

    def test_foreign_origin_get_refused(self):
        status, h = self.request("GET", "/projects", {"Origin": EVIL})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", h)

    def test_foreign_origin_simple_post_refused(self):
        # A "simple" cross-site POST: no preflight, text/plain body.
        status, h = self.request("POST", "/page/health",
                                 {"Origin": EVIL, "Content-Type": "text/plain"}, body="{}")
        self.assertEqual(status, 403)

    def test_foreign_preflight_refused(self):
        status, h = self.request("OPTIONS", "/query", {
            "Origin": EVIL, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", h)
        self.assertNotIn("access-control-allow-methods", h)

    def test_loopback_preflight_allowed(self):
        status, h = self.request("OPTIONS", "/query", {
            "Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(status, 204)
        self.assertEqual(h.get("access-control-allow-origin"), "http://127.0.0.1:5173")

    def test_rebound_host_refused(self):
        status, _ = self.request("GET", "/projects", {"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)

    def test_foreign_head_refused(self):
        status, _ = self.request("HEAD", "/index.html", {"Origin": EVIL})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
