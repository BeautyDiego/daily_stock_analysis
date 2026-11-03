# -*- coding: utf-8 -*-
"""模拟盘自动交易 bot 的离线测试（下单、开盘执行、盘中风控、收盘结算）。"""

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from src.config import Config
from src.services.cn_tracker import paper_bot_service
from src.services.cn_tracker.holding_tracker_service import (
    ensure_tracked_positions,
    set_position_status,
)
from src.services.cn_tracker.paper_bot_service import (
    execute_open,
    fetch_realtime_prices,
    plan_orders,
    settle,
    watch_intraday,
)
from src.services.cn_tracker.paper_trading_service import ensure_paper_account, run_daily_paper
from src.services.portfolio_service import PortfolioService
from src.storage import (
    DatabaseManager,
    HoldingReview,
    PaperOrder,
    ScreeningPick,
    StockDaily,
    TrackedPosition,
)


def _tencent_payload(code: str, *, price: float, prev_close: float, change_pct: float) -> bytes:
    parts = ["1", f"股票{code}", code, str(price), str(prev_close), str(price)]
    parts += ["0"] * 40
    parts[32] = str(change_pct)
    parts[33] = str(price)
    parts[34] = str(price)
    symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
    return (f'v_{symbol}="' + "~".join(parts) + '";').encode("gbk")


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class PaperBotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "paper_bot.db")
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

    # ------------------------------------------------------------------
    # 构造数据
    # ------------------------------------------------------------------

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
                    high=close * 1.01,
                    low=close * 0.99,
                    close=close,
                    volume=1_000_000,
                    amount=close * 1_000_000,
                )
            )
            session.commit()

    def _add_review(self, code: str, review_date: date, advice: str) -> None:
        ensure_tracked_positions(
            [{"code": code, "name": f"股票{code}", "price": 10.0}],
            pick_date=review_date,
            db=self.db,
        )
        with self.db.get_session() as session:
            position = (
                session.query(TrackedPosition)
                .filter(TrackedPosition.code == code)
                .order_by(TrackedPosition.id.desc())
                .first()
            )
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

    def _open_position(self, code: str, signal_date: date, buy_date: date, price: float = 3.5) -> None:
        self._add_pick(code, f"股票{code}", price, signal_date, rank=1)
        self._add_daily(code, buy_date, close=price)
        ensure_paper_account(initial_cash=20000, db=self.db)
        run_daily_paper(as_of=buy_date, db=self.db)

    # ------------------------------------------------------------------
    # 实时行情解析
    # ------------------------------------------------------------------

    def test_fetch_realtime_prices_parses_payload(self):
        payload = _tencent_payload("600016", price=3.42, prev_close=3.40, change_pct=0.59)
        with patch("requests.get", return_value=_FakeResponse(payload)):
            quotes = fetch_realtime_prices(["600016"])

        self.assertIn("600016", quotes)
        self.assertAlmostEqual(quotes["600016"]["price"], 3.42)
        self.assertAlmostEqual(quotes["600016"]["change_pct"], 0.59)

    def test_fetch_realtime_prices_returns_empty_without_codes(self):
        self.assertEqual(fetch_realtime_prices([]), {})

    # ------------------------------------------------------------------
    # 盘前下单
    # ------------------------------------------------------------------

    def test_plan_orders_creates_buy_from_previous_picks(self):
        signal_date = date(2026, 11, 2)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        ensure_paper_account(initial_cash=20000, db=self.db)

        stats = plan_orders(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["buys"]), 1)
        self.assertEqual(stats["buys"][0]["code"], "600016")
        with self.db.get_session() as session:
            order = session.query(PaperOrder).one()
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.side, "buy")

    def test_plan_orders_creates_sell_from_review(self):
        signal_date = date(2026, 11, 2)
        buy_date = date(2026, 11, 3)
        self._open_position("600016", signal_date, buy_date)
        review_date = date(2026, 11, 4)
        self._add_daily("600016", review_date, close=3.6)
        self._add_review("600016", review_date, "exit")

        stats = plan_orders(as_of=review_date, db=self.db)

        self.assertEqual(len(stats["sells"]), 1)
        self.assertEqual(stats["sells"][0]["advice"], "exit")

    # ------------------------------------------------------------------
    # 开盘执行
    # ------------------------------------------------------------------

    def test_execute_open_fills_normal_order(self):
        signal_date = date(2026, 11, 2)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        ensure_paper_account(initial_cash=20000, db=self.db)
        plan_orders(as_of=trade_date, db=self.db)

        payload = _tencent_payload("600016", price=3.52, prev_close=3.5, change_pct=0.57)
        with patch("requests.get", return_value=_FakeResponse(payload)), patch.object(
            paper_bot_service, "_send", lambda *_args, **_kwargs: True
        ):
            stats = execute_open(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["filled"]), 1)
        self.assertEqual(stats["filled"][0]["side"], "buy")
        self.assertAlmostEqual(stats["filled"][0]["price"], 3.53, places=2)

    def test_execute_open_skips_high_gap(self):
        signal_date = date(2026, 11, 2)
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, signal_date, rank=1)
        ensure_paper_account(initial_cash=20000, db=self.db)
        plan_orders(as_of=trade_date, db=self.db)

        # 高开 5%，超过 3% 阈值
        payload = _tencent_payload("600016", price=3.675, prev_close=3.5, change_pct=5.0)
        with patch("requests.get", return_value=_FakeResponse(payload)), patch.object(
            paper_bot_service, "_send", lambda *_args, **_kwargs: True
        ):
            stats = execute_open(as_of=trade_date, db=self.db)

        self.assertEqual(stats["filled"], [])
        self.assertEqual(len(stats["cancelled"]), 1)
        self.assertIn("高开", stats["cancelled"][0]["why"])

    # ------------------------------------------------------------------
    # 盘中止损 / 止盈
    # ------------------------------------------------------------------

    def _prepare_holding(self, code: str = "600016") -> date:
        signal_date = date(2026, 11, 2)
        buy_date = date(2026, 11, 3)
        self._open_position(code, signal_date, buy_date)
        return buy_date

    def test_watch_intraday_triggers_stop_loss(self):
        # T+1：建仓当日不可卖，风控检查放到次日
        trade_date = self._prepare_holding() + timedelta(days=1)
        # 成本约 3.5，现价跌 9%
        payload = _tencent_payload("600016", price=3.18, prev_close=3.5, change_pct=-9.1)
        with patch("requests.get", return_value=_FakeResponse(payload)):
            stats = watch_intraday(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["triggered"]), 1)
        self.assertIn("止损", stats["triggered"][0]["reason"])

    def test_watch_intraday_triggers_hard_take_profit(self):
        # T+1：建仓当日不可卖，风控检查放到次日
        trade_date = self._prepare_holding() + timedelta(days=1)
        payload = _tencent_payload("600016", price=4.3, prev_close=3.5, change_pct=22.0)
        with patch("requests.get", return_value=_FakeResponse(payload)):
            stats = watch_intraday(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["triggered"]), 1)
        self.assertIn("止盈", stats["triggered"][0]["reason"])

    def test_watch_intraday_keeps_position_without_signal(self):
        # T+1：建仓当日不可卖，风控检查放到次日
        trade_date = self._prepare_holding() + timedelta(days=1)
        payload = _tencent_payload("600016", price=3.6, prev_close=3.5, change_pct=2.8)
        with patch("requests.get", return_value=_FakeResponse(payload)):
            stats = watch_intraday(as_of=trade_date, db=self.db)

        self.assertEqual(stats["triggered"], [])

    def test_watch_intraday_trailing_take_profit(self):
        # T+1：建仓当日不可卖，风控检查放到次日
        trade_date = self._prepare_holding() + timedelta(days=1)
        # 先把峰值抬到 +16%（成本 3.5 → 4.06）
        peak_payload = _tencent_payload("600016", price=4.06, prev_close=3.5, change_pct=16.0)
        with patch("requests.get", return_value=_FakeResponse(peak_payload)):
            watch_intraday(as_of=trade_date, db=self.db)

        # 回落到 3.85（从 4.06 回落 5.2%），仍在 +10% 上方
        drop_payload = _tencent_payload("600016", price=3.85, prev_close=3.5, change_pct=10.0)
        with patch("requests.get", return_value=_FakeResponse(drop_payload)):
            stats = watch_intraday(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["triggered"]), 1)
        self.assertIn("移动止盈", stats["triggered"][0]["reason"])

    # ------------------------------------------------------------------
    # 手动立即建仓
    # ------------------------------------------------------------------

    def test_trade_now_uses_same_day_signal(self):
        """手动建仓可以直接用当天的选股信号（不受 T+1 限制）。"""
        trade_date = date(2026, 11, 3)
        self._add_pick("600016", "民生银行", 3.5, trade_date, rank=1)
        ensure_paper_account(initial_cash=20000, db=self.db)

        payload = _tencent_payload("600016", price=3.52, prev_close=3.5, change_pct=0.57)
        with patch("requests.get", return_value=_FakeResponse(payload)), patch.object(
            paper_bot_service, "_send", lambda *_args, **_kwargs: True
        ):
            stats = paper_bot_service.trade_now(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["filled"]), 1)
        self.assertEqual(stats["filled"][0]["side"], "buy")

    def test_trade_now_ignores_open_gap_threshold(self):
        """手动建仓不套用高开 3% 的机械阈值。"""
        trade_date = date(2026, 11, 4)
        self._add_pick("600016", "民生银行", 3.5, trade_date, rank=1)
        ensure_paper_account(initial_cash=20000, db=self.db)

        payload = _tencent_payload("600016", price=3.71, prev_close=3.5, change_pct=6.0)
        with patch("requests.get", return_value=_FakeResponse(payload)), patch.object(
            paper_bot_service, "_send", lambda *_args, **_kwargs: True
        ):
            stats = paper_bot_service.trade_now(as_of=trade_date, db=self.db)

        self.assertEqual(len(stats["filled"]), 1)

    # ------------------------------------------------------------------
    # 收盘结算
    # ------------------------------------------------------------------

    def test_settle_reports_today_pct(self):
        trade_date = self._prepare_holding()
        # 生成一份当日快照作为基准
        PortfolioService().get_portfolio_snapshot(
            account_id=ensure_paper_account(db=self.db)["id"],
            as_of=trade_date,
            include_realtime=False,
        )

        with patch.object(paper_bot_service, "_send", lambda *_args, **_kwargs: True):
            stats = settle(as_of=trade_date, db=self.db)

        self.assertGreater(stats["equity"], 0)
        self.assertEqual(stats["positions"], 1)
        self.assertIn("buy", stats["trades"])


if __name__ == "__main__":
    unittest.main()
