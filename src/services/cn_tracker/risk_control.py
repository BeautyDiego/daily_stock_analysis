# -*- coding: utf-8 -*-
"""组合级风控与市场环境判断。

个股止损只能控制单票损失，控不住组合层面的回撤。这里补两道闸：

1. **账户风控**：从近期最高净值回撤超过阈值 → 暂停开新仓；单日亏损超阈值 → 当日停手。
2. **市场环境**：基准指数跌破 MA20 → 仓位上限砍半（熊市不满仓）。

两道闸都只在「盘前生成订单」时判断，**只拦新开仓，不拦卖出**——
风控的目的是阻止在不利环境下继续加仓，而不是阻止止损离场。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, Optional

from src.storage import DatabaseManager, PortfolioDailySnapshot, StockDaily

logger = logging.getLogger(__name__)

# 从近期最高净值回撤超过该比例 → 暂停开新仓
RISK_MAX_DRAWDOWN_PCT = -10.0
# 单日亏损超过该比例 → 当日不再开新仓
RISK_MAX_DAILY_LOSS_PCT = -3.0
# 判断回撤时回看的快照条数（约 3 个月交易日）
RISK_LOOKBACK_SNAPSHOTS = 60

# 市场环境基准与均线周期。
# 用沪深 300ETF（510300）而非指数裸码 000300：后者与深市股票代码格式相同，
# 走行情接口时会被当成股票而取不到数据（实测 sync 时报错）。
# ETF 跟踪指数、日线完整，作为「大盘在不在 MA20 上方」的代理足够。
MARKET_REGIME_CODE = "510300"
MARKET_REGIME_MA_DAYS = 20
# 熊市时仓位上限的折减系数
RISK_OFF_SLOT_RATIO = 0.5


def evaluate_account_risk(
    db: DatabaseManager, account_id: int, trade_date: date
) -> Dict[str, Any]:
    """按最近快照判断账户是否应暂停开新仓。

    用「最近一条快照」而不是实时净值：本函数在盘前调用，此时当日净值尚未产生，
    判断依据是上一交易日收盘时的账户状态，这与人工复盘时的决策依据一致。
    """
    with db.get_session() as session:
        rows = (
            session.query(
                PortfolioDailySnapshot.snapshot_date,
                PortfolioDailySnapshot.total_equity,
            )
            .filter(
                PortfolioDailySnapshot.account_id == account_id,
                PortfolioDailySnapshot.snapshot_date <= trade_date,
            )
            .order_by(PortfolioDailySnapshot.snapshot_date.desc())
            .limit(RISK_LOOKBACK_SNAPSHOTS)
            .all()
        )

    if not rows:
        return {
            "paused": False,
            "reason": "no_snapshot",
            "drawdown_pct": None,
            "daily_pct": None,
        }

    values = [float(row[1] or 0.0) for row in rows if row[1]]
    if not values:
        return {
            "paused": False,
            "reason": "no_equity",
            "drawdown_pct": None,
            "daily_pct": None,
        }

    latest = values[0]
    peak = max(values)
    drawdown_pct = (latest - peak) / peak * 100 if peak > 0 else None

    daily_pct: Optional[float] = None
    if len(values) >= 2 and values[1] > 0:
        daily_pct = (latest - values[1]) / values[1] * 100

    paused = False
    reason = ""
    if drawdown_pct is not None and drawdown_pct <= RISK_MAX_DRAWDOWN_PCT:
        paused = True
        reason = f"账户回撤 {drawdown_pct:.1f}%（阈值 {RISK_MAX_DRAWDOWN_PCT:.0f}%）"
    elif daily_pct is not None and daily_pct <= RISK_MAX_DAILY_LOSS_PCT:
        paused = True
        reason = f"单日亏损 {daily_pct:.1f}%（阈值 {RISK_MAX_DAILY_LOSS_PCT:.0f}%）"

    return {
        "paused": paused,
        "reason": reason,
        "drawdown_pct": round(drawdown_pct, 2) if drawdown_pct is not None else None,
        "daily_pct": round(daily_pct, 2) if daily_pct is not None else None,
    }


def evaluate_market_regime(
    db: DatabaseManager, trade_date: date
) -> Dict[str, Any]:
    """基准指数收盘价与 MA20 的关系；数据不足时视为中性（不拦）。

    取「不晚于 trade_date」的最近 20 个收盘价自算均线，不依赖 ``stock_daily.ma20``
    字段——指数日线未必带这列，自算更稳。
    """
    with db.get_session() as session:
        rows = (
            session.query(StockDaily.date, StockDaily.close)
            .filter(
                StockDaily.code == MARKET_REGIME_CODE,
                StockDaily.date <= trade_date,
            )
            .order_by(StockDaily.date.desc())
            .limit(MARKET_REGIME_MA_DAYS)
            .all()
        )

    closes = [float(row[1]) for row in rows if row[1]]
    if len(closes) < MARKET_REGIME_MA_DAYS:
        return {
            "risk_off": False,
            "close": closes[0] if closes else None,
            "ma20": None,
            "note": f"index_data_insufficient({len(closes)}/{MARKET_REGIME_MA_DAYS})",
        }

    latest = closes[0]
    ma20 = sum(closes) / len(closes)
    risk_off = latest < ma20
    return {
        "risk_off": risk_off,
        "close": round(latest, 2),
        "ma20": round(ma20, 2),
        "note": "below_ma20" if risk_off else "above_ma20",
    }


def adjusted_slots(base_slots: int, regime: Dict[str, Any]) -> int:
    """熊市折减仓位上限。"""
    if not regime.get("risk_off"):
        return base_slots
    return max(1, int(base_slots * RISK_OFF_SLOT_RATIO))
