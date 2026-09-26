from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from localdish import capture, cli, grpcweb, wire
from localdish.grpcweb import GrpcError
from tests.protobuild import field, file, ld, message

P = ".SpaceX.API.Device."


def _map(name, value_type):
    return message(name, [field("key", 1, "string"), field("value", 2, "message", value_type)], map_entry=True)


def household_files() -> list:
    """A small SpaceX.API.Device whose answers carry everything that identifies a household."""
    client = message("Client", [
        field("name", 1, "string"), field("given_name", 2, "string"), field("mac_address", 3, "string"),
        field("ip_address", 4, "string"), field("ipv6_addresses", 5, "string", repeated=True),
        field("device_id", 6, "string"), field("domain", 7, "string"), field("hostname", 8, "string"),
        field("upstream_mac_address", 9, "string"), field("iface", 10, "string"), field("signal_strength", 11, "float"),
        field("note", 12, "string")])
    network = message("Network", [field("ssid", 1, "string"), field("password", 2, "string"), field("psk", 3, "string"),
                                  field("bssid", 4, "string")])
    downstream = message("DownstreamRouter", [field("ip_address", 1, "string")])
    status = message("DishGetStatusResponse", [
        field("device_info", 1, "message", P + "DeviceInfo"),
        field("connected_routers", 2, "string", repeated=True),
        field("account_shard", 3, "string"),
        field("downstream_routers", 4, "message", P + "DishGetStatusResponse.DownstreamRoutersEntry", repeated=True),
        field("pop_ping_latency_ms", 5, "float"),
        field("ipv4_wan_address", 6, "string"),
        field("auth_token", 7, "string"),
    ], nested=[_map("DownstreamRoutersEntry", P + "DownstreamRouter")])
    clients = message("WifiGetClientsResponse", [field("clients", 1, "message", P + "Client", repeated=True)])
    wifi = message("WifiGetStatusResponse", [field("networks", 1, "message", P + "Network", repeated=True),
                                             field("device_info", 2, "message", P + "DeviceInfo"),
                                             field("ipv6_wan_addresses", 3, "string", repeated=True)])
    info = message("DeviceInfo", [field("id", 1, "string"), field("hardware_version", 2, "string")])
    target = message("PingTarget", [field("address", 1, "string"), field("location", 2, "string")])
    result = message("PingResult", [field("target", 1, "message", P + "PingTarget"),
                                    field("latency_ms", 2, "float")])
    ping = message("GetPingResponse", [field("results", 1, "message", P + "GetPingResponse.ResultsEntry",
                                             repeated=True)], nested=[_map("ResultsEntry", P + "PingResult")])
    info_resp = message("GetDeviceInfoResponse", [field("device_info", 1, "message", P + "DeviceInfo")])
    empty = [message(n) for n in ("GetStatusRequest", "WifiGetClientsRequest", "GetPingRequest",
                                  "GetDeviceInfoRequest", "GetLocationRequest")]
    req = message("Request", [
        field("id", 1, "uint64"),
        field("get_status", 1004, "message", P + "GetStatusRequest", oneof=0),
        field("wifi_get_clients", 1005, "message", P + "WifiGetClientsRequest", oneof=0),
        field("get_ping", 1006, "message", P + "GetPingRequest", oneof=0),
        field("get_device_info", 1007, "message", P + "GetDeviceInfoRequest", oneof=0),
        field("get_location", 1008, "message", P + "GetLocationRequest", oneof=0),
    ], oneofs=["request"])
    resp = message("Response", [
        field("id", 1, "uint64"), field("api_version", 3, "uint64"),
        field("dish_get_status", 2004, "message", P + "DishGetStatusResponse", oneof=0),
        field("wifi_get_status", 2009, "message", P + "WifiGetStatusResponse", oneof=0),
        field("wifi_get_clients", 2005, "message", P + "WifiGetClientsResponse", oneof=0),
        field("get_ping", 2006, "message", P + "GetPingResponse", oneof=0),
        field("get_device_info", 2007, "message", P + "GetDeviceInfoResponse", oneof=0),
    ], oneofs=["response"])
    return [file("spacex/api/device.proto", "SpaceX.API.Device", messages=[
        client, network, downstream, status, clients, wifi, info, target, result, ping, info_resp, *empty, req, resp])]


DISH_ID = "0000abcd-11112222-33334444"
ROUTER_ID = "Router-0100000000000000a1b2c3d4"
SECRETS = [DISH_ID, ROUTER_ID, "shard-9f8e7d", "Kitchen iPad", "Grandma's Phone", "kitchen-ipad", "aa:bb:cc:dd:ee:01",
           "AA:BB:CC:DD:EE:02", "aa:bb:cc:dd:ee:03", "aa:bb:cc:dd:ee:04", "client-device-7c1d", "home.arpa",
           "2600:1700:abcd::42", "2600:1700:abcd::1", "100.79.12.34", "10.20.30.40", "OurHouseWifi", "hunter22",
           "psk-value-1", "tok-3141"]

