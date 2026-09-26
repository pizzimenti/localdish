"""Protobuf codec driven by descriptors fetched from the device (see DESIGN.md "wire.py"). Standard library only."""
from __future__ import annotations

import base64
import struct

# FieldDescriptorProto.Type
_KINDS = {
    1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32", 6: "fixed64", 7: "fixed32", 8: "bool",
    9: "string", 10: "group", 11: "message", 12: "bytes", 13: "uint32", 14: "enum", 15: "sfixed32",
    16: "sfixed64", 17: "sint32", 18: "sint64",
}
_VARINT = {"int64", "uint64", "int32", "bool", "uint32", "enum", "sint32", "sint64"}
_FIXED = {"double": "d", "fixed64": "Q", "sfixed64": "q", "float": "f", "fixed32": "I", "sfixed32": "i"}
_WIRE = dict([(k, 0) for k in _VARINT] + [(k, 5 if struct.calcsize(c) == 4 else 1) for k, c in _FIXED.items()]
             + [("string", 2), ("bytes", 2), ("message", 2), ("group", 3)])
_MASK32, _MASK64 = (1 << 32) - 1, (1 << 64) - 1
_F32 = struct.Struct("<f")
_INF = float("inf")


def _varint(buf, i: int):
    b = buf[i]
    if b < 0x80:
        return b, i + 1
    r, s = b & 0x7F, 7
    i += 1
    while True:
        b = buf[i]
        i += 1
        r |= (b & 0x7F) << s
        if b < 0x80:
            return r, i
        s += 7


