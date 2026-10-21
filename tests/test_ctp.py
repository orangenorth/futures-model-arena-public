"""CTP 行情源：合约代码规则、tick 聚合、主力选择、登录与订阅链路。

CTP 的原生库在本沙箱里加载不了（容器缺 locale），真实前置也连不上，所以整条链路用**注入的
假 SDK** 驱动：`CtpMarketSource(sdk=FakeSdk())` 走的是和真实运行完全相同的代码路径，
只是底下的 `CThostFtdcMdApi` 换成了记录调用的替身。字段名与方法签名照 openctp-ctp 6.7.x
的绑定抄写（`CreateFtdcMdApi(flow)`、`SubscribeMarketData(list, count)`、
`OnRtnDepthMarketData(field)`、`InstrumentID/LastPrice/Volume/OpenInterest/UpdateTime/ActionDay`）。
"""
from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from arena.exchange.contracts import load_cn_contracts
from arena.market.base import SourceError
from arena.market.ctp import (CTP_NULL, CtpMarketSource, TickAggregator, candidate_contracts,
                              contract_id, retention_floor, variety_code)
from arena.market.sina_cn import INDICATOR_MIN_BARS
from arena.models import Bar

ROOT = Path(__file__).resolve().parents[1]
SH = ZoneInfo("Asia/Shanghai")


# ---------- 假 SDK ----------

class FakeApi:
    def __init__(self, flow=""):
        self.flow = flow
        self.spi = None
        self.front = ""
        self.subscribed = []
        self.subscribe_calls = 0
        self.logins = []
        self.released = False
        self.inited = False

    def RegisterSpi(self, spi):
        self.spi = spi

    def RegisterFront(self, addr):
        self.front = addr

    def Init(self):
        self.inited = True
        self.spi.OnFrontConnected()

    def SubscribeMarketData(self, ids, count):
        assert count == len(ids), "count 必须与列表长度一致，否则 SDK 会读越界"
        self.subscribe_calls += 1
        self.subscribed.append(list(ids))
        # 真实前置对每个合约各回一条 OnRspSubMarketData（这里同步回，真实的是异步）；
        # 返回 0 只表示请求排进了队列，订没订上要看这条应答
        self.respond_subscriptions(list(ids))
        return 0

    def respond_subscriptions(self, ids, error_for=None):
        """按 CTP 的方式逐个应答订阅；`error_for` 指定哪些合约以什么错误码拒绝。"""
        for i, cid in enumerate(ids):
            code = (error_for or {}).get(cid, 0)
            info = SimpleNamespace(ErrorID=code, ErrorMsg=("CTP:找不到合约" if code == 16 else ("CTP:前置忙" if code else "")))
            self.spi.OnRspSubMarketData(SimpleNamespace(InstrumentID=cid), info, 1, i == len(ids) - 1)

    def ReqUserLogin(self, field, request_id):
        self.logins.append((field.BrokerID, field.UserID, field.Password))
        self.spi.OnRspUserLogin(None, SimpleNamespace(ErrorID=0, ErrorMsg=""), request_id, True)
        return 0

    def Release(self):
        self.released = True


class FailingLoginApi(FakeApi):
    def ReqUserLogin(self, field, request_id):
        self.logins.append((field.BrokerID, field.UserID, field.Password))
        self.spi.OnRspUserLogin(None, SimpleNamespace(ErrorID=3, ErrorMsg="CTP:用户口令校验失败"),
                                request_id, True)
        return 0


class FakeSdk:
    """只暴露本源真正用到的三样东西：MdApi 工厂、Spi 基类、登录域。"""
    api_class = FakeApi

    def __init__(self, api_class=FakeApi):
        sdk = self

        class CThostFtdcMdSpi:
            def __init__(self):
                pass

        class CThostFtdcReqUserLoginField:
            BrokerID = ""
            UserID = ""
            Password = ""

        class CThostFtdcMdApi:
            @staticmethod
            def CreateFtdcMdApi(flow=""):
                sdk.api = api_class(flow)
                return sdk.api

        self.CThostFtdcMdSpi = CThostFtdcMdSpi
        self.CThostFtdcReqUserLoginField = CThostFtdcReqUserLoginField
        self.CThostFtdcMdApi = CThostFtdcMdApi
        self.api = None


def tick(instrument, price, volume=0, open_interest=0, clock="09:05:00", day="20260916",
         settlement=3395.0, millisec=0):
    return SimpleNamespace(InstrumentID=instrument, LastPrice=price, Volume=volume,
                           OpenInterest=open_interest, UpdateTime=clock, ActionDay=day,
                           UpdateMillisec=millisec, PreSettlementPrice=settlement)


@pytest.fixture
def source(tmp_path):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    symbols = ["RB0", "SR0"]
    # 观察期默认关掉：多数用例灌完 tick 就断言，观察期会把它们全挡在门外。
    # 观察期本身由 test_initial_discovery_* 专门覆盖。
    src = CtpMarketSource([s for s in symbols], {s: specs[s] for s in symbols}, holidays,
                          md_front="tcp://127.0.0.1:10131", broker_id="0001",
                          user_id="88888888", password="secret", discovery_grace_sec=0,
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    yield src
    src.close()


# ---------- 合约代码规则 ----------

def test_variety_code_strips_the_continuous_suffix():
    assert variety_code("RB0") == "RB" and variety_code("AU0") == "AU" and variety_code("IF0") == "IF"


@pytest.mark.parametrize("symbol,exchange,expected", [
    ("RB0", "SHFE", "rb2601"),     # 上期所：小写 + 两位年
    ("AU0", "SHFE", "au2601"),
    ("SC0", "INE", "sc2601"),
    ("M0", "DCE", "m2601"),
    ("SI0", "GFEX", "si2601"),
    ("SR0", "CZCE", "SR601"),      # 郑商所：大写 + 一位年
    ("TA0", "CZCE", "TA601"),
    ("IF0", "CFFEX", "IF2601"),    # 中金所：大写 + 两位年
    ("T0", "CFFEX", "T2601"),
])
def test_contract_id_follows_each_exchange_naming_rule(symbol, exchange, expected):
    assert contract_id(symbol, exchange, 2026, 1) == expected


def test_czce_uses_a_single_digit_year():
    """郑商所写成 SR2601 会被前置拒绝订阅——这条规则错了整个郑商所都没有行情。"""
    assert contract_id("SR0", "CZCE", 2026, 12) == "SR612"
    assert contract_id("SR0", "SHFE", 2026, 12) == "sr2612"   # 同一品种换交易所规则就变


def test_candidates_roll_across_the_year_boundary():
    ids = candidate_contracts("RB0", "SHFE", months=4, today=date(2025, 11, 20))
    assert ids == ["rb2511", "rb2512", "rb2601", "rb2602"]


def test_candidates_include_the_current_month():
    """当月合约在月中就到期，所以最小的窗口也带上下一个挂牌月（见第 28 轮）。"""
    ids = candidate_contracts("RB0", "SHFE", months=1, today=date(2026, 3, 5))
    assert ids == ["rb2603", "rb2604"]


# ---------- tick 聚合 ----------

def test_volume_is_the_increment_not_the_daily_cumulative():
    """CTP 的 Volume 是当日累计。直接记进 bar 会把一整天的量记进每一根。"""
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 1, 3400.0, 100, 50000)
    agg.add(base + 2, 3410.0, 130, 50100)
    agg.add(base + 3, 3405.0, 150, 50200)
    bars = agg.snapshot()
    assert len(bars) == 1
    assert bars[0].volume == 50.0, "应当是 150-100，而不是累计值 150"
    assert bars[0].open == 3400.0 and bars[0].high == 3410.0 and bars[0].low == 3400.0
    assert bars[0].close == 3405.0 and bars[0].hold == 50200


def test_a_new_trading_day_resets_the_cumulative_volume():
    """夜盘开盘时累计成交量清零；不处理就会记出一个负的成交量。"""
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 1, 3400.0, 5000, 50000)
    agg.add(base + 61, 3402.0, 12, 50010)      # 累计值变小 = 换日
    bars = agg.snapshot()
    assert all(b.volume >= 0 for b in bars), f"出现负成交量：{[b.volume for b in bars]}"
    agg.add(base + 62, 3403.0, 40, 50020)
    assert agg.snapshot()[-1].volume == 28.0   # 从清零后重新起算：40-12


def test_ticks_are_bucketed_into_fixed_length_bars():
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    for i, price in enumerate([3400.0, 3401.0, 3402.0]):
        agg.add(base + i, price, 10 * (i + 1), 1000)
    agg.add(base + 65, 3410.0, 100, 1000)
    bars = agg.snapshot()
    assert [b.ts for b in bars] == [base, base + 60]
    assert bars[0].close == 3402.0 and bars[1].open == 3410.0


def test_a_late_tick_does_not_rewrite_a_closed_bar():
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 10, 3400.0, 10, 1000)
    agg.add(base + 70, 3500.0, 20, 1000)
    agg.add(base + 20, 9999.0, 30, 1000)       # 迟到的上一根 tick
    bars = agg.snapshot()
    assert bars[0].high == 3400.0, "已收口的 bar 被迟到的 tick 改写了"


def test_the_open_bar_is_included_so_indicators_are_not_a_minute_behind():
    """决策轮落在一分钟中间时，排除未收口的 bar 等于丢掉最新一分钟的全部价格。"""
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 5, 3400.0, 10, 1000)
    assert [b.close for b in agg.snapshot()] == [3400.0]


def test_seed_preheats_from_persisted_bars(source):
    """预热按**品种**存：主力换月后这段历史仍然接得上。"""
    from arena.models import Bar
    source.start()
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    source.seed_bars("RB0", [Bar(ts=base - 120, open=1, high=2, low=1, close=2),
                             Bar(ts=base - 60, open=2, high=3, low=2, close=3)])
    source._sdk.api.spi.OnRtnDepthMarketData(tick(source.dominant_contract("RB0"), 3400.0,
                                                  volume=10, open_interest=1000))
    assert [b.ts for b in source.klines("RB0", "1m", 10)][:2] == [base - 120, base - 60]


# ---------- 登录与订阅 ----------

def test_start_connects_logs_in_and_subscribes_every_candidate(source):
    source.start()
    api = source._sdk.api
    assert api.front == "tcp://127.0.0.1:10131" and api.inited
    assert api.logins == [("0001", "88888888", "secret")]
    subscribed = api.subscribed[0]
    assert len(subscribed) == 24, "两个品种各 12 个月候选"
    assert any(i.startswith("rb") for i in subscribed) and any(i.startswith("SR") for i in subscribed)
    assert source.health()["ok"] is True


def test_subscription_is_one_call_not_one_per_contract(source):
    """订阅是推送式的：一次调用挂上全部合约，之后没有任何轮询请求。

    这正是 CTP 相对 HTTP 行情源的关键区别——品种数不会把请求量线性放大。
    """
    source.start()
    assert source._sdk.api.subscribe_calls == 1


def test_a_login_failure_reports_the_broker_message(tmp_path):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays,
                          md_front="tcp://127.0.0.1:10131", broker_id="0001",
                          user_id="88888888", password="wrong",
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(FailingLoginApi))
    try:
        with pytest.raises(SourceError) as excinfo:
            src.start()
        assert "口令校验失败" in str(excinfo.value), "应当把前置给的原因透出来，而不是只说超时"
        assert src.health()["ok"] is False
    finally:
        src.close()


@pytest.mark.parametrize("missing", ["md_front", "broker_id", "user_id", "password"])
def test_incomplete_credentials_fail_before_touching_the_sdk(tmp_path, missing):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    kwargs = dict(md_front="tcp://127.0.0.1:10131", broker_id="0001", user_id="8", password="p")
    kwargs[missing] = ""
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays,
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(), **kwargs)
    try:
        with pytest.raises(SourceError) as excinfo:
            src.start()
        assert missing in str(excinfo.value)
        assert src._sdk.api is None, "凭证不全时不应当已经建好 API"
    finally:
        src.close()


