from __future__ import annotations

import http.client
import json
import pathlib
import tempfile
import threading
import unittest

from localdish import poller, server
from localdish.grpcweb import GrpcError

try:
    from . import fakes
except ImportError:
    import fakes


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.clock = fakes.Clock()
        self.dish = fakes.FakeDev({
            "get_status": {"pop_ping_latency_ms": 20.5, "snr": float("nan"),
                           "wifi": {"networks": [{"ssid": "home", "password": "hunter2", "psk": "x",
                                                  "auth_token": "t", "keys": [1], "public_key": "k"}]}},
            "reboot": GrpcError(9, "not now"),
        })
        self.poller = poller.Poller({"dish": self.dish, "router": None}, device=fakes.fake_device_module(),
                                    explain=fakes.fake_explain_module(), clock=self.clock, log=lambda line: None)
        self.httpd = server.make_server(self.poller, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, body=None, headers=None, host=""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not None:        # None: no Host header at all
                conn.putheader("Host", host or f"127.0.0.1:{self.port}")
            data = json.dumps(body).encode() if body is not None else b""
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            if method == "POST":
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data if method == "POST" else None)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, dict(resp.getheaders()), raw
        finally:
            conn.close()

    def get_json(self, path, **kw):
        status, headers, raw = self.request("GET", path, **kw)
        return status, json.loads(raw)

    def post(self, path, body=None, headers=None):
        h = {"X-Localdish": "1", "Content-Type": "application/json"} if headers is None else headers
        status, _, raw = self.request("POST", path, body if body is not None else {}, h)
        return status, json.loads(raw) if raw.startswith(b"{") else raw

    # ---- guards

    def test_hosts_allowed(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}", f"LOCALHOST:{self.port}"):
            self.assertEqual(self.request("GET", "/api/stats", host=host)[0], 200, host)

    def test_foreign_host_is_403(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", f"127.0.0.1:{self.port + 1}", None):
            self.assertEqual(self.request("GET", "/api/state", host=host)[0], 403, host)
        self.assertEqual(self.request("GET", "/", host="attacker.test")[0], 403)
        self.assertFalse(self.poller.watching())

    def test_post_needs_the_header_and_json(self):
        self.assertEqual(self.post("/api/refresh/info", headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.post("/api/refresh/info", headers={"X-Localdish": "1"})[0], 403)
        self.assertEqual(self.post("/api/refresh/info", headers={"X-Localdish": "1",
                                                                  "Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.post("/api/refresh/info", headers={"X-Localdish": "1",
                                                                  "Content-Type": "application/json; charset=utf-8"})[0],
                         200)

    def test_never_cors(self):
        replies = [self.request("GET", "/api/state"), self.request("GET", "/nothing"),
                   self.request("OPTIONS", "/api/state", headers={"Origin": "http://evil.example"}),
                   self.request("GET", "/api/state", host="evil.example"),
                   self.request("POST", "/api/control/restart", {}, {"Origin": "http://evil.example"})]
        for status, headers, _ in replies:
            self.assertFalse([h for h in headers if h.lower().startswith("access-control-")], headers)

    # ---- the API

    def test_state_is_json_without_secrets_or_nan(self):
        self.poller.run_job("status")
        status, headers, raw = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        state = json.loads(raw)
        st = state["dish"]["status"]
        self.assertIsNone(st["snr"])
        self.assertEqual(st["wifi"]["networks"], [{"ssid": "home", "keys": [1]}])
        self.assertEqual(set(state), {"localdish", "dish", "router", "explain", "controls", "running", "events"})
        self.assertTrue(self.poller.watching())

    def test_clean_at_any_depth(self):
        v = {"a": [{"b": {"Secret": 1, "c": [{"api_key": 2, "d": 3}]}}], "PSK": 4, "tokens": 5}
        self.assertEqual(server.clean(v), {"a": [{"b": {"c": [{"d": 3}]}}], "tokens": 5})

    def test_history_obstruction_stats(self):
        self.assertEqual(self.get_json("/api/history"),
                         (200, {"age_s": None, "error": None, "ring": None, "outages": [], "event_log": {}}))
        self.assertEqual(self.get_json("/api/obstruction"), (200, {"age_s": None, "error": None, "map": None}))
        status, stats = self.get_json("/api/stats")
        self.assertEqual(stats, {"calls": {}, "watching": False})
        self.assertEqual(self.get_json("/api/nope")[0], 404)

    def test_refresh_rate_limit_429(self):
        self.assertEqual(self.post("/api/refresh/obstruction"), (200, {"ok": True}))
        status, body = self.post("/api/refresh/obstruction")
        self.assertEqual((status, body), (429, {"ok": False, "retry_after_s": 10}))
        self.assertEqual(self.post("/api/refresh/router")[0], 409)     # no router here

    def test_control_flow(self):
        self.assertEqual(self.post("/api/control/stow", {"params": {}}),
                         (409, {"ok": False, "error": "this dish has no motors"}))
        self.assertEqual(self.post("/api/control/restart"), (502, {"ok": False, "error": "not now"}))
        self.dish.answers["reboot"] = {}
        status, body = self.post("/api/control/restart")
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.post("/api/control/snow_melt", {"params": {"mode": "NEVER"}})[0], 400)
        self.assertEqual(self.post("/api/control/snow_melt", {"params": "AUTO"})[0], 400)
        self.assertEqual(self.post("/api/control/restart", ["x"])[0], 400)

    def test_bad_json_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/refresh/info", body=b"",
                     headers={"X-Localdish": "1", "Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 200)          # an empty body is {}
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/control/restart", body=b"{nope",
                     headers={"X-Localdish": "1", "Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 400)
        conn.close()

    # ---- static files

    def test_static_missing_is_404(self):
        orig = server._files
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "static").mkdir()
            server._files = lambda pkg: pathlib.Path(tmp)
            try:
                self.assertEqual(self.request("GET", "/")[0], 404)
                self.assertEqual(self.request("GET", "/app.js")[0], 404)
            finally:
                server._files = orig

    def test_static_served_from_the_package(self):
        orig = server._files
        with tempfile.TemporaryDirectory() as tmp:
            static = pathlib.Path(tmp) / "static"
            static.mkdir()
            (static / "index.html").write_text("<!doctype html><title>localdish</title>")
            (static / "app.js").write_text("// js")
            (pathlib.Path(tmp) / "secret.txt").write_text("no")
            server._files = lambda pkg: pathlib.Path(tmp)
            try:
                status, headers, raw = self.request("GET", "/")
                self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn(b"localdish", raw)
                status, headers, _ = self.request("GET", "/app.js")
                self.assertEqual((status, headers["Content-Type"]), (200, "text/javascript; charset=utf-8"))
                self.assertEqual(self.request("GET", "/static/app.js")[0], 200)
                for bad in ("/../secret.txt", "/static/../secret.txt", "/%2e%2e/secret.txt", "/.hidden"):
                    self.assertEqual(self.request("GET", bad)[0], 404, bad)
            finally:
                server._files = orig


if __name__ == "__main__":
    unittest.main()
