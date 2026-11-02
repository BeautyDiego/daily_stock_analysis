# -*- coding: utf-8 -*-
"""A 股模拟盘的离线测试（账户、下单约束、建仓/调仓、概览）。"""

import os
import tempfile
import unittest
from datetime import date

from src.config import Config
from src.services.cn_tracker.holding_tracker_service import ensure_tracked_positions
from src.services.cn_tracker.paper_trading_service import (
    _affordable_quantity,
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

    def _add_pick(self, code: str, name: str, price: float, pick_date: date, rank: int = 1) -> None:
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

    def _add_daily(self, code: str, trade_date: date, close: float) -> None:
        with self.db.get_session() as session:
            session.add(
                StockDaily(
                    code=code,
                    date=trade_date,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1_000_000,
                    amount=close * 1_000_000,
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
    # 建仓 / 调仓
    # ------------------------------------------------------------------

    def test_run_daily_paper_buys_picks(self):
        trade_date = date(2026, 11, 2)
        self._add_pick("600016", "民生银行", 3.5, trade_date, rank=1)
        self._add_pick("601668", "中国建筑", 5.0, trade_date, rank=2)
        self._add_daily("600016", trade_date, 3.5)
        self._add_daily("601668", trade_date, 5.0)

        ensure_paper_account(initial_cash=20000, db=self.db)
        stats = run_daily_paper(as_of=trade_date, db=self.db)

        quantities = {item["code"]: item["quantity"] for item in stats["buys"]}
        self.assertEqual(quantities.get("600016"), 1100)
        self.assertEqual(quantities.get("601668"), 800)
        self.assertGreater(stats["snapshot"]["total_equity"], 0)
        self.assertLessEqual(stats["snapshot"]["total_cash"], 20000)

    def test_run_daily_paper_sells_on_exit_advice(self):
        buy_date = date(2026, 10, 30)
        sell_date = date(2026, 11, 2)

        self._add_pick("600016", "民生银行", 3.5, buy_date, rank=1)
        self._add_daily("600016", buy_date, 3.5)
        ensure_paper_account(initial_cash=20000, db=self.db)
        run_daily_paper(as_of=buy_date, db=self.db)

        ensure_tracked_positions(
            [{"code": "600016", "name": "民生银行", "price": 3.5}],
            pick_date=buy_date,
            db=self.db,
        )
        self._add_daily("600016", sell_date, 3.4)
        with self.db.get_session() as session:
            position = session.query(TrackedPosition).first()
            session.add(
                HoldingReview(
                    position_id=position.id,
                    code="600016",
                    review_date=sell_date,
                    holding_days=1,
                    advice="exit",
                )
            )
            session.commit()

        stats = run_daily_paper(as_of=sell_date, db=self.db)

        self.assertEqual(len(stats["sells"]), 1)
        self.assertEqual(stats["sells"][0]["code"], "600016")
        self.assertEqual(stats["sells"][0]["advice"], "exit")
        # 清仓后不应再持有
        self.assertEqual(stats["snapshot"]["positions"], [])

    def test_run_daily_paper_does_not_buy_without_close_price(self):
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, trade_date, rank=1)
        ensure_paper_account(initial_cash=20000, db=self.db)

        stats = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(stats["buys"], [])
        self.assertAlmostEqual(stats["snapshot"]["total_cash"], 20000.0)

    def test_run_daily_paper_is_idempotent(self):
        trade_date = date(2026, 11, 4)
        self._add_pick("600016", "民生银行", 3.5, trade_date, rank=1)
        self._add_daily("600016", trade_date, 3.5)
        ensure_paper_account(initial_cash=20000, db=self.db)

        first = run_daily_paper(as_of=trade_date, db=self.db)
        second = run_daily_paper(as_of=trade_date, db=self.db)

        self.assertEqual(len(first["buys"]), 1)
        self.assertEqual(second["buys"], [])
        self.assertAlmostEqual(
            first["snapshot"]["total_equity"], second["snapshot"]["total_equity"], places=2
        )

    # ------------------------------------------------------------------
    # 概览
    # ------------------------------------------------------------------

    def test_paper_report_before_init(self):
        report = get_paper_report(as_of=date(2026, 11, 5), db=self.db)
        self.assertFalse(report["initialized"])

    def test_paper_report_after_run(self):
        trade_date = date(2026, 11, 6)
        self._add_pick("600016", "民生银行", 3.5, trade_date, rank=1)
        self._add_daily("600016", trade_date, 3.5)
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
