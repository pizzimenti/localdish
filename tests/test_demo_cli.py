from __future__ import annotations

import http.client
import importlib.util
import json
import random
import socket
import threading
import time
import unittest

from localdish import cli, demo
from localdish.grpcweb import GrpcError

try:
    from . import fakes
except ImportError:
    import fakes


class FakeDeviceTest(unittest.TestCase):
    def setUp(self):
        self.clock = fakes.Clock()
        self.logged = []
        self.dish, self.router = demo.devices(clock=self.clock, rng=random.Random(1), log=self.logged.append)

    def test_recorded_answers(self):
        r = self.dish.call("get_status")
        self.assertIn("dish_get_status", r)
        self.assertIn("wifi_get_clients", self.router.call("wifi_get_clients"))

    def test_recorded_error_raises(self):
        with self.assertRaises(GrpcError) as cm:
            self.dish.call("get_location")
        self.assertEqual(cm.exception.name, "PERMISSION_DENIED")

    def test_unrecorded_is_unimplemented(self):
        with self.assertRaises(GrpcError) as cm:
            self.dish.call("dish_get_emc")
        self.assertEqual(cm.exception.code, 12)

    def test_controls_do_nothing_but_say_so(self):
        self.assertEqual(self.dish.call("reboot", {}), {"reboot": {}})
        self.assertTrue(self.logged and self.logged[-1].startswith("demo: dish reboot"))
        self.dish.call("dish_set_config", {"dish_config": {"snow_melt_mode": "ALWAYS_ON", "apply_snow_melt_mode": True}})
        cfg = self.dish.call("dish_get_config")["dish_get_config"]["dish_config"]
        self.assertEqual(cfg["snow_melt_mode"], "ALWAYS_ON")

    def test_live_values_drift_a_little(self):
        base = demo.load()["dish"]["get_status"]["dish_get_status"]["pop_ping_latency_ms"]
        seen = {self.dish.call("get_status")["dish_get_status"]["pop_ping_latency_ms"] for _ in range(5)}
        self.assertGreater(len(seen), 1)
        for v in seen:
            self.assertLessEqual(abs(v - base), base * demo.DRIFT + 1e-9)

    def test_history_ring_moves_with_the_clock(self):
        h0 = self.dish.call("get_history")["dish_get_history"]
        self.clock.advance(7)
        h1 = self.dish.call("get_history")["dish_get_history"]
        self.assertEqual(h1["current"], h0["current"] + 7)
        self.assertEqual(len(h1["pop_ping_latency_ms"]), len(h0["pop_ping_latency_ms"]))

    def test_speedtest_runs_then_finishes(self):
        self.router.call("start_speedtest", {})
        self.clock.advance(3)
        st = self.router.call("get_speedtest_status")["get_speedtest_status"]["status"]
        self.assertTrue(st["running"])
        self.assertEqual(len(st["down"]["throughputs_mbps"]), 3)
        self.clock.advance(demo.SPEEDTEST_S)
        self.assertFalse(self.router.call("get_speedtest_status")["get_speedtest_status"]["status"]["running"])


class RouterDiscoveryTest(unittest.TestCase):
    def test_linux(self):
        text = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
                "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
                "eth0\t00000000\t0102A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")
        self.assertEqual(cli.linux_gateways(text), ["192.168.1.1", "192.168.2.1"])

    def test_macos(self):
        text = "   route to: default\ndestination: default\n       mask: default\n    gateway: 192.168.2.1\n"
        self.assertEqual(cli.macos_gateways(text), ["192.168.2.1"])

    def test_windows(self):
        text = ("Wireless LAN adapter Wi-Fi:\r\n\r\n"
                "   IPv4 Address. . . . . . . . . . . : 192.168.1.23\r\n"
                "   Default Gateway . . . . . . . . . : fe80::1%12\r\n"
                "                                       192.168.1.1\r\n"
                "   DHCP Server . . . . . . . . . . . : 192.168.1.9\r\n")
        self.assertEqual(cli.windows_gateways(text), ["192.168.1.1"])

    def test_first_that_accepts(self):
        tried = []

        def connect(addr, timeout):
            tried.append((addr, timeout))
            if addr[0] != "192.168.2.1":
                raise ConnectionRefusedError()
            return socket.socket()

        self.assertEqual(cli.discover_router(["10.0.0.1", "192.168.2.1", "192.168.1.1"], connect=connect),
                         "192.168.2.1")
        self.assertEqual(tried, [(("10.0.0.1", 9001), 1.0), (("192.168.2.1", 9001), 1.0)])

    def test_none_accepts(self):
        def refuse(addr, timeout):
            raise socket.timeout()

        self.assertIsNone(cli.discover_router(["192.168.1.1"], connect=refuse))

    def test_real_tcp_on_loopback(self):
        with socket.socket() as ls:
            ls.bind(("127.0.0.1", 0))
            ls.listen(1)
            port = ls.getsockname()[1]
            self.assertEqual(cli.discover_router(["127.0.0.1"], port=port), "127.0.0.1")

    def test_candidates_end_with_the_factory_address(self):
        c = cli.gateway_candidates()
        self.assertEqual(c[-1], "192.168.1.1")
        self.assertEqual(len(c), len(set(c)))

    def test_args(self):
        a = cli.parse([])
        self.assertEqual((a.port, a.dish, a.router, a.demo), (8686, "192.168.100.1", "auto", False))
        with self.assertRaises(SystemExit) as cm:
            with open("/dev/null", "w") as null:
                import contextlib
                with contextlib.redirect_stdout(null):
                    cli.parse(["--version"])
        self.assertEqual(cm.exception.code, 0)


