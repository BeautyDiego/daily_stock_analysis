# -*- coding: utf-8 -*-
"""A 股政策采集、去重过滤与持仓复盘的离线测试。"""

import os
import tempfile
import unittest
from datetime import date, timedelta

import pandas as pd

from src.config import Config
from src.services.cn_tracker.dedupe_filter import (
    filter_candidates,
    recent_recommended_codes,
)
from src.services.cn_tracker.holding_tracker_service import (
    _score_review,
    build_review_report,
    ensure_tracked_positions,
    review_holdings,
    set_position_status,
)
from src.services.cn_tracker.industry import fill_missing_industries, infer_industry
from src.services.cn_tracker.macro_policy_service import (
    _detect_direction,
    _detect_level,
    _detect_sectors,
    _sector_matches,
    policy_delta,
)
from src.storage import DatabaseManager, ScreeningPick, TrackedPosition


def _frame(closes, volumes=None) -> pd.DataFrame:
    volumes = volumes or [1_000_000] * len(closes)
    dates = pd.date_range("2026-09-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": volumes,
        }
    )


class IndustryInferenceTest(unittest.TestCase):
    def test_bank_name_inferred(self):
        self.assertEqual(infer_industry("600919", "江苏银行"), "银行")

    def test_construction_name_inferred(self):
        self.assertEqual(infer_industry("601668", "中国建筑"), "建筑装饰")

    def test_unknown_name_returns_empty(self):
        self.assertEqual(infer_industry("000000", "某某股份"), "")

    def test_fill_only_missing_fields(self):
        items = [
            {"code": "600919", "name": "江苏银行"},
            {"code": "601668", "name": "中国建筑", "industry": "自定义"},
        ]
        fill_missing_industries(items)
        self.assertEqual(items[0]["industry"], "银行")
        self.assertEqual(items[1]["industry"], "自定义")


class _DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "cn_tracker_test.db")
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


class PolicyClassificationTest(unittest.TestCase):
    def test_detects_sector_from_keywords(self):
        sectors = _detect_sectors("央行宣布降准 支持银行普惠金融")
        self.assertIn("银行", sectors)

    def test_direction_prefers_negative_on_conflict(self):
        self.assertEqual(_detect_direction("加大支持力度"), "positive")
        self.assertEqual(_detect_direction("严控风险 限制新增产能"), "negative")
        self.assertEqual(_detect_direction("召开例行会议"), "neutral")

    def test_level_detection(self):
        self.assertEqual(_detect_level("国务院常务会议部署"), "central")
        self.assertEqual(_detect_level("发改委发布通知"), "ministry")
        self.assertEqual(_detect_level("某省出台措施"), "local")

    def test_policy_delta_clamped(self):
        positive = [{"direction": "positive", "strength": 5}] * 4
        negative = [{"direction": "negative", "strength": 5}] * 4
        self.assertEqual(policy_delta(positive), 10.0)
        self.assertEqual(policy_delta(negative), -10.0)
        self.assertEqual(policy_delta([{"direction": "neutral", "strength": 5}]), 0.0)

    def test_sector_matches_both_directions(self):
        self.assertTrue(_sector_matches("银行", ["银行"]))
        self.assertTrue(_sector_matches("银行II", ["银行"]))
        self.assertFalse(_sector_matches("煤炭", ["银行"]))
        self.assertFalse(_sector_matches("", ["银行"]))


class DedupeFilterTest(_DbTestCase):
    def _add_pick(self, code: str, pick_date: date) -> None:
        with self.db.get_session() as session:
            session.add(
                ScreeningPick(
                    run_id=f"run-{code}-{pick_date}",
                    pick_date=pick_date,
                    market="cn",
                    code=code,
                    name=code,
                    rank=1,
                )
            )
            session.commit()

    def test_cooldown_blocks_recent_code(self):
        today = date(2026, 11, 2)
        self._add_pick("600016", today - timedelta(days=3))

        cooldown = recent_recommended_codes(db=self.db, days=10, as_of=today)
        self.assertIn("600016", cooldown)

        kept, stats = filter_candidates(
            [{"code": "600016", "industry": "银行"}],
            db=self.db,
            as_of=today,
            limit=5,
        )
        self.assertEqual(kept, [])
        self.assertEqual(stats["dropped"], 1)

    def test_cooldown_exempted_by_strong_policy(self):
        today = date(2026, 11, 2)
        self._add_pick("600016", today - timedelta(days=3))

        kept, _ = filter_candidates(
            [{"code": "600016", "industry": "银行", "policy_delta": 5.0}],
            db=self.db,
            as_of=today,
            limit=5,
        )
        self.assertEqual(len(kept), 1)
        self.assertIn("cooldown_exempt", kept[0]["dedupe_note"])

    def test_exemption_is_capped(self):
        """同一行业共享一条政策时，不能让整批标的绕过冷却。"""
        today = date(2026, 11, 2)
        codes = ["600001", "600002", "600003", "600004", "600005"]
        for code in codes:
            self._add_pick(code, today - timedelta(days=2))

        candidates = [
            {"code": code, "industry": "银行", "policy_delta": 5.0} for code in codes
        ]
        kept, stats = filter_candidates(
            candidates,
            db=self.db,
            as_of=today,
            limit=5,
            max_per_industry=5,
        )
        # 豁免名额 = max(1, limit // 2) = 2
        self.assertEqual(stats["cooldown_exempted"], 2)
        self.assertEqual(len(kept), 2)

    def test_industry_cap(self):
        today = date(2026, 11, 2)
        candidates = [
            {"code": "600001", "industry": "银行"},
            {"code": "600002", "industry": "银行"},
            {"code": "600003", "industry": "银行"},
            {"code": "600004", "industry": "煤炭"},
        ]
        kept, stats = filter_candidates(
            candidates, db=self.db, as_of=today, limit=5, max_per_industry=2
        )
        codes = [item["code"] for item in kept]
        self.assertEqual(codes, ["600001", "600002", "600004"])
        self.assertEqual(stats["dropped"], 1)

    def test_active_tracking_is_excluded(self):
        today = date(2026, 11, 2)
        with self.db.get_session() as session:
            session.add(
                TrackedPosition(
                    code="600016",
                    name="民生银行",
                    market="cn",
                    source_date=today - timedelta(days=2),
                    status="holding",
                )
            )
            session.commit()

        kept, _ = filter_candidates(
            [{"code": "600016", "industry": "银行"}],
            db=self.db,
            as_of=today,
            limit=5,
        )
        self.assertEqual(kept, [])


