"""Asks the dish and router for their state on a fixed cadence and keeps what they said (DESIGN.md "poller.py").

The page only ever reads these caches. The only device calls that do not come from the cadence table are the
controls a person presses and the refreshes they ask for, and both go through the same single-flight path.
"""
from __future__ import annotations

import collections
import math
import socket
import sys
import threading
import time
import traceback
from typing import NamedTuple, Optional

from . import __version__
from .grpcweb import GrpcError


class Job(NamedTuple):
    name: str
    target: str                 # "dish" | "router"
    ops: tuple                  # called one after another, each on its own
    watched_s: Optional[float]  # seconds between runs while a page is watching; None: not while watching
    idle_s: Optional[float]     # seconds between runs while nobody is; None: not while nobody is
    timeout: Optional[float]    # per call; None: the client's default (5 s)


# The cadence. An interval counts from the previous attempt, whether it worked or not: nothing is retried early.
CADENCE = (
    Job("status", "dish", ("get_status",), 1, 30, None),
    Job("history", "dish", ("get_history",), 5, None, 20),
    Job("obstruction", "dish", ("dish_get_obstruction_map",), 60, None, 20),
    Job("info", "dish", ("get_device_info", "dish_get_config"), 300, 300, None),
    Job("diagnostics", "dish", ("get_diagnostics",), 60, None, None),
    Job("location", "dish", ("get_location",), 300, None, None),   # held after PERMISSION_DENIED until a refresh
    Job("router", "router", ("get_status", "wifi_get_clients"), 30, None, None),
    Job("router_info", "router", ("get_device_info",), 300, 300, None),
)

# The job that says whether a device is there. While a device is unreachable only this one runs.
PRIMARY = {"dish": "status", "router": "router"}

# Where each answer is kept: (target, op) → slot.
SLOTS = {
    ("dish", "get_status"): "status",
    ("dish", "get_history"): "history",
    ("dish", "dish_get_obstruction_map"): "obstruction",
    ("dish", "get_device_info"): "device_info",
    ("dish", "dish_get_config"): "config",
    ("dish", "get_diagnostics"): "diagnostics",
    ("dish", "get_location"): "location",
    ("router", "get_status"): "status",
    ("router", "wifi_get_clients"): "clients",
    ("router", "get_device_info"): "device_info",
    ("router", "get_ping"): "ping",
}

# POST /api/refresh/<what> → the jobs it runs now.
REFRESH = {
    "obstruction": ("obstruction",),
    "info": ("info", "router_info"),
    "router": ("router",),
    "location": ("location",),
}

WATCH_WINDOW_S = 10.0       # a GET /api/state this recent means a page is watching
REFRESH_MIN_S = 10.0        # refreshes of one kind at least this far apart (counting the cadence's own runs)
EVENTS_MAX = 200
EVENTS_SHOWN = 50
SPEEDTEST_POLL_S = 1.0
SPEEDTEST_MAX_S = 90.0
SPEEDTEST_START_S = 5.0     # a test not seen running this long after the start is taken as over

_ENVELOPE = ("api_version", "status", "id")


def body(resp: dict) -> object:
    """A Response's one-of answer: {"dish_get_status": {...}, "api_version": 42} → {...}."""
    rest = {k: v for k, v in resp.items() if k not in _ENVELOPE}
    if len(rest) == 1:
        return next(iter(rest.values()))
    return rest


def check(resp: dict) -> dict:
    """A Response can carry a failure in its own status field while the grpc-status said OK."""
    st = resp.get("status")
    if isinstance(st, dict) and st.get("code"):
        raise GrpcError(int(st["code"]), str(st.get("message", "")))
    return resp


def describe(exc: BaseException) -> str:
    if isinstance(exc, GrpcError):
        return str(exc)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    text = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    return text[:1].lower() + text[1:]


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class _Slot:
    __slots__ = ("value", "ok_t", "error", "code")

    def __init__(self):
        self.value = None
        self.ok_t = None     # when it last answered
        self.error = None    # the last attempt's error, None when it worked
        self.code = None     # grpc status name of that error


