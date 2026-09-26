"""What we read from the dish and router, and the few controls we send (see DESIGN.md "device.py").

Pure Python: the only I/O is read(), which asks a device through its call(). Everything else works on the decoded
dicts the poller caches, so it can be tested offline.
"""
from __future__ import annotations

from typing import NamedTuple

# name → (target, request op, response key). The response key is the Response one-of the answer arrives in.
READS: dict[str, tuple[str, str, str]] = {
    "dish_status": ("dish", "get_status", "dish_get_status"),
    "dish_history": ("dish", "get_history", "dish_get_history"),
    "dish_obstruction_map": ("dish", "dish_get_obstruction_map", "dish_get_obstruction_map"),
    "dish_device_info": ("dish", "get_device_info", "get_device_info"),
    "dish_config": ("dish", "dish_get_config", "dish_get_config"),
    "dish_diagnostics": ("dish", "get_diagnostics", "dish_get_diagnostics"),
    "dish_location": ("dish", "get_location", "get_location"),
    "router_status": ("router", "get_status", "wifi_get_status"),
    "router_clients": ("router", "wifi_get_clients", "wifi_get_clients"),
    "router_device_info": ("router", "get_device_info", "get_device_info"),
    "router_ping": ("router", "get_ping", "get_ping"),
    "router_speedtest_status": ("router", "get_speedtest_status", "get_speedtest_status"),
}


def read(dev, name: str) -> dict:
    """Ask `dev` (the device READS[name] targets) for one read; the answer's body, or {} if it carried none."""
    _, op, key = READS[name]
    body = dev.call(op).get(key)
    return body if isinstance(body, dict) else {}


class Control(NamedTuple):
    name: str
    label: str
    group: str          # "restart" | "settings" | "maintenance" | "tests"
    target: str         # "dish" | "router"
    op: str
    confirm: str        # the question the page asks before sending
    params: list        # [{"name", "type": "bool"|"int"|"choice", "choices"?, "min"?, "max"?, "label"}]


SNOW_MELT_MODES = ["AUTO", "ALWAYS_ON", "ALWAYS_OFF"]

CONTROLS: list[Control] = [
    Control("restart", "restart dish", "restart", "dish", "reboot",
            "restart the dish? the internet drops while it boots, usually a minute or two.", []),
    Control("snow_melt", "snow melt", "settings", "dish", "dish_set_config",
            "change the dish's snow melt setting?",
            [{"name": "mode", "type": "choice", "choices": SNOW_MELT_MODES, "label": "snow melt"}]),
    Control("power_save", "power save", "settings", "dish", "dish_set_config",
            "change the power-save schedule? while the dish sleeps there is no internet.",
            [{"name": "enabled", "type": "bool", "label": "power save on"},
             {"name": "start_minutes", "type": "int", "min": 0, "max": 1439,
              "label": "starts"},
             {"name": "duration_minutes", "type": "int", "min": 1, "max": 1440, "label": "lasts, minutes"}]),
    Control("share_location", "share location", "settings", "dish", "dish_set_config",
            "change whether devices on your network may read the dish's location?",
            [{"name": "share", "type": "bool", "label": "share location on the local network"}]),
    Control("clear_obstructions", "clear obstruction map", "maintenance", "dish", "dish_clear_obstruction_map",
            "clear the obstruction map? the dish builds it again as satellites pass, which takes hours.", []),
    Control("stow", "stow", "maintenance", "dish", "dish_stow",
            "stow or unstow the dish? stowed, it lies flat and there is no internet until you unstow it.",
            [{"name": "unstow", "type": "bool", "label": "unstow (off: stow)"}]),
    Control("speedtest", "speed test", "tests", "router", "start_speedtest",
            "run a speed test through the router? it fills the connection for a short while and counts toward "
            "any data limit.", []),
    Control("ping", "ping test", "tests", "router", "get_ping",
            "ask the router to ping its test hosts?", []),
]

_BY_NAME = {c.name: c for c in CONTROLS}


def _control(name: str) -> Control:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"unknown control: {name}") from None