# ---------- 行情 ----------

def test_ticks_become_quotes_and_klines(source):
    source.start()
    spi = source._sdk.api.spi
    rb = source.dominant_contract("RB0")
    for i in range(5):
        spi.OnRtnDepthMarketData(tick(rb, 3400.0 + i, volume=10 * (i + 1), open_interest=200000,
                                      clock=f"09:0{i}:00"))
    quotes = source.latest(["RB0"])
    assert quotes["RB0"].price == 3404.0
    assert quotes["RB0"].open_interest == 200000
    # 涨跌幅相对昨结算 3395，而不是当日开盘
    assert quotes["RB0"].change_pct == pytest.approx((3404 / 3395 - 1) * 100)
    assert len(source.klines("RB0", "1m", 10)) == 5
    assert len(source.klines("RB0", "5m", 10)) == 1


def test_a_symbol_without_ticks_is_absent_rather_than_stale(source):
    """没有 tick 的品种不返回报价，引擎按缺报价处理，不会拿旧价或假价去决策。"""
    source.start()
    assert source.latest(["RB0", "SR0"]) == {}
    with pytest.raises(SourceError):
        source.klines("RB0", "5m", 10)


def test_an_unsubscribed_instrument_is_ignored(source):
    source.start()
    source._sdk.api.spi.OnRtnDepthMarketData(tick("zz9999", 1.0, volume=1))
    assert source.latest(["RB0", "SR0"]) == {}


@pytest.mark.parametrize("bad", [CTP_NULL, 0.0, -1.0, float("nan"), float("inf")])
def test_sentinel_and_invalid_prices_never_become_quotes(source, bad):
    """尚未成交的合约最新价是 DBL_MAX；把它当价格发布出去会直接污染权益与风控。"""
    source.start()
    rb = source.dominant_contract("RB0")
    source._sdk.api.spi.OnRtnDepthMarketData(tick(rb, bad, volume=1, open_interest=100))
    assert source.latest(["RB0"]) == {}


def test_the_timestamp_comes_from_the_exchange_business_day(source):
    """夜盘跨自然日：用本机时钟会把 bar 标到错误的日期上。"""
    source.start()
    rb = source.dominant_contract("RB0")
    source._sdk.api.spi.OnRtnDepthMarketData(
        tick(rb, 3400.0, volume=1, open_interest=100, clock="21:30:00", day="20260916"))
    quote = source.latest(["RB0"])["RB0"]
    assert datetime.fromtimestamp(quote.ts, SH).strftime("%Y-%m-%d %H:%M") == "2026-09-16 21:30"


def test_a_malformed_timestamp_is_pinned_to_the_last_good_exchange_tick(source):
    """时间字段坏掉的 tick 照收，但不许它推进这条按交易所时刻排序的序列。

    原来退回本机时钟并直接送进累加器：本机时钟快出一个 bar 周期，累加器就开一根未来的 bar，
    之后所有正常的交易所 tick 都比它旧而被丢掉——这个合约的行情就此冻住。价格与持仓量是有用的，
    坏掉的只有时钟，所以钉在该合约最后一笔好 tick 的时刻上。
    """
    source.start()
    rb = source.dominant_contract("RB0")
    spi = source._sdk.api.spi
    spi.OnRtnDepthMarketData(tick(rb, 3400.0, volume=1, open_interest=100, clock="21:30:00", day="20260916"))
    good_ts = source.latest(["RB0"])["RB0"].ts

    spi.OnRtnDepthMarketData(tick(rb, 3412.0, volume=5, open_interest=180, clock="", day=""))

    quote = source.latest(["RB0"])["RB0"]
    assert quote.ts == good_ts, "坏掉的时间字段把时间轴推走了"
    assert quote.price == 3412.0, "价格是有用的，不该跟着时钟一起丢"
    assert quote.open_interest == 180


def test_a_future_local_clock_cannot_freeze_the_contract(source):
    """把本机时钟调快一天：坏时间的 tick 一旦开出未来的 bar，之后的正常 tick 就全被当成迟到。"""
    source.start()
    rb = source.dominant_contract("RB0")
    spi = source._sdk.api.spi
    spi.OnRtnDepthMarketData(tick(rb, 3400.0, volume=1, open_interest=100, clock="09:05:00", day="20260916"))
    spi.OnRtnDepthMarketData(tick(rb, 3401.0, volume=2, open_interest=100, clock="", day=""))  # 本机时钟

    spi.OnRtnDepthMarketData(tick(rb, 3420.0, volume=9, open_interest=100, clock="09:06:00", day="20260916"))

    quote = source.latest(["RB0"])["RB0"]
    assert quote.price == 3420.0, "后续正常的交易所 tick 被当成迟到丢掉了"
    assert datetime.fromtimestamp(quote.ts, SH).strftime("%H:%M") == "09:06"


def test_a_tick_with_no_good_timestamp_yet_is_dropped(source):
    """一笔好 tick 都还没有过时，本机时钟没有资格给这条序列打桩。"""
    source.start()
    rb = source.dominant_contract("RB0")
    source._sdk.api.spi.OnRtnDepthMarketData(tick(rb, 3400.0, volume=1, clock="", day=""))
    assert source.latest(["RB0"]) == {}, "用本机时钟给交易所时序打了桩"


# ---------- 主力合约 ----------

def test_the_dominant_contract_is_the_one_with_the_most_open_interest(source):
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=100_000))
    spi.OnRtnDepthMarketData(tick(far, 3450.0, volume=10, open_interest=900_000))
    assert source.dominant_contract("RB0") == far
    assert source.latest(["RB0"])["RB0"].price == 3450.0


def test_the_dominant_contract_rolls_when_open_interest_moves(source):
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=900_000))
    assert source.dominant_contract("RB0") == near
    spi.OnRtnDepthMarketData(tick(far, 3450.0, volume=10, open_interest=1_000_000))
    assert source.dominant_contract("RB0") == far, "持仓量反超后应当换月"


def test_a_late_tick_is_rejected_before_it_touches_any_metadata(source):
    """迟到的 tick 不能改写持仓量、也不能把报价时刻拉回去。

    CTP 的回调不保证有序。`TickAggregator.add()` 会丢掉落在已收口 bar 里的那笔，但丢弃是
    静默的——回调在那之前已经写过持仓量/昨结算/活跃度并重选过主力，之后又把这笔旧 tick 的
    时刻当成"最新报价时刻"发布。后果是价格一点没变，报价却凭空显得陈旧，引擎把这个品种
    移出本轮决策（持仓品种更是整轮停下）。
    """
    source.start()
    spi = source._sdk.api.spi
    rb = source.dominant_contract("RB0")
    spi.OnRtnDepthMarketData(tick(rb, 3400.0, volume=10, open_interest=200_000, clock="09:05:00"))
    spi.OnRtnDepthMarketData(tick(rb, 3410.0, volume=20, open_interest=210_000, clock="09:06:00"))
    fresh = source.latest(["RB0"])["RB0"]

    spi.OnRtnDepthMarketData(tick(rb, 3300.0, volume=30, open_interest=999_999, clock="09:04:00"))

    after = source.latest(["RB0"])["RB0"]
    assert after.ts == fresh.ts, "迟到的 tick 把报价时刻拉回去了"
    assert after.price == 3410.0
    assert after.open_interest == 210_000, "迟到的 tick 改写了持仓量"


def test_a_late_tick_cannot_resurrect_a_contract_that_already_lost_the_roll(source):
    """已经换月之后，旧主力一笔迟到的 tick 不能凭它那个旧持仓量把主力换回去。"""
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=900_000, clock="09:05:00"))
    assert source.dominant_contract("RB0") == near
    spi.OnRtnDepthMarketData(tick(far, 3450.0, volume=10, open_interest=1_000_000, clock="09:06:00"))
    assert source.dominant_contract("RB0") == far

    # 旧主力迟到的那笔带着一个更大的持仓量：照单全收就会凭空换回去
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=11, open_interest=5_000_000, clock="09:04:00"))

    assert source.dominant_contract("RB0") == far, "迟到的 tick 造出了一次假换月"


def test_a_new_dominant_tick_inside_a_closed_bar_does_not_rewind_the_quote(source):
    """换月后新主力的第一笔落在旧主力已收口的 bar 里时，不能发布它那个更早的时刻。

    两个合约各自有序，合到同一条连续序列上就未必有序——按合约的迟到判定拦不住这种情况，
    只能看累加器收没收下。
    """
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=900_000, clock="09:05:00"))
    spi.OnRtnDepthMarketData(tick(near, 3405.0, volume=20, open_interest=900_000, clock="09:06:00"))
    before = source.latest(["RB0"])["RB0"].ts

    # far 自己的第一笔，不迟到；但它落在 near 已经收口的 09:05 那根里
    spi.OnRtnDepthMarketData(tick(far, 3450.0, volume=10, open_interest=1_000_000, clock="09:05:30"))

    assert source.dominant_contract("RB0") == far, "持仓量反超仍应换月"
    assert source.latest(["RB0"])["RB0"].ts == before, "报价时刻被拉回到了已收口的那根 bar"


def test_a_roll_mid_bar_does_not_merge_two_contracts_into_one_bar():
    """换月发生在一根 bar 中途时，两个合约的价不能并进同一根。

    价位差出几百个跳价的两个合约并成一根，会造出一段从没发生过的高低点——它会被落库、
    喂进 ATR、再喂给模型。已经收口的 bar 永不改写，所以只能把这根未收口的从新主力重开。
    """
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 10, 3400.0, 10, 900_000, source="rb2605")
    agg.add(base + 20, 3402.0, 20, 900_000, source="rb2605")
    agg.add(base + 30, 3650.0, 5, 1_000_000, source="rb2610")   # 同一根桶里换月

    bars = agg.snapshot()
    assert len(bars) == 1, "同一个桶被写成了两根 bar"
    bar = bars[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (3650.0, 3650.0, 3650.0, 3650.0), \
        "两个合约的价并进了同一根，造出一段没发生过的行情"


def test_seeding_older_history_keeps_the_live_bar_source():
    """预热的历史全在实时 bar 之前时，那根未收口的 bar 仍然只来自实时那个合约。

    把来源清成 None，同一根 bar 里再发生换月，add() 的换源判定就失效——两个合约的价被并成
    一段没发生过的行情。引擎在 start() 里预热，而 CTP 在引擎构造期间就可能推来 tick，
    所以这条路径在真实运行中是会走到的。
    """
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 10, 3400.0, 10, 900_000, source="rb2605")          # 先有实时 tick
    agg.seed([Bar(ts=base - 120, open=3380.0, high=3385.0, low=3375.0,
                  close=3382.0, volume=5, hold=900_000)])              # 再预热更早的历史

    agg.add(base + 30, 3650.0, 5, 1_000_000, source="rb2610")          # 同一根桶里换月

    bar = agg.snapshot()[-1]
    assert (bar.open, bar.high, bar.low, bar.close) == (3650.0, 3650.0, 3650.0, 3650.0), \
        "预热把实时 bar 的来源清掉了，换月又并进了同一根"


