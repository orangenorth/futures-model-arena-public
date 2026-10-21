"""Regression 61: 新浪行情的请求扇出——报价批量化与 K 线按周期分档缓存。

放开到 82 个品种之后，原来的"一个品种一次请求"变成每 20s 一个 tick 就 82 次请求
（≈4.1 req/s 持续），决策轮再按 5m/15m/1h 各取一次共 246 次。被限流之后大批品种等不到
`latest_budget_sec`，报价变陈旧，新鲜度闸门直接卡住决策。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from arena.exchange.contracts import load_cn_contracts
from arena.market.base import SourceError
from arena.market.sina_cn import PERIOD_CACHE_SEC, SPOT_BATCH_SIZE, SinaFuturesSource

ROOT = Path(__file__).resolve().parents[1]
ROW = [["2026-09-16 09:05:00", "3400", "3410", "3390", "3405", "120", "100000"]]
SYMS = ["RB0", "I0", "AU0", "AG0", "CU0", "SC0", "M0", "TA0"]


def make_sina(symbols, timeout=1.0):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return SinaFuturesSource(symbols, specs, holidays, timeout=timeout)


def spot_text(prices: dict) -> str:
    """按新浪 nf_ 字段序拼一条多合约响应体（字段 8=最新、10=昨结算、13=持仓量、14=成交量、17=日期）。"""
    return "".join(
        f'var hq_str_nf_{s}="{s}连续,090501,3400,3410,3390,0,0,0,{px},0,3395,0,0,2200000,1500000,0,0,20260916";\n'
        for s, px in prices.items()
    )


# ---------- 报价批量化 ----------

def test_latest_requests_all_symbols_in_one_batch():
    """八个品种一次请求拿完。还原成逐品种请求后本用例失败（会数到 8 次）。"""
    src = make_sina(SYMS)
    requests = []

    def fake_get(method, params, attempts=3):
        requests.append(tuple(params["symbols"]))
        return {s: ROW for s in params["symbols"]}

    src._get = fake_get  # type: ignore[method-assign]
    try:
        quotes = src.latest(SYMS)
        assert set(quotes) == set(SYMS)
        assert len(requests) == 1, f"八个品种应合成一次请求，实际 {len(requests)} 次"
        assert requests[0] == tuple(SYMS)
    finally:
        src.close()


def test_batches_are_capped_and_cover_every_symbol():
    """超过一批时按固定大小切分，边界（恰好一批 / 多一个）都不能漏品种。"""
    src = make_sina(SYMS)
    src.spot_batch_size = 3
    requests = []

    def fake_get(method, params, attempts=3):
        requests.append(tuple(params["symbols"]))
        return {s: ROW for s in params["symbols"]}

    src._get = fake_get  # type: ignore[method-assign]
    try:
        quotes = src.latest(SYMS)
        assert set(quotes) == set(SYMS)
        # 三批是并发发出的，到达 `requests` 的先后由线程池调度决定（实测出现过 [3, 2, 3]），
        # 所以按集合比对：批**内**的顺序与切分方式仍然逐字校验，只是不约束批**间**的先后。
        expected = [tuple(SYMS[i:i + 3]) for i in range(0, len(SYMS), 3)]
        assert sorted(requests) == sorted(expected), "分批打乱、漏掉或重复了品种"
        assert max(len(b) for b in requests) <= 3, "有一批超过了批量上限"
    finally:
        src.close()


def test_eighty_two_symbols_cost_two_requests_per_tick():
    """默认批量下，全品种配置每个 tick 只发两次请求，而不是 82 次。"""
    src = make_sina([f"X{i:02d}" for i in range(82)])
    counted = []

    def fake_get(method, params, attempts=3):
        counted.append(len(params["symbols"]))
        return {s: ROW for s in params["symbols"]}

    src._get = fake_get  # type: ignore[method-assign]
    try:
        src.latest(src.symbols)
        assert len(counted) == 2 and sum(counted) == 82
        assert max(counted) <= SPOT_BATCH_SIZE
    finally:
        src.close()


def test_one_bad_contract_does_not_discard_the_batch():
    """一个合约在响应体里缺席/损坏时只丢它自己，同批其他品种照常返回。"""
    src = make_sina(["RB0", "I0"])
    text = spot_text({"RB0": "3405"})          # I0 根本没出现在响应里

    class Resp:
        content = text.encode("gb18030")

        @staticmethod
        def raise_for_status():
            return None

    src.client.get = lambda url, **kw: Resp()  # type: ignore[method-assign]
    try:
        quotes = src.latest(["RB0", "I0"])
        assert set(quotes) == {"RB0"}
        assert quotes["RB0"].price == 3405.0
    finally:
        src.close()


def test_symbols_are_validated_individually_before_being_joined():
    """逐个校验代码再拼接：对拼好的串整体校验等于没有校验（逗号能放进任何东西）。"""
    src = make_sina(["RB0"])
    urls = []

    class Resp:
        content = spot_text({"RB0": "3405"}).encode("gb18030")

        @staticmethod
        def raise_for_status():
            return None

    def fake_client_get(url, **kw):
        urls.append(url)
        return Resp()

    src.client.get = fake_client_get  # type: ignore[method-assign]
    try:
        quotes = src.latest(["RB0", "RB0;rm -rf /", "../../etc/passwd"])
        assert set(quotes) == {"RB0"}
        assert len(urls) == 1 and urls[0].endswith("list=nf_RB0"), urls
        with pytest.raises(SourceError):
            src._get("getRealtimeQuote", {"symbols": ["bad code"]}, attempts=1)
    finally:
        src.close()


# ---------- K 线按周期分档缓存 ----------

def test_slow_periods_are_reused_across_rounds():
    """同一个 TTL 套在所有周期上时，1h bar 在它成形的那一小时里被反复重取。

    还原成统一 `cache_sec` 后本用例失败：15 秒之后 15m/1h 都会再请求一次。
    """
    src = make_sina(["RB0"])
    src.cache_sec = 15
    calls = []

    def fake_get(method, params, attempts=3):
        calls.append(params.get("type"))
        return ROW

    src._get = fake_get  # type: ignore[method-assign]
    try:
        for period in ("5m", "15m", "1h"):
            src.klines("RB0", period, 10)
        assert len(calls) == 3
        # 把每个周期的缓存时间戳往前推 30 秒：统一 TTL（15s）下三个周期都会过期
        for key in list(src._cache):
            ts, bars = src._cache[key]
            src._cache[key] = (ts - 30, bars)
        for period in ("5m", "15m", "1h"):
            src.klines("RB0", period, 10)
        assert calls[3:] == [], "30 秒内不应重取任何周期"

        # 再推到 5m 的档位之外，15m/1h 仍在各自档位内
        for key in list(src._cache):
            ts, bars = src._cache[key]
            src._cache[key] = (ts - 60, bars)
        for period in ("5m", "15m", "1h"):
            src.klines("RB0", period, 10)
        assert calls[3:] == ["5"], f"只有 5m 该过期，实际重取了 {calls[3:]}"
    finally:
        src.close()


def test_period_ttls_never_drop_below_cache_sec_and_zero_still_disables():
    """分档只放大不缩小；`cache_sec <= 0` 是显式关缓存，分档不得把它重新打开。"""
    src = make_sina(["RB0"])
    try:
        src.cache_sec = 3600
        assert all(src._cache_ttl(p) == 3600 for p in PERIOD_CACHE_SEC), "调大 cache_sec 必须整体生效"
        src.cache_sec = 15
        assert src._cache_ttl("1h") == PERIOD_CACHE_SEC["1h"] > src._cache_ttl("5m") > 15
        src.cache_sec = 0
        assert all(src._cache_ttl(p) == 0.0 for p in PERIOD_CACHE_SEC)
    finally:
        src.close()


def test_cache_disabled_still_refetches_every_period():
    src = make_sina(["RB0"])
    src.cache_sec = 0
    calls = []

    def fake_get(method, params, attempts=3):
        calls.append(params.get("type"))
        return ROW

    src._get = fake_get  # type: ignore[method-assign]
    try:
        src.klines("RB0", "1h", 10)
        src.klines("RB0", "1h", 10)
        assert calls == ["60", "60"]
    finally:
        src.close()
