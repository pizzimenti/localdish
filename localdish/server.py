"""The local HTTP server: the API, its guards and the page's files (DESIGN.md "server.py")."""
from __future__ import annotations

import json
import math
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

try:
    from importlib.resources import files as _files
except ImportError:  # pragma: no cover - importlib.resources.files is 3.9+, this is belt and braces
    _files = None

SECRET = re.compile(r"(?i)(password|passphrase|psk|secret|token|key)$")
STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MAX_BODY = 64 * 1024

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


def clean(value):
    """Drops credential-named keys at any depth, and turns NaN and infinities (which JSON has no word for) into null."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not (isinstance(k, str) and SECRET.search(k))}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def static_file(name: str) -> bytes | None:
    """A file from localdish/static, or None. Only plain names: no folders, nothing hidden."""
    if not STATIC_NAME.match(name) or _files is None:
        return None
    try:
        return (_files("localdish") / "static" / name).read_bytes()
    except (OSError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "localdish"
    sys_version = ""

    def log_message(self, format, *args):   # the poller's events are the log; requests are noise
        pass

    # ---- replies

    def _send(self, status: int, data: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status: int, value) -> None:
        data = json.dumps(clean(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    def _refuse(self, status: int, error: str) -> None:
        self.close_connection = True     # the body, if any, was not read
        if self.path.startswith("/api/"):
            self._json(status, {"ok": False, "error": error})
        else:
            self._send(status, (error + "\n").encode(), "text/plain; charset=utf-8")

    # ---- guards

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def _post_ok(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return self.headers.get("X-Localdish") == "1" and ctype == "application/json"

    # ---- methods

    def do_GET(self):
        self._guarded(self._get)

    def do_HEAD(self):
        self._guarded(self._get)

    def do_POST(self):
        self._guarded(self._post)

    def _guarded(self, fn) -> None:
        if not self._host_ok():
            return self._refuse(403, "forbidden: localdish only answers to 127.0.0.1, localhost or [::1]")
        try:
            fn(urlsplit(self.path).path)
        except Exception:
            print(traceback.format_exc().rstrip(), file=sys.stderr, flush=True)
            self.close_connection = True
            self._json(500, {"ok": False, "error": "localdish hit a bug; the details are in its terminal"})

    def _get(self, path: str) -> None:
        poller = self.server.poller
        if path == "/api/state":
            poller.touch()
            return self._json(200, poller.state())
        if path == "/api/history":
            return self._json(200, poller.history())
        if path == "/api/obstruction":
            return self._json(200, poller.obstruction())
        if path == "/api/stats":
            return self._json(200, poller.stats())
        if path.startswith("/api/"):
            return self._json(404, {"ok": False, "error": "not found"})
        name = (path[len("/static/"):] if path.startswith("/static/") else path.lstrip("/")) or "index.html"
        data = static_file(name)
        if data is None:
            return self._send(404, b"not found\n", "text/plain; charset=utf-8")
        ext = name[name.rfind("."):].lower() if "." in name else ""
        self._send(200, data, TYPES.get(ext, "application/octet-stream"))

    def _post(self, path: str) -> None:
        if not self._post_ok():
            return self._refuse(403, "forbidden: a POST needs X-Localdish: 1 and Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return self._refuse(400, "bad content-length")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"ok": False, "error": "the body is not json"})
        if not isinstance(payload, dict):
            return self._json(400, {"ok": False, "error": "the body must be a json object"})
        poller = self.server.poller
        parts = path.split("/")
        if len(parts) == 4 and parts[:3] == ["", "api", "control"]:
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                return self._json(400, {"ok": False, "error": "params must be an object"})
            status, reply = poller.control(parts[3], params)
            return self._json(status, reply)
        if len(parts) == 4 and parts[:3] == ["", "api", "refresh"]:
            status, reply = poller.refresh(parts[3])
            return self._json(status, reply)
        return self._json(404, {"ok": False, "error": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, poller):
        self.poller = poller
        super().__init__(address, Handler)


def make_server(poller, host: str = "127.0.0.1", port: int = 8686) -> Server:
    return Server((host, port), poller)
