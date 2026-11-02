# -*- coding: utf-8 -*-
"""A 股跟踪标的状态机与每日复盘。

流程：
1. 每日选股落库后，把入选标的写入 ``tracked_positions``（status=watching）；
2. 每个交易日盘前对活跃跟踪标的做一次评分卡复盘，写入 ``holding_reviews``，
   并给出 hold / reduce / exit 三档建议；
3. 建议只作为提示，不自动改变持仓状态——实际建仓/了结由用户标记。

本模块只处理 A 股（``market="cn"``）。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from src.services.cn_tracker.macro_policy_service import recent_policy_hits
from src.storage import (
    DatabaseManager,
    HoldingReview,
    ScreeningPick,
    TrackedPosition,
)

logger = logging.getLogger(__name__)

BENCHMARK_CODE = "sh000001"
ACTIVE_STATUSES = ("watching", "holding", "reduced")

# 三档阈值
ADVICE_HOLD_SCORE = 70.0
ADVICE_REDUCE_SCORE = 45.0

# 硬性离场
HARD_STOP_LOSS_PCT = -8.0
TIME_STOP_DAYS = 10
TIME_STOP_EXCESS_PCT = -3.0
BREAKOUT_VOLUME_MULTIPLE = 1.5
NEGATIVE_POLICY_MIN_STRENGTH = 4

_MIN_BARS = 6


def ensure_tracked_positions(
    picks: Sequence[Dict[str, Any]],
    *,
    pick_date: date,
    db: Optional[DatabaseManager] = None,
    market: str = "cn",
) -> int:
    """为本次入选标的建立跟踪记录，返回新建条数。

    已存在活跃记录的代码只更新来源信息，不重复创建。
    """
    manager = db or DatabaseManager.get_instance()
    created = 0
    with manager.get_session() as session:
        for item in picks:
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            existing = (
                session.query(TrackedPosition)
                .filter(
                    TrackedPosition.code == code,
                    TrackedPosition.market == market,
                    TrackedPosition.status.in_(ACTIVE_STATUSES),
                )
                .first()
            )
            if existing is not None:
                existing.source_date = pick_date
                existing.name = str(item.get("name") or existing.name or "")
                existing.industry = str(item.get("industry") or existing.industry or "")
                continue
            session.add(
                TrackedPosition(
                    code=code,
                    name=str(item.get("name") or ""),
                    market=market,
                    industry=str(item.get("industry") or ""),
                    source_pick_id=_as_int(item.get("pick_id")),
                    source_date=pick_date,
                    entry_price=_as_float(item.get("price") or item.get("entry_price")),
                    status="watching",
                )
            )
            created += 1
        session.commit()
    logger.info("建立跟踪标的: created=%s total=%s", created, len(picks))
    return created


def review_holdings(
    *,
    as_of: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
    market: str = "cn",
) -> Dict[str, Any]:
    """对全部活跃跟踪标的做一次复盘，返回统计与明细。"""
    manager = db or DatabaseManager.get_instance()
    review_date = as_of or date.today()

    with manager.get_session() as session:
        positions = (
            session.query(TrackedPosition)
            .filter(
                TrackedPosition.market == market,
                TrackedPosition.status.in_(ACTIVE_STATUSES),
            )
            .order_by(TrackedPosition.code)
            .all()
        )
        position_rows = [_position_snapshot(row) for row in positions]

    if not position_rows:
        logger.info("持仓复盘：没有活跃跟踪标的")
        return {
            "review_date": review_date.isoformat(),
            "positions": 0,
            "reviews": [],
            "advice_counts": {},
            "errors": 0,
        }

    benchmark = _load_history(BENCHMARK_CODE)
    # 已持仓标的用模拟盘实际成本，未持仓的仍用推荐价
    cost_map = _load_paper_costs(manager, market=market)
    reviews: List[Dict[str, Any]] = []
    errors = 0

    for row in position_rows:
        try:
            history = _load_history(row["code"])
            review = _score_review(
                position=row,
                history=history,
                benchmark=benchmark,
                review_date=review_date,
                db=manager,
                cost_map=cost_map,
            )
            reviews.append(review)
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
            errors += 1
            logger.warning("持仓复盘失败 code=%s: %s", row["code"], exc)

    _persist_reviews(manager, reviews)

    counts: Dict[str, int] = {}
    for review in reviews:
        counts[review["advice"]] = counts.get(review["advice"], 0) + 1

    logger.info(
        "持仓复盘完成: positions=%s reviews=%s errors=%s advice=%s",
        len(position_rows),
        len(reviews),
        errors,
        counts,
    )
    return {
        "review_date": review_date.isoformat(),
        "positions": len(position_rows),
        "reviews": reviews,
        "advice_counts": counts,
        "errors": errors,
    }


def set_position_status(
    code: str,
    status: str,
    *,
    entry_price: Optional[float] = None,
    quantity: Optional[float] = None,
    exit_price: Optional[float] = None,
    exit_date: Optional[date] = None,
    exit_reason: str = "",
    db: Optional[DatabaseManager] = None,
    market: str = "cn",
) -> bool:
    """人工标记跟踪状态（holding / reduced / closed）。

    ``exit_date`` 用于补记历史交割；不传则取当天。
    """
    normalized = str(status or "").strip().lower()
    if normalized not in ("watching", "holding", "reduced", "closed"):
        raise ValueError(f"Unsupported position status: {status}")

    manager = db or DatabaseManager.get_instance()
    with manager.get_session() as session:
        row = (
            session.query(TrackedPosition)
            .filter(
                TrackedPosition.code == str(code).strip(),
                TrackedPosition.market == market,
                TrackedPosition.status.in_(ACTIVE_STATUSES),
            )
            .order_by(TrackedPosition.id.desc())
            .first()
        )
        if row is None:
            return False
        row.status = normalized
        if entry_price is not None:
            row.entry_price = float(entry_price)
            row.entry_date = row.entry_date or date.today()
        if quantity is not None:
            row.quantity = float(quantity)
        if normalized == "closed":
            row.exit_date = exit_date or date.today()
            row.exit_price = float(exit_price) if exit_price is not None else row.entry_price
            row.exit_reason = str(exit_reason or "")[:32] or None
        session.commit()
    return True


def sync_daily_history(
    *,
    codes: Optional[Sequence[str]] = None,
    lookback_days: int = 120,
    db: Optional[DatabaseManager] = None,
    market: str = "cn",
) -> Dict[str, Any]:
    """把跟踪标的与近期推荐标的的日线落库，供复盘与回测复用。"""
    manager = db or DatabaseManager.get_instance()
    targets: List[str] = [str(code) for code in (codes or []) if str(code).strip()]

    if not targets:
        with manager.get_session() as session:
            tracked = [
                row[0]
                for row in session.query(TrackedPosition.code)
                .filter(
                    TrackedPosition.market == market,
                    TrackedPosition.status.in_(ACTIVE_STATUSES),
                )
                .all()
            ]
            recent = [
                row[0]
                for row in session.query(ScreeningPick.code)
                .filter(ScreeningPick.market == market)
                .order_by(ScreeningPick.pick_date.desc())
                .limit(50)
                .all()
            ]
        targets = [code for code in dict.fromkeys(tracked + recent) if code]

    saved = 0
    errors = 0
    for code in targets:
        frame = _load_history(code, lookback_days=lookback_days)
        if frame is None or frame.empty:
            errors += 1
            continue
        payload = frame.copy()
        payload["date"] = payload["date"].dt.date if hasattr(payload["date"], "dt") else payload["date"]
        columns = ["date", "open", "high", "low", "close", "volume"]
        if "amount" in payload.columns:
            columns.append("amount")
        # pct_chg 是一字板判定的方向依据；数据源不提供时按收盘价自行计算，
        # 否则 save_daily_data 会写 NULL，模拟盘就无法分辨涨停还是跌停。
        if "pct_chg" not in payload.columns:
            payload["pct_chg"] = (
                pd.to_numeric(payload["close"], errors="coerce").pct_change() * 100
            ).round(2)
        columns.append("pct_chg")
        try:
            manager.save_daily_data(
                payload[columns], code=code, data_source="cn_tracker"
            )
            saved += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("日线落库失败 code=%s: %s", code, exc)

    # 顺带刷新行业映射（默认 7 天内复用，不重复拉取 Tushare）
    from src.services.cn_tracker.industry import sync_industry_map

    industry_stats = sync_industry_map()

    logger.info(
        "日线同步完成: targets=%s saved=%s errors=%s industry=%s",
        len(targets),
        saved,
        errors,
        industry_stats.get("reason"),
    )
    return {
        "targets": len(targets),
        "saved": saved,
        "errors": errors,
        "industry": industry_stats,
    }


def build_review_report(reviews: Sequence[Dict[str, Any]], *, review_date: date) -> str:
    """把复盘结果渲染为 Markdown 推送正文。"""
    if not reviews:
        return ""
    title_map = {"hold": "继续持有", "reduce": "减仓观察", "exit": "建议离场"}
    lines = [f"持仓复盘 · {review_date.isoformat()}", ""]
    for item in reviews:
        advice = str(item.get("advice") or "reduce")
        label = title_map.get(advice, advice)
        score = item.get("scores", {}).get("total")
        score_text = f"{score:.0f}" if isinstance(score, (int, float)) else "--"
        return_pct = item.get("return_pct")
        return_text = f"{return_pct:+.2f}%" if isinstance(return_pct, (int, float)) else "--"
        lines.append(
            f"- {item.get('name') or item.get('code')}({item.get('code')}) "
            f"{label}｜评分 {score_text}｜浮盈 {return_text}"
        )
        reasons = item.get("reasons") or []
        if reasons:
            lines.append(f"  理由：{'；'.join(str(r) for r in reasons[:3])}")
    lines.append("")
    lines.append("> 建议仅作研究参考，据此操作风险自负。")
    return "\n".join(lines)


def notify_holdings_review(reviews: Sequence[Dict[str, Any]], *, review_date: date) -> bool:
    """推送复盘结果；无内容或未配置渠道时返回 False。"""
    content = build_review_report(reviews, review_date=review_date)
    if not content:
        logger.info("持仓复盘：无内容，跳过推送")
        return False
    try:
        from src.notification import get_notification_service

        result = get_notification_service().send_with_results(content, route_type="report")
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响复盘落库
        logger.warning("持仓复盘推送失败: %s", exc)
        return False
    success = bool(getattr(result, "success", False))
    logger.info("持仓复盘推送完成: success=%s", success)
    return success


# ----------------------------------------------------------------------
# 内部实现
# ----------------------------------------------------------------------


def _position_snapshot(row: TrackedPosition) -> Dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name or "",
        "market": row.market,
        "industry": row.industry or "",
        "source_date": row.source_date,
        "entry_date": row.entry_date,
        "entry_price": row.entry_price,
        "quantity": row.quantity,
        "avg_cost": row.avg_cost,
        "status": row.status,
    }


def _load_paper_costs(db: DatabaseManager, *, market: str = "cn") -> Dict[str, float]:
    """模拟盘实际持仓成本（含佣金），用于统一复盘与日报的浮盈口径。

    复盘原本一律用「推荐价」计算，而模拟盘日报用「含佣金成本」，
    同一只票会出现两个不同的盈亏数字；有持仓时以实际成本为准。
    """
    try:
        from src.services.cn_tracker.paper_trading_service import PAPER_ACCOUNT_NAME
    except Exception:  # noqa: BLE001 - 取不到账户名时退回推荐价
        return {}

    from src.storage import PortfolioAccount, PortfolioPosition

    with db.get_session() as session:
        account = (
            session.query(PortfolioAccount)
            .filter(PortfolioAccount.name == PAPER_ACCOUNT_NAME)
            .first()
        )
        if account is None:
            return {}
        rows = (
            session.query(PortfolioPosition.symbol, PortfolioPosition.avg_cost)
            .filter(
                PortfolioPosition.account_id == account.id,
                PortfolioPosition.market == market,
                PortfolioPosition.quantity > 0,
            )
            .all()
        )

    costs: Dict[str, float] = {}
    for symbol, cost in rows:
        try:
            value = float(cost)
        except (TypeError, ValueError):
            continue
        if value > 0:
            costs[str(symbol)] = value
    return costs


def _load_history(code: str, *, lookback_days: int = 60) -> Optional[pd.DataFrame]:
    try:
        from src.services.screening.daily import fetch_daily_history

        frame = fetch_daily_history(code, lookback_days=lookback_days, source="auto")
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘取日线失败 code=%s: %s", code, exc)
        return None
    if frame is None or getattr(frame, "empty", True):
        return None

    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result = result.dropna(subset=["date"]).sort_values("date")
    for column in ("open", "high", "low", "close", "volume"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    if result["close"].dropna().empty:
        return None
    return result


def _score_review(
    *,
    position: Dict[str, Any],
    history: Optional[pd.DataFrame],
    benchmark: Optional[pd.DataFrame],
    review_date: date,
    db: DatabaseManager,
    cost_map: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    code = str(position.get("code") or "")
    name = str(position.get("name") or "")
    entry = _as_float(position.get("entry_price"))
    # 已持仓时用模拟盘实际成本（含佣金），否则复盘与日报会给同一只票两个浮盈
    actual_cost = (cost_map or {}).get(code)
    if actual_cost:
        entry = float(actual_cost)
    source_date = position.get("source_date")
    holding_days = _count_holding_days(source_date, review_date)

    reasons: List[str] = []
    review: Dict[str, Any] = {
        "position_id": position.get("id"),
        "code": code,
        "name": name,
        "review_date": review_date,
        "holding_days": holding_days,
        "close": None,
        "return_pct": None,
        "excess_return_pct": None,
        "scores": {
            "trend": 0.0,
            "relative": 0.0,
            "volume": 0.0,
            "risk": 0.0,
            "policy": 0.0,
            "total": 0.0,
        },
        "advice": "reduce",
        "reasons": reasons,
        "hard_exit": False,
    }

    if history is None or history["close"].dropna().shape[0] < _MIN_BARS:
        reasons.append("日线数据不足，暂按观察处理")
        review["scores"]["total"] = 0.0
        return review

    closes = history["close"].dropna()
    close = float(closes.iloc[-1])
    ma5 = float(closes.tail(5).mean())
    ma10 = float(closes.tail(10).mean()) if len(closes) >= 10 else ma5
    ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else ma10
    prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else close
    day_change = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    return_pct = (close - entry) / entry * 100 if entry and entry > 0 else None

    stock_5d = _pct_change(closes, 5)
    bench_5d = _pct_change(benchmark["close"] if benchmark is not None else None, 5)
    excess = (
        stock_5d - bench_5d
        if stock_5d is not None and bench_5d is not None
        else None
    )

    volumes = pd.to_numeric(history.get("volume"), errors="coerce").dropna()
    volume_ratio: Optional[float] = None
    if len(volumes) >= _MIN_BARS:
        baseline = float(volumes.iloc[-6:-1].mean())
        if baseline > 0:
            volume_ratio = float(volumes.iloc[-1]) / baseline

    # --- 趋势 30 ---
    if close > ma5 > ma10 > ma20:
        trend_score = 30.0
        reasons.append("收盘价站上 MA5/MA10/MA20，多头排列")
    elif close > ma20:
        trend_score = 18.0
        reasons.append("收盘价仍在 MA20 上方")
    else:
        trend_score = 0.0
        reasons.append("收盘价跌破 MA20")

    # --- 相对强度 20 ---
    if excess is None:
        relative_score = 10.0
    elif excess >= 5:
        relative_score = 20.0
        reasons.append(f"近 5 日跑赢上证 {excess:.1f} 个百分点")
    elif excess >= 0:
        relative_score = 12.0
        reasons.append(f"近 5 日小幅跑赢上证 {excess:.1f} 个百分点")
    else:
        relative_score = 4.0
        reasons.append(f"近 5 日跑输上证 {abs(excess):.1f} 个百分点")

    # --- 量能 15 ---
    if volume_ratio is None:
        volume_score = 8.0
    elif 1.2 <= volume_ratio <= 3.0 and day_change > 0:
        volume_score = 15.0
        reasons.append("温和放量上涨")
    elif volume_ratio > 3.0 and day_change < 0:
        volume_score = 0.0
        reasons.append("放量下跌，注意资金出逃")
    else:
        volume_score = 8.0

    # --- 政策/消息 15 ---
    hits = recent_policy_hits(
        industry=str(position.get("industry") or ""),
        days=5,
        db=db,
        as_of=review_date,
    )
    negative_hits = [
        hit
        for hit in hits
        if hit.get("direction") == "negative"
        and int(hit.get("strength") or 0) >= NEGATIVE_POLICY_MIN_STRENGTH
    ]
    if negative_hits:
        policy_score = 0.0
        reasons.append(f"命中负面政策：{negative_hits[0].get('title', '')[:30]}")
    elif hits:
        policy_score = 15.0
        reasons.append("行业政策面偏正面")
    else:
        policy_score = 15.0

    # --- 风险 20 ---
    hard_reasons: List[str] = []
    if return_pct is not None and return_pct <= HARD_STOP_LOSS_PCT:
        risk_score = 0.0
        hard_reasons.append(f"浮亏 {return_pct:.1f}%，触及 {HARD_STOP_LOSS_PCT:.0f}% 止损线")
    elif return_pct is not None and return_pct <= -5:
        risk_score = 10.0
        reasons.append(f"浮亏 {return_pct:.1f}%，接近止损线")
    elif close < ma20:
        risk_score = 5.0
    else:
        risk_score = 20.0

    if close < ma20 and volume_ratio is not None and volume_ratio >= BREAKOUT_VOLUME_MULTIPLE:
        hard_reasons.append("跌破 MA20 且放量 1.5 倍以上")

    if negative_hits:
        hard_reasons.append("命中高等级负面政策事件")

    if (
        holding_days >= TIME_STOP_DAYS
        and excess is not None
        and excess < TIME_STOP_EXCESS_PCT
    ):
        hard_reasons.append(
            f"持有 {holding_days} 个交易日仍跑输上证 {abs(excess):.1f} 个百分点"
        )

    total = trend_score + relative_score + volume_score + risk_score + policy_score

    hard_exit = bool(hard_reasons)
    if hard_exit:
        advice = "exit"
        reasons.extend(hard_reasons)
    elif total >= ADVICE_HOLD_SCORE:
        advice = "hold"
    elif total >= ADVICE_REDUCE_SCORE:
        advice = "reduce"
    else:
        advice = "exit"

    review.update(
        {
            "close": close,
            "return_pct": return_pct,
            "excess_return_pct": excess,
            "scores": {
                "trend": trend_score,
                "relative": relative_score,
                "volume": volume_score,
                "risk": risk_score,
                "policy": policy_score,
                "total": float(total),
            },
            "advice": advice,
            "hard_exit": hard_exit,
        }
    )
    return review


def _persist_reviews(
    manager: DatabaseManager, reviews: Sequence[Dict[str, Any]]
) -> None:
    if not reviews:
        return
    with manager.get_session() as session:
        for review in reviews:
            position_id = review.get("position_id")
            if not position_id:
                continue
            position = session.get(TrackedPosition, int(position_id))
            if position is None:
                continue

            existing = (
                session.query(HoldingReview)
                .filter(
                    HoldingReview.position_id == position.id,
                    HoldingReview.review_date == review["review_date"],
                )
                .first()
            )
            if existing is None:
                existing = HoldingReview(
                    position_id=position.id,
                    code=review["code"],
                    review_date=review["review_date"],
                )
                session.add(existing)

            scores = review.get("scores") or {}
            existing.holding_days = int(review.get("holding_days") or 0)
            existing.close = review.get("close")
            existing.return_pct = review.get("return_pct")
            existing.excess_return_pct = review.get("excess_return_pct")
            existing.trend_score = scores.get("trend")
            existing.relative_score = scores.get("relative")
            existing.volume_score = scores.get("volume")
            existing.risk_score = scores.get("risk")
            existing.policy_score = scores.get("policy")
            existing.total_score = scores.get("total")
            existing.advice = str(review.get("advice") or "reduce")
            existing.reasons = json.dumps(
                review.get("reasons") or [], ensure_ascii=False
            )
            existing.hard_exit = bool(review.get("hard_exit"))

            position.last_review_date = review["review_date"]
            position.last_advice = existing.advice
            position.last_score = existing.total_score
        session.commit()


def _count_holding_days(source_date: Any, review_date: date) -> int:
    """持有交易日数（不含开仓当日）。

    时间止损按「交易日」衡量：用自然日会把周末算进去，导致 10 个交易日的
    止损线实际提前到第 7 个交易日触发。交易日历不可用时退回自然日估算。
    """
    if not isinstance(source_date, date):
        return 0
    try:
        from src.core.trading_calendar import get_trading_dates

        dates = get_trading_dates("cn", source_date, review_date)
        if dates is not None and len(dates):
            return max(0, len(dates) - 1)
    except Exception as exc:  # noqa: BLE001 - 日历不可用时降级
        logger.warning("交易日历不可用，持有天数按自然日估算: %s", exc)
    return max(0, (review_date - source_date).days)


def _pct_change(series: Any, days: int) -> Optional[float]:
    if series is None:
        return None
    values = pd.to_numeric(pd.Series(list(series)), errors="coerce").dropna()
    if len(values) < days + 1:
        return None
    base = float(values.iloc[-(days + 1)])
    latest = float(values.iloc[-1])
    if base <= 0:
        return None
    return (latest - base) / base * 100


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
