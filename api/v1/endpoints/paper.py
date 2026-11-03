# -*- coding: utf-8 -*-
"""模拟盘看板接口。

展示模拟账户的净值曲线、当前持仓、近期成交与风控参数，数据复用
``paper_trading_service.get_paper_report``，不额外建表。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/dashboard", summary="模拟盘看板数据", response_model=dict)
async def get_paper_dashboard(
    curve_days: int = Query(default=30, ge=5, le=365, description="净值曲线天数"),
    trade_limit: int = Query(default=30, ge=1, le=200, description="近期成交条数"),
) -> Dict[str, Any]:
    """返回模拟盘净值、持仓、近期成交与风控参数。"""
    try:
        from src.services.cn_tracker.paper_trading_service import get_paper_report

        report = get_paper_report(curve_days=curve_days, trade_limit=trade_limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("模拟盘看板查询失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "paper_dashboard_failed", "message": str(exc)},
        ) from exc

    if not report.get("initialized"):
        return {
            "initialized": False,
            "message": report.get("message") or "模拟盘尚未初始化",
        }

    risk: Dict[str, float] = {}
    try:
        from src.services.cn_tracker.paper_bot_service import (
            HARD_STOP_LOSS_PCT,
            HARD_TAKE_PROFIT_PCT,
            TRAILING_ACTIVATE_PCT,
            TRAILING_DRAWDOWN_PCT,
        )

        risk = {
            "stop_loss_pct": HARD_STOP_LOSS_PCT,
            "take_profit_pct": HARD_TAKE_PROFIT_PCT,
            "trailing_activate_pct": TRAILING_ACTIVATE_PCT,
            "trailing_drawdown_pct": TRAILING_DRAWDOWN_PCT,
        }
    except Exception as exc:  # noqa: BLE001 - 风控参数缺失不影响主数据
        logger.debug("读取风控参数失败: %s", exc)

    return {
        "initialized": True,
        "as_of": report.get("as_of"),
        "initial_cash": report.get("initial_cash"),
        "return_pct": report.get("return_pct"),
        "snapshot": report.get("snapshot") or {},
        "positions": report.get("positions") or [],
        "curve": report.get("curve") or [],
        "trades": report.get("trades") or [],
        "risk": risk,
    }
