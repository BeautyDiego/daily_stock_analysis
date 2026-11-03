# -*- coding: utf-8 -*-
"""因子分层回测框架的离线测试（构造已知趋势的合成面板）。"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from src.config import Config
from src.services.cn_tracker.factor_layering import (
    _factor_series,
    _forward_returns,
    build_layering_report,
    run_factor_layering,
)
from src.storage import DatabaseManager, StockDaily

START = date(2026, 5, 1)


class FactorLayeringTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "layering.db")
        self._env_path = os.path.join(self._temp_dir.name, ".env")
        with open(self._env_path, "w", encoding="utf-8") as handle:
            handle.write("SCREENING_ENABLED=true\n")

        self._original_env = {
            key: os.environ.get(key) for key in ("ENV_FILE", "DATABASE_PATH")
        }
        os.environ["ENV_FILE"] = self._env_path
        os.environ["DATABASE_PATH"] = self._db_path

        Config._instance = None
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        Config._instance = None
        DatabaseManager.reset_instance()
        self._temp_dir.cleanup()

    def _seed_trending_panel(self, code_count: int = 30, days: int = 80) -> list:
        """构造「日内趋势越强、未来收益越高」的面板，用来验证分层方向。"""
        codes = []
        with self.db.get_session() as session:
            for index in range(code_count):
                code = f"60{index:04d}"
                codes.append(code)
                slope = 0.001 + index * 0.0004  # 0.1% → 1.3% 日涨幅
                price = 10.0
                for offset in range(days):
                    price *= 1 + slope
                    session.add(
                        StockDaily(
                            code=code,
                            date=START + timedelta(days=offset),
                            open=price,
                            high=price * 1.002,
                            low=price * 0.998,
                            close=price,
                            volume=1_000_000,
                            amount=price * 1_000_000,
                        )
                    )
            session.commit()
        return codes

    def test_forward_returns_avoid_lookahead(self):
        import pandas as pd

        frame = pd.DataFrame({"open": [10.0, 11.0, 12.0], "close": [10.5, 11.5, 12.5]})
        forward = _forward_returns(frame, 2)
        # T=0: 用 T+1 开盘 11.0 买入、T+2 收盘 12.5 卖出 → 12.5/11.0-1
        self.assertAlmostEqual(forward.iloc[0], 12.5 / 11.0 - 1, places=6)
        self.assertTrue(pd.isna(forward.iloc[-1]))

    def test_factor_series_direction(self):
        import pandas as pd

        closes = [10 * (1.01 ** step) for step in range(30)]
        frame = pd.DataFrame({"close": closes, "volume": [1.0] * 30, "amount": [1.0] * 30})
        momentum = _factor_series(frame, "momentum_5d").iloc[-1]
        reversal = _factor_series(frame, "reversal_5d").iloc[-1]
        self.assertGreater(momentum, 0)
        self.assertAlmostEqual(float(reversal), -float(momentum), places=8)

    def test_strong_trends_land_in_top_layer(self):
        codes = self._seed_trending_panel()

        stats = run_factor_layering(
            codes=codes,
            lookback_days=200,
            sync_missing=False,
            db=self.db,
        )

        item = stats["factors"]["momentum_5d"]
        self.assertGreater(item["periods"], 10)
        # 趋势最强的分在 Q5，收益应高于 Q1
        self.assertGreater(item["layer_returns"][-1], item["layer_returns"][0])
        self.assertGreater(item["ic_mean"], 0)
        self.assertEqual(item["verdict"], "正向有效")

    def test_report_renders(self):
        codes = self._seed_trending_panel(code_count=20, days=60)
        stats = run_factor_layering(
            codes=codes,
            lookback_days=200,
            sync_missing=False,
            db=self.db,
        )
        text = build_layering_report(stats)
        self.assertIn("因子分层回测", text)
        self.assertIn("近 5 日动量", text)

    def test_insufficient_data_returns_error(self):
        stats = run_factor_layering(
            codes=["600000"],
            lookback_days=200,
            sync_missing=False,
            db=self.db,
        )
        self.assertEqual(stats.get("error"), "insufficient_data")
        self.assertIn("未完成", build_layering_report(stats))


if __name__ == "__main__":
    unittest.main()
