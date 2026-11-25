import unittest
from unittest.mock import patch
import cong
import ai


class CongTest(unittest.TestCase):
    def test_pullback_accepts_older_cross_without_dead_cross(self):
        bars = [{"date": "2026-11-09", "open": 10, "close": 10,
                 "high": 10.1, "low": 9.9, "volume": 100} for _ in range(120)]
        bars[-1].update(open=10, close=10.2, high=10.25, volume=200)
        # 金叉距今8根；最后红柱继续增长，均线支撑处回升。
        diff = [0]*111 + [.05]*8 + [.1]
        stock = {"code": "600001", "name": "测试", "price": 10.2, "mktcap": 50e8}
        with patch("cong.ema", side_effect=[diff, [0]*120, [0]*120]):
            result = cong.inspect(stock, bars, "2026-11-09")
        self.assertIsNotNone(result)
        self.assertEqual(result["setup"], "pullback")
        self.assertFalse(result["actionable"])
        with patch("cong.ema", side_effect=[[-.1]*120, [0]*120, [0]*120]):
            self.assertIsNone(cong.inspect(stock, bars, "2026-11-09", watch=True))

    def test_fit_is_bounded_and_rewards_fresher_tighter_shape(self):
        good = cong.suitability(1, 1, 1, 1.5, .9, 3, 10)
        weak = cong.suitability(7, 5, 7, .4, .5, 15, 30)
        self.assertGreater(good["score"], weak["score"])
        for fit in (good, weak):
            self.assertTrue(0 <= fit["score"] <= 100)
            self.assertAlmostEqual(fit["score"], sum(p["score"] for p in fit["parts"]))
            self.assertEqual(sum(p["max"] for p in fit["parts"]), 100)

    def test_watch_volume_without_buy_authorization(self):
        bars = [{"date": "2026-11-09", "open": 10, "close": 10,
                 "high": 10.1, "low": 9.9, "volume": 100} for _ in range(120)]
        bars[-1].update(close=10.4, high=10.45, volume=50)
        stock = {"code": "600001", "name": "测试", "price": 10.4, "mktcap": 50e8}
        for watch in (False, True):
            with patch("cong.ema", side_effect=[[0]*119+[.1], [0]*120, [0]*120]):
                result = cong.inspect(stock, bars, "2026-11-09", watch=watch)
            if watch:
                self.assertEqual(result["stage"], "watch")
                self.assertFalse(result["actionable"])
                self.assertTrue(result["defects"])
            else:
                self.assertIsNone(result)
        self.assertIsNone(cong.inspect(stock, bars[:-1], "2026-11-09", watch=True))
        flat = [dict(bars[0]) for _ in range(120)]
        self.assertIsNone(cong.inspect(stock, flat, "2026-11-09", watch=True))

    def test_missing_invalid_and_outside_pool_reviews(self):
        p = {"code": "600001", "actionable": False, "review": {"status": "pending"}}
        payload = {"cong": {"items": [p]}}
        for reviews in (None, {}, [None], [{"code": "600002", "decision": "保留观察", "reason": "未知"}],
                        [{"code": "600001", "decision": "保留观察", "reason": " "}]):
            ai.apply_cong_reviews(payload, reviews)
            self.assertEqual(p["review"]["status"], "pending")
            self.assertFalse(p["actionable"])

    def test_review_cannot_authorize_buy_or_unknown_code(self):
        p = {"code": "600001", "actionable": False}
        payload = {"cong": {"items": [p]}}
        ai.apply_cong_reviews(payload, [{"code": "600001", "decision": "买入", "reason": "好"}])
        self.assertNotIn("review", p)
        ai.apply_cong_reviews(payload, [{"code": "600001", "decision": "保留观察", "reason": "量能待收盘确认"}])
        self.assertFalse(p["actionable"])
        self.assertEqual(p["review"]["decision"], "保留观察")

    def test_universe(self):
        rows = [{"f12": c, "f14": n, "f20": cap, "f2": 10}
                for c, n, cap in [("600001", "正常", 50e8), ("300001", "创业", 50e8),
                                  ("000001", "ST测试", 50e8), ("600002", "大盘", 150e8)]]
        self.assertEqual([s["code"] for s in cong.universe(rows)], ["600001"])

    def test_bullish_shape_still_requires_chip_confirmation(self):
        bars = [{"date": "2026-11-04", "open": 10, "close": 10,
                 "high": 10.1, "low": 9.9, "volume": 100} for _ in range(120)]
        bars[-1].update(close=10.4, high=10.45, volume=200)
        # 控制MACD路径，单独检查形态、风险过滤与未确认状态。
        with patch("cong.ema", side_effect=[[0]*119+[.1], [0]*120, [0]*120]):
            result = cong.inspect({"code": "600001", "name": "测试", "price": 10.4,
                                   "mktcap": 50e8}, bars, "2026-11-04")
        self.assertIsNotNone(result)
        self.assertFalse(result["actionable"])
        self.assertEqual(len(result["pending"]), 2)

    def test_resonance_counts_exclude_self(self):
        rows = [
            {"code": "600001", "industry": "食品行业", "pct": 10.0},
            {"code": "600002", "industry": "食品行业", "pct": 6.0},
            {"code": "600003", "industry": "食品行业", "pct": 1.0},
            {"code": "600004", "industry": "电子信息", "pct": 5.5},
        ]
        res = cong.resonance(rows)
        self.assertEqual(res["600001"]["lu"], 0)      # 自身涨停不计入行业家数
        self.assertEqual(res["600001"]["up5"], 1)
        self.assertEqual(res["600003"]["up5"], 2)     # 同行业另有两只涨超5%
        self.assertEqual(res["600003"]["ind_n"], 3)
        self.assertEqual(res["600004"]["industry"], "电子信息")
        self.assertEqual(res["600004"]["up5"], 0)

    def test_scan_downgrades_shape_without_sector_resonance(self):
        bars = [{"date": "2026-11-09", "open": 10, "close": 10,
                 "high": 10.1, "low": 9.9, "volume": 100} for _ in range(120)]
        bars[-1].update(close=10.4, high=10.45, volume=200)
        stocks = [{"code": "600001", "name": "测试", "price": 10.4, "mktcap": 50e8}]
        weak = {"600001": {"industry": "食品行业", "ind_n": 20, "lu": 0, "up5": 1}}
        strong = {"600001": {"industry": "电子信息", "ind_n": 20, "lu": 3, "up5": 8}}
        with patch("cong.ema", side_effect=[[0]*119+[.1], [0]*120, [0]*120]):
            weak_res = cong.scan(stocks, {"600001": bars}, "2026-11-09", reson=weak)
        with patch("cong.ema", side_effect=[[0]*119+[.1], [0]*120, [0]*120]):
            strong_res = cong.scan(stocks, {"600001": bars}, "2026-11-09", reson=strong)
        self.assertEqual(weak_res["items"][0]["stage"], "watch")
        self.assertEqual(weak_res["items"][0]["reson_gate"], "weak")
        self.assertEqual(weak_res["shape_count"], 0)
        self.assertEqual(weak_res["reson_weak_count"], 1)
        self.assertFalse(weak_res["items"][0]["actionable"])
        self.assertEqual(strong_res["items"][0]["stage"], "shape")
        self.assertEqual(strong_res["items"][0]["reson_gate"], "ok")
        self.assertEqual(strong_res["shape_count"], 1)
        self.assertEqual(strong_res["reson_weak_count"], 0)

    def test_unknown_industry_does_not_downgrade(self):
        bars = [{"date": "2026-11-09", "open": 10, "close": 10,
                 "high": 10.1, "low": 9.9, "volume": 100} for _ in range(120)]
        bars[-1].update(close=10.4, high=10.45, volume=200)
        stocks = [{"code": "600001", "name": "测试", "price": 10.4, "mktcap": 50e8}]
        unknown = {"600001": {"industry": "未分类", "ind_n": 0, "lu": 0, "up5": None}}
        with patch("cong.ema", side_effect=[[0]*119+[.1], [0]*120, [0]*120]):
            out = cong.scan(stocks, {"600001": bars}, "2026-11-09", reson=unknown)
        self.assertEqual(out["items"][0]["stage"], "shape")
        self.assertEqual(out["items"][0]["reson_gate"], "unknown")
        self.assertEqual(out["reson_weak_count"], 0)
        # 未分类桶不产生行业家数，避免把全市场误当成一个行业
        rows = [{"code": "600001", "industry": "未分类", "pct": 10.0},
                {"code": "600002", "industry": "未分类", "pct": 10.0}]
        self.assertIsNone(cong.resonance(rows)["600001"]["up5"])

    def test_flat_dead_cross_and_stale_rejected(self):
        bars = [{"date": "2026-11-03", "open": 10, "close": 10,
                 "high": 10.1, "low": 9.9, "volume": 100} for _ in range(120)]
        self.assertIsNone(cong.inspect({}, bars, "2026-11-04"))
        self.assertIsNone(cong.inspect({}, bars, "2026-11-03"))
        self.assertEqual(cong.scan([{"code": "600001"}], {}, "2026-11-04")["missing"], 1)


if __name__ == "__main__":
    unittest.main()
