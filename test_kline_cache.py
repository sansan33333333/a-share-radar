import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import date, timedelta
import kline_store as ks


class KlineCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(ks.config, "DATA_DIR", Path(self.tmp.name))
        self.path_patch.start()
        self.bars = [{"date": str(date(2026, 1, 1)+timedelta(days=i)),
                      "open": 10, "close": 10, "high": 11, "low": 9} for i in range(150)]
        self.day = self.bars[-1]["date"]

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_incremental_and_shared_reuse(self):
        ks.save("1.600001", self.bars)
        with patch.object(ks.ds, "fetch_kline", return_value=self.bars[-40:]) as fetch:
            result, how = ks.fetch("1.600001", self.day)
            self.assertEqual(how, "incremental")
            self.assertEqual(len(result), 150)
            fetch.assert_called_once_with("1.600001", lmt=40)
            self.assertEqual(ks.fetch("1.600001", self.day)[1], "cached")
            self.assertEqual(fetch.call_count, 1)

    def test_adjustment_refills_history(self):
        ks.save("1.600001", self.bars)
        adjusted = [dict(b, close=8, open=8) for b in self.bars]
        with patch.object(ks.ds, "fetch_kline", side_effect=[adjusted[-40:], adjusted]) as fetch:
            result, how = ks.fetch("1.600001", self.day)
            self.assertEqual(how, "full")
            self.assertEqual(fetch.call_args.kwargs["lmt"], 500)
            self.assertEqual(result[0]["close"], 8)

    def test_failure_keeps_history_but_returns_no_current_data(self):
        ks.save("1.600001", self.bars)
        with patch.object(ks.ds, "fetch_kline", return_value=[]):
            self.assertEqual(ks.fetch("1.600001", self.day), ([], "fail"))
        self.assertEqual(len(ks.read("1.600001")), 150)


if __name__ == "__main__":
    unittest.main()
