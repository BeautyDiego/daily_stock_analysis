# -*- coding: utf-8 -*-
"""因子分层回测：验证单因子在选股池内是否真的有区分度。

流程：

1. 准备候选池（与策略同口径的硬过滤，按成交额取前 N）
2. 确保池子的日线入库（不足时自动同步）
3. 对每个交易日 T，用 T 及之前的数据算因子值
4. 按因子值分 N 层，计算每层在 [T+1 开盘, T+holding 收盘] 的收益
5. 输出每层平均收益、多空价差、IC（秩相关）与结论

**三条已知偏差**（先跑通流程，后续用全量数据修正）：

- **池子偏差**：用「当前时点」的候选池回溯历史，存在选择偏差
- **因子范围**：只用日线可得因子；价值/质量因子需历史财务数据
- **未扣成本**：多空价差未计佣金与滑点

因此本模块的结论用于**比较因子之间的相对强弱**，不用于推算策略收益率。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from src.storage import DatabaseManager, StockDaily

logger = logging.getLogger(__name__)

# 只用日线可得的因子；value / quality 需历史财务（未接入）
FACTOR_LABELS: Dict[str, str] = {
    "momentum_5d": "近 5 日动量",
    "momentum_20d": "近 20 日动量",
    "reversal_5d": "近 5 日反转（取负）",
    "volatility_20d": "20 日波动（取负，低波动优）",
    "amount_20d": "20 日平均成交额",
    "volume_ratio": "量比（近 1 日 / 前 20 日均量）",
}

DEFAULT_HOLDING_DAYS = 5
DEFAULT_LAYERS = 5
MIN_LAYER_SIZE = 5
# IC 绝对值门槛：低于此值视为无区分度
IC_THRESHOLD = 0.02
# 因子计算所需的最小历史长度
MIN_HISTORY_BARS = 21


def build_pool(
    db: DatabaseManager, *, pool_size: int = 300, market: str = "cn"
) -> List[str]:
    """按策略同口径的硬过滤，取成交额前 N 作为回测池。

    用「当前快照」筛，所以只能代表当下的可交易集合——这是已知的池子偏差。
    """
    from src.services.screening.snapshot import fetch_snapshot_with_fallback

    try:
        frame = fetch_snapshot_with_fallback(["sina"], market=market)
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取快照失败，无法构建回测池: %s", exc)
        return []

    if frame is None or frame.empty:
        return []

    working = frame.copy()
    for column in ("amount", "market_cap", "price"):
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")

    mask = pd.Series(True, index=working.index)
    if "name" in working.columns:
        mask &= ~working["name"].astype(str).str.contains("ST|退", regex=True, na=False)
    if "amount" in working.columns:
        mask &= working["amount"].fillna(0) >= 1e8
    if "market_cap" in working.columns:
        mask &= working["market_cap"].fillna(0) >= 5e9
    if "price" in working.columns:
        mask &= working["price"].between(3, 15)

    filtered = working[mask]
    if filtered.empty:
        filtered = working

    if "amount" in filtered.columns:
        filtered = filtered.sort_values("amount", ascending=False)

    codes = [str(code) for code in filtered["code"].head(pool_size).tolist()]
    logger.info("回测池: %s 只（从 %s 只候选中筛出）", len(codes), len(frame))
    return codes


def _load_panel(
    db: DatabaseManager, codes: Sequence[str], start_date: date
) -> Dict[str, pd.DataFrame]:
    """一次查出整池日线，按代码分组。"""
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
                StockDaily.volume,
                StockDaily.amount,
            )
            .filter(
                StockDaily.code.in_([str(code) for code in codes]),
                StockDaily.date >= start_date,
            )
            .order_by(StockDaily.code, StockDaily.date)
            .all()
        )

    records = [
        {
            "code": str(row[0]),
            "date": row[1],
            "open": float(row[2] or 0),
            "high": float(row[3] or 0),
            "low": float(row[4] or 0),
            "close": float(row[5] or 0),
            "volume": float(row[6] or 0),
            "amount": float(row[7] or 0),
        }
        for row in rows
    ]
    if not records:
        return {}

    frame = pd.DataFrame(records)
    panel: Dict[str, pd.DataFrame] = {}
    for code, group in frame.groupby("code"):
        if len(group) < MIN_HISTORY_BARS:
            continue
        panel[str(code)] = group.reset_index(drop=True)
    return panel


def _factor_series(frame: pd.DataFrame, factor: str) -> pd.Series:
    """按行计算因子值序列（只用当日及之前的数据）。"""
    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    amount = pd.to_numeric(frame["amount"], errors="coerce")

    if factor == "momentum_5d":
        return close.pct_change(5)
    if factor == "momentum_20d":
        return close.pct_change(20)
    if factor == "reversal_5d":
        return -close.pct_change(5)
    if factor == "volatility_20d":
        # 低波动更优，取负让「值越大越好」的方向一致
        return -close.pct_change().rolling(20).std()
    if factor == "amount_20d":
        # 部分数据源不提供 amount，入库为 NULL；用收盘价×成交量代替
        turnover = (close * volume).where(amount.fillna(0) <= 0, amount)
        return turnover.rolling(20).mean()
    if factor == "volume_ratio":
        baseline = volume.shift(1).rolling(20).mean()
        return volume / baseline.replace(0, pd.NA)
    return pd.Series([pd.NA] * len(frame))


def _forward_returns(frame: pd.DataFrame, holding_days: int) -> pd.Series:
    """T+1 开盘买入、T+holding 收盘卖出的收益（避免前视偏差）。"""
    entry = pd.to_numeric(frame["open"], errors="coerce").shift(-1)
    exit_price = pd.to_numeric(frame["close"], errors="coerce").shift(-holding_days)
    return exit_price / entry - 1


def _spearman_ic(factor: pd.Series, forward: pd.Series) -> Optional[float]:
    """秩相关 IC：先取秩再算 Pearson。

    不用 ``Series.corr(method="spearman")``——它需要 scipy，而线上镜像没装
    （本地有 scipy，测试掩盖了这个问题，实测才暴露）。
    """
    if len(factor) < 3:
        return None
    corr = factor.rank().corr(forward.rank())
    return float(corr) if pd.notna(corr) else None


def _verdict(ic_mean: Optional[float], long_short: Optional[float]) -> str:
    if ic_mean is None or long_short is None:
        return "样本不足"
    if abs(ic_mean) < IC_THRESHOLD:
        return "无区分度"
    if ic_mean > 0 and long_short > 0:
        return "正向有效"
    if ic_mean < 0 and long_short < 0:
        return "反向有效"
    return "方向不一致"


def run_factor_layering(
    *,
    pool_size: int = 300,
    lookback_days: int = 120,
    holding_days: int = DEFAULT_HOLDING_DAYS,
    layers: int = DEFAULT_LAYERS,
    factors: Optional[Sequence[str]] = None,
    codes: Optional[Sequence[str]] = None,
    sync_missing: bool = True,
    db: Optional[DatabaseManager] = None,
) -> Dict[str, Any]:
    """跑分层回测，返回每个因子的分层收益与 IC。"""
    manager = db or DatabaseManager.get_instance()
    pool = [str(code) for code in (codes or [])]
    if not pool:
        pool = build_pool(manager, pool_size=pool_size)

    if not pool:
        return {"error": "empty_pool", "factors": {}}

    start_date = date.today() - timedelta(days=lookback_days)
    panel = _load_panel(manager, pool, start_date)

    # 数据不足时补同步（首次跑会很慢，约 1–2 分钟）
    if sync_missing and len(panel) < max(20, pool_size // 3):
        logger.info("回测池日线不足（%s/%s），开始同步…", len(panel), len(pool))
        try:
            from src.services.cn_tracker.holding_tracker_service import (
                sync_daily_history,
            )

            sync_daily_history(codes=pool, lookback_days=lookback_days, db=manager)
            panel = _load_panel(manager, pool, start_date)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回测池日线同步失败: %s", exc)

    if len(panel) < 20:
        return {
            "error": "insufficient_data",
            "pool_size": len(pool),
            "loaded": len(panel),
            "factors": {},
        }

    target_factors = list(factors) if factors else list(FACTOR_LABELS)
    results: Dict[str, Any] = {}

    for factor in target_factors:
        factor_panel: Dict[str, pd.Series] = {}
        return_panel: Dict[str, pd.Series] = {}
        date_index: Optional[pd.Index] = None

        for code, frame in panel.items():
            series = _factor_series(frame, factor)
            forward = _forward_returns(frame, holding_days)
            if series.dropna().empty:
                continue
            index = pd.Index(frame["date"], name="date")
            factor_panel[code] = pd.Series(series.values, index=index)
            return_panel[code] = pd.Series(forward.values, index=index)
            if date_index is None:
                date_index = index

        if not factor_panel or date_index is None:
            results[factor] = {"label": FACTOR_LABELS.get(factor, factor), "periods": 0}
            continue

        factor_frame = pd.DataFrame(factor_panel)
        return_frame = pd.DataFrame(return_panel)

        layer_sums = [0.0] * layers
        layer_counts = [0] * layers
        long_short_values: List[float] = []
        ic_values: List[float] = []
        periods = 0

        for as_of, row in factor_frame.iterrows():
            valid = row.dropna()
            if len(valid) < layers * MIN_LAYER_SIZE:
                continue
            forward_row = return_frame.loc[as_of].reindex(valid.index).dropna()
            aligned = valid.reindex(forward_row.index).dropna()
            if len(aligned) < layers * MIN_LAYER_SIZE:
                continue

            periods += 1
            ranks = aligned.rank(pct=True)
            layer_returns: List[float] = []
            for layer in range(layers):
                lower = layer / layers
                upper = (layer + 1) / layers
                mask = (ranks > lower) & (ranks <= upper)
                if not mask.any():
                    layer_returns.append(0.0)
                    continue
                mean_return = float(forward_row[mask].mean())
                layer_returns.append(mean_return)
                layer_sums[layer] += mean_return
                layer_counts[layer] += 1

            long_short_values.append(layer_returns[-1] - layer_returns[0])
            ic = _spearman_ic(aligned, forward_row)
            if ic is not None:
                ic_values.append(ic)

        if periods == 0:
            results[factor] = {
                "label": FACTOR_LABELS.get(factor, factor),
                "periods": 0,
            }
            continue

        layer_means = [
            round((layer_sums[index] / layer_counts[index]) * 100, 3)
            if layer_counts[index]
            else None
            for index in range(layers)
        ]
        long_short = (
            round(float(pd.Series(long_short_values).mean()) * 100, 3)
            if long_short_values
            else None
        )
        ic_mean = round(float(pd.Series(ic_values).mean()), 4) if ic_values else None
        ic_positive = (
            round(
                sum(1 for value in ic_values if value > 0) / len(ic_values), 3
            )
            if ic_values
            else None
        )

        results[factor] = {
            "label": FACTOR_LABELS.get(factor, factor),
            "periods": periods,
            "layer_returns": layer_means,
            "long_short": long_short,
            "ic_mean": ic_mean,
            "ic_positive_ratio": ic_positive,
            "verdict": _verdict(ic_mean, long_short),
        }

    return {
        "pool_size": len(pool),
        "loaded": len(panel),
        "lookback_days": lookback_days,
        "holding_days": holding_days,
        "layers": layers,
        "factors": results,
    }


def build_layering_report(stats: Dict[str, Any]) -> str:
    """把回测结果渲染为可读文本。"""
    if stats.get("error"):
        return (
            f"因子分层回测未完成：{stats['error']}"
            f"（池 {stats.get('pool_size')} 只，已加载 {stats.get('loaded')} 只）"
        )

    lines = [
        "因子分层回测",
        "",
        f"池: {stats.get('pool_size')} 只（已加载 {stats.get('loaded')} 只）"
        f"｜回看 {stats.get('lookback_days')} 天"
        f"｜持有 {stats.get('holding_days')} 日"
        f"｜分层 {stats.get('layers')}",
        "",
        f"{'因子':<28}{'期数':>5}{'多空':>9}{'IC':>8}{'IC>0':>7}  结论",
    ]
    for factor, item in (stats.get("factors") or {}).items():
        if not item.get("periods"):
            lines.append(f"{item.get('label', factor):<28}{'--':>5}{'--':>9}{'--':>8}{'--':>7}  {item.get('verdict', '样本不足')}")
            continue
        long_short = item.get("long_short")
        ic_mean = item.get("ic_mean")
        ic_positive = item.get("ic_positive_ratio")
        lines.append(
            f"{item.get('label', factor):<28}"
            f"{item.get('periods'):>5}"
            f"{(f'{long_short:+.2f}%' if long_short is not None else '--'):>9}"
            f"{(f'{ic_mean:+.4f}' if ic_mean is not None else '--'):>8}"
            f"{(f'{ic_positive:.0%}' if ic_positive is not None else '--'):>7}"
            f"  {item.get('verdict')}"
        )

    lines.append("")
    lines.append("分层收益（最弱 → 最强，%）")
    for factor, item in (stats.get("factors") or {}).items():
        layer_returns = item.get("layer_returns")
        if not layer_returns:
            continue
        cells = "  ".join(
            f"{value:+.2f}" if value is not None else "  --" for value in layer_returns
        )
        lines.append(f"  {item.get('label', factor):<26}{cells}")

    lines.append("")
    lines.append("说明：池子按「当前快照」筛出，存在选择偏差；收益未扣交易成本。")
    lines.append("结论只用于比较因子相对强弱，不能直接推算策略收益。")
    return "\n".join(lines)
