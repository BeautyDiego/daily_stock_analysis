# -*- coding: utf-8 -*-
"""每日选股 Top N 落库与次日（T+1）验证。

职责：
1. 调用选股管线产出 A 股 / 美股各 Top N 候选，并落库到独立表
2. 取选股日之后的第一个交易日收盘价，判定「上涨且跑赢基准」
3. 为看板提供按选股日聚合的对照数据与收益 / 胜率汇总
4. 把选股与验证结果推送到已配置的通知渠道（企业微信等）

与 BacktestService 的关系：BacktestService 评估的是个股分析决策（消费
analysis_history），本模块评估的是选股管线产出的候选，两者输入不同，
因此独立建表与实现，不在回测结果表上扩展语义。

推送渠道完全由既有通知配置驱动（WECHAT_WEBHOOK_URL 等），本模块不感知
具体渠道，也不内置任何 webhook 地址。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from src.config import Config, get_config
from src.core.trading_calendar import build_market_phase_context
from src.storage import (
    DatabaseManager,
    ScreeningPick,
    ScreeningVerification,
)

logger = logging.getLogger(__name__)

# 选股市场 -> 基准指数代码。
# cn 用上证综指（sh000001），us 用标普 500（^GSPC）。
# 可通过 SCREENING_PICKS_BENCHMARK_CN / _US 覆盖，便于切换成沪深300 或纳指。
DEFAULT_BENCHMARK_CODES: Dict[str, str] = {
    "cn": "sh000001",
    "us": "^GSPC",
}

# 取日 K 的数据源：美股走新浪美股 K 线（国内可直连），A 股走自动降级链
_MARKET_DAILY_SOURCE: Dict[str, str] = {
    "cn": "auto",
    "us": "sina_us",
}

# 支持的市场及其展示名
SUPPORTED_MARKETS: Tuple[str, ...] = ("cn", "us")

_MARKET_LABELS: Dict[str, str] = {"cn": "A股", "us": "美股", "hk": "港股"}


class ScreeningVerificationService:
    """选股落库与次日验证服务。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        db_manager: Optional[DatabaseManager] = None,
    ) -> None:
        self._config = config or get_config()
        self._db = db_manager or DatabaseManager()
        # 单次运行内的日 K 缓存，避免同一标的（尤其基准指数）重复取数
        self._history_cache: Dict[Tuple[str, str], Optional[List[Tuple[date, float, float]]]] = {}

    # ------------------------------------------------------------------
    # 选股落库
    # ------------------------------------------------------------------

    def run_daily_picks(
        self,
        *,
        markets: Optional[List[str]] = None,
        max_results: int = 5,
        strategy: Optional[str] = None,
        pick_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """跑选股并把各市场 Top N 写入 screening_picks。

        Args:
            markets: 市场列表，默认 cn + us
            max_results: 每个市场取前几名
            strategy: 选股策略 ID；None 时按市场自动挑选支持该市场的策略
            pick_date: 选股日，默认今天

        Returns:
            统计字典，含 saved / skipped / markets 分项
        """
        from src.services.screening_service import ScreeningService

        # 选股管线的候选池上限是模块级常量（默认 12，面向 LLM 排序设计，不读环境变量）。
        # A 股要做跨日去重与行业分散，12 只往往不足以在分散后凑满 Top N，运行时抬高。
        try:
            from src.services import screening_service as _screening_module

            _screening_module.DSA_SCREENING_LLM_MAX_CANDIDATES = 30
        except Exception as exc:  # noqa: BLE001 - 调整失败时退回默认候选池
            logger.debug("调整选股候选池上限失败: %s", exc)

        target_markets = [m for m in (markets or list(SUPPORTED_MARKETS))]
        default_pick_date = pick_date or date.today()
        top_n = max(int(max_results), 1)

        screening = ScreeningService(self._config)
        stats: Dict[str, Any] = {
            "pick_date": default_pick_date.isoformat(),
            "top_n": top_n,
            "markets": {},
            "saved": 0,
            "skipped": 0,
            "errors": [],
        }

        for market in target_markets:
            # 选股日必须等于入场价所属的交易日：美股在北京时间下午选股时尚未开盘，
            # 快照价来自上一个美股交易日，若按北京日期记账会让 T+1 整体后移一天。
            market_pick_date = pick_date or _resolve_pick_date(
                market, default_date=default_pick_date
            )
            market_stats: Dict[str, Any] = {
                "saved": 0,
                "skipped": 0,
                "error": None,
                "pick_date": market_pick_date.isoformat(),
            }
            try:
                # A 股多取候选，给「政策加成 + 跨日去重 + 行业分散」留足空间：
                # 因子打分容易集中在同一行业，候选太少会导致分散后凑不满 Top N
                candidate_limit = top_n * 6 if market == "cn" else top_n
                payload = screening.screen(
                    strategy=strategy or self._default_strategy(screening, market),
                    market=market,
                    max_results=candidate_limit,
                )
            except Exception as exc:  # noqa: BLE001 - 单一市场失败不影响其他市场
                logger.warning("选股失败，已跳过市场 %s: %s", market, exc)
                market_stats["error"] = str(exc)
                stats["markets"][market] = market_stats
                stats["errors"].append({"market": market, "message": str(exc)})
                continue

            picks = self._extract_picks(payload)
            if not picks:
                logger.warning("市场 %s 本次未产出候选", market)
                stats["markets"][market] = market_stats
                continue

            if market == "cn":
                picks = self._apply_cn_filters(
                    picks,
                    as_of=market_pick_date,
                    limit=top_n,
                    market_stats=market_stats,
                )
                if not picks:
                    logger.warning("市场 %s 去重后无剩余候选", market)
                    stats["markets"][market] = market_stats
                    continue

            run_id = str(payload.get("run_id") or f"{market}-{market_pick_date.isoformat()}")
            saved, skipped = self._save_picks(
                picks=picks[:top_n],
                run_id=run_id,
                market=market,
                pick_date=market_pick_date,
                strategy=strategy or payload.get("strategy"),
            )
            market_stats["saved"] = saved
            market_stats["skipped"] = skipped
            market_stats["run_id"] = run_id
            stats["saved"] += saved
            stats["skipped"] += skipped
            stats["markets"][market] = market_stats

            # A 股：仅在本次确实落库时建立跟踪；否则跟踪的会是「未入库的候选」
            if market == "cn" and saved > 0:
                try:
                    from src.services.cn_tracker.holding_tracker_service import (
                        ensure_tracked_positions,
                    )

                    market_stats["tracked"] = ensure_tracked_positions(
                        picks[:top_n],
                        pick_date=market_pick_date,
                        db=self._db,
                        market="cn",
                    )
                except Exception as exc:  # noqa: BLE001 - 跟踪失败不影响落库
                    logger.warning("建立跟踪标的失败: %s", exc)
                    stats["errors"].append(
                        {"market": market, "message": f"track: {exc}"}
                    )

        return stats

    def _apply_cn_filters(
        self,
        picks: List[Dict[str, Any]],
        *,
        as_of: date,
        limit: int,
        market_stats: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """A 股：政策加成 → 跨日去重/行业分散 → 按总分重排。"""
        from src.services.cn_tracker.dedupe_filter import filter_candidates
        from src.services.cn_tracker.industry import fill_missing_industries
        from src.services.cn_tracker.macro_policy_service import apply_policy_bonus

        # 快照不提供行业字段：先补齐，否则行业分散与政策匹配都会失效
        picks = list(fill_missing_industries(picks))

        try:
            picks = apply_policy_bonus(picks, db=self._db, as_of=as_of)
        except Exception as exc:  # noqa: BLE001 - 政策加成失败不阻断选股
            logger.warning("政策加成失败，按原始分数继续: %s", exc)

        cooldown_days = _env_int("CN_COOLDOWN_TRADING_DAYS", 10)
        max_per_industry = _env_int("CN_MAX_PER_INDUSTRY", 2)

        kept, dedupe_stats = filter_candidates(
            picks,
            db=self._db,
            as_of=as_of,
            cooldown_days=cooldown_days,
            max_per_industry=max_per_industry,
            limit=limit,
            market="cn",
        )
        market_stats["dedupe"] = {
            "input": dedupe_stats["input"],
            "kept": dedupe_stats["kept"],
            "dropped": dedupe_stats["dropped"],
            "exempted": dedupe_stats["cooldown_exempted"],
        }

        kept.sort(
            key=lambda item: -_as_float(item.get("score") or item.get("final_score") or 0)
        )
        for index, item in enumerate(kept, start=1):
            item["rank"] = index
        return kept

    @staticmethod
    def _default_strategy(screening: Any, market: str) -> str:
        """取第一个支持该市场的选股策略 ID。

        内置策略的 market_scope 区分 A 股与美股，直接取第一个策略会在
        美股市场上被管线拒绝，因此必须按 market_scope 过滤。
        """
        try:
            payload = screening.strategies()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"无法获取可用选股策略: {exc}") from exc

        items = (payload or {}).get("strategies") or []
        for item in items:
            scopes = [str(scope) for scope in (item.get("market_scope") or [])]
            if market in scopes and item.get("id"):
                return str(item["id"])
        raise ValueError(f"没有支持市场 {market} 的可用选股策略")

    @staticmethod
    def _extract_picks(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从选股响应中提取候选列表，兼容常见字段名。"""
        for key in ("candidates", "picks", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                return [item for item in value if isinstance(item, dict)]
        return []

    def _save_picks(
        self,
        *,
        picks: List[Dict[str, Any]],
        run_id: str,
        market: str,
        pick_date: date,
        strategy: Optional[str],
    ) -> Tuple[int, int]:
        """幂等写入选股结果，返回 (saved, skipped)。

        同一天同一市场只保留一份结果：只要当天该市场已有记录，就说明当天的选股
        已完成，后续运行（cron 重试、手工重复执行）直接跳过。仅按「选股日 + 市场
        + 代码」去重是不够的——多次运行会选出不同候选，叠加后当天的记录数会超过
        Top N，rank 也会重复。
        """
        if not picks:
            return 0, 0

        saved = 0
        skipped = 0
        with self._db.get_session() as session:
            existing_count = (
                session.query(ScreeningPick)
                .filter(
                    ScreeningPick.pick_date == pick_date,
                    ScreeningPick.market == market,
                )
                .count()
            )
            if existing_count:
                logger.info(
                    "当天该市场已有选股记录，跳过写入: pick_date=%s market=%s existing=%d",
                    pick_date,
                    market,
                    existing_count,
                )
                return 0, len(picks)

            for index, item in enumerate(picks, start=1):
                code = str(item.get("code") or "").strip()
                if not code:
                    skipped += 1
                    continue

                session.add(
                    ScreeningPick(
                        run_id=run_id,
                        pick_date=pick_date,
                        market=market,
                        strategy=strategy,
                        code=code,
                        name=str(item.get("name") or ""),
                        rank=int(item.get("rank") or index),
                        entry_price=_as_float(item.get("price") or item.get("entry_price")),
                        final_score=_as_float(item.get("score") or item.get("final_score")),
                        ranking_reason=str(item.get("ranking_reason") or item.get("reason") or ""),
                        factor_scores=_json_or_none(item.get("factor_scores")),
                        policy_hits=_json_or_none(item.get("policy_hits")),
                        dedupe_note=str(item.get("dedupe_note") or "")[:255] or None,
                        strategy_version=str(item.get("strategy_version") or "")[:64] or None,
                    )
                )
                saved += 1
            if saved:
                session.commit()
        return saved, skipped

    # ------------------------------------------------------------------
    # 次日（T+1）验证
    # ------------------------------------------------------------------

    def verify_picks(
        self,
        *,
        market: Optional[str] = None,
        force: bool = False,
        limit: int = 200,
        as_of: Optional[date] = None,
    ) -> Dict[str, Any]:
        """对尚未验证的选股记录做 T+1 验证。

        验证口径：取选股日之后的**第一个交易日**收盘价，相对入场价计算收益，
        并与同一交易日基准指数的开盘->收盘涨幅比较。

        T+1 的 K 线尚未产生时记为 insufficient，不写库，下次运行自动重试。

        Args:
            market: 限定市场；None 表示全部
            force: 是否重算已验证记录
            limit: 单次最多处理条数
            as_of: 运行日期，仅用于错误记录的 verify_date，默认今天

        Returns:
            统计字典
        """
        run_date = as_of or date.today()
        pending = self._list_pending_picks(market=market, limit=limit, force=force)
        stats: Dict[str, Any] = {
            "verify_date": run_date.isoformat(),
            "processed": 0,
            "done": 0,
            "insufficient": 0,
            "errors": 0,
        }

        for pick in pending:
            stats["processed"] += 1
            try:
                outcome = self._verify_one(pick, run_date)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                logger.warning(
                    "选股验证失败 code=%s market=%s: %s", pick.code, pick.market, exc
                )
                self._save_verification(
                    pick=pick,
                    verify_date=run_date,
                    eval_status="error",
                    error_message=str(exc),
                )
                stats["errors"] += 1
                continue

            if outcome == "insufficient":
                stats["insufficient"] += 1
            else:
                stats["done"] += 1

        return stats

    def _list_pending_picks(
        self,
        *,
        market: Optional[str],
        limit: int,
        force: bool,
    ) -> List[ScreeningPick]:
        with self._db.get_session() as session:
            query = session.query(ScreeningPick)
            if market:
                query = query.filter(ScreeningPick.market == market)
            if not force:
                verified_ids = session.query(ScreeningVerification.pick_id).filter(
                    ScreeningVerification.eval_status == "done"
                )
                query = query.filter(~ScreeningPick.id.in_(verified_ids))
            return query.order_by(ScreeningPick.pick_date.desc()).limit(int(limit)).all()

    def _verify_one(self, pick: ScreeningPick, run_date: date) -> str:
        """验证单条选股，返回 done / insufficient。"""
        benchmark_code = self._benchmark_code(pick.market)

        stock_rows = self._history_rows(pick.code, pick.market)
        stock_row = _row_after(stock_rows, pick.pick_date)
        if stock_row is None:
            # T+1 的 K 线还没出来，本条保持待验证，下次运行重试
            return "insufficient"

        verify_date, _, stock_close = stock_row
        entry = pick.entry_price
        if entry is None or entry <= 0:
            raise ValueError(f"选股记录缺少有效入场价: {pick.code}")

        return_pct = (stock_close - entry) / entry * 100.0

        benchmark_open = benchmark_close = None
        benchmark_return_pct = None
        beat_benchmark: Optional[bool] = None
        note: Optional[str] = None

        if benchmark_code:
            bench_row = _row_after(self._history_rows(benchmark_code, pick.market), pick.pick_date)
            if bench_row is not None:
                _, benchmark_open, benchmark_close = bench_row
                if benchmark_open and benchmark_open > 0:
                    benchmark_return_pct = (benchmark_close - benchmark_open) / benchmark_open * 100.0
            if benchmark_return_pct is None:
                note = "基准数据缺失，仅按上涨判定"
                logger.warning(
                    "基准 %s 在 %s 之后无可用行情，code=%s 仅按上涨判定",
                    benchmark_code, pick.pick_date, pick.code,
                )

        is_up = return_pct > 0
        excess_return_pct = (
            return_pct - benchmark_return_pct
            if benchmark_return_pct is not None
            else None
        )
        if benchmark_return_pct is not None:
            beat_benchmark = return_pct > benchmark_return_pct
            met_expectation = bool(is_up and beat_benchmark)
        else:
            met_expectation = bool(is_up)

        self._save_verification(
            pick=pick,
            verify_date=verify_date,
            eval_status="done",
            close_price=stock_close,
            return_pct=return_pct,
            benchmark_code=benchmark_code,
            benchmark_open=benchmark_open,
            benchmark_close=benchmark_close,
            benchmark_return_pct=benchmark_return_pct,
            excess_return_pct=excess_return_pct,
            is_up=is_up,
            beat_benchmark=beat_benchmark,
            met_expectation=met_expectation,
            error_message=note,
        )
        return "done"

    def _benchmark_code(self, market: str) -> Optional[str]:
        env_key = f"SCREENING_PICKS_BENCHMARK_{str(market).upper()}"
        return os.getenv(env_key) or DEFAULT_BENCHMARK_CODES.get(market)

    def _history_rows(
        self, code: str, market: str
    ) -> Optional[List[Tuple[date, float, float]]]:
        """取日 K 并规整为 [(date, open, close)] 升序列表，带运行内缓存。"""
        cache_key = (code, market)
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        rows: Optional[List[Tuple[date, float, float]]] = None
        try:
            from src.services.screening.daily import fetch_daily_history

            source = _MARKET_DAILY_SOURCE.get(market, "auto")
            frame = fetch_daily_history(code, lookback_days=120, source=source)
            rows = _normalize_frame(frame)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取日K失败 code=%s market=%s: %s", code, market, exc)
            rows = None

        self._history_cache[cache_key] = rows
        return rows

    def _save_verification(
        self,
        *,
        pick: ScreeningPick,
        verify_date: date,
        eval_status: str,
        close_price: Optional[float] = None,
        return_pct: Optional[float] = None,
        benchmark_code: Optional[str] = None,
        benchmark_open: Optional[float] = None,
        benchmark_close: Optional[float] = None,
        benchmark_return_pct: Optional[float] = None,
        excess_return_pct: Optional[float] = None,
        is_up: Optional[bool] = None,
        beat_benchmark: Optional[bool] = None,
        met_expectation: Optional[bool] = None,
        error_message: Optional[str] = None,
    ) -> None:
        with self._db.get_session() as session:
            existing = (
                session.query(ScreeningVerification)
                .filter(
                    ScreeningVerification.pick_id == pick.id,
                    ScreeningVerification.verify_date == verify_date,
                )
                .first()
            )
            if existing is None:
                existing = ScreeningVerification(
                    pick_id=pick.id,
                    code=pick.code,
                    market=pick.market,
                    pick_date=pick.pick_date,
                    verify_date=verify_date,
                    entry_price=pick.entry_price,
                )
                session.add(existing)

            existing.eval_status = eval_status
            existing.close_price = close_price
            existing.return_pct = return_pct
            existing.benchmark_code = benchmark_code
            existing.benchmark_open = benchmark_open
            existing.benchmark_close = benchmark_close
            existing.benchmark_return_pct = benchmark_return_pct
            existing.excess_return_pct = excess_return_pct
            existing.is_up = is_up
            existing.beat_benchmark = beat_benchmark
            existing.met_expectation = met_expectation
            existing.error_message = error_message
            existing.verified_at = datetime.now()
            session.commit()

    # ------------------------------------------------------------------
    # 大屏查询
    # ------------------------------------------------------------------

    def get_dashboard(self, *, limit_days: int = 30) -> Dict[str, Any]:
        """按选股日聚合，返回大屏所需的选股 + 验证对照数据。"""
        with self._db.get_session() as session:
            picks = (
                session.query(ScreeningPick)
                .order_by(
                    ScreeningPick.pick_date.desc(),
                    ScreeningPick.market,
                    ScreeningPick.rank,
                )
                .limit(int(limit_days) * 12)
                .all()
            )
            if not picks:
                return {"days": [], "summary": _empty_summary()}

            pick_ids = [p.id for p in picks]
            verifications = (
                session.query(ScreeningVerification)
                .filter(ScreeningVerification.pick_id.in_(pick_ids))
                .all()
            )

        verify_by_pick = {v.pick_id: v for v in verifications}

        grouped: Dict[str, Dict[str, Any]] = {}
        for pick in picks:
            day = grouped.setdefault(
                pick.pick_date.isoformat(),
                {
                    "pick_date": pick.pick_date.isoformat(),
                    "markets": {},
                    "met_count": 0,
                    "up_count": 0,
                    "verified_count": 0,
                    "total_count": 0,
                    "return_sum": 0.0,
                    "excess_sum": 0.0,
                },
            )
            market_bucket = day["markets"].setdefault(
                pick.market,
                {"market": pick.market, "picks": []},
            )
            verification = verify_by_pick.get(pick.id)
            market_bucket["picks"].append(_pick_view(pick, verification))
            day["total_count"] += 1
            if verification is not None and verification.eval_status == "done":
                day["verified_count"] += 1
                if verification.met_expectation:
                    day["met_count"] += 1
                if verification.is_up:
                    day["up_count"] += 1
                if verification.return_pct is not None:
                    day["return_sum"] += float(verification.return_pct)
                if verification.excess_return_pct is not None:
                    day["excess_sum"] += float(verification.excess_return_pct)

        days = sorted(grouped.values(), key=lambda item: item["pick_date"], reverse=True)
        days = days[: int(limit_days)]
        return {"days": [_day_view(day) for day in days], "summary": _build_summary(days)}

    # ------------------------------------------------------------------
    # 结果推送
    # ------------------------------------------------------------------

    def notify_daily_picks(self, *, pick_date: Optional[date] = None) -> bool:
        """推送当日选股结果。

        ``pick_date`` 为空时按市场各取最近一次选股日：A 股与美股可能不在同一个
        交易日（美股在北京时间下午选股时，取价来自上一个美股交易日），只查
        ``date.today()`` 会整体漏掉美股。

        Returns:
            是否推送成功；无内容或未配置渠道时返回 False
        """
        if pick_date is not None:
            content = self.build_picks_report(pick_date)
        else:
            parts = [self.build_picks_report(item) for item in self._latest_pick_dates()]
            content = "\n\n".join(part for part in parts if part)
        if not content:
            logger.info("无当日选股记录，跳过推送")
            return False
        return self._send(content)

    def _latest_pick_dates(self) -> List[date]:
        """各市场最近一次选股日，按日期倒序。"""
        with self._db.get_session() as session:
            rows = session.query(ScreeningPick.market, ScreeningPick.pick_date).all()
        latest: Dict[str, date] = {}
        for market, pick_date in rows:
            if not isinstance(pick_date, date):
                continue
            key = str(market)
            if key not in latest or pick_date > latest[key]:
                latest[key] = pick_date
        return sorted(set(latest.values()), reverse=True)

    def notify_verification_result(self, *, limit_days: int = 2) -> bool:
        """推送最近一批选股的次日验证结果。

        ``limit_days`` 默认取两个交易日：A 股与美股的选股日相邻但可能不同天。
        """
        dashboard = self.get_dashboard(limit_days=limit_days)
        days = dashboard.get("days") or []
        if not days:
            logger.info("无选股验证记录，跳过推送")
            return False

        content = self.build_verification_report(days)
        if not content:
            return False
        return self._send(content)

    def _send(self, content: str) -> bool:
        if not getattr(self._config, "screening_picks_notify_enabled", True):
            logger.info("选股推送已关闭（SCREENING_PICKS_NOTIFY_ENABLED=false），跳过")
            return False
        try:
            from src.notification import get_notification_service

            result = get_notification_service().send_with_results(
                content, route_type="report"
            )
        except Exception as exc:  # noqa: BLE001 - 推送失败不影响主流程
            logger.warning("选股结果推送失败: %s", exc)
            return False

        success = bool(getattr(result, "success", False))
        logger.info("选股结果推送完成: success=%s", success)
        return success

    def build_picks_report(self, pick_date: date) -> str:
        """把某日的选股结果渲染为 Markdown 推送正文。"""
        with self._db.get_session() as session:
            picks = (
                session.query(ScreeningPick)
                .filter(ScreeningPick.pick_date == pick_date)
                .order_by(ScreeningPick.market, ScreeningPick.rank)
                .all()
            )
        if not picks:
            return ""

        buckets: Dict[str, List[ScreeningPick]] = {}
        for pick in picks:
            buckets.setdefault(pick.market, []).append(pick)

        lines: List[str] = [f"每日选股 · {pick_date.isoformat()}", ""]
        for market in sorted(buckets):
            lines.append(f"【{_market_label(market)}】")
            for pick in buckets[market]:
                price = f"{pick.entry_price:.2f}" if pick.entry_price else "--"
                score = f"{pick.final_score:.2f}" if pick.final_score is not None else "--"
                lines.append(
                    f"{pick.rank}. {pick.name or pick.code}({pick.code}) "
                    f"入场 {price} 评分 {score}"
                )
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def build_verification_report(days: List[Dict[str, Any]]) -> str:
        """把选股验证结果渲染为 Markdown 推送正文。"""
        if not days:
            return ""

        latest = days[0]
        lines: List[str] = [
            f"选股次日验证 · {latest.get('pick_date')}",
            "",
        ]

        for market_key in SUPPORTED_MARKETS:
            group = (latest.get("markets") or {}).get(market_key)
            if not group or not group.get("picks"):
                continue
            lines.append(f"【{_market_label(market_key)}】")
            for item in group["picks"]:
                verification = item.get("verification") or {}
                status = _verdict_text(verification)
                lines.append(
                    f"{item.get('rank')}. {item.get('name') or item.get('code')}"
                    f"({item.get('code')}) {status}"
                )
                detail = _verdict_detail(verification)
                if detail:
                    lines.append(f"   {detail}")
            lines.append("")

        verified = latest.get("verified_count") or 0
        met = latest.get("met_count") or 0
        up = latest.get("up_count") or 0
        avg_return = latest.get("avg_return_pct")
        if verified:
            lines.append(f"符合预期 {met}/{verified} · 上涨 {up}/{verified}")
            if avg_return is not None:
                lines.append(f"平均收益 {avg_return:+.2f}%")
        return "\n".join(lines).strip()


def _empty_summary() -> Dict[str, Any]:
    return {
        "total_picks": 0,
        "verified_picks": 0,
        "met_expectation": 0,
        "up_count": 0,
        "hit_rate_pct": None,
        "win_rate_pct": None,
        "avg_return_pct": None,
        "avg_excess_return_pct": None,
        "tracked_days": 0,
    }


def _build_summary(days: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = sum(day["total_count"] for day in days)
    verified = sum(day["verified_count"] for day in days)
    met = sum(day["met_count"] for day in days)
    up = sum(day["up_count"] for day in days)
    return_sum = sum(day["return_sum"] for day in days)
    excess_sum = sum(day["excess_sum"] for day in days)
    return {
        "total_picks": total,
        "verified_picks": verified,
        "met_expectation": met,
        "up_count": up,
        # 命中率：上涨且跑赢基准的比例
        "hit_rate_pct": _ratio(met, verified),
        # 胜率：次日上涨的比例
        "win_rate_pct": _ratio(up, verified),
        "avg_return_pct": round(return_sum / verified, 2) if verified else None,
        "avg_excess_return_pct": round(excess_sum / verified, 2) if verified else None,
        "tracked_days": len(days),
    }


def _day_view(day: Dict[str, Any]) -> Dict[str, Any]:
    """把内部聚合结果转成可序列化的看板日视图。"""
    verified = day["verified_count"]
    return {
        "pick_date": day["pick_date"],
        "markets": day["markets"],
        "total_count": day["total_count"],
        "verified_count": verified,
        "met_count": day["met_count"],
        "up_count": day["up_count"],
        "hit_rate_pct": _ratio(day["met_count"], verified),
        "win_rate_pct": _ratio(day["up_count"], verified),
        "avg_return_pct": round(day["return_sum"] / verified, 2) if verified else None,
        "avg_excess_return_pct": round(day["excess_sum"] / verified, 2) if verified else None,
    }


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator * 100.0, 2) if denominator else None


def _pick_view(pick: ScreeningPick, verification: Optional[ScreeningVerification]) -> Dict[str, Any]:
    return {
        "code": pick.code,
        "name": pick.name,
        "rank": pick.rank,
        "market": pick.market,
        "strategy": pick.strategy,
        "entry_price": pick.entry_price,
        "final_score": pick.final_score,
        "ranking_reason": pick.ranking_reason,
        "verification": None if verification is None else {
            "verify_date": verification.verify_date.isoformat(),
            "eval_status": verification.eval_status,
            "close_price": verification.close_price,
            "return_pct": verification.return_pct,
            "benchmark_code": verification.benchmark_code,
            "benchmark_return_pct": verification.benchmark_return_pct,
            "excess_return_pct": verification.excess_return_pct,
            "is_up": verification.is_up,
            "beat_benchmark": verification.beat_benchmark,
            "met_expectation": verification.met_expectation,
            "error_message": verification.error_message,
        },
    }


def _resolve_pick_date(market: str, *, default_date: date) -> date:
    """返回该市场的选股日，也就是入场价所属的交易日。

    A 股在北京时间收盘后选股，快照价就是当日收盘价，选股日即当天；
    美股在北京时间下午选股时市场尚未开盘，快照价来自上一个美股交易日，
    因此要按美股日历把选股日落到那个已完成交易日上。否则验证会去找
    「选股日之后」的 K 线，等价于整体后移一天（表现为长期 insufficient）。
    """
    if market != "us":
        return default_date
    try:
        context = build_market_phase_context(market="us")
    except Exception as exc:  # noqa: BLE001 - 交易日历不可用时退回默认日
        logger.warning("无法解析美股交易日历，回退默认选股日: %s", exc)
        return default_date
    effective = getattr(context, "effective_daily_bar_date", None)
    if isinstance(effective, date):
        return effective
    return default_date


def _market_label(market: str) -> str:
    return _MARKET_LABELS.get(market, str(market).upper())


def _verdict_text(verification: Dict[str, Any]) -> str:
    """单只股票的判定文案。"""
    if not verification:
        return "待验证"
    status = verification.get("eval_status")
    if status == "error":
        return "取价失败"
    if status != "done":
        return "数据不足"
    if verification.get("met_expectation"):
        return "符合预期"
    if not verification.get("is_up"):
        return "未上涨"
    return "跑输基准"


def _verdict_detail(verification: Dict[str, Any]) -> str:
    """单只股票的收益明细。"""
    if not verification or verification.get("eval_status") != "done":
        return ""
    return_pct = verification.get("return_pct")
    if return_pct is None:
        return ""
    text = f"收益 {return_pct:+.2f}%"
    benchmark_pct = verification.get("benchmark_return_pct")
    if benchmark_pct is not None:
        text += f" / 基准 {benchmark_pct:+.2f}%"
        text += f" (超额 {(verification.get('excess_return_pct') or 0.0):+.2f}%)"
    return text


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读取整型环境变量，非法值回退默认值。

    一条写错的配置（如 ``CN_COOLDOWN_TRADING_DAYS=ten``）不应该让整次选股失败。
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("环境变量 %s 不是整数（%r），已回退默认值 %s", name, raw, default)
        return default
    return max(minimum, value)


def _json_or_none(value: Any) -> Optional[str]:
    """把可序列化对象写成 JSON 字符串；空值返回 None。"""
    if not value:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # 过滤 NaN


def _normalize_frame(frame: Any) -> Optional[List[Tuple[date, float, float]]]:
    """把日 K DataFrame 规整为 [(date, open, close)] 升序列表。"""
    if frame is None or getattr(frame, "empty", True):
        return None
    rows: List[Tuple[date, float, float]] = []
    for _, row in frame.iterrows():
        row_date = _coerce_date(row.get("date"))
        open_price = _as_float(row.get("open"))
        close_price = _as_float(row.get("close"))
        if row_date is None or close_price is None:
            continue
        rows.append((row_date, open_price if open_price is not None else close_price, close_price))
    if not rows:
        return None
    rows.sort(key=lambda item: item[0])
    return rows


def _coerce_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _row_after(
    rows: Optional[List[Tuple[date, float, float]]], after: date
) -> Optional[Tuple[date, float, float]]:
    """取 after 之后的首个交易日记录（严格的次日语义）。"""
    if not rows:
        return None
    for row in rows:
        if row[0] > after:
            return row
    return None
