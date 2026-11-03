# -*- coding: utf-8 -*-
"""全市场日线同步的离线测试（解析、单位换算、去重）。"""

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from src.config import Config
from src.services.cn_tracker import universe_sync
from src.services.cn_tracker.universe_sync import (
    _fetch_daily_rows,
    _insert_rows,
    _recent_trade_dates,
)
from src.storage import DatabaseManager, StockDaily


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class UniverseSyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "universe.db")
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

    def test_recent_trade_dates_returns_requested_count(self):
        dates = _recent_trade_dates(20)
        self.assertEqual(len(dates), 20)
        self.assertLess(dates[0], dates[-1])

    def test_fetch_daily_rows_converts_units(self):
        payload = {
            "code": 0,
            "data": {
                "fields": [
                    "ts_code",
                    "trade_date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "vol",
                    "amount",
                ],
                "items": [
                    ["000001.SZ", "20261030", 11.53, 11.67, 11.40, 11.64, 1864245.07, 2152393.88]
                ],
            },
        }
        with patch(
            "requests.post", return_value=_FakeResponse(payload)
        ) as request:
            rows = _fetch_daily_rows("token", date(2026, 10, 30))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["code"], "000001")
        self.assertAlmostEqual(row["close"], 11.64)
        # vol 手 → 股，amount 千元 → 元
        self.assertAlmostEqual(row["volume"], 1864245.07 * 100, places=1)
        self.assertAlmostEqual(row["amount"], 2152393.88 * 1000, places=1)
        self.assertEqual(request.call_args.kwargs["json"]["api_name"], "daily")

    def test_fetch_daily_rows_rejects_non_trade_day(self):
        payload = {"code": 0, "data": {"fields": ["close"], "items": []}}
        with patch("requests.post", return_value=_FakeResponse(payload)):
            self.assertEqual(_fetch_daily_rows("token", date(2026, 10, 31)), [])

    def test_insert_rows_is_idempotent(self):
        trade_date = date(2026, 10, 30)
        rows = [
            {
                "code": "000001",
                "date": trade_date,
                "open": 11.5,
                "high": 11.6,
                "low": 11.4,
                "close": 11.64,
                "volume": 1000.0,
                "amount": 12000.0,
            }
        ]

        self.assertEqual(_insert_rows(self.db, trade_date, rows), 1)
        # 再插一次不应重复
        self.assertEqual(_insert_rows(self.db, trade_date, rows), 0)

        with self.db.get_session() as session:
            self.assertEqual(session.query(StockDaily).count(), 1)

    def test_sync_requires_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TUSHARE_TOKEN", None)
            stats = universe_sync.sync_universe_daily(token="", db=self.db)
        self.assertEqual(stats["error"], "no_tushare_token")


if __name__ == "__main__":
    unittest.main()
