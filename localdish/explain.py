"""What the dish's numbers mean, in plain words (see DESIGN.md "explain.py").

Pure functions of decoded dicts: no I/O, no clock of their own. A proto3 field at its default is not on the wire,
so a missing number is 0, a missing bool false and a missing enum its first value; we read with .get(key, default).
"""
from __future__ import annotations

import math
import time

# GPS time starts 1980-01-06 and has no leap seconds; 18 have been added to UTC since then.
GPS_EPOCH_UNIX = 315964800
GPS_LEAP_SECONDS = 18

# aim is good enough within these (degrees)
AIM_TURN_OK = 5.0
AIM_TILT_OK = 3.0

OUTAGE_CAUSES = {
    "UNKNOWN": "unknown",
    "BOOTING": "booting",
    "STOWED": "stowed",
    "THERMAL_SHUTDOWN": "too hot, shut down",
    "NO_SCHEDULE": "no schedule from the network",
    "NO_SATS": "no satellites in view",
    "OBSTRUCTED": "obstructed",
    "NO_DOWNLINK": "no signal from the satellite",
    "NO_PINGS": "no answer from the internet",
    "ACTUATOR_ACTIVITY": "motors moving",
    "CABLE_TEST": "testing the cable",
    "SLEEPING": "sleeping (power save)",
    "SKY_SEARCH": "searching the sky",
    "INHIBIT_RF": "radio held off",
}

DISABLEMENT = {
    "NO_ACTIVE_ACCOUNT": "no active Starlink account",
    "TOO_FAR_FROM_SERVICE_ADDRESS": "too far from the service address",
    "IN_OCEAN": "at sea, and the plan does not cover it",
    "BLOCKED_COUNTRY": "service is not allowed in this country",
    "DATA_OVERAGE_SANDBOX_POLICY": "the plan's data is used up",
    "CELL_IS_DISABLED": "service is off in this area",
    "ROAM_RESTRICTED": "the plan does not allow roaming here",
    "UNKNOWN_LOCATION": "the network cannot tell where the dish is",
    "ACCOUNT_DISABLED": "the account is disabled",
    "UNSUPPORTED_VERSION": "the firmware is too old for the network",
    "MOVING_TOO_FAST_FOR_POLICY": "moving too fast for the plan",
    "UNDER_AVIATION_FLYOVER_LIMITS": "under an aviation flyover limit",
    "BLOCKED_AREA": "service is blocked in this area",
}

# DishAlerts: key → (text, tone)
ALERTS = {
    "motors_stuck": ("motors stuck", "bad"),
    "thermal_throttle": ("too warm: slowed down to cool off", "warn"),
    "thermal_shutdown": ("too hot: shut down", "bad"),
    "mast_not_near_vertical": ("the mast is not vertical", "warn"),
    "unexpected_location": ("the dish is not where the service expects it", "warn"),
    "slow_ethernet_speeds": ("ethernet is slower than 1 Gb/s", "warn"),
    "slow_ethernet_speeds_100": ("ethernet is slower than 100 Mb/s", "warn"),
    "roaming": ("roaming", "ok"),
    "install_pending": ("an update is waiting to install", "ok"),
    "is_heating": ("heating to melt snow", "ok"),
    "power_supply_thermal_throttle": ("power supply too warm: slowed down", "warn"),
    "is_power_save_idle": ("sleeping (power save)", "ok"),
    "dbf_telem_stale": ("internal telemetry is stale", "warn"),
    "low_motor_current": ("low motor current", "warn"),
    "lower_signal_than_predicted": ("signal is weaker than expected", "warn"),
    "obstruction_map_reset": ("the obstruction map was reset", "ok"),
    "dish_water_detected": ("water in the dish", "bad"),
    "router_water_detected": ("water in the router", "bad"),
    "upsu_router_port_slow": ("the power supply's router port is slow", "warn"),
    "no_ethernet_link": ("no ethernet link", "warn"),
}

_TONE_ORDER = {"bad": 0, "warn": 1, "ok": 2}

