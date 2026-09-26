from __future__ import annotations

import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from localdish import grpcweb, wire
from localdish.grpcweb import Client, Device, GrpcError, fetch_schema
from tests.protobuild import device_files, ld, vi

V1ALPHA, V1 = grpcweb.REFLECTION


def trailer(text: bytes) -> bytes:
    return b"\x80" + struct.pack(">I", len(text)) + text


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.server.connections += 1

    def log_message(self, *args):
        pass

    def send(self, body: bytes, status: int = 200, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", "application/grpc-web+proto")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:                             # the client gave up first (the timeout test)
            self.close_connection = True

    def reply(self, data: bytes, status: bytes = b"grpc-status: 0\r\n"):
        self.send(grpcweb.frame(data) + trailer(status))

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        srv = self.server
        srv.requests.append((self.path, self.headers, body))
        msg, _ = grpcweb.unframe(body)
        p = self.path
        if p == "/echo":
            self.reply(msg)
        elif p == "/echo-close":                    # answers, then hangs up without saying so
            self.reply(msg)
            self.close_connection = True
        elif p == "/trailer-error":
            self.send(trailer(b"Grpc-Status: 12\r\ngrpc-message: not%20here\r\n"))
        elif p == "/trailers-only":
            self.send(b"", headers=[("Grpc-Status", "7"),
                                    ("Grpc-Message", "GetLocation requests disabled due to policy")])
        elif p == "/drop":
            self.close_connection = True
        elif p == "/slow":
            time.sleep(0.6)
            self.reply(msg)
        elif p == "/garbage":
            self.send(b"\x00\x00\x00")
        elif p == "/http404":
            self.send(b"", status=404)
        elif p in (V1ALPHA, V1):
            self.reflection(p, msg)
        elif p == grpcweb.HANDLE:
            self.handle_request(msg)
        else:
            self.send(b"", status=500)

    def reflection(self, path: str, msg: bytes):
        srv = self.server
        if path == V1ALPHA and not srv.v1alpha:
            self.send(b"", headers=[("grpc-status", "12")])
            return
        req = wire._raw_fields(msg)
        files = srv.files
        if 4 in req:                                # file_containing_symbol: only the first file, deps left out
            self.reply(ld(4, ld(1, files[0])))
        elif 3 in req:
            name = bytes(req[3][0]).decode()
            match = [f for f in files if wire._str(wire._raw_fields(f), 1) == name]
            if match:
                self.reply(ld(4, ld(1, match[0])))
            else:
                self.reply(ld(7, vi(1, 5) + ld(2, "not found")))

    def handle_request(self, msg: bytes):
        srv = self.server
        if srv.down:
            self.close_connection = True
            return
        req = srv.schema.decode("SpaceX.API.Device.Request", msg)
        if "get_status" in req:
            self.reply(srv.schema.encode("SpaceX.API.Device.Response", {
                "id": 9, "dish_get_status": {"uptime_s": 5, "snr": 9.5, "state": "CONNECTED"}}))
        else:
            self.send(b"", headers=[("Grpc-Status", "7"), ("Grpc-Message", "reboot denied")])


class FakeServerTest(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.srv.connections = 0
        self.srv.requests = []
        self.srv.v1alpha = False
        self.srv.down = False
        self.srv.files = device_files()
        self.srv.schema = wire.Schema.from_files(self.srv.files)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, args=(0.02,), daemon=True).start()
        self.closers = [self.srv.server_close, self.srv.shutdown]

    def tearDown(self):
        for c in reversed(self.closers):
            c()

    def client(self, timeout: float = 2.0) -> Client:
        c = Client("127.0.0.1", self.port, timeout)
        self.closers.append(c.close)
        return c


class ClientTest(FakeServerTest):
    def test_reply_and_trailer_frame(self):
        c = self.client()
        self.assertEqual(c.unary("/echo", b"hello"), b"hello")
        self.assertEqual(c.unary("/echo", b""), b"")
        path, headers, body = self.srv.requests[0]
        self.assertEqual(body, b"\x00\x00\x00\x00\x05hello")
        self.assertEqual(headers["content-type"], "application/grpc-web+proto")
        self.assertEqual(headers["x-grpc-web"], "1")

    def test_error_in_trailer_frame(self):
        c = self.client()
        with self.assertRaises(GrpcError) as cm:
            c.unary("/trailer-error", b"")
        self.assertEqual((cm.exception.code, cm.exception.name, cm.exception.message), (12, "UNIMPLEMENTED", "not here"))
        c.unary("/echo", b"x")
        self.assertEqual(self.srv.connections, 1)        # a grpc error is an answer, not a broken connection

    def test_trailers_only_error_capitalised(self):
        with self.assertRaises(GrpcError) as cm:
            self.client().unary("/trailers-only", b"")
        e = cm.exception
        self.assertEqual((e.code, e.name), (7, "PERMISSION_DENIED"))
        self.assertEqual(e.message, "GetLocation requests disabled due to policy")

    def test_http_error_without_grpc_status(self):
        with self.assertRaises(GrpcError) as cm:
            self.client().unary("/http404", b"")
        self.assertEqual(cm.exception.name, "UNIMPLEMENTED")

    def test_keep_alive_reuses_one_connection(self):
        c = self.client()
        for i in range(5):
            self.assertEqual(c.unary("/echo", bytes([i])), bytes([i]))
        self.assertEqual((self.srv.connections, c.connects), (1, 1))

    def test_server_drop_then_reconnect(self):
        c = self.client()
        c.unary("/echo", b"a")
        with self.assertRaises(OSError):
            c.unary("/drop", b"")
        self.assertIsNone(c._conn)
        self.assertEqual(c.unary("/echo", b"b"), b"b")      # no retry: the next call is the reconnect
        self.assertEqual(self.srv.connections, 2)
        self.assertEqual([r[0] for r in self.srv.requests], ["/echo", "/drop", "/echo"])

    def test_idle_connection_closed_by_device(self):
        c = self.client()
        c.unary("/echo-close", b"a")
        time.sleep(0.1)
        self.assertEqual(c.unary("/echo", b"b"), b"b")      # noticed before sending, not after failing
        self.assertEqual(self.srv.connections, 2)

    def test_timeout_closes_the_connection(self):
        c = self.client(timeout=0.2)
        c.unary("/echo", b"a")
        t0 = time.monotonic()
        with self.assertRaises(OSError):                    # TimeoutError is an OSError
            c.unary("/slow", b"")
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertIsNone(c._conn)
        self.assertEqual(c.unary("/slow", b"z", timeout=2.0), b"z")   # a per-call timeout
        self.assertEqual(self.srv.connections, 2)

    def test_bad_framing_closes(self):
        c = self.client()
        with self.assertRaises(OSError):
            c.unary("/garbage", b"")
        self.assertIsNone(c._conn)

    def test_unframe(self):
        data, tr = grpcweb.unframe(grpcweb.frame(b"ab") + grpcweb.frame(b"cd") + trailer(b"GRPC-STATUS: 0\r\n"))
        self.assertEqual((data, tr), (b"abcd", {"grpc-status": "0"}))
        with self.assertRaises(OSError):
            grpcweb.unframe(b"\x01\x00\x00\x00\x00")       # compressed: never asked for


class ReflectionTest(FakeServerTest):
    def test_v1_after_v1alpha_and_missing_dependencies(self):
        files = fetch_schema(self.client())
        self.assertEqual(files, self.srv.files)
        self.assertEqual([r[0] for r in self.srv.requests], [V1ALPHA, V1, V1])
        self.assertIn("SpaceX.API.Device.Response", wire.Schema.from_files(files).message_names())

    def test_v1alpha_first(self):
        self.srv.v1alpha = True
        fetch_schema(self.client())
        self.assertEqual([r[0] for r in self.srv.requests], [V1ALPHA, V1ALPHA])

    def test_dependency_not_found_is_skipped(self):
        self.srv.files = self.srv.files[:1]
        self.assertEqual(len(fetch_schema(self.client())), 1)

    def test_reflection_error_response(self):
        with self.assertRaises(GrpcError) as cm:
            grpcweb.reflection_files(ld(7, vi(1, 5) + ld(2, "no such symbol")))
        self.assertEqual((cm.exception.code, cm.exception.message), (5, "no such symbol"))


class DeviceTest(FakeServerTest):
    def device(self) -> Device:
        d = Device("127.0.0.1", self.port, timeout=2.0)
        self.closers.append(d.close)
        return d

    def reflections(self) -> int:
        return sum(1 for r in self.srv.requests if r[0] in (V1ALPHA, V1))

    def test_call(self):
        d = self.device()
        self.assertIsNone(d.schema)
        want = {"dish_get_status": {"uptime_s": 5, "snr": 9.5, "state": "CONNECTED"}}
        self.assertEqual(d.call("get_status"), want)
        self.assertIsNotNone(d.schema)
        self.assertEqual(d.call("get_status", {}), want)
        self.assertEqual(self.reflections(), 3)              # v1alpha, v1, one missing file: once
        handle = [r[2] for r in self.srv.requests if r[0] == grpcweb.HANDLE]
        self.assertEqual(handle[0], grpcweb.frame(b"\xe2\x3e\x00"))   # get_status (1004) {}: tag and a zero length
        self.assertEqual(self.srv.connections, 1)

    def test_grpc_error_and_bad_op(self):
        d = self.device()
        with self.assertRaises(GrpcError) as cm:
            d.call("reboot")
        self.assertEqual((cm.exception.name, cm.exception.message), ("PERMISSION_DENIED", "reboot denied"))
        with self.assertRaises(ValueError):
            d.call("launch")

    def test_schema_refetched_after_a_minute_unreachable(self):
        d = self.device()
        d.call("get_status")
        self.srv.down = True
        with self.assertRaises(OSError):
            d.call("get_status")
        self.srv.down = False
        d.call("get_status")
        self.assertEqual(self.reflections(), 3)              # down briefly: same schema
        self.srv.down = True
        with self.assertRaises(OSError):
            d.call("get_status")
        d._down_since -= grpcweb.SCHEMA_STALE_S + 1          # as if a minute has passed
        self.srv.down = False
        before = d.schema_loaded
        d.call("get_status")
        self.assertEqual(self.reflections(), 6)
        self.assertGreaterEqual(d.schema_loaded, before)
        d.call("get_status")
        self.assertEqual(self.reflections(), 6)

    def test_unreachable_raises_oserror(self):
        self.srv.server_close()
        self.srv.shutdown()
        self.closers = []
        d = Device("127.0.0.1", self.port, timeout=0.5)
        with self.assertRaises(OSError):
            d.call("get_status")
        self.assertIsNone(d.schema)


if __name__ == "__main__":
    unittest.main()