def test_seeding_never_merges_persisted_bars_into_a_live_one():
    """重启后落在同一个桶上的预热 bar 必须整根丢掉，不能并进实时那根。

    落库的 bar 没有记来源合约，而重启后第一笔 tick 完全可能来自新选出的主力。并起来就是
    一段没发生过的行情（3390–3395 的旧合约配 3650 的新合约），还会被落库、喂进 ATR。
    只清 `_bar_source` 是不够的——合并本身已经把假行情做出来了。
    """
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 10, 3650.0, 10, 900_000, source="rb2610")           # 重启后的新主力
    agg.seed([Bar(ts=base, open=3390.0, high=3395.0, low=3385.0,       # 库里那半根（旧主力）
                  close=3392.0, volume=7, hold=900_000)])

    bar = agg.snapshot()[-1]
    assert (bar.open, bar.high, bar.low, bar.close) == (3650.0, 3650.0, 3650.0, 3650.0), \
        "库里那半根被并进了实时这根，造出一段没发生过的行情"
    assert agg._bar_source == "rb2610", "实时这根的来源被清掉了，后续换月判定会失效"


def test_seeding_older_history_still_attaches_in_front():
    """更早的历史各自成根、不参与合并，照常接在实时序列前面。"""
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 10, 3400.0, 10, 900_000, source="rb2605")
    agg.seed([Bar(ts=base - 60, open=3390.0, high=3395.0, low=3385.0, close=3392.0,
                  volume=7, hold=900_000)])
    assert [b.ts for b in agg.snapshot()] == [base - 60, base]


def test_a_seeded_open_bar_is_replaced_by_the_first_sourced_tick():
    """预热那半根的来源不可证，所以带来源的第一笔 tick 到达时整根作废、从它重开。

    "来源未知"不等于"来源相同"。重启后第一笔 tick 完全可能来自新选出的主力——库里 3390 的
    旧合约配上 9000 的新合约，并出来就是一段没发生过的行情，还会被落库、喂进 ATR。
    代价是重启那一瞬间那根未收口的 bar 只剩重启后的部分。要恢复合并，得先把来源合约持久化
    （`md_bars` 现在没有这一列）。
    """
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.seed([Bar(ts=base, open=3390.0, high=3395.0, low=3385.0, close=3392.0, volume=7, hold=900_000)])
    agg.add(base + 30, 9000.0, 10, 900_000, source="rb2610")      # 换月后的新主力
    bar = agg.snapshot()[-1]
    assert (bar.open, bar.high, bar.low, bar.close) == (9000.0, 9000.0, 9000.0, 9000.0), \
        "库里那半根被并进来了，造出 3390–9000 这段没发生过的行情"
    assert agg._bar_source == "rb2610"


def test_add_reports_whether_the_tick_was_accepted():
    """调用方要靠这个返回值决定发不发布时间戳，所以它必须是可信的。"""
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    assert agg.add(base + 10, 3400.0, 10, 1000) is True      # 开一根新的
    assert agg.add(base + 20, 3410.0, 20, 1000) is True      # 并进同一根
    assert agg.add(base + 70, 3500.0, 30, 1000) is True      # 收口再开一根
    assert agg.add(base + 30, 9999.0, 40, 1000) is False     # 迟到


def test_quote_metadata_comes_from_the_contract_that_supplied_the_price(source):
    """换月选出来了、但新主力第一笔被累加器丢掉时，报价不能拼两个合约的数据。

    价与时刻还来自旧主力，昨结算与持仓量却取新主力，会算出一个自相矛盾的涨跌幅——
    两个合约的价位差几百个跳价时，这个数字完全是假的。
    """
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=900_000,
                                  clock="09:05:00", settlement=3395.0))
    spi.OnRtnDepthMarketData(tick(near, 3405.0, volume=20, open_interest=900_000,
                                  clock="09:06:00", settlement=3395.0))
    # far 持仓量反超、成为主力，但它这一笔落在 near 已收口的 09:05 那根里，会被丢掉
    spi.OnRtnDepthMarketData(tick(far, 3650.0, volume=10, open_interest=1_000_000,
                                  clock="09:05:30", settlement=3640.0))

    assert source.dominant_contract("RB0") == far, "持仓量反超仍应换月"
    assert source.publishing_contract("RB0") == near, "数据面不该在新主力落地之前就切过去"
    quote = source.latest(["RB0"])["RB0"]
    assert quote.price == 3405.0
    assert quote.open_interest == 900_000, "报价混进了新合约的持仓量"
    assert quote.change_pct == pytest.approx((3405 / 3395 - 1) * 100), "涨跌幅拿新合约的昨结算去比旧合约的价"


def test_the_old_contract_keeps_supplying_until_the_new_one_lands_a_tick(source):
    """这段时间里若两边都进不去，行情会冻在旧主力最后那个收盘价上。"""
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=900_000, clock="09:05:00"))
    spi.OnRtnDepthMarketData(tick(near, 3405.0, volume=20, open_interest=900_000, clock="09:06:00"))
    spi.OnRtnDepthMarketData(tick(far, 3650.0, volume=10, open_interest=1_000_000, clock="09:05:30"))

    spi.OnRtnDepthMarketData(tick(near, 3412.0, volume=30, open_interest=900_000, clock="09:07:00"))

    quote = source.latest(["RB0"])["RB0"]
    assert quote.price == 3412.0, "旧主力的后续 tick 被丢了，行情冻住"
    assert datetime.fromtimestamp(quote.ts, SH).strftime("%H:%M") == "09:07"


def test_the_data_plane_switches_once_the_new_contract_is_accepted(source):
    """新主力真有一笔被收下之后，价、时刻、昨结算、持仓量必须整体切过去。"""
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=900_000,
                                  clock="09:05:00", settlement=3395.0))
    spi.OnRtnDepthMarketData(tick(far, 3650.0, volume=10, open_interest=1_000_000,
                                  clock="09:06:00", settlement=3640.0))

    assert source.publishing_contract("RB0") == far
    quote = source.latest(["RB0"])["RB0"]
    assert quote.price == 3650.0
    assert quote.open_interest == 1_000_000
    assert quote.change_pct == pytest.approx((3650 / 3640 - 1) * 100)


def test_a_pinned_instrument_is_never_auto_rolled(tmp_path):
    """配置里显式指定了合约就以它为准，不能被自动选主力覆盖掉。"""
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays,
                          md_front="tcp://127.0.0.1:10131", broker_id="0001", user_id="8",
                          password="p", instruments={"RB0": "rb2605"}, discovery_grace_sec=0,
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        src.start()
        assert src._sdk.api.subscribed[0] == ["rb2605"], "指定合约时不应再枚举候选"
        src._sdk.api.spi.OnRtnDepthMarketData(tick("rb2605", 3400.0, volume=1, open_interest=1))
        assert src.dominant_contract("RB0") == "rb2605"
    finally:
        src.close()


# ---------- 连接状态 ----------

def test_a_disconnect_is_reported_and_resubscribes_on_relogin(source):
    source.start()
    api = source._sdk.api
    api.spi.OnFrontDisconnected(0x1001)
    assert source.health()["ok"] is False and "断开" in source.health()["detail"]
    api.spi.OnFrontConnected()                    # SDK 自动重连后重新登录
    assert source.health()["ok"] is True
    assert api.subscribe_calls >= 2, "重连后必须重新订阅，否则再也收不到行情"


def test_a_callback_exception_does_not_kill_the_feed(source):
    """回调跑在 SDK 自己的线程上，异常逃出去会掐断整条行情推送。"""
    source.start()
    spi = source._sdk.api.spi
    spi.OnRtnDepthMarketData(SimpleNamespace())   # 缺字段，内部会抛
    rb = source.dominant_contract("RB0")
    spi.OnRtnDepthMarketData(tick(rb, 3400.0, volume=1, open_interest=100))
    assert source.latest(["RB0"])["RB0"].price == 3400.0


def test_close_releases_the_api(source):
    source.start()
    api = source._sdk.api
    source.close()
    assert api.released is True


def test_sessions_come_from_the_contract_table(source):
    """开市判断沿用合约表的时段，与新浪源同一套口径。"""
    assert source.is_open(datetime(2026, 9, 16, 22, 0, tzinfo=SH), "RB0") is True
    assert source.is_open(datetime(2026, 9, 16, 3, 0, tzinfo=SH), "RB0") is False
    assert source.next_open(datetime(2026, 9, 16, 3, 0, tzinfo=SH)) is not None


# ---------- 端到端：真实引擎 + 假 SDK ----------

def make_ctp_cfg(tmp_path, timezone="Asia/Shanghai", **ctp):
    from arena.config import AppConfig, TraderCfg
    base = {"md_front": "tcp://127.0.0.1:10131", "broker_id": "0001", "user_id": "88888888",
            "password": "secret", "flow_dir": str(tmp_path / "flow"), "auto_months": 2,
            "discovery_grace_sec": 0}
    base.update(ctp)
    return AppConfig(
        competition={"name": "ctp-e2e", "market": "ctp", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1,
                     "timezone": timezone},
        symbols=["RB0", "I0"],
        market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json"}, "ctp": base},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="rule", fallback_rule="trend")],
        storage={"db_path": str(tmp_path / "ctp.db")},
        base_dir=ROOT,
    )


def build_engine(tmp_path, monkeypatch, timezone="Asia/Shanghai", **ctp):
    """真实 Engine、真实 CtpMarketSource，只把 SDK 的加载换成假的。

    不替换 `CtpMarketSource` 这个名字本身——引擎里用 `isinstance` 判断要不要落库行情 bar，
    把类换成工厂函数会让那个判断永远为假，测试就测不到真实路径了。
    """
    from arena.db import Database
    from arena.engine import loop as loop_mod

    def fake_load(self):
        self._sdk = FakeSdk()
        return self._sdk

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    cfg = make_ctp_cfg(tmp_path, timezone=timezone, **ctp)
    db = Database(cfg.db_path)
    return loop_mod.Engine(cfg, db, accelerate=True), db


def feed(engine, symbol, prices, open_interest=500_000):
    """给该品种的主力合约灌一串 tick，每个基础周期一根，**最后一根落在当前时刻**。

    时间戳必须是新鲜的：引擎对实盘源有新鲜度闸门（`_live_bar_admission_reason`），
    几天前的 bar 会被整批丢掉，于是快照为空——而用例看上去只是"没建出快照"。
    这正是"喂给被测代码的数据要符合真实行为"那条教训的同一形态。
    """
    spi = engine.source._sdk.api.spi
    instrument = engine.source.dominant_contract(symbol)
    step = engine.source.bar_seconds
    now = time.time()
    for i, price in enumerate(prices):
        at = datetime.fromtimestamp(now - (len(prices) - 1 - i) * step, SH)
        spi.OnRtnDepthMarketData(tick(instrument, float(price), volume=10 * (i + 1),
                                      open_interest=open_interest,
                                      clock=at.strftime("%H:%M:%S"), day=at.strftime("%Y%m%d")))


def test_engine_builds_a_ctp_source_and_prices_flow_through(tmp_path, monkeypatch):
    """market: ctp 时引擎应当建出 CTP 源、登录订阅，并把 tick 变成可用报价。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        assert isinstance(eng.source, CtpMarketSource)
        assert eng.source._sdk.api.logins == [("0001", "88888888", "secret")]
        assert len(eng.source._sdk.api.subscribed[0]) == 4      # 两个品种 × 两个月
        feed(eng, "RB0", [3400, 3405, 3410])
        feed(eng, "I0", [780, 782, 784])
        quotes = eng.source.latest(eng.symbols)
        assert quotes["RB0"].price == 3410.0 and quotes["I0"].price == 784.0
        assert eng.source.health()["ok"] is True
    finally:
        eng.stop()
        db.close()


def test_bars_survive_a_restart(tmp_path, monkeypatch):
    """CTP 不给历史 K 线，全靠本地积累。不落库的话每次重开软件都要从头攒 1h 序列。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        feed(eng, "RB0", [3400 + i for i in range(30)])
        eng._persist_md_bars()
        assert len(db.md_bars("RB0")) == 30
    finally:
        eng.stop()
        db.close()

    eng2, db2 = build_engine(tmp_path, monkeypatch)
    try:
        eng2._seed_md_bars()
        bars = eng2.source.klines("RB0", "1m", 100)
        assert len(bars) == 30, "重启后未从数据库预热"
        assert bars[-1].close == 3429.0
    finally:
        eng2.stop()
        db2.close()


