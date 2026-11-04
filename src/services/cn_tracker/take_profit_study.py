# -*- coding: utf-8 -*-
"""止盈方式对比研究：一次性 vs 分批 vs 纯移动止盈。

方法：从历史日线里取大量「买入点」，对同一个买入点分别模拟三种止盈规则，
比较最终收益。因为是**配对样本**（同一买入点、同一起点），差异只来自止盈方式，
不受选股质量干扰。

三种规则：

1. ``single``：涨到 4×ATR 一次性全卖（当前实现）
2. ``split``：涨到 2×ATR 卖 1/3、涨到 4×ATR 再卖 1/3，剩余到期卖
3. ``trail``：不设固定止盈，浮盈超 10% 后从峰值回落 5% 全卖

局限：未扣交易成本（分批止盈必然多付佣金，见结论说明）。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from src.services.cn_tracker.risk_control import (
    ATR_PERIOD,
    FALLBACK_TAKE_PROFIT_PCT,
    TAKE_PCT_CEIL,
    TAKE_PCT_FLOOR,
    atr_percent_from_bars,
    take_profit_pct,
)
from src.storage import DatabaseManager, StockDaily

logger = logging.getLogger(__name__)

MODES = ("single", "split", "trail")
MODE_LABELS = {
    "single": "一次性（4×ATR 全卖）",
    "split": "分三批（2×ATR 卖 1/3、4×ATR 再 1/3、余下到期）",
    "trail": "纯移动止盈（+10% 后回落 5%）",
}

DEFAULT_MAX_HOLD_DAYS = 10
TRAILING_ACTIVATE_PCT = 10.0
TRAILING_DRAWDOWN_PCT = 5.0
# 采样间隔：每 N 个交易日取一个买入点（控制样本量）
SAMPLE_STEP_DAYS = 3
MIN_HISTORY_BARS = ATR_PERIOD + 1


def _load_pool(
    db: DatabaseManager, start_date: date, *, min_days: int = 60
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
                StockDaily.high,
                StockDaily.low,
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
        rows, columns=["code", "date", "open", "high", "low", "close"]
    )
    frame["code"] = frame["code"].astype(str)
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)

    panel: Dict[str, pd.DataFrame] = {}
    for code, group in frame.groupby("code", sort=False):
        if len(group) < MIN_HISTORY_BARS + 1:
            continue
        panel[str(code)] = group.reset_index(drop=True)
    return panel


def _simulate(
    bars: Sequence[Dict[str, float]],
    entry: float,
    *,
    target_pct: float,
    mode: str,
    max_hold_days: int,
) -> Optional[Dict[str, Any]]:
    """模拟一笔交易，返回收益率（%）与是否触发止盈。"""
    if entry <= 0 or not bars:
        return None

    remaining = 1.0
    realized = 0.0
    triggered = False

    single_target = entry * (1 + target_pct / 100)
    first_target = entry * (1 + target_pct / 200)
    second_target = single_target

    peak = entry
    sold_first = False
    sold_second = False

    end_index = min(len(bars), max_hold_days)
    for index in range(end_index):
        bar = bars[index]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        if high <= 0 or close <= 0:
            continue
        peak = max(peak, high)

        if mode == "single":
            if high >= single_target:
                realized += remaining * (target_pct / 100)
                remaining = 0.0
                triggered = True
                break
        elif mode == "split":
            if not sold_first and high >= first_target:
                realized += (1 / 3) * (target_pct / 200)
                remaining -= 1 / 3
                sold_first = True
                triggered = True
            if not sold_second and high >= second_target:
                realized += (1 / 3) * (target_pct / 100)
                remaining -= 1 / 3
                sold_second = True
                triggered = True
        elif mode == "trail":
            if peak >= entry * (1 + TRAILING_ACTIVATE_PCT / 100):
                stop_line = peak * (1 - TRAILING_DRAWDOWN_PCT / 100)
                # 盘中检查：当日最低价触及回落线即视为卖出
                if low > 0 and low <= stop_line:
                    realized += remaining * (stop_line / entry - 1)
                    remaining = 0.0
                    triggered = True
                    break

    if remaining > 0 and end_index > 0:
        last_close = float(bars[end_index - 1]["close"])
        if last_close > 0:
            realized += remaining * (last_close / entry - 1)

    return {"return_pct": realized * 100, "triggered": triggered}


def compare_take_profit(
    *,
    lookback_days: int = 120,
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    pool_size: int = 0,
    sample_step_days: int = SAMPLE_STEP_DAYS,
    target_multiplier: Optional[float] = None,
    db: Optional[DatabaseManager] = None,
) -> Dict[str, Any]:
    """在同一个买入点集合上比较三种止盈规则。

    ``target_multiplier`` 用于覆盖默认的 4×ATR（验证「止盈目标设多高」）。
    """
    manager = db or DatabaseManager.get_instance()
    start_date = date.today() - timedelta(days=lookback_days)
    codes = _load_pool(manager, start_date)
    if pool_size > 0:
        codes = codes[:pool_size]

    panel = _load_panel(manager, codes, start_date)
    if len(panel) < 50:
        return {"error": "insufficient_data", "loaded": len(panel), "results": {}}

    samples: Dict[str, List[float]] = {mode: [] for mode in MODES}
    triggered: Dict[str, int] = {mode: 0 for mode in MODES}
    win: Dict[str, int] = {mode: 0 for mode in MODES}
    buy_points = 0

    for code, frame in panel.items():
        opens = frame["open"].tolist()
        highs = frame["high"].tolist()
        lows = frame["low"].tolist()
        closes = frame["close"].tolist()

        # 留出 ATR 窗口与持有期
        last_entry = len(frame) - max_hold_days - 2
        for index in range(MIN_HISTORY_BARS, max(MIN_HISTORY_BARS, last_entry), sample_step_days):
            entry = float(opens[index])
            if entry <= 0:
                continue

            history = [
                (float(highs[i]), float(lows[i]), float(closes[i]))
                for i in range(index - ATR_PERIOD - 1, index)
                if highs[i] > 0 and lows[i] > 0 and closes[i] > 0
            ]
            atr_pct = atr_percent_from_bars(history, period=ATR_PERIOD)
            if target_multiplier is not None:
                target_pct = (
                    max(
                        TAKE_PCT_FLOOR,
                        min(TAKE_PCT_CEIL, target_multiplier * atr_pct),
                    )
                    if atr_pct
                    else FALLBACK_TAKE_PROFIT_PCT
                )
            else:
                target_pct = take_profit_pct(atr_pct)

            bars = [
                {
                    "high": float(highs[i]),
                    "low": float(lows[i]),
                    "close": float(closes[i]),
                }
                for i in range(index, min(index + max_hold_days, len(frame)))
            ]
            if len(bars) < 2:
                continue

            buy_points += 1
            for mode in MODES:
                result = _simulate(
                    bars,
                    entry,
                    target_pct=target_pct,
                    mode=mode,
                    max_hold_days=max_hold_days,
                )
                if result is None:
                    continue
                samples[mode].append(result["return_pct"])
                if result["triggered"]:
                    triggered[mode] += 1
                if result["return_pct"] > 0:
                    win[mode] += 1

    results: Dict[str, Any] = {}
    for mode in MODES:
        values = samples[mode]
        if not values:
            continue
        series = pd.Series(values)
        results[mode] = {
            "label": MODE_LABELS[mode],
            "samples": len(values),
            "mean": round(float(series.mean()), 3),
            "median": round(float(series.median()), 3),
            "win_rate": round(win[mode] / len(values) * 100, 1),
            "trigger_rate": round(triggered[mode] / len(values) * 100, 1),
            "p90_loss": round(float(series.quantile(0.1)), 3),
        }

    return {
        "buy_points": buy_points,
        "loaded": len(panel),
        "lookback_days": lookback_days,
        "max_hold_days": max_hold_days,
        "results": results,
    }


def build_study_report(stats: Dict[str, Any]) -> str:
    """渲染对比报告。"""
    if stats.get("error"):
        return f"止盈对比未完成：{stats['error']}（已加载 {stats.get('loaded')} 只）"

    results = stats.get("results") or {}
    lines = [
        "止盈方式对比（配对样本，同一买入点）",
        "",
        f"买入点 {stats.get('buy_points')} 个｜回看 {stats.get('lookback_days')} 天"
        f"｜最长持有 {stats.get('max_hold_days')} 日",
        "",
        f"{'方式':<34}{'样本':>8}{'平均':>9}{'中位':>9}{'胜率':>8}{'触发':>8}{'P10':>9}",
    ]
    for mode in MODES:
        item = results.get(mode)
        if not item:
            continue
        label = item["label"]
        lines.append(
            f"{label:<34}"
            f"{item['samples']:>8}"
            f"{item['mean']:>+8.3f}%"
            f"{item['median']:>+8.3f}%"
            f"{item['win_rate']:>7.1f}%"
            f"{item['trigger_rate']:>7.1f}%"
            f"{item['p90_loss']:>+8.2f}%"
        )

    single = results.get("single")
    split = results.get("split")
    trail = results.get("trail")
    if single and split:
        delta = split["mean"] - single["mean"]
        lines.append("")
        lines.append(f"分批相对一次性：平均 {delta:+.3f}pct，胜率 {split['win_rate'] - single['win_rate']:+.1f}pct")
    if single and trail:
        delta = trail["mean"] - single["mean"]
        lines.append(f"移动相对一次性：平均 {delta:+.3f}pct，胜率 {trail['win_rate'] - single['win_rate']:+.1f}pct")

    lines.append("")
    lines.append("说明：未扣交易成本；分批必然多付佣金（每多一笔至少 5 元）。")
    lines.append("低价股单笔约 4000 元，多一笔的成本约 0.13%。")
    return "\n".join(lines)
