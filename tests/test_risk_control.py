# -*- coding: utf-8 -*-
"""组合风控、市场环境、滑点分档与退市风险过滤的离线测试。"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from src.config import Config
from src.services.cn_tracker.risk_control import (
    MARKET_REGIME_CODE,
    adjusted_slots,
    atr_percent_from_bars,
    evaluate_account_risk,
    evaluate_market_regime,
    stop_loss_pct,
    take_profit_pct,
)
from src.services.cn_tracker.risk_filter import filter_delisting_risk, has_delisting_risk
from src.services.cn_tracker.paper_trading_service import slippage_rate
from src.storage import (
    DatabaseManager,
    PortfolioAccount,
    PortfolioDailySnapshot,
    StockDaily,
)

ACCOUNT_ID = 1


class RiskControlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "risk.db")
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

    def _snapshot(self, day: date, equity: float) -> None:
        with self.db.get_session() as session:
            if session.query(PortfolioAccount).filter(
                PortfolioAccount.id == ACCOUNT_ID
            ).first() is None:
                session.add(
                    PortfolioAccount(
                        id=ACCOUNT_ID,
                        name="test-account",
                        base_currency="CNY",
                    )
                )
                session.commit()
            session.add(
                PortfolioDailySnapshot(
                    account_id=ACCOUNT_ID,
                    snapshot_date=day,
                    total_cash=0.0,
                    total_market_value=equity,
                    total_equity=equity,
                )
            )
            session.commit()

    def _index_days(self, closes) -> None:
        start = date(2026, 9, 1)
        with self.db.get_session() as session:
            for offset, close in enumerate(closes):
                session.add(
                    StockDaily(
                        code=MARKET_REGIME_CODE,
                        date=start + timedelta(days=offset),
                        open=close,
                        high=close,
                        low=close,
                        close=close,
                        volume=1_000_000,
                    )
                )
            session.commit()

    # ------------------------------------------------------------------
    # 账户风控
    # ------------------------------------------------------------------

    def test_pauses_on_drawdown(self):
        self._snapshot(date(2026, 10, 30), 100000)
        self._snapshot(date(2026, 11, 2), 89000)  # -11%

        result = evaluate_account_risk(self.db, ACCOUNT_ID, date(2026, 11, 3))

        self.assertTrue(result["paused"])
        self.assertIn("回撤", result["reason"])
        self.assertAlmostEqual(result["drawdown_pct"], -11.0, places=1)

    def test_pauses_on_daily_loss(self):
        self._snapshot(date(2026, 10, 30), 100000)
        self._snapshot(date(2026, 11, 2), 96500)  # 单日 -3.5%

        result = evaluate_account_risk(self.db, ACCOUNT_ID, date(2026, 11, 3))

        self.assertTrue(result["paused"])
        self.assertIn("单日", result["reason"])

    def test_no_pause_within_limits(self):
        self._snapshot(date(2026, 10, 30), 100000)
        self._snapshot(date(2026, 11, 2), 99000)  # -1%

        result = evaluate_account_risk(self.db, ACCOUNT_ID, date(2026, 11, 3))

        self.assertFalse(result["paused"])
        self.assertEqual(result["reason"], "")

    def test_no_snapshot_is_not_paused(self):
        result = evaluate_account_risk(self.db, ACCOUNT_ID, date(2026, 11, 3))
        self.assertFalse(result["paused"])
        self.assertEqual(result["reason"], "no_snapshot")

    # ------------------------------------------------------------------
    # 市场环境
    # ------------------------------------------------------------------

    def test_market_regime_risk_off_below_ma20(self):
        # 前 19 天 3000，最新一天 2900 → 收盘低于 MA20
        self._index_days([3000.0] * 19 + [2900.0])

        result = evaluate_market_regime(self.db, date(2026, 10, 1))

        self.assertTrue(result["risk_off"])
        self.assertEqual(result["note"], "below_ma20")

    def test_market_regime_ok_above_ma20(self):
        self._index_days([3000.0] * 19 + [3100.0])

        result = evaluate_market_regime(self.db, date(2026, 10, 1))

        self.assertFalse(result["risk_off"])

    def test_market_regime_degrades_without_data(self):
        result = evaluate_market_regime(self.db, date(2026, 11, 3))
        self.assertFalse(result["risk_off"])
        self.assertIn("insufficient", result["note"])

    def test_adjusted_slots_halves_in_risk_off(self):
        self.assertEqual(adjusted_slots(5, {"risk_off": True}), 2)
        self.assertEqual(adjusted_slots(5, {"risk_off": False}), 5)
        self.assertEqual(adjusted_slots(1, {"risk_off": True}), 1)

    # ------------------------------------------------------------------
    # ATR 自适应止损止盈
    # ------------------------------------------------------------------

    def test_stop_loss_scales_with_volatility(self):
        self.assertAlmostEqual(stop_loss_pct(2.0), -5.0)    # 下限
        self.assertAlmostEqual(stop_loss_pct(5.0), -10.0)
        self.assertAlmostEqual(stop_loss_pct(10.0), -15.0)  # 上限
        self.assertAlmostEqual(stop_loss_pct(None), -8.0)   # 兜底

    def test_take_profit_scales_with_volatility(self):
        self.assertAlmostEqual(take_profit_pct(2.0), 10.0)   # 下限
        self.assertAlmostEqual(take_profit_pct(5.0), 20.0)
        self.assertAlmostEqual(take_profit_pct(15.0), 40.0)  # 上限
        self.assertAlmostEqual(take_profit_pct(None), 20.0)

    def test_atr_percent_from_bars(self):
        # 21 根 bar，TR 恒为 1.0，收盘 100.5 → ATR% ≈ 1.0
        bars = [(101.0, 100.0, 100.5)] * 21
        pct = atr_percent_from_bars(bars)
        self.assertIsNotNone(pct)
        self.assertAlmostEqual(pct, 0.995, places=2)

    def test_atr_returns_none_without_enough_bars(self):
        self.assertIsNone(atr_percent_from_bars([(1.0, 1.0, 1.0)] * 5))

    def test_atr_returns_none_for_bad_close(self):
        bars = [(101.0, 100.0, 0.0)] * 21
        self.assertIsNone(atr_percent_from_bars(bars))

    # ------------------------------------------------------------------
    # 滑点分档
    # ------------------------------------------------------------------

    def test_slippage_tiers(self):
        self.assertAlmostEqual(slippage_rate(4.0), 0.004)
        self.assertAlmostEqual(slippage_rate(10.0), 0.0025)
        self.assertAlmostEqual(slippage_rate(30.0), 0.0015)

    def test_slippage_falls_back_on_bad_input(self):
        self.assertAlmostEqual(slippage_rate(0), 0.001)

    # ------------------------------------------------------------------
    # 退市风险
    # ------------------------------------------------------------------

    def test_filters_delisting_and_loss_makers(self):
        kept, dropped = filter_delisting_risk(
            [
                {"code": "600001", "name": "正常股份", "pe": 10, "pb": 1},
                {"code": "600002", "name": "*ST退市", "pe": 10, "pb": 1},
                {"code": "600003", "name": "亏损股份", "pe": -5, "pb": 1},
                {"code": "600004", "name": "负净资产", "pe": 12, "pb": -1},
            ]
        )
        self.assertEqual(dropped, 3)
        self.assertEqual([item["code"] for item in kept], ["600001"])

    def test_normal_candidate_passes(self):
        self.assertFalse(
            has_delisting_risk({"code": "600016", "name": "民生银行", "pe": 5.5, "pb": 0.6})
        )


if __name__ == "__main__":
    unittest.main()
