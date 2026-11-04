# -*- coding: utf-8 -*-
"""组合约束实验：行业上限与冷却期对 5 只组合收益的影响。

之前验证的都是「单笔交易」层面的参数（止盈、止损、持有期）。行业上限与冷却期
不同——它们决定「5 只票怎么选出来」，影响的是**组合**，必须在组合层面测。

方法：

1. 每个交易日用当时可得的因子给全市场排序（只用日线可得因子，见 ``FACTOR_LABEL``）
2. 按给定的行业上限、冷却期挑出 Top N
3. 计算这 N 只等权持有 ``holding_days`` 的收益
4. 汇总各约束下的平均收益、波动、胜率、行业分散度

局限：价值/质量因子需要历史财务数据（未接入），这里只能用「低波动」这个
已被验证的正向因子代理选股逻辑，因此比较的是**约束之间的相对差异**，
不能当成实盘收益预测。
"""

from __future__ import annotations

import csv
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from src.storage import DatabaseManager, StockDaily

logger = logging.getLogger(__name__)

FACTOR_LABEL = "负 20 日波动率（低波动优先）"
FACTOR_WINDOW = 20
DEFAULT_TOP_N = 5
DEFAULT_HOLDING_DAYS = 5
DEFAULT_COOLDOWN_DAYS = 0
UNLIMITED = 99
MIN_HISTORY_BARS = FACTOR_WINDOW + 1


def _load_pool(
    db: DatabaseManager, start_date: date, *, min_days: int = 40
) -> List[str]:
    from sqlalchemy import func

    with db.get_session() as session:
        rows = (
            session.query(StockDaily.code, func.count(StockDaily.id))
            .filter(StockDaily.date >= start_date)
            .group_by(StockDaily.code)
            .having(func.count(StockDaily.id) >= min_days)
            .all()
        )
    return [str(row[0]) for row in rows]


def _load_panel(
    db: DatabaseManager, codes: Sequence[str], start_date: date
) -> Dict[str, pd.DataFrame]:
    if not codes:
        return {}
    with db.get_session() as session:
        rows = (
            session.query(
                StockDaily.code,
                StockDaily.date,
                StockDaily.open,
                StockDaily.close,
            )
            .filter(
                StockDaily.code.in_([str(code) for code in codes]),
                StockDaily.date >= start_date,
            )
            .order_by(StockDaily.code, StockDaily.date)
            .all()
        )
    if not rows:
        return {}

    frame = pd.DataFrame.from_records(
        rows, columns=["code", "date", "open", "close"]
    )
    frame["code"] = frame["code"].astype(str)
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)

    panel: Dict[str, pd.DataFrame] = {}
    for code, group in frame.groupby("code", sort=False):
        if len(group) < MIN_HISTORY_BARS + 1:
            continue
        panel[str(code)] = group.reset_index(drop=True)
    return panel


def _load_industry_map() -> Dict[str, str]:
    """读取行业映射文件（由 ``--sync-daily`` 生成）。"""
    try:
        from src.services.cn_tracker.industry import industry_map_path
    except Exception:  # noqa: BLE001
        return {}

    path = industry_map_path()
    if not path.is_file():
        return {}
    mapping: Dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                code = str(row.get("code") or "").strip()
                industry = str(row.get("industry") or "").strip()
                if len(code) == 6 and industry:
                    mapping[code] = industry
    except OSError as exc:
        logger.warning("读取行业映射失败: %s", exc)
    return mapping


def _factor_series(frame: pd.DataFrame) -> pd.Series:
    """负 20 日波动率：值越大（波动越小）越优先。"""
    close = pd.to_numeric(frame["close"], errors="coerce")
    return -close.pct_change().rolling(FACTOR_WINDOW).std()


def _pick_top(
    ranked: List[Tuple[str, float]],
    *,
    industries: Dict[str, str],
    top_n: int,
    industry_cap: int,
    cooldown: Dict[str, date],
    trade_date: date,
    cooldown_days: int,
) -> List[str]:
    """按行业上限与冷却期挑出 Top N。"""
    picked: List[str] = []
    per_industry: Dict[str, int] = {}
    for code, _ in ranked:
        if len(picked) >= top_n:
            break
        last = cooldown.get(code)
        if last is not None and cooldown_days > 0:
            if (trade_date - last).days < cooldown_days:
                continue
        industry = industries.get(code) or ""
        if industry and industry_cap < UNLIMITED:
            if per_industry.get(industry, 0) >= industry_cap:
                continue
        picked.append(code)
        if industry:
            per_industry[industry] = per_industry.get(industry, 0) + 1
    return picked


