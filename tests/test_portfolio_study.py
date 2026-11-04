# -*- coding: utf-8 -*-
"""组合约束实验的离线测试（选股逻辑与报告渲染）。"""

import unittest
from datetime import date

from src.services.cn_tracker.portfolio_study import (
    UNLIMITED,
    _pick_top,
    build_constraint_report,
)

TRADE_DATE = date(2026, 11, 3)


class PickTopTestCase(unittest.TestCase):
    def _pick(self, ranked, *, industries=None, top_n=3, cap=UNLIMITED, cooldown=None, cooldown_days=0):
        return _pick_top(
            ranked,
            industries=industries or {},
            top_n=top_n,
            industry_cap=cap,
            cooldown=cooldown or {},
            trade_date=TRADE_DATE,
            cooldown_days=cooldown_days,
        )

    def test_cap_limits_same_industry(self):
        ranked = [("600001", 1.0), ("600002", 0.9), ("600003", 0.8)]
        industries = {"600001": "银行", "600002": "银行", "600003": "钢铁"}

        picked = self._pick(ranked, industries=industries, cap=1)

        # 银行只取 1 只，钢铁补上；两只银行不能同时入选
        self.assertEqual(picked, ["600001", "600003"])

    def test_cap_two_allows_two_of_same_industry(self):
        ranked = [("600001", 1.0), ("600002", 0.9), ("600003", 0.8)]
        industries = {"600001": "银行", "600002": "银行", "600003": "钢铁"}

        picked = self._pick(ranked, industries=industries, cap=2)

        self.assertEqual(picked, ["600001", "600002", "600003"])

    def test_unlimited_cap_takes_all(self):
        ranked = [("600001", 1.0), ("600002", 0.9), ("600003", 0.8)]
        industries = {"600001": "银行", "600002": "银行", "600003": "银行"}

        picked = self._pick(ranked, industries=industries, cap=UNLIMITED)

        self.assertEqual(len(picked), 3)

    def test_unknown_industry_is_not_capped(self):
        ranked = [("600001", 1.0), ("600002", 0.9)]
        # 两只都无行业信息：不应被上限拦住
        picked = self._pick(ranked, industries={}, cap=1, top_n=2)

        self.assertEqual(picked, ["600001", "600002"])

    def test_cooldown_skips_recent_pick(self):
        ranked = [("600001", 1.0)]
        cooldown = {"600001": date(2026, 11, 1)}

        picked = self._pick(
            ranked, top_n=1, cooldown=cooldown, cooldown_days=10
        )

        self.assertEqual(picked, [])

    def test_cooldown_expires(self):
        ranked = [("600001", 1.0)]
        cooldown = {"600001": date(2026, 10, 1)}

        picked = self._pick(
            ranked, top_n=1, cooldown=cooldown, cooldown_days=10
        )

        self.assertEqual(picked, ["600001"])

    def test_cooldown_disabled_when_zero(self):
        ranked = [("600001", 1.0)]
        cooldown = {"600001": TRADE_DATE}

        picked = self._pick(ranked, top_n=1, cooldown=cooldown, cooldown_days=0)

        self.assertEqual(picked, ["600001"])


class ReportTestCase(unittest.TestCase):
    def test_report_renders_caps(self):
        stats = {
            "loaded": 500,
            "industry_covered": 300,
            "factor": "负 20 日波动率",
            "holding_days": 5,
            "cooldown_days": 0,
            "results": {
                1: {
                    "label": "≤1 只",
                    "periods": 70,
                    "mean": 0.5,
                    "median": 0.4,
                    "volatility": 2.0,
                    "win_rate": 55.0,
                    "avg_industries": 5.0,
                },
                99: {
                    "label": "不限",
                    "periods": 70,
                    "mean": 0.3,
                    "median": 0.2,
                    "volatility": 3.0,
                    "win_rate": 52.0,
                    "avg_industries": 2.1,
                },
            },
        }
        text = build_constraint_report(stats)
        self.assertIn("组合约束对比", text)
        self.assertIn("≤1 只", text)
        self.assertIn("不限", text)

    def test_report_handles_error(self):
        text = build_constraint_report({"error": "insufficient_data", "loaded": 10})
        self.assertIn("未完成", text)


if __name__ == "__main__":
    unittest.main()
