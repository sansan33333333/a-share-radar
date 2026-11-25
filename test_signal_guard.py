import copy
import unittest
import signal_guard


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.pick = {"price": 10, "model": {"p_tb": 60, "p_up_3d": 60,
                     "rank_pct": .95, "exp_excess_3d": 1},
                     "plan": {"buy_low": 9.9, "buy_high": 10.1, "stop": 9.6, "target1": 11}}
        self.ctx = {"live": {"n": 8, "hit": 60, "avg_ret": 1, "ic": .1}}
        self.pick["trend_guard"] = {"ok": True}

    def test_trend_rejects_downtrend_and_invalid_data(self):
        self.assertFalse(signal_guard.trend_check([{"close": 30-i*.2} for i in range(25)], 30)["ok"])
        self.assertFalse(signal_guard.trend_check([{"close": 10}]*25, 9)["ok"])
        self.assertFalse(signal_guard.trend_check([], 10)["ok"])
        self.assertTrue(signal_guard.trend_check([{"close": 10+i*.1} for i in range(25)], 12.4)["ok"])
        self.pick.pop("trend_guard")
        self.assertIn("个股趋势尚未确认", signal_guard.reasons(self.pick, self.ctx))

    def test_healthy_signal(self):
        self.assertEqual(signal_guard.reasons(self.pick, self.ctx), [])

    def test_failed_model_blocks_high_score(self):
        self.pick["score"] = 99
        self.ctx["live"]["hit"] = 32
        self.assertTrue(signal_guard.reasons(self.pick, self.ctx))

    def test_missing_and_nan_fail_closed(self):
        self.assertTrue(signal_guard.reasons(self.pick, {}))
        self.pick["model"]["p_up_3d"] = float("nan")
        self.assertTrue(signal_guard.reasons(self.pick, self.ctx))

    def test_chasing_and_stop_distance(self):
        for price, stop in ((10.5, 9.6), (10, 9.0)):
            pick = copy.deepcopy(self.pick)
            pick["price"], pick["plan"]["stop"] = price, stop
            self.assertTrue(signal_guard.reasons(pick, self.ctx))


if __name__ == "__main__":
    unittest.main()
