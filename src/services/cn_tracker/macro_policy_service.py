# -*- coding: utf-8 -*-
"""宏观政策事件采集与政策加成（A 股专用）。

政策源复用项目既有搜索能力（Tavily / Bocha / 搜狗等），采集后按关键词规则
归类为「层级 / 类别 / 方向 / 强度 / 影响行业」并落库到 ``macro_events``。
落库之后，选股与复盘只读数据库，不依赖搜索服务在线，也不依赖 LLM。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.storage import DatabaseManager, MacroEvent

logger = logging.getLogger(__name__)

# 政策检索式：覆盖货币 / 财政 / 产业 / 监管四类
POLICY_QUERIES: Tuple[str, ...] = (
    "国务院常务会议 稳增长 政策 最新",
    "央行 降准 降息 LPR 货币政策 最新",
    "财政部 专项债 减税降费 财政政策 最新",
    "发改委 产业政策 重大项目 最新",
    "证监会 资本市场 监管政策 最新",
)

# 行业关键词（申万一级 -> 触发词）
SECTOR_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "银行": ("银行", "信贷", "存款准备金", "LPR", "净息差", "普惠金融"),
    "非银金融": ("券商", "证券", "保险", "资本市场", "IPO", "并购重组"),
    "房地产": ("房地产", "楼市", "住房", "保交楼", "公积金"),
    "建筑装饰": ("基建", "专项债", "重大项目", "城市更新", "水利"),
    "建筑材料": ("水泥", "玻璃", "建材"),
    "钢铁": ("钢铁", "粗钢", "产能置换"),
    "有色金属": ("有色", "铜价", "铝价", "锂矿", "稀土"),
    "煤炭": ("煤炭", "煤价", "保供"),
    "石油石化": ("原油", "石油", "石化", "天然气"),
    "基础化工": ("化工", "化肥", "纯碱"),
    "电力设备": ("光伏", "风电", "储能", "新能源", "电网", "特高压"),
    "机械设备": ("机器人", "工程机械", "设备更新", "机床"),
    "国防军工": ("军工", "国防", "航空发动机"),
    "汽车": ("汽车", "新能源车", "以旧换新", "智能驾驶"),
    "家用电器": ("家电", "以旧换新"),
    "食品饮料": ("白酒", "食品", "消费券", "餐饮"),
    "医药生物": ("创新药", "医保", "集采", "药品", "医疗器械"),
    "公用事业": ("电价", "电力", "燃气", "水务", "环保"),
    "交通运输": ("物流", "航运", "航空", "铁路", "港口"),
    "电子": ("半导体", "芯片", "集成电路", "消费电子", "面板"),
    "计算机": ("人工智能", "算力", "软件", "信创", "数据要素"),
    "通信": ("5G", "光模块", "通信", "卫星"),
    "传媒": ("游戏", "影视", "传媒", "出版"),
    "商贸零售": ("零售", "免税", "跨境电商"),
    "农林牧渔": ("农业", "种业", "生猪", "粮食"),
    "社会服务": ("旅游", "酒店", "教育", "养老"),
    "纺织服饰": ("纺织", "服装"),
    "轻工制造": ("造纸", "家居", "包装"),
    "美容护理": ("化妆品", "医美"),
}

POSITIVE_TERMS = (
    "支持", "鼓励", "加大", "提振", "减税", "降费", "补贴", "放宽", "扩大",
    "促进", "推动", "利好", "降准", "降息", "宽松", "释放", "提速", "加快",
    "落地", "扩容", "试点", "扶持",
)
NEGATIVE_TERMS = (
    "收紧", "限制", "禁止", "处罚", "整改", "下调", "压降", "风险", "严控",
    "加税", "提高门槛", "去产能", "叫停", "约谈", "规范",
)
CENTRAL_TERMS = ("国务院", "中共中央", "政治局", "中央经济工作会议", "国常会")
MINISTRY_TERMS = ("部", "委", "局", "央行", "人民银行", "证监会", "银保监", "外汇局")
LOCAL_TERMS = ("省", "市", "区", "县")

# 必须命中的政策信号词：过滤「行情点评 / 活动 / 招聘」类噪音
POLICY_SIGNAL_TERMS = (
    "政策", "通知", "意见", "方案", "措施", "部署", "会议", "规划", "印发",
    "决定", "公告", "降准", "降息", "专项债", "减税", "降费", "补贴", "监管",
    "支持", "推进", "加快", "试点", "改革", "调控",
)
# 可信来源：政府站点与主流财经媒体
TRUSTED_DOMAINS = (
    "gov.cn", "xinhuanet.com", "people.com.cn", "cs.com.cn", "cnstock.com",
    "stcn.com", "cls.cn", "yicai.com", "21jingji.com", "nbd.com.cn",
    "ce.cn", "chinadaily.com.cn", "wallstreetcn.com", "thepaper.cn",
    "eastmoney.com", "sina.com.cn", "hexun.com", "jrj.com.cn",
)
# 明显噪音词：命中直接丢弃（含行情点评类，它们不是政策）
NOISE_TERMS = (
    "秋招", "招聘", "校招", "培训", "报名", "直播预告", "峰会", "沙龙",
    "免责声明", "风险提示", "广告", "课程", "考试",
    "股指", "涨超", "涨停", "跌停", "早盘", "尾盘", "收盘", "盘中",
    "行情回顾", "机构调研",
)

_DATE_PATTERN = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def collect_macro_events(
    *,
    db: Optional[DatabaseManager] = None,
    lookback_days: int = 3,
    max_results_per_query: int = 6,
    queries: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """采集政策新闻并结构化入库。

    Returns:
        统计字典：``queries / results / saved / skipped / errors / no_search``
    """
    manager = db or DatabaseManager.get_instance()
    stats: Dict[str, Any] = {
        "queries": 0,
        "results": 0,
        "saved": 0,
        "skipped": 0,
        "errors": 0,
        "no_search": False,
    }

    try:
        from src.search_service import get_search_service

        service = get_search_service()
    except Exception as exc:  # noqa: BLE001 - 搜索不可用不应阻断其它任务
        logger.warning("政策采集：搜索服务初始化失败: %s", exc)
        stats["errors"] += 1
        stats["no_search"] = True
        return stats

    if not getattr(service, "is_available", False):
        logger.info("政策采集：未配置可用搜索源，跳过本次采集")
        stats["no_search"] = True
        return stats

    for query in (queries or POLICY_QUERIES):
        stats["queries"] += 1
        try:
            response = _search_policy_query(
                service,
                query,
                max_results=max_results_per_query,
                days=lookback_days,
            )
        except Exception as exc:  # noqa: BLE001 - 单个检索式失败不影响其它
            logger.warning("政策采集：搜索失败 query=%s: %s", query, exc)
            stats["errors"] += 1
            continue

        if response is None or not getattr(response, "success", False):
            stats["errors"] += 1
            continue

        for item in getattr(response, "results", []) or []:
            stats["results"] += 1
            try:
                if _save_event(manager, item, query):
                    stats["saved"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("政策采集：写入失败 title=%s: %s", getattr(item, "title", ""), exc)
                stats["errors"] += 1

    logger.info(
        "政策采集完成: queries=%s results=%s saved=%s skipped=%s errors=%s",
        stats["queries"],
        stats["results"],
        stats["saved"],
        stats["skipped"],
        stats["errors"],
    )
    return stats


def recent_policy_hits(
    *,
    industry: str,
    days: int = 5,
    db: Optional[DatabaseManager] = None,
    as_of: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """返回近 ``days`` 日命中该行业的政策事件，按强度降序。"""
    if not industry:
        return []
    manager = db or DatabaseManager.get_instance()
    end = as_of or date.today()
    start = end - timedelta(days=max(1, int(days)))
    with manager.get_session() as session:
        rows = (
            session.query(MacroEvent)
            .filter(MacroEvent.event_date >= start, MacroEvent.event_date <= end)
            .order_by(MacroEvent.event_date.desc())
            .all()
        )

    hits: List[Dict[str, Any]] = []
    for row in rows:
        sectors = _load_sectors(row.sectors)
        if not _sector_matches(industry, sectors):
            continue
        hits.append(
            {
                "event_id": row.id,
                "direction": row.direction,
                "strength": int(row.strength or 3),
                "category": row.category,
                "level": row.level,
                "event_date": row.event_date.isoformat() if row.event_date else "",
                "title": row.title,
            }
        )
    # 强度高的排前面；同强度时日期更新的排前面（原实现按日期升序，取到的是最旧一条）
    hits.sort(
        key=lambda item: (int(item["strength"]), item["event_date"]),
        reverse=True,
    )
    return hits


def policy_delta(hits: Sequence[Dict[str, Any]]) -> float:
    """把命中事件折算为 [-10, +10] 的加分。"""
    total = 0.0
    for hit in hits:
        strength = int(hit.get("strength") or 3)
        direction = str(hit.get("direction") or "neutral")
        if direction == "positive":
            total += 2.0 if strength <= 2 else 5.0
        elif direction == "negative":
            total -= 3.0 if strength <= 2 else 8.0
    return max(-10.0, min(10.0, total))


def apply_policy_bonus(
    candidates: Sequence[Dict[str, Any]],
    *,
    db: Optional[DatabaseManager] = None,
    as_of: Optional[date] = None,
    days: int = 5,
) -> List[Dict[str, Any]]:
    """对候选批量施加政策加成，就地写回 ``final_score`` 与 ``policy_hits``。

    保持输入顺序不变（排序由调用方负责），返回同一个列表便于链式调用。
    """
    for item in candidates:
        industry = str(item.get("industry") or "")
        hits = recent_policy_hits(
            industry=industry, days=days, db=db, as_of=as_of
        )
        if not hits:
            item.setdefault("policy_hits", [])
            continue
        delta = policy_delta(hits)
        base = item.get("score", item.get("final_score"))
        try:
            base_value = float(base) if base is not None else 0.0
        except (TypeError, ValueError):
            base_value = 0.0
        item["policy_delta"] = round(delta, 2)
        item["policy_hits"] = hits
        item["score"] = round(base_value + delta, 4)
        item["final_score"] = item["score"]
    return list(candidates)


def _search_policy_query(
    service: Any, query: str, *, max_results: int, days: int
) -> Any:
    """兼容不同版本搜索门面：优先主题新闻接口，其次通用 search。"""
    topic_search = getattr(service, "search_topic_news", None)
    if callable(topic_search):
        return topic_search(query, max_results=max_results)
    generic_search = getattr(service, "search", None)
    if callable(generic_search):
        return generic_search(query, max_results=max_results, days=max(1, int(days)))
    raise AttributeError("搜索服务缺少可用的检索方法")


def _save_event(manager: DatabaseManager, item: Any, query: str) -> bool:
    title = str(getattr(item, "title", "") or "").strip()
    if not title:
        return False

    snippet = str(getattr(item, "snippet", "") or "").strip()
    text = f"{title} {snippet}"

    # 噪音过滤只看标题：财经摘要普遍含「早盘/收盘」等词，扫全文会误杀
    if any(term in title for term in NOISE_TERMS):
        return False
    # 政策信号门槛：没有政策类词汇的不是政策事件
    if not any(term in text for term in POLICY_SIGNAL_TERMS):
        return False
    # 来源门槛：只接受政府站点与主流财经媒体
    origin = f"{getattr(item, 'url', '') or ''} {getattr(item, 'source', '') or ''}".lower()
    if not any(domain in origin for domain in TRUSTED_DOMAINS):
        return False

    sectors = _detect_sectors(text)
    direction = _detect_direction(text)
    level = _detect_level(text)
    category = _detect_category(text)
    strength = _detect_strength(level=level, direction=direction)

    event_date = _parse_published_date(getattr(item, "published_date", None)) or date.today()
    source = str(getattr(item, "source", "") or "search").strip()[:100] or "search"
    url = str(getattr(item, "url", "") or "").strip()

    with manager.get_session() as session:
        exists = (
            session.query(MacroEvent.id)
            .filter(
                MacroEvent.event_date == event_date,
                MacroEvent.title == title[:300],
                MacroEvent.source == source,
            )
            .first()
        )
        if exists:
            return False
        session.add(
            MacroEvent(
                event_date=event_date,
                title=title[:300],
                source=source,
                url=url[:1000] or None,
                level=level,
                category=category,
                direction=direction,
                strength=strength,
                sectors=json.dumps(sectors, ensure_ascii=False),
                summary=snippet[:2000] or None,
                raw_text=f"[query={query}] {text}"[:4000],
            )
        )
        session.commit()
    return True


def _detect_sectors(text: str) -> List[str]:
    hits: List[str] = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            hits.append(sector)
    return hits


def _detect_direction(text: str) -> str:
    positive = sum(1 for term in POSITIVE_TERMS if term in text)
    negative = sum(1 for term in NEGATIVE_TERMS if term in text)
    if negative > positive:
        return "negative"
    if positive > negative:
        return "positive"
    return "neutral"


def _detect_level(text: str) -> str:
    if any(term in text for term in CENTRAL_TERMS):
        return "central"
    if any(term in text for term in MINISTRY_TERMS):
        return "ministry"
    if any(term in text for term in LOCAL_TERMS):
        return "local"
    return "ministry"


def _detect_category(text: str) -> str:
    if any(term in text for term in ("降准", "降息", "LPR", "货币政策", "流动性", "MLF")):
        return "monetary"
    if any(term in text for term in ("财政", "专项债", "减税", "赤字", "国债")):
        return "fiscal"
    if any(term in text for term in ("监管", "证监会", "处罚", "规范", "IPO")):
        return "regulatory"
    return "industry"


def _detect_strength(*, level: str, direction: str) -> int:
    base = {"central": 5, "ministry": 4, "local": 2}.get(level, 3)
    if direction == "neutral":
        base -= 1
    return max(1, min(5, base))


def _load_sectors(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def _sector_matches(industry: str, sectors: Sequence[str]) -> bool:
    """行业名与事件行业做双向包含匹配（兼容「银行」与「银行II」这类写法）。"""
    target = str(industry or "").strip()
    if not target:
        return False
    for sector in sectors:
        candidate = str(sector or "").strip()
        if not candidate:
            continue
        if candidate in target or target in candidate:
            return True
    return False


def _parse_published_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
