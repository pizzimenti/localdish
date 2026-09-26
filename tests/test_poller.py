from __future__ import annotations

import threading
import unittest

from localdish import poller
from localdish.grpcweb import GrpcError

try:
    from . import fakes
except ImportError:
    import fakes


def make(dish_answers=None, router_answers=None, router=True, clock=None, delay=0.0):
    clock = clock or fakes.Clock()
    dish = fakes.FakeDev(dish_answers, delay=delay)
    rtr = fakes.FakeDev(router_answers) if router else None
    logged = []
    p = poller.Poller({"dish": dish, "router": rtr}, device=fakes.fake_device_module(),
                      explain=fakes.fake_explain_module(), hosts={"dish": "192.168.100.1", "router": "192.168.1.1"},
                      clock=clock, log=logged.append)
    return p, dish, rtr, clock


class CadenceTest(unittest.TestCase):
    def test_the_table_is_the_designed_one(self):
        t = {j.name: (j.target, j.ops, j.watched_s, j.idle_s) for j in poller.CADENCE}
        self.assertEqual(t["status"], ("dish", ("get_status",), 1, 30))
        self.assertEqual(t["history"], ("dish", ("get_history",), 5, None))
        self.assertEqual(t["obstruction"], ("dish", ("dish_get_obstruction_map",), 60, None))
        self.assertEqual(t["info"], ("dish", ("get_device_info", "dish_get_config"), 300, 300))
        self.assertEqual(t["router"], ("router", ("get_status", "wifi_get_clients"), 30, None))

    def test_nobody_watching_status_every_30_s(self):
        p, dish, _, clock = make()
        self.assertEqual(p.run_due("dish"), ["status", "info"])
        for _ in range(29):
            clock.advance(1)
            self.assertEqual(p.run_due("dish"), [])
        clock.advance(1)
        self.assertEqual(p.run_due("dish"), ["status"])
        self.assertEqual(dish.ops().count("get_history"), 0)

    def test_watched_status_1_s_history_5_s_map_60_s(self):
        p, dish, _, clock = make()
        p.touch()
        self.assertEqual(p.run_due("dish"), ["status", "history", "obstruction", "info", "diagnostics", "location"])
        seen = {}
        for s in range(1, 61):
            clock.advance(1)
            p.touch()
            for name in p.run_due("dish"):
                seen.setdefault(name, []).append(s)
        self.assertEqual(seen["status"], list(range(1, 61)))
        self.assertEqual(seen["history"], list(range(5, 61, 5)))
        self.assertEqual(seen["obstruction"], [60])
        self.assertEqual(seen["diagnostics"], [60])
        self.assertNotIn("info", seen)
        self.assertEqual(dish.calls[dish.ops().index("get_history")][2], 20)   # the long timeout

    def test_watching_ends_10_s_after_the_last_state_request(self):
        p, _, _, clock = make()
        p.touch()
        clock.advance(9.9)
        self.assertTrue(p.watching())
        clock.advance(0.2)
        self.assertFalse(p.watching())

    def test_router_batch_every_30_s_while_watched(self):
        p, _, rtr, clock = make()
        p.touch()
        self.assertEqual(p.run_due("router"), ["router", "router_info"])
        self.assertEqual(rtr.ops(), ["get_status", "wifi_get_clients", "get_device_info"])
        for _ in range(29):
            clock.advance(1)
            p.touch()
            self.assertEqual(p.run_due("router"), [])
        clock.advance(1)
        p.touch()
        self.assertEqual(p.run_due("router"), ["router"])

    def test_nothing_is_retried_early_after_an_error(self):
        p, dish, _, clock = make({"get_status": ConnectionRefusedError(111, "Connection refused")})
        p.touch()
        self.assertEqual(p.run_due("dish"), ["status"])     # unreachable: only the status probe runs
        clock.advance(0.5)
        p.touch()
        self.assertEqual(p.run_due("dish"), [])
        clock.advance(0.5)
        p.touch()
        self.assertEqual(p.run_due("dish"), ["status"])
        self.assertEqual(dish.ops(), ["get_status", "get_status"])
        line = p.state()["localdish"]["dish"]
        self.assertFalse(line["reachable"])
        self.assertEqual(line["error"], "connection refused")

    def test_a_grpc_error_waits_its_slot_too(self):
        p, dish, _, clock = make({"get_diagnostics": GrpcError(13, "boom")})
        p.touch()
        p.run_due("dish")
        for _ in range(59):
            clock.advance(1)
            p.touch()
            self.assertNotIn("diagnostics", p.run_due("dish"))
        clock.advance(1)
        p.touch()
        self.assertIn("diagnostics", p.run_due("dish"))

    def test_location_held_after_permission_denied_until_refresh(self):
        p, dish, _, clock = make({"get_location": GrpcError(7, "GetLocation requests disabled due to policy")})
        p.touch()
        p.run_due("dish")
        self.assertEqual(p.state()["dish"]["location_error"],
                         "PERMISSION_DENIED: GetLocation requests disabled due to policy")
        clock.advance(301)
        p.touch()
        self.assertNotIn("location", p.run_due("dish"))
        self.assertEqual(p.refresh("location"), (200, {"ok": True}))
        self.assertIn("location", p.run_due("dish"))
        self.assertEqual(dish.ops().count("get_location"), 2)

    def test_the_batch_stops_at_an_unreachable_device(self):
        p, rtr_dish, rtr, clock = make(router_answers={"get_status": TimeoutError()})
        p.touch()
        p.run_due("router")
        self.assertEqual(rtr.ops(), ["get_status"])
        self.assertEqual(p.state()["localdish"]["router"]["error"], "timed out")

    def test_clients_are_a_list(self):
        p, _, _, _ = make(router_answers={"wifi_get_clients": {"clients": [{"name": "a"}]}})
        p.touch()
        p.run_due("router")
        self.assertEqual(p.state()["router"]["clients"], [{"name": "a"}])


