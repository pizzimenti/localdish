"""Build FileDescriptorProto blobs by hand, so the codec's tests need no protobuf library and no device."""
from __future__ import annotations

import struct

TYPES = {
    "double": 1, "float": 2, "int64": 3, "uint64": 4, "int32": 5, "fixed64": 6, "fixed32": 7, "bool": 8,
    "string": 9, "message": 11, "bytes": 12, "uint32": 13, "enum": 14, "sfixed32": 15, "sfixed64": 16,
    "sint32": 17, "sint64": 18,
}


def varint(v: int) -> bytes:
    v &= (1 << 64) - 1
    out = bytearray()
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def tag(num: int, wt: int) -> bytes:
    return varint(num << 3 | wt)


def vi(num: int, v: int) -> bytes:
    return tag(num, 0) + varint(v)


def ld(num: int, b) -> bytes:
    if isinstance(b, str):
        b = b.encode("utf-8")
    return tag(num, 2) + varint(len(b)) + b


def fx32(num: int, fmt: str, v) -> bytes:
    return tag(num, 5) + struct.pack("<" + fmt, v)


def fx64(num: int, fmt: str, v) -> bytes:
    return tag(num, 1) + struct.pack("<" + fmt, v)


def field(name: str, number: int, kind: str, type_name: str | None = None, repeated: bool = False,
          oneof: int | None = None, packed: bool | None = None) -> bytes:
    b = ld(1, name) + vi(3, number) + vi(4, 3 if repeated else 1) + vi(5, TYPES[kind])
    if type_name:
        b += ld(6, type_name)
    if packed is not None:
        b += ld(8, vi(2, int(packed)))          # FieldOptions.packed
    if oneof is not None:
        b += vi(9, oneof)
    return b


def message(name: str, fields=(), nested=(), enums=(), oneofs=(), map_entry: bool = False) -> bytes:
    b = ld(1, name)
    b += b"".join(ld(2, f) for f in fields)
    b += b"".join(ld(3, n) for n in nested)
    b += b"".join(ld(4, e) for e in enums)
    if map_entry:
        b += ld(7, vi(7, 1))                    # MessageOptions.map_entry
    b += b"".join(ld(8, ld(1, o)) for o in oneofs)
    return b


def enum(name: str, values) -> bytes:
    return ld(1, name) + b"".join(ld(2, ld(1, n) + vi(2, v)) for n, v in values)


def file(name: str, package: str, messages=(), enums=(), deps=(), syntax: str = "proto3") -> bytes:
    b = ld(1, name) + (ld(2, package) if package else b"")
    b += b"".join(ld(3, d) for d in deps)
    b += b"".join(ld(4, m) for m in messages)
    b += b"".join(ld(5, e) for e in enums)
    return b + (ld(12, syntax) if syntax else b"")


# ---- the synthetic schema the tests share ---------------------------------------------------------------

def test_files() -> list:
    other = file("other.proto", "t.other", messages=[message("Dep", [field("n", 1, "int32")])])
    color = enum("Color", [("RED", 0), ("GREEN", 1), ("BLUE", 2), ("NEGATIVE", -1)])
    inner = message("Inner", [field("a", 1, "int32"), field("s", 2, "string")])
    all_ = message("All", [
        field("d", 1, "double"), field("f", 2, "float"), field("i64", 3, "int64"), field("u64", 4, "uint64"),
        field("i32", 5, "int32"), field("f64", 6, "fixed64"), field("f32", 7, "fixed32"), field("b", 8, "bool"),
        field("s", 9, "string"), field("by", 12, "bytes"), field("u32", 13, "uint32"),
        field("color", 14, "enum", ".t.All.Color"), field("sf32", 15, "sfixed32"), field("sf64", 16, "sfixed64"),
        field("si32", 17, "sint32"), field("si64", 18, "sint64"),
        field("inner", 20, "message", ".t.Inner"),
        field("packed", 21, "int32", repeated=True),
        field("unpacked", 22, "int32", repeated=True, packed=False),
        field("floats", 23, "float", repeated=True),
        field("inners", 24, "message", ".t.Inner", repeated=True),
        field("smap", 25, "message", ".t.All.SmapEntry", repeated=True),
        field("imap", 26, "message", ".t.All.ImapEntry", repeated=True),
        field("reboot", 30, "message", ".t.Empty", oneof=0),
        field("get", 31, "message", ".t.Inner", oneof=0),
        field("deep", 40, "message", "Nested.Deep"),          # relative, as protoc may write it
        field("dep", 41, "message", ".t.other.Dep"),
        field("colors", 42, "enum", ".t.All.Color", repeated=True),
        field("sints", 43, "sint64", repeated=True),
        field("doubles", 44, "double", repeated=True),
    ], nested=[
        message("SmapEntry", [field("key", 1, "string"), field("value", 2, "int32")], map_entry=True),
        message("ImapEntry", [field("key", 1, "int32"), field("value", 2, "message", ".t.Inner")], map_entry=True),
        message("Nested", nested=[message("Deep", [field("x", 1, "int32")])]),
    ], enums=[color], oneofs=["op"])
    main = file("test.proto", "t", messages=[message("Empty"), inner, all_], deps=["other.proto"])
    return [main, other]


def device_files() -> list:
    """A tiny SpaceX.API.Device, shaped like the real one: Request/Response with an op one-of."""
    status = message("DishGetStatusResponse", [field("uptime_s", 1, "uint64"), field("snr", 2, "float"),
                                               field("state", 3, "enum", ".SpaceX.API.Device.State")])
    req = message("Request", [
        field("id", 1, "uint64"),
        field("reboot", 1001, "message", ".SpaceX.API.Device.RebootRequest", oneof=0),
        field("get_status", 1004, "message", ".SpaceX.API.Device.GetStatusRequest", oneof=0),
    ], oneofs=["request"])
    resp = message("Response", [
        field("id", 1, "uint64"),
        field("reboot", 2001, "message", ".SpaceX.API.Device.RebootResponse", oneof=0),
        field("dish_get_status", 2004, "message", ".SpaceX.API.Device.DishGetStatusResponse", oneof=0),
    ], oneofs=["response"])
    common = file("spacex/api/common.proto", "SpaceX.API.Device",
                  enums=[enum("State", [("UNKNOWN", 0), ("CONNECTED", 1)])])
    dev = file("spacex/api/device.proto", "SpaceX.API.Device", deps=["spacex/api/common.proto"], messages=[
        message("RebootRequest"), message("GetStatusRequest"), message("RebootResponse"), status, req, resp])
    return [dev, common]