def _have_real_modules() -> bool:
    return all(importlib.util.find_spec(f"localdish.{m}") for m in ("device", "explain"))


class DemoEndToEndTest(unittest.TestCase):
    """The whole process in demo mode on 127.0.0.1 with an ephemeral port: threads, cadence, server."""

    def run_demo(self, **modules):
        args = cli.parse(["--demo", "--port", "0"])
        p, httpd, line = cli.build(args, log=lambda text: None, **modules)
        port = httpd.server_address[1]
        self.assertEqual(line, f"localdish {cli.__version__} — http://127.0.0.1:{port} (demo: a recorded Starlink Mini)")
        p.start()
        th = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        try:
            def get(path):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", path)
                r = c.getresponse()
                out = r.status, json.loads(r.read())
                c.close()
                return out

            def post(path, body):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("POST", path, body=json.dumps(body),
                          headers={"X-Localdish": "1", "Content-Type": "application/json"})
                r = c.getresponse()
                out = r.status, json.loads(r.read())
                c.close()
                return out

            deadline = time.time() + 5
            while True:
                status, state = get("/api/state")
                self.assertEqual(status, 200)
                if state["dish"]["status"] and state["router"]["status"] is not None or time.time() > deadline:
                    break
                time.sleep(0.1)
            self.assertTrue(state["localdish"]["demo"])
            self.assertTrue(state["localdish"]["dish"]["reachable"])
            self.assertIsNotNone(state["dish"]["status"])
            self.assertIsNotNone(state["router"]["status"])
            self.assertTrue(state["dish"]["location_error"].startswith("PERMISSION_DENIED"))
            self.assertTrue(state["explain"]["headline"]["text"])

            deadline = time.time() + 5
            while get("/api/history")[1]["ring"] is None and time.time() < deadline:
                time.sleep(0.1)
            self.assertIsNotNone(get("/api/history")[1]["ring"])
            status, restart = post("/api/control/restart", {"params": {}})
            self.assertEqual((status, restart["ok"]), (200, True))
            texts = [e["text"] for e in get("/api/state")[1]["events"]]
            self.assertTrue(any(t.startswith("demo: dish reboot") for t in texts), texts)
            stats = get("/api/stats")[1]
            self.assertTrue(stats["watching"])
            self.assertGreaterEqual(stats["calls"]["dish:get_status"]["total"], 1)
            return state
        finally:
            httpd.shutdown()
            httpd.server_close()
            p.stop()

    def test_with_fake_device_and_explain(self):
        self.run_demo(device=fakes.fake_device_module(), explain=fakes.fake_explain_module())

    @unittest.skipUnless(_have_real_modules(), "device.py or explain.py not merged yet")
    def test_with_the_real_modules(self):
        state = self.run_demo()
        self.assertEqual({c["name"] for c in state["controls"]} >= {"restart", "snow_melt", "speedtest"}, True)
        # the server's credential stripping must not eat our own fields (it once ate alerts[].key)
        self.assertTrue(state["explain"]["alerts"])
        self.assertTrue(all(a.get("name") for a in state["explain"]["alerts"]))


if __name__ == "__main__":
    unittest.main()
