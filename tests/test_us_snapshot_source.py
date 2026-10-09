# -*- coding: utf-8 -*-
"""美股快照（腾讯报价）与美股日线（新浪 K 线）的离线测试。

这些源替代了原先只能走 yfinance 的美股链路（Yahoo 对机房 IP 返回 429）。
测试全部离线，通过替换 ``snapshot_us.requests`` 注入固定响应。
"""

import pandas as pd
import pytest

from src.services.screening import snapshot_us

_TENCENT_FIELD_COUNT = 47


class _FakeResponse:
    def __init__(self, *, content: bytes = b"", payload=None):
        self.content = content
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        if self._payload is None:
            raise ValueError("response has no json body")
        return self._payload


class _FakeRequests:
    """仅替换 snapshot_us 模块内的 requests，避免污染全局。"""

    def __init__(self, response, calls=None):
        self._response = response
        self.calls = calls if calls is not None else []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def _build_tencent_body(entries: list[tuple[str, dict]]) -> bytes:
    """构造腾讯批量报价响应：v_us<symbol>="200~名称~代码~价格~...";"""
    lines: list[str] = []
    for symbol, fields in entries:
        parts = [""] * _TENCENT_FIELD_COUNT
        parts[0] = "200"
        parts[1] = fields.get("name_cn", "")
        parts[2] = fields.get("code_raw", symbol)
        parts[3] = fields.get("price", "")
        parts[4] = fields.get("prev_close", "")
        parts[32] = fields.get("change_pct", "")
        parts[37] = fields.get("amount", "")
        parts[38] = fields.get("turnover_rate", "")
        parts[39] = fields.get("pe", "")
        parts[44] = fields.get("total_mv_100m", "")
        parts[45] = fields.get("circ_mv_100m", "")
        parts[46] = fields.get("name_en", "")
        lines.append('v_us{0}="{1}";'.format(symbol, "~".join(parts)))
    return "\n".join(lines).encode("gbk")


def test_fetch_us_snapshot_maps_tencent_quote_fields(monkeypatch):
    body = _build_tencent_body([
        ("AAPL", {
            "name_cn": "苹果",
            "name_en": "Apple Inc.",
            "price": "336.67",
            "prev_close": "333.63",
            "change_pct": "0.91",
            "amount": "11473685816",
            "turnover_rate": "0.23",
            "pe": "38.61",
            "total_mv_100m": "49103.68606",
            "circ_mv_100m": "49134.22581",
        }),
    ])
    monkeypatch.setattr(snapshot_us, "requests", _FakeRequests(_FakeResponse(content=body)))

    df = snapshot_us.fetch_us_snapshot(["AAPL"])

    assert df.attrs["snapshot_source"] == "tencent_us"
    row = df.iloc[0]
    assert row["code"] == "AAPL"
    assert row["name"] == "苹果"
    assert row["price"] == pytest.approx(336.67)
    assert row["change_pct"] == pytest.approx(0.91)
    assert row["amount"] == pytest.approx(11473685816.0)
    # 腾讯市值字段单位是「亿」
    assert row["total_mv"] == pytest.approx(49103.68606 * 1e8)
    assert row["circ_mv"] == pytest.approx(49134.22581 * 1e8)
    assert row["pe_ratio"] == pytest.approx(38.61)
    assert row["turnover_rate"] == pytest.approx(0.23)
    # 该接口不提供 PB / 量比，必须留空而不是填 0
    assert pd.isna(row["pb_ratio"])
    assert pd.isna(row["volume_ratio"])


def test_fetch_us_snapshot_derives_change_pct_when_missing(monkeypatch):
    body = _build_tencent_body([
        ("AAPL", {"name_cn": "苹果", "price": "110.0", "prev_close": "100.0"}),
    ])
    monkeypatch.setattr(snapshot_us, "requests", _FakeRequests(_FakeResponse(content=body)))

    df = snapshot_us.fetch_us_snapshot(["AAPL"])

    assert df.iloc[0]["change_pct"] == pytest.approx(10.0)


def test_fetch_us_snapshot_keeps_class_share_ticker(monkeypatch):
    """BRK-B 请求时转成腾讯的 BRK.B，返回后必须还原成原始代码。"""
    body = _build_tencent_body([
        ("BRK.B", {"name_cn": "伯克希尔", "price": "480.10", "prev_close": "478.00"}),
    ])
    calls: list = []
    monkeypatch.setattr(
        snapshot_us, "requests", _FakeRequests(_FakeResponse(content=body), calls=calls)
    )

    df = snapshot_us.fetch_us_snapshot(["BRK-B"])

    assert "usBRK.B" in calls[0][0]
    assert df.iloc[0]["code"] == "BRK-B"


def _symbols_from_quote_url(url: str) -> list[str]:
    query = url.split("q=", 1)[-1]
    return [item[2:] for item in query.split(",") if item.startswith("us")]


class _TencentFakeRequests:
    """按请求里的 symbol 列表回包，模拟真实批量接口的分批行为。"""

    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        entries = [
            (symbol, {"name_cn": symbol, "price": "10.0", "prev_close": "9.5"})
            for symbol in _symbols_from_quote_url(url)
        ]
        return _FakeResponse(content=_build_tencent_body(entries))


