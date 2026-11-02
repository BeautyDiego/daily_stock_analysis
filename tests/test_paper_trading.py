# -*- coding: utf-8 -*-
"""A 股模拟盘的离线测试（账户、下单约束、次日开盘成交、建仓/调仓、概览）。"""

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from src.config import Config
from src.services.cn_tracker.holding_tracker_service import ensure_tracked_positions
from src.services.cn_tracker.paper_trading_service import (
    DEFAULT_POSITION_SLOTS,
    _affordable_quantity,
    _position_slots,
    build_paper_report_text,
    ensure_paper_account,
    get_paper_report,
    run_daily_paper,
)
from src.storage import (
    DatabaseManager,
    HoldingReview,
    PortfolioCashLedger,
    ScreeningPick,
    StockDaily,
    TrackedPosition,
)


class PaperTradingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "paper_trading.db")
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

    def _add_pick(
        self, code: str, name: str, price: float, pick_date: date, rank: int = 1
    ) -> None:
        with self.db.get_session() as session:
            session.add(
                ScreeningPick(
                    run_id=f"run-{code}-{pick_date}",
                    pick_date=pick_date,
                    market="cn",
                    code=code,
                    name=name,
                    rank=rank,
                    entry_price=price,
                )
            )
            session.commit()

    def _add_daily(
        self,
        code: str,
        trade_date: date,
        close: float,
        *,
        open_price: float | None = None,
        high: float | None = None,
        low: float | None = None,
        volume: float = 1_000_000,
        pct_chg: float = 0.0,
    ) -> None:
        open_price = close if open_price is None else open_price
        # 默认给出正常振幅，避免被「最高价 == 最低价」判成一字板
        high = open_price * 1.01 if high is None else high
        low = open_price * 0.99 if low is None else low
        with self.db.get_session() as session:
            session.add(
                StockDaily(
                    code=code,
                    date=trade_date,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=close * volume,
                    pct_chg=pct_chg,
                )
            )
            session.commit()

    # ------------------------------------------------------------------
    # 下单约束
    # ------------------------------------------------------------------

    def test_affordable_quantity_respects_budget_and_lot(self):
        # 4000 元预算：3.5 元 → 1100 股；12.72 元 → 300 股；50 元 → 一手都买不起
        self.assertEqual(_affordable_quantity(4000, 3.5, 20000), 1100)
        self.assertEqual(_affordable_quantity(4000, 12.72, 20000), 300)
        self.assertEqual(_affordable_quantity(4000, 50.0, 20000), 0)

    def test_affordable_quantity_respects_cash(self):
        self.assertEqual(_affordable_quantity(4000, 3.5, 1000), 200)

    def test_position_slots_follow_screening_top_n(self):
        with patch.dict(os.environ, {"SCREENING_PICKS_TOP_N": "3"}):
            self.assertEqual(_position_slots(), 3)
        with patch.dict(os.environ, {"SCREENING_PICKS_TOP_N": "10"}):
            self.assertEqual(_position_slots(), 10)

    def test_position_slots_fall_back_on_invalid_value(self):
        with patch.dict(os.environ, {"SCREENING_PICKS_TOP_N": "abc"}):
            self.assertEqual(_position_slots(), DEFAULT_POSITION_SLOTS)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SCREENING_PICKS_TOP_N", None)
            self.assertEqual(_position_slots(), DEFAULT_POSITION_SLOTS)

    # ------------------------------------------------------------------
    # 账户
    # ------------------------------------------------------------------

    def test_ensure_account_deposits_only_once(self):
        first = ensure_paper_account(initial_cash=20000, db=self.db)
        second = ensure_paper_account(initial_cash=20000, db=self.db)

        self.assertEqual(first["id"], second["id"])
        with self.db.get_session() as session:
            entries = (
                session.query(PortfolioCashLedger)
                .filter(PortfolioCashLedger.account_id == first["id"])
                .all()
            )
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(float(entries[0].amount), 20000.0)

    # ------------------------------------------------------------------
    # 建仓：必须用「次日开盘价」，而不是当日收盘价
    # ------------------------------------------------------------------

    def test_buys_at_next_day_open_not_close(self):
        signal_date = date(2026, 11, 2)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        self._add_daily("600016", trade_date, close=3.6, open_price=3.52)

        ensure_paper_account(initial_cash=20000, db=self.db)
        stats = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["buys"]), 1)
        # 成交价取开盘价（含 0.1% 滑点），不是收盘价 3.6
        self.assertAlmostEqual(stats["buys"][0]["price"], 3.52, places=2)
        self.assertLess(stats["buys"][0]["price"], 3.6)

    def test_ignores_same_day_picks(self):
        """T 日收盘后生成的选股，T 日当天不能成交。"""
        trade_date = date(2026, 11, 4)
        self._add_pick("600016", "民生银行", 3.5, trade_date, rank=1)
        ensure_paper_account(initial_cash=20000, db=self.db)

        stats = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(stats["buys"], [])

    def test_skips_sealed_limit_up(self):
        signal_date = date(2026, 11, 4)
        trade_date = date(2026, 11, 5)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        self._add_daily(
            "600016",
            trade_date,
            close=3.85,
            open_price=3.85,
            high=3.85,
            low=3.85,
            pct_chg=9.9,
        )
        ensure_paper_account(initial_cash=20000, db=self.db)

        stats = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(stats["buys"], [])

    def test_skips_suspended_stock(self):
        signal_date = date(2026, 11, 4)
        trade_date = date(2026, 11, 5)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        self._add_daily("600016", trade_date, close=3.5, volume=0)
        ensure_paper_account(initial_cash=20000, db=self.db)

        stats = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(stats["buys"], [])

    def test_skips_stale_signal(self):
        """半个月前的旧信号不能当成新信号执行。"""
        signal_date = date(2026, 9, 1)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        self._add_daily("600016", trade_date, close=3.6, open_price=3.5)
        ensure_paper_account(initial_cash=20000, db=self.db)

        stats = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(stats["buys"], [])

    def test_skips_when_no_daily_bar(self):
        signal_date = date(2026, 11, 4)
        trade_date = date(2026, 11, 5)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        ensure_paper_account(initial_cash=20000, db=self.db)

        stats = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(stats["buys"], [])
        self.assertAlmostEqual(stats["snapshot"]["total_cash"], 20000.0)

    def test_run_daily_paper_buys_multiple_picks(self):
        signal_date = date(2026, 11, 2)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        self._add_pick("601668", "中国建筑", 5.0, signal_date, rank=2)
        self._add_daily("600016", trade_date, close=3.6, open_price=3.5)
        self._add_daily("601668", trade_date, close=5.1, open_price=5.0)

        ensure_paper_account(initial_cash=20000, db=self.db)
        stats = run_daily_paper(as_of=trade_date, db=self.db)

        quantities = {item["code"]: item["quantity"] for item in stats["buys"]}
        self.assertEqual(quantities.get("600016"), 1100)
        self.assertEqual(quantities.get("601668"), 800)

    # ------------------------------------------------------------------
    # 调仓：exit 清仓、reduce 减半、跌停卖不出
    # ------------------------------------------------------------------

    def _open_position(self, signal_date: date, buy_date: date, code: str = "600016") -> None:
        self._add_pick(code, "民生银行", 3.5, signal_date, rank=1)
        self._add_daily(code, buy_date, close=3.6, open_price=3.5)
        ensure_paper_account(initial_cash=20000, db=self.db)
        run_daily_paper(as_of=buy_date, db=self.db)

    def _add_review(self, code: str, review_date: date, advice: str) -> None:
        ensure_tracked_positions(
            [{"code": code, "name": "民生银行", "price": 3.5}],
            pick_date=review_date - timedelta(days=1),
            db=self.db,
        )
        with self.db.get_session() as session:
            position = session.query(TrackedPosition).first()
            session.add(
                HoldingReview(
                    position_id=position.id,
                    code=code,
                    review_date=review_date,
                    holding_days=1,
                    advice=advice,
                )
            )
            session.commit()

    def test_sells_all_on_exit_advice(self):
        self._open_position(date(2026, 11, 2), date(2026, 11, 3))
        sell_date = date(2026, 11, 4)
        self._add_daily("600016", sell_date, close=3.7, open_price=3.65)
        self._add_review("600016", sell_date, "exit")

        stats = run_daily_paper(as_of=sell_date, db=self.db)

        self.assertEqual(len(stats["sells"]), 1)
        self.assertEqual(stats["sells"][0]["advice"], "exit")
        self.assertEqual(stats["snapshot"]["positions"], [])

    def test_skips_sell_when_limit_down_sealed(self):
        """一字跌停当天卖不掉，必须留到下一交易日。"""
        self._open_position(date(2026, 11, 2), date(2026, 11, 3))
        sell_date = date(2026, 11, 4)
        self._add_daily(
            "600016",
            sell_date,
            close=3.24,
            open_price=3.24,
            high=3.24,
            low=3.24,
            pct_chg=-10.0,
        )
        self._add_review("600016", sell_date, "exit")

        stats = run_daily_paper(as_of=sell_date, db=self.db)

        self.assertEqual(stats["sells"], [])
        self.assertTrue(stats["snapshot"]["positions"])

    def test_reduce_halves_position(self):
        self._open_position(date(2026, 11, 2), date(2026, 11, 3))
        sell_date = date(2026, 11, 4)
        self._add_daily("600016", sell_date, close=3.7, open_price=3.65)
        self._add_review("600016", sell_date, "reduce")

        stats = run_daily_paper(as_of=sell_date, db=self.db)

        self.assertEqual(len(stats["sells"]), 1)
        sold = stats["sells"][0]["quantity"]
        remaining = stats["snapshot"]["positions"][0]["quantity"]
        # 1100 股 → 卖 500（整手、不超过一半）→ 剩 600
        self.assertEqual(sold, 500)
        self.assertEqual(remaining, 600)

    def test_hold_keeps_position(self):
        self._open_position(date(2026, 11, 2), date(2026, 11, 3))
        review_date = date(2026, 11, 4)
        self._add_daily("600016", review_date, close=3.7, open_price=3.65)
        self._add_review("600016", review_date, "hold")

        stats = run_daily_paper(as_of=review_date, db=self.db)

        self.assertEqual(stats["sells"], [])
        self.assertTrue(stats["snapshot"]["positions"])

    def test_run_daily_paper_is_idempotent(self):
        signal_date = date(2026, 11, 2)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        self._add_daily("600016", trade_date, close=3.6, open_price=3.5)
        ensure_paper_account(initial_cash=20000, db=self.db)

        first = run_daily_paper(as_of=trade_date, db=self.db)
        second = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(len(first["buys"]), 1)
        self.assertEqual(second["buys"], [])
        self.assertAlmostEqual(
            first["snapshot"]["total_equity"], second["snapshot"]["total_equity"], places=2
        )

    # ------------------------------------------------------------------
    # 与跟踪表的同步（否则复盘覆盖不到模拟盘持仓）
    # ------------------------------------------------------------------

    def test_buy_syncs_tracked_position_as_holding(self):
        signal_date = date(2026, 11, 2)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        self._add_daily("600016", trade_date, close=3.6, open_price=3.5)
        ensure_paper_account(initial_cash=20000, db=self.db)

        stats = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["buys"]), 1)
        with self.db.get_session() as session:
            position = (
                session.query(TrackedPosition)
                .filter(TrackedPosition.code == "600016")
                .one()
            )
        self.assertEqual(position.status, "holding")
        self.assertEqual(position.source_date, trade_date)

    def test_exit_closes_tracked_position(self):
        self._open_position(date(2026, 11, 2), date(2026, 11, 3))
        sell_date = date(2026, 11, 4)
        self._add_daily("600016", sell_date, close=3.7, open_price=3.65)
        self._add_review("600016", sell_date, "exit")

        run_daily_paper(as_of=sell_date, db=self.db)

        with self.db.get_session() as session:
            position = (
                session.query(TrackedPosition)
                .filter(TrackedPosition.code == "600016")
                .one()
            )
        self.assertEqual(position.status, "closed")

    # ------------------------------------------------------------------
    # 概览
    # ------------------------------------------------------------------

    def test_paper_report_before_init(self):
        report = get_paper_report(as_of=date(2026, 11, 5), db=self.db)
        self.assertFalse(report["initialized"])

    def test_paper_report_after_run(self):
        signal_date = date(2026, 11, 2)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        self._add_daily("600016", trade_date, close=3.6, open_price=3.5)
        ensure_paper_account(initial_cash=20000, db=self.db)
        run_daily_paper(as_of=trade_date, db=self.db)

        report = get_paper_report(as_of=trade_date, db=self.db)
        self.assertTrue(report["initialized"])
        self.assertEqual(report["initial_cash"], 20000.0)
        self.assertTrue(report["positions"])

        text = build_paper_report_text(report)
        self.assertIn("模拟盘日报", text)
        self.assertIn("600016", text)


if __name__ == "__main__":
    unittest.main()
