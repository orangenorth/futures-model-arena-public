"""加密行情源：交易所返回 HTTP 451/403（地区限制）时不再重试，健康详情给出可操作的提示；其他错误照常重试。"""
import httpx
import pytest

from arena.market.crypto import CryptoSource, SourceError


def fake_get(status: int):
    def get(url, params=None):
        return httpx.Response(status, request=httpx.Request("GET", url))
    return get


def test_geo_blocked_response_is_not_retried_and_explains_itself():
    src = CryptoSource(["BTCUSDT"], provider="binance", timeout=1)
    src.client.get = fake_get(451)  # type: ignore[method-assign]
    with pytest.raises(SourceError, match="451"):
        src._get("https://fapi.binance.com/fapi/v1/ticker/price", {})
    assert src._errors == 1, "地区限制是确定性的，不应重试三次"
    detail = src.health()["detail"]
    assert "451" in detail and "Binance" in detail and "okx" in detail
    assert src.health()["ok"] is False


def test_other_http_errors_still_retry(monkeypatch):
    monkeypatch.setattr("arena.market.crypto.time.sleep", lambda s: None)
    src = CryptoSource(["BTC-USDT-SWAP"], provider="okx", timeout=1)
    src.client.get = fake_get(503)  # type: ignore[method-assign]
    with pytest.raises(SourceError, match="503"):
        src._get("https://www.okx.com/api/v5/market/ticker", {})
    assert src._errors == 3
    assert "503" in src.health()["detail"] and "地区" not in src.health()["detail"]
