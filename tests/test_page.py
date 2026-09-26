"""The page: static files that make no outside requests, and example API replies that carry what app.js reads."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "localdish" / "static"
API = Path(__file__).resolve().parent / "fixtures" / "api"
FILES = ("index.html", "app.js", "app.css")

# Every path app.js reads, per reply. "x[]" means every item of list x; "x?" means the key must be present (it may be
# null); "x~" means it may be absent altogether (a proto3 field at its default is not on the wire). Keep this in step with app.js: a key the page reads that the server stops sending is a blank card.
READS = {
    "state": [
        "localdish.version", "localdish.now", "localdish.demo",
        "localdish.dish.host", "localdish.dish.reachable", "localdish.dish.error?", "localdish.dish.age_s",
        "localdish.router.host", "localdish.router.reachable", "localdish.router.age_s",
        "dish.status.pop_ping_latency_ms", "dish.status.pop_ping_drop_rate~",
        "dish.status.downlink_throughput_bps", "dish.status.uplink_throughput_bps",
        "dish.status.obstruction_stats.fraction_obstructed", "dish.status.obstruction_stats.time_obstructed",
        "dish.status.device_state.uptime_s", "dish.status.eth_speed_mbps", "dish.status.is_snr_above_noise_floor",
        "dish.status.gps_stats.gps_sats", "dish.status.gps_stats.gps_valid", "dish.status.alerts",
        "dish.device_info", "dish.config", "dish.diagnostics", "dish.location?", "dish.location_error?",
        "router.status", "router.device_info", "router.ping.results", "router.ping_age_s?",
        "router.clients[].name", "router.clients[].iface",
        "explain.headline.text", "explain.headline.tone", "explain.headline.detail",
        "explain.alerts[].text", "explain.alerts[].tone",
        "explain.aim.az_now", "explain.aim.az_want", "explain.aim.el_now", "explain.aim.el_want",
        "explain.aim.turn_deg?", "explain.aim.ok?", "explain.aim.text", "explain.aim.confidence",
        "explain.aim.uncertainty_deg?", "explain.aim.held_s~", "explain.facts",
        "controls[].name", "controls[].label", "controls[].group", "controls[].confirm", "controls[].params",
        "controls[].available", "controls[].reason?", "controls[].current",
        "running.speedtest?",
        "events[].t", "events[].kind", "events[].text",
    ],
    "history": [
        "age_s", "error?", "ring.latency_ms", "ring.drop", "ring.down_bps", "ring.up_bps", "ring.power_w",
        "outages[].cause", "outages[].start_unix", "outages[].ago_s", "outages[].duration_s", "event_log",
    ],
    "obstruction": [
        "age_s", "error?", "map.num_rows", "map.num_cols", "map.snr", "map.max_theta_deg", "map.min_elevation_deg",
        "map.map_reference_frame",
    ],
}

PARAM_TYPES = {"bool", "int", "choice", "enum"}


def text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def api(name: str) -> dict:
    with open(API / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


def missing(obj, path: str) -> list[str]:
    """The places where path is absent in obj, as readable paths; [] when it is everywhere."""
    if path.endswith("~"):
        return []
    nullable = path.endswith("?")
    parts = path.rstrip("?").split(".")

    def walk(o, i, where):
        if i == len(parts):
            return []
        key = parts[i]
        many = key.endswith("[]")
        key = key[:-2] if many else key
        if not isinstance(o, dict) or key not in o:
            return [f"{where}.{key}".lstrip(".")]
        v = o[key]
        if v is None:
            return [] if nullable and i == len(parts) - 1 else [f"{where}.{key} is null"]
        if many:
            if not isinstance(v, list) or not v:
                return [f"{where}.{key} is not a non-empty list"]
            out = []
            for n, item in enumerate(v):
                out += walk(item, i + 1, f"{where}.{key}[{n}]")
            return out
        return walk(v, i + 1, f"{where}.{key}")

    return walk(obj, 0, "")


class TestStaticFiles(unittest.TestCase):
    def test_files_exist(self):
        for name in FILES:
            self.assertTrue((STATIC / name).is_file(), name)
            self.assertGreater((STATIC / name).stat().st_size, 0, name)

    def test_no_outside_urls(self):
        for name in FILES:
            for m in re.finditer(r"https?://([^/\s\"'<>)]+)", text(name)):
                host = m.group(1).split(":")[0]
                self.assertIn(host, ("127.0.0.1", "localhost"), f"{name}: {m.group(0)}")
            self.assertNotRegex(text(name), r"""(?:src|href)\s*=\s*["']//""", f"{name}: protocol-relative url")
            self.assertNotRegex(text(name), r"@import|url\(\s*['\"]?(?!data:)", f"{name}: a stylesheet fetch")

    def test_no_browser_dialogs_or_eval(self):
        js = text("app.js")
        self.assertNotIn("window.confirm", js)
        self.assertNotRegex(js, r"\balert\s*\(")
        self.assertNotRegex(js, r"\bconfirm\s*\(")
        self.assertNotRegex(js, r"\beval\s*\(")
        self.assertNotRegex(js, r"new\s+Function\s*\(")
        self.assertIn("<dialog", text("index.html"))

    def test_nothing_inline_the_csp_would_block(self):
        html, js = text("index.html"), text("app.js")
        self.assertNotRegex(html, r"\sstyle\s*=", "inline style attribute")
        self.assertNotRegex(html, r"\son[a-z]+\s*=", "inline event handler")
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html), [], "inline script")
        self.assertNotIn("<style", html)
        self.assertNotRegex(js, r"""style\s*=\s*\\?["']""", "a style attribute written from app.js")
        self.assertNotRegex(js, r"javascript:")

    def test_page_loads_its_own_files(self):
        html = text("index.html")
        self.assertRegex(html, r'<link rel="stylesheet" href="app\.css">')
        self.assertRegex(html, r'<script src="app\.js"></script>')

    def test_every_id_app_js_looks_up_exists(self):
        ids = set(re.findall(r'\bid="([^"]+)"', text("index.html")))
        wanted = set(re.findall(r'\$\("([^"]+)"\)', text("app.js")))
        wanted |= set(re.findall(r'getElementById\("([^"]+)"\)', text("app.js")))
        self.assertGreater(len(wanted), 20)
        self.assertEqual(sorted(wanted - ids), [])

    def test_ids_are_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', text("index.html"))
        self.assertEqual(len(ids), len(set(ids)))

    def test_posts_carry_the_guard_header(self):
        js = text("app.js")
        self.assertIn('"X-Localdish": "1"', js)
        self.assertIn('"Content-Type": "application/json"', js)

    def test_polls_at_the_designed_cadence_only_while_visible(self):
        js = text("app.js")
        self.assertRegex(js, r"state:\s*1000\b")
        self.assertRegex(js, r"history:\s*5000\b")
        self.assertRegex(js, r"obstruction:\s*60000\b")
        self.assertIn('document.visibilityState === "visible"', js)
        self.assertIn("visibilitychange", js)


