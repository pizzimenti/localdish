"""localdish [--port 8686] [--dish 192.168.100.1] [--router auto|ADDRESS|none] [--demo] [--capture DIR] [--version]"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys

from . import __version__

DISH_PORT = 9201          # gRPC-web
ROUTER_PORT = 9001        # gRPC-web
FACTORY_ROUTER = "192.168.1.1"
PROBE_TIMEOUT_S = 1.0


# ---- router discovery

def linux_gateways(text: str) -> list:
    """Default gateways from /proc/net/route: destination 0.0.0.0 with the gateway flag, addresses little-endian hex."""
    out = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 4 or cols[1] != "00000000":
            continue
        try:
            flags, gw = int(cols[3], 16), int(cols[2], 16)
        except ValueError:
            continue
        if flags & 0x2 and gw:
            out.append(socket.inet_ntoa(struct.pack("<I", gw)))
    return out


def macos_gateways(text: str) -> list:
    """From `route -n get default`: a line `    gateway: 192.168.1.1`."""
    return re.findall(r"^\s*gateway:\s*(\S+)", text, re.M)


def windows_gateways(text: str) -> list:
    """From `ipconfig`: `Default Gateway . . . : fe80::1%12` followed, perhaps, by more addresses on their own lines."""
    out, inside = [], False
    for line in text.splitlines():
        if "Default Gateway" in line or "Standardgateway" in line:
            inside = True
            line = line.split(":", 1)[1] if ":" in line else ""
        elif inside and ":" in line and not re.match(r"^\s+[0-9a-fA-F:.%]+\s*$", line):
            inside = False
        if inside:
            out.extend(a for a in line.split() if _ipv4(a))
    return out


def _ipv4(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address)
    except ValueError:
        return False


def _run(cmd: list) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gateway_candidates() -> list:
    """Where the router may be, most likely first: this computer's default gateways, then the factory address."""
    found = []
    if os.path.exists("/proc/net/route"):
        try:
            with open("/proc/net/route") as f:
                found = linux_gateways(f.read())
        except OSError:
            pass
    elif sys.platform == "darwin":
        found = macos_gateways(_run(["route", "-n", "get", "default"]))
    elif sys.platform.startswith("win"):
        found = windows_gateways(_run(["ipconfig"]))
    out = []
    for a in [g for g in found if _ipv4(g)] + [FACTORY_ROUTER]:
        if a not in out:
            out.append(a)
    return out


def discover_router(candidates: list, port: int = ROUTER_PORT, timeout: float = PROBE_TIMEOUT_S,
                    connect=socket.create_connection):
    """The first candidate whose gRPC-web port accepts a TCP connection within the timeout, or None."""
    for host in candidates:
        try:
            connect((host, port), timeout=timeout).close()
            return host
        except OSError:
            continue
    return None


# ---- wiring

def parse(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="localdish", description="a local web page for a Starlink dish and its router")
    p.add_argument("--port", type=int, default=8686, help="the page's port on 127.0.0.1 (default 8686)")
    p.add_argument("--dish", default="192.168.100.1", help="the dish's address (default 192.168.100.1)")
    p.add_argument("--router", default="auto",
                   help="the router's address, 'auto' to find it (the default), or 'none' to leave it out")
    p.add_argument("--demo", action="store_true", help="show a recorded Starlink Mini; talk to no device")
    p.add_argument("--capture", metavar="DIR",
                   help="read the dish and router once, scrub what identifies you, write one JSON file to DIR, and exit")
    p.add_argument("--version", action="version", version=f"localdish {__version__}")
    return p.parse_args(argv)


def real_devices(args: argparse.Namespace) -> tuple:
    """(dish, router or None, the router's address or None) as grpcweb.Devices, finding the router if asked."""
    from . import grpcweb
    if args.router == "none":
        router_host = None
    elif args.router == "auto":
        router_host = discover_router(gateway_candidates())
    else:
        router_host = args.router
    dish = grpcweb.Device(args.dish, DISH_PORT)
    router = grpcweb.Device(router_host, ROUTER_PORT) if router_host else None
    return dish, router, router_host


def build(args: argparse.Namespace, *, device=None, explain=None, log=None):
    """(poller, http server, the startup line). device and explain default to localdish's own modules."""
    from . import poller as poller_mod
    from . import server
    if device is None:
        from . import device
    if explain is None:
        from . import explain

    holder = {}

    def event_log(text):     # the demo's "demo: …" lines become events on the page, once the poller exists
        if "poller" in holder:
            holder["poller"].event("app", text)

    if args.demo:
        from . import demo
        dish, router = demo.devices(log=event_log)
        hosts = {"dish": "demo", "router": "demo" if router else None}
        where = "demo: a recorded Starlink Mini"
    else:
        dish, router, router_host = real_devices(args)
        hosts = {"dish": args.dish, "router": router_host}
        where = f"dish {args.dish}, router {router_host or 'none'}"

    p = poller_mod.Poller({"dish": dish, "router": router}, device=device, explain=explain, hosts=hosts,
                          demo=args.demo, log=log)
    holder["poller"] = p
    httpd = server.make_server(p, "127.0.0.1", args.port)
    line = f"localdish {__version__} — http://127.0.0.1:{httpd.server_address[1]} ({where})"
    return p, httpd, line


def main(argv=None) -> int:
    args = parse(argv)
    if args.capture:
        from . import capture
        if args.demo:
            from . import demo
            dish, router = demo.devices()
            where = "the demo's recorded Starlink Mini"
        else:
            dish, router, router_host = real_devices(args)
            where = f"dish {args.dish}, router {router_host or 'none'}"
        print(f"localdish {__version__} capture ({where}): read-only, one read of each, {capture.SPACING_S:g} s apart",
              flush=True)
        return capture.run(args.capture, dish, router)
    try:
        p, httpd, line = build(args)
    except OSError as e:
        print(f"localdish: cannot listen on 127.0.0.1:{args.port} ({e.strerror or e}); try --port {args.port + 1}",
              file=sys.stderr)
        return 1
    print(line, flush=True)
    p.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        p.stop()
    return 0
