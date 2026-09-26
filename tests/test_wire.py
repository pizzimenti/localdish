from __future__ import annotations

import json
import math
import random
import struct
import unittest

from localdish import wire
from tests.protobuild import fx32, fx64, ld, tag, test_files, varint, vi

ALL = "t.All"


def f32_reference(v: float) -> float:
    """The definition, spelled out: try 1, 2, … 9 significant digits."""
    want = struct.pack("<f", v)
    for p in range(1, 10):
        s = float("%.*g" % (p, v))
        try:
            if struct.pack("<f", s) == want:
                return s
        except OverflowError:
            pass
    return v


def as_f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.s = wire.Schema.from_files(test_files())

    def test_names_fields_enums(self):
        names = self.s.message_names()
        for n in ("t.All", "t.Inner", "t.All.Nested.Deep", "t.All.SmapEntry", "t.other.Dep", "t.Empty"):
            self.assertIn(n, names)
        f = self.s.fields(ALL)
        self.assertEqual(f["inner"].type_name, "t.Inner")
        self.assertEqual(f["deep"].type_name, "t.All.Nested.Deep")     # relative name resolved from the inside out
        self.assertEqual(f["dep"].type_name, "t.other.Dep")
        self.assertEqual(f["color"].kind, "enum")
        self.assertTrue(f["smap"].map and f["imap"].map and not f["inners"].map)
        self.assertEqual(f["reboot"].oneof, "op")
        self.assertTrue(f["packed"].packed)
        self.assertFalse(f["unpacked"].packed)
        self.assertEqual(self.s.fields(".t.Inner"), self.s.fields("t.Inner"))
        self.assertEqual(self.s.enum_values("t.All.Color"), {"RED": 0, "GREEN": 1, "BLUE": 2, "NEGATIVE": -1})
        with self.assertRaises(KeyError):
            self.s.fields("t.Nope")

    def test_every_scalar(self):
        data = (fx64(1, "d", 0.1) + fx32(2, "f", 27.45) + vi(3, -2) + vi(4, (1 << 64) - 1) + vi(5, -1)
                + fx64(6, "Q", (1 << 64) - 1) + fx32(7, "I", (1 << 32) - 1) + vi(8, 1) + ld(9, b"caf\xc3\xa9 \xff")
                + ld(12, b"\x00\x01\xfe") + vi(13, 4000000000) + vi(14, 2) + fx32(15, "i", -5)
                + fx64(16, "q", -6) + vi(17, 5) + vi(18, (1 << 64) - 1))
        d = self.s.decode(ALL, data)
        self.assertEqual(d, {
            "d": 0.1, "f": 27.45, "i64": -2, "u64": (1 << 64) - 1, "i32": -1, "f64": (1 << 64) - 1,
            "f32": (1 << 32) - 1, "b": True, "s": "café �", "by": "AAH+", "u32": 4000000000, "color": "BLUE",
            "sf32": -5, "sf64": -6, "si32": -3, "si64": -(1 << 63),
        })
        for k in ("i64", "u64", "i32", "f64", "f32", "u32", "sf32", "sf64", "si32", "si64"):
            self.assertIs(type(d[k]), int, k)
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_enum_unknown_number_stays_int(self):
        self.assertEqual(self.s.decode(ALL, vi(14, 9)), {"color": 9})
        self.assertEqual(self.s.decode(ALL, vi(14, -1)), {"color": "NEGATIVE"})
        self.assertEqual(self.s.decode(ALL, ld(42, varint(0) + varint(7))), {"colors": ["RED", 7]})

    def test_absent_stays_absent(self):
        self.assertEqual(self.s.decode(ALL, b""), {})
        self.assertEqual(self.s.decode(ALL, ld(20, vi(1, 3))), {"inner": {"a": 3}})
        self.assertEqual(self.s.decode(ALL, ld(20, b"")), {"inner": {}})

    def test_nested_and_cross_package(self):
        d = self.s.decode(ALL, ld(40, vi(1, 7)) + ld(41, vi(1, 8)) + ld(24, vi(1, 1)) + ld(24, ld(2, "x")))
        self.assertEqual(d, {"deep": {"x": 7}, "dep": {"n": 8}, "inners": [{"a": 1}, {"s": "x"}]})

    def test_repeated_packed_and_unpacked(self):
        packed = ld(21, varint(1) + varint(-2) + varint(300))
        unpacked = vi(21, 1) + vi(21, -2) + vi(21, 300)
        want = {"packed": [1, -2, 300]}
        self.assertEqual(self.s.decode(ALL, packed), want)
        self.assertEqual(self.s.decode(ALL, unpacked), want)
        self.assertEqual(self.s.decode(ALL, vi(21, 1) + ld(21, varint(-2) + varint(300))), want)   # mixed is legal
        floats = ld(23, struct.pack("<3f", 0.1, -1.0, 27.45))
        self.assertEqual(self.s.decode(ALL, floats), {"floats": [0.1, -1.0, 27.45]})
        self.assertEqual(self.s.decode(ALL, fx32(23, "f", 0.5) + fx32(23, "f", 0.25)), {"floats": [0.5, 0.25]})
        self.assertEqual(self.s.decode(ALL, ld(43, varint(1) + varint(2) + varint(3))), {"sints": [-1, 1, -2]})
        self.assertEqual(self.s.decode(ALL, ld(44, struct.pack("<2d", 1.5, -2.25))), {"doubles": [1.5, -2.25]})

    def test_maps(self):
        entry = lambda k, v: ld(25, ld(1, k) + vi(2, v))
        d = self.s.decode(ALL, entry("a", 1) + entry("b", 2) + entry("a", 3))
        self.assertEqual(d, {"smap": {"a": 3, "b": 2}})                 # a repeated key: the last wins
        d = self.s.decode(ALL, ld(26, vi(1, 7) + ld(2, vi(1, 1))) + ld(26, vi(1, -3)) + ld(26, ld(2, b"")))
        self.assertEqual(d, {"imap": {"7": {"a": 1}, "-3": {}, "0": {}}})   # int keys as strings; defaults filled
        self.assertEqual(self.s.decode(ALL, ld(25, ld(1, "z"))), {"smap": {"z": 0}})

    def test_oneof_last_wins(self):
        self.assertEqual(self.s.decode(ALL, ld(30, b"") + ld(31, vi(1, 2))), {"get": {"a": 2}})
        self.assertEqual(self.s.decode(ALL, ld(31, vi(1, 2)) + ld(30, b"")), {"reboot": {}})
        with self.assertRaises(ValueError):
            self.s.encode(ALL, {"reboot": {}, "get": {}})

    def test_singular_message_twice_merges(self):
        d = self.s.decode(ALL, ld(20, vi(1, 3)) + ld(20, ld(2, "x")))
        self.assertEqual(d, {"inner": {"a": 3, "s": "x"}})

    def test_unknown_fields(self):
        data = vi(99, 5) + fx32(98, "I", 7) + fx64(97, "Q", 8) + ld(96, b"\x01\x02") + vi(99, 6)
        data += tag(95, 3) + vi(1, 1) + tag(95, 4)                          # a group: skipped
        d = self.s.decode(ALL, data + vi(5, 4))
        self.assertEqual(d, {"#99": [5, 6], "#98": 7, "#97": 8, "#96": "AQI=", "i32": 4})
        self.assertEqual(self.s.decode(ALL, fx32(5, "I", 1)), {"#5": 1})    # right number, wrong wire type

    def test_truncated_raises_value_error(self):
        for bad in (b"\x08", ld(9, "hello")[:-1], b"\x0d\x00", b"\x0f"):
            with self.assertRaises(ValueError):
                self.s.decode(ALL, bad)

    def test_round_trips(self):
        value = {
            "d": -0.5, "f": 27.45, "i64": -(1 << 63), "u64": (1 << 64) - 1, "i32": -(1 << 31), "f64": 1 << 40,
            "f32": 12, "b": False, "s": "snø", "by": "AAH+", "u32": 7, "color": "GREEN", "sf32": -1, "sf64": 2,
            "si32": -(1 << 31), "si64": (1 << 63) - 1, "inner": {"a": 1, "s": ""}, "packed": [1, -1, 0],
            "unpacked": [5, -5], "floats": [0.1, 3.4028235e38], "inners": [{}, {"a": 2}],
            "smap": {"x": 1, "": -2}, "imap": {"-4": {"a": 1}, "9": {}}, "get": {"a": 1}, "deep": {"x": -1},
            "dep": {"n": 0}, "colors": ["RED", "BLUE", 17], "sints": [-1, 0, 1], "doubles": [1e300],
        }
        blob = self.s.encode(ALL, value)
        self.assertEqual(self.s.decode(ALL, blob), value)
        self.assertEqual(self.s.encode(ALL, self.s.decode(ALL, blob)), blob)
        self.assertIn(ld(21, varint(1) + varint(-1) + varint(0)), blob)     # packed
        self.assertIn(vi(22, 5) + vi(22, -5), blob)                         # [packed = false]

    def test_encode_empty_message_emits_its_tag(self):
        self.assertEqual(self.s.encode(ALL, {"reboot": {}}), b"\xf2\x01\x00")
        self.assertEqual(self.s.encode(ALL, {"inner": {}, "i32": None}), ld(20, b""))

    def test_encode_accepts_enum_numbers_and_bytes(self):
        self.assertEqual(self.s.encode(ALL, {"color": 1}), self.s.encode(ALL, {"color": "GREEN"}))
        self.assertEqual(self.s.encode(ALL, {"by": b"\x00\x01\xfe"}), self.s.encode(ALL, {"by": "AAH+"}))

    def test_encode_rejects_what_it_cannot_write(self):
        for bad in ({"nope": 1}, {"inner": {"nope": 1}}, {"color": "PURPLE"}, {"i32": "1"}, {"s": 1},
                    {"u32": -1}, {"packed": 1}, {"smap": [1]}, {"imap": {"x": {}}}, {"by": "not base64!"},
                    {"inner": 5}):
            with self.assertRaises(ValueError, msg=bad):
                self.s.encode(ALL, bad)