class TestExampleReplies(unittest.TestCase):
    def test_replies_parse_and_carry_what_the_page_reads(self):
        for name, paths in READS.items():
            data = api(name)
            for path in paths:
                self.assertEqual(missing(data, path), [], f"{name}.json: {path}")

    def test_ring_is_unrolled_and_even(self):
        ring = api("history")["ring"]
        lengths = {k: len(ring[k]) for k in ("latency_ms", "drop", "down_bps", "up_bps", "power_w")}
        self.assertEqual(set(lengths.values()), {ring["count"]}, lengths)

    def test_outages_newest_first(self):
        starts = [o["start_unix"] for o in api("history")["outages"]]
        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_obstruction_grid_matches_its_size(self):
        m = api("obstruction")["map"]
        self.assertEqual(len(m["snr"]), m["num_rows"] * m["num_cols"])
        self.assertTrue(any(v < 0 for v in m["snr"]) and any(v >= 0 for v in m["snr"]))

    def test_control_params_are_ones_the_page_can_draw(self):
        for c in api("state")["controls"]:
            self.assertIn(c["group"], ("restart", "settings", "maintenance", "tests"), c["name"])
            for p in c["params"]:
                self.assertIn(p["type"], PARAM_TYPES, f"{c['name']}.{p['name']}")
                if p["type"] in ("choice", "enum"):
                    self.assertTrue(p.get("choices"), f"{c['name']}.{p['name']}")
            if not c["available"]:
                self.assertTrue(c["reason"], f"{c['name']} is unavailable without a reason")

    def test_no_credential_named_keys(self):
        pattern = re.compile(r"(?i)(password|passphrase|psk|secret|token|key)$")

        def walk(o, where):
            if isinstance(o, dict):
                for k, v in o.items():
                    self.assertIsNone(pattern.search(str(k)), f"{where}.{k}")
                    walk(v, f"{where}.{k}")
            elif isinstance(o, list):
                for v in o:
                    walk(v, where)

        for name in READS:
            walk(api(name), name)


if __name__ == "__main__":
    unittest.main()
