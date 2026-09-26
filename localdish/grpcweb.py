"""gRPC-web client for the dish and router (see DESIGN.md "grpcweb.py"). Standard library only."""
from __future__ import annotations

import http.client
import select
import struct
import threading
import time
import urllib.parse

from . import wire

# grpc status codes, by number (https://grpc.github.io/grpc/core/md_doc_statuscodes.html)
CODE_NAMES = {
    0: "OK", 1: "CANCELLED", 2: "UNKNOWN", 3: "INVALID_ARGUMENT", 4: "DEADLINE_EXCEEDED", 5: "NOT_FOUND",
    6: "ALREADY_EXISTS", 7: "PERMISSION_DENIED", 8: "RESOURCE_EXHAUSTED", 9: "FAILED_PRECONDITION", 10: "ABORTED",
    11: "OUT_OF_RANGE", 12: "UNIMPLEMENTED", 13: "INTERNAL", 14: "UNAVAILABLE", 15: "DATA_LOSS", 16: "UNAUTHENTICATED",
}


class GrpcError(Exception):
    """A device answered with a non-zero grpc-status."""

    def __init__(self, code: int, message: str = ""):
        self.code = code
        self.name = CODE_NAMES.get(code, str(code))
        self.message = message
        super().__init__(f"{self.name}: {message}" if message else self.name)


# the gRPC mapping of HTTP statuses, for a reply that carries no grpc-status at all
_HTTP_CODES = {400: 13, 401: 16, 403: 7, 404: 12, 429: 14, 502: 14, 503: 14, 504: 14}

SERVICE = "SpaceX.API.Device.Device"
HANDLE = "/SpaceX.API.Device.Device/Handle"
REFLECTION = ("/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
              "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo")
SCHEMA_STALE_S = 60.0       # unreachable this long → the device may have rebooted into new firmware: refetch


def frame(message: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(message)) + message


def unframe(body: bytes) -> tuple:
    """A gRPC-web body → (the data frames joined, the trailer frames' headers, lowercased)."""
    data, trailers = [], {}
    i, n = 0, len(body)
    while i < n:
        if i + 5 > n:
            raise OSError("bad grpc-web framing: %d stray bytes" % (n - i))
        flag, ln = body[i], struct.unpack(">I", body[i + 1:i + 5])[0]
        i += 5
        if i + ln > n:
            raise OSError("bad grpc-web framing: frame of %d bytes, %d left" % (ln, n - i))
        chunk = body[i:i + ln]
        i += ln
        if flag & 0x80:
            for line in chunk.decode("utf-8", "replace").split("\r\n"):
                k, sep, v = line.partition(":")
                if sep:
                    trailers[k.strip().lower()] = v.strip()
        elif flag & 0x01:
            raise OSError("bad grpc-web framing: compressed frame")
        else:
            data.append(chunk)
    return b"".join(data), trailers


class Client:
    """One keep-alive HTTP/1.1 connection to one gRPC-web port; one call at a time."""

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._conn: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()
        self.connects = 0           # connections opened, for stats and tests

    def unary(self, path: str, message: bytes, timeout: float | None = None) -> bytes:
        t = self.timeout if timeout is None else timeout
        with self._lock:
            try:
                conn = self._connection(t)
                conn.request("POST", path, body=frame(message), headers={
                    "Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1", "Accept": "application/grpc-web+proto"})
                resp = conn.getresponse()
                body = resp.read()
                data, trailers = unframe(body)
            except http.client.HTTPException as e:
                self._close()
                raise OSError("%s: %s" % (type(e).__name__, e)) from e
            except BaseException:
                self._close()           # refused, reset, timeout, bad framing: start clean next call
                raise
            if resp.will_close:
                self._close()
        # trailer frame first; a trailers-only reply puts the status in the HTTP headers instead
        status = trailers.get("grpc-status", resp.getheader("grpc-status"))
        message_ = trailers.get("grpc-message", resp.getheader("grpc-message", ""))
        if status is None:
            if resp.status != 200:
                raise GrpcError(_HTTP_CODES.get(resp.status, 2), "http %d %s" % (resp.status, resp.reason))
            status = "0"
        try:
            code = int(status)
        except ValueError:
            raise GrpcError(2, "bad grpc-status %r" % status) from None
        if code:
            raise GrpcError(code, urllib.parse.unquote(message_))
        return data

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        conn = self._conn
        if conn is not None and conn.sock is not None:
            # an idle keep-alive the device has since closed reads as EOF; drop it before sending into it
            try:
                readable = select.select([conn.sock], [], [], 0)[0]
            except (OSError, ValueError):
                readable = True
            if readable:
                self._close()
                conn = None
        if conn is None or conn.sock is None:
            self._close()
            conn = self._conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
            conn.connect()
            self.connects += 1
        conn.timeout = timeout
        conn.sock.settimeout(timeout)
        return conn

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._close()