UPDATE_STATES = {
    "SOFTWARE_UPDATE_STATE_UNKNOWN": "unknown",
    "IDLE": "none waiting",
    "FETCHING": "downloading",
    "PRE_CHECK": "checking",
    "WRITING": "installing",
    "POST_CHECK": "checking",
    "REBOOT_REQUIRED": "installed, needs a restart",
    "DISABLED": "updates off",
    "FAULTED": "update failed",
}

SNOW_MELT = {"AUTO": "automatic", "ALWAYS_ON": "always on", "ALWAYS_OFF": "off"}

BANDWIDTH = {
    "UNKNOWN": "unknown",
    "NO_LIMIT": "no limit",
    "POLICY_LIMIT": "limited by plan",
    "USER_CUSTOM_LIMIT": "limited by you",
    "OVERAGE_LIMIT": "limited: data used up",
    "LOW_SPEED_POLICY_LIMIT": "limited to low speed",
}


def _words(key) -> str:
    """An enum or field name as words: "dish_water_detected" → "dish water detected"."""
    return str(key).replace("_", " ").lower().strip()


def _num(value) -> float | None:
    """A finite number, or None (the dish reports NaN for "no reading")."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _body(section: dict, key: str, inner: str) -> dict:
    """section[key], unwrapped from its response wrapper if the poller kept one ({"device_info": {...}})."""
    value = _dict(section.get(key))
    return value[inner] if isinstance(value.get(inner), dict) else value


def _clock_minutes(minutes: int) -> str:
    minutes = int(minutes) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _utc_minutes_local(minutes: int, now_unix: float | None = None) -> str:
    """A power-save schedule minute (minutes after midnight **UTC** — checked against the Starlink app, which showed
    615 as 3:15 AM in UTC−7) as hh:mm on this computer's clock, today."""
    now_unix = time.time() if now_unix is None else now_unix
    midnight_utc = int(now_unix // 86400) * 86400
    return time.strftime("%H:%M", time.localtime(midnight_utc + (int(minutes) % 1440) * 60))


def _clock_unix(t: float) -> str:
    """hh:mm on this computer's clock (the dish's own utc_offset_s ignores daylight saving)."""
    return time.strftime("%H:%M", time.localtime(t))


def _duration(seconds) -> str:
    s = _num(seconds)
    if s is None:
        return "—"
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def _pct(fraction, digits: int = 1) -> str:
    return f"{fraction * 100:.{digits}f} %"


def _ms(value) -> str:
    return f"{value:.1f} ms" if value < 10 else f"{value:.0f} ms"


# ---- history ---------------------------------------------------------------------------------------------------

def ring(history: dict) -> dict:
    """The 1 Hz history arrays unrolled oldest→newest, valid samples only; NaN becomes None."""
    history = _dict(history)
    keys = {"latency_ms": "pop_ping_latency_ms", "drop": "pop_ping_drop_rate", "down_bps": "downlink_throughput_bps",
            "up_bps": "uplink_throughput_bps", "power_w": "power_in"}
    arrays = {out: history.get(key) if isinstance(history.get(key), list) else [] for out, key in keys.items()}
    n = max((len(a) for a in arrays.values()), default=0)
    current = int(history.get("current", 0) or 0)
    count = min(max(current, 0), n)
    # the newest sample is at (current - 1) % n; before the ring fills, the oldest is at 0
    order = [(current - count + i) % n for i in range(count)]
    out = {"n": n, "current": current, "count": count}
    for name, arr in arrays.items():
        out[name] = [_num(arr[i]) if i < len(arr) else None for i in order]
    return out


def gps_ns_to_unix(ns: int) -> float:
    return ns / 1e9 + GPS_EPOCH_UNIX - GPS_LEAP_SECONDS


def outages(history: dict, now_unix: float) -> list[dict]:
    """The dish's outage list, newest first, with GPS times turned into Unix seconds."""
    out = []
    for o in _dict(history).get("outages") or []:
        if not isinstance(o, dict):
            continue
        start = gps_ns_to_unix(int(o.get("start_timestamp_ns", 0)))
        cause = o.get("cause", "UNKNOWN")
        out.append({"cause": cause, "cause_text": OUTAGE_CAUSES.get(cause, _words(cause)),
                    "start_unix": start, "ago_s": now_unix - start,
                    "duration_s": int(o.get("duration_ns", 0)) / 1e9, "did_switch": bool(o.get("did_switch", False))})
    out.sort(key=lambda o: o["start_unix"], reverse=True)
    return out


# ---- status ----------------------------------------------------------------------------------------------------

def _update(status: dict) -> dict:
    stats = _dict(status.get("software_update_stats"))
    state = stats.get("software_update_state") or status.get("software_update_state") or "SOFTWARE_UPDATE_STATE_UNKNOWN"
    at = _num(stats.get("reboot_scheduled_utc_time"))
    return {"state": state, "pending": state == "REBOOT_REQUIRED" or bool(stats.get("update_requires_reboot")),
            "at": at if at and at > 0 else None, "progress": _num(stats.get("software_update_progress"))}


def _update_note(status: dict) -> str | None:
    u = _update(status)
    if not u["pending"]:
        return None
    if u["at"]:
        return f"update pending, restarts ~{_clock_unix(u['at'])}"
    return "update pending, restarts when it can"


_SEARCHING = {"NO_SCHEDULE", "NO_SATS", "SKY_SEARCH"}


def headline(status: dict | None, error: str | None) -> dict:
    """One line for the top of the page: {"text", "tone": "ok"|"warn"|"bad", "detail"}."""
    if error:
        return {"text": "dish unreachable", "tone": "bad", "detail": str(error)}
    if not isinstance(status, dict):
        return {"text": "connecting", "tone": "warn", "detail": "no answer from the dish yet"}
    note = _update_note(status)

    def said(text, tone, detail):
        parts = [p for p in (detail, note) if p]
        return {"text": text, "tone": tone, "detail": " · ".join(parts)}

    code = status.get("disablement_code", "UNKNOWN_STATE")
    if code not in ("OKAY", "UNKNOWN_STATE"):
        return said("disabled", "bad", DISABLEMENT.get(code, _words(code)))

    # get_status carries "outage" only while the dish is down
    if "outage" in status:
        cause = _dict(status.get("outage")).get("cause", "UNKNOWN")
        if cause == "BOOTING":
            return said("booting", "warn", "the dish is starting up")
        if cause in _SEARCHING:
            return said("searching", "warn", OUTAGE_CAUSES[cause])
        if cause == "OBSTRUCTED":
            return said("obstructed now", "bad", "something is blocking the dish's view of the sky")
        if cause == "SLEEPING":
            return said("sleeping", "warn", "power save: no internet until the schedule ends")
        if cause == "STOWED":
            return said("stowed", "warn", "the dish is stowed: no internet until it is unstowed")
        if cause == "THERMAL_SHUTDOWN":
            return said("too hot", "bad", "the dish shut down to cool off")
        return said("offline", "bad", OUTAGE_CAUSES.get(cause, _words(cause)))

    drop = _num(status.get("pop_ping_drop_rate", 0)) or 0.0
    if drop >= 1:
        return said("no internet", "bad", "the dish is up, but no pings reach the internet")

    alerts_on = _dict(status.get("alerts"))
    notes = []
    if 0 < drop < 1:
        notes.append(f"losing {_pct(drop, 0)} of pings")
    if status.get("is_snr_persistently_low"):
        notes.append("signal has been weak for a while")
    for key in ("thermal_throttle", "power_supply_thermal_throttle"):
        if alerts_on.get(key) is True:
            notes.append(ALERTS[key][0])
    if notes:
        return said("degraded", "warn", "; ".join(notes))

    latency = _num(status.get("pop_ping_latency_ms"))
    return said("online", "ok", f"{_ms(latency)} to starlink" if latency else None)


def alerts(status: dict) -> list[dict]:
    """Every alert that is on, worst first; ones we don't know are worded from their key."""
    out = []
    for key, on in _dict(_dict(status).get("alerts")).items():
        if on is not True:
            continue
        text, tone = ALERTS.get(key, (_words(key), "warn"))
        out.append({"name": key, "text": text, "tone": tone})
    out.sort(key=lambda a: (_TONE_ORDER[a["tone"]], a["name"]))
    return out


def _turn(now: float, want: float) -> float:
    """Shortest signed want − now in (−180, 180]."""
    d = (want - now) % 360.0
    return d - 360.0 if d > 180.0 else d


def aim(status: dict) -> dict | None:
    """Which way to move a dish without motors; None for dishes with motors or without alignment numbers."""
    status = _dict(status)
    stats = _dict(status.get("alignment_stats"))
    if stats.get("has_actuators", status.get("has_actuators")) == "HAS_ACTUATORS_YES":
        return None
    az_now = _num(stats.get("boresight_azimuth_deg", status.get("boresight_azimuth_deg")))
    el_now = _num(stats.get("boresight_elevation_deg", status.get("boresight_elevation_deg")))
    az_want = _num(stats.get("desired_boresight_azimuth_deg"))
    el_want = _num(stats.get("desired_boresight_elevation_deg"))
    if None in (az_now, el_now, az_want, el_want):
        return None
    confidence = stats.get("attitude_estimation_state", "FILTER_RESET")
    out = {"az_now": az_now % 360.0, "az_want": az_want % 360.0, "el_now": el_now, "el_want": el_want,
           "turn_deg": None, "tilt_deg": None, "ok": None, "text": [],
           "confidence": confidence, "uncertainty_deg": _num(stats.get("attitude_uncertainty_deg"))}
    if confidence != "FILTER_CONVERGED":
        out["text"] = ["the dish is still working out which way it faces, so no directions yet"]
        return out
    turn = _turn(az_now, az_want)
    tilt = el_want - el_now
    ok = abs(turn) <= AIM_TURN_OK and abs(tilt) <= AIM_TILT_OK
    out.update(turn_deg=turn, tilt_deg=tilt, ok=ok)
    if ok:
        out["text"] = [f"aimed well: {abs(turn):.0f}° off in direction and {abs(tilt):.0f}° in tilt"]
        return out
    text = []
    if abs(turn) > AIM_TURN_OK:
        side = "right" if turn > 0 else "left"
        text.append(f"turn it {abs(turn):.0f}° to the {side}, standing behind it")
    else:
        text.append(f"direction is fine ({abs(turn):.0f}° off)")
    if abs(tilt) > AIM_TILT_OK:
        if tilt > 0:
            text.append(f"tilt it {abs(tilt):.0f}° up, toward the sky")
        else:
            text.append(f"tilt it {abs(tilt):.0f}° down, toward the horizon")
    else:
        text.append(f"tilt is fine ({abs(tilt):.0f}° off)")
    out["text"] = text
    return out


# ---- facts -----------------------------------------------------------------------------------------------------

def facts(state: dict) -> list[list]:
    """[[label, value_text], …] for the facts table; a row is left out when the device didn't say."""
    state = _dict(state)
    dish = _dict(state.get("dish"))
    router = _dict(state.get("router"))
    status = _dict(dish.get("status"))
    info = _body(dish, "device_info", "device_info") or _dict(status.get("device_info"))
    config = _body(dish, "config", "dish_config") or _dict(status.get("config"))
    rstatus = _dict(router.get("status"))
    rinfo = _body(router, "device_info", "device_info") or _dict(rstatus.get("device_info"))
    rows = []

    def add(label, value):
        if value is not None and value != "":
            rows.append([label, str(value)])

    add("dish hardware", info.get("hardware_version"))
    add("dish firmware", info.get("software_version"))
    if rinfo:
        hw = rinfo.get("hardware_version")
        index = rinfo.get("hardware_index")
        add("router hardware", f"{hw} ({_words(index)})" if hw and index else hw or (index and _words(index)))
        add("router firmware", rinfo.get("software_version"))
    add("country", info.get("country_code") or rinfo.get("country_code"))
    if status:
        add("service", _words(status.get("class_of_service", "UNKNOWN_USER_CLASS_OF_SERVICE"))
            .replace("unknown user class of service", "unknown"))
        add("mobility", _words(status.get("mobility_class", "STATIONARY")))
        add("dish uptime", _duration(_dict(status.get("device_state")).get("uptime_s", 0)))
        eth = status.get("eth_speed_mbps")
        add("ethernet", f"{eth} Mb/s" if eth else "no link")
        gps = _dict(status.get("gps_stats"))
        if gps:
            sats = int(gps.get("gps_sats", 0))
            add("gps", f"{'fixed' if gps.get('gps_valid') else 'no fix'}, {sats} satellites")
        u = _update(status)
        text = UPDATE_STATES.get(u["state"], _words(u["state"]))
        if u["state"] in ("FETCHING", "WRITING") and u["progress"] is not None:
            text += f" {_pct(u['progress'], 0)}"
        add("software update", text)
        if u["pending"] and u["at"]:
            add("update restart", f"~{_clock_unix(u['at'])}")
    if config:
        add("update hour", _clock_minutes(int(config.get("swupdate_reboot_hour", 0)) * 60))
        mode = config.get("snow_melt_mode", "AUTO")
        add("snow melt", SNOW_MELT.get(mode, _words(mode)))
        if config.get("power_save_mode"):
            start = int(config.get("power_save_start_minutes", 0))
            length = int(config.get("power_save_duration_minutes", 0))
            add("power save", f"on, {_utc_minutes_local(start)}–{_utc_minutes_local(start + length)}")
        else:
            add("power save", "off")
        add("location sharing", "on (local network)" if config.get("location_request_mode") == "LOCAL" else "off")
    if status:
        obs = _dict(status.get("obstruction_stats"))
        if obs:
            add("obstructed sky", _pct(_num(obs.get("fraction_obstructed", 0)) or 0.0))
            add("time obstructed", _pct(_num(obs.get("time_obstructed", 0)) or 0.0))
            length = _num(obs.get("avg_prolonged_obstruction_duration_s"))
            every = _num(obs.get("avg_prolonged_obstruction_interval_s"))
            if length and every:
                add("long obstructions", f"{length:.1f} s long, every {_duration(every)}")
        add("signal", "above the noise floor" if status.get("is_snr_above_noise_floor") else "below the noise floor")
        down = status.get("dl_bandwidth_restricted_reason", "UNKNOWN")
        up = status.get("ul_bandwidth_restricted_reason", "UNKNOWN")
        if down == up:
            add("bandwidth", BANDWIDTH.get(down, _words(down)))
        else:
            add("bandwidth", f"download {BANDWIDTH.get(down, _words(down))}, upload {BANDWIDTH.get(up, _words(up))}")
    if rstatus:
        add("router uptime", _duration(_dict(rstatus.get("device_state")).get("uptime_s", 0)))
        to_dish = _num(rstatus.get("dish_ping_latency_ms"))
        if to_dish is not None:
            add("router → dish", _ms(to_dish))
        to_pop = _num(rstatus.get("pop_ping_latency_ms"))
        if to_pop is not None:
            loss = _num(rstatus.get("pop_ping_drop_rate_5m", 0)) or 0.0
            add("router → Starlink", f"{_ms(to_pop)}, {_pct(loss)} lost over 5 min")
        to_net = _num(rstatus.get("ping_latency_ms"))
        if to_net is not None:
            loss = _num(rstatus.get("ping_drop_rate_5m", 0)) or 0.0
            add("router → internet", f"{_ms(to_net)}, {_pct(loss)} lost over 5 min")
    return rows


def explain(state: dict, now_unix: float) -> dict:
    """The "explain" block of /api/state."""
    state = _dict(state)
    status = _dict(state.get("dish")).get("status")
    link = _dict(_dict(state.get("localdish")).get("dish"))
    error = link.get("error") if link.get("reachable") is False else None
    if link.get("reachable") is False and not error:
        error = "no answer"
    status = status if isinstance(status, dict) else None
    held = _dict(state.get("dish")).get("aim_status")
    pointed = aim(status) if status else None
    if pointed is None and isinstance(held, dict) and status is not None:
        # the dish is searching or hasn't said where it wants to point: show the last reading, marked with its age
        pointed = aim(held)
        if pointed is not None:
            age = _num(_dict(state.get("dish")).get("aim_age_s"))
            pointed["held_s"] = age
            pointed["text"] = pointed["text"] + [f"as of {_duration(age)} ago — the dish isn't saying right now"]
    return {"headline": headline(status, error), "alerts": alerts(status or {}),
            "aim": pointed, "facts": facts(state)}
