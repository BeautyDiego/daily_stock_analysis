# -*- coding: utf-8 -*-
"""A 股模拟盘：初始资金、按推荐建仓、按复盘调仓、每日估值。

复用项目既有的 portfolio 账本，不另建表：

| 用途 | 复用对象 |
| --- | --- |
| 账户 | ``portfolio_accounts``（名称为「A股模拟盘」） |
| 入金 | ``portfolio_cash_ledger``（direction=in） |
| 成交 | ``portfolio_trades``（含佣金 fee / 印花税 tax） |
| 持仓 | ``portfolio_positions``（FIFO replay 产出） |
| 净值 | ``portfolio_daily_snapshots.total_equity`` |

交易约定（模拟盘，非实盘）：

- 成交价取**当日收盘价**，不计滑点与冲击成本；
- A 股 1 手 = 100 股，买入按预算向下取整到手；
- 佣金万 2.5（单笔最低 5 元），卖出另收印花税千 1；
- 同一账户 + 同一交易日 + 同一标的 + 同一方向只下单一次（``dedup_hash`` 幂等）。

因此模拟盘收益是**理论上限**，实盘要在此基础上扣掉滑点与执行偏差。
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.services.portfolio_service import PortfolioService
from src.storage import (
    DatabaseManager,
    HoldingReview,
    PortfolioDailySnapshot,
    PortfolioPosition,
    PortfolioTrade,
    ScreeningPick,
    StockDaily,
)

logger = logging.getLogger(__name__)

PAPER_ACCOUNT_NAME = "A股模拟盘"
DEFAULT_INITIAL_CASH = 20000.0
# 仓位数量跟随 SCREENING_PICKS_TOP_N；这里的默认值仅在配置缺失时使用
DEFAULT_POSITION_SLOTS = 5
MAX_POSITION_SLOTS = 20
LOT_SIZE = 100

COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_TAX_RATE = 0.001
# 滑点：模拟盘按收盘价成交，买上浮 / 卖下压 0.1%，避免系统性高估收益
SLIPPAGE_RATE = 0.001

# 初始资金视为「开户时已存入」的历史入金：这样回放任意 as_of 都能取到现金，
# 不需要每次调用都对齐入金日期（现金不计息，不影响收益计算）。
PAPER_CASH_START_DATE = date(2000, 1, 1)

# 信号时效：选股日与执行日相差超过该天数就不再建仓（覆盖长假，
# 同时避免把半个月前的旧信号当成新信号执行）
MAX_SIGNAL_AGE_DAYS = 10


def ensure_paper_account(
    *,
    initial_cash: float = DEFAULT_INITIAL_CASH,
    db: Optional[DatabaseManager] = None,
) -> Dict[str, Any]:
    """创建或复用模拟盘账户；仅在首次创建时入金。"""
    service = PortfolioService()
    existing = _find_paper_account(service)
    if existing is not None:
        return existing

    account = service.create_account(
        name=PAPER_ACCOUNT_NAME,
        broker="paper",
        market="cn",
        base_currency="CNY",
    )
    service.record_cash_ledger(
        account_id=int(account["id"]),
        event_date=PAPER_CASH_START_DATE,
        direction="in",
        amount=float(initial_cash),
        currency="CNY",
        note="模拟盘初始资金",
    )
    logger.info("模拟盘账户已创建: id=%s 初始资金=%.2f", account["id"], initial_cash)
    return account


def run_daily_paper(
    *,
    as_of: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
) -> Dict[str, Any]:
    """执行一个交易日的模拟盘操作：卖出 → 买入 → 估值。

    卖出依据当日复盘建议（exit 清仓 / reduce 减半），买入依据当日选股结果。
    没有任何推荐或复盘时也会做一次估值，保证净值曲线连续。
    """
    manager = db or DatabaseManager.get_instance()
    trade_date = as_of or date.today()
    account = ensure_paper_account(db=manager)
    account_id = int(account["id"])
    portfolio = PortfolioService()

    def _snapshot() -> Dict[str, Any]:
        return portfolio.get_portfolio_snapshot(
            account_id=account_id,
            as_of=trade_date,
            include_realtime=False,
        )

    # replay 一次，确保 portfolio_positions 反映到 trade_date 为止的成交
    _snapshot()

    sells = _execute_sells(portfolio, account_id, trade_date, db=manager)

    # 卖出后现金增加，重新估值后再决定买入预算
    after_sells = _snapshot()
    buys = _execute_buys(
        portfolio,
        account_id,
        trade_date,
        db=manager,
        cash=float(after_sells.get("total_cash") or 0.0),
        equity=float(after_sells.get("total_equity") or 0.0),
    )

    snapshot = _snapshot()
    summary = _extract_account_snapshot(snapshot, account_id, manager)

    # 把实际持仓同步到跟踪表：否则复盘只覆盖「当日选股」，模拟盘持有的历史标的
    # 永远拿不到持有建议（表现为卖出信号一直匹配不上）。
    holdings = summary.get("positions") or []
    if holdings:
        _sync_tracked_holdings(holdings, trade_date=trade_date, db=manager)

    logger.info(
        "模拟盘交易日完成: date=%s buys=%s sells=%s equity=%.2f cash=%.2f",
        trade_date.isoformat(),
        len(buys),
        len(sells),
        summary.get("total_equity", 0.0),
        summary.get("total_cash", 0.0),
    )
    return {
        "trade_date": trade_date.isoformat(),
        "account_id": account_id,
        "buys": buys,
        "sells": sells,
        "snapshot": summary,
    }


def get_paper_report(
    *,
    as_of: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
    curve_days: int = 30,
    trade_limit: int = 20,
) -> Dict[str, Any]:
    """模拟盘概览：账户净值、持仓、净值曲线与最近成交。"""
    manager = db or DatabaseManager.get_instance()
    report_date = as_of or date.today()
    service = PortfolioService()
    account = _find_paper_account(service)
    if account is None:
        return {"initialized": False, "message": "模拟盘尚未初始化，先执行 --paper-init"}

    account_id = int(account["id"])
    snapshot = service.get_portfolio_snapshot(
        account_id=account_id,
        as_of=report_date,
        include_realtime=False,
        read_only=True,
    )
    summary = _extract_account_snapshot(snapshot, account_id, manager)

    with manager.get_session() as session:
        curve_rows = (
            session.query(PortfolioDailySnapshot)
            .filter(PortfolioDailySnapshot.account_id == account_id)
            .order_by(PortfolioDailySnapshot.snapshot_date.desc())
            .limit(max(1, int(curve_days)))
            .all()
        )
    curve = [
        {
            "date": row.snapshot_date.isoformat(),
            "total_equity": float(row.total_equity or 0.0),
            "total_cash": float(row.total_cash or 0.0),
            "market_value": float(row.total_market_value or 0.0),
        }
        for row in reversed(curve_rows)
    ]

    try:
        trades = service.list_trade_events(
            account_id=account_id, page=1, page_size=max(1, int(trade_limit))
        ).get("items") or []
    except Exception as exc:  # noqa: BLE001 - 交易流水只用于展示
        logger.warning("读取模拟盘成交流水失败: %s", exc)
        trades = []

    initial_cash = DEFAULT_INITIAL_CASH
    positions = summary.get("positions") or []
    # 盘中成交价是实时价，持仓若仍按历史收盘价估值，会把两边混算成虚假浮盈
    _refresh_positions_with_live_price(positions)

    cash = float(summary.get("total_cash") or 0.0)
    market_value = sum(
        float(item.get("last_price") or 0.0) * float(item.get("quantity") or 0.0)
        for item in positions
    )
    equity = cash + market_value if positions else float(summary.get("total_equity") or 0.0)
    summary["total_market_value"] = round(market_value, 2)
    summary["total_equity"] = round(equity, 2)

    return {
        "initialized": True,
        "account": account,
        "as_of": report_date.isoformat(),
        "initial_cash": initial_cash,
        "snapshot": summary,
        "return_pct": round((equity - initial_cash) / initial_cash * 100, 2)
        if initial_cash > 0
        else None,
        "positions": positions,
        "curve": curve,
        "trades": trades,
    }


def _refresh_positions_with_live_price(positions: List[Dict[str, Any]]) -> None:
    """用实时价刷新持仓现价与浮盈；取不到时保持原有收盘价。"""
    if not positions:
        return
    try:
        from src.services.cn_tracker.paper_bot_service import fetch_realtime_prices

        quotes = fetch_realtime_prices(
            [str(item.get("symbol") or "") for item in positions]
        )
    except Exception as exc:  # noqa: BLE001 - 拉不到就沿用收盘价
        logger.debug("刷新实时价失败: %s", exc)
        return

    for item in positions:
        quote = quotes.get(str(item.get("symbol") or "")) or {}
        price = quote.get("price")
        cost = float(item.get("avg_cost") or 0.0)
        quantity = float(item.get("quantity") or 0.0)
        if not price or float(price) <= 0:
            continue
        item["last_price"] = float(price)
        if cost > 0:
            item["unrealized_pnl"] = round((float(price) - cost) * quantity, 2)


def build_paper_report_text(report: Dict[str, Any]) -> str:
    """把模拟盘概览渲染为 Markdown（可用于推送）。"""
    if not report.get("initialized"):
        return ""

    snapshot = report.get("snapshot") or {}
    initial = report.get("initial_cash") or DEFAULT_INITIAL_CASH
    equity = snapshot.get("total_equity") or 0.0
    cash = snapshot.get("total_cash") or 0.0
    market_value = snapshot.get("total_market_value") or 0.0
    return_pct = report.get("return_pct")

    lines = [f"模拟盘日报 · {report.get('as_of')}", ""]
    pct_text = f"{return_pct:+.2f}%" if isinstance(return_pct, (int, float)) else "--"
    lines.append(
        f"总资产 {equity:.2f} 元（初始 {initial:.0f} 元，"
        f"收益 {equity - initial:+.2f} 元 / {pct_text}）"
    )
    lines.append(f"持仓市值 {market_value:.2f} 元｜可用现金 {cash:.2f} 元")

    positions = report.get("positions") or []
    if positions:
        lines.append("")
        lines.append("【持仓】")
        for row in positions:
            pnl = row.get("unrealized_pnl") or 0.0
            lines.append(
                f"- {row.get('symbol')} {row.get('quantity'):.0f} 股｜"
                f"成本 {row.get('avg_cost'):.2f}｜现价 {row.get('last_price'):.2f}｜"
                f"浮盈 {pnl:+.2f}"
            )
    else:
        lines.append("")
        lines.append("当前空仓。")

    lines.append("")
    lines.append(
        "> 模拟成交按「信号次日开盘价」并计滑点与手续费；一字板与停牌日不成交，"
        "仅供研究参考。"
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 内部实现
# ----------------------------------------------------------------------


def _find_paper_account(service: PortfolioService) -> Optional[Dict[str, Any]]:
    for account in service.list_accounts():
        if account.get("name") == PAPER_ACCOUNT_NAME:
            return account
    return None


def _execute_sells(
    portfolio: PortfolioService,
    account_id: int,
    trade_date: date,
    *,
    db: DatabaseManager,
) -> List[Dict[str, Any]]:
    with db.get_session() as session:
        reviews = (
            session.query(HoldingReview.code, HoldingReview.advice)
            .filter(HoldingReview.review_date == trade_date)
            .all()
        )
    if not reviews:
        return []

    positions = _load_positions(db, account_id)
    executed: List[Dict[str, Any]] = []

    for code, advice in reviews:
        advice = str(advice or "")
        if advice not in ("exit", "reduce"):
            continue
        code = str(code)
        position = positions.get(code)
        if not position:
            continue

        available = int(position["quantity"] // LOT_SIZE) * LOT_SIZE
        if available < LOT_SIZE:
            continue

        if advice == "exit":
            quantity = available
        else:
            # 减半：向下取整到整手。若减半不足一手（持仓 ≤ 200 股），
            # 直接清仓 —— 否则会剩下 100 股既减不动也管不了。
            quantity = int(available // (LOT_SIZE * 2)) * LOT_SIZE
            if quantity < LOT_SIZE:
                quantity = available
        if quantity < LOT_SIZE:
            continue

        price, status = _tradable_price(db, code, trade_date, "sell")
        if price is None:
            logger.warning(
                "模拟盘卖出未成交（%s）: code=%s advice=%s", status, code, advice
            )
            continue

        amount = price * quantity
        fee = max(COMMISSION_MIN, amount * COMMISSION_RATE)
        tax = amount * STAMP_TAX_RATE
        try:
            portfolio.record_trade(
                account_id=account_id,
                symbol=code,
                trade_date=trade_date,
                side="sell",
                quantity=float(quantity),
                price=float(price),
                fee=round(fee, 2),
                tax=round(tax, 2),
                market="cn",
                currency="CNY",
                dedup_hash=_dedup_hash(account_id, trade_date, code, "sell"),
                note=f"复盘建议 {advice}",
            )
            executed.append(
                {
                    "code": code,
                    "side": "sell",
                    "quantity": quantity,
                    "price": price,
                    "advice": advice,
                }
            )
            # 清仓后把跟踪状态改成 closed，避免复盘继续对已了结标的出建议
            if advice == "exit":
                try:
                    from src.services.cn_tracker.holding_tracker_service import (
                        set_position_status,
                    )

                    set_position_status(
                        code, "closed", exit_price=price, db=db
                    )
                except Exception as sync_exc:  # noqa: BLE001
                    logger.debug("同步清仓状态失败 code=%s: %s", code, sync_exc)
        except Exception as exc:  # noqa: BLE001 - 单笔失败不影响其它
            logger.warning("模拟盘卖出失败 code=%s: %s", code, exc)
    return executed


def _execute_buys(
    portfolio: PortfolioService,
    account_id: int,
    trade_date: date,
    *,
    db: DatabaseManager,
    cash: float,
    equity: float,
) -> List[Dict[str, Any]]:
    with db.get_session() as session:
        # 选股在 T 日收盘后产生，最早只能 T+1 开盘买入，
        # 所以取「严格早于执行日」的最近一个选股日作为买入信号。
        signal_row = (
            session.query(ScreeningPick.pick_date)
            .filter(
                ScreeningPick.market == "cn",
                ScreeningPick.pick_date < trade_date,
            )
            .order_by(ScreeningPick.pick_date.desc())
            .first()
        )
        if signal_row is None:
            logger.info("模拟盘买入跳过：%s 之前没有选股记录", trade_date)
            return []
        signal_date = signal_row[0]
        if (trade_date - signal_date).days > MAX_SIGNAL_AGE_DAYS:
            logger.info(
                "模拟盘买入跳过：信号日 %s 距执行日 %s 超过 %d 天",
                signal_date,
                trade_date,
                MAX_SIGNAL_AGE_DAYS,
            )
            return []
        picks = (
            session.query(ScreeningPick.code, ScreeningPick.name)
            .filter(
                ScreeningPick.pick_date == signal_date,
                ScreeningPick.market == "cn",
            )
            .order_by(ScreeningPick.rank)
            .all()
        )
    if not picks:
        return []
    logger.info("模拟盘买入信号日: %s → 执行日 %s", signal_date, trade_date)

    if cash <= 0 or equity <= 0:
        logger.warning("模拟盘买入跳过：账户估值不可用 cash=%.2f equity=%.2f", cash, equity)
        return []

    positions = _load_positions(db, account_id)

    # 每个仓位等额预算；已有持仓不重复买入
    slots = _position_slots()
    budget = equity / slots
    logger.info(
        "模拟盘建仓预算: 仓位=%s 单仓=%.2f 可用现金=%.2f", slots, budget, cash
    )
    executed: List[Dict[str, Any]] = []

    for code, name in picks:
        code = str(code)
        if code in positions:
            continue
        # 同一批信号只执行一次：否则信号日之后的每个交易日都会重复建仓
        # （尤其是「当天卖出后又被买回来」这种错误）
        buy_key = _dedup_hash(account_id, signal_date, code, "buy")
        with db.get_session() as session:
            already_bought = (
                session.query(PortfolioTrade.id)
                .filter(
                    PortfolioTrade.account_id == account_id,
                    PortfolioTrade.dedup_hash == buy_key,
                )
                .first()
            )
        if already_bought is not None:
            continue
        price, status = _tradable_price(db, code, trade_date, "buy")
        if price is None:
            logger.warning("模拟盘买入未成交（%s）: code=%s", status, code)
            continue

        quantity = _affordable_quantity(budget, price, cash)
        if quantity < LOT_SIZE:
            continue

        amount = price * quantity
        fee = max(COMMISSION_MIN, amount * COMMISSION_RATE)
        try:
            portfolio.record_trade(
                account_id=account_id,
                symbol=code,
                trade_date=trade_date,
                side="buy",
                quantity=float(quantity),
                price=float(price),
                fee=round(fee, 2),
                tax=0.0,
                market="cn",
                currency="CNY",
                dedup_hash=_dedup_hash(account_id, signal_date, code, "buy"),
                note="每日选股建仓",
            )
            executed.append(
                {
                    "code": code,
                    "name": name,
                    "side": "buy",
                    "quantity": quantity,
                    "price": price,
                    "amount": round(amount, 2),
                }
            )
            cash -= amount + fee
            positions[code] = {"symbol": code, "quantity": float(quantity)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("模拟盘买入失败 code=%s: %s", code, exc)
    return executed


def _sync_tracked_holdings(
    holdings: Sequence[Dict[str, Any]], *, trade_date: date, db: DatabaseManager
) -> None:
    """把模拟盘实际持仓登记为 holding，供每日复盘覆盖。

    入参同时兼容两种结构：成交返回 ``code/name/price`` 与持仓快照
    ``symbol/avg_cost``。
    """
    try:
        from src.services.cn_tracker.holding_tracker_service import (
            ensure_tracked_positions,
            set_position_status,
        )

        normalized: List[Dict[str, Any]] = []
        for item in holdings:
            code = str(item.get("code") or item.get("symbol") or "").strip()
            if not code:
                continue
            normalized.append(
                {
                    "code": code,
                    "name": item.get("name") or "",
                    "price": item.get("price") or item.get("avg_cost"),
                    "quantity": item.get("quantity"),
                }
            )
        if not normalized:
            return

        ensure_tracked_positions(normalized, pick_date=trade_date, db=db)
        for item in normalized:
            set_position_status(
                item["code"],
                "holding",
                entry_price=item.get("price"),
                quantity=item.get("quantity"),
                db=db,
            )
    except Exception as exc:  # noqa: BLE001 - 同步失败不影响成交
        logger.warning("同步模拟盘持仓到跟踪表失败: %s", exc)


def _position_slots() -> int:
    """单日建仓的仓位数量，跟随 ``SCREENING_PICKS_TOP_N``。

    之前硬编码 5：一旦把选股只数改大，模拟盘仍然只买 5 只，配置等于失效。
    """
    raw = os.getenv("SCREENING_PICKS_TOP_N")
    if raw is None or not str(raw).strip():
        return DEFAULT_POSITION_SLOTS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "SCREENING_PICKS_TOP_N 不是整数（%r），模拟盘按默认 %s 个仓位执行",
            raw,
            DEFAULT_POSITION_SLOTS,
        )
        return DEFAULT_POSITION_SLOTS
    return max(1, min(MAX_POSITION_SLOTS, value))


def _affordable_quantity(budget: float, price: float, cash: float) -> int:
    """在「单仓预算」内向下取整到 100 股，并确保现金足以覆盖手续费。

    预算优先于现金：不允许为了凑满仓位而吃掉其它仓位的钱。
    """
    if price <= 0 or budget <= 0 or cash <= 0:
        return 0
    quantity = int(min(budget, cash) / price / LOT_SIZE) * LOT_SIZE
    while quantity >= LOT_SIZE:
        amount = price * quantity
        fee = max(COMMISSION_MIN, amount * COMMISSION_RATE)
        if amount + fee <= cash:
            return quantity
        quantity -= LOT_SIZE
    return 0


def _load_positions(db: DatabaseManager, account_id: int) -> Dict[str, Dict[str, Any]]:
    with db.get_session() as session:
        rows = (
            session.query(PortfolioPosition)
            .filter(
                PortfolioPosition.account_id == account_id,
                PortfolioPosition.market == "cn",
                PortfolioPosition.quantity > 0,
            )
            .all()
        )
    return {
        str(row.symbol): {
            "symbol": row.symbol,
            "quantity": float(row.quantity or 0.0),
            "avg_cost": float(row.avg_cost or 0.0),
            "last_price": float(row.last_price or 0.0),
            "unrealized_pnl": float(row.unrealized_pnl_base or 0.0),
        }
        for row in rows
    }


def _cash_and_equity(
    db: DatabaseManager, account_id: int, trade_date: date
) -> tuple[Optional[float], Optional[float]]:
    with db.get_session() as session:
        row = (
            session.query(PortfolioDailySnapshot)
            .filter(
                PortfolioDailySnapshot.account_id == account_id,
                PortfolioDailySnapshot.snapshot_date == trade_date,
            )
            .order_by(PortfolioDailySnapshot.id.desc())
            .first()
        )
    if row is None:
        return None, None
    return float(row.total_cash or 0.0), float(row.total_equity or 0.0)


def _tradable_price(
    db: DatabaseManager, code: str, trade_date: date, side: str
) -> Tuple[Optional[float], str]:
    """返回当日「开盘挂单」的成交价与状态。

    模拟盘不能按收盘价成交——收盘价是已经走完的价格，现实中挂不上单。
    真实时序是：信号在前一交易日收盘后产生，**次日开盘**挂单，能不能成交取决于：

    - 当日无 K 线 / 成交量为零 → 停牌，``suspend``
    - 一字涨停（最高价 = 最低价且上涨）→ 买不进，``limit_up_sealed``
    - 一字跌停（最高价 = 最低价且下跌）→ 卖不出，``limit_down_sealed``
    - 其余情况按开盘价成交，并施加滑点

    一字板用「最高价 == 最低价」判定，而不是猜涨跌停幅度：
    主板 10%、创业板/科创板 20%、ST 5%，硬编码比例会误判。
    """
    with db.get_session() as session:
        row = (
            session.query(
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.volume,
                StockDaily.pct_chg,
            )
            .filter(
                StockDaily.code == str(code),
                StockDaily.date == trade_date,
            )
            .order_by(StockDaily.id.desc())
            .first()
        )

    if row is None:
        return None, "suspend"
    open_price, high, low, volume, pct_chg = row
    if open_price is None or float(open_price) <= 0:
        return None, "suspend"
    if volume is None or float(volume or 0) <= 0:
        return None, "suspend"

    if high is not None and low is not None and float(high) == float(low):
        change = float(pct_chg) if pct_chg is not None else None
        if change is None:
            # 存量日线可能没有 pct_chg：退回用前收盘价自行计算方向
            prev_close = _previous_close(db, code, trade_date)
            if prev_close:
                change = (float(open_price) - prev_close) / prev_close * 100
        change = change or 0.0
        if change < 0 and side == "sell":
            return None, "limit_down_sealed"
        if change > 0 and side == "buy":
            return None, "limit_up_sealed"

    factor = 1 + SLIPPAGE_RATE if side == "buy" else 1 - SLIPPAGE_RATE
    return round(float(open_price) * factor, 2), "ok"


def _previous_close(
    db: DatabaseManager, code: str, trade_date: date
) -> Optional[float]:
    """取严格早于 trade_date 的最近一根收盘价（用于补算涨跌幅）。"""
    with db.get_session() as session:
        row = (
            session.query(StockDaily.close)
            .filter(
                StockDaily.code == str(code),
                StockDaily.date < trade_date,
            )
            .order_by(StockDaily.date.desc())
            .first()
        )
    if row and row[0]:
        return float(row[0])
    return None


def _dedup_hash(account_id: int, trade_date: date, code: str, side: str) -> str:
    return f"paper:{account_id}:{trade_date.isoformat()}:{code}:{side}"


def _extract_account_snapshot(
    snapshot: Dict[str, Any], account_id: int, db: DatabaseManager
) -> Dict[str, Any]:
    """取账户聚合值（单账户时快照顶层汇总即该账户）与持仓明细。"""
    payload = snapshot or {}
    positions = list(_load_positions(db, account_id).values())
    return {
        "total_cash": float(payload.get("total_cash") or 0.0),
        "total_market_value": float(payload.get("total_market_value") or 0.0),
        "total_equity": float(payload.get("total_equity") or 0.0),
        "unrealized_pnl": float(payload.get("unrealized_pnl") or 0.0),
        "realized_pnl": float(payload.get("realized_pnl") or 0.0),
        "fee_total": float(payload.get("fee_total") or 0.0),
        "tax_total": float(payload.get("tax_total") or 0.0),
        "positions": positions,
    }
