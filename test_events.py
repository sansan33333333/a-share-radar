# -*- coding: utf-8 -*-
"""events.py 规则分类的离线测试。不发网络请求。"""
import unittest
from datetime import datetime

import events


def d(day):
    return datetime(2026, 11, 10, day, 0)


class TestClassifyTitles(unittest.TestCase):
    def test_holder_reduction_blocks(self):
        """煌上煌真实公告序列 → block。"""
        items = [
            {"date": "2026-10-30 17:27", "title": "煌上煌：新余煌上煌及其一致行动人持股比例已降至61.41%"},
            {"date": "2026-11-03 20:28", "title": "煌上煌：截至10月31日公司累计回购股份6890000股"},
            {"date": "2026-11-09 17:33", "title": "煌上煌大宗交易成交134.00万股"},
            {"date": "2026-11-10 18:30", "title": "煌上煌：新余煌上煌减持304万股 占总股本0.54%"},
            {"date": "2026-11-10 20:34", "title": "煌上煌：关于控股股东及其一致行动人权益变动触及1%整数倍的公告"},
        ]
        r = events.classify_titles(items, days=30, as_of=d(22))
        self.assertEqual(r["level"], "block")
        self.assertTrue(any("减持" in x or "权益变动" in x for x in r["reasons"]))

    def test_small_holder_reduction_warns(self):
        """一般股东减持（非控股股东）→ warn，不否决。"""
        items = [{"date": "2026-11-05 09:00", "title": "某股东减持计划实施完毕的公告"}]
        r = events.classify_titles(items, days=30, as_of=d(10))
        self.assertEqual(r["level"], "warn")

    def test_non_reduction_pledge_warns(self):
        items = [{"date": "2026-11-05 09:00", "title": "关于股东部分股份质押的公告"}]
        r = events.classify_titles(items, days=30, as_of=d(10))
        self.assertEqual(r["level"], "warn")
        self.assertTrue(any("质押" in x for x in r["reasons"]))

    def test_investigation_blocks(self):
        items = [{"date": "2026-11-05 09:00", "title": "关于公司及实际控制人收到中国证监会立案告知书的公告"}]
        r = events.classify_titles(items, days=30, as_of=d(10))
        self.assertEqual(r["level"], "block")

    def test_no_reduction_pledge_is_excluded(self):
        """不减持/终止减持/解除质押是利多或中性，不算利空。"""
        items = [
            {"date": "2026-11-05 09:00", "title": "关于控股股东承诺不减持公司股份的公告"},
            {"date": "2026-11-05 09:00", "title": "关于股东提前终止减持计划的公告"},
            {"date": "2026-11-05 09:00", "title": "关于股东部分股份解除质押的公告"},
        ]
        r = events.classify_titles(items, days=30, as_of=d(10))
        self.assertEqual(r["level"], "none")

    def test_old_announcements_ignored(self):
        """超出门窗期的公告不计入。"""
        items = [{"date": "2026-08-01 09:00", "title": "控股股东减持股份计划公告"}]
        r = events.classify_titles(items, days=30, as_of=d(22))
        self.assertEqual(r["level"], "none")

    def test_clean_list_none(self):
        items = [
            {"date": "2026-11-01 09:00", "title": "2026年第三季度报告"},
            {"date": "2026-11-08 09:00", "title": "关于召开2026年第一次临时股东大会的通知"},
        ]
        r = events.classify_titles(items, days=30, as_of=d(12))
        self.assertEqual(r["level"], "none")
        self.assertEqual(r["hits"], [])

    def test_block_wins_over_warn(self):
        """同时命中 warn 与 block 时取 block。"""
        items = [
            {"date": "2026-11-05 09:00", "title": "控股股东减持股份的公告"},
            {"date": "2026-11-06 09:00", "title": "关于股东部分股份质押的公告"},
        ]
        r = events.classify_titles(items, days=30, as_of=d(10))
        self.assertEqual(r["level"], "block")


if __name__ == "__main__":
    unittest.main()
