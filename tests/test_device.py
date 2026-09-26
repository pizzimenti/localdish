from __future__ import annotations

import copy
import json
import os
import unittest

from localdish import device

HERE = os.path.dirname(os.path.abspath(__file__))
MINI = json.load(open(os.path.join(HERE, "..", "localdish", "demo", "mini.json")))
STANDARD = json.load(open(os.path.join(HERE, "fixtures", "standard.json")))


def state_of(fixture, router=True):
    dish = fixture["dish"]
    state = {
        "localdish": {"dish": {"reachable": True, "error": None}, "router": {"reachable": True, "error": None}},
        "dish": {"status": dish["get_status"]["dish_get_status"],
                 "device_info": dish["get_device_info"]["get_device_info"]["device_info"],
                 "config": dish["dish_get_config"]["dish_get_config"]["dish_config"]},
        "router": None,
        "running": {"speedtest": None},
    }
    if router and fixture["router"]:
        state["router"] = {"status": fixture["router"]["get_status"]["wifi_get_status"]}
    return state


class FakeDevice:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def call(self, op, fields=None, *, timeout=None):
        self.calls.append((op, fields))
        return self.answers[op]


class ReadTest(unittest.TestCase):
    def test_reads_the_response_key(self):
        dev = FakeDevice(MINI["dish"])
        status = device.read(dev, "dish_status")
        self.assertEqual(status["disablement_code"], "OKAY")
        self.assertEqual(dev.calls, [("get_status", None)])

    def test_router_status_key(self):
        self.assertEqual(device.READS["router_status"], ("router", "get_status", "wifi_get_status"))
        self.assertIn("uptime_s", device.read(FakeDevice(MINI["router"]), "router_status")["device_state"])

    def test_absent_key_is_empty(self):
        self.assertEqual(device.read(FakeDevice({"get_status": {"api_version": 42}}), "dish_status"), {})

    def test_every_read_names_a_target(self):
        for name, (target, op, key) in device.READS.items():
            self.assertIn(target, ("dish", "router"), name)


class BuildTest(unittest.TestCase):
    def test_restart(self):
        self.assertEqual(device.build("restart", {}), ("dish", "reboot", {}))
        self.assertEqual(device.build("restart", None), ("dish", "reboot", {}))

    def test_snow_melt(self):
        self.assertEqual(device.build("snow_melt", {"mode": "AUTO"}),
                         ("dish", "dish_set_config", {"dish_config": {"snow_melt_mode": "AUTO",
                                                                      "apply_snow_melt_mode": True}}))

    def test_power_save(self):
        target, op, fields = device.build("power_save", {"enabled": True, "start_minutes": 0, "duration_minutes": 1440})
        cfg = fields["dish_config"]
        self.assertEqual((target, op), ("dish", "dish_set_config"))
        self.assertEqual((cfg["power_save_mode"], cfg["power_save_start_minutes"], cfg["power_save_duration_minutes"]),
                         (True, 0, 1440))
        self.assertTrue(cfg["apply_power_save_mode"] and cfg["apply_power_save_start_minutes"]
                        and cfg["apply_power_save_duration_minutes"])

    def test_share_location(self):
        on = device.build("share_location", {"share": True})[2]["dish_config"]
        off = device.build("share_location", {"share": False})[2]["dish_config"]
        self.assertEqual((on["location_request_mode"], off["location_request_mode"]), ("LOCAL", "NONE"))
        self.assertTrue(on["apply_location_request_mode"])

    def test_other_ops(self):
        self.assertEqual(device.build("clear_obstructions", {}), ("dish", "dish_clear_obstruction_map", {}))
        self.assertEqual(device.build("stow", {"unstow": True}), ("dish", "dish_stow", {"unstow": True}))
        self.assertEqual(device.build("speedtest", {}), ("router", "start_speedtest", {}))
        self.assertEqual(device.build("ping", {}), ("router", "get_ping", {}))

    def test_bad_params(self):
        bad = [
            ("nope", {}),
            ("restart", {"now": True}),
            ("snow_melt", {}),
            ("snow_melt", {"mode": "auto"}),
            ("snow_melt", {"mode": "AUTO", "extra": 1}),
            ("power_save", {"enabled": 1, "start_minutes": 0, "duration_minutes": 60}),
            ("power_save", {"enabled": True, "start_minutes": 1440, "duration_minutes": 60}),
            ("power_save", {"enabled": True, "start_minutes": -1, "duration_minutes": 60}),
            ("power_save", {"enabled": True, "start_minutes": 0, "duration_minutes": 0}),
            ("power_save", {"enabled": True, "start_minutes": 0, "duration_minutes": 1441}),
            ("power_save", {"enabled": True, "start_minutes": 0.5, "duration_minutes": 60}),
            ("power_save", {"enabled": True, "start_minutes": True, "duration_minutes": 60}),
            ("power_save", {"enabled": True, "start_minutes": "60", "duration_minutes": 60}),
            ("share_location", {"share": "yes"}),
            ("stow", {}),
            ("stow", [True]),
        ]
        for name, params in bad:
            with self.subTest(name=name, params=params):
                with self.assertRaises(ValueError) as caught:
                    device.build(name, params)
                message = str(caught.exception)
                self.assertTrue(message and message[0].islower(), message)

    def test_controls_table(self):
        self.assertEqual([c.name for c in device.CONTROLS],
                         ["restart", "snow_melt", "power_save", "share_location", "clear_obstructions", "stow",
                          "speedtest", "ping"])
        for c in device.CONTROLS:
            self.assertIn(c.group, ("restart", "settings", "maintenance", "tests"))
            self.assertEqual(c.confirm, c.confirm.lower(), c.name)
            self.assertEqual(c.label, c.label.lower(), c.name)


