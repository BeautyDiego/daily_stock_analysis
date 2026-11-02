# -*- coding: utf-8 -*-
"""A 股选股的跨日去重与行业分散。

现有系统只保证「同一天同一市场同一代码」不重复；本模块补上跨日维度：
最近 N 个选股日推荐过的代码进入冷却期，同时限制单一行业占比，避免每天
推荐高度重叠的名单。
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.storage import DatabaseManager, ScreeningPick, TrackedPosition

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_DAYS = 10
DEFAULT_MAX_PER_INDUSTRY = 2
# 冷却豁免：正向政策足够强时允许同一标的重新入选
DEFAULT_EXEMPT_POLICY_DELTA = 5.0
ACTIVE_TRACKING_STATUSES = ("watching", "holding", "reduced")


def recent_recommended_codes(
    *,
    db: Optional[DatabaseManager] = None,
    days: int = DEFAULT_COOLDOWN_DAYS,
    as_of: Optional[date] = None,
    market: str = "cn",
) -> Dict[str, date]:
    """返回最近 ``days`` 个选股日内推荐过的代码及其最近推荐日。

    以「实际产生过选股记录的日期」为准，而不是自然日，避免长假后冷却期失真。
    """
    manager = db or DatabaseManager.get_instance()
    end = as_of or date.today()
    lookback = max(1, int(days))

    with manager.get_session() as session:
        recent_dates = [
            row[0]
            for row in (
                session.query(ScreeningPick.pick_date)
                .filter(
                    ScreeningPick.market == market,
                    ScreeningPick.pick_date < end,
                )
                .distinct()
                .order_by(ScreeningPick.pick_date.desc())
                .limit(lookback)
                .all()
            )
            if row[0] is not None
        ]
        if not recent_dates:
            return {}

        start = min(recent_dates)
        rows = (
            session.query(ScreeningPick.code, ScreeningPick.pick_date)
            .filter(
                ScreeningPick.market == market,
                ScreeningPick.pick_date >= start,
                ScreeningPick.pick_date < end,
            )
            .all()
        )

    result: Dict[str, date] = {}
    for code, pick_date in rows:
        if not code or pick_date is None:
            continue
        key = str(code)
        if key not in result or pick_date > result[key]:
            result[key] = pick_date
    return result


def active_tracking_codes(
    *,
    db: Optional[DatabaseManager] = None,
    market: str = "cn",
) -> set[str]:
    """返回正在跟踪（含已持仓）的代码集合。"""
    manager = db or DatabaseManager.get_instance()
    with manager.get_session() as session:
        rows = (
            session.query(TrackedPosition.code)
            .filter(
                TrackedPosition.market == market,
                TrackedPosition.status.in_(ACTIVE_TRACKING_STATUSES),
            )
            .all()
        )
    return {str(row[0]) for row in rows if row[0]}


def filter_candidates(
    candidates: Sequence[Dict[str, Any]],
    *,
    db: Optional[DatabaseManager] = None,
    as_of: Optional[date] = None,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    max_per_industry: int = DEFAULT_MAX_PER_INDUSTRY,
    limit: int = 5,
    exempt_policy_delta: float = DEFAULT_EXEMPT_POLICY_DELTA,
    market: str = "cn",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """按冷却窗与行业上限过滤候选，返回 (保留列表, 统计)。

    冷却豁免条件：本条候选的正向政策加成 ``>= exempt_policy_delta``。
    豁免会在候选上写入 ``dedupe_note``，便于事后解释「为什么又推荐了它」。
    """
    manager = db or DatabaseManager.get_instance()
    cooldown = recent_recommended_codes(
        db=manager, days=cooldown_days, as_of=as_of, market=market
    )
    tracked = active_tracking_codes(db=manager, market=market)

    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    industry_counts: Counter[str] = Counter()
    exempted = 0
    # 豁免名额有限：否则同一行业的多只标的会因共享同一条政策而集体绕过冷却
    exemption_limit = max(1, int(limit) // 2)

    for item in candidates:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        industry = str(item.get("industry") or "").strip()

        if code in tracked:
            dropped.append({"code": code, "reason": "already_tracked"})
            continue

        if code in cooldown:
            delta = _as_float(item.get("policy_delta"))
            if delta >= exempt_policy_delta and exempted < exemption_limit:
                exempted += 1
                item["dedupe_note"] = (
                    f"cooldown_exempt(policy_delta={delta:+.1f},"
                    f"last_pick={cooldown[code].isoformat()})"
                )
            else:
                dropped.append(
                    {
                        "code": code,
                        "reason": "cooldown",
                        "last_pick": cooldown[code].isoformat(),
                    }
                )
                continue
        else:
            item.setdefault("dedupe_note", "cooldown_ok")

        if industry and industry_counts[industry] >= max(1, int(max_per_industry)):
            dropped.append(
                {"code": code, "reason": "industry_cap", "industry": industry}
            )
            continue

        if industry:
            industry_counts[industry] += 1
        kept.append(item)

    target = max(1, int(limit))
    stats = {
        "input": len(candidates),
        "kept": len(kept),
        "dropped": len(dropped),
        "dropped_detail": dropped,
        "cooldown_exempted": exempted,
        "cooldown_days": cooldown_days,
        "max_per_industry": max_per_industry,
    }
    logger.info(
        "选股去重: input=%s kept=%s dropped=%s exempted=%s",
        stats["input"],
        stats["kept"],
        stats["dropped"],
        stats["cooldown_exempted"],
    )
    return kept[:target], stats


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