def test_persisting_twice_updates_the_open_bar_instead_of_duplicating(tmp_path, monkeypatch):
    """最后一根 bar 在它那一分钟内会被反复更新：只插不更会把它冻结在第一个 tick 的价格上。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        feed(eng, "RB0", [3400])
        eng._persist_md_bars()
        assert [r["close"] for r in db.md_bars("RB0")] == [3400.0]
        # 同一根 bar 内的第二个 tick：从**上一笔 tick 的时刻**往后半秒，而不是从那根 bar 的起点
        # 往后一秒——feed() 的最后一根落在当前时刻，bar 起点通常在它之前，那样构造出来的是一笔
        # 乱序 tick（真实 CTP 里它会被丢掉），测的就不是"同一根 bar 被更新"了。
        last_ts = eng.source._last_tick_at["RB0"]
        at = datetime.fromtimestamp(last_ts, SH)
        spi = eng.source._sdk.api.spi
        spi.OnRtnDepthMarketData(tick(eng.source.dominant_contract("RB0"), 3450.0, volume=99,
                                      open_interest=500_000, clock=at.strftime("%H:%M:%S"),
                                      day=at.strftime("%Y%m%d"), millisec=500))
        assert eng.source._last_tick_at["RB0"] == last_ts + 0.5, "这一笔应当被收下"
        eng._persist_md_bars()
        rows = db.md_bars("RB0")
        assert len(rows) == 1, "同一根 bar 被写成了两行"
        assert rows[0]["close"] == 3450.0, "同一根 bar 的更新没有写回去"
    finally:
        eng.stop()
        db.close()


def test_persistence_can_be_switched_off(tmp_path, monkeypatch):
    eng, db = build_engine(tmp_path, monkeypatch, persist_bars=False)
    try:
        feed(eng, "RB0", [3400, 3401])
        eng._persist_md_bars()
        assert db.md_bars("RB0") == []
    finally:
        eng.stop()
        db.close()


def test_saved_bars_are_capped_per_symbol(tmp_path, monkeypatch):
    from arena.db import Database
    from arena.models import Bar
    db = Database(str(tmp_path / "cap.db"))
    try:
        base = 1_700_000_000.0
        db.save_md_bars("RB0", [Bar(ts=base + i * 60, open=1, high=1, low=1, close=float(i))
                                for i in range(50)], keep=20)
        rows = db.md_bars("RB0", 100)
        assert len(rows) == 20 and rows[-1]["close"] == 49.0 and rows[0]["close"] == 30.0
    finally:
        db.close()


# ---------- Regression 65：审查发现的四条 ----------

def test_slow_periods_are_bucketed_by_time_not_by_bar_count():
    """按根数分块只在"每根都是一分钟且一分钟不落"时才等于目标周期，而这两条都不成立。"""
    from arena.market.ctp import resample
    from arena.models import Bar
    base = 1_700_000_000 - (1_700_000_000 % 3600)
    # 09:00、09:05、09:10 三根，然后跳到 10:00（中间没有成交）
    bars = [Bar(ts=base, open=1, high=5, low=1, close=4, volume=10),
            Bar(ts=base + 300, open=4, high=6, low=3, close=5, volume=10),
            Bar(ts=base + 600, open=5, high=7, low=4, close=6, volume=10),
            Bar(ts=base + 3600, open=9, high=9, low=9, close=9, volume=10)]
    hours = resample(bars, 3600)
    assert [b.ts for b in hours] == [base, base + 3600], "缺失的分钟应当让桶变薄，而不是把两小时揉成一根"
    assert hours[0].open == 1 and hours[0].close == 6 and hours[0].high == 7 and hours[0].volume == 30
    assert hours[1].open == 9 and hours[1].volume == 10


def test_a_non_default_bar_length_still_produces_true_periods(tmp_path):
    """bar_seconds=300 时，"5m" 必须还是五分钟——按根数算会变成二十五分钟。"""
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays,
                          md_front="tcp://127.0.0.1:10131", broker_id="0001", user_id="8",
                          password="p", bar_seconds=300, discovery_grace_sec=0,
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        src.start()
        spi = src._sdk.api.spi
        rb = src.dominant_contract("RB0")
        for i in range(12):                        # 一小时 = 12 根五分钟
            spi.OnRtnDepthMarketData(tick(rb, 3400.0 + i, volume=10 * (i + 1), open_interest=1000,
                                          clock=f"09:{i * 5:02d}:00"))
        assert len(src.klines("RB0", "5m", 50)) == 12, "bar_seconds 就是 5m，应当原样返回"
        hours = src.klines("RB0", "1h", 50)
        assert len(hours) == 1, f"12 根五分钟应当正好一小时，得到 {len(hours)} 根"
        with pytest.raises(SourceError):
            src.klines("RB0", "1m", 10)            # 比聚合周期还短，造不出来
    finally:
        src.close()


def test_a_session_break_does_not_stretch_a_bar(source):
    """日盘收盘到夜盘开盘之间隔着好几个小时，按根数分块会造出横跨大半天的"1 小时"。"""
    source.start()
    spi = source._sdk.api.spi
    rb = source.dominant_contract("RB0")
    for clock in ("14:30:00", "14:45:00", "21:00:00", "21:15:00"):
        spi.OnRtnDepthMarketData(tick(rb, 3400.0, volume=10, open_interest=1000, clock=clock))
    hours = source.klines("RB0", "1h", 50)
    spans = {datetime.fromtimestamp(b.ts, SH).hour for b in hours}
    assert spans == {14, 21}, f"时段间断被揉进了同一根：{sorted(spans)}"


def test_preheated_history_survives_a_dominant_contract_switch(source):
    """预热塞进"第一个候选合约"的话，主力一换（远月才是主力）历史就全丢。"""
    from arena.models import Bar
    source.start()
    base = 1_700_000_000 - (1_700_000_000 % 60)
    source.seed_bars("RB0", [Bar(ts=base - 60 * (10 - i), open=1, high=2, low=1, close=float(i))
                             for i in range(10)])
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=100_000))
    spi.OnRtnDepthMarketData(tick(far, 3450.0, volume=10, open_interest=900_000))
    assert source.dominant_contract("RB0") == far, "前提不成立：主力没有切到远月"
    bars = source.klines("RB0", "1m", 50)
    assert len(bars) == 11, f"预热的十根历史在换月后丢了，只剩 {len(bars)} 根"
    assert bars[-1].close == 3450.0


def test_a_partial_bar_is_replaced_by_post_restart_ticks(source):
    """重启正好落在某根 bar 中途：库里那根是"半根"，带来源的 tick 到达时它整根作废。

    原来这里断言的是"必须并进去"。但落库的 bar 没有记来源合约，而重启后第一笔 tick 可能来自
    新选出的主力——合并出来的高低点是一段没发生过的行情，会被落库并喂进 ATR。两害相权，
    丢掉一根未收口的 bar 好过造一段假行情。仍然断言完整的 OHLCV，只是断言的是替换而非合并。
    """
    from arena.models import Bar
    source.start()
    # 先预热（生产里 _seed_md_bars 就在 start() 之后、第一条 tick 之前），最后一根是半根
    partial_ts = 1_700_000_000 - (1_700_000_000 % 60)
    source.seed_bars("RB0", [Bar(ts=partial_ts - 60, open=1, high=1, low=1, close=1, volume=5),
                             Bar(ts=partial_ts, open=3390.0, high=3395.0, low=3385.0,
                                 close=3392.0, volume=7)])
    spi = source._sdk.api.spi
    rb = source.dominant_contract("RB0")
    # 时间串必须按交易所时区构造：源就是按它解析的，用容器本地时区会算出差八小时的桶，
    # 那条 tick 会被当成迟到直接丢弃，而用例看上去只是"没合并"
    at = datetime.fromtimestamp(partial_ts + 30, SH)
    clock, day = at.strftime("%H:%M:%S"), at.strftime("%Y%m%d")
    spi.OnRtnDepthMarketData(tick(rb, 3400.0, volume=99, open_interest=1000, clock=clock, day=day))

    bars = source.klines("RB0", "1m", 10)
    assert [b.ts for b in bars] == [partial_ts - 60, partial_ts], f"出现重复的 bar：{[b.ts for b in bars]}"
    current = bars[-1]
    assert (current.open, current.high, current.low, current.close) == (3400.0, 3400.0, 3400.0, 3400.0), \
        "库里那半根被并进来了——来源证明不了时不能合并"
    assert current.volume == 0.0, "首个 tick 的累计值不可比，本桶只能从 0 起算"


def test_ctp_credentials_never_reach_the_competition_snapshot(tmp_path, monkeypatch):
    """快照会写进 competitions.config_json：漏掉的凭证字段会在数据库里留下永久副本。"""
    from arena.db import Database
    eng, db = build_engine(tmp_path, monkeypatch, password="SECRET-PW", password_env="ALSO-SECRET",
                           user_id="99998888")
    try:
        snapshot = eng._config_snapshot_json()
        for secret in ("SECRET-PW", "ALSO-SECRET", "99998888"):
            assert secret not in snapshot, f"快照里出现了凭证 {secret}"
        rows = db._q("SELECT config_json FROM competitions", ())
        for row in rows:
            for secret in ("SECRET-PW", "ALSO-SECRET", "99998888"):
                assert secret not in (row["config_json"] or "")
        # 非凭证字段仍然保留，快照的可复现用途不受影响
        assert "tcp://127.0.0.1:10131" in snapshot
    finally:
        eng.stop()
        db.close()


def test_a_late_tick_does_not_move_the_volume_baseline():
    """迟到的 tick 累计成交量更低；先更新基准再丢弃它，下一个正常 tick 会把中间那段重复计入。

    还原成"先更新基准再判迟到"后本用例失败（会得到 60 而不是 10）。
    """
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 10, 3400.0, 200, 1000)      # 开一根，基准 200
    agg.add(base + 70, 3410.0, 205, 1000)      # 下一根，+5
    agg.add(base + 20, 3390.0, 150, 1000)      # 迟到：累计值更低，必须整条丢弃
    agg.add(base + 80, 3420.0, 215, 1000)      # 正常：相对 205 应当是 +10
    bars = agg.snapshot()
    assert bars[-1].volume == 15.0, f"第二根的成交量是 {bars[-1].volume}，应当是 5+10"
    assert bars[0].high == 3400.0, "迟到的 tick 改写了已收口的 bar"
    assert bars[-1].close == 3420.0 and bars[-1].low == 3410.0, "迟到的价格混进了后一根"


@pytest.mark.parametrize("bad_seconds", [
    90, 200, 7,           # 不整除 300：一根基础 bar 横跨 5m 边界，指标悄悄变形
    900, 1800, 3600,      # 比 5m 还长：`_build_snapshot` 要不到 5m，快照永远是空的
    1, 5,                 # 短于 10 秒：整除 300 没问题，但 1h 的保留根数会涨到几 GB
])
def test_an_unusable_bar_length_is_rejected_at_construction(tmp_path, bad_seconds):
    """三类坏配置在构造时挡掉：必须能整除 300 秒（引擎的快照每次都先要 5m），且不短于 10 秒。

    比 5m 长这一类尤其隐蔽——不报错、不崩溃，只是每一轮快照都空、一个交易员都不会被调用，
    日志里只有几行"K线获取失败"。
    太短的那一类则是另一头：保留根数与 bar_seconds 成反比（见 `retention_floor`），
    1 秒的 bar 每个品种要留 216000 根才算得出 1h 的 ema60，七十个品种直接把内存吃光。
    """
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    with pytest.raises(ValueError) as excinfo:
        CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:1",
                        broker_id="0001", user_id="8", password="p", bar_seconds=bad_seconds,
                        flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    assert str(bad_seconds) in str(excinfo.value)


@pytest.mark.parametrize("ok_seconds", [30, 60, 150, 300])
def test_an_accepted_bar_length_actually_produces_a_snapshot(tmp_path, monkeypatch, ok_seconds):
    """接受一个取值，等于承诺它能跑——所以断言**引擎真的建得出快照**，而不只是构造没抛错。

    上一版这里只断言了"构造成功"，于是把 900/1800/3600 这几个会让整机不决策的取值
    当成合法配置锁了下来：测试不仅没抓住问题，还给它发了证。
    """
    eng, db = build_engine(tmp_path, monkeypatch, bar_seconds=ok_seconds)
    try:
        feed(eng, "RB0", [3400 + i for i in range(80)])
        feed(eng, "I0", [780 + i for i in range(80)])
        # 直接把报价传进去（决策轮也是这么做的）：否则用例会依赖"现在是不是开市"，
        # 周末跑就必然失败——那种失败与被测的 bar_seconds 毫无关系
        snap = eng._build_snapshot(quotes=eng.source.latest(eng.symbols))
        assert snap is not None and snap.symbols, f"bar_seconds={ok_seconds} 建不出快照"
        view = snap.symbols["RB0"]
        assert view.bars_5m, "快照里没有 5m 序列"
        assert view.ind_5m.get("last") is not None
    finally:
        eng.stop()
        db.close()


def test_the_display_timezone_does_not_reinterpret_exchange_timestamps(tmp_path, monkeypatch):
    """`competition.timezone` 是看板的显示时区，不是交易所的时区。

    CTP 推的 `ActionDay`/`UpdateTime` 永远是交易所本地时间（上海）。拿显示时区去解释它，
    `timezone: UTC` 下一条 10:00 的 tick 会被当成 10:00 UTC——比现在早八个小时，于是
    `_live_bar_admission_reason` 判"K线时间异常"整批丢掉、报价也永远是"未来的"，
    CTP 的决策轮一轮都跑不起来。新浪源早就把这条分开了（`_local` 用交易所时区），
    CTP 这边不能又合回去。
    """
    eng, db = build_engine(tmp_path, monkeypatch, timezone="UTC")
    try:
        feed(eng, "RB0", [3400 + i for i in range(80)])
        quote = eng.source.latest(["RB0"])["RB0"]
        assert abs(quote.ts - time.time()) < 120, \
            f"报价时间偏了 {(quote.ts - time.time()) / 3600:.1f} 小时——时区解释错了"
        snap = eng._build_snapshot(quotes=eng.source.latest(eng.symbols))
        assert snap is not None and "RB0" in snap.symbols, "所有 bar 都被当成未来时间丢掉了"
        assert str(eng.source.tz) == "Asia/Shanghai", "行情源拿了看板的显示时区去解释交易所时间戳"
    finally:
        eng.stop()
        db.close()


def test_a_short_bar_length_raises_the_retention_to_what_1h_needs(tmp_path):
    """保留根数必须随 bar_seconds 反比缩放，否则 1h 的慢指标永远算不出来。"""
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:1",
                          broker_id="0001", user_id="8", password="p", bar_seconds=30,
                          max_bars=3000, flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        assert retention_floor(30) == 7200      # 60 根 1h × 3600 秒 ÷ 30 秒
        assert src.max_bars == 7200, "30 秒的基础 bar 留 3000 根只有 25 小时，凑不满 60 根 1h"
        assert src._agg["RB0"].max_bars == 7200, "抬高的只是字段，聚合器还按旧值淘汰"
    finally:
        src.close()


def test_sub_minute_bars_still_reach_the_1h_indicators(tmp_path, monkeypatch):
    """接受 30 秒的基础周期，就得保证 1h 的**全套**指标最终真的算得出来。

    之前那条验收用例只断言"快照非空"，而 ema60 缺失恰恰不会让快照变空——1h 指标整块是
    None、其余照常，快照、报价、决策全都正常。所以这里直接断 `ind_1h["ema60"]`，
    并且一路断到落库：`save_md_bars` 的默认 keep=5000 同样会把 30 秒周期下该留的 7200 根裁掉，
    于是重启后 1h 又从零开始攒。
    """
    need = retention_floor(30)
    eng, db = build_engine(tmp_path, monkeypatch, bar_seconds=30)   # max_bars 用默认值
    try:
        feed(eng, "RB0", [3400 + (i % 40) for i in range(need)])
        assert len(eng.source.klines("RB0", "1h", 200)) >= INDICATOR_MIN_BARS, \
            f"喂满 {need} 根 30 秒 bar（60 小时）之后，1h 序列仍然不足 {INDICATOR_MIN_BARS} 根"
        snap = eng._build_snapshot(quotes=eng.source.latest(eng.symbols))
        assert snap is not None and "RB0" in snap.symbols
        assert snap.symbols["RB0"].ind_1h.get("ema60") is not None, "1h 的 ema60 永久缺失"
        assert snap.symbols["RB0"].ind_1h.get("macd_dif") is not None
        eng.stop()      # 落库发生在 stop() 里
        rows = db.md_bars("RB0", limit=need * 2, bar_seconds=30)
        assert len(rows) >= need, f"库里只留下 {len(rows)} 根，重启预热后照样凑不满 1h"
    finally:
        eng.stop()
        db.close()

    # 重启后只预热、不喂任何新 tick：1h 指标就该是齐的——这正是把 bar 落库的理由
    eng2, db2 = build_engine(tmp_path, monkeypatch, bar_seconds=30)
    try:
        eng2._seed_md_bars()
        assert len(eng2.source.klines("RB0", "1h", 200)) >= INDICATOR_MIN_BARS, \
            "预热只读回了配置里那个根数，1h 又要从零攒一遍"
    finally:
        eng2.stop()
        db2.close()


def test_a_rollover_does_not_rewrite_history_already_published(source):
    """换月只改变"之后由谁供数"；已经发布出去的 bar 永不改写。

    按合约各存一份、用时取当前主力那份的话，一次换月会把整个进程生命期的价格与成交量
    追溯性替换成新合约的——落库时还会 upsert 覆盖历史，实验结果就此失真。
    """
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]

    # 近月当主力，两根 bar；远月同时也在走行情（价格明显不同），但不该进入连续序列
    for i, clock in enumerate(("09:00:00", "09:01:00")):
        spi.OnRtnDepthMarketData(tick(near, 3400.0 + i, volume=10 * (i + 1),
                                      open_interest=900_000, clock=clock))
        spi.OnRtnDepthMarketData(tick(far, 9000.0 + i, volume=10 * (i + 1),
                                      open_interest=100_000, clock=clock))
    published = [b.close for b in source.klines("RB0", "1m", 10)]
    assert published == [3400.0, 3401.0], f"非主力合约的行情混进了连续序列：{published}"

    # 远月持仓量反超，成为主力
    spi.OnRtnDepthMarketData(tick(far, 9002.0, volume=40, open_interest=2_000_000, clock="09:02:00"))
    assert source.dominant_contract("RB0") == far
    after = [b.close for b in source.klines("RB0", "1m", 10)]
    assert after[:2] == [3400.0, 3401.0], f"换月把已发布的历史改写了：{after}"
    assert after[-1] == 9002.0, "换月后应当由新主力供数"


def test_a_rollover_does_not_fabricate_a_volume_spike(source):
    """两个合约的累计成交量毫无关系，换月后直接相减会凭空造出一个巨大的增量。"""
    source.start()
    spi = source._sdk.api.spi
    near, far = source._candidates["RB0"][0], source._candidates["RB0"][3]
    spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=100, open_interest=900_000, clock="09:00:00"))
    spi.OnRtnDepthMarketData(tick(near, 3401.0, volume=150, open_interest=900_000, clock="09:01:00"))
    # 远月的累计成交量高得多
    spi.OnRtnDepthMarketData(tick(far, 9000.0, volume=9_999_999, open_interest=2_000_000,
                                  clock="09:02:00"))
    assert source.dominant_contract("RB0") == far
    last = source.klines("RB0", "1m", 10)[-1]
    assert last.volume == 0.0, f"换月那根凭空多出 {last.volume} 的成交量"
    spi.OnRtnDepthMarketData(tick(far, 9001.0, volume=10_000_009, open_interest=2_000_000,
                                  clock="09:03:00"))
    assert source.klines("RB0", "1m", 10)[-1].volume == 10.0, "换月之后应当正常按新合约做差"


def test_bars_are_flushed_on_clean_shutdown(tmp_path, monkeypatch):
    """最后一次定期快照之后的 tick 只存在内存里，CTP 又补不回历史：停机前必须冲一次。

    还原成"stop() 不落库"后本用例失败（库里一根都没有）。
    """
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        feed(eng, "RB0", [3400, 3405, 3410])
        assert db.md_bars("RB0") == [], "前提不成立：定期快照已经落过库了"
        eng.stop()                       # 只停一次，不经过任何定期快照
        rows = db.md_bars("RB0")
        assert len(rows) == 3, f"停机前没有落库，丢了 {3 - len(rows)} 根"
        assert rows[-1]["close"] == 3410.0
    finally:
        db.close()


def test_history_from_a_different_bar_length_is_not_reused(tmp_path, monkeypatch):
    """`bar_seconds` 改了之后旧行不能再读回来：否则指标把每一根旧的一分钟当成五分钟用。

    还原成"读历史时不按 bar_seconds 过滤"后本用例失败。
    """
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        feed(eng, "RB0", [3400 + i for i in range(5)])
        eng._persist_md_bars()
        assert len(db.md_bars("RB0", bar_seconds=60)) == 5
    finally:
        eng.stop()
        db.close()

    # 换成五分钟的基础周期重启
    eng2, db2 = build_engine(tmp_path, monkeypatch, bar_seconds=300)
    try:
        eng2._seed_md_bars()
        with pytest.raises(SourceError):
            eng2.source.klines("RB0", "5m", 10)   # 旧的一分钟行不该被当成五分钟读回来
        assert db2.md_bars("RB0", bar_seconds=60) != [], "旧行本身应当还在，只是不被这次使用"
    finally:
        eng2.stop()
        db2.close()


# ---------- Regression 66：启动/停机的时序 ----------

def test_initial_discovery_does_not_publish_before_it_settles(tmp_path):
    """启动瞬间 `_ticked` 是空的，谁先回调谁就是"持仓量最大"。

    近月先到、真正的主力后到，就会被当成一次换月——两个价位截然不同的合约并进同一根 bar，
    凭空造出高低点与 ATR，而那只是回调顺序。
    还原成"没有观察期"后本用例失败（会看到一根 3400–9000 的 bar）。
    """
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:1",
                          broker_id="0001", user_id="8", password="p", auto_months=4,
                          discovery_grace_sec=0.4, flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        src.start()
        spi = src._sdk.api.spi
        near, far = src._candidates["RB0"][0], src._candidates["RB0"][3]
        # 近月先回调，远月（真正的主力）后回调
        spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=100_000))
        spi.OnRtnDepthMarketData(tick(far, 9000.0, volume=10, open_interest=900_000))
        assert src._continuous_bars("RB0") == [], "观察期内不该发布任何 bar"
        assert src.dominant_contract("RB0") == far, "观察期内仍要收集持仓量并选出主力"

        time.sleep(0.45)
        spi.OnRtnDepthMarketData(tick(far, 9001.0, volume=20, open_interest=900_000))
        bars = src._continuous_bars("RB0")
        assert len(bars) == 1
        assert bars[0].high == 9001.0 and bars[0].low == 9001.0, (
            f"近月的 3400 混进了同一根 bar：high={bars[0].high} low={bars[0].low}")
    finally:
        src.close()


def test_seeding_after_live_ticks_does_not_erase_them(source):
    """订阅发生在引擎构造期间、预热在 Engine.start() 里：两者之间到达的 tick 不能被预热覆盖。

    同一个桶上的那根**整根丢掉**，不再与实时那根合并：落库的 bar 没有记来源合约，
    而重启后第一笔 tick 可能来自新选出的主力，合并出来的高低点是一段没发生过的行情。
    代价是重启那一瞬间那根未收口的 bar 只剩重启后的部分；更早的历史照常接在前面。
    """
    from arena.models import Bar
    source.start()
    spi = source._sdk.api.spi
    rb = source.dominant_contract("RB0")
    now = time.time()
    at = datetime.fromtimestamp(now, SH)
    spi.OnRtnDepthMarketData(tick(rb, 3500.0, volume=10, open_interest=1000,
                                  clock=at.strftime("%H:%M:%S"), day=at.strftime("%Y%m%d")))
    live_ts = source.bars_1m("RB0")[-1].ts

    # 预热在实时 tick **之后**才到（就是真实的启动顺序）
    source.seed_bars("RB0", [Bar(ts=live_ts - 120, open=1, high=1, low=1, close=1, volume=3),
                             Bar(ts=live_ts - 60, open=2, high=2, low=2, close=2, volume=4),
                             Bar(ts=live_ts, open=3490.0, high=3495.0, low=3485.0,
                                 close=3492.0, volume=5)])
    bars = source.bars_1m("RB0")
    assert [b.ts for b in bars] == [live_ts - 120, live_ts - 60, live_ts]
    current = bars[-1]
    assert current.close == 3500.0, "实时 tick 被预热覆盖了"
    assert (current.open, current.high, current.low) == (3500.0, 3500.0, 3500.0), \
        "库里那半截被并进了实时这根——来源证明不了时不能合并"
    assert source.latest(["RB0"])["RB0"].price == 3500.0, "当前报价退回了库里的陈旧值"


def test_a_tick_arriving_during_stop_is_still_persisted(tmp_path, monkeypatch):
    """回调跑在 SDK 自己的线程上：先落库再关闭的话，中间到达的 tick 还是会丢。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        feed(eng, "RB0", [3400, 3405])
        source = eng.source
        original_close = source.close
        at = datetime.fromtimestamp(time.time() + 1, SH)

        def close_then_late_tick():
            # 模拟"关闭正在进行时，SDK 线程又推来一条"：close() 之后聚合器仍在内存里
            source._sdk.api.spi.OnRtnDepthMarketData(
                tick(source.dominant_contract("RB0"), 3499.0, volume=999, open_interest=500_000,
                     clock=at.strftime("%H:%M:%S"), day=at.strftime("%Y%m%d")))
            return original_close()

        monkeypatch.setattr(source, "close", close_then_late_tick)
        eng.stop()
        closes = [r["close"] for r in db.md_bars("RB0")]
        assert closes and closes[-1] == 3499.0, f"停机过程中到达的 tick 没落库：{closes}"
    finally:
        db.close()