# ---- reflection -----------------------------------------------------------------------------------------

def _reflection_request(field: int, value: str) -> bytes:
    # ServerReflectionRequest: file_by_filename 3, file_containing_symbol 4
    b = value.encode("utf-8")
    return _varint_bytes(field << 3 | 2) + _varint_bytes(len(b)) + b


def _varint_bytes(v: int) -> bytes:
    out = bytearray()
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def reflection_files(reply: bytes) -> list:
    """A ServerReflectionResponse → its FileDescriptorProto blobs; its error_response raises GrpcError."""
    top = wire._raw_fields(reply)
    for err in top.get(7, ()):              # error_response: error_code 1, error_message 2
        e = wire._raw_fields(bytes(err))
        raise GrpcError(wire._int(e, 1, 2), wire._str(e, 2))
    files = []
    for fdr in top.get(4, ()):              # file_descriptor_response: file_descriptor_proto 1 (repeated bytes)
        files.extend(bytes(b) for b in wire._raw_fields(bytes(fdr)).get(1, ()))
    return files


def fetch_schema(client: Client, symbol: str = SERVICE, timeout: float | None = None) -> list:
    """The FileDescriptorProto blobs the service needs, by reflection (v1alpha, then v1)."""
    path = None
    for p in REFLECTION:
        try:
            reply = client.unary(p, _reflection_request(4, symbol), timeout)
        except GrpcError as e:
            if e.code == 12:                # UNIMPLEMENTED: try the next version
                continue
            raise
        path = p
        break
    if path is None:
        raise GrpcError(12, "no server reflection")
    files = reflection_files(reply)
    have = {}
    for b in files:
        d = wire._raw_fields(b)
        have[wire._str(d, 1)] = d
    # one reply has held every file so far, but a server may send only the first: fetch what is still missing
    asked = set()
    while True:
        deps = {bytes(dep).decode("utf-8") for d in have.values() for dep in d.get(3, ())}
        missing = sorted(deps - set(have) - asked)
        if not missing:
            return files
        for name in missing:
            asked.add(name)
            try:
                more = reflection_files(client.unary(path, _reflection_request(3, name), timeout))
            except GrpcError as e:
                if e.code == 5:             # NOT_FOUND: a well-known file the device doesn't serve; fine if unused
                    continue
                raise
            for b in more:
                d = wire._raw_fields(b)
                n = wire._str(d, 1)
                if n not in have:
                    have[n] = d
                    files.append(b)


# ---- the device -----------------------------------------------------------------------------------------

class Device:
    """A dish or router: Request{op: fields} in, the decoded Response out, schema fetched from the device."""

    def __init__(self, host: str, port: int, *, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.client = Client(host, port, timeout)
        self._schema: wire.Schema | None = None
        self.schema_loaded: float | None = None     # time.time() of the last schema load; the poller logs changes
        self._down_since: float | None = None       # monotonic time of the first failure in a run of them
        self._lock = threading.Lock()

    @property
    def schema(self) -> "wire.Schema | None":
        return self._schema

    def call(self, op: str, fields: dict | None = None, *, timeout: float | None = None) -> dict:
        with self._lock:
            try:
                if self._schema is None:
                    self._load(timeout)
                elif self._down_since is not None and time.monotonic() - self._down_since >= SCHEMA_STALE_S:
                    try:
                        self._load(timeout)
                    except GrpcError:
                        pass                # it answers but won't reflect: keep the schema we have
                    self._down_since = None
                request = self._schema.encode("SpaceX.API.Device.Request", {op: fields or {}})
                reply = self.client.unary(HANDLE, request, timeout)
            except OSError:
                if self._down_since is None:
                    self._down_since = time.monotonic()
                raise
            except GrpcError:
                self._down_since = None     # it answered
                raise
            self._down_since = None
            out = self._schema.decode("SpaceX.API.Device.Response", reply)
            out.pop("id", None)
            return out

    def _load(self, timeout: float | None) -> None:
        self._schema = wire.Schema.from_files(fetch_schema(self.client, timeout=timeout))
        self.schema_loaded = time.time()

    def close(self) -> None:
        self.client.close()