ANSWERS = {
    ("dish", "get_status"): {"api_version": 42, "id": 7, "dish_get_status": {
        "device_info": {"id": DISH_ID, "hardware_version": "rev4_prod1"},
        "connected_routers": [ROUTER_ID],
        "account_shard": "shard-9f8e7d",
        "downstream_routers": {ROUTER_ID: {"ip_address": "192.168.1.47"}},
        "pop_ping_latency_ms": float("nan"),
        "ipv4_wan_address": "100.79.12.34",
        "auth_token": "tok-3141"}},
    ("dish", "get_device_info"): {"api_version": 42, "get_device_info": {"device_info": {"id": DISH_ID}}},
    ("router", "get_device_info"): {"api_version": 126, "get_device_info": {"device_info": {"id": ROUTER_ID}}},
    ("router", "get_status"): {"api_version": 126, "wifi_get_status": {
        "networks": [{"ssid": "OurHouseWifi", "password": "hunter22", "psk": "psk-value-1",
                      "bssid": "aa:bb:cc:dd:ee:04"}],
        "device_info": {"id": ROUTER_ID},
        "ipv6_wan_addresses": ["2600:1700:abcd::1"]}},
    ("router", "wifi_get_clients"): {"api_version": 126, "wifi_get_clients": {"clients": [
        {"name": "Kitchen iPad", "given_name": "Grandma's Phone", "mac_address": "aa:bb:cc:dd:ee:01",
         "ip_address": "192.168.2.23", "ipv6_addresses": ["2600:1700:abcd::42"], "device_id": "client-device-7c1d",
         "domain": "home.arpa", "hostname": "kitchen-ipad", "upstream_mac_address": "AA:BB:CC:DD:EE:02",
         "iface": "RF_5GHZ", "signal_strength": -51.0, "note": "aa:bb:cc:dd:ee:03"},
        {"name": "Kitchen iPad", "mac_address": "AA:BB:CC:DD:EE:01", "ip_address": "10.20.30.40"}]}},
    ("router", "get_ping"): {"api_version": 126, "get_ping": {"results": {
        "8.8.8.8": {"target": {"address": "8.8.8.8", "location": "Global"}, "latency_ms": 21.5}}}},
}


