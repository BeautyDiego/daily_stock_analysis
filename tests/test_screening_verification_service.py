# -*- coding: utf-8 -*-
"""Tests for the daily screening pick persistence and T+1 verification service.

Runs against a temporary SQLite DB (same approach as other service tests) and
covers: per-market Top N persistence, idempotency, strict next-session (T+1)
verification semantics, benchmark comparison, dashboard aggregation and the
push report rendering.
"""

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from src.config import Config
from src.services.screening_verification_service import ScreeningVerificationService
from src.storage import DatabaseManager, ScreeningPick


def _frame(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    """Build a daily-history-like DataFrame with date/open/close columns."""
    return pd.DataFrame(
        [{"date": d, "open": o, "close": c} for d, o, c in rows]
    )


class _FakeScreening:
    """Minimal ScreeningService stub returning fixed candidates per market."""

    def __init__(self, config=None):
        self.calls: list[dict] = []

    def strategies(self):
        return {
            "strategies": [
                {"id": "dual_low", "market_scope": ["cn"]},
                {"id": "us_blue_chip_momentum", "market_scope": ["us"]},
            ]
        }

    def screen(self, *, strategy, market, max_results, **kwargs):
        self.calls.append({"strategy": strategy, "market": market})
        prefix = "60" if market == "cn" else "US"
        return {
            "run_id": f"run-{market}",
            "strategy": strategy,
            "candidates": [
                {
                    "rank": index,
                    "code": f"{prefix}{index:04d}",
                    "name": f"{market}-{index}",
                    "price": 10.0 + index,
                    "score": 90.0 - index,
                    "ranking_reason": f"reason-{index}",
                }
                for index in range(1, max_results + 1)
            ],
        }


class _FakeMarketContext:
    """交易日历上下文替身，只暴露 effective_daily_bar_date。"""

    def __init__(self, effective_daily_bar_date):
        self.effective_daily_bar_date = effective_daily_bar_date


class ScreeningVerificationServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_pick_verification.db")
        self._env_path = os.path.join(self._temp_dir.name, ".env")
        with open(self._env_path, "w", encoding="utf-8") as env_file:
            env_file.write("SCREENING_ENABLED=true\nSCREENING_PICKS_TOP_N=5\n")

        self._original_env = {
            key: os.environ.get(key) for key in ("ENV_FILE", "DATABASE_PATH")
        }
        os.environ["ENV_FILE"] = self._env_path
        os.environ["DATABASE_PATH"] = self._db_path

        Config._instance = None
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.service = ScreeningVerificationService(db_manager=self.db)

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
    # 选股落库
    # ------------------------------------------------------------------

    def test_run_daily_picks_persists_top_n_per_market(self) -> None:
        with patch("src.services.screening_service.ScreeningService", _FakeScreening):
            stats = self.service.run_daily_picks(max_results=5)

        self.assertEqual(stats["saved"], 10)
        self.assertEqual(sorted(stats["markets"]), ["cn", "us"])
        self.assertEqual(stats["markets"]["cn"]["saved"], 5)
        self.assertEqual(stats["markets"]["us"]["saved"], 5)

        with self.db.get_session() as session:
            cn_codes = [
                pick.code
                for pick in session.query(ScreeningPick)
                .filter(ScreeningPick.market == "cn")
                .all()
            ]
        self.assertEqual(len(cn_codes), 5)

    def test_run_daily_picks_is_idempotent_for_same_day(self) -> None:
        with patch("src.services.screening_service.ScreeningService", _FakeScreening):
            first = self.service.run_daily_picks(max_results=5)
            second = self.service.run_daily_picks(max_results=5)

        self.assertEqual(first["saved"], 10)
        self.assertEqual(second["saved"], 0)
        self.assertEqual(second["skipped"], 10)

    def test_second_run_with_different_candidates_does_not_accumulate(self) -> None:
        """当天已落库后，重跑选出不同候选也不能叠加，否则会超过 Top N。"""
        calls = {"n": 0}

        class _ShiftingScreening(_FakeScreening):
            def screen(self, *, strategy, market, max_results, **kwargs):
                calls["n"] += 1
                payload = super().screen(
                    strategy=strategy, market=market, max_results=max_results, **kwargs
                )
                for item in payload["candidates"]:
                    item["code"] = f"{item['code']}{calls['n']}"
                return payload

        with patch("src.services.screening_service.ScreeningService", _ShiftingScreening):
            first = self.service.run_daily_picks(max_results=5)
            second = self.service.run_daily_picks(max_results=5)

        self.assertEqual(first["saved"], 10)
        self.assertEqual(second["saved"], 0)
        self.assertEqual(second["skipped"], 10)

        with self.db.get_session() as session:
            total = session.query(ScreeningPick).count()
            cn_codes = [
                pick.code
                for pick in session.query(ScreeningPick)
                .filter(ScreeningPick.market == "cn")
                .all()
            ]

        self.assertEqual(total, 10)
        # 只保留第一轮的候选（后缀为第一轮的调用序号）
        self.assertTrue(all(code.endswith("1") for code in cn_codes), cn_codes)

    def test_us_pick_date_uses_last_closed_us_session(self) -> None:
        """美股选股日必须等于取价日（上一个已完成的美股交易日）。"""
        from src.services import screening_verification_service as service_module

        with patch.object(
            service_module,
            "build_market_phase_context",
            lambda market="cn": _FakeMarketContext(date(2026, 10, 7)),
        ):
            resolved = service_module._resolve_pick_date(
                "us", default_date=date(2026, 10, 8)
            )

        self.assertEqual(resolved, date(2026, 10, 7))

    def test_cn_pick_date_keeps_default_date(self) -> None:
        from src.services import screening_verification_service as service_module

        resolved = service_module._resolve_pick_date(
            "cn", default_date=date(2026, 10, 8)
        )

        self.assertEqual(resolved, date(2026, 10, 8))

    def test_us_pick_date_falls_back_when_calendar_unavailable(self) -> None:
        from src.services import screening_verification_service as service_module

        def _boom(market="cn"):
            raise RuntimeError("calendar unavailable")

        with patch.object(service_module, "build_market_phase_context", _boom):
            resolved = service_module._resolve_pick_date(
                "us", default_date=date(2026, 10, 8)
            )

        self.assertEqual(resolved, date(2026, 10, 8))

    def test_run_daily_picks_persists_us_with_resolved_pick_date(self) -> None:
        """落库的美股记录应使用美股选股日，而不是北京日期。"""
        from src.services import screening_verification_service as service_module

        with patch.object(
            service_module,
            "build_market_phase_context",
            lambda market="cn": _FakeMarketContext(date(2026, 10, 7)),
        ), patch("src.services.screening_service.ScreeningService", _FakeScreening):
            stats = self.service.run_daily_picks(markets=["us"], max_results=2)

        self.assertEqual(stats["markets"]["us"]["pick_date"], "2026-10-07")
        with self.db.get_session() as session:
            pick = session.query(ScreeningPick).first()
        self.assertIsNotNone(pick)
        self.assertEqual(pick.pick_date, date(2026, 10, 7))

    def test_default_strategy_is_chosen_by_market_scope(self) -> None:
        with patch("src.services.screening_service.ScreeningService", _FakeScreening):
            self.service.run_daily_picks(markets=["us"], max_results=2)

        with self.db.get_session() as session:
            pick = (
                session.query(ScreeningPick)
                .filter(ScreeningPick.market == "us")
                .first()
            )
        # 美股必须使用美股策略，否则会被选股管线拒绝
        self.assertEqual(pick.strategy, "us_blue_chip_momentum")

    def test_single_market_failure_does_not_block_other_markets(self) -> None:
        class _BrokenUS(_FakeScreening):
            def screen(self, *, strategy, market, max_results, **kwargs):
                if market == "us":
                    raise RuntimeError("yfinance unreachable")
                return super().screen(
                    strategy=strategy, market=market, max_results=max_results
                )

        with patch("src.services.screening_service.ScreeningService", _BrokenUS):
            stats = self.service.run_daily_picks(max_results=5)

        self.assertEqual(stats["saved"], 5)
        self.assertIsNotNone(stats["markets"]["us"]["error"])
        self.assertEqual(len(stats["errors"]), 1)

    # ------------------------------------------------------------------
    # T+1 验证
    # ------------------------------------------------------------------

    def _seed_pick(self, code: str, market: str, pick_date: date, entry: float) -> ScreeningPick:
        with self.db.get_session() as session:
            pick = ScreeningPick(
                run_id=f"run-{market}",
                pick_date=pick_date,
                market=market,
                code=code,
                name=f"{market}-{code}",
                rank=1,
                entry_price=entry,
            )
            session.add(pick)
            session.commit()
            session.refresh(pick)
        return pick

    def test_verification_uses_strictly_next_session(self) -> None:
        self._seed_pick("600001", "cn", date(2024, 5, 10), 100.0)
        history = _frame(
            [
                ("2024-05-09", 99.0, 99.0),
                ("2024-05-10", 100.0, 100.0),
                ("2024-05-13", 101.0, 110.0),  # T+1（跳过周末）
                ("2024-05-14", 110.0, 120.0),
            ]
        )

        def _fake_fetch(code, *, lookback_days=120, source="auto", **kwargs):
            return history

        with patch("src.services.screening.daily.fetch_daily_history", _fake_fetch):
            stats = self.service.verify_picks()

        self.assertEqual(stats["done"], 1)
        dashboard = self.service.get_dashboard()
        pick_view = dashboard["days"][0]["markets"]["cn"]["picks"][0]
        verification = pick_view["verification"]

        self.assertEqual(verification["verify_date"], "2024-05-13")
        self.assertAlmostEqual(verification["return_pct"], 10.0, places=4)

    def test_verification_is_insufficient_before_next_session(self) -> None:
        self._seed_pick("600001", "cn", date(2024, 5, 10), 100.0)
        history = _frame([("2024-05-09", 99.0, 99.0), ("2024-05-10", 100.0, 100.0)])

        def _fake_fetch(code, *, lookback_days=120, source="auto", **kwargs):
            return history

        with patch("src.services.screening.daily.fetch_daily_history", _fake_fetch):
            stats = self.service.verify_picks()

        self.assertEqual(stats["insufficient"], 1)
        self.assertEqual(stats["done"], 0)
        # 未产生 T+1 K 线时不写库，下次运行会重试
        with self.db.get_session() as session:
            from src.storage import ScreeningVerification

            self.assertEqual(session.query(ScreeningVerification).count(), 0)

    def test_verification_compares_against_benchmark(self) -> None:
        self._seed_pick("600001", "cn", date(2024, 5, 10), 100.0)

        def _fake_fetch(code, *, lookback_days=120, source="auto", **kwargs):
            if code == "600001":
                return _frame([("2024-05-10", 100.0, 100.0), ("2024-05-13", 100.0, 103.0)])
            # 基准：开盘 1000 -> 收盘 1010（+1%）
            return _frame([("2024-05-10", 1000.0, 1000.0), ("2024-05-13", 1000.0, 1010.0)])

        with patch("src.services.screening.daily.fetch_daily_history", _fake_fetch):
            self.service.verify_picks()

        dashboard = self.service.get_dashboard()
        verification = dashboard["days"][0]["markets"]["cn"]["picks"][0]["verification"]
        self.assertAlmostEqual(verification["return_pct"], 3.0, places=4)
        self.assertAlmostEqual(verification["benchmark_return_pct"], 1.0, places=4)
        self.assertAlmostEqual(verification["excess_return_pct"], 2.0, places=4)
        self.assertTrue(verification["is_up"])
        self.assertTrue(verification["beat_benchmark"])
        self.assertTrue(verification["met_expectation"])

    def test_verification_falls_back_to_up_only_without_benchmark(self) -> None:
        self._seed_pick("600001", "cn", date(2024, 5, 10), 100.0)

        def _fake_fetch(code, *, lookback_days=120, source="auto", **kwargs):
            if code == "600001":
                return _frame([("2024-05-10", 100.0, 100.0), ("2024-05-13", 100.0, 105.0)])
            return _frame([])

        with patch("src.services.screening.daily.fetch_daily_history", _fake_fetch):
            self.service.verify_picks()

        dashboard = self.service.get_dashboard()
        verification = dashboard["days"][0]["markets"]["cn"]["picks"][0]["verification"]
        self.assertIsNone(verification["benchmark_return_pct"])
        self.assertTrue(verification["met_expectation"])
        self.assertEqual(verification["error_message"], "基准数据缺失，仅按上涨判定")

    def test_verify_picks_skips_already_verified(self) -> None:
        self._seed_pick("600001", "cn", date(2024, 5, 10), 100.0)

        def _fake_fetch(code, *, lookback_days=120, source="auto", **kwargs):
            return _frame([("2024-05-10", 100.0, 100.0), ("2024-05-13", 100.0, 110.0)])

        with patch("src.services.screening.daily.fetch_daily_history", _fake_fetch):
            first = self.service.verify_picks()
            second = self.service.verify_picks()

        self.assertEqual(first["done"], 1)
        self.assertEqual(second["processed"], 0)

    # ------------------------------------------------------------------
    # 看板聚合
    # ------------------------------------------------------------------

    def test_dashboard_summary_reports_return_and_win_rates(self) -> None:
        with patch("src.services.screening_service.ScreeningService", _FakeScreening):
            self.service.run_daily_picks(max_results=2)

        def _fake_fetch(code, *, lookback_days=120, source="auto", **kwargs):
            if code in ("sh000001", "^GSPC"):
                # 基准：开盘 1000 -> 收盘 1010（+1%）
                return _frame([("2024-05-10", 1000.0, 1000.0), ("2024-05-13", 1000.0, 1010.0)])
            # 个股 +10%
            return _frame([("2024-05-10", 100.0, 100.0), ("2024-05-13", 100.0, 110.0)])

        with self.db.get_session() as session:
            for pick in session.query(ScreeningPick).all():
                pick.pick_date = date(2024, 5, 10)
                pick.entry_price = 100.0
            session.commit()

        with patch("src.services.screening.daily.fetch_daily_history", _fake_fetch):
            self.service.verify_picks()

        dashboard = self.service.get_dashboard()
        summary = dashboard["summary"]
        self.assertEqual(summary["total_picks"], 4)
        self.assertEqual(summary["verified_picks"], 4)
        self.assertEqual(summary["hit_rate_pct"], 100.0)
        self.assertEqual(summary["win_rate_pct"], 100.0)
        self.assertAlmostEqual(summary["avg_return_pct"], 10.0, places=2)
        self.assertAlmostEqual(summary["avg_excess_return_pct"], 9.0, places=2)

    # ------------------------------------------------------------------
    # 推送正文
    # ------------------------------------------------------------------

    def test_build_picks_report_lists_both_markets(self) -> None:
        report_date = date(2026, 10, 8)
        with patch("src.services.screening_service.ScreeningService", _FakeScreening):
            self.service.run_daily_picks(max_results=2, pick_date=report_date)

        content = self.service.build_picks_report(report_date)
        self.assertIn("每日选股", content)
        self.assertIn("【A股】", content)
        self.assertIn("【美股】", content)

    def test_notify_daily_picks_covers_both_market_dates(self) -> None:
        """A 股与美股选股日不同天时，推送正文要同时包含两个市场。"""
        from src.services import screening_verification_service as service_module

        with patch.object(
            service_module,
            "build_market_phase_context",
            lambda market="cn": _FakeMarketContext(date(2026, 10, 7)),
        ), patch("src.services.screening_service.ScreeningService", _FakeScreening):
            self.service.run_daily_picks(max_results=2)

        sent: list[str] = []

        def _capture(content: str) -> bool:
            sent.append(content)
            return True

        with patch.object(self.service, "_send", _capture):
            ok = self.service.notify_daily_picks()

        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertIn("【A股】", sent[0])
        self.assertIn("【美股】", sent[0])

    def test_build_verification_report_includes_hit_summary(self) -> None:
        with patch("src.services.screening_service.ScreeningService", _FakeScreening):
            self.service.run_daily_picks(max_results=2)

        with self.db.get_session() as session:
            for pick in session.query(ScreeningPick).all():
                pick.pick_date = date(2024, 5, 10)
                pick.entry_price = 100.0
            session.commit()

        def _fake_fetch(code, *, lookback_days=120, source="auto", **kwargs):
            if code in ("sh000001", "^GSPC"):
                return _frame([("2024-05-10", 1000.0, 1000.0), ("2024-05-13", 1000.0, 1005.0)])
            return _frame([("2024-05-10", 100.0, 100.0), ("2024-05-13", 100.0, 110.0)])

        with patch("src.services.screening.daily.fetch_daily_history", _fake_fetch):
            self.service.verify_picks()

        dashboard = self.service.get_dashboard()
        content = ScreeningVerificationService.build_verification_report(
            dashboard["days"]
        )
        self.assertIn("选股次日验证", content)
        self.assertIn("符合预期 4/4", content)

    def test_notify_is_skipped_when_disabled(self) -> None:
        config = Config.get_instance()
        config.screening_picks_notify_enabled = False
        service = ScreeningVerificationService(config=config, db_manager=self.db)
        self.assertFalse(service._send("anything"))


if __name__ == "__main__":
    unittest.main()