def test_a_quote_timestamp_is_the_tick_time_not_the_bucket_start(tmp_path):
    """bar_seconds=300 时，10:04:59 的 tick 桶起点是 10:00:00——一过 10:05 就显得超过
    300 秒没更新，而它其实只有几秒钟。持仓品种一旦这样，整轮决策都会被拦下。"""
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:1",
                          broker_id="0001", user_id="8", password="p", bar_seconds=300,
                          discovery_grace_sec=0, flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        src.start()
        at = datetime(2026, 9, 16, 10, 4, 59, tzinfo=SH)
        src._sdk.api.spi.OnRtnDepthMarketData(
            tick(src.dominant_contract("RB0"), 3400.0, volume=10, open_interest=1000,
                 clock=at.strftime("%H:%M:%S"), day=at.strftime("%Y%m%d")))
        quote = src.latest(["RB0"])["RB0"]
        # 时间戳**不能**用 pytest.approx 的默认容差：它是相对的（1e-6），
        # 在 1.8e9 这个量级上等于 ±1790 秒——比这里要区分的 299 秒差还大得多，
        # 断言会在两种实现下都通过。这类量级陷阱只能靠绝对容差躲开。
        assert quote.ts == pytest.approx(at.timestamp(), abs=0.001), (
            f"报价用了桶起点 {datetime.fromtimestamp(quote.ts, SH)}，应当是 tick 的真实时刻")
        bucket = datetime(2026, 9, 16, 10, 0, tzinfo=SH).timestamp()
        assert src.klines("RB0", "5m", 5)[-1].ts == pytest.approx(bucket, abs=0.001), "K 线仍应按桶起点"
        assert quote.ts - bucket == pytest.approx(299.0, abs=0.001), "两者本就该差 299 秒"
    finally:
        src.close()


