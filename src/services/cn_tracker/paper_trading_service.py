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
from datetime import date
from typing import Any, Dict, List, Optional

from src.services.portfolio_service import PortfolioService
from src.storage import (
    DatabaseManager,
    HoldingReview,
    PortfolioDailySnapshot,
    PortfolioPosition,
    ScreeningPick,
    StockDaily,
)

logger = logging.getLogger(__name__)

PAPER_ACCOUNT_NAME = "A股模拟盘"
DEFAULT_INITIAL_CASH = 20000.0
POSITION_SLOTS = 5
LOT_SIZE = 100

COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_TAX_RATE = 0.001

# 初始资金视为「开户时已存入」的历史入金：这样回放任意 as_of 都能取到现金，
# 不需要每次调用都对齐入金日期（现金不计息，不影响收益计算）。
PAPER_CASH_START_DATE = date(2000, 1, 1)


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
    equity = summary.get("total_equity") or 0.0
    return {
        "initialized": True,
        "account": account,
        "as_of": report_date.isoformat(),
        "initial_cash": initial_cash,
        "snapshot": summary,
        "return_pct": round((equity - initial_cash) / initial_cash * 100, 2)
        if initial_cash > 0
        else None,
        "positions": summary.get("positions") or [],
        "curve": curve,
        "trades": trades,
    }


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
    lines.append(
        f"总资产 {equity:.2f} 元（初始 {initial:.0f} 元，"
        f"收益 {equity - initial:+.2f} 元 / {return_pct:+.2f}%）"
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
    lines.append("> 模拟成交按收盘价、未计滑点，收益为理论上限，仅供研究参考。")
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

        quantity = available if advice == "exit" else max(
            LOT_SIZE, int(available // (LOT_SIZE * 2)) * LOT_SIZE
        )
        if quantity < LOT_SIZE:
            continue

        price = _close_price(db, code, trade_date)
        if price is None:
            logger.warning("模拟盘卖出跳过（当日无收盘价）: code=%s", code)
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
        picks = (
            session.query(ScreeningPick.code, ScreeningPick.name)
            .filter(
                ScreeningPick.pick_date == trade_date,
                ScreeningPick.market == "cn",
            )
            .order_by(ScreeningPick.rank)
            .all()
        )
    if not picks:
        return []

    if cash <= 0 or equity <= 0:
        logger.warning("模拟盘买入跳过：账户估值不可用 cash=%.2f equity=%.2f", cash, equity)
        return []

    positions = _load_positions(db, account_id)

    # 每个仓位等额预算；已有持仓不重复买入
    budget = equity / POSITION_SLOTS
    executed: List[Dict[str, Any]] = []

    for code, name in picks:
        code = str(code)
        if code in positions:
            continue
        price = _close_price(db, code, trade_date)
        if price is None or price <= 0:
            logger.warning("模拟盘买入跳过（当日无收盘价）: code=%s", code)
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
                dedup_hash=_dedup_hash(account_id, trade_date, code, "buy"),
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


def _close_price(db: DatabaseManager, code: str, trade_date: date) -> Optional[float]:
    """只接受当日收盘价：用其它日期成交会让模拟盘失真。"""
    with db.get_session() as session:
        row = (
            session.query(StockDaily.close)
            .filter(
                StockDaily.code == str(code),
                StockDaily.date == trade_date,
            )
            .order_by(StockDaily.id.desc())
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