class Float32Test(unittest.TestCase):
    def test_examples(self):
        for raw, want in ((27.45, 27.45), (0.1, 0.1), (-1.0, -1.0), (0.0, 0.0), (1e-45, 1e-45),
                          (3.4028234663852886e38, 3.4028235e38), (16777216.0, 16777216.0), (0.5, 0.5),
                          (123456.789, 123456.79), (-273.15, -273.15)):
            got = wire.f32(as_f32(raw))
            self.assertEqual(got, want, raw)
            self.assertEqual(struct.pack("<f", got), struct.pack("<f", raw))
        self.assertTrue(math.isnan(wire.f32(float("nan"))))
        self.assertEqual(wire.f32(float("-inf")), float("-inf"))
        self.assertEqual(math.copysign(1, wire.f32(-0.0)), -1)

    def test_matches_the_definition(self):
        rnd = random.Random(7)
        vals = [struct.unpack("<f", struct.pack("<I", rnd.getrandbits(32)))[0] for _ in range(3000)]
        vals += [as_f32(rnd.uniform(-1000, 1000)) for _ in range(3000)]
        vals += [math.ldexp(1.0, e) for e in range(-149, 128)]                     # every power of two
        vals += [as_f32(math.ldexp(1.0, e) * (1 - 2 ** -24)) for e in range(-120, 128)]   # just below each
        for v in vals:
            if math.isfinite(v):
                self.assertEqual(wire.f32(v), f32_reference(v), repr(v))


if __name__ == "__main__":
    unittest.main()