class _Flight:
    __slots__ = ("done", "result", "exc")

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.exc = None


class Poller:
    """devices: {"dish": dev, "router": dev | None}, where a dev has call(op, fields=None, *, timeout=None) -> dict
    and raises grpcweb.GrpcError or OSError. device and explain are the modules (or anything shaped like them)."""

    def __init__(self, devices: dict, *, device, explain, hosts: dict | None = None, clock=time.time,
                 demo: bool = False, log=None):
        self.devices = {"dish": devices.get("dish"), "router": devices.get("router")}
        self.targets = [t for t, d in self.devices.items() if d is not None]
        self.hosts = dict(hosts or {})
        self.device = device
        self.explain = explain
        self.clock = clock
        self.demo = demo
        self._log = log or _stderr
        self.jobs = [j for j in CADENCE if self.devices.get(j.target) is not None]

        self._lock = threading.RLock()                                  # guards all the state below
        self._call_locks = {t: threading.Lock() for t in self.devices}  # one call in flight per device
        self._slots = {t: collections.defaultdict(_Slot) for t in self.devices}
        self._last_run = {}          # job name → when it was last attempted
        self._forced = set()         # job names to run at the next chance, whatever the cadence says
        self._inflight = {}          # job name → _Flight
        self._reachable = {t: None for t in self.devices}
        self._schema = {}
        self._headline = None
        self._calls = {}             # "dish:get_status" → [total, deque of call times]
        self._events = collections.deque(maxlen=EVENTS_MAX)
        self._watched_at = None
        self._refreshed = {}
        self._unavailable = {}       # control name → why, for the rest of the process
        self._speedtest = None       # the public run state
        self._st_polled = None
        self._st_seen_running = False
        self._stop = threading.Event()
        self._wake = {t: threading.Event() for t in self.devices}
        self._threads = []

    # ---- events and watching

    def event(self, kind: str, text: str) -> None:
        t = self.clock()
        with self._lock:
            self._events.append({"t": t, "kind": kind, "text": text})
        self._log(f"{time.strftime('%H:%M:%S', time.localtime(t))} {kind}: {text}")

    def watching(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        return self._watched_at is not None and now - self._watched_at < WATCH_WINDOW_S

    def touch(self) -> None:
        """A page asked for /api/state."""
        now = self.clock()
        with self._lock:
            was = self.watching(now)
            self._watched_at = now
        if not was:
            self._wake_all()

    def _wake_all(self) -> None:
        for e in self._wake.values():
            e.set()

    # ---- the cadence

    def job(self, name: str) -> Job:
        for j in self.jobs:
            if j.name == name:
                return j
        raise KeyError(name)

    def _interval(self, job: Job, watching: bool) -> Optional[float]:
        if job.name == "location" and self._slots["dish"]["location"].code == "PERMISSION_DENIED":
            return None
        if job.name != PRIMARY[job.target] and self._reachable[job.target] is False:
            return None
        return job.watched_s if watching else job.idle_s

    def due(self, job: Job, now: float) -> bool:
        with self._lock:
            if job.name in self._forced:
                return True
            interval = self._interval(job, self.watching(now))
            if interval is None:
                return False
            last = self._last_run.get(job.name)
            return last is None or now - last >= interval

    def next_due(self, target: str) -> float:
        """When something for this device next falls due (at most 1 s ahead; wakes come sooner)."""
        now = self.clock()
        soonest = now + 1.0
        with self._lock:
            watching = self.watching(now)
            if any(n in self._forced for n in (j.name for j in self.jobs if j.target == target)):
                return now
            for j in self.jobs:
                if j.target != target:
                    continue
                interval = self._interval(j, watching)
                if interval is None:
                    continue
                last = self._last_run.get(j.name)
                soonest = min(soonest, now if last is None else last + interval)
        return soonest

    def run_due(self, target: str) -> list:
        """Runs, in table order, every job for this device that is due. Returns their names."""
        ran = []
        for j in self.jobs:
            if j.target == target and self.due(j, self.clock()):
                self.run_job(j.name)
                ran.append(j.name)
        if target == "router":
            self.poll_speedtest()
        return ran

    def run_job(self, name: str) -> None:
        """Single-flight: a caller that finds this job already running waits for that run instead of calling again."""
        job = self.job(name)
        with self._lock:
            flight = self._inflight.get(name)
            leader = flight is None
            if leader:
                flight = self._inflight[name] = _Flight()
        if not leader:
            flight.done.wait()
            return
        try:
            self._run(job)
        finally:
            with self._lock:
                del self._inflight[name]
            flight.done.set()

    def _run(self, job: Job) -> None:
        with self._lock:
            self._forced.discard(job.name)
            self._last_run[job.name] = self.clock()
        for op in job.ops:
            try:
                resp = self._call(job.target, op, None, job.timeout)
            except GrpcError as e:
                self._store_error(job.target, op, e)
                continue
            except OSError as e:
                self._store_error(job.target, op, e)
                break           # it isn't there: the rest of the batch would only wait for their timeouts too
            self._store(job.target, op, resp)
        if job.name == "status":
            self._check_headline()

    def _call(self, target: str, op: str, fields, timeout) -> dict:
        dev = self.devices[target]
        with self._call_locks[target]:
            self._count(target, op)
            try:
                resp = check(dev.call(op, fields, timeout=timeout))
            except GrpcError:
                self._seen(target, True, None)
                raise
            except OSError as e:
                self._seen(target, False, e)
                raise
            self._seen(target, True, None)
            return resp

    def _count(self, target: str, op: str) -> None:
        now = self.clock()
        with self._lock:
            entry = self._calls.setdefault(f"{target}:{op}", [0, collections.deque()])
            entry[0] += 1
            entry[1].append(now)
            while entry[1] and now - entry[1][0] > 60:
                entry[1].popleft()

    def _seen(self, target: str, reachable: bool, exc) -> None:
        with self._lock:
            was = self._reachable[target]
            self._reachable[target] = reachable
        host = self.hosts.get(target) or target
        if reachable and was is not True:
            self.event(target, f"{target} reachable at {host}")
        elif not reachable and was is not False:
            self.event(target, f"{target} unreachable at {host}: {describe(exc)}")
        schema = getattr(self.devices[target], "schema", None)
        if schema is not None and schema is not self._schema.get(target):
            first = target not in self._schema
            self._schema[target] = schema
            self.event(target, f"{target} schema loaded" if first else f"{target} schema reloaded")

    def _store(self, target: str, op: str, resp: dict) -> None:
        value = body(resp)
        if op == "wifi_get_clients":
            value = value.get("clients", []) if isinstance(value, dict) else []
        with self._lock:
            slot = self._slots[target][SLOTS[(target, op)]]
            slot.value, slot.ok_t, slot.error, slot.code = value, self.clock(), None, None

    def _store_error(self, target: str, op: str, exc: BaseException) -> None:
        with self._lock:
            slot = self._slots[target][SLOTS[(target, op)]]
            slot.error = describe(exc)
            slot.code = exc.name if isinstance(exc, GrpcError) else None

    def _check_headline(self) -> None:
        with self._lock:
            slot = self._slots["dish"]["status"]
            status, error = slot.value, slot.error
        text = self.explain.headline(status, error).get("text")
        if text != self._headline:
            self._headline = text
            self.event("dish", text)

    # ---- threads

    def start(self) -> None:
        for t in self.targets:
            th = threading.Thread(target=self._loop, args=(t,), name=f"localdish-{t}", daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        self._wake_all()
        for th in self._threads:
            th.join(timeout=2)
        for t in self.targets:
            close = getattr(self.devices[t], "close", None)
            if close:
                close()

    def _loop(self, target: str) -> None:
        while not self._stop.is_set():
            try:
                self.run_due(target)
                wait = self.next_due(target) - self.clock()
            except Exception:
                self._log(traceback.format_exc().rstrip())
                wait = 1.0
            self._wake[target].wait(min(max(wait, 0.02), 1.0))
            self._wake[target].clear()

    # ---- what the page reads

    def _device_line(self, target: str, now: float) -> dict | None:
        if self.devices.get(target) is None:
            return None
        slot = self._slots[target]["status"]
        return {
            "host": self.hosts.get(target),
            "reachable": bool(self._reachable[target]),
            "error": slot.error,
            "age_s": None if slot.ok_t is None else round(now - slot.ok_t, 1),
            "schema": getattr(self.devices[target], "schema", None) is not None,
        }

    def caches(self, now: float | None = None) -> dict:
        """The state the page, explain and device.available all see: /api/state's localdish, dish and router."""
        now = self.clock() if now is None else now
        with self._lock:
            d = self._slots["dish"]
            state = {
                "localdish": {"version": __version__, "now": now, "demo": self.demo,
                              "dish": self._device_line("dish", now), "router": self._device_line("router", now)},
                "dish": {"status": d["status"].value, "device_info": d["device_info"].value,
                         "config": d["config"].value, "diagnostics": d["diagnostics"].value,
                         "location": d["location"].value, "location_error": d["location"].error},
                "router": None,
            }
            if self.devices.get("router") is not None:
                r = self._slots["router"]
                state["router"] = {"status": r["status"].value, "clients": r["clients"].value,
                                   "device_info": r["device_info"].value, "ping": r["ping"].value}
        return state

    def state(self) -> dict:
        """GET /api/state."""
        now = self.clock()
        state = self.caches(now)
        try:
            explained = self.explain.explain(state, now)
        except Exception as e:
            self._log(traceback.format_exc().rstrip())
            explained = {"headline": {"text": "localdish could not explain this", "tone": "bad", "detail": repr(e)},
                         "alerts": [], "aim": None, "facts": []}
        out = dict(state)
        out["explain"] = explained
        out["controls"] = self.controls(state)
        with self._lock:
            out["running"] = {"speedtest": dict(self._speedtest) if self._speedtest else None}
            out["events"] = list(self._events)[-EVENTS_SHOWN:]
        return out

    def history(self) -> dict:
        """GET /api/history."""
        now = self.clock()
        with self._lock:
            slot = self._slots["dish"]["history"]
            value, ok_t, error = slot.value, slot.ok_t, slot.error
        return {
            "age_s": None if ok_t is None else round(now - ok_t, 1),
            "error": error,
            "ring": self.explain.ring(value) if value else None,
            "outages": self.explain.outages(value, now) if value else [],
            "event_log": (value or {}).get("event_log", {}),
        }

    def obstruction(self) -> dict:
        """GET /api/obstruction."""
        now = self.clock()
        with self._lock:
            slot = self._slots["dish"]["obstruction"]
            return {"age_s": None if slot.ok_t is None else round(now - slot.ok_t, 1), "error": slot.error,
                    "map": slot.value}

    def stats(self) -> dict:
        """GET /api/stats."""
        now = self.clock()
        with self._lock:
            calls = {k: {"total": v[0], "last_60s": sum(1 for t in v[1] if now - t <= 60)}
                     for k, v in sorted(self._calls.items())}
            return {"calls": calls, "watching": self.watching(now)}

    def events(self) -> list:
        with self._lock:
            return list(self._events)

    # ---- refresh

    def refresh(self, what: str) -> tuple:
        """POST /api/refresh/<what> → (http status, body). Runs the jobs at the next chance; does not wait."""
        if what not in REFRESH:
            return 404, {"ok": False, "error": f"nothing to refresh called {what}"}
        names = [n for n in REFRESH[what] if any(j.name == n for j in self.jobs)]
        if not names:
            return 409, {"ok": False, "error": "no router"}
        now = self.clock()
        with self._lock:
            last = max([self._refreshed.get(what, -math.inf)] + [self._last_run.get(n, -math.inf) for n in names])
            if now - last < REFRESH_MIN_S:
                return 429, {"ok": False, "retry_after_s": math.ceil(REFRESH_MIN_S - (now - last))}
            self._refreshed[what] = now
            self._forced.update(names)
        for n in names:
            self._wake[self.job(n).target].set()
        return 200, {"ok": True}

    # ---- controls

    def _find_control(self, name: str):
        for c in self.device.CONTROLS:
            if c.name == name:
                return c
        return None

    def _available(self, ctl, state: dict) -> tuple:
        if self.devices.get(ctl.target) is None:
            return False, f"no {ctl.target} found"
        with self._lock:
            if ctl.name in self._unavailable:
                return False, self._unavailable[ctl.name]
            if ctl.name == "speedtest" and self._speedtest and not self._speedtest["done"]:
                return False, "a speed test is running"
        return self.device.available(ctl.name, state)

    def controls(self, state: dict) -> list:
        out = []
        for c in self.device.CONTROLS:
            ok, reason = self._available(c, state)
            try:
                current = self.device.current(c.name, state)
            except Exception:
                current = {}
            out.append({"name": c.name, "label": c.label, "group": c.group, "confirm": c.confirm,
                        "params": c.params, "available": ok, "reason": reason, "current": current})
        return out

    def control(self, name: str, params: dict) -> tuple:
        """POST /api/control/<name> → (http status, body)."""
        ctl = self._find_control(name)
        if ctl is None:
            return 404, {"ok": False, "error": f"no control called {name}"}
        ok, reason = self._available(ctl, self.caches())
        if not ok:
            return 409, {"ok": False, "error": reason or "not available"}
        try:
            target, op, fields = self.device.build(name, params)
        except ValueError as e:
            return 400, {"ok": False, "error": str(e)}
        if name == "speedtest":
            with self._lock:           # claim the run before the call, so a second press gets a 409
                if self._speedtest and not self._speedtest["done"]:
                    return 409, {"ok": False, "error": "a speed test is running"}
                self._speedtest = {"started": self.clock(), "status": None, "done": False, "error": None}
                self._st_polled, self._st_seen_running = None, False
        try:
            resp = self._call(target, op, fields, None)
        except (GrpcError, OSError) as e:
            if name == "speedtest":
                with self._lock:
                    self._speedtest = None
            if isinstance(e, GrpcError) and e.name == "UNIMPLEMENTED":
                with self._lock:
                    self._unavailable[name] = f"this {target}'s firmware does not offer it"
            self.event("control", f"{ctl.label}: failed, {describe(e)}")
            if isinstance(e, GrpcError):
                return 502, {"ok": False, "error": e.message or str(e)}
            return 502, {"ok": False, "error": f"{target} {describe(e)}"}
        result = body(resp)
        if name == "ping":
            self._store(target, op, resp)
        if target == "dish":
            with self._lock:
                self._forced.add("info")
                if name == "share_location":
                    self._forced.add("location")
        self._wake[target].set()
        text = "speed test started" if name == "speedtest" else "done"
        self.event("control", f"{ctl.label}: {text}")
        return 200, {"ok": True, "result": result, "text": text}

    def poll_speedtest(self) -> None:
        """While a test runs: its status every 1 s, for at most 90 s."""
        now = self.clock()
        with self._lock:
            run = self._speedtest
            if not run or run["done"] or self.devices.get("router") is None:
                return
            if self._st_polled is not None and now - self._st_polled < SPEEDTEST_POLL_S:
                return
            self._st_polled = now
        elapsed = now - run["started"]
        try:
            resp = self._call("router", "get_speedtest_status", None, None)
        except GrpcError as e:
            self._finish_speedtest(describe(e))
            return
        except OSError as e:
            if elapsed >= SPEEDTEST_MAX_S:
                self._finish_speedtest(describe(e))
            return
        value = body(resp)
        status = value.get("status", value) if isinstance(value, dict) else {}
        running = bool(status.get("running"))
        with self._lock:
            run["status"] = status
            self._st_seen_running = self._st_seen_running or running
            seen = self._st_seen_running
        if running and elapsed < SPEEDTEST_MAX_S:
            return
        if not running and not seen and elapsed < SPEEDTEST_START_S:
            return
        self._finish_speedtest(None if not running else "stopped watching after 90 s")

    def _finish_speedtest(self, error: str | None) -> None:
        with self._lock:
            if not self._speedtest:
                return
            self._speedtest["done"] = True
            self._speedtest["error"] = error
        self.event("control", "speed test finished" if error is None else f"speed test: {error}")
