# -*- coding: utf-8 -*-
"""A 股行业归属推断。

A 股全市场快照（新浪 / 腾讯）只提供价格、成交额、市值等字段，**不含行业**，
而行业分散与政策匹配都依赖它。这里先做名称启发式推断：

- 大多数 A 股证券简称带行业特征（``XX银行`` / ``中国建筑`` / ``XX钢铁``）；
- 推断不到时返回空串，调用方按「行业未知」处理（不参与行业上限计数）。

后续若要精确分类，可替换为申万分类表或 Tushare ``stock_basic.industry``。
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

INDUSTRY_MAP_FILENAME = "industry_map.csv"
INDUSTRY_MAP_MAX_AGE_DAYS = 7
_TUSHARE_ENDPOINT = "https://api.tushare.pro"

# 申万一级 -> 证券简称关键词（顺序即优先级）
INDUSTRY_NAME_HINTS: Dict[str, Tuple[str, ...]] = {
    "银行": ("银行", "农商", "农信", "村镇"),
    "非银金融": ("证券", "保险", "信托", "期货", "金控"),
    "建筑装饰": ("建筑", "建工", "建设", "工程", "路桥", "隧道", "铁建", "城建"),
    "建筑材料": ("建材", "水泥", "玻璃", "防水"),
    "钢铁": ("钢铁", "钢构", "特钢", "钢管"),
    "有色金属": ("有色", "铜业", "铝业", "稀土", "黄金", "矿业", "锌业"),
    "煤炭": ("煤业", "煤炭", "焦化"),
    "石油石化": ("石油", "石化", "油气", "海油"),
    "基础化工": ("化工", "化学", "新材料", "树脂", "橡胶"),
    "电力设备": ("电气", "光伏", "风电", "储能", "电池", "能源科技", "机电"),
    "机械设备": ("机械", "重工", "装备", "机器人", "机床", "精工"),
    "国防军工": ("航天", "航空", "军工", "船舶", "兵器"),
    "汽车": ("汽车", "客车", "重卡", "车业", "轮胎"),
    "家用电器": ("电器", "家电", "空调"),
    "食品饮料": ("食品", "乳业", "酒业", "酿酒", "饮料", "啤酒"),
    "医药生物": ("药业", "制药", "医药", "生物", "医疗", "疫苗", "诊断"),
    "公用事业": ("水务", "燃气", "环保", "热电", "供水"),
    "交通运输": ("港口", "航运", "机场", "高速", "铁路", "物流", "快递"),
    "电子": ("电子", "半导体", "芯片", "光电", "显示", "微电"),
    # 只保留指向性较强的词：「科技 / 智能 / 数据 / 云」过于宽泛，
    # 会把大量非软件公司误归到计算机（实测「科技」一项就误判 800+ 只）。
    "计算机": ("软件", "信息技术", "云计算", "网络安全"),
    "通信": ("通信", "网络", "光缆"),
    "传媒": ("传媒", "影视", "文化", "出版", "游戏"),
    "商贸零售": ("商业", "百货", "超市", "零售", "电商"),
    "农林牧渔": ("农业", "种业", "牧业", "渔业", "饲料", "养殖"),
    "社会服务": ("旅游", "酒店", "餐饮", "教育"),
    "纺织服饰": ("纺织", "服装", "服饰", "鞋业"),
    "轻工制造": ("纸业", "包装", "家居", "文娱用品"),
    "房地产": ("地产", "置业", "房产"),
}


def infer_industry(code: str, name: str) -> str:
    """按证券简称推断申万一级行业，推断不到返回空串。"""
    text = str(name or "").strip()
    if not text:
        return ""
    for industry, keywords in INDUSTRY_NAME_HINTS.items():
        if any(keyword in text for keyword in keywords):
            return industry
    return ""


# 粗分类名称：新浪行业把银行/保险/证券合并为「金融行业」，
# 与 Tushare 的细分「银行」混用会让行业上限失效——实测 4 只银行
# 被拆成「银行 2 + 金融行业 2」全部通过上限。
COARSE_INDUSTRY_NAMES = {"金融行业", "金融"}


def normalize_industry(industry: str, code: str, name: str) -> str:
    """把粗分类归一：用证券简称细分回具体行业（银行/保险/证券）。"""
    text = str(industry or "").strip()
    if not text:
        return ""
    if text in COARSE_INDUSTRY_NAMES:
        return infer_industry(code, name) or text
    return text


def fill_missing_industries(
    candidates: Sequence[dict],
) -> Sequence[dict]:
    """就地补齐候选的 ``industry`` 字段，返回同一个列表。

    这是**兜底**：快照的 ``industry`` 通常已由选股管线按
    ``INDUSTRY_MAP_FILES`` 填充（见 ``sync_industry_map``），只有缺值时
    才退回名称关键词推断。
    """
    for item in candidates:
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        industry = str(item.get("industry") or "")
        if industry:
            item["industry"] = normalize_industry(industry, code, name)
        else:
            item["industry"] = infer_industry(code, name)
    return candidates


def industry_map_path(data_dir: Optional[Any] = None) -> Path:
    """行业映射文件路径（默认落在 SCREENING_DATA_DIR 下）。"""
    base = Path(data_dir or os.getenv("SCREENING_DATA_DIR", "data/screening"))
    return base / INDUSTRY_MAP_FILENAME


def sync_industry_map(
    *,
    force: bool = False,
    max_age_days: int = INDUSTRY_MAP_MAX_AGE_DAYS,
    data_dir: Optional[Any] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """从 Tushare ``stock_basic`` 刷新 ``code -> industry`` 映射文件。

    生成的文件由选股管线通过 ``INDUSTRY_MAP_FILES`` 读取，**不需要改选股代码**：
    快照的 ``industry`` 列会在 L1 之前被填好，选股的行业分散与政策匹配直接受益。

    失败时保留旧文件并返回原因，不抛异常（行业映射属于增强项，不应阻断主流程）。
    """
    target = industry_map_path(data_dir)
    stats: Dict[str, Any] = {
        "path": str(target),
        "updated": False,
        "rows": 0,
        "reason": "",
    }

    if not force and _is_fresh(target, max_age_days):
        stats["reason"] = "fresh"
        stats["rows"] = _count_rows(target)
        return stats

    resolved_token = (token or os.getenv("TUSHARE_TOKEN") or "").strip()
    rows: List[Tuple[str, str]] = []
    sources: List[str] = []

    # 首选 Tushare：细分行业更精确（如「钢加工」「乳制品」）
    if resolved_token:
        sources.append("tushare")
        try:
            rows = _fetch_tushare_industries(resolved_token)
        except Exception as exc:  # noqa: BLE001 - 换下一个源
            logger.warning("Tushare 行业映射失败（%s），尝试新浪兜底", exc)

    # 兜底新浪：免费无配额，但分类较粗（如「玻璃行业」）
    if not rows:
        sources.append("sina")
        try:
            rows = _fetch_sina_industries()
        except Exception as exc:  # noqa: BLE001
            logger.warning("新浪行业映射失败: %s", exc)

    if not rows:
        stats["reason"] = f"all_sources_failed: {','.join(sources) or 'none'}"
        return stats

    stats["source"] = sources[-1]

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["code", "industry"])
            writer.writerows(rows)
    except OSError as exc:
        logger.warning("行业映射写入失败: %s", exc)
        stats["reason"] = f"write_failed: {exc}"
        return stats

    stats.update({"updated": True, "rows": len(rows), "reason": "ok"})
    logger.info("行业映射已更新: %s 条 → %s", len(rows), target)
    return stats


def _is_fresh(path: Path, max_age_days: int) -> bool:
    if not path.is_file():
        return False
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return False
    return (date.today() - modified).days <= max(1, int(max_age_days))


def _count_rows(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return max(0, sum(1 for _ in handle) - 1)
    except OSError:
        return 0


def _fetch_sina_industries() -> List[Tuple[str, str]]:
    """从新浪行业板块构建 code->industry（无需 key、无配额）。"""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    }
    base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"

    response = requests.get(
        f"{base}/Market_Center.getHQNodes", headers=headers, timeout=20
    )
    response.raise_for_status()
    nodes = _collect_sina_industry_nodes(response.json())
    if not nodes:
        raise RuntimeError("sina returned no industry nodes")

    mapping: Dict[str, str] = {}
    for name, node_id in nodes:
        try:
            resp = requests.get(
                f"{base}/Market_Center.getHQNodeData",
                params={
                    "page": 1,
                    "num": 1000,
                    "sort": "symbol",
                    "asc": 1,
                    "node": node_id,
                    "symbol": "",
                    "_s_r_a": "page",
                },
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:  # noqa: BLE001 - 单板块失败不影响整体
            logger.debug("新浪板块 %s 取数失败: %s", node_id, exc)
            continue
        for item in items or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if len(code) == 6:
                mapping.setdefault(code, name)

    logger.info("新浪行业映射: 板块=%s 覆盖=%s", len(nodes), len(mapping))
    return list(mapping.items())


def _collect_sina_industry_nodes(tree: Any) -> List[Tuple[str, str]]:
    """从新浪板块树取出「新浪行业」下的 (行业名, node_id)。"""
    if not isinstance(tree, list):
        return []
    if len(tree) >= 2 and tree[0] == "新浪行业" and isinstance(tree[1], list):
        nodes: List[Tuple[str, str]] = []
        for child in tree[1]:
            if isinstance(child, list) and len(child) >= 3:
                name = str(child[0] or "").strip()
                node_id = str(child[2] or "").strip()
                if name and node_id:
                    nodes.append((name, node_id))
        return nodes

    found: List[Tuple[str, str]] = []
    for child in tree:
        found.extend(_collect_sina_industry_nodes(child))
    return found


def _fetch_tushare_industries(token: str) -> List[Tuple[str, str]]:
    import requests

    payload = {
        "api_name": "stock_basic",
        "token": token,
        "params": {"exchange": "", "list_status": "L"},
        "fields": "symbol,name,industry",
    }
    response = requests.post(_TUSHARE_ENDPOINT, json=payload, timeout=30)
    response.raise_for_status()
    body = response.json()
    if body.get("code") not in (0, None):
        raise RuntimeError(str(body.get("msg") or "tushare error"))

    data = body.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    try:
        symbol_at = fields.index("symbol")
        industry_at = fields.index("industry")
    except ValueError as exc:
        raise RuntimeError("unexpected stock_basic fields") from exc

    rows: List[Tuple[str, str]] = []
    for item in items:
        symbol = str(item[symbol_at] or "").strip()
        industry = str(item[industry_at] or "").strip()
        if len(symbol) == 6 and industry:
            rows.append((symbol, industry))
    return rows
