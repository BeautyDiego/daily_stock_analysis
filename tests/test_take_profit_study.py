# -*- coding: utf-8 -*-
"""止盈方式对比的离线测试（合成价格路径）。"""

import unittest

from src.services.cn_tracker.take_profit_study import _simulate, build_study_report


def _bar(high: float, low: float, close: float) -> dict:
    return {"high": high, "low": low, "close": close}


class SimulateTestCase(unittest.TestCase):
    """买入价统一 10.0，止盈目标 20%（4×ATR 的典型值）。"""

    ENTRY = 10.0
    TARGET = 20.0

    def _run(self, bars, mode):
        return _simulate(
            bars,
            self.ENTRY,
            target_pct=self.TARGET,
            mode=mode,
            max_hold_days=10,
        )

    # --- 一次性 ---

    def test_single_hits_target(self):
        bars = [_bar(10.5, 9.9, 10.4), _bar(12.2, 10.4, 12.0)]
        result = self._run(bars, "single")
        self.assertTrue(result["triggered"])
        self.assertAlmostEqual(result["return_pct"], 20.0, places=2)

    def test_single_never_hits_uses_last_close(self):
        bars = [_bar(10.5, 9.9, 10.3), _bar(10.8, 10.1, 10.2)]
        result = self._run(bars, "single")
        self.assertFalse(result["triggered"])
        self.assertAlmostEqual(result["return_pct"], 2.0, places=2)

    # --- 分批 ---

    def test_split_sells_stages_progressively(self):
        # 先触及 +10%（第一批），再触及 +20%（第二批），余下到期
        bars = [
            _bar(11.1, 9.9, 11.0),
            _bar(12.2, 11.0, 12.0),
            _bar(12.3, 11.8, 12.0),
        ]
        result = self._run(bars, "split")

        # 1/3 × 10% + 1/3 × 20% + 1/3 × 20%（到期收盘 12.0）
        expected = (1 / 3) * 10 + (1 / 3) * 20 + (1 / 3) * 20
        self.assertTrue(result["triggered"])
        self.assertAlmostEqual(result["return_pct"], expected, places=2)

    def test_split_only_first_stage(self):
        # 只触及 +10%，之后回落
        bars = [
            _bar(11.1, 9.9, 11.0),
            _bar(11.2, 10.0, 10.1),
            _bar(10.2, 9.8, 10.0),
        ]
        result = self._run(bars, "split")

        # 1/3 × 10% + 2/3 × 0%（到期收盘 10.0）
        self.assertTrue(result["triggered"])
        self.assertAlmostEqual(result["return_pct"], (1 / 3) * 10, places=2)

    # --- 移动止盈 ---

    def test_trail_activates_then_exits_on_pullback(self):
        # 先涨到 +12%（激活），再从峰值 11.2 回落 5% → 10.64
        bars = [
            _bar(11.2, 10.9, 11.1),
            _bar(11.2, 10.5, 10.6),
        ]
        result = self._run(bars, "trail")
        self.assertTrue(result["triggered"])
        self.assertAlmostEqual(result["return_pct"], 6.4, places=1)

    def test_trail_never_activates(self):
        bars = [_bar(10.5, 9.9, 10.3), _bar(10.6, 10.0, 10.1)]
        result = self._run(bars, "trail")
        self.assertFalse(result["triggered"])
        self.assertAlmostEqual(result["return_pct"], 1.0, places=2)

    # --- 止损 ---

    def test_stop_loss_cuts_before_target(self):
        # 第二根 bar 最低 9.1，跌破 -8% 的 9.2 → 按止损价成交
        bars = [_bar(10.2, 9.9, 10.1), _bar(9.5, 9.1, 9.3)]
        result = _simulate(
            bars,
            self.ENTRY,
            target_pct=20.0,
            mode="single",
            max_hold_days=10,
            stop_pct=-8.0,
        )
        self.assertTrue(result["stopped"])
        self.assertFalse(result["triggered"])
        self.assertAlmostEqual(result["return_pct"], -8.0, places=2)

    def test_stop_wins_when_both_touched_same_day(self):
        """同日既触及止损又触及止盈时，按止损处理（保守）。"""
        bars = [_bar(12.5, 9.0, 11.0)]
        result = _simulate(
            bars,
            self.ENTRY,
            target_pct=20.0,
            mode="single",
            max_hold_days=10,
            stop_pct=-8.0,
        )
        self.assertTrue(result["stopped"])
        self.assertAlmostEqual(result["return_pct"], -8.0, places=2)

    def test_returns_none_without_bars(self):
        self.assertIsNone(
            _simulate([], self.ENTRY, target_pct=20, mode="single", max_hold_days=10)
        )

    # --- 报告 ---

    def test_report_renders(self):
        stats = {
            "buy_points": 100,
            "loaded": 50,
            "lookback_days": 120,
            "max_hold_days": 10,
            "results": {
                "single": {
                    "label": "一次性（4×ATR 全卖）",
                    "samples": 100,
                    "mean": 1.2,
                    "median": 0.8,
                    "win_rate": 48.0,
                    "trigger_rate": 30.0,
                    "p90_loss": -3.2,
                },
                "split": {
                    "label": "分三批（2×ATR 卖 1/3、4×ATR 再 1/3、余下到期）",
                    "samples": 100,
                    "mean": 1.5,
                    "median": 0.9,
                    "win_rate": 51.0,
                    "trigger_rate": 42.0,
                    "p90_loss": -3.0,
                },
            },
        }
        text = build_study_report(stats)
        self.assertIn("止盈方式对比", text)
        self.assertIn("分批相对一次性", text)
        self.assertIn("+0.300pct", text)


if __name__ == "__main__":
    unittest.main()
