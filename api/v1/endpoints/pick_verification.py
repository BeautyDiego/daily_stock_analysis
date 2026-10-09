# -*- coding: utf-8 -*-
"""Daily screening pick verification endpoints.

Serves the pick-verification dashboard: per-market Top N picks of each
screening day along with their next-session (T+1) real performance.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from src.services.screening_verification_service import (
    SUPPORTED_MARKETS,
    ScreeningVerificationService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/dashboard",
    summary="选股验证大屏数据",
    response_model=dict,
)
async def get_dashboard(
    limit_days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """返回按选股日聚合的选股与次日验证对照数据。"""
    try:
        return ScreeningVerificationService().get_dashboard(limit_days=limit_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("选股验证大屏查询失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "pick_verification_dashboard_failed",
                "message": str(exc),
            },
        ) from exc


@router.post(
    "/picks",
    summary="触发每日选股",
    response_model=dict,
)
async def run_picks(
    markets: Optional[str] = Query(default=None, description="逗号分隔，例如 cn,us"),
    top: int = Query(default=5, ge=1, le=20),
) -> dict:
    """手动触发每日选股并把各市场 Top N 落库。"""
    parsed: Optional[List[str]] = None
    if markets:
        parsed = [item.strip() for item in markets.split(",") if item.strip()]
        unsupported = [item for item in parsed if item not in SUPPORTED_MARKETS]
        if unsupported:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "pick_verification_unsupported_market",
                    "message": f"不支持的市场: {', '.join(unsupported)}",
                },
            )
    try:
        return ScreeningVerificationService().run_daily_picks(
            markets=parsed,
            max_results=top,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("每日选股执行失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "pick_verification_picks_failed",
                "message": str(exc),
            },
        ) from exc


@router.post(
    "/verify",
    summary="触发次日验证",
    response_model=dict,
)
async def run_verify(
    market: Optional[str] = Query(default=None),
    force: bool = Query(default=False),
) -> dict:
    """手动触发选股次日验证，返回处理统计。"""
    if market and market not in SUPPORTED_MARKETS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "pick_verification_unsupported_market",
                "message": f"不支持的市场: {market}",
            },
        )
    try:
        return ScreeningVerificationService().verify_picks(
            market=market,
            force=force,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("选股验证执行失败: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "pick_verification_run_failed",
                "message": str(exc),
            },
        ) from exc