# ---------- Regression 68：落库时的周期隔离 ----------

def _bars(base: float, step: float, count: int, price: float):
    """一串等间隔的 bar，价格各不相同，便于分辨哪一行被谁改写了。"""
    from arena.models import Bar
    return [Bar(ts=base + step * i, open=price + i, high=price + 1 + i, low=price - 1 + i,
                close=price + 0.5 + i, volume=10 + i, hold=100 + i) for i in range(count)]


def test_a_different_bar_length_does_not_overwrite_the_stored_series(tmp_path):
    """两个周期的 bar 会撞在同一个时间戳上：每根 5m 的起点都正好是某根 1m 的起点。

    主键只有 `(symbol, ts)` 的话，写 5m 会把同一时刻的 1m 行**覆盖并改写成 5m**。
    读回来按 `bar_seconds` 过滤，于是 1m 序列每五根少一根——比"旧历史不可用"更糟，
    因为它看上去是可用的：重采样出来的 OHLCV 与指标都是错的，而且不报任何错。
    """
    from arena.db import Database
    db = Database(str(tmp_path / "md.db"))
    try:
        base = 1_700_000_000.0 - (1_700_000_000.0 % 300)
        minute = _bars(base, 60, 10, 3400.0)
        db.save_md_bars("RB0", minute, bar_seconds=60)
        db.save_md_bars("RB0", _bars(base, 300, 2, 9000.0), bar_seconds=300)   # 撞上第 0 根与第 5 根

        back = db.md_bars("RB0", limit=100, bar_seconds=60)
        assert [r["ts"] for r in back] == [b.ts for b in minute], "5m 的写入把同一时刻的 1m 行改写成了 5m"
        assert [r["close"] for r in back] == [b.close for b in minute], "1m 行的 OHLCV 被 5m 的值覆盖了"
        assert len(db.md_bars("RB0", limit=100, bar_seconds=300)) == 2
    finally:
        db.close()


def test_trimming_one_interval_does_not_evict_another(tmp_path):
    """裁剪也要按周期分开算：按品种一起裁，写几根 5m 就能把 1m 的历史削掉大半。"""
    from arena.db import Database
    db = Database(str(tmp_path / "md.db"))
    try:
        base = 1_700_000_000.0 - (1_700_000_000.0 % 300)
        db.save_md_bars("RB0", _bars(base, 60, 20, 3400.0), keep=20, bar_seconds=60)
        db.save_md_bars("RB0", _bars(base, 300, 3, 9000.0), keep=3, bar_seconds=300)
        assert len(db.md_bars("RB0", limit=100, bar_seconds=60)) == 20, "5m 的裁剪把 1m 的历史一起删了"
        assert len(db.md_bars("RB0", limit=100, bar_seconds=300)) == 3
    finally:
        db.close()