def compare_portfolio_constraints(
    *,
    industry_caps: Sequence[int] = (1, 2, 3, UNLIMITED),
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    top_n: int = DEFAULT_TOP_N,
    holding_days: int = DEFAULT_HOLDING_DAYS,
    lookback_days: int = 120,
    db: Optional[DatabaseManager] = None,
) -> Dict[str, Any]:
    """对比不同行业上限下的组合收益。"""
    manager = db or DatabaseManager.get_instance()
    start_date = date.today() - timedelta(days=lookback_days)
    codes = _load_pool(manager, start_date)
    panel = _load_panel(manager, codes, start_date)
    if len(panel) < 100:
        return {"error": "insufficient_data", "loaded": len(panel), "results": {}}

    industries = _load_industry_map()

    # 预计算每只票的因子序列与未来收益，避免按上限重复计算
    factor_map: Dict[str, pd.Series] = {}
    forward_map: Dict[str, pd.Series] = {}
    date_index: Optional[pd.Index] = None
    for code, frame in panel.items():
        series = _factor_series(frame)
        if series.dropna().empty:
            continue
        forward = (
            pd.to_numeric(frame["close"], errors="coerce").shift(-holding_days)
            / pd.to_numeric(frame["open"], errors="coerce").shift(-1)
            - 1
        )
        index = pd.Index(frame["date"], name="date")
        factor_map[code] = pd.Series(series.values, index=index)
        forward_map[code] = pd.Series(forward.values, index=index)
        if date_index is None:
            date_index = index

    if not factor_map or date_index is None:
        return {"error": "no_factor", "loaded": len(panel), "results": {}}

    factor_frame = pd.DataFrame(factor_map)
    forward_frame = pd.DataFrame(forward_map)

    results: Dict[int, Dict[str, Any]] = {}
    for cap in industry_caps:
        cooldown: Dict[str, date] = {}
        returns: List[float] = []
        industry_counts: List[int] = []
        periods = 0

        for as_of, row in factor_frame.iterrows():
            valid = row.dropna()
            if len(valid) < top_n * 4:
                continue
            ranked = sorted(
                ((code, float(value)) for code, value in valid.items()),
                key=lambda item: item[1],
                reverse=True,
            )
            picked = _pick_top(
                ranked,
                industries=industries,
                top_n=top_n,
                industry_cap=cap,
                cooldown=cooldown,
                trade_date=as_of,
                cooldown_days=cooldown_days,
            )
            if len(picked) < top_n:
                continue
            forward_row = forward_frame.loc[as_of].reindex(picked).dropna()
            if len(forward_row) < top_n:
                continue

            periods += 1
            returns.append(float(forward_row.mean()))
            industry_counts.append(
                len({industries.get(code) or code for code in picked})
            )
            for code in picked:
                cooldown[code] = as_of

        if not returns:
            results[cap] = {"periods": 0}
            continue

        series = pd.Series(returns)
        results[cap] = {
            "label": "不限" if cap >= UNLIMITED else f"≤{cap} 只",
            "periods": periods,
            "mean": round(float(series.mean()) * 100, 3),
            "median": round(float(series.median()) * 100, 3),
            "volatility": round(float(series.std()) * 100, 3),
            "win_rate": round(float((series > 0).mean()) * 100, 1),
            "avg_industries": round(
                sum(industry_counts) / len(industry_counts), 2
            ),
        }

    return {
        "loaded": len(panel),
        "industry_covered": len(industries),
        "factor": FACTOR_LABEL,
        "holding_days": holding_days,
        "cooldown_days": cooldown_days,
        "results": results,
    }


def build_constraint_report(stats: Dict[str, Any]) -> str:
    """渲染组合约束对比报告。"""
    if stats.get("error"):
        return f"组合约束实验未完成：{stats['error']}（已加载 {stats.get('loaded')} 只）"

    lines = [
        "组合约束对比（行业上限 × 冷却期）",
        "",
        f"选股因子：{stats.get('factor')}"
        f"｜样本 {stats.get('loaded')} 只"
        f"｜行业覆盖 {stats.get('industry_covered')}"
        f"｜持有 {stats.get('holding_days')} 日"
        f"｜冷却 {stats.get('cooldown_days')} 日",
        "",
        f"{'行业上限':<10}{'期数':>6}{'平均':>10}{'中位':>10}{'波动':>9}"
        f"{'收益/波动':>11}{'胜率':>8}{'实际行业数':>11}",
    ]
    for cap, item in (stats.get("results") or {}).items():
        if not item.get("periods"):
            continue
        ratio = (
            item["mean"] / item["volatility"] if item["volatility"] else None
        )
        lines.append(
            f"{item['label']:<10}"
            f"{item['periods']:>6}"
            f"{item['mean']:>+9.3f}%"
            f"{item['median']:>+9.3f}%"
            f"{item['volatility']:>8.3f}%"
            f"{(f'{ratio:+.3f}' if ratio is not None else '--'):>11}"
            f"{item['win_rate']:>7.1f}%"
            f"{item['avg_industries']:>11.2f}"
        )

    lines.append("")
    lines.append("说明：因子仅用低波动代理（价值/质量需历史财务数据），")
    lines.append("结论用于比较约束之间的相对优劣，不代表实盘收益。")
    return "\n".join(lines)
