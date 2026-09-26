"""--capture DIR: read a dish (and its router) once, scrub what they said, and write one JSON file in the fixture format.

For bug reports and new fixtures. Read-only and one-shot: every operation here only asks, and the dish's reads are
spaced SPACING_S apart. Nothing is sent that would make a device do work.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time

from .grpcweb import GrpcError

DISH_READS = ("get_device_info", "get_status", "get_history", "dish_get_obstruction_map", "dish_get_config",
              "get_diagnostics", "get_location")
ROUTER_READS = ("get_device_info", "get_status", "wifi_get_clients", "get_ping")
SMALL_READS = ("get_device_info", "get_status", "dish_get_config")       # tests/fixtures/standard.json's shape
SPACING_S = 2.0

# Scrubbing hands out replacements in order of first sight, so the order is fixed: by device, then by these labels
# (the order the first fixtures were made in). The same capture always scrubs to the same file.
LABELS = {"get_device_info": "device_info", "get_status": "status", "get_history": "history",
          "dish_get_obstruction_map": "obstruction_map", "dish_get_config": "config", "get_diagnostics": "diagnostics",
          "get_location": "location", "wifi_get_clients": "clients", "get_ping": "ping"}

# ---- scrubbing

MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}$")
ID_KEYS = {"id", "device_id", "dish_id", "captive_client_id", "account_shard"}
NAME_KEYS = {"name", "given_name", "hostname"}
MAC_KEYS = {"mac_address", "upstream_mac_address", "mac_lan", "mac_wan", "bssid"}
IP_KEYS = {"ip_address", "ipv4_wan_address", "ipv6_wan_addresses", "ipv6_addresses", "subnet", "ipv4",
           "server_addresses", "address"}
DROP_KEYS = re.compile(r"(password|passphrase|psk|secret|token|key)$", re.I)
KEEP_IPS = {"192.168.100.1", "0.0.0.0", "127.0.0.1"}     # the dish's fixed address and the non-addresses


class Scrub:
    """Replaces what identifies a household with stable stand-ins: the same value always gets the same stand-in."""

    def __init__(self):
        self.maps = {}
        self.counts = {}            # kind → values replaced (occurrences), or keys dropped

    def _count(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def sub(self, kind: str, value, make):
        m = self.maps.setdefault(kind, {})
        if value not in m:
            m[value] = make(len(m) + 1)
        self._count(kind.split(":")[0])
        return m[value]

    def ip(self, s: str) -> str:
        try:
            a = ipaddress.ip_address(s.split("/")[0])
        except ValueError:
            return s
        suffix = "/" + s.split("/")[1] if "/" in s else ""
        if a.version == 6:
            return self.sub("ip", s, lambda n: f"2001:db8::{n:x}")     # the prefix length goes too
        if str(a) in KEEP_IPS:
            return s
        if a.is_private and str(a).startswith("192.168."):           # a LAN → Starlink's default LAN, same host
            self._count("ip")
            return f"192.168.1.{str(a).split('.')[3]}{suffix}"
        if a.is_private or str(a).startswith("100."):
            return self.sub("ip:4", s, lambda n: f"100.64.0.{n}") + suffix
        return s                                                       # public service targets (ping) stay

    def walk(self, o, key: str = ""):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if DROP_KEYS.search(k):
                    self._count("credential key")
                    continue
                if k.startswith("Router-"):                             # downstream_routers is keyed by router id
                    k = self.sub("router id", k, lambda n: f"Router-{n:024d}")
                out[k] = self.walk(v, k)
            return out
        if isinstance(o, list):
            if key == "connected_routers":
                return [self.sub("router id", x, lambda n: f"Router-{n:024d}") for x in o]
            return [self.walk(v, key) for v in o]
        if isinstance(o, str):
            if key in ID_KEYS:
                return self.sub("id:" + key, o, lambda n: f"{key}-demo-{n}")
            if key in NAME_KEYS:
                return self.sub("name", o, lambda n: f"client-{n}")
            if key == "ssid":
                return self.sub("ssid", o, lambda n: f"STARLINK-DEMO-{n}")
            if key in MAC_KEYS or MAC_RE.match(o):
                return self.sub("mac", o.lower(), lambda n: f"02:00:00:00:00:{n:02x}")
            if key in ("domain", "domains") and o != "lan":
                self._count("domain")
                return "lan"
            if key in IP_KEYS:
                return self.ip(o)
        return o


def scrub(capture: dict) -> tuple:
    """A capture {"dish": {op: Response}, "router": {…}, "errors": {…}} → (the scrubbed copy, counts per kind)."""
    sc = Scrub()
    out = {"dish": {}, "router": {}, "errors": {}}
    for dev in ("dish", "router"):
        for op in sorted(capture.get(dev, {}), key=lambda op: LABELS.get(op, op)):
            out[dev][op] = sc.walk(capture[dev][op])
    for dev, errs in capture.get("errors", {}).items():
        out["errors"][dev] = dict(errs)
    for dev in ("dish", "router"):
        out["errors"].setdefault(dev, {})
    return out, dict(sorted(sc.counts.items()))


# ---- reading

def read(dev, ops, *, spacing: float = 0.0, sleep=time.sleep, say=print) -> tuple:
    """Each op once, in order, spacing seconds apart → ({op: Response}, {op: {"code", "message"}}). Stops at the
    first transport error: a device that isn't there would only make the rest wait out their timeouts."""
    answers, errors = {}, {}
    for i, op in enumerate(ops):
        if i and spacing:
            sleep(spacing)
        try:
            answers[op] = dev.call(op, timeout=20)
            say(f"  {op}: ok")
        except GrpcError as e:
            errors[op] = {"code": e.code, "message": e.message}
            say(f"  {op}: {e.name}")
        except (ValueError, KeyError):            # the codec: this firmware's schema has no such operation
            errors[op] = {"code": 12, "message": f"{op} is not in this firmware's schema"}     # UNIMPLEMENTED
            say(f"  {op}: not in this firmware")
            continue
        except OSError as e:
            errors[op] = {"code": 14, "message": f"unreachable: {e}"}         # UNAVAILABLE
            say(f"  {op}: unreachable ({e}); not reading the rest")
            break
    return answers, errors


def capture(dish, router, *, small: bool = False, sleep=time.sleep, say=print) -> dict:
    out = {"dish": {}, "router": {}, "errors": {"dish": {}, "router": {}}}
    say("dish:")
    out["dish"], out["errors"]["dish"] = read(dish, SMALL_READS if small else DISH_READS, spacing=SPACING_S,
                                              sleep=sleep, say=say)
    if router is not None and not small:
        say("router:")
        out["router"], out["errors"]["router"] = read(router, ROUTER_READS, say=say)
    return out


def write(folder: str, data: dict, *, now: float | None = None) -> str:
    """One file, named for the time, in the fixtures' format: sorted keys, compact, NaN as the decoder gave it."""
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    path = os.path.join(folder, f"localdish-capture-{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
    return path


def run(folder: str, dish, router, *, say=print) -> int:
    """--capture: read, scrub, write, say what happened. Exit status 0 if the dish answered anything, else 1."""
    raw = capture(dish, router, say=say)
    data, counts = scrub(raw)
    path = write(folder, data)
    for dev in (dish, router):
        if dev is not None:
            dev.close()
    kinds = ", ".join(f"{n} {kind}" for kind, n in counts.items()) or "nothing"
    say(f"scrubbed: {kinds}")
    say(f"wrote {path} ({os.path.getsize(path)} bytes). Look it over before you share it.")
    return 0 if data["dish"] else 1