def test_an_existing_database_is_migrated_to_the_per_interval_key(tmp_path):
    """旧库（主键只有 symbol, ts）打开时要就地升级，而不是继续互相覆盖。"""
    import sqlite3

    from arena.db import Database
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE md_bars (
          symbol TEXT NOT NULL, ts REAL NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
          low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
          hold REAL NOT NULL DEFAULT 0, bar_seconds INTEGER NOT NULL DEFAULT 60,
          PRIMARY KEY (symbol, ts)
        );
        INSERT INTO md_bars VALUES ('RB0', 1700000100.0, 1, 2, 0.5, 1.5, 10, 100, 60);
    """)
    old.commit()
    old.close()

    db = Database(path)
    try:
        assert len(db.md_bars("RB0", limit=10, bar_seconds=60)) == 1, "升级时把旧数据丢了"
        db.save_md_bars("RB0", _bars(1700000100.0, 300, 1, 9000.0), bar_seconds=300)
        kept = db.md_bars("RB0", limit=10, bar_seconds=60)
        assert len(kept) == 1 and kept[0]["close"] == 1.5, "旧库没升级，5m 照样覆盖了 1m 那一行"
    finally:
        db.close()


def test_a_database_from_before_the_interval_column_is_migrated_too(tmp_path):
    """更早的一版 `md_bars` 连 `bar_seconds` 这一列都没有。

    `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，所以这种库打开后写 bar 会直接
    "no such column: bar_seconds"，读也拿不到周期。升级要能从这一版接上，并把旧行按
    当时唯一的周期（60 秒）标注。
    """
    import sqlite3

    from arena.db import Database
    path = str(tmp_path / "older.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE md_bars (
          symbol TEXT NOT NULL, ts REAL NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
          low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
          hold REAL NOT NULL DEFAULT 0,
          PRIMARY KEY (symbol, ts)
        );
        INSERT INTO md_bars VALUES ('RB0', 1700000100.0, 1, 2, 0.5, 1.5, 10, 100);
    """)
    old.commit()
    old.close()

    db = Database(path)
    try:
        rows = db.md_bars("RB0", limit=10, bar_seconds=60)
        assert len(rows) == 1 and rows[0]["close"] == 1.5, "旧库里的 bar 没接上"
        db.save_md_bars("RB0", _bars(1700000160.0, 60, 1, 3400.0), bar_seconds=60)   # 写得进去
        assert len(db.md_bars("RB0", limit=10, bar_seconds=60)) == 2
    finally:
        db.close()


class _FailingConn:
    """在某条语句上注入失败的连接代理。

    `executescript` 按分号拆开逐条执行，是为了**如实复刻** sqlite3 的行为：脚本跑在自动提交
    的连接上，每条语句各自提交——所以半路失败时 `DROP TABLE` 已经落定了。不这样复刻的话，
    这条用例在"升级不是一个事务"的旧实现上也会通过，等于什么都没测到。
    """

    def __init__(self, real, fail_on: str):
        self.real = real
        self.fail_on = fail_on

    def execute(self, sql, *args):
        import sqlite3
        if self.fail_on in sql:
            raise sqlite3.OperationalError(f"注入的失败：{self.fail_on}")
        return self.real.execute(sql, *args)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)


def test_an_interrupted_upgrade_leaves_the_old_table_intact(tmp_path):
    """升级必须是一个事务：DROP 落定、RENAME 没跑成，历史就会永久失联。

    下次启动时 `SCHEMA` 会建出一张空的新结构 `md_bars`，升级一看主键已经对了就直接返回，
    搬到一半的数据留在 `md_bars_migrated` 里再也没人读——CTP 的预热历史悄悄归零。
    """
    import sqlite3

    from arena.db import Database
    path = str(tmp_path / "interrupted.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE md_bars (
          symbol TEXT NOT NULL, ts REAL NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
          low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
          hold REAL NOT NULL DEFAULT 0, bar_seconds INTEGER NOT NULL DEFAULT 60,
          PRIMARY KEY (symbol, ts)
        );
        INSERT INTO md_bars VALUES ('RB0', 1700000100.0, 1, 2, 0.5, 1.5, 10, 100, 60);
    """)
    conn.commit()
    try:
        with pytest.raises(sqlite3.OperationalError):
            Database._migrate_md_bars(_FailingConn(conn, "RENAME TO"))
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "md_bars" in tables, "原表被删了，而改名没跑成——历史没了"
        assert "md_bars_migrated" not in tables, "半成品表留了下来，下次启动会被当成已升级"
        assert conn.execute("SELECT COUNT(*) AS c FROM md_bars").fetchone()["c"] == 1
    finally:
        conn.close()

    # 事务回滚之后再正常打开一次，升级仍然做得成、数据还在
    db = Database(path)
    try:
        assert len(db.md_bars("RB0", limit=10, bar_seconds=60)) == 1
    finally:
        db.close()


MIGRATED_SCHEMA = """
    CREATE TABLE {name} (
      symbol TEXT NOT NULL, ts REAL NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
      low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
      hold REAL NOT NULL DEFAULT 0, bar_seconds INTEGER NOT NULL DEFAULT 60,
      PRIMARY KEY (symbol, bar_seconds, ts)
    );
"""


def test_a_stranded_upgrade_table_is_recovered(tmp_path):
    """旧实现（非事务）在删表之后、改名之前中断过的库，要能接回来。

    那种库长这样：`SCHEMA` 建出一张空的新结构 `md_bars`，历史全在 `md_bars_migrated` 里。
    只看主键对不对就返回的话，历史永远没人读——CTP 的预热数据悄悄归零。
    事务化只挡住了以后再出现这种状态，修不了已经出现的那一份。
    """
    import sqlite3

    from arena.db import Database
    path = str(tmp_path / "stranded.db")
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATED_SCHEMA.format(name="md_bars")
                       + MIGRATED_SCHEMA.format(name="md_bars_migrated")
                       + "INSERT INTO md_bars_migrated VALUES ('RB0', 1700000100.0, 1, 2, 0.5, 1.5, 10, 100, 60);")
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        rows = db.md_bars("RB0", limit=10, bar_seconds=60)
        assert len(rows) == 1 and rows[0]["close"] == 1.5, "搬到一半的历史没有被接回来"
        left = db._q("SELECT name FROM sqlite_master WHERE type='table' AND name='md_bars_migrated'")
        assert not left, "半成品表还留着，下次启动又要判一遍"
    finally:
        db.close()


def test_a_stale_copy_beside_the_original_is_discarded(tmp_path):
    """中断发生在删表**之前**时，原表才是准的，半成品拷贝要丢掉而不是并回去。"""
    import sqlite3

    from arena.db import Database
    path = str(tmp_path / "stale.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE md_bars (
          symbol TEXT NOT NULL, ts REAL NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
          low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
          hold REAL NOT NULL DEFAULT 0, bar_seconds INTEGER NOT NULL DEFAULT 60,
          PRIMARY KEY (symbol, ts)
        );
        INSERT INTO md_bars VALUES ('RB0', 1700000100.0, 1, 2, 0.5, 1.5, 10, 100, 60);
        INSERT INTO md_bars VALUES ('RB0', 1700000160.0, 3, 4, 2.5, 3.5, 20, 200, 60);
    """ + MIGRATED_SCHEMA.format(name="md_bars_migrated")
        + "INSERT INTO md_bars_migrated VALUES ('RB0', 1700000100.0, 9, 9, 9, 9.5, 90, 900, 60);")
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        rows = db.md_bars("RB0", limit=10, bar_seconds=60)
        assert [r["close"] for r in rows] == [1.5, 3.5], "原表被半成品拷贝的值盖了"
        left = db._q("SELECT name FROM sqlite_master WHERE type='table' AND name='md_bars_migrated'")
        assert not left, "半成品表还留着"
    finally:
        db.close()


def test_only_the_changed_bars_are_written_on_each_snapshot(tmp_path, monkeypatch):
    """预热历史暖起来之后，每次快照都全量重写整条序列是跑不动的。

    默认 71 个品种 × 3600 根保留 ≈ 每分钟 25 万次 upsert，而且是**同步**跑在引擎循环里，
    盯市与止损/强平检查全被它拖住。实际每次变的通常只有当前那一根。
    """
    eng, db = build_engine(tmp_path, monkeypatch)
    written = []
    real = db.save_md_bars

    def spy(symbol, bars, **kw):
        rows = list(bars)
        written.append((symbol, len(rows)))
        return real(symbol, rows, **kw)

    monkeypatch.setattr(db, "save_md_bars", spy)
    try:
        feed(eng, "RB0", [3400 + i for i in range(10)])
        eng._persist_md_bars()
        first = [n for s, n in written if s == "RB0"]
        assert first == [10], f"第一次应当写全量 10 根，实际 {first}"

        written.clear()
        feed(eng, "RB0", [3999])          # 同一根 bar 内的新 tick：只有最后一根变了
        eng._persist_md_bars()
        again = [n for s, n in written if s == "RB0"]
        assert again and max(again) <= 2, f"只有一根 bar 变了，却写了 {again} 根"

        rows = db.md_bars("RB0", limit=100, bar_seconds=60)
        assert len(rows) == 10, f"增量写入把历史写丢了，只剩 {len(rows)} 根"
        assert rows[-1]["close"] == 3999.0, "最后那根未收口的 bar 没有被更新"
    finally:
        eng.stop()
        db.close()

    # 重启：预热来的那一段本来就是从库里读出来的，第一次落库不该把它原样重写一遍
    eng2, db2 = build_engine(tmp_path, monkeypatch)
    written2 = []
    real2 = db2.save_md_bars

    def spy2(symbol, bars, **kw):
        rows = list(bars)
        written2.append((symbol, len(rows)))
        return real2(symbol, rows, **kw)

    monkeypatch.setattr(db2, "save_md_bars", spy2)
    try:
        eng2._seed_md_bars()
        feed(eng2, "RB0", [4001])
        eng2._persist_md_bars()
        after_restart = [n for s, n in written2 if s == "RB0"]
        assert after_restart and max(after_restart) <= 2, \
            f"重启后第一次落库把预热的历史重写了一遍：{after_restart}"
        assert len(db2.md_bars("RB0", limit=100, bar_seconds=60)) == 10
    finally:
        eng2.stop()
        db2.close()


def test_a_failed_startup_releases_the_api(tmp_path, monkeypatch):
    """登录被拒/连接超时时，`start()` 已经把原生 API 建出来并 `Init()` 过了。

    引擎构造在这里抛出去，`run_desktop()` 还没来得及挂上 `engine.stop()`，于是谁也不会调
    `close()`：SDK 的回调线程和 flow 资源就那么留着，可能把退出挂住，也会干扰同一进程里的重试。
    """
    from arena.db import Database
    from arena.engine import loop as loop_mod

    sdks = []

    def fake_load(self):
        self._sdk = FakeSdk(FailingLoginApi)
        sdks.append(self._sdk)
        return self._sdk

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    cfg = make_ctp_cfg(tmp_path)
    db = Database(cfg.db_path)
    try:
        with pytest.raises(SourceError):
            loop_mod.Engine(cfg, db, accelerate=True)
        assert sdks, "前提不成立：根本没走到建 API 这一步"
        assert sdks[0].api.released is True, "启动失败后没有释放 API：回调线程与 flow 资源都留着"
    finally:
        db.close()


def test_startup_can_be_retried_after_a_failure(tmp_path):
    """重试必须真的重来一次，而不是沿用上一次的连接/登录结果。

    连接与登录的状态是 Event，第一次失败后仍然是置位的：不归零的话第二次 `start()` 不会真去
    等新的应答，而是立刻按上一次的结果再抛一次同样的错——看上去"重试也连不上"，其实压根没重试。

    这里的假 SDK 必须**异步**回调（`Init()` 之后由另一个线程报连接与登录），因为真实的 CTP
    就是这样：同步的假 SDK 在进入 `wait()` 之前就把新结果填好了，陈旧的 Event 根本显不出来，
    用例在没有归零的实现上照样通过——等于什么都没测到。
    """
    import threading

    class AsyncLoginApi(FakeApi):
        attempts = 0
        threads = []

        def Init(self):
            self.inited = True
            timer = threading.Timer(0.05, self.spi.OnFrontConnected)   # 回调来自 SDK 自己的线程
            timer.daemon = True
            AsyncLoginApi.threads.append(timer)
            timer.start()

        def ReqUserLogin(self, field, request_id):
            AsyncLoginApi.attempts += 1
            ok = AsyncLoginApi.attempts > 1
            self.logins.append((field.BrokerID, field.UserID, field.Password))
            self.spi.OnRspUserLogin(None, SimpleNamespace(ErrorID=0 if ok else 3,
                                                          ErrorMsg="" if ok else "CTP:用户口令校验失败"),
                                    request_id, True)
            return 0

    FlakyLoginApi = AsyncLoginApi

    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:1",
                          broker_id="0001", user_id="8", password="p", auto_months=2,
                          discovery_grace_sec=0, connect_timeout_sec=5,
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(FlakyLoginApi))
    try:
        with pytest.raises(SourceError) as first:
            src.start()
        assert "口令" in str(first.value)
        assert src._sdk.api.released is True
        src.start()                     # 第二次：口令这次通过
        assert src.health()["ok"] is True, "重试沿用了上一次的失败结果，等于没重试"
        assert src._sdk.api.subscribe_calls >= 1, "重试成功后没有订阅，等于一条行情都收不到"
    finally:
        src.close()


def test_a_later_construction_failure_leaves_no_live_ctp_api(tmp_path, monkeypatch):
    """登录成功之后，构造的后续步骤仍然可能抛（例如切换竞赛前补做失败的 RolloverBlocked）。

    那时这个 Engine 永远不会交到调用方手里，`run.py` 直接返回，谁也不会调 `source.close()`——
    原生 API 的回调线程与 flow 资源就那么留着。`start()` 自身的清理只管 `start()` 里面的失败。
    """
    from arena.db import Database
    from arena.engine import loop as loop_mod

    sdks = []

    def fake_load(self):
        self._sdk = FakeSdk()
        sdks.append(self._sdk)
        return self._sdk

    def boom(self):
        raise RuntimeError("构造中途失败")

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    monkeypatch.setattr(loop_mod.Engine, "_load_or_create_competition", boom)
    cfg = make_ctp_cfg(tmp_path)
    db = Database(cfg.db_path)
    try:
        with pytest.raises(RuntimeError):
            loop_mod.Engine(cfg, db, accelerate=True)
        live = [s for s in sdks if s.api is not None and not s.api.released]
        assert not live, "构造中途失败后还留着一个活着的 CTP API（回调线程与 flow 资源都在）"
    finally:
        db.close()


def test_a_failed_ctp_startup_does_not_archive_the_previous_competition(tmp_path, monkeypatch):
    """换到 CTP 时启动失败，不能把上一场实验先归档掉。

    `_load_or_create_competition()` 在换市场时会**落库**：归档旧竞赛、新建一条。如果行情源
    在它之后才启动，凭证错或前置不可达就成了"旧实验先被归档、然后启动失败退出"——软件根本
    没跑起来，而那场实验按正常启动路径再也接不回来了。所以源必须在提交这个切换之前先起来。
    """
    from arena.db import Database
    from arena.engine import loop as loop_mod

    def fake_load(self):
        self._sdk = FakeSdk(FailingLoginApi)
        return self._sdk

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    cfg = make_ctp_cfg(tmp_path)
    db = Database(cfg.db_path)
    try:
        old = db.create_competition("上一场", "cn_futures", 1_000_000, "{}")
        with pytest.raises(SourceError):
            loop_mod.Engine(cfg, db, accelerate=True)
        active = db.active_competition()
        assert active is not None and active["id"] == old, "上一场实验被归档了，正常启动接不回去"
        assert active["market"] == "cn_futures", "凭证还没验通就已经切到 CTP 了"
    finally:
        db.close()


class FakeClock:
    """可控的单调时钟：用例自己决定"过了多久"，不必真的 sleep。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_rollover_source(tmp_path, clock, **kw):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:1",
                           broker_id="0001", user_id="8", password="p", auto_months=2,
                           discovery_grace_sec=0, flow_dir=str(tmp_path / "flow"),
                           sdk=FakeSdk(), clock=clock, **kw)


def test_a_dominant_that_stops_reporting_is_dropped_from_the_selection(tmp_path):
    """主力到期/停牌之后不再报价，它最后那个持仓量会永远留在表里。

    不把它剔出候选的话，后继合约的 tick 会一直被当成"非主力"丢掉——该品种的报价与 K 线
    就此冻结，而整体健康状态一切正常（tick 一直在来），日志里一个字都没有。
    """
    clock = FakeClock()
    src = make_rollover_source(tmp_path, clock, dominant_stale_sec=60)
    try:
        src.start()
        spi = src._sdk.api.spi
        near, far = src._candidates["RB0"][0], src._candidates["RB0"][1]
        spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=900_000))
        clock.advance(1)
        spi.OnRtnDepthMarketData(tick(far, 3500.0, volume=10, open_interest=100_000))
        assert src.dominant_contract("RB0") == near, "前提不成立：持仓量大的应当是主力"

        # 近月到期不再报价；远月继续报，交易时长不断累积
        # （单次间隔按 IDLE_GAP_CAP_SEC 封顶 5 秒，所以要走够 60 秒得有十几条 tick）
        for i in range(20):
            clock.advance(5)
            spi.OnRtnDepthMarketData(tick(far, 3500.0 + i, volume=20 + i, open_interest=100_000,
                                          clock=f"09:{10 + i:02d}:00"))
        assert src.dominant_contract("RB0") == far, "死掉的合约还占着主力，后继的行情全被丢掉"
        assert src.latest(["RB0"])["RB0"].price == 3519.0, "报价冻结在死合约上"
    finally:
        src.close()


def test_a_market_break_does_not_look_like_an_expired_contract(tmp_path):
    """午休两小时之后的第一条 tick，不能让当前主力显得"已经安静了两小时"。

    按墙上时间算失效的话，每个交易时段开头都会变成"谁先回调谁当主力"——正是观察期要挡的
    那种假换月，只不过换到了每天好几次。所以失效按**观察到的交易时长**算，长间隔要封顶。
    """
    clock = FakeClock()
    src = make_rollover_source(tmp_path, clock, dominant_stale_sec=60)
    try:
        src.start()
        spi = src._sdk.api.spi
        near, far = src._candidates["RB0"][0], src._candidates["RB0"][1]
        spi.OnRtnDepthMarketData(tick(near, 3400.0, volume=10, open_interest=900_000))
        clock.advance(1)
        spi.OnRtnDepthMarketData(tick(far, 3500.0, volume=10, open_interest=100_000))
        assert src.dominant_contract("RB0") == near

        clock.advance(2 * 3600)          # 午休：两小时里谁都没报价
        spi.OnRtnDepthMarketData(tick(far, 3505.0, volume=11, open_interest=100_000,
                                      clock="13:30:00"))   # 远月先回调
        assert src.dominant_contract("RB0") == near, "休市的间隔把主力算成了失效，开盘就假换月"
    finally:
        src.close()


def test_seeding_keeps_the_volume_baseline_of_ticks_already_received(tmp_path):
    """预热发生在登录订阅之后，所以预热之前可能已经收到 tick 了。

    那时的累计成交量基准是**本进程自己攒的**，与库里的历史无关。跟着历史一起清掉的话，
    预热后的第一条 tick 会被当成"首个 tick"重新起算，它那一段增量凭空消失
    （100 → 110 → 预热 → 120，本该 +20 却只记了 +10），vol_mean20 也跟着失真。
    """
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.add(base + 1, 3400.0, 100, 1000, source="rb2601")
    agg.add(base + 2, 3405.0, 110, 1000, source="rb2601")      # +10
    assert agg.snapshot()[-1].volume == 10.0

    agg.seed([Bar(ts=base - 60, open=3390.0, high=3395.0, low=3385.0, close=3392.0,
                   volume=55.0, hold=900.0)])                   # 预热更早的历史
    agg.add(base + 3, 3410.0, 120, 1000, source="rb2601")      # 相对 110 应当再 +10

    bars = agg.snapshot()
    assert [b.ts for b in bars] == [base - 60, base], "预热的历史没接上"
    assert bars[-1].volume == 20.0, f"预热把本进程的成交量基准清掉了，这一根只记了 {bars[-1].volume}"


def test_a_cold_seed_still_drops_the_stale_volume_baseline(tmp_path):
    """反过来，冷启动时库里的历史与本次的累计计数器不可比，基准必须清掉。"""
    agg = TickAggregator(bar_seconds=60)
    base = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    agg.seed([Bar(ts=base, open=3390.0, high=3395.0, low=3385.0, close=3392.0,
                   volume=55.0, hold=900.0)])
    agg.add(base + 5, 3400.0, 1_000_000, 1000, source="rb2601")   # 新一天的累计值
    bar = agg.snapshot()[-1]
    assert bar.volume == 0.0, "把上一次运行的累计值当成了基准，凭空多出百万成交量"
    assert bar.open == 3400.0, "预热那根来源不可证，带来源的第一笔 tick 到达时应当整根作废"


def test_the_documented_cold_start_matches_the_contract_table():
    """文档里那个"1h 每交易日 5~11 根、凑满 60 根要 5.5~12 个交易日"必须是算出来的。

    同一句话被纠正过三轮：先按"跑一小时就齐"写，再按交易小时数估，最后才发现该数的是
    `resample()` 真正的分桶——它按 `ts - ts % 3600` 分，横跨开收盘的那个桶只要有成交就算
    一根（IF 的 09:30–11:30 + 13:00–15:00 产出 09/10/11/13/14 五根，而不是 4 根）。
    所以这里不再靠人算：用真实的 `resample()` 数一遍合约表里每个品种，再和文档里的数字比对。
    """
    import json

    from arena.market.indicators import resample
    table = json.loads((ROOT / "config" / "contracts_cn.json").read_text(encoding="utf-8"))

    def minutes(text: str) -> int:
        hh, mm = text.split(":")
        return int(hh) * 60 + int(mm)

    day = datetime(2026, 9, 16, tzinfo=SH).timestamp()
    per_day = {}
    for code, spec in table["contracts"].items():
        bars = []
        for start, end in spec["sessions"]:
            first, last = minutes(start), minutes(end)
            if last <= first:
                last += 24 * 60
            for m in range(first, last):        # 左闭右开：收盘那一分钟不产生新桶
                at = day + m * 60
                bars.append(Bar(ts=at, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0, hold=1.0))
        per_day[code] = len(resample(sorted(bars, key=lambda b: b.ts), 3600))

    low, high = min(per_day.values()), max(per_day.values())
    assert (low, high) == (5, 11), f"合约表的时段变了：现在每交易日 {low}~{high} 根 1h"
    assert per_day["IF0"] == 5 and per_day["AU0"] == 11 and per_day["RB0"] == 7

    doc = (ROOT / "docs" / "ctp.md").read_text(encoding="utf-8")
    assert "5～11" in doc, "文档里的每日桶数与合约表对不上了"
    # 60 根 ÷ 每日桶数：11 根/日 → 5.5 日，5 根/日 → 12 日
    assert "5.5～12" in doc, "文档里的冷启动交易日数与合约表对不上了"


def test_seeded_history_alone_does_not_become_a_quote(tmp_path):
    """预热只给指标用，不给报价用。

    盘中重启时库里的尾巴可能只有几分钟旧，拿它当当前报价的话，引擎会立刻盯市、触发止损/强平，
    还会开一轮要花钱的决策——而此时连哪个合约是主力都还没定下来（观察期），涨跌幅更是拿新选出
    的合约的昨结算去比旧合约的收盘价。
    """
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:1",
                          broker_id="0001", user_id="8", password="p", auto_months=2,
                          discovery_grace_sec=0, flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        src.start()
        now = time.time()
        recent = now - (now % 60) - 60          # 一分钟前的那根：盘中重启时它很"新鲜"
        src.seed_bars("RB0", [Bar(ts=recent, open=3400.0, high=3402.0, low=3398.0,
                                  close=3401.0, volume=12.0, hold=500_000.0)])

        assert src.latest(["RB0"]) == {}, "只凭预热的历史就发出了当前报价"
        assert src.klines("RB0", "1m", 10), "预热的历史仍然要能给指标用"

        instrument = src.dominant_contract("RB0")
        at = datetime.fromtimestamp(now, SH)
        src._sdk.api.spi.OnRtnDepthMarketData(tick(instrument, 3410.0, volume=5, open_interest=500_000,
                                                   clock=at.strftime("%H:%M:%S"), day=at.strftime("%Y%m%d")))
        quote = src.latest(["RB0"])["RB0"]
        assert quote.price == 3410.0, "来了实时 tick 之后报价要跟上"
        assert abs(quote.ts - now) < 5
    finally:
        src.close()


def test_ctp_is_treated_as_a_domestic_market_for_pre_open_research(tmp_path, monkeypatch):
    """CTP 也是境内期货：有交易所日历、报价与 K 线，盘前研究的闸门不能只认 cn_futures。

    这两条闸门是在 CTP 行情源存在之前写的，合并之后才暴露出来：真实 CTP 运行下
    `preopen_debate.enabled` 形同虚设，配置好的盘前窗口一次都不会调度。
    """
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        assert eng.cfg.competition.market == "ctp"
        assert eng._preopen_debate_state()["automatic_supported"] is True, \
            "CTP 运行下自动盘前研究被报成不支持"
    finally:
        eng.stop()
        db.close()


def test_a_stale_ctp_feed_does_not_burn_the_preopen_reservation(tmp_path, monkeypatch):
    """行情还陈旧时不能占掉这个开盘时刻的永久预约。

    一次尝试（成功/不可用/不完整）就是该开盘时刻的唯一记录，之后不再重试；而 CTP 的 last_ok
    是最后一条推送 tick 的到达时刻，收盘之后必然超过 max_quote_age_sec。在 30 分钟前置窗口
    一开始就尝试，等于把"不可用"钉死，集合竞价的 tick 真到了也不会再跑。
    """
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        started = []
        monkeypatch.setattr(eng, "_start_debate",
                            lambda *a, **k: started.append(a) or True)
        stale = time.time() - eng.cfg.preopen_debate.max_quote_age_sec - 60
        monkeypatch.setattr(eng.source, "health", lambda: {"ok": True, "last_ok": stale})

        # 距开盘还早：陈旧就先不占坑，留给集合竞价的 tick
        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 900))
        assert eng._maybe_start_preopen_debate() is False
        assert started == [], "行情陈旧时就把这个开盘时刻的预约占掉了"

        # 窗口只剩最后一分钟：仍然尝试一次，让"始终没等到行情"留下审计记录
        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 30))
        assert eng._maybe_start_preopen_debate() is True
        assert started, "窗口末尾也没尝试，整个开盘时刻会悄无声息地跳过"
    finally:
        eng.stop()
        db.close()


def test_a_fresh_ctp_feed_schedules_immediately(tmp_path, monkeypatch):
    """行情新鲜时照常在窗口一开始就调度——新增的守卫不能把正常路径一起挡了。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        started = []
        monkeypatch.setattr(eng, "_start_debate",
                            lambda *a, **k: started.append(a) or True)
        monkeypatch.setattr(eng.source, "health", lambda: {"ok": True, "last_ok": time.time()})
        from arena.models import Quote
        eng.quotes = {s: Quote(symbol=s, price=3400.0, ts=time.time() - 1.0, open_interest=1.0) for s in ("RB0", "I0")}
        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 900))
        assert eng._maybe_start_preopen_debate() is True
        assert started
    finally:
        eng.stop()
        db.close()