def _check(control: Control, params: dict | None) -> dict:
    """The params, validated strictly against the control's list: every one present, nothing extra, right type."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    known = {p["name"] for p in control.params}
    extra = sorted(set(params) - known)
    if extra:
        raise ValueError(f"{control.name} does not take: {', '.join(extra)}")
    out = {}
    for p in control.params:
        name = p["name"]
        if name not in params:
            raise ValueError(f"{control.name} needs {name}")
        value = params[name]
        if p["type"] == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif p["type"] == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be a whole number")
            if value < p["min"] or value > p["max"]:
                raise ValueError(f"{name} must be from {p['min']} to {p['max']}")
        elif p["type"] == "choice":
            if value not in p["choices"]:
                raise ValueError(f"{name} must be one of: {', '.join(p['choices'])}")
        out[name] = value
    return out


def build(name: str, params: dict | None) -> tuple[str, str, dict]:
    """(target, op, request fields) for a control; ValueError with a plain message when the params are wrong."""
    control = _control(name)
    p = _check(control, params)
    if name == "snow_melt":
        fields = {"dish_config": {"snow_melt_mode": p["mode"], "apply_snow_melt_mode": True}}
    elif name == "power_save":
        fields = {"dish_config": {
            "power_save_mode": p["enabled"], "apply_power_save_mode": True,
            "power_save_start_minutes": p["start_minutes"], "apply_power_save_start_minutes": True,
            "power_save_duration_minutes": p["duration_minutes"], "apply_power_save_duration_minutes": True,
        }}
    elif name == "share_location":
        fields = {"dish_config": {"location_request_mode": "LOCAL" if p["share"] else "NONE",
                                  "apply_location_request_mode": True}}
    elif name == "stow":
        fields = {"unstow": p["unstow"]}
    else:
        fields = {}
    return control.target, control.op, fields


def _section(state: dict, device: str) -> dict:
    section = state.get(device) if isinstance(state, dict) else None
    return section if isinstance(section, dict) else {}


def _body(section: dict, key: str, inner: str) -> dict:
    """section[key], unwrapped from its response wrapper if the poller kept one ({"dish_config": {...}})."""
    value = section.get(key)
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get(inner), dict):
        return value[inner]
    return value


def dish_config(state: dict) -> dict:
    """The dish's live config: from dish_get_config when we have it, else the copy inside get_status."""
    dish = _section(state, "dish")
    config = _body(dish, "config", "dish_config")
    if config:
        return config
    status = dish.get("status")
    return status.get("config", {}) if isinstance(status, dict) else {}


def _has_actuators(status: dict) -> str:
    value = status.get("has_actuators")
    if value is None:
        value = (status.get("alignment_stats") or {}).get("has_actuators")
    return value or "HAS_ACTUATORS_UNKNOWN"


def available(name: str, state: dict) -> tuple[bool, str | None]:
    """Whether a control can be pressed now, and in plain words why not."""
    control = _control(name)
    info = (state.get("localdish") or {}) if isinstance(state, dict) else {}
    if control.target == "router":
        if not isinstance(state, dict) or state.get("router") is None:
            return False, "no router found"
        if (info.get("router") or {}).get("reachable") is False:
            return False, "the router is not answering"
    elif (info.get("dish") or {}).get("reachable") is False:
        return False, "the dish is not answering"
    if name == "stow":
        status = _section(state, "dish").get("status")
        if not isinstance(status, dict):
            return False, "waiting for the dish's status"
        if _has_actuators(status) != "HAS_ACTUATORS_YES":
            return False, "this dish has no motors"
    if name == "speedtest":
        running = ((state.get("running") or {}).get("speedtest")) if isinstance(state, dict) else None
        if running and not running.get("done"):
            return False, "a speed test is running"
    return True, None


def current(name: str, state: dict) -> dict:
    """The control's current values, keyed like its params; {} when unknown or when it has none.

    A proto3 field left at its default is not on the wire, so a missing field in a config we did read means the
    default: snow melt AUTO, power save off, location NONE."""
    _control(name)
    if name in ("snow_melt", "power_save", "share_location"):
        config = dish_config(state)
        if not config:
            return {}
        if name == "snow_melt":
            return {"mode": config.get("snow_melt_mode", "AUTO")}
        if name == "power_save":
            return {"enabled": bool(config.get("power_save_mode", False)),
                    "start_minutes": int(config.get("power_save_start_minutes", 0)),
                    "duration_minutes": int(config.get("power_save_duration_minutes", 0))}
        return {"share": config.get("location_request_mode", "NONE") == "LOCAL"}
    if name == "stow":
        status = _section(state, "dish").get("status")
        if not isinstance(status, dict):
            return {}
        return {"unstow": bool(status.get("stow_requested", False))}
    return {}
