# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""US equity snapshot and daily history providers.

US quotes come from Tencent's public quote endpoint (batch request, GBK payload,
no API key) and US daily bars from Sina's public US K-line endpoint. Both are
reachable from mainland networks without a proxy, so they are the primary
sources instead of Yahoo Finance — Yahoo rate limits datacenter IPs (HTTP 429)
and therefore cannot be used on a cloud host at all.

``fetch_daily_history_yfinance`` is kept only for explicit ``source="yfinance"``
requests; it is never part of an automatic US path.

HK is not supported yet: there is no HK universe source or ticker
configuration path, so ``market="hk"`` is rejected at the pipeline level
rather than silently screening the US pool.
"""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

from src.core.trading_calendar import MARKET_TIMEZONE

logger = logging.getLogger(__name__)

_SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

_DEFAULT_US_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B",
    "AVGO", "JPM", "LLY", "V", "MA", "UNH", "XOM", "COST", "HD", "PG",
    "JNJ", "ABBV", "WMT", "NFLX", "BAC", "KO", "CRM", "CVX", "MRK",
    "PEP", "AMD", "TMO", "LIN", "ACN", "CSCO", "MCD", "ABT", "ADBE",
    "WFC", "GE", "DHR", "TXN", "PM", "ISRG", "MS", "NEE", "INTU",
    "DIS", "QCOM", "CAT", "NOW",
]

# ============================================================================
# Tencent US quote endpoint（批量报价，GBK，无需 key）
# ============================================================================
_US_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={symbols}"
_US_TENCENT_BATCH_SIZE = 50
_US_TENCENT_TIMEOUT_SECONDS = 15.0
_US_TENCENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}
# 响应形如：v_usAAPL="200~苹果~AAPL.OQ~336.67~...";
_US_QUOTE_VARIABLE_PATTERN = re.compile(r'v_us([A-Za-z0-9._\-]+)="([^"]*)"')

# 腾讯美股报价字段下标（已用 JPM/XOM/WMT 等真实标的交叉核对过 PE 与振幅口径）
_TC_INDEX_NAME_CN = 1
_TC_INDEX_CODE = 2
_TC_INDEX_PRICE = 3
_TC_INDEX_PREV_CLOSE = 4
_TC_INDEX_CHANGE_PCT = 32
_TC_INDEX_AMOUNT = 37
_TC_INDEX_TURNOVER_RATE = 38
_TC_INDEX_PE = 39
_TC_INDEX_TOTAL_MV_100M = 44
_TC_INDEX_CIRC_MV_100M = 45
_TC_INDEX_NAME_EN = 46
_TC_MIN_FIELD_COUNT = 46
# 腾讯市值字段单位是「亿」（币种与标的报价币种一致）
_TC_MARKET_CAP_UNIT = 1e8

# ============================================================================
# Sina US daily endpoint（美股日线，无 key、无配额）
# ============================================================================
_US_SINA_DAILY_URL = (
    "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK"
)
_US_SINA_TIMEOUT_SECONDS = 20.0
_US_SINA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
}
# yfinance 风格指数代码 -> 新浪美股 symbol
_US_INDEX_SYMBOL_MAP = {
    "^GSPC": ".INX",
    "^SPX": ".INX",
    ".INX": ".INX",
    "^IXIC": ".IXIC",
    "^DJI": ".DJI",
    "^RUT": ".RUT",
}


def fetch_us_universe(source: str = "auto") -> list[str]:
    """Return a list of US equity tickers.

    Sources:
        sp500   — scrape S&P 500 from Wikipedia
        env     — read SCREENING_US_TICKERS (comma-separated)
        default — hardcoded top-50 US large-caps
        auto    — try sp500 → env → default
    """
    src = source.lower()
    if src == "auto":
        for s in ("sp500", "env", "default"):
            try:
                tickers = fetch_us_universe(s)
                if tickers:
                    logger.info("US universe from %s: %d tickers", s, len(tickers))
                    return tickers
            except Exception as e:
                logger.debug("US universe source %s failed: %s", s, e)
        return list(_DEFAULT_US_UNIVERSE)

    if src == "sp500":
        return _fetch_sp500_tickers()
    elif src == "env":
        raw = os.getenv("SCREENING_US_TICKERS", "").strip()
        if not raw:
            raise ValueError("SCREENING_US_TICKERS not set")
        return [t.strip() for t in raw.split(",") if t.strip()]
    elif src == "default":
        return list(_DEFAULT_US_UNIVERSE)
    else:
        raise ValueError(f"Unknown US universe source: {source}")


def _fetch_sp500_tickers() -> list[str]:
    tables = pd.read_html(_SP500_WIKI_URL)
    for tbl in tables:
        if "Symbol" in tbl.columns:
            return sorted(tbl["Symbol"].dropna().str.strip().str.replace(".", "-", regex=False).tolist())
    raise RuntimeError("Could not find Symbol column in S&P 500 Wikipedia table")


def fetch_us_snapshot(
    tickers: list[str] | None = None,
    *,
    universe_source: str = "auto",
    max_workers: int = 8,
) -> pd.DataFrame:
    """Fetch a US equity snapshot in the screening schema.

    Quotes come from Tencent's public endpoint, requested in batches. Returns a
    DataFrame matching the standard snapshot columns: code, name, price,
    change_pct, amount, total_mv, circ_mv, pe_ratio, pb_ratio, volume_ratio,
    turnover_rate, industry.

    ``pb_ratio`` and ``volume_ratio`` are not published by this endpoint and are
    returned as ``None``; the bundled US strategy does not filter on them.
    """
    if tickers is None:
        tickers = fetch_us_universe(universe_source)

    normalized = [str(item).strip().upper() for item in tickers if str(item).strip()]
    if not normalized:
        raise RuntimeError("US snapshot requested without any ticker")

    logger.info("Fetching US snapshot for %d tickers via tencent", len(normalized))
    rows = _fetch_us_quotes_tencent(normalized, max_workers=max_workers)
    if not rows:
        raise RuntimeError("tencent returned no valid data for any ticker")

    df = pd.DataFrame(rows)
    numeric_cols = [
        "price", "change_pct", "amount", "total_mv", "circ_mv",
        "pe_ratio", "pb_ratio", "volume_ratio", "turnover_rate",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["price"])
    df = df[df["price"] > 0]
    if df.empty:
        raise RuntimeError("tencent returned no valid data for any ticker")

    df.attrs["snapshot_source"] = "tencent_us"
    logger.info("US snapshot: %d rows from tencent", len(df))
    return df


def _fetch_us_quotes_tencent(
    tickers: list[str],
    *,
    max_workers: int = 8,
) -> list[dict]:
    """Fetch US quotes from Tencent in batches, tolerating per-batch failures."""
    symbol_to_ticker = {_to_tencent_us_symbol(t): t for t in tickers}
    symbols = list(symbol_to_ticker)
    batches = [
        symbols[index:index + _US_TENCENT_BATCH_SIZE]
        for index in range(0, len(symbols), _US_TENCENT_BATCH_SIZE)
    ]

    rows: list[dict] = []
    worker_count = max(1, min(int(max_workers or 1), len(batches)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(_fetch_us_quote_batch, batch, symbol_to_ticker): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the snapshot
                logger.warning(
                    "US snapshot batch failed (%d symbols): %s", len(batch), exc
                )
    return rows


def _fetch_us_quote_batch(
    symbols: list[str],
    symbol_to_ticker: dict[str, str],
) -> list[dict]:
    query = ",".join(f"us{symbol}" for symbol in symbols)
    response = requests.get(
        _US_TENCENT_QUOTE_URL.format(symbols=query),
        headers=_US_TENCENT_HEADERS,
        timeout=_US_TENCENT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    text = response.content.decode("gbk", errors="replace")

    rows: list[dict] = []
    for symbol, payload in _US_QUOTE_VARIABLE_PATTERN.findall(text):
        code = symbol_to_ticker.get(symbol) or symbol.replace(".", "-")
        row = _parse_us_quote_payload(payload, code=code)
        if row is not None:
            rows.append(row)
    return rows


def _parse_us_quote_payload(payload: str, *, code: str) -> dict | None:
    parts = payload.split("~")
    if len(parts) < _TC_MIN_FIELD_COUNT:
        return None

    price = _to_float(parts[_TC_INDEX_PRICE])
    if price is None or price <= 0:
        return None

    change_pct = _to_float(parts[_TC_INDEX_CHANGE_PCT])
    if change_pct is None:
        prev_close = _to_float(parts[_TC_INDEX_PREV_CLOSE])
        if prev_close and prev_close > 0:
            change_pct = (price - prev_close) / prev_close * 100.0

    total_mv = _scale_market_cap(parts[_TC_INDEX_TOTAL_MV_100M])
    circ_mv = _scale_market_cap(parts[_TC_INDEX_CIRC_MV_100M])
    name_cn = _clean_text(parts[_TC_INDEX_NAME_CN])
    name_en = _clean_text(parts[_TC_INDEX_NAME_EN])

    return {
        "code": code,
        "name": name_cn or name_en or code,
        "price": price,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "amount": _to_float(parts[_TC_INDEX_AMOUNT]),
        "total_mv": total_mv,
        "circ_mv": circ_mv if circ_mv is not None else total_mv,
        "pe_ratio": _to_float(parts[_TC_INDEX_PE]),
        # 腾讯美股报价不提供 PB 与量比，保持为空而不是伪造 0
        "pb_ratio": None,
        "volume_ratio": None,
        "turnover_rate": _to_float(parts[_TC_INDEX_TURNOVER_RATE]),
        "industry": "",
    }


def _to_tencent_us_symbol(ticker: str) -> str:
    """Tencent encodes class shares with a dot (``BRK-B`` -> ``BRK.B``)."""
    return str(ticker or "").strip().upper().replace("-", ".")


def _to_sina_us_symbol(ticker: str) -> str:
    """Map a yfinance-style ticker to Sina's US symbol, including indices."""
    text = str(ticker or "").strip().upper()
    if text in _US_INDEX_SYMBOL_MAP:
        return _US_INDEX_SYMBOL_MAP[text]
    return text.replace("-", ".")


