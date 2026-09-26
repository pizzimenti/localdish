from __future__ import annotations

import copy
import json
import math
import os
import time
import unittest

from localdish import explain

HERE = os.path.dirname(os.path.abspath(__file__))
MINI = json.load(open(os.path.join(HERE, "..", "localdish", "demo", "mini.json")))
STANDARD = json.load(open(os.path.join(HERE, "fixtures", "standard.json")))
MINI_STATUS = MINI["dish"]["get_status"]["dish_get_status"]
MINI_HISTORY = MINI["dish"]["get_history"]["dish_get_history"]
STANDARD_STATUS = STANDARD["dish"]["get_status"]["dish_get_status"]


def status(**changes):
    """A healthy, minimal get_status body, changed as asked (None removes a key)."""
    s = {"disablement_code": "OKAY", "pop_ping_latency_ms": 25.0, "alerts": {}}
    for key, value in changes.items():
        if value is None:
            s.pop(key, None)
        else:
            s[key] = value
    return s


def aligned(az_now, az_want, el_now=60.0, el_want=60.0, state="FILTER_CONVERGED", actuators="HAS_ACTUATORS_NO"):
    return {"alignment_stats": {"boresight_azimuth_deg": az_now, "desired_boresight_azimuth_deg": az_want,
                                "boresight_elevation_deg": el_now, "desired_boresight_elevation_deg": el_want,
                                "attitude_estimation_state": state, "attitude_uncertainty_deg": 1.0,
                                "has_actuators": actuators}}


