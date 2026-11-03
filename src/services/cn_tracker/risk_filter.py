# -*- coding: utf-8 -*-
"""退市风险剔除（选股链路的最后一道人工约束）。

策略 yaml 已经通过 `exclude_st` / `pe_ttm_min: 0` / `pb_min: 0` 剔除了
ST、亏损、净资产为负的标的。这里补两个 yaml 覆盖不到的点：

1. **退市整理期**：名称含「退」的标的（部分数据源的 ST 标记存在延迟）
2. **PE/PB 兜底校验**：上游快照字段缺失时，yaml 的数值过滤会静默失效

大股东质押比例、审计意见需要 Tushare 财务接口（`pledge_stat` / `fina_audit`），
留待 P1 阶段接入。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 退市整理期标的的名称标记
DELISTING_NAME_MARKERS = ("退市", "退")

# yaml 之外的兜底阈值（只在字段存在时生效）
PE_MIN = 0.0
PB_MIN = 0.0


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def has_delisting_risk(item: Dict[str, Any]) -> bool:
    """判断单个候选是否带退市风险。"""
    name = str(item.get("name") or "")
    if any(marker in name for marker in DELISTING_NAME_MARKERS):
        return True

    pe = item.get("pe")
    if pe is None:
        pe = item.get("pe_ttm")
    pb = item.get("pb")

    pe_value = _as_float(pe)
    if pe_value is not None and pe_value <= PE_MIN:
        return True
    pb_value = _as_float(pb)
    if pb_value is not None and pb_value <= PB_MIN:
        return True
    return False


def filter_delisting_risk(
    candidates: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """剔除退市风险标的，返回 (保留列表, 剔除数量)。"""
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for item in candidates:
        if has_delisting_risk(item):
            dropped += 1
            logger.debug(
                "剔除退市风险标的: code=%s name=%s",
                item.get("code"),
                item.get("name"),
            )
            continue
        kept.append(item)
    return kept, dropped