class Handler(BaseHTTPRequestHandler):
    """A dish or router over gRPC-web: reflection answers every file at once, as the real ones do."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send(self, body: bytes, headers=()):
        self.send_response(200)
        self.send_header("Content-Type", "application/grpc-web+proto")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def reply(self, data: bytes):
        trailer = b"grpc-status: 0\r\n"
        self.send(grpcweb.frame(data) + b"\x80" + len(trailer).to_bytes(4, "big") + trailer)

    def do_POST(self):
        msg, _ = grpcweb.unframe(self.rfile.read(int(self.headers["Content-Length"])))
        srv = self.server
        if self.path in grpcweb.REFLECTION:
            self.reply(ld(4, b"".join(ld(1, f) for f in srv.files)))
            return
        op = next(iter(k for k in srv.schema.decode("SpaceX.API.Device.Request", msg) if k != "id"))
        srv.ops.append(op)
        answer = ANSWERS.get((srv.who, op))
        if answer is None:
            self.send(b"", headers=[("Grpc-Status", "7"), ("Grpc-Message", "GetLocation requests disabled due to policy")])
        else:
            self.reply(srv.schema.encode("SpaceX.API.Device.Response", answer))


def serve(who: str):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.who, srv.ops = who, []
    srv.files = household_files()
    srv.schema = wire.Schema.from_files(srv.files)
    threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
    return srv


class CaptureEndToEndTest(unittest.TestCase):
    """Real grpcweb.Devices against fake devices on 127.0.0.1: reflection, decode, scrub, write."""

    def setUp(self):
        self.servers = {w: serve(w) for w in ("dish", "router")}
        self.devices = {w: grpcweb.Device("127.0.0.1", s.server_address[1]) for w, s in self.servers.items()}
        self.sleeps = []
        self.said = []
        raw = capture.capture(self.devices["dish"], self.devices["router"], sleep=self.sleeps.append,
                              say=self.said.append)
        self.data, self.counts = capture.scrub(raw)

    def tearDown(self):
        for d in self.devices.values():
            d.close()
        for s in self.servers.values():
            s.shutdown()
            s.server_close()

    def test_reads_are_read_only_and_spaced(self):
        self.assertEqual(self.servers["dish"].ops, ["get_device_info", "get_status", "get_location"])
        self.assertEqual(self.servers["router"].ops, list(capture.ROUTER_READS))
        self.assertEqual(self.sleeps, [capture.SPACING_S] * (len(capture.DISH_READS) - 1))   # none for the router

    def test_fixture_format(self):
        d = self.data
        self.assertEqual(set(d), {"dish", "router", "errors"})
        self.assertEqual(set(d["dish"]), {"get_device_info", "get_status"})
        self.assertEqual(d["errors"]["dish"]["get_location"],
                         {"code": 7, "message": "GetLocation requests disabled due to policy"})
        for op in ("get_history", "dish_get_obstruction_map", "dish_get_config", "get_diagnostics"):
            self.assertEqual(d["errors"]["dish"][op]["code"], 12)       # not in this firmware
        self.assertEqual(d["errors"]["router"], {})
        self.assertNotIn("id", d["dish"]["get_status"])                  # the Response id is dropped
        self.assertEqual(d["dish"]["get_status"]["api_version"], 42)

    def test_scrubbed(self):
        st = self.data["dish"]["get_status"]["dish_get_status"]
        self.assertEqual(st["device_info"], {"id": "id-demo-1", "hardware_version": "rev4_prod1"})
        self.assertEqual(self.data["dish"]["get_device_info"]["get_device_info"]["device_info"]["id"], "id-demo-1")
        router = "Router-%024d" % 1
        self.assertEqual(st["connected_routers"], [router])
        self.assertEqual(st["downstream_routers"], {router: {"ip_address": "192.168.1.47"}})
        self.assertEqual(st["account_shard"], "account_shard-demo-1")
        self.assertEqual(st["ipv4_wan_address"], "100.64.0.1")
        self.assertNotIn("auth_token", st)
        self.assertNotEqual(st["pop_ping_latency_ms"], st["pop_ping_latency_ms"])    # NaN, kept
        c1, c2 = self.data["router"]["wifi_get_clients"]["wifi_get_clients"]["clients"]
        self.assertEqual(c1["name"], "client-1")
        self.assertEqual(c1["mac_address"], "02:00:00:00:00:01")
        self.assertEqual(c2["mac_address"], "02:00:00:00:00:01")          # same device, either case
        self.assertEqual((c1["ip_address"], c2["ip_address"]), ("192.168.1.23", "100.64.0.2"))
        self.assertEqual(c1["ipv6_addresses"], ["2001:db8::1"])     # clients are scrubbed before status
        self.assertEqual((c1["domain"], c1["iface"], c1["note"]), ("lan", "RF_5GHZ", "02:00:00:00:00:03"))
        net = self.data["router"]["get_status"]["wifi_get_status"]["networks"][0]
        self.assertEqual(net, {"ssid": "STARLINK-DEMO-1", "bssid": "02:00:00:00:00:04"})
        ping = self.data["router"]["get_ping"]["get_ping"]["results"]["8.8.8.8"]
        self.assertEqual(ping["target"]["address"], "8.8.8.8")                        # public targets stay

    def test_counts_name_kinds_never_values(self):
        self.assertEqual(self.counts["credential key"], 3)
        self.assertEqual(self.counts["name"], 4)
        self.assertEqual(set(self.counts), {"credential key", "domain", "id", "ip", "mac", "name", "router id", "ssid"})

    def test_nothing_sensitive_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = capture.write(tmp, self.data)
            with open(path, encoding="utf-8") as f:
                text = f.read()
        self.assertIn("NaN", text)
        for s in SECRETS:
            self.assertNotIn(s.lower(), text.lower(), s)

    def test_the_same_capture_scrubs_the_same(self):
        raw = capture.capture(self.devices["dish"], self.devices["router"], sleep=lambda s: None, say=lambda s: None)
        self.assertEqual(json.dumps(capture.scrub(raw)[0], sort_keys=True), json.dumps(self.data, sort_keys=True))


class ReadTest(unittest.TestCase):
    def test_stops_at_an_unreachable_device(self):
        class Gone:
            calls = 0

            def call(self, op, fields=None, *, timeout=None):
                Gone.calls += 1
                raise ConnectionRefusedError(111, "Connection refused")

        sleeps = []
        answers, errors = capture.read(Gone(), capture.DISH_READS, spacing=2, sleep=sleeps.append, say=lambda s: None)
        self.assertEqual((answers, Gone.calls, sleeps), ({}, 1, []))
        self.assertEqual(errors["get_device_info"]["code"], 14)

    def test_grpc_error_recorded(self):
        class Denies:
            def call(self, op, fields=None, *, timeout=None):
                raise GrpcError(7, "no")

        _, errors = capture.read(Denies(), ("get_location",), say=lambda s: None)
        self.assertEqual(errors, {"get_location": {"code": 7, "message": "no"}})


class CaptureCliTest(unittest.TestCase):
    def test_demo_capture_writes_one_scrubbed_file_and_exits(self):
        spacing = capture.SPACING_S
        capture.SPACING_S = 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["--capture", tmp, "--demo"]), 0)
                files = os.listdir(tmp)
                self.assertEqual(len(files), 1)
                with open(os.path.join(tmp, files[0]), encoding="utf-8") as f:
                    data = json.load(f)
        finally:
            capture.SPACING_S = spacing
        self.assertEqual(set(data["dish"]), set(capture.DISH_READS) - {"get_location"})
        self.assertEqual(set(data["router"]), set(capture.ROUTER_READS))
        self.assertEqual(data["errors"]["dish"]["get_location"]["code"], 7)
        text = out.getvalue()
        self.assertIn("scrubbed: ", text)
        self.assertIn(files[0], text)


if __name__ == "__main__":
    unittest.main()
