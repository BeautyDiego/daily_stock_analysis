# -*- coding: utf-8 -*-
"""A 股专用的分析与持仓跟踪能力。

包含三块：
- ``macro_policy_service``：宏观政策事件采集、归类与政策加成
- ``dedupe_filter``：跨日去重（冷却窗）与行业分散
- ``holding_tracker_service``：跟踪标的状态机、每日复盘评分卡与建议

本包只服务 A 股（``market="cn"``），不引用任何美股数据源。
"""

from src.services.cn_tracker.macro_policy_service import (
    collect_macro_events,
    policy_delta,
    recent_policy_hits,
)

__all__ = [
    "collect_macro_events",
    "policy_delta",
    "recent_policy_hits",
]