def _put_varint(out: bytearray, v: int) -> None:
    v &= _MASK64
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def _signed(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def f32(v: float) -> float:
    """The shortest decimal that round-trips to the same float32 (27.45, not 27.450000762939453)."""
    if v != v or v == _INF or v == -_INF:
        return v
    want = _F32.pack(v)

    def ok(p: int) -> bool:
        try:
            return _F32.pack(float("%.*g" % (p, v))) == want
        except OverflowError:                       # rounded past the float32 range
            return False

    if int.from_bytes(want, "little") & 0x7FFFFF:
        # the round-trip interval is symmetric, so more digits never stop round-tripping: bisect
        lo, hi = 1, 9
        while lo < hi:
            mid = (lo + hi) // 2
            if ok(mid):
                hi = mid
            else:
                lo = mid + 1
    else:
        # a power of two: the interval below is half the one above, so walk up like the definition says
        lo = 1
        while lo < 9 and not ok(lo):
            lo += 1
    return float("%.*g" % (lo, v))


def _skip_group(buf, i: int, num: int) -> int:
    while True:
        key, i = _varint(buf, i)
        wt = key & 7
        if wt == 0:
            _, i = _varint(buf, i)
        elif wt == 1:
            i += 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            i += ln
        elif wt == 3:
            i = _skip_group(buf, i, key >> 3)
        elif wt == 4:
            if key >> 3 != num:
                raise ValueError("mismatched end group")
            return i
        elif wt == 5:
            i += 4
        else:
            raise ValueError("bad wire type %d" % wt)


def _raw_fields(buf: bytes) -> dict:
    """Field number → list of raw values, for reading descriptors."""
    out: dict = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        wt = key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 2:
            ln, i = _varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wt == 5:
            v = buf[i:i + 4]
            i += 4
        elif wt == 1:
            v = buf[i:i + 8]
            i += 8
        elif wt == 3:
            i = _skip_group(buf, i, key >> 3)
            continue
        else:
            raise ValueError("bad wire type %d" % wt)
        out.setdefault(key >> 3, []).append(v)
    if i != n:
        raise ValueError("truncated descriptor")
    return out


def _str(raw: dict, num: int, default: str = "") -> str:
    v = raw.get(num)
    return bytes(v[-1]).decode("utf-8") if v else default


def _int(raw: dict, num: int, default: int = 0) -> int:
    v = raw.get(num)
    return v[-1] if v else default


class Field:
    """One field of a message, as the schema describes it."""

    __slots__ = ("name", "number", "kind", "type_name", "repeated", "map", "oneof", "packed",
                 "_wire", "_msg", "_enum", "_siblings")

    def __init__(self, name: str, number: int, kind: str, type_name: str | None, repeated: bool,
                 oneof: str | None, packed: bool):
        self.name = name
        self.number = number
        self.kind = kind                    # "int32", "float", "enum", "message", … (descriptor.proto's names)
        self.type_name = type_name          # full name of the message or enum, without the leading dot
        self.repeated = repeated
        self.map = False                    # a map<k, v>: repeated entries of a map_entry message
        self.oneof = oneof                  # the oneof's name, if the field is in one
        self.packed = packed                # how encode writes a repeated number; decode accepts both
        self._wire = _WIRE.get(kind, 2)
        self._msg: _Message | None = None
        self._enum: dict | None = None      # number → name
        self._siblings: tuple = ()          # other members of the same oneof

    def __repr__(self) -> str:
        return "Field(%r, %d, %s%s)" % (self.name, self.number, "repeated " if self.repeated else "",
                                        self.type_name or self.kind)


class _Message:
    __slots__ = ("name", "fields", "by_number", "map_entry")

    def __init__(self, name: str):
        self.name = name
        self.fields: dict = {}
        self.by_number: dict = {}
        self.map_entry = False


def _default(f: Field):
    """A map entry's missing key or value (the one place protobuf fills a default in)."""
    k = f.kind
    if k == "message":
        return {}
    if k == "enum":
        return f._enum.get(0, 0) if f._enum is not None else 0
    if k in ("string", "bytes"):
        return ""
    if k == "bool":
        return False
    return 0.0 if k in ("float", "double") else 0


class Schema:
    """Every message and enum in a set of FileDescriptorProto blobs (a device's reflection output)."""

    def __init__(self) -> None:
        self._messages: dict = {}           # full name → _Message
        self._enums: dict = {}              # full name → {name: number}
        self._enum_names: dict = {}         # full name → {number: name}

    @classmethod
    def from_files(cls, files: list) -> "Schema":
        self = cls()
        pending = []                        # (Field, scope, raw type_name, raw type)
        for blob in files:
            fd = _raw_fields(bytes(blob))
            pkg = _str(fd, 2)
            syntax = _str(fd, 12, "proto2")
            for m in fd.get(4, ()):
                self._add_message(m, pkg, syntax, pending)
            for e in fd.get(5, ()):
                self._add_enum(e, pkg)
        for f, scope, tname, ftype in pending:
            full = self._resolve(tname, scope)
            if full in self._messages:
                f.kind, f._wire, f.type_name, f._msg = "message", 2, full, self._messages[full]
            elif full in self._enums:
                f.kind, f._wire, f.type_name, f._enum = "enum", 0, full, self._enum_names[full]
            else:
                f.type_name = tname.lstrip(".")    # unresolved: decoded as an unknown field
        for m in self._messages.values():
            for f in m.fields.values():
                f.map = bool(f.repeated and f._msg is not None and f._msg.map_entry)
        return self

    def _add_message(self, blob, scope: str, syntax: str, pending: list) -> None:
        d = _raw_fields(bytes(blob))
        full = _join(scope, _str(d, 1))
        m = _Message(full)
        self._messages[full] = m
        opts = d.get(7)
        m.map_entry = bool(opts and _int(_raw_fields(bytes(opts[-1])), 7))
        oneofs = [_str(_raw_fields(bytes(o)), 1) for o in d.get(8, ())]
        members: dict = {}
        for fb in d.get(2, ()):
            fr = _raw_fields(bytes(fb))
            ftype = _int(fr, 5)
            kind = _KINDS.get(ftype, "message")
            repeated = _int(fr, 4) == 3
            oi = fr.get(9)
            oneof = oneofs[oi[-1]] if oi and oi[-1] < len(oneofs) else None
            fopts = fr.get(8)
            packed_opt = _raw_fields(bytes(fopts[-1])).get(2) if fopts else None
            if packed_opt:
                packed = bool(packed_opt[-1])
            else:
                packed = syntax != "proto2"
            packed = packed and repeated and (kind in _VARINT or kind in _FIXED)
            f = Field(_str(fr, 1), _int(fr, 3), kind, None, repeated, oneof, packed)
            m.fields[f.name] = f
            m.by_number[f.number] = f
            if oneof is not None:
                members.setdefault(oneof, []).append(f)
            if 6 in fr:
                pending.append((f, full, _str(fr, 6), ftype))
        for fs in members.values():
            for f in fs:
                f._siblings = tuple(g.name for g in fs if g is not f)
        for n in d.get(3, ()):
            self._add_message(n, full, syntax, pending)
        for e in d.get(4, ()):
            self._add_enum(e, full)

    def _add_enum(self, blob, scope: str) -> None:
        d = _raw_fields(bytes(blob))
        full = _join(scope, _str(d, 1))
        by_name: dict = {}
        by_number: dict = {}
        for vb in d.get(2, ()):
            v = _raw_fields(bytes(vb))
            name, num = _str(v, 1), _signed(_int(v, 2), 32)
            by_name[name] = num
            by_number.setdefault(num, name)     # aliases: the first name wins, as protobuf does
        self._enums[full] = by_name
        self._enum_names[full] = by_number

    def _resolve(self, name: str, scope: str) -> str:
        if name.startswith("."):
            return name[1:]
        parts = scope.split(".") if scope else []
        for k in range(len(parts), -1, -1):     # innermost scope first, as protoc does
            cand = _join(".".join(parts[:k]), name)
            if cand in self._messages or cand in self._enums:
                return cand
        return name

    # ---- lookups ------------------------------------------------------------------------------------

    def _message(self, name: str) -> _Message:
        try:
            return self._messages[name.lstrip(".")]
        except KeyError:
            raise KeyError("no message %r in the schema" % name) from None

    def message_names(self) -> list:
        return sorted(self._messages)

    def fields(self, message: str) -> dict:
        return dict(self._message(message).fields)

    def enum_values(self, enum: str) -> dict:
        try:
            return dict(self._enums[enum.lstrip(".")])
        except KeyError:
            raise KeyError("no enum %r in the schema" % enum) from None

    # ---- decode -------------------------------------------------------------------------------------

    def decode(self, message: str, data: bytes) -> dict:
        try:
            return self._decode(self._message(message), bytes(data), {})
        except (IndexError, struct.error) as e:
            raise ValueError("truncated or malformed %s: %s" % (message, e)) from None

    def _decode(self, m: _Message, buf: bytes, f32cache: dict) -> dict:
        out: dict = {}
        mraw: dict = {}                     # a singular message seen twice merges, like protobuf: re-decode the concatenation
        by_number = m.by_number
        i, n = 0, len(buf)
        while i < n:
            key, i = _varint(buf, i)
            num, wt = key >> 3, key & 7
            if wt == 0:
                raw, i = _varint(buf, i)
            elif wt == 2:
                ln, i = _varint(buf, i)
                j = i + ln
                if j > n:
                    raise IndexError("length past the end")
                raw = buf[i:j]
                i = j
            elif wt == 5:
                raw = buf[i:i + 4]
                i += 4
            elif wt == 1:
                raw = buf[i:i + 8]
                i += 8
            elif wt == 3:
                i = _skip_group(buf, i, num)
                continue
            else:
                raise IndexError("bad wire type %d" % wt)
            if i > n:
                raise IndexError("value past the end")
            f = by_number.get(num)
            if f is None or (f.kind == "message" and f._msg is None) or (f.kind == "enum" and f._enum is None):
                _unknown(out, num, wt, raw)
                continue
            if wt != f._wire:
                if f.repeated and wt == 2 and f._wire != 2:
                    out.setdefault(f.name, []).extend(self._packed(f, raw, f32cache))
                else:
                    _unknown(out, num, wt, raw)     # a type the schema doesn't expect: keep it, by number
                continue
            name = f.name
            if f.map:
                entry = self._decode(f._msg, raw, f32cache)
                kf, vf = f._msg.by_number.get(1), f._msg.by_number.get(2)
                k = entry.get("key", _default(kf) if kf else "")
                v = entry.get("value", _default(vf) if vf else None)
                out.setdefault(name, {})[str(k)] = v
                continue
            if f.kind == "message":
                if f.repeated:
                    out.setdefault(name, []).append(self._decode(f._msg, raw, f32cache))
                    continue
                if num in mraw:
                    raw = mraw[num] + raw
                mraw[num] = raw
                v = self._decode(f._msg, raw, f32cache)
            else:
                v = _scalar(f, wt, raw, f32cache)
            if f.repeated:
                out.setdefault(name, []).append(v)
            else:
                for s in f._siblings:
                    out.pop(s, None)
                out[name] = v
        return out

    def _packed(self, f: Field, raw: bytes, f32cache: dict) -> list:
        code = _FIXED.get(f.kind)
        if code is not None:
            size = struct.calcsize(code)
            if len(raw) % size:
                raise IndexError("packed %s not a multiple of %d bytes" % (f.kind, size))
            vals = list(struct.unpack("<%d%s" % (len(raw) // size, code), raw))
            if code == "f":
                vals = [_cached_f32(v, f32cache) for v in vals]
            return vals
        out = []
        i, n = 0, len(raw)
        while i < n:
            v, i = _varint(raw, i)
            out.append(_scalar(f, 0, v, f32cache))
        if i != n:
            raise IndexError("packed varints past the end")
        return out

    # ---- encode -------------------------------------------------------------------------------------

    def encode(self, message: str, value: dict) -> bytes:
        out = bytearray()
        self._encode(self._message(message), value, out)
        return bytes(out)

    def _encode(self, m: _Message, value: dict, out: bytearray) -> None:
        if not isinstance(value, dict):
            raise ValueError("%s wants a dict, got %s" % (m.name, type(value).__name__))
        items = []
        for name, v in value.items():
            f = m.fields.get(name)
            if f is None:
                raise ValueError("%s has no field %r" % (m.name, name))
            if v is None:
                continue
            if f.kind in ("message", "enum") and f._msg is None and f._enum is None:
                raise ValueError("%s.%s has a type the schema doesn't define" % (m.name, name))
            for s in f._siblings:
                if value.get(s) is not None:
                    raise ValueError("%s: %r and %r are in the same oneof" % (m.name, name, s))
            items.append((f.number, f, v))
        items.sort(key=lambda t: t[0])
        for _, f, v in items:
            try:
                if f.map:
                    if not isinstance(v, dict):
                        raise ValueError("a map wants a dict")
                    kf = f._msg.by_number[1]
                    for k, mv in v.items():
                        entry = bytearray()
                        self._encode_one(kf, _map_key(kf, k), entry)
                        self._encode_one(f._msg.by_number[2], mv, entry)
                        _put_varint(out, f.number << 3 | 2)
                        _put_varint(out, len(entry))
                        out += entry
                elif f.repeated:
                    if not isinstance(v, (list, tuple)):
                        raise ValueError("a repeated field wants a list")
                    if f.packed and v:
                        body = bytearray()
                        for x in v:
                            _put_scalar(f, x, body, self)
                        _put_varint(out, f.number << 3 | 2)
                        _put_varint(out, len(body))
                        out += body
                    else:
                        for x in v:
                            self._encode_one(f, x, out)
                else:
                    self._encode_one(f, v, out)
            except (TypeError, struct.error, OverflowError) as e:
                raise ValueError("%s.%s: %s" % (m.name, f.name, e)) from None

    def _encode_one(self, f: Field, v, out: bytearray) -> None:
        if f.kind == "message":
            body = bytearray()
            self._encode(f._msg, v, body)
            _put_varint(out, f.number << 3 | 2)
            _put_varint(out, len(body))
            out += body                     # an empty message still writes its tag: that is how an op is chosen
            return
        _put_varint(out, f.number << 3 | f._wire)
        if f._wire == 2:
            if f.kind == "string":
                if not isinstance(v, str):
                    raise ValueError("%s wants a str" % f.name)
                b = v.encode("utf-8")
            elif isinstance(v, (bytes, bytearray)):
                b = bytes(v)
            elif isinstance(v, str):
                try:
                    b = base64.b64decode(v, validate=True)
                except ValueError:
                    raise ValueError("%s wants bytes or base64" % f.name) from None
            else:
                raise ValueError("%s wants bytes or base64" % f.name)
            _put_varint(out, len(b))
            out += b
        else:
            _put_scalar(f, v, out, self)


def _join(scope: str, name: str) -> str:
    return scope + "." + name if scope else name


def _cached_f32(v: float, cache: dict) -> float:
    r = cache.get(v)
    if r is None:
        r = cache[v] = f32(v)
    return r


def _scalar(f: Field, wt: int, raw, f32cache: dict):
    k = f.kind
    if wt == 0:
        if k == "enum":
            v = _signed(raw, 32)
            return f._enum.get(v, v)
        if k == "bool":
            return raw != 0
        if k == "int64":
            return _signed(raw, 64)
        if k == "int32":
            return _signed(raw, 32)
        if k == "uint32":
            return raw & _MASK32
        if k == "sint32" or k == "sint64":
            raw &= _MASK64 if k == "sint64" else _MASK32
            return (raw >> 1) ^ -(raw & 1)
        return raw & _MASK64                # uint64
    if wt == 2:
        if k == "string":
            return raw.decode("utf-8", "replace")
        return base64.b64encode(raw).decode("ascii")
    v = struct.unpack("<" + _FIXED[k], raw)[0]
    return _cached_f32(v, f32cache) if k == "float" else v


def _unknown(out: dict, num: int, wt: int, raw) -> None:
    if wt == 2:
        v = base64.b64encode(raw).decode("ascii")
    elif wt == 0:
        v = raw
    else:
        v = int.from_bytes(raw, "little")
    key = "#%d" % num
    if key in out:
        prev = out[key]
        if isinstance(prev, list):
            prev.append(v)
        else:
            out[key] = [prev, v]
    else:
        out[key] = v


def _map_key(f: Field, k):
    if f.kind == "string":
        return str(k)
    if f.kind == "bool":
        if isinstance(k, str):
            if k.lower() not in ("true", "false"):
                raise ValueError("bad bool map key %r" % k)
            return k.lower() == "true"
        return bool(k)
    try:
        return int(k)
    except ValueError:
        raise ValueError("bad integer map key %r" % k) from None


def _put_scalar(f: Field, v, out: bytearray, schema: Schema) -> None:
    k = f.kind
    if k == "enum":
        if isinstance(v, str):
            names = schema._enums[f.type_name]
            if v not in names:
                raise ValueError("%s has no value %r" % (f.type_name, v))
            v = names[v]
        elif not isinstance(v, int):
            raise ValueError("%s wants an enum name or number" % f.name)
        _put_varint(out, v)
        return
    if k in ("float", "double"):
        if not isinstance(v, (int, float)):
            raise ValueError("%s wants a number" % f.name)
        out += struct.pack("<" + _FIXED[k], v)
        return
    if not isinstance(v, int):
        raise ValueError("%s wants an int" % f.name)
    if k in _FIXED:
        out += struct.pack("<" + _FIXED[k], v)
    elif k == "sint32" or k == "sint64":
        bits = 64 if k == "sint64" else 32
        _put_varint(out, ((v << 1) ^ (v >> (bits - 1))) & ((1 << bits) - 1))
    elif k in ("uint32", "uint64") and v < 0:
        raise ValueError("%s is unsigned" % f.name)
    else:
        _put_varint(out, int(v))