class HoldingReviewTest(_DbTestCase):
    def _position(self, **overrides):
        base = {
            "id": None,
            "code": "600016",
            "name": "民生银行",
            "industry": "银行",
            "source_date": date(2026, 10, 30),
            "entry_price": 10.0,
            "status": "watching",
        }
        base.update(overrides)
        return base

    def test_uptrend_scores_hold(self):
        closes = [10.0 + index * 0.1 for index in range(20)]
        review = _score_review(
            position=self._position(),
            history=_frame(closes),
            benchmark=_frame([3000.0] * 20),
            review_date=date(2026, 11, 2),
            db=self.db,
        )
        self.assertEqual(review["advice"], "hold")
        self.assertGreaterEqual(review["scores"]["total"], 70)

    def test_hard_stop_loss_forces_exit(self):
        closes = [12.0] * 10 + [10.0] * 10  # 相对入场 10 元实际为 +0，改用高成本触发
        review = _score_review(
            position=self._position(entry_price=12.0),
            history=_frame(closes),
            benchmark=_frame([3000.0] * 20),
            review_date=date(2026, 11, 2),
            db=self.db,
        )
        self.assertTrue(review["hard_exit"])
        self.assertEqual(review["advice"], "exit")
        self.assertLess(review["return_pct"], -8)

    def test_missing_history_degrades_to_reduce(self):
        review = _score_review(
            position=self._position(),
            history=None,
            benchmark=None,
            review_date=date(2026, 11, 2),
            db=self.db,
        )
        self.assertEqual(review["advice"], "reduce")
        self.assertEqual(review["scores"]["total"], 0.0)

    def test_review_report_renders_advice(self):
        content = build_review_report(
            [
                {
                    "code": "600016",
                    "name": "民生银行",
                    "advice": "hold",
                    "return_pct": 1.23,
                    "scores": {"total": 85},
                    "reasons": ["站上均线"],
                }
            ],
            review_date=date(2026, 11, 2),
        )
        self.assertIn("民生银行", content)
        self.assertIn("继续持有", content)
        self.assertIn("85", content)

    def test_ensure_positions_is_idempotent(self):
        picks = [{"code": "600016", "name": "民生银行", "industry": "银行", "price": 3.56}]
        pick_date = date(2026, 11, 2)
        first = ensure_tracked_positions(picks, pick_date=pick_date, db=self.db)
        second = ensure_tracked_positions(picks, pick_date=pick_date, db=self.db)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        with self.db.get_session() as session:
            self.assertEqual(session.query(TrackedPosition).count(), 1)

    def test_ensure_positions_does_not_resurrect_closed(self):
        pick_date = date(2026, 11, 2)
        ensure_tracked_positions(
            [{"code": "600016", "name": "民生银行", "price": 3.56}],
            pick_date=pick_date,
            db=self.db,
        )
        self.assertTrue(set_position_status("600016", "closed", exit_price=3.6, db=self.db))

        created = ensure_tracked_positions(
            [{"code": "600016", "name": "民生银行", "price": 3.7}],
            pick_date=pick_date + timedelta(days=1),
            db=self.db,
        )
        self.assertEqual(created, 1)
        with self.db.get_session() as session:
            active = (
                session.query(TrackedPosition)
                .filter(TrackedPosition.status.in_(("watching", "holding", "reduced")))
                .count()
            )
        self.assertEqual(active, 1)

    def test_review_holdings_without_positions(self):
        stats = review_holdings(as_of=date(2026, 11, 2), db=self.db)
        self.assertEqual(stats["positions"], 0)
        self.assertEqual(stats["reviews"], [])


if __name__ == "__main__":
    unittest.main()