def test_fetch_us_snapshot_batches_requests(monkeypatch):
    tickers = [f"AAA{i}" for i in range(snapshot_us._US_TENCENT_BATCH_SIZE + 3)]
    calls: list = []
    monkeypatch.setattr(snapshot_us, "requests", _TencentFakeRequests(calls=calls))

    df = snapshot_us.fetch_us_snapshot(tickers)

    assert len(calls) == 2
    assert sorted(df["code"]) == sorted(tickers)


def test_fetch_us_snapshot_skips_rows_without_price(monkeypatch):
    body = _build_tencent_body([
        ("AAPL", {"name_cn": "苹果", "price": "336.67", "prev_close": "333.63"}),
        ("DEAD", {"name_cn": "空价", "price": "0", "prev_close": "1"}),
    ])
    monkeypatch.setattr(snapshot_us, "requests", _FakeRequests(_FakeResponse(content=body)))

    df = snapshot_us.fetch_us_snapshot(["AAPL", "DEAD"])

    assert list(df["code"]) == ["AAPL"]


def test_fetch_us_snapshot_raises_when_no_usable_row(monkeypatch):
    short_payload = 'v_usAAPL="200~苹果~~~";'.encode("gbk")
    monkeypatch.setattr(
        snapshot_us, "requests", _FakeRequests(_FakeResponse(content=short_payload))
    )

    with pytest.raises(RuntimeError):
        snapshot_us.fetch_us_snapshot(["AAPL"])


def test_fetch_us_snapshot_rejects_empty_ticker_list(monkeypatch):
    monkeypatch.setattr(snapshot_us, "requests", _FakeRequests(_FakeResponse(content=b"")))

    with pytest.raises(RuntimeError):
        snapshot_us.fetch_us_snapshot([])


@pytest.mark.parametrize(
    "ticker, expected",
    [
        ("^GSPC", ".INX"),
        ("^IXIC", ".IXIC"),
        ("^DJI", ".DJI"),
        ("AAPL", "AAPL"),
        ("brk-b", "BRK.B"),
    ],
)
def test_sina_us_symbol_mapping(ticker, expected):
    assert snapshot_us._to_sina_us_symbol(ticker) == expected


def test_fetch_daily_history_us_sina_parses_rows(monkeypatch):
    payload = [
        {"d": "2026-10-05", "o": "332.81", "h": "336.21", "l": "331.65", "c": "332.89", "v": "34400850", "a": "11468000000"},
        {"d": "2026-10-06", "o": "332.28", "h": "334.38", "l": "330.62", "c": "333.63", "v": "30449004", "a": "10145500000"},
        {"d": "2026-10-07", "o": "336.96", "h": "338.67", "l": "332.78", "c": "336.67", "v": "34147860", "a": "11474500000"},
    ]
    monkeypatch.setattr(
        snapshot_us, "requests", _FakeRequests(_FakeResponse(payload=payload))
    )

    df = snapshot_us.fetch_daily_history_us_sina("AAPL", lookback_days=30)

    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 3
    assert df.iloc[-1]["date"] == "2026-10-07"
    assert df.iloc[-1]["close"] == pytest.approx(336.67)
    assert df.iloc[-1]["volume"] == pytest.approx(34147860.0)


def test_fetch_daily_history_us_sina_trims_to_lookback(monkeypatch):
    payload = [
        {"d": f"2026-09-{day:02d}", "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        for day in range(1, 21)
    ]
    monkeypatch.setattr(
        snapshot_us, "requests", _FakeRequests(_FakeResponse(payload=payload))
    )

    df = snapshot_us.fetch_daily_history_us_sina("AAPL", lookback_days=30)

    # 少于 30 条时全部保留；lookback 只做上界裁剪
    assert len(df) == 20


def test_fetch_daily_history_us_sina_raises_when_empty(monkeypatch):
    monkeypatch.setattr(
        snapshot_us, "requests", _FakeRequests(_FakeResponse(payload=[]))
    )

    with pytest.raises(RuntimeError):
        snapshot_us.fetch_daily_history_us_sina("AAPL")


def test_fetch_daily_history_us_sina_raises_on_non_json(monkeypatch):
    monkeypatch.setattr(
        snapshot_us, "requests", _FakeRequests(_FakeResponse(content=b"<html>blocked</html>"))
    )

    with pytest.raises(RuntimeError):
        snapshot_us.fetch_daily_history_us_sina("AAPL")


def test_us_market_defaults_to_sina_us_daily_source():
    from src.services import screening_verification_service as service

    assert service._MARKET_DAILY_SOURCE["us"] == "sina_us"
    assert service._MARKET_DAILY_SOURCE["cn"] == "auto"


def test_daily_history_accepts_sina_us_source(monkeypatch):
    """daily.fetch_daily_history(source="sina_us") 必须走到新源并带上来源标记。"""
    from src.services.screening import daily

    frame = pd.DataFrame([
        {"date": "2026-10-06", "open": 332.28, "high": 334.38, "low": 330.62, "close": 333.63, "volume": 30449004, "amount": 10145500000},
        {"date": "2026-10-07", "open": 336.96, "high": 338.67, "low": 332.78, "close": 336.67, "volume": 34147860, "amount": 11474500000},
    ])
    monkeypatch.setattr(
        snapshot_us,
        "fetch_daily_history_us_sina",
        lambda ticker, lookback_days=120: frame.copy(),
    )

    result = daily.fetch_daily_history("AAPL", lookback_days=30, source="sina_us")

    assert result.attrs["daily_source"] == "sina_us"
    assert len(result) == 2
