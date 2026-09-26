"""--demo: a recorded Starlink Mini that answers like the real one, with a little life in it (DESIGN.md "cli.py and
demo.py"). Controls succeed without doing anything."""
from __future__ import annotations

import copy
import json
import random
import threading
import time

from .grpcweb import GrpcError

try:
    from importlib.resources import files as _files
except ImportError:  # pragma: no cover
    _files = None

# Operations the controls use (DESIGN.md's control table). In demo they answer, and change nothing but the demo.
CONTROL_OPS = frozenset({"reboot", "dish_set_config", "dish_clear_obstruction_map", "dish_stow",
                         "start_speedtest", "get_speedtest_status", "get_ping"})

SPEEDTEST_S = 15.0      # how long a demo speed test runs
DRIFT = 0.08            # live values wander by up to ±8 %

# the ring's series that drift; the rest (drop rate) repeat what was recorded
_RING_DRIFT = ("pop_ping_latency_ms", "downlink_throughput_bps", "uplink_throughput_bps", "power_in")
_STATUS_DRIFT = ("pop_ping_latency_ms", "downlink_throughput_bps", "uplink_throughput_bps")


def load() -> dict:
    """The recorded Mini: {"dish": {op: Response}, "router": {op: Response}, "errors": {device: {op: {code, message}}}}."""
    return json.loads((_files("localdish") / "demo" / "mini.json").read_text(encoding="utf-8"))


class _DemoSchema:
    """Stands where a device's reflected schema would: the demo has none, and needs none."""


class FakeDevice:
    """Answers call(op) from recorded Responses: recorded errors raise GrpcError, controls succeed doing nothing,
    anything else is UNIMPLEMENTED — as a device whose firmware lacks the operation would say."""

    def __init__(self, name: str, recorded: dict, errors: dict | None = None, *, clock=time.time,
                 rng: random.Random | None = None, log=None):
        self.name = name
        self._recorded = copy.deepcopy(recorded)
        self._errors = dict(errors or {})
        self._clock = clock
        self._rng = rng or random.Random()
        self._log = log or (lambda text: None)
        self._lock = threading.Lock()
        self._t0 = clock()
        self._ring_t = self._t0            # the history ring has been advanced up to here
        self._speedtest = None             # (started, id)
        self.schema = _DemoSchema()

    def call(self, op: str, fields: dict | None = None, *, timeout: float | None = None) -> dict:
        with self._lock:
            if op in self._errors:
                err = self._errors[op]
                raise GrpcError(int(err.get("code", 2)), str(err.get("message", "")))
            if op == "start_speedtest":
                self._speedtest = (self._clock(), self._rng.randrange(1, 1 << 31))
                self._log(f"demo: {self.name} start_speedtest (nothing sent; the result is made up)")
                return {"start_speedtest": {}}
            if op == "get_speedtest_status":
                return {"get_speedtest_status": {"status": self._speedtest_status()}}
            if op in self._recorded:
                return self._answer(op)
            if op in CONTROL_OPS:
                self._log(f"demo: {self.name} {op} {json.dumps(fields or {}, sort_keys=True)} (nothing sent)")
                if op == "dish_set_config":
                    self._apply_config((fields or {}).get("dish_config", {}))
                return {op: {}}
            raise GrpcError(12, f"demo: {op} was not recorded")

    def close(self) -> None:
        pass

    # ---- the made-up parts

    def _jitter(self, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return type(v)(v * (1 + self._rng.uniform(-DRIFT, DRIFT)))
        return v

    def _answer(self, op: str) -> dict:
        resp = self._recorded[op]
        if op == "get_history":
            self._advance_ring(resp.get("dish_get_history", {}))
        out = copy.deepcopy(resp)
        if op == "get_status":
            st = out.get("dish_get_status", {})
            for k in _STATUS_DRIFT:
                if k in st:
                    st[k] = self._jitter(st[k])
            ds = st.get("device_state")
            if isinstance(ds, dict) and "uptime_s" in ds:
                ds["uptime_s"] = int(ds["uptime_s"]) + int(self._clock() - self._t0)
        return out

    def _advance_ring(self, h: dict) -> None:
        """One new sample per second since the last look, each the recorded one from 15 min before, jittered."""
        now = self._clock()
        steps = int(now - self._ring_t)
        if steps <= 0 or "current" not in h:
            return
        self._ring_t += steps
        n = max((len(h[k]) for k in _RING_DRIFT if isinstance(h.get(k), list)), default=0)
        if not n:
            return
        cur = int(h["current"])
        for i in range(cur, cur + min(steps, n)):
            for k in _RING_DRIFT:
                arr = h.get(k)
                if isinstance(arr, list) and len(arr) == n:
                    arr[i % n] = self._jitter(arr[i % n])
        h["current"] = cur + steps
        log = h.get("event_log")
        if isinstance(log, dict) and "current_timestamp_ns" in log:
            log["current_timestamp_ns"] = int(log["current_timestamp_ns"]) + steps * 1_000_000_000

    def _apply_config(self, cfg: dict) -> None:
        """So the page shows the new setting after a demo control, as it would with a dish."""
        rec = self._recorded.get("dish_get_config", {}).get("dish_get_config", {}).get("dish_config")
        if isinstance(rec, dict):
            rec.update({k: v for k, v in cfg.items() if not k.startswith("apply_")})

    def _speedtest_status(self) -> dict:
        if not self._speedtest:
            return {"running": False}
        started, test_id = self._speedtest
        elapsed = self._clock() - started
        samples = max(0, min(int(elapsed), int(SPEEDTEST_S)))
        rng = random.Random(test_id)       # the same test gives the same numbers on every look
        return {
            "running": elapsed < SPEEDTEST_S,
            "id": test_id,
            "down": {"throughputs_mbps": [round(rng.uniform(60, 140), 1) for _ in range(samples)]},
            "up": {"throughputs_mbps": [round(rng.uniform(8, 25), 1) for _ in range(samples)]},
        }


def devices(data: dict | None = None, *, clock=time.time, rng: random.Random | None = None, log=None) -> tuple:
    """(dish, router) FakeDevices over the recorded Mini."""
    data = load() if data is None else data
    errors = data.get("errors", {})
    rng = rng or random.Random()
    dish = FakeDevice("dish", data.get("dish", {}), errors.get("dish"), clock=clock, rng=rng, log=log)
    router = None
    if data.get("router"):
        router = FakeDevice("router", data["router"], errors.get("router"), clock=clock, rng=rng, log=log)
    return dish, router
