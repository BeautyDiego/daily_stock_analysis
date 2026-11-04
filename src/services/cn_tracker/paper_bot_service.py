# -*- coding: utf-8 -*-
"""A 股模拟盘自动交易 bot：盘前下单 → 开盘执行 → 盘中止损止盈 → 收盘结算。

时间线（cron，仅工作日）：

    09:25  plan_orders      生成订单（买：昨日选股 / 卖：今日复盘建议）
    09:35  execute_open     按实时价执行订单（高开超 3% 放弃买入）
    10:00 / 11:30 / 14:00  watch_intraday  止损 / 硬止盈 / 移动止盈
    15:05  settle           收盘估值 + 结算

与 ``paper_trading_service`` 的区别：那边是「收盘后回放开盘成交」，这里是
**盘前挂单 + 盘中实时价成交**，成交判断发生在事前，更接近真实交易。

推送只有两条：开盘简报、收盘简报（不逐笔推送）。

风控参数（对应复盘里的硬性离场，但盘中更早执行）：

- 硬止损 ``-8%``
- 硬止盈 ``+20%``
- 移动止盈：浮盈曾达 ``+10%`` 后，从持仓期最高价回落 ``5%`` 清仓
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.services.cn_tracker.paper_trading_service import (
    COMMISSION_MIN,
    COMMISSION_RATE,
    LOT_SIZE,
    PAPER_ACCOUNT_NAME,
    SLIPPAGE_RATE,
    STAMP_TAX_RATE,
    _affordable_quantity,
    _load_positions,
    _position_slots,
    _today_bought_codes,
    ensure_paper_account,
    slippage_rate,
)
from src.services.cn_tracker.risk_control import (
    adjusted_slots,
    atr_percent_from_db,
    evaluate_account_risk,
    evaluate_market_regime,
    stop_loss_pct,
    take_profit_pct,
)
from src.services.portfolio_service import PortfolioService
from src.storage import (
    DatabaseManager,
    HoldingReview,
    PaperOrder,
    PortfolioAccount,
    PortfolioDailySnapshot,
    ScreeningPick,
    TrackedPosition,
)

logger = logging.getLogger(__name__)

# 风控阈值（百分点）
HARD_STOP_LOSS_PCT = -8.0
HARD_TAKE_PROFIT_PCT = 20.0
TRAILING_ACTIVATE_PCT = 10.0
TRAILING_DRAWDOWN_PCT = 5.0
# 买入时相对昨收的最大高开幅度：超过就放弃，避免追高
OPEN_GAP_MAX_PCT = 3.0
# 跌停判定（卖出用），留一点缓冲
LIMIT_DOWN_PCT = -9.8
# 信号时效，与 paper_trading_service 保持一致
MAX_SIGNAL_AGE_DAYS = 10

_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
QUOTE_TIMEOUT_SECONDS = 8.0


# ----------------------------------------------------------------------
# 实时行情
# ----------------------------------------------------------------------


def _to_tencent_symbol(code: str) -> str:
    raw = str(code or "").strip()
    if raw.startswith(("sh", "sz", "bj")):
        return raw
    if raw.startswith(("6", "9", "5")):
        return f"sh{raw}"
    if raw.startswith(("4", "8")):
        return f"bj{raw}"
    return f"sz{raw}"


def fetch_realtime_prices(
    codes: Sequence[str], *, timeout: float = QUOTE_TIMEOUT_SECONDS
) -> Dict[str, Dict[str, Any]]:
    """批量拉 A 股实时报价（腾讯，逗号批量，无需 key）。

    返回 ``{code: {name, price, prev_close, open, high, low, change_pct}}``。
    单次最多几十只足够用；失败返回空字典，调用方按「无报价」跳过。
    """
    normalized = [str(code).strip() for code in codes if str(code).strip()]
    if not normalized:
        return {}

    import requests

    symbols = ",".join(_to_tencent_symbol(code) for code in normalized)
    try:
        response = requests.get(
            f"{_TENCENT_QUOTE_URL}{symbols}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        response.raise_for_status()
        text = response.content.decode("gbk", errors="ignore")
    except Exception as exc:  # noqa: BLE001 - 拉不到就跳过，不影响流程
        logger.warning("实时行情获取失败: %s", exc)
        return {}

    quotes: Dict[str, Dict[str, Any]] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        payload = line.split("=", 1)[1].strip().strip('"')
        parts = payload.split("~")
        if len(parts) < 35:
            continue

        def _num(index: int) -> Optional[float]:
            try:
                return float(parts[index])
            except (IndexError, TypeError, ValueError):
                return None

        code = str(parts[2] or "").strip()
        if not code:
            continue
        quotes[code] = {
            "name": str(parts[1] or "").strip(),
            "price": _num(3),
            "prev_close": _num(4),
            "open": _num(5),
            "high": _num(33),
            "low": _num(34),
            "change_pct": _num(32),
        }
    return quotes


# ----------------------------------------------------------------------
# 09:25 生成订单
# ----------------------------------------------------------------------


def plan_orders(
    *,
    as_of: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
    allow_same_day: bool = False,
    ignore_age: bool = False,
) -> Dict[str, Any]:
    """盘前生成当日订单：卖出按复盘建议，买入按昨日选股。

    ``allow_same_day`` / ``ignore_age`` 仅供手动立即建仓使用。
    """
    manager = db or DatabaseManager.get_instance()
    trade_date = as_of or date.today()
    account = ensure_paper_account(db=manager)
    account_id = int(account["id"])
    portfolio = PortfolioService()

    portfolio.get_portfolio_snapshot(
        account_id=account_id, as_of=trade_date, include_realtime=False
    )
    positions = _load_positions(manager, account_id)

    result: Dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "sells": [],
        "buys": [],
        "skipped": [],
    }

    # 1) 卖单：今日复盘建议
    with manager.get_session() as session:
        reviews = (
            session.query(HoldingReview.code, HoldingReview.advice)
            .filter(HoldingReview.review_date == trade_date)
            .all()
        )
    for code, advice in reviews:
        code = str(code)
        advice = str(advice or "")
        if advice not in ("exit", "reduce"):
            continue
        position = positions.get(code)
        if not position:
            continue
        available = int(position["quantity"] // LOT_SIZE) * LOT_SIZE
        if available < LOT_SIZE:
            continue
        if advice == "exit":
            quantity = available
        else:
            quantity = int(available // (LOT_SIZE * 2)) * LOT_SIZE
            if quantity < LOT_SIZE:
                quantity = available
        reason = "复盘离场" if advice == "exit" else "复盘减仓"
        _upsert_order(
            manager, account_id, trade_date, code, position.get("name"), "sell",
            quantity, None, reason,
        )
        result["sells"].append(
            {"code": code, "quantity": quantity, "advice": advice, "reason": reason}
        )

    # 2) 买单：先过组合风控（只拦新开仓，卖出订单已在上面生成，不受影响）
    account_risk = evaluate_account_risk(manager, account_id, trade_date)
    market_regime = evaluate_market_regime(manager, trade_date)
    result["risk"] = account_risk
    result["regime"] = market_regime
    if account_risk.get("paused"):
        result["skipped"].append(f"risk_paused:{account_risk.get('reason')}")
        logger.info("模拟盘暂停开新仓: %s", account_risk.get("reason"))
        return result

    # 3) 买单：默认取严格早于当日的最近一个选股日；手动模式下允许当天
    signal_date = _latest_pick_date(
        manager, trade_date, include_same_day=allow_same_day
    )
    if signal_date is None:
        result["skipped"].append("no_signal")
        return result
    if not ignore_age and (trade_date - signal_date).days > MAX_SIGNAL_AGE_DAYS:
        result["skipped"].append(f"stale_signal:{signal_date}")
        return result

    with manager.get_session() as session:
        picks = (
            session.query(
                ScreeningPick.code,
                ScreeningPick.name,
                ScreeningPick.entry_price,
                ScreeningPick.industry,
                ScreeningPick.policy_hits,
            )
            .filter(
                ScreeningPick.pick_date == signal_date,
                ScreeningPick.market == "cn",
            )
            .order_by(ScreeningPick.rank)
            .all()
        )
    if not picks:
        result["skipped"].append("no_picks")
        return result

    base_slots = _position_slots()
    # 熊市折减的是「允许持有的仓位数量」，单仓预算仍按满仓数算，
    # 这样少开仓就等于自动留出现金，而不是把资金堆到少数几只上
    slots = adjusted_slots(base_slots, market_regime)
    remaining_slots = max(0, slots - len(positions))
    if remaining_slots <= 0:
        result["skipped"].append(
            "slots_full" if not market_regime.get("risk_off") else "risk_off_no_slot"
        )
        return result

    base_equity = _last_equity(manager, account_id, trade_date)
    if base_equity <= 0:
        # 首次运行还没有净值快照，用初始资金当预算基准
        base_equity = float(os.getenv("PAPER_INITIAL_CASH", "20000") or 20000)
    budget = base_equity / max(1, base_slots)

    for code, name, entry_price, industry, policy_hits in picks:
        # 用已生成的买单数量判断，而不是先切片：切片会把「已持有」的候选
        # 当成一个名额白白消耗掉，导致明明还有空位却不下单
        if len(result["buys"]) >= remaining_slots:
            break
        code = str(code)
        if code in positions:
            continue
        reference = float(entry_price or 0)
        if reference <= 0:
            result["skipped"].append(f"no_price:{code}")
            continue
        quantity = int(budget / reference / LOT_SIZE) * LOT_SIZE
        if quantity < LOT_SIZE:
            result["skipped"].append(f"budget_too_small:{code}")
            continue
        _upsert_order(
            manager, account_id, trade_date, code, name, "buy", quantity,
            reference, _buy_reason(signal_date, industry, policy_hits),
        )
        result["buys"].append(
            {
                "code": code,
                "name": name,
                "quantity": quantity,
                "reference_price": reference,
                "signal_date": signal_date.isoformat(),
            }
        )

    logger.info(
        "模拟盘订单生成: date=%s buys=%s sells=%s skipped=%s",
        trade_date.isoformat(),
        len(result["buys"]),
        len(result["sells"]),
        result["skipped"],
    )
    return result


# ----------------------------------------------------------------------
# 09:35 开盘执行
# ----------------------------------------------------------------------


def execute_open(
    *,
    as_of: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
    notify: bool = True,
    skip_gap_check: bool = False,
) -> Dict[str, Any]:
    """按实时价执行待成交订单：先卖后买，高开过多的买单放弃。

    ``skip_gap_check`` 供手动立即建仓使用（人工决策不套机械阈值）。
    """
    manager = db or DatabaseManager.get_instance()
    trade_date = as_of or date.today()
    account = ensure_paper_account(db=manager)
    account_id = int(account["id"])
    portfolio = PortfolioService()

    with manager.get_session() as session:
        rows = (
            session.query(PaperOrder)
            .filter(
                PaperOrder.account_id == account_id,
                PaperOrder.trade_date == trade_date,
                PaperOrder.status == "pending",
            )
            .all()
        )
        orders = [
            {
                "id": int(row.id),
                "code": str(row.code),
                "name": row.name,
                "side": str(row.side),
                "quantity": float(row.quantity or 0.0),
                "reference_price": row.signal_price,
                "reason": row.reason,
            }
            for row in rows
        ]

    result: Dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "filled": [],
        "cancelled": [],
    }
    if not orders:
        if notify:
            notify_open_brief(result, db=manager)
        return result

    quotes = fetch_realtime_prices([order["code"] for order in orders])

    # 先 replay 出当日现金：现金由流水决定，若沿用昨日快照会算出透支
    portfolio.get_portfolio_snapshot(
        account_id=account_id, as_of=trade_date, include_realtime=False
    )
    cash_available = _current_cash(manager, account_id, trade_date)

    # 先卖后买：卖出释放的现金可以用于买入
    for side in ("sell", "buy"):
        for order in [item for item in orders if item["side"] == side]:
            quote = quotes.get(order["code"]) or {}
            price = quote.get("price")
            if not price or float(price) <= 0:
                _mark_order(manager, order["id"], "cancelled", note="无实时报价")
                result["cancelled"].append({**order, "why": "无实时报价"})
                continue

            price = float(price)
            prev_close = float(quote.get("prev_close") or price)
            gap = (price - prev_close) / prev_close * 100 if prev_close else 0.0
            change_pct = quote.get("change_pct")

            if side == "buy" and not skip_gap_check and gap > OPEN_GAP_MAX_PCT:
                _mark_order(manager, order["id"], "cancelled", note=f"高开 {gap:.1f}%")
                result["cancelled"].append({**order, "why": f"高开 {gap:.1f}% 放弃"})
                continue
            if side == "sell" and change_pct is not None and float(change_pct) <= LIMIT_DOWN_PCT:
                _mark_order(manager, order["id"], "cancelled", note="跌停无法卖出")
                result["cancelled"].append({**order, "why": "跌停无法卖出"})
                continue

            if side == "buy":
                quantity = _affordable_quantity(
                    float(price) * order["quantity"], price, cash_available
                )
                if quantity < LOT_SIZE:
                    _mark_order(manager, order["id"], "cancelled", note="现金不足")
                    result["cancelled"].append({**order, "why": "现金不足"})
                    continue
                order["quantity"] = quantity
            else:
                position = _load_positions(manager, account_id).get(order["code"]) or {}
                available = int(float(position.get("quantity") or 0) // LOT_SIZE) * LOT_SIZE
                if available < LOT_SIZE:
                    _mark_order(manager, order["id"], "cancelled", note="无可卖持仓")
                    result["cancelled"].append({**order, "why": "无可卖持仓"})
                    continue
                order["quantity"] = min(order["quantity"], available)

            rate = slippage_rate(price)
            fill_price = round(price * (1 + rate if side == "buy" else 1 - rate), 2)
            quantity = float(order["quantity"])
            amount = fill_price * quantity
            fee = max(COMMISSION_MIN, amount * COMMISSION_RATE)
            tax = amount * STAMP_TAX_RATE if side == "sell" else 0.0

            try:
                portfolio.record_trade(
                    account_id=account_id,
                    symbol=order["code"],
                    trade_date=trade_date,
                    side=side,
                    quantity=quantity,
                    price=fill_price,
                    fee=round(fee, 2),
                    tax=round(tax, 2),
                    market="cn",
                    currency="CNY",
                    dedup_hash=f"bot:{account_id}:{trade_date.isoformat()}:{order['code']}:{side}",
                    note=str(order.get("reason") or "")[:64] or None,
                )
                _mark_order(manager, order["id"], "filled", filled_price=fill_price)
                if side == "buy":
                    cash_available -= amount + fee
                result["filled"].append(
                    {
                        "code": order["code"],
                        "name": order.get("name"),
                        "side": side,
                        "quantity": quantity,
                        "price": fill_price,
                        "reason": order.get("reason"),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单笔失败不影响其它
                logger.warning("模拟盘开盘成交失败 %s %s: %s", side, order["code"], exc)
                _mark_order(manager, order["id"], "cancelled", note=str(exc)[:120])

    # 同步跟踪表并结算当前权益
    _replay_and_sync(manager, account_id, trade_date, portfolio)
    if notify:
        notify_open_brief(result, db=manager)
    logger.info(
        "模拟盘开盘执行: date=%s filled=%s cancelled=%s",
        trade_date.isoformat(),
        len(result["filled"]),
        len(result["cancelled"]),
    )
    return result


# ----------------------------------------------------------------------
# 盘中止损 / 止盈
# ----------------------------------------------------------------------


def watch_intraday(
    *,
    as_of: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
) -> Dict[str, Any]:
    """盘中按实时价检查止损与止盈，触发即清仓。"""
    manager = db or DatabaseManager.get_instance()
    trade_date = as_of or date.today()
    account = ensure_paper_account(db=manager)
    account_id = int(account["id"])
    portfolio = PortfolioService()

    portfolio.get_portfolio_snapshot(
        account_id=account_id, as_of=trade_date, include_realtime=False
    )
    positions = _load_positions(manager, account_id)
    result: Dict[str, Any] = {"trade_date": trade_date.isoformat(), "triggered": []}
    if not positions:
        return result

    quotes = fetch_realtime_prices(list(positions.keys()))
    # T+1：当日买入的标的当日不可卖，盘中风控也不能破例
    bought_today = _today_bought_codes(manager, account_id, trade_date)
    for code, position in positions.items():
        if code in bought_today:
            continue
        quote = quotes.get(code) or {}
        price = quote.get("price")
        if not price or float(price) <= 0:
            continue

        price = float(price)
        cost = float(position.get("avg_cost") or 0.0)
        if cost <= 0:
            continue

        peak = _update_peak_price(manager, code, price)
        return_pct = (price - cost) / cost * 100
        peak_pct = (peak - cost) / cost * 100 if peak else return_pct
        drawdown = (peak - price) / peak * 100 if peak and peak > 0 else 0.0

        # 阈值按 ATR 自适应：低波动标的止损更紧、高波动更宽
        atr_pct = atr_percent_from_db(manager, code, trade_date)
        stop_pct = stop_loss_pct(atr_pct)
        take_pct = take_profit_pct(atr_pct)

        reason = ""
        if return_pct <= stop_pct:
            reason = f"止损 {return_pct:.1f}%（阈值 {stop_pct:.1f}%）"
        elif return_pct >= take_pct:
            reason = f"止盈 {return_pct:+.1f}%（阈值 {take_pct:.1f}%）"
        elif (
            _trailing_enabled()
            and peak_pct >= TRAILING_ACTIVATE_PCT
            and drawdown >= TRAILING_DRAWDOWN_PCT
        ):
            reason = f"移动止盈（峰值 {peak_pct:+.1f}% 回落 {drawdown:.1f}%）"
        if not reason:
            continue

        quantity = int(float(position.get("quantity") or 0) // LOT_SIZE) * LOT_SIZE
        if quantity < LOT_SIZE:
            continue
        fill_price = round(price * (1 - slippage_rate(price)), 2)
        amount = fill_price * quantity
        fee = max(COMMISSION_MIN, amount * COMMISSION_RATE)
        tax = amount * STAMP_TAX_RATE
        try:
            portfolio.record_trade(
                account_id=account_id,
                symbol=code,
                trade_date=trade_date,
                side="sell",
                quantity=quantity,
                price=fill_price,
                fee=round(fee, 2),
                tax=round(tax, 2),
                market="cn",
                currency="CNY",
                dedup_hash=f"bot:{account_id}:{trade_date.isoformat()}:{code}:stop",
                note=reason[:64],
            )
            _close_tracked(manager, code, trade_date, fill_price)
            result["triggered"].append(
                {
                    "code": code,
                    "quantity": quantity,
                    "price": fill_price,
                    "return_pct": round(return_pct, 2),
                    "reason": reason,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("模拟盘风控卖出失败 %s: %s", code, exc)

    if result["triggered"]:
        _replay_and_sync(manager, account_id, trade_date, portfolio)
        logger.info(
            "模拟盘盘中风控触发: date=%s triggered=%s",
            trade_date.isoformat(),
            len(result["triggered"]),
        )
    return result


# ----------------------------------------------------------------------
# 15:05 收盘结算
# ----------------------------------------------------------------------


def settle(
    *,
    as_of: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
    notify: bool = True,
) -> Dict[str, Any]:
    """收盘估值：写快照、算当日盈亏，并推送收盘简报。"""
    manager = db or DatabaseManager.get_instance()
    trade_date = as_of or date.today()
    account = ensure_paper_account(db=manager)
    account_id = int(account["id"])
    portfolio = PortfolioService()

    snapshot = portfolio.get_portfolio_snapshot(
        account_id=account_id, as_of=trade_date, include_realtime=False
    )
    equity = float(snapshot.get("total_equity") or 0.0)
    previous = _previous_equity(manager, account_id, trade_date)

    today_pct = None
    if previous and previous > 0:
        today_pct = (equity - previous) / previous * 100
    initial = float(os.getenv("PAPER_INITIAL_CASH", "20000") or 20000)
    total_pct = (equity - initial) / initial * 100 if initial > 0 else None

    holdings = _holdings_detail(manager, account_id)
    result = {
        "trade_date": trade_date.isoformat(),
        "equity": round(equity, 2),
        "cash": round(float(snapshot.get("total_cash") or 0.0), 2),
        "market_value": round(float(snapshot.get("total_market_value") or 0.0), 2),
        "today_pct": round(today_pct, 2) if today_pct is not None else None,
        "total_pct": round(total_pct, 2) if total_pct is not None else None,
        "positions": len(holdings),
        "holdings": holdings,
        "triggered": _today_triggered(manager, account_id, trade_date),
        "trades": _today_trade_summary(manager, account_id, trade_date),
    }
    if notify:
        notify_close_brief(result, db=manager)
    logger.info(
        "模拟盘收盘结算: date=%s equity=%.2f today=%.2f%% total=%.2f%%",
        trade_date.isoformat(),
        equity,
        result["today_pct"] or 0.0,
        result["total_pct"] or 0.0,
    )
    return result


# ----------------------------------------------------------------------
# 推送（只有开盘/收盘两条）
# ----------------------------------------------------------------------


def notify_open_brief(result: Dict[str, Any], *, db: Optional[DatabaseManager] = None) -> bool:
    """开盘简报：当前持仓 + 账户状态（不含成交明细，成交只在收盘汇总）。"""
    manager = db or DatabaseManager.get_instance()
    account_id = _paper_account_id(manager)
    if account_id is None:
        return False

    holdings = _holdings_detail(manager, account_id, live=True)
    try:
        cash = _current_cash(
            manager, account_id, date.fromisoformat(str(result.get("trade_date")))
        )
    except (TypeError, ValueError):
        cash = 0.0
    market_value = sum(
        float(item.get("price") or 0.0) * float(item.get("quantity") or 0.0)
        for item in holdings
    )
    # 总资产必须与市值同源：快照里的权益是按收盘价估的，
    # 直接拿来用会和实时市值对不上（实测差几十元）
    equity = cash + market_value
    initial = float(os.getenv("PAPER_INITIAL_CASH", "20000") or 20000)
    total_pct = (equity - initial) / initial * 100 if initial > 0 else None

    filled = result.get("filled") or []
    lines = [f"【模拟盘·开盘】{_md_date(result.get('trade_date'))}", ""]
    lines.append("【成交】")
    lines.extend(_trade_lines(filled))
    lines.append("")
    lines.extend(_holdings_block(holdings))
    lines.append("")
    lines.append("【账户】")
    lines.append(
        f"持仓 {len(holdings)} 只｜市值 {_yuan(market_value)}｜现金 {_yuan(cash)}"
    )
    lines.append(f"总资产 {_yuan(equity)}｜累计 {_pct(total_pct)}")
    return _send("\n".join(lines))


def _holdings_block(holdings: Sequence[Dict[str, Any]]) -> List[str]:
    """持仓段（开盘/收盘共用）：名称 代码 股数 成本 → 现价 浮盈%。"""
    if not holdings:
        return ["当前空仓"]
    lines = ["【持仓】"]
    for item in holdings:
        pct = item.get("pnl_pct")
        pct_text = f"{pct:+.2f}%" if pct is not None else "--"
        lines.append(
            f"{item.get('name') or ''} {item.get('code')}  "
            f"{int(item.get('quantity') or 0)}股  "
            f"{float(item.get('cost') or 0):.2f} → "
            f"{float(item.get('price') or 0):.2f}  {pct_text}"
        )
    return lines


def notify_close_brief(result: Dict[str, Any], *, db: Optional[DatabaseManager] = None) -> bool:
    """收盘简报：今日战况 + 持仓浮盈（百分比）+ 账户。"""
    trades = result.get("trades") or {}
    lines = [f"【模拟盘·收盘】{_md_date(result.get('trade_date'))}", ""]
    lines.append(
        f"今日成交：买 {trades.get('buy', 0)}｜卖 {trades.get('sell', 0)}"
    )
    triggered = result.get("triggered") or []
    lines.append(
        "盘中触发："
        + (
            "、".join(
                f"{item.get('code')}（{item.get('reason')}）" for item in triggered[:3]
            )
            if triggered
            else "无"
        )
    )

    holdings = result.get("holdings") or []
    lines.append("")
    lines.extend(_holdings_block(holdings))

    lines.append("")
    lines.append("【账户】")
    lines.append(
        f"总资产 {_yuan(result.get('equity'))}"
        f"（现金 {_yuan(result.get('cash'))} + 市值 {_yuan(result.get('market_value'))}）"
    )
    lines.append(
        f"今日 {_pct(result.get('today_pct'))}｜累计 {_pct(result.get('total_pct'))}"
    )
    return _send("\n".join(lines))


def _send(content: str) -> bool:
    try:
        from src.notification import NotificationService

        service = NotificationService()
        ok = service.send(content, email_stock_codes=[])
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响结算
        logger.warning("模拟盘简报推送失败: %s", exc)
        return False
    if not ok:
        logger.warning("模拟盘简报未发出（检查通知渠道配置）")
    return bool(ok)


def _yuan(value: Any) -> str:
    try:
        return f"{float(value or 0.0):.0f}"
    except (TypeError, ValueError):
        return "--"


def _holdings_detail(
    manager: DatabaseManager, account_id: int, *, live: bool = False
) -> List[Dict[str, Any]]:
    """持仓明细：成本、现价、浮盈百分比。

    ``live=True`` 时用实时价刷新现价（开盘简报用），否则用日线收盘价
    （收盘简报用，此时 ``--sync-daily`` 已把当日收盘写入）。
    """
    positions = _load_positions(manager, account_id)
    names = _tracked_names(manager, list(positions.keys()))
    details: List[Dict[str, Any]] = []
    for code, position in positions.items():
        cost = float(position.get("avg_cost") or 0.0)
        price = float(position.get("last_price") or 0.0)
        pct = (price - cost) / cost * 100 if cost > 0 and price > 0 else None
        details.append(
            {
                "code": code,
                "name": names.get(code) or "",
                "quantity": float(position.get("quantity") or 0.0),
                "cost": cost,
                "price": price,
                "pnl_pct": round(pct, 2) if pct is not None else None,
            }
        )

    if live and details:
        quotes = fetch_realtime_prices([item["code"] for item in details])
        for item in details:
            quote = quotes.get(item["code"]) or {}
            price = quote.get("price")
            if not price or float(price) <= 0:
                continue
            item["price"] = float(price)
            cost = float(item.get("cost") or 0.0)
            item["pnl_pct"] = (
                round((float(price) - cost) / cost * 100, 2) if cost > 0 else None
            )

    details.sort(key=lambda item: item.get("pnl_pct") or 0.0, reverse=True)
    return details


def _tracked_names(manager: DatabaseManager, codes: Sequence[str]) -> Dict[str, str]:
    """持仓名称：优先跟踪表，缺失时回退选股表（跟踪表的 name 可能为空）。"""
    normalized = [str(code) for code in codes if str(code)]
    if not normalized:
        return {}
    names: Dict[str, str] = {}
    with manager.get_session() as session:
        for code, name in (
            session.query(TrackedPosition.code, TrackedPosition.name)
            .filter(TrackedPosition.code.in_(normalized))
            .all()
        ):
            if name:
                names.setdefault(str(code), str(name))

        missing = [code for code in normalized if code not in names]
        if missing:
            for code, name in (
                session.query(ScreeningPick.code, ScreeningPick.name)
                .filter(ScreeningPick.code.in_(missing))
                .order_by(ScreeningPick.pick_date.desc())
                .all()
            ):
                if name:
                    names.setdefault(str(code), str(name))
    return names


def _today_triggered(
    manager: DatabaseManager, account_id: int, trade_date: date
) -> List[Dict[str, Any]]:
    """当日由风控触发的卖出（止损/止盈），供收盘简报展示。"""
    from src.storage import PortfolioTrade

    with manager.get_session() as session:
        rows = (
            session.query(PortfolioTrade.symbol, PortfolioTrade.note)
            .filter(
                PortfolioTrade.account_id == account_id,
                PortfolioTrade.trade_date == trade_date,
                PortfolioTrade.side == "sell",
            )
            .all()
        )
    triggered: List[Dict[str, Any]] = []
    for symbol, note in rows:
        text = str(note or "")
        if "止损" in text or "止盈" in text:
            triggered.append({"code": str(symbol), "reason": text})
    return triggered


def _pct(value: Any) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "--"


def _md_date(value: Any) -> str:
    text = str(value or "")
    return text[5:] if len(text) >= 10 else text


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------


def _paper_account_id(manager: DatabaseManager) -> Optional[int]:
    with manager.get_session() as session:
        account = (
            session.query(PortfolioAccount)
            .filter(PortfolioAccount.name == PAPER_ACCOUNT_NAME)
            .first()
        )
    return int(account.id) if account else None


def _trailing_enabled() -> bool:
    """移动止盈开关，默认关闭。

    88,317 个买入点的配对回测：移动止盈比「一次性硬止盈」平均差 0.26pct。
    原因是它的启动条件是「先涨到 +10%」，只对涨过的标的生效——
    在能涨的标的上提前卖飞，在对下跌的标的上完全不提供保护。
    保留实现以便日后在上行区间重新评估（``PAPER_TRAILING_ENABLED=true``）。
    """
    return str(os.getenv("PAPER_TRAILING_ENABLED", "false")).strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _buy_reason(signal_date: date, industry: Any, policy_hits: Any) -> str:
    """买入依据：信号日 + 行业 + 政策加成，供开盘简报展示。"""
    parts = [f"选股 {signal_date.isoformat()}"]
    if industry:
        parts.append(str(industry))
    delta = _policy_delta(policy_hits)
    if delta:
        parts.append(f"政策{delta:+.0f}")
    return "｜".join(parts)


def _policy_delta(policy_hits: Any) -> float:
    """从 ``screening_picks.policy_hits`` 汇总政策加分。"""
    import json

    if not policy_hits:
        return 0.0
    try:
        items = (
            json.loads(policy_hits)
            if isinstance(policy_hits, str)
            else policy_hits
        )
    except (TypeError, ValueError):
        return 0.0

    total = 0.0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            total += float(item.get("delta") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _trade_lines(filled: Sequence[Dict[str, Any]]) -> List[str]:
    """成交段：名称 + 股数 + 价格 + 金额，并附买入依据。"""
    if not filled:
        return ["买入 无｜卖出 无"]

    lines: List[str] = []
    for side, label in (("buy", "买入"), ("sell", "卖出")):
        items = [item for item in filled if item.get("side") == side]
        if not items:
            lines.append(f"{label} 无")
            continue
        for item in items[:5]:
            price = float(item.get("price") or 0.0)
            quantity = float(item.get("quantity") or 0.0)
            lines.append(
                f"{label} {item.get('name') or ''} {item.get('code')}  "
                f"{int(quantity)}股 @{price:.2f}  共 {price * quantity:.0f} 元"
            )
            if item.get("reason"):
                lines.append(f"  依据：{item.get('reason')}")
    return lines


def _upsert_order(
    manager: DatabaseManager,
    account_id: int,
    trade_date: date,
    code: str,
    name: Optional[str],
    side: str,
    quantity: float,
    signal_price: Optional[float],
    reason: str,
) -> None:
    with manager.get_session() as session:
        row = (
            session.query(PaperOrder)
            .filter(
                PaperOrder.account_id == account_id,
                PaperOrder.trade_date == trade_date,
                PaperOrder.code == code,
                PaperOrder.side == side,
            )
            .first()
        )
        if row is None:
            session.add(
                PaperOrder(
                    account_id=account_id,
                    trade_date=trade_date,
                    code=code,
                    name=name,
                    side=side,
                    quantity=float(quantity),
                    signal_price=signal_price,
                    reason=reason[:64],
                )
            )
        elif row.status == "pending":
            row.quantity = float(quantity)
            row.signal_price = signal_price
            row.reason = reason[:64]
            row.name = name or row.name
        session.commit()


def _mark_order(
    manager: DatabaseManager,
    order_id: int,
    status: str,
    *,
    filled_price: Optional[float] = None,
    note: Optional[str] = None,
) -> None:
    with manager.get_session() as session:
        row = session.query(PaperOrder).filter(PaperOrder.id == order_id).first()
        if row is None:
            return
        row.status = status
        if filled_price is not None:
            row.filled_price = float(filled_price)
            row.filled_at = datetime.now()
        if note:
            row.note = note[:255]
        session.commit()


def _latest_pick_date(
    manager: DatabaseManager,
    trade_date: date,
    *,
    include_same_day: bool = False,
) -> Optional[date]:
    """最近一个选股日。``include_same_day`` 用于手动立即建仓。"""
    with manager.get_session() as session:
        query = session.query(ScreeningPick.pick_date).filter(
            ScreeningPick.market == "cn"
        )
        if include_same_day:
            query = query.filter(ScreeningPick.pick_date <= trade_date)
        else:
            query = query.filter(ScreeningPick.pick_date < trade_date)
        row = query.order_by(ScreeningPick.pick_date.desc()).first()
    return row[0] if row else None


def trade_now(
    *,
    as_of: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
    notify: bool = True,
) -> Dict[str, Any]:
    """手动立即建仓：用「当前最新信号」按实时价成交。

    与 cron 流程的差别，仅用于人工干预：

    - 信号取最近的选股日（**允许是当天**），不受 T+1 与 10 天时效约束；
    - 跳过「高开 3% 放弃」，因为这是主动决策，不是机械执行；
    - 剩余仓位与现金约束仍然生效，不会透支。
    """
    manager = db or DatabaseManager.get_instance()
    trade_date = as_of or date.today()

    plan = plan_orders(
        as_of=trade_date, db=manager, allow_same_day=True, ignore_age=True
    )
    result = execute_open(
        as_of=trade_date, db=manager, notify=False, skip_gap_check=True
    )
    result["plan"] = plan
    if notify:
        notify_open_brief(result, db=manager)
    logger.info(
        "模拟盘手动建仓: date=%s filled=%s cancelled=%s",
        trade_date.isoformat(),
        len(result.get("filled") or []),
        len(result.get("cancelled") or []),
    )
    return result


def _last_equity(manager: DatabaseManager, account_id: int, trade_date: date) -> float:
    """最近一次快照的总权益（含当日已写入的），用作下单预算基准。"""
    with manager.get_session() as session:
        row = (
            session.query(PortfolioDailySnapshot.total_equity)
            .filter(
                PortfolioDailySnapshot.account_id == account_id,
                PortfolioDailySnapshot.snapshot_date <= trade_date,
            )
            .order_by(PortfolioDailySnapshot.snapshot_date.desc())
            .first()
        )
    return float(row[0]) if row and row[0] else 0.0


def _previous_equity(manager: DatabaseManager, account_id: int, trade_date: date) -> float:
    """上一个交易日的总权益，用于计算当日盈亏。"""
    with manager.get_session() as session:
        row = (
            session.query(PortfolioDailySnapshot.total_equity)
            .filter(
                PortfolioDailySnapshot.account_id == account_id,
                PortfolioDailySnapshot.snapshot_date < trade_date,
            )
            .order_by(PortfolioDailySnapshot.snapshot_date.desc())
            .first()
        )
    return float(row[0]) if row and row[0] else 0.0


def _current_cash(manager: DatabaseManager, account_id: int, trade_date: date) -> float:
    with manager.get_session() as session:
        row = (
            session.query(PortfolioDailySnapshot.total_cash)
            .filter(
                PortfolioDailySnapshot.account_id == account_id,
                PortfolioDailySnapshot.snapshot_date <= trade_date,
            )
            .order_by(PortfolioDailySnapshot.snapshot_date.desc())
            .first()
        )
    return float(row[0]) if row and row[0] else 0.0


def _current_equity_and_pct(
    manager: DatabaseManager, account_id: int
) -> Tuple[float, Optional[float]]:
    with manager.get_session() as session:
        rows = (
            session.query(
                PortfolioDailySnapshot.snapshot_date,
                PortfolioDailySnapshot.total_equity,
            )
            .filter(PortfolioDailySnapshot.account_id == account_id)
            .order_by(PortfolioDailySnapshot.snapshot_date.desc())
            .limit(2)
            .all()
        )
    if not rows:
        return 0.0, None
    equity = float(rows[0][1] or 0.0)
    if len(rows) < 2:
        return equity, None
    previous = float(rows[1][1] or 0.0)
    if previous <= 0:
        return equity, None
    return equity, (equity - previous) / previous * 100


def _today_trade_summary(
    manager: DatabaseManager, account_id: int, trade_date: date
) -> Dict[str, int]:
    from src.storage import PortfolioTrade

    with manager.get_session() as session:
        rows = (
            session.query(PortfolioTrade.side)
            .filter(
                PortfolioTrade.account_id == account_id,
                PortfolioTrade.trade_date == trade_date,
            )
            .all()
        )
    return {
        "buy": sum(1 for row in rows if str(row[0]) == "buy"),
        "sell": sum(1 for row in rows if str(row[0]) == "sell"),
    }


def _update_peak_price(manager: DatabaseManager, code: str, price: float) -> float:
    """更新持仓期最高价并返回最新峰值。"""
    with manager.get_session() as session:
        row = (
            session.query(TrackedPosition)
            .filter(
                TrackedPosition.code == str(code),
                TrackedPosition.status.in_(("watching", "holding", "reduced")),
            )
            .order_by(TrackedPosition.id.desc())
            .first()
        )
        if row is None:
            return price
        peak = max(float(row.peak_price or 0.0), float(price))
        row.peak_price = peak
        session.commit()
    return peak


def _close_tracked(
    manager: DatabaseManager, code: str, trade_date: date, price: float
) -> None:
    try:
        from src.services.cn_tracker.holding_tracker_service import set_position_status

        set_position_status(
            str(code), "closed", exit_price=price, exit_date=trade_date, db=manager
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("同步清仓状态失败 code=%s: %s", code, exc)


def _replay_and_sync(
    manager: DatabaseManager, account_id: int, trade_date: date, portfolio: PortfolioService
) -> None:
    """重放持仓并把实际持仓同步到跟踪表。"""
    portfolio.get_portfolio_snapshot(
        account_id=account_id, as_of=trade_date, include_realtime=False
    )
    try:
        from src.services.cn_tracker.paper_trading_service import _sync_tracked_holdings

        holdings = list(_load_positions(manager, account_id).values())
        if holdings:
            _sync_tracked_holdings(holdings, trade_date=trade_date, db=manager)
    except Exception as exc:  # noqa: BLE001
        logger.warning("同步模拟盘持仓到跟踪表失败: %s", exc)