def _scale_market_cap(value: object) -> float | None:
    raw = _to_float(value)
    if raw is None or raw <= 0:
        return None
    return raw * _TC_MARKET_CAP_UNIT


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def fetch_daily_history_us_sina(
    ticker: str,
    *,
    lookback_days: int = 120,
) -> pd.DataFrame:
    """Fetch US daily OHLCV history from Sina's public US K-line endpoint.

    Returns a DataFrame with columns ``date, open, high, low, close, volume,
    amount`` matching the schema expected by the daily enrichment logic. Sina
    returns the full history, so only the trailing ``lookback_days`` rows are
    kept.
    """
    symbol = _to_sina_us_symbol(ticker)
    count = max(int(lookback_days), 30)
    response = requests.get(
        _US_SINA_DAILY_URL,
        params={"symbol": symbol},
        headers=_US_SINA_HEADERS,
        timeout=_US_SINA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"sina us daily history malformed for {ticker}") from exc

    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"sina us daily history empty for {ticker}")

    rows: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append({
            "date": item.get("d"),
            "open": item.get("o"),
            "high": item.get("h"),
            "low": item.get("l"),
            "close": item.get("c"),
            "volume": item.get("v"),
            "amount": item.get("a", pd.NA),
        })

    df = pd.DataFrame(
        rows, columns=["date", "open", "high", "low", "close", "volume", "amount"]
    )
    df = df.dropna(subset=["date", "close"])
    if df.empty:
        raise RuntimeError(f"sina us daily history malformed for {ticker}")

    df = df.sort_values("date")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.tail(count).copy()


def fetch_daily_history_yfinance(
    ticker: str,
    *,
    lookback_days: int = 120,
) -> pd.DataFrame:
    """Fetch daily OHLCV history for a US ticker via yfinance.

    Kept for explicit ``source="yfinance"`` requests only. Yahoo returns HTTP
    429 for datacenter IPs, so this is not used by any automatic US path.

    Returns a DataFrame with columns: date, open, high, low, close, volume
    matching the schema expected by the daily enrichment logic.
    """
    import yfinance as yf

    today = pd.Timestamp.now(tz=MARKET_TIMEZONE["us"]).normalize()
    # yfinance excludes end; include today's US session regardless of host TZ.
    end = today + pd.DateOffset(days=1)
    start = today - pd.Timedelta(days=max(lookback_days * 2, 180))
    hist = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=False,
    )
    if hist is None or hist.empty:
        raise RuntimeError(f"yfinance daily history empty for {ticker}")

    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.droplevel("Ticker")

    hist = hist.tail(max(lookback_days, 30)).copy()
    hist = hist.rename(columns={
        "Open": "开盘", "High": "最高", "Low": "最低",
        "Close": "收盘", "Volume": "成交量",
    })
    hist.index.name = "日期"
    hist = hist.reset_index()
    return hist
