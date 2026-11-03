# -*- coding: utf-8 -*-
"""全市场日线同步（按交易日批量拉取）。

因子分层回测需要「全市场 × 多日」的日线，而 ``--sync-daily`` 只覆盖跟踪标的
（十几只）。逐只拉 5000+ 只要一小时以上，Tushare ``daily`` 支持按交易日一次
返回当日全市场（约 5500 行），120 个交易日只需 120 次调用。

注意：``daily`` 的 ``vol`` 单位是手、``amount`` 单位是千元，这里统一转成
股与元，与其它数据源保持一致。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from src.storage import DatabaseManager, StockDaily

logger = logging.getLogger(__name__)

_TUSHARE_ENDPOINT = "https://api.tushare.pro"
DEFAULT_UNIVERSE_DAYS = 120
# 调用间隔：避免触发频次限制
REQUEST_INTERVAL_SECONDS = 0.2
BATCH_SIZE = 2000


def _recent_trade_dates(days: int) -> List[date]:
    """最近 N 个交易日（含今日）。"""
    from src.core.trading_calendar import get_trading_dates

    end = date.today()
    start = end - timedelta(days=int(days * 1.8) + 15)
    dates = get_trading_dates("cn", start, end)
    if dates is None or len(dates) == 0:
        # 日历不可用时退化为自然日（仅用于回测，容忍误差）
        return [start + timedelta(days=offset) for offset in range(int(days))]
    return [item.date() if hasattr(item, "date") else item for item in dates][-int(days) :]


def _fetch_daily_rows(token: str, trade_date: date) -> List[Dict[str, Any]]:
    """拉取某个交易日的全市场日线。"""
    import requests

    payload = {
        "api_name": "daily",
        "token": token,
        "params": {"trade_date": trade_date.strftime("%Y%m%d")},
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
    }
    response = requests.post(_TUSHARE_ENDPOINT, json=payload, timeout=40)
    response.raise_for_status()
    body = response.json()
    if body.get("code") not in (0, None):
        raise RuntimeError(str(body.get("msg") or "tushare error"))

    data = body.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    if not items:
        return []

    index = {name: position for position, name in enumerate(fields)}
    try:
        code_at = index["ts_code"]
        open_at = index["open"]
        high_at = index["high"]
        low_at = index["low"]
        close_at = index["close"]
        vol_at = index["vol"]
        amount_at = index["amount"]
    except KeyError as exc:
        raise RuntimeError("unexpected daily fields") from exc

    rows: List[Dict[str, Any]] = []
    for item in items:
        ts_code = str(item[code_at] or "")
        code = ts_code.split(".")[0]
        if len(code) != 6:
            continue
        try:
            close_price = float(item[close_at] or 0)
        except (TypeError, ValueError):
            continue
        if close_price <= 0:
            continue
        rows.append(
            {
                "code": code,
                "date": trade_date,
                "open": float(item[open_at] or 0),
                "high": float(item[high_at] or 0),
                "low": float(item[low_at] or 0),
                "close": close_price,
                # vol 单位手 → 股；amount 单位千元 → 元
                "volume": float(item[vol_at] or 0) * 100,
                "amount": float(item[amount_at] or 0) * 1000,
            }
        )
    return rows


def _insert_rows(
    manager: DatabaseManager, trade_date: date, rows: Sequence[Dict[str, Any]]
) -> int:
    """按 (code, date) 去重后批量插入。"""
    if not rows:
        return 0
    with manager.get_session() as session:
        existing = {
            str(code)
            for (code,) in session.query(StockDaily.code)
            .filter(StockDaily.date == trade_date)
            .all()
        }
    fresh = [row for row in rows if row["code"] not in existing]
    if not fresh:
        return 0

    with manager.get_session() as session:
        for start in range(0, len(fresh), BATCH_SIZE):
            session.bulk_insert_mappings(
                StockDaily, fresh[start : start + BATCH_SIZE]
            )
        session.commit()
    return len(fresh)


def sync_universe_daily(
    *,
    days: int = DEFAULT_UNIVERSE_DAYS,
    db: Optional[DatabaseManager] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """同步最近 N 个交易日的全市场日线。"""
    manager = db or DatabaseManager.get_instance()
    resolved_token = (token or os.getenv("TUSHARE_TOKEN") or "").strip()
    if not resolved_token:
        return {"error": "no_tushare_token", "saved": 0, "dates": 0}

    trade_dates = _recent_trade_dates(days)
    if not trade_dates:
        return {"error": "no_trade_dates", "saved": 0, "dates": 0}

    saved_total = 0
    errors = 0
    empty = 0
    for position, trade_date in enumerate(trade_dates, start=1):
        try:
            rows = _fetch_daily_rows(resolved_token, trade_date)
        except Exception as exc:  # noqa: BLE001 - 单日失败不影响整体
            errors += 1
            logger.warning("全市场日线拉取失败 %s: %s", trade_date, exc)
            continue
        if not rows:
            empty += 1
            continue
        try:
            saved_total += _insert_rows(manager, trade_date, rows)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("全市场日线入库失败 %s: %s", trade_date, exc)
        if position % 20 == 0:
            logger.info(
                "全市场日线同步进度: %s/%s 已入库 %s 行",
                position,
                len(trade_dates),
                saved_total,
            )
        time.sleep(REQUEST_INTERVAL_SECONDS)

    logger.info(
        "全市场日线同步完成: dates=%s saved=%s empty=%s errors=%s",
        len(trade_dates),
        saved_total,
        empty,
        errors,
    )
    return {
        "dates": len(trade_dates),
        "saved": saved_total,
        "empty": empty,
        "errors": errors,
        "range": [trade_dates[0].isoformat(), trade_dates[-1].isoformat()],
    }