class SingleFlightTest(unittest.TestCase):
    def test_two_concurrent_readers_make_one_device_call(self):
        p, dish, _, _ = make(delay=0.2)
        ts = [threading.Thread(target=p.run_job, args=("status",)) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(dish.ops(), ["get_status"])

    def test_one_call_in_flight_per_device(self):
        p, dish, _, _ = make(delay=0.05)
        ts = [threading.Thread(target=p.run_job, args=(n,)) for n in ("status", "history", "info", "diagnostics")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(dish.calls), 5)
        self.assertEqual(dish.max_active, 1)


class StatsAndEventsTest(unittest.TestCase):
    def test_calls_counted_total_and_last_60_s(self):
        p, _, _, clock = make()
        p.run_job("status")
        clock.advance(61)
        p.run_job("status")
        p.touch()
        s = p.stats()
        self.assertEqual(s["calls"]["dish:get_status"], {"total": 2, "last_60s": 1})
        self.assertTrue(s["watching"])

    def test_events_capped_at_200_and_50_shown(self):
        p, _, _, _ = make()
        for i in range(300):
            p.event("app", f"e{i}")
        self.assertEqual(len(p.events()), 200)
        shown = p.state()["events"]
        self.assertEqual(len(shown), 50)
        self.assertEqual(shown[-1]["text"], "e299")

    def test_reachability_headline_and_schema_events(self):
        p, dish, _, clock = make()
        p.run_job("status")
        texts = [e["text"] for e in p.events()]
        self.assertIn("dish reachable at 192.168.100.1", texts)
        self.assertIn("dish schema loaded", texts)
        self.assertIn("online", texts)
        dish.answers["get_status"] = OSError(113, "No route to host")
        clock.advance(30)
        p.run_job("status")
        texts = [e["text"] for e in p.events()]
        self.assertIn("dish unreachable at 192.168.100.1: no route to host", texts)
        self.assertEqual(texts[-1], "dish unreachable")          # the headline changed
        n = len(texts)
        p.run_job("status")
        self.assertEqual(len(p.events()), n)                    # no change, no event


    def test_headline_flicker_logs_once_per_tone_pair_per_30_s(self):
        p, dish, _, clock = make()
        p.run_job("status")                                       # waiting → online
        flip = [OSError(113, "No route to host"), {}] * 10
        for answer in flip:                                       # bad, ok, bad, ok … once a second
            dish.answers["get_status"] = answer
            clock.advance(1)
            p.run_job("status")
        heads = [e["text"] for e in p.events() if e["text"] in ("online", "dish unreachable")]
        self.assertEqual(heads, ["online", "dish unreachable", "online"])
        clock.advance(30)
        dish.answers["get_status"] = OSError(113, "No route to host")
        p.run_job("status")
        heads = [e["text"] for e in p.events() if e["text"] in ("online", "dish unreachable")]
        self.assertEqual(heads[-1], "dish unreachable")


class RefreshTest(unittest.TestCase):
    def test_rate_limit(self):
        p, _, _, clock = make()
        self.assertEqual(p.refresh("obstruction"), (200, {"ok": True}))
        p.touch()
        self.assertIn("obstruction", p.run_due("dish"))
        clock.advance(3)
        status, body = p.refresh("obstruction")
        self.assertEqual(status, 429)
        self.assertEqual(body, {"ok": False, "retry_after_s": 7})
        clock.advance(7)
        self.assertEqual(p.refresh("obstruction")[0], 200)

    def test_unknown_and_missing_router(self):
        p, _, _, _ = make(router=False)
        self.assertEqual(p.refresh("everything")[0], 404)
        self.assertEqual(p.refresh("router")[0], 409)


class ControlTest(unittest.TestCase):
    def test_ok_forces_info(self):
        p, dish, _, _ = make()
        status, body = p.control("restart", {})
        self.assertEqual((status, body["ok"], body["text"]), (200, True, "done"))
        self.assertEqual(dish.ops(), ["reboot"])
        self.assertIn("info", p.run_due("dish"))
        self.assertTrue(any(e["kind"] == "control" and e["text"] == "restart: done" for e in p.events()))

    def test_unavailable_is_409(self):
        p, dish, _, _ = make()
        status, body = p.control("stow", {})
        self.assertEqual((status, body), (409, {"ok": False, "error": "this dish has no motors"}))
        self.assertEqual(dish.calls, [])

    def test_no_router_is_409(self):
        p, _, _, _ = make(router=False)
        self.assertEqual(p.control("ping", {})[0], 409)

    def test_bad_params_400_unknown_404(self):
        p, _, _, _ = make()
        self.assertEqual(p.control("snow_melt", {"mode": "SOMETIMES"})[0], 400)
        self.assertEqual(p.control("launch", {})[0], 404)

    def test_grpc_error_is_502_with_its_message(self):
        p, _, _, _ = make({"reboot": GrpcError(9, "not now")})
        self.assertEqual(p.control("restart", {}), (502, {"ok": False, "error": "not now"}))
        self.assertEqual(p.control("restart", {})[0], 502)       # still offered: only UNIMPLEMENTED retires it

    def test_unimplemented_retires_the_control(self):
        p, dish, _, _ = make({"reboot": GrpcError(12, "")})
        self.assertEqual(p.control("restart", {})[0], 502)
        status, body = p.control("restart", {})
        self.assertEqual(status, 409)
        self.assertEqual(len(dish.calls), 1)
        c = next(c for c in p.state()["controls"] if c["name"] == "restart")
        self.assertFalse(c["available"])

    def test_unreachable_is_502(self):
        p, _, _, _ = make({"reboot": ConnectionResetError(104, "Connection reset by peer")})
        self.assertEqual(p.control("restart", {}), (502, {"ok": False, "error": "dish connection reset by peer"}))

    def test_ping_result_kept(self):
        p, _, _, _ = make(router_answers={"get_ping": {"results": {"x": {"latencyMs": 20.5}}}})
        self.assertEqual(p.control("ping", {})[0], 200)
        self.assertEqual(p.state()["router"]["ping"], {"results": {"x": {"latencyMs": 20.5}}})

    def test_controls_listed_with_current_values(self):
        p, _, _, _ = make({"dish_get_config": {"dish_config": {"snow_melt_mode": "AUTO"}}})
        p.run_job("info")
        c = {c["name"]: c for c in p.state()["controls"]}
        self.assertEqual(c["snow_melt"]["current"], {"mode": "AUTO"})
        self.assertEqual((c["stow"]["available"], c["stow"]["reason"]), (False, "this dish has no motors"))
        self.assertEqual(set(c["restart"]), {"name", "label", "group", "confirm", "params", "available", "reason",
                                             "current"})


class SpeedtestTest(unittest.TestCase):
    def test_runs_polls_every_second_and_finishes(self):
        p, _, rtr, clock = make(router_answers={"get_speedtest_status": {"status": {"running": True}}})
        self.assertEqual(p.control("speedtest", {})[0], 200)
        self.assertEqual(p.control("speedtest", {})[0], 409)         # one at a time
        p.poll_speedtest()
        p.poll_speedtest()                                             # same second: no second call
        self.assertEqual(rtr.ops().count("get_speedtest_status"), 1)
        run = p.state()["running"]["speedtest"]
        self.assertEqual((run["done"], run["status"]), (False, {"running": True}))
        clock.advance(1)
        rtr.answers["get_speedtest_status"] = {"status": {"running": False, "down": {"throughputs_mbps": [99.0]}}}
        p.poll_speedtest()
        run = p.state()["running"]["speedtest"]
        self.assertTrue(run["done"])
        self.assertEqual(run["status"]["down"], {"throughputs_mbps": [99.0]})
        self.assertEqual(p.control("speedtest", {})[0], 200)          # and again

    def test_gives_up_after_90_s(self):
        p, _, rtr, clock = make(router_answers={"get_speedtest_status": {"status": {"running": True}}})
        p.control("speedtest", {})
        for _ in range(91):
            p.poll_speedtest()
            clock.advance(1)
        run = p.state()["running"]["speedtest"]
        self.assertTrue(run["done"])
        self.assertLessEqual(rtr.ops().count("get_speedtest_status"), 91)


class CodecErrorTest(unittest.TestCase):
    class Schema:
        def fields(self, message):
            return {"get_status": None, "reboot": None}

    def test_an_op_the_firmware_lacks_is_unimplemented(self):
        p, dish, _, _ = make({"reboot": ValueError("unknown field dish_stow")})
        dish.schema = self.Schema()
        dish.answers["dish_stow"] = ValueError("unknown field dish_stow")
        p.device.available = lambda name, state: (True, None)
        self.assertEqual(p.control("stow", {}), (502, {"ok": False, "error": "this dish's firmware has no dish_stow"}))
        self.assertEqual(p.control("stow", {})[0], 409)

    def test_an_unreadable_reply_is_an_error_not_a_crash(self):
        p, dish, _, _ = make({"get_status": ValueError("truncated varint")})
        dish.schema = self.Schema()
        p.run_job("status")
        line = p.state()["localdish"]["dish"]
        self.assertTrue(line["reachable"])
        self.assertEqual(line["error"], "INTERNAL: could not read the dish's reply to get_status: truncated varint")

    def test_schema_reload_logged_from_the_stamp(self):
        p, dish, _, _ = make()
        dish.schema_loaded = 1.0
        p.run_job("status")
        dish.schema_loaded = 2.0
        p.run_job("info")
        texts = [e["text"] for e in p.events()]
        self.assertEqual((texts.count("dish schema loaded"), texts.count("dish schema reloaded")), (1, 1))


class HelpersTest(unittest.TestCase):
    def test_body_and_status_check(self):
        self.assertEqual(poller.body({"api_version": 42, "dish_get_status": {"a": 1}}), {"a": 1})
        self.assertEqual(poller.body({"api_version": 42}), {})
        with self.assertRaises(GrpcError) as cm:
            poller.check({"status": {"code": 9, "message": "busy"}, "reboot": {}})
        self.assertEqual(cm.exception.name, "FAILED_PRECONDITION")


if __name__ == "__main__":
    unittest.main()