class RingTest(unittest.TestCase):
    def test_before_the_ring_fills(self):
        h = {"current": 3, "pop_ping_latency_ms": [10.0, 11.0, 12.0, 0.0, 0.0], "pop_ping_drop_rate": [0, 0, 1, 0, 0]}
        r = explain.ring(h)
        self.assertEqual((r["n"], r["current"], r["count"]), (5, 3, 3))
        self.assertEqual(r["latency_ms"], [10.0, 11.0, 12.0])
        self.assertEqual(r["drop"], [0, 0, 1])
        self.assertEqual(r["down_bps"], [None, None, None])

    def test_wrapped(self):
        # 7 samples into a ring of 5: values 5, 6 overwrote slots 0, 1; newest (6) at (7 - 1) % 5 = 1
        h = {"current": 7, "pop_ping_latency_ms": [5.0, 6.0, 2.0, 3.0, 4.0]}
        r = explain.ring(h)
        self.assertEqual(r["count"], 5)
        self.assertEqual(r["latency_ms"], [2.0, 3.0, 4.0, 5.0, 6.0])

    def test_exactly_full(self):
        r = explain.ring({"current": 5, "power_in": [0.0, 1.0, 2.0, 3.0, 4.0]})
        self.assertEqual(r["power_w"], [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_empty_and_nan(self):
        self.assertEqual(explain.ring({})["count"], 0)
        self.assertEqual(explain.ring({"current": 2, "pop_ping_latency_ms": [float("nan"), 1.0]})["latency_ms"],
                         [None, 1.0])

    def test_mini(self):
        r = explain.ring(MINI_HISTORY)
        self.assertEqual((r["n"], r["count"]), (900, 900))
        newest = (MINI_HISTORY["current"] - 1) % 900
        self.assertEqual(r["latency_ms"][-1], MINI_HISTORY["pop_ping_latency_ms"][newest])
        self.assertEqual(r["up_bps"][0], MINI_HISTORY["uplink_throughput_bps"][MINI_HISTORY["current"] % 900])
        json.dumps(r, allow_nan=False)


class OutagesTest(unittest.TestCase):
    def test_gps_to_unix(self):
        # GPS 0 is 1980-01-06T00:00:00 in GPS time; 18 leap seconds later UTC reads 18 s behind
        self.assertEqual(explain.gps_ns_to_unix(0), 315964800 - 18)
        # a recorded pair: the Mini's first outage and its event-log entry for the same boot
        self.assertAlmostEqual(explain.gps_ns_to_unix(1474434132159601881),
                               MINI_HISTORY["event_log"]["start_timestamp_ns"] / 1e9, places=3)

    def test_newest_first(self):
        h = {"outages": [{"cause": "BOOTING", "start_timestamp_ns": 1_000_000_000_000_000_000, "duration_ns": 2_500_000_000},
                         {"start_timestamp_ns": 1_000_000_100_000_000_000, "duration_ns": 1_000_000_000,
                          "did_switch": True}]}
        out = explain.outages(h, now_unix=1_000_000_200 + 315964800 - 18)
        self.assertEqual([o["cause"] for o in out], ["UNKNOWN", "BOOTING"])
        self.assertEqual(out[0]["ago_s"], 100.0)
        self.assertEqual(out[0]["did_switch"], True)
        self.assertEqual(out[1]["did_switch"], False)
        self.assertEqual(out[1]["duration_s"], 2.5)
        self.assertEqual(out[1]["cause_text"], "booting")

    def test_mini(self):
        out = explain.outages(MINI_HISTORY, now_unix=1790410225.44)
        self.assertEqual(len(out), 499)
        self.assertTrue(all(a["start_unix"] >= b["start_unix"] for a, b in zip(out, out[1:])))
        self.assertEqual(out[-1]["cause"], "BOOTING")
        self.assertTrue(0 < out[0]["ago_s"] < 3600)


class HeadlineTest(unittest.TestCase):
    def check(self, st, text, tone, error=None):
        h = explain.headline(st, error)
        self.assertEqual((h["text"], h["tone"]), (text, tone), h)
        self.assertEqual(h["detail"], h["detail"].lower() if "Starlink" not in h["detail"] else h["detail"])
        return h

    def test_unreachable(self):
        h = self.check(None, "dish unreachable", "bad", error="timed out")
        self.assertEqual(h["detail"], "timed out")
        self.check(status(), "dish unreachable", "bad", error="connection refused")

    def test_no_answer_yet(self):
        self.check(None, "connecting", "warn")

    def test_booting(self):
        self.check(status(outage={"cause": "BOOTING"}), "booting", "warn")

    def test_searching(self):
        for cause in ("NO_SCHEDULE", "NO_SATS", "SKY_SEARCH"):
            self.check(status(outage={"cause": cause}), "searching", "warn")
        self.assertIn("schedule", explain.headline(status(outage={"cause": "NO_SCHEDULE"}), None)["detail"])

    def test_obstructed_now(self):
        self.check(status(outage={"cause": "OBSTRUCTED", "did_switch": True}), "obstructed now", "bad")

    def test_other_outages(self):
        self.check(status(outage={"cause": "SLEEPING"}), "sleeping", "warn")
        self.check(status(outage={"cause": "STOWED"}), "stowed", "warn")
        self.check(status(outage={"cause": "THERMAL_SHUTDOWN"}), "too hot", "bad")
        self.check(status(outage={"cause": "NO_DOWNLINK"}), "offline", "bad")
        self.check(status(outage={}), "offline", "bad")      # cause UNKNOWN is off the wire
        self.assertEqual(explain.headline(status(outage={"cause": "NEW_CAUSE"}), None)["detail"], "new cause")

    def test_disabled_per_code(self):
        h = self.check(status(disablement_code="NO_ACTIVE_ACCOUNT"), "disabled", "bad")
        self.assertEqual(h["detail"], "no active Starlink account")
        h = self.check(status(disablement_code="TOO_FAR_FROM_SERVICE_ADDRESS"), "disabled", "bad")
        self.assertEqual(h["detail"], "too far from the service address")
        h = self.check(status(disablement_code="SOME_NEW_CODE"), "disabled", "bad")
        self.assertEqual(h["detail"], "some new code")
        # disabled outranks the outage it causes
        self.check(status(disablement_code="BLOCKED_AREA", outage={"cause": "NO_SCHEDULE"}), "disabled", "bad")

    def test_unknown_state_is_not_disabled(self):
        self.check(status(disablement_code=None), "online", "ok")
        self.check(status(disablement_code="UNKNOWN_STATE"), "online", "ok")

    def test_no_internet(self):
        self.check(status(pop_ping_drop_rate=1.0), "no internet", "bad")

    def test_degraded(self):
        h = self.check(status(pop_ping_drop_rate=0.25), "degraded", "warn")
        self.assertIn("25 %", h["detail"])
        self.check(status(is_snr_persistently_low=True), "degraded", "warn")
        self.check(status(alerts={"thermal_throttle": True}), "degraded", "warn")

    def test_online(self):
        h = self.check(status(), "online", "ok")
        self.assertEqual(h["detail"], "25 ms to starlink")
        self.check(STANDARD_STATUS, "online", "ok")

    def test_update_pending(self):
        at = 1790420099
        h = self.check(MINI_STATUS, "online", "ok")
        self.assertIn(f"update pending, restarts ~{time.strftime('%H:%M', time.localtime(at))}", h["detail"])
        # it rides along with any other headline
        st = copy.deepcopy(MINI_STATUS)
        st["outage"] = {"cause": "BOOTING"}
        self.assertIn("update pending", explain.headline(st, None)["detail"])
        st = status(software_update_stats={"software_update_state": "REBOOT_REQUIRED"})
        self.assertIn("update pending, restarts when it can", explain.headline(st, None)["detail"])
        self.assertNotIn("update", explain.headline(STANDARD_STATUS, None)["detail"])


class AlertsTest(unittest.TestCase):
    def test_known_and_unknown(self):
        out = explain.alerts({"alerts": {"install_pending": True, "dish_water_detected": True,
                                         "some_new_alert": True, "roaming": False}})
        self.assertEqual([a["key"] for a in out], ["dish_water_detected", "some_new_alert", "install_pending"])
        self.assertEqual(out[0], {"key": "dish_water_detected", "text": "water in the dish", "tone": "bad"})
        self.assertEqual(out[1]["text"], "some new alert")

    def test_every_schema_alert_is_worded(self):
        # the DishAlerts fields in a Mini's schema, 2026.05
        keys = ["motors_stuck", "thermal_throttle", "thermal_shutdown", "mast_not_near_vertical", "unexpected_location",
                "slow_ethernet_speeds", "slow_ethernet_speeds_100", "roaming", "install_pending", "is_heating",
                "power_supply_thermal_throttle", "is_power_save_idle", "dbf_telem_stale", "low_motor_current",
                "lower_signal_than_predicted", "obstruction_map_reset", "dish_water_detected",
                "router_water_detected", "upsu_router_port_slow", "no_ethernet_link"]
        self.assertEqual(sorted(explain.ALERTS), sorted(keys))
        out = explain.alerts({"alerts": {k: True for k in keys}})
        self.assertEqual(len(out), len(keys))

    def test_none(self):
        self.assertEqual(explain.alerts({}), [])
        self.assertEqual(explain.alerts(STANDARD_STATUS), [])


class AimTest(unittest.TestCase):
    def test_mini_reads_about_11_left(self):
        a = explain.aim(MINI_STATUS)
        self.assertAlmostEqual(a["turn_deg"], -10.8, places=1)
        self.assertEqual(a["text"][0], "turn it 11° to the left, standing behind it")
        self.assertFalse(a["ok"])
        self.assertAlmostEqual(a["az_now"], 352.86, places=2)          # −7.14 as a compass bearing

    def test_quadrants(self):
        cases = [
            (10, 30, 20.0, "right"),        # north-east, clockwise
            (30, 10, -20.0, "left"),
            (100, 170, 70.0, "right"),      # south-east
            (200, 250, 50.0, "right"),      # south-west
            (300, 280, -20.0, "left"),      # north-west
            (170, -170, 20.0, "right"),     # across south (±180)
            (-170, 170, -20.0, "left"),
            (350, 10, 20.0, "right"),       # across north (0/360)
            (10, 350, -20.0, "left"),
            (-7, 3, 10.0, "right"),
        ]
        for now, want, turn, side in cases:
            with self.subTest(now=now, want=want):
                a = explain.aim(aligned(now, want))
                self.assertAlmostEqual(a["turn_deg"], turn)
                self.assertIn(f"to the {side}", a["text"][0])

    def test_half_turn_is_positive(self):
        self.assertEqual(explain.aim(aligned(0, 180))["turn_deg"], 180.0)
        self.assertEqual(explain.aim(aligned(180, 0))["turn_deg"], 180.0)

    def test_tilt(self):
        up = explain.aim(aligned(0, 0, el_now=60, el_want=70))
        self.assertEqual(up["tilt_deg"], 10)
        self.assertIn("10° up", up["text"][1])
        down = explain.aim(aligned(0, 0, el_now=70.6, el_want=63.4))
        self.assertLess(down["tilt_deg"], 0)
        self.assertIn("7° down", down["text"][1])

    def test_ok(self):
        a = explain.aim(aligned(0, 5, el_now=60, el_want=63))
        self.assertTrue(a["ok"])
        self.assertEqual(len(a["text"]), 1)
        self.assertFalse(explain.aim(aligned(0, 5.5))["ok"])

    def test_unconverged(self):
        a = explain.aim(aligned(0, 90, state="FILTER_UNCONVERGED"))
        self.assertIsNone(a["turn_deg"])
        self.assertIsNone(a["ok"])
        self.assertIn("no directions", a["text"][0])
        self.assertEqual(a["confidence"], "FILTER_UNCONVERGED")

    def test_motors_and_missing(self):
        self.assertIsNone(explain.aim(aligned(0, 90, actuators="HAS_ACTUATORS_YES")))
        self.assertIsNone(explain.aim({}))
        self.assertIsNone(explain.aim({"alignment_stats": {"boresight_azimuth_deg": 1.0}}))

    def test_standard(self):
        a = explain.aim(STANDARD_STATUS)
        self.assertLess(abs(a["turn_deg"]), 5)
        self.assertIn("down", a["text"][1])


def mini_state():
    return {
        "localdish": {"dish": {"reachable": True, "error": None}},
        "dish": {"status": MINI_STATUS, "device_info": MINI["dish"]["get_device_info"]["get_device_info"],
                 "config": MINI["dish"]["dish_get_config"]["dish_get_config"]},
        "router": {"status": MINI["router"]["get_status"]["wifi_get_status"],
                   "device_info": MINI["router"]["get_device_info"]["get_device_info"]["device_info"]},
    }


class FactsTest(unittest.TestCase):
    def test_mini(self):
        f = dict(explain.facts(mini_state()))
        self.assertEqual(f["dish hardware"], "mini1_pez_proto1")
        self.assertEqual(f["dish firmware"], "2026.05.13.mr80201")
        self.assertEqual(f["router firmware"], "2026.05.08.mr76937")
        self.assertEqual(f["country"], "US")
        self.assertEqual(f["service"], "consumer")
        self.assertEqual(f["mobility"], "mobile")
        self.assertEqual(f["dish uptime"], "3 h 8 min")
        self.assertEqual(f["ethernet"], "1000 Mb/s")
        self.assertEqual(f["gps"], "fixed, 21 satellites")
        self.assertEqual(f["software update"], "installed, needs a restart")
        self.assertTrue(f["update restart"].startswith("~"))
        self.assertEqual(f["update hour"], "03:00")
        self.assertEqual(f["snow melt"], "off")
        self.assertEqual(f["power save"], "on, 09:00–13:00")
        self.assertEqual(f["location sharing"], "off")
        self.assertEqual(f["obstructed sky"], "12.5 %")
        self.assertEqual(f["time obstructed"], "3.2 %")
        self.assertEqual(f["signal"], "above the noise floor")
        self.assertEqual(f["bandwidth"], "no limit")
        self.assertEqual(f["router uptime"], "3 h 1 min")
        self.assertEqual(f["router → dish"], "0.5 ms")
        self.assertIn("lost over 5 min", f["router → internet"])
        for label, value in f.items():
            self.assertIsInstance(value, str, label)

    def test_standard_without_router(self):
        state = {"dish": {"status": STANDARD_STATUS}, "router": None}
        f = dict(explain.facts(state))
        self.assertEqual(f["power save"], "off")                # a schedule is saved, but power_save_mode is off the wire
        self.assertEqual(f["bandwidth"], "download limited by plan, upload no limit")
        self.assertEqual(f["dish uptime"], "7 d 20 h")
        self.assertNotIn("long obstructions", f)          # interval NaN
        self.assertFalse(any(label.startswith("router") for label in f))

    def test_power_save_past_midnight(self):
        state = {"dish": {"config": {"power_save_mode": True, "power_save_start_minutes": 1380,
                                     "power_save_duration_minutes": 120}}}
        self.assertEqual(dict(explain.facts(state))["power save"], "on, 23:00–01:00")

    def test_nothing(self):
        self.assertEqual(explain.facts({}), [])


class ExplainTest(unittest.TestCase):
    def test_mini(self):
        out = explain.explain(mini_state(), 1790410225.44)
        self.assertEqual(set(out), {"headline", "alerts", "aim", "facts"})
        self.assertEqual(out["headline"]["text"], "online")
        self.assertEqual(out["alerts"][0]["key"], "install_pending")
        json.dumps(out, allow_nan=False)

    def test_unreachable(self):
        state = mini_state()
        state["localdish"]["dish"] = {"reachable": False, "error": "timed out"}
        self.assertEqual(explain.explain(state, 0)["headline"]["text"], "dish unreachable")

    def test_empty(self):
        out = explain.explain({}, 0)
        self.assertEqual(out["headline"]["text"], "connecting")
        self.assertIsNone(out["aim"])


if __name__ == "__main__":
    unittest.main()