class AvailableTest(unittest.TestCase):
    def test_stow_needs_motors(self):
        self.assertEqual(device.available("stow", state_of(MINI)), (False, "this dish has no motors"))
        self.assertEqual(device.available("stow", state_of(STANDARD))[0], False)
        motors = state_of(STANDARD)
        motors["dish"]["status"] = copy.deepcopy(motors["dish"]["status"])
        motors["dish"]["status"]["has_actuators"] = "HAS_ACTUATORS_YES"
        self.assertEqual(device.available("stow", motors), (True, None))

    def test_router_controls_need_a_router(self):
        for name in ("speedtest", "ping"):
            self.assertEqual(device.available(name, state_of(STANDARD)), (False, "no router found"))
            self.assertEqual(device.available(name, state_of(MINI)), (True, None))

    def test_unreachable(self):
        state = state_of(MINI)
        state["localdish"]["dish"]["reachable"] = False
        self.assertEqual(device.available("restart", state), (False, "the dish is not answering"))
        state["localdish"]["router"]["reachable"] = False
        self.assertEqual(device.available("ping", state)[0], False)

    def test_one_speedtest_at_a_time(self):
        state = state_of(MINI)
        state["running"]["speedtest"] = {"started": 1.0, "status": None, "done": False}
        self.assertEqual(device.available("speedtest", state), (False, "a speed test is running"))
        state["running"]["speedtest"]["done"] = True
        self.assertEqual(device.available("speedtest", state), (True, None))

    def test_settings_available(self):
        for name in ("restart", "snow_melt", "power_save", "share_location", "clear_obstructions"):
            self.assertEqual(device.available(name, state_of(MINI)), (True, None), name)


class CurrentTest(unittest.TestCase):
    def test_mini(self):
        state = state_of(MINI)
        self.assertEqual(device.current("snow_melt", state), {"mode": "ALWAYS_OFF"})
        self.assertEqual(device.current("power_save", state),
                         {"enabled": True, "start_minutes": 540, "duration_minutes": 240})
        self.assertEqual(device.current("share_location", state), {"share": False})
        self.assertEqual(device.current("stow", state), {"unstow": False})
        self.assertEqual(device.current("restart", state), {})

    def test_defaults_when_off_the_wire(self):
        state = {"dish": {"config": {"dish_config": {"swupdate_reboot_hour": 3}}}}
        self.assertEqual(device.current("snow_melt", state), {"mode": "AUTO"})
        self.assertEqual(device.current("power_save", state),
                         {"enabled": False, "start_minutes": 0, "duration_minutes": 0})
        state["dish"]["config"]["dish_config"]["location_request_mode"] = "LOCAL"
        self.assertEqual(device.current("share_location", state), {"share": True})

    def test_config_from_status_when_no_config_read(self):
        state = state_of(STANDARD)
        state["dish"]["config"] = None
        self.assertEqual(device.current("power_save", state)["start_minutes"], 420)

    def test_nothing_read(self):
        self.assertEqual(device.current("snow_melt", {"dish": {"status": None, "config": None}}), {})


if __name__ == "__main__":
    unittest.main()
