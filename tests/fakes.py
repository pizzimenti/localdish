"""Tiny stand-ins for the devices, device.py and explain.py, so the poller and server are tested on their own."""
from __future__ import annotations

import threading
import time
import types
from typing import NamedTuple

from localdish.grpcweb import GrpcError


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeDev:
    """Answers {op: body}; an op mapped to an exception raises it. Records every call."""

    def __init__(self, answers: dict | None = None, delay: float = 0.0):
        self.answers = dict(answers or {})
        self.calls = []
        self.delay = delay
        self.schema = object()
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def call(self, op, fields=None, *, timeout=None):
        with self._lock:
            self.calls.append((op, fields, timeout))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            a = self.answers.get(op, {})
            if isinstance(a, BaseException):
                raise a
            if isinstance(a, type) and issubclass(a, BaseException):
                raise a()
            return {"api_version": 1, op: a}
        finally:
            with self._lock:
                self.active -= 1

    def ops(self) -> list:
        return [c[0] for c in self.calls]


class Control(NamedTuple):
    name: str
    label: str
    group: str
    target: str
    op: str
    confirm: str
    params: list


CONTROLS = [
    Control("restart", "restart", "restart", "dish", "reboot", "restart the dish?", []),
    Control("snow_melt", "snow melt", "settings", "dish", "dish_set_config", "change snow melt?",
            [{"name": "mode", "type": "choice", "choices": ["AUTO", "ALWAYS_ON", "ALWAYS_OFF"], "label": "mode"}]),
    Control("stow", "stow", "maintenance", "dish", "dish_stow", "stow the dish?", []),
    Control("speedtest", "speed test", "tests", "router", "start_speedtest", "run a speed test?", []),
    Control("ping", "ping", "tests", "router", "get_ping", "ping?", []),
]


def fake_device_module():
    def build(name, params):
        c = next(c for c in CONTROLS if c.name == name)
        if name == "snow_melt":
            if params.get("mode") not in ("AUTO", "ALWAYS_ON", "ALWAYS_OFF"):
                raise ValueError("mode must be AUTO, ALWAYS_ON or ALWAYS_OFF")
            return c.target, c.op, {"dish_config": {"snow_melt_mode": params["mode"], "apply_snow_melt_mode": True}}
        return c.target, c.op, {}

    def available(name, state):
        if name == "stow":
            return False, "this dish has no motors"
        return True, None

    def current(name, state):
        if name == "snow_melt":
            cfg = ((state.get("dish") or {}).get("config") or {}).get("dish_config", {})
            return {"mode": cfg.get("snow_melt_mode")}
        return {}

    return types.SimpleNamespace(CONTROLS=CONTROLS, build=build, available=available, current=current)


def fake_explain_module():
    def headline(status, error):
        if error:
            return {"text": "dish unreachable", "tone": "bad", "detail": error}
        if status is None:
            return {"text": "waiting for the dish", "tone": "warn", "detail": ""}
        return {"text": "online", "tone": "ok", "detail": ""}

    def explain(state, now):
        d = state["dish"]
        return {"headline": headline(d["status"], state["localdish"]["dish"]["error"]), "alerts": [], "aim": None,
                "facts": []}

    def ring(history):
        return {"n": 900, "current": history.get("current"), "count": 0}

    def outages(history, now):
        return []

    return types.SimpleNamespace(headline=headline, explain=explain, ring=ring, outages=outages)
