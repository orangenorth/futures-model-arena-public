"""Regression 70：

- Jev 席位没配 `model` 时实际请求的是 `DEFAULT_MODEL`，阵容签名与 traders 行必须记这个生效值，
  而不是空字符串：否则改了默认模型竞赛照样续跑，显式写上同一个默认值反而另起一场。
- 收盘校验比的是最后一根 bar 的桶起点，不能再加 `bar_seconds`：5m bar 下 14:50 那根会被当成
  15:00 的收盘，提前量从五分钟放宽到十分钟。
"""
from __future__ import annotations

from datetime import date, datetime, time as clock, timedelta

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_cn_contracts
from arena.market.ctp import CLOSED_SESSION_QUOTE_GRACE_SEC, CtpMarketSource
from arena.models import Bar
from arena.traders.jev import DEFAULT_MODEL

from .test_ctp import ROOT, SH, FakeSdk, tick
from .test_jev_trader import _app_config


def _jev_signature(**kwargs):
    kwargs.setdefault("base_url", "https://api.typesafe.ai")
    cfg = AppConfig(traders=[TraderCfg(id="jev", name="Jev", provider="jev", **kwargs)])
    return Engine._configured_roster_signature(cfg)


def test_a_jev_seat_without_a_model_is_signed_with_the_model_it_actually_requests():
    assert _jev_signature(model="") == _jev_signature(model=DEFAULT_MODEL)
    assert _jev_signature(model="") != _jev_signature(model="jev-next")


def test_a_jev_seat_without_a_model_records_and_resumes_on_the_effective_model(tmp_path):
    blank = TraderCfg(id="jev", name="Jev", provider="jev", base_url="https://api.typesafe.ai", model="")
    explicit = TraderCfg(id="jev", name="Jev", provider="jev", base_url="https://api.typesafe.ai",
                         model=DEFAULT_MODEL)
    db = Database(_app_config(tmp_path, blank).db_path)
    first = Engine(_app_config(tmp_path, blank), db, accelerate=True)
    comp = first.comp_id
    first.stop()
    try:
        assert db.traders(comp)[0]["model"] == DEFAULT_MODEL
        again = Engine(_app_config(tmp_path, explicit), db, accelerate=True)
        try:
            assert again.comp_id == comp, "显式写上已经生效的默认模型不该另起一场竞赛"
        finally:
            again.stop()
    finally:
        db.close()


def _five_minute_source(tmp_path):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return CtpMarketSource(["IF0"], {"IF0": specs["IF0"]}, holidays, md_front="tcp://127.0.0.1:10131",
                           broker_id="0001", user_id="8", password="p", discovery_grace_sec=0,
                           flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(), bar_seconds=300)


def _five_minute_bars(day: date, last: clock):
    bars, i = [], 0
    for start, end in ((clock(9, 30), clock(11, 30)), (clock(13, 0), last)):
        at = datetime.combine(day, start, tzinfo=SH)
        stop = datetime.combine(day, end, tzinfo=SH)
        while at <= stop:
            bars.append(Bar(ts=at.timestamp(), open=4200.0, high=4201.0, low=4199.0, close=4200.0 + i,
                            volume=10, hold=1000 + i))
            at += timedelta(minutes=5)
            i += 1
    return bars


def test_a_five_minute_bar_that_started_ten_minutes_before_the_bell_is_not_a_prior_close(tmp_path):
    day = date(2026, 9, 22)
    close_ts = datetime.combine(day, clock(15, 0), tzinfo=SH).timestamp()
    now = datetime.combine(day, clock(20, 59, 40), tzinfo=SH).timestamp()

    src = _five_minute_source(tmp_path / "early")
    try:
        bars = _five_minute_bars(day, clock(14, 50))
        assert bars[-1].ts + 300 == close_ts - CLOSED_SESSION_QUOTE_GRACE_SEC, "前提：正是该边界"
        src.seed_bars("IF0", bars)
        assert src.closed_session_quotes(["IF0"], now=now) == {}
    finally:
        src.close()

    src = _five_minute_source(tmp_path / "within")
    try:
        bars = _five_minute_bars(day, clock(14, 55))
        src.seed_bars("IF0", bars)
        assert src.closed_session_quotes(["IF0"], now=now)["IF0"].ts == bars[-1].ts
    finally:
        src.close()


def _engine_view(source):
    from types import SimpleNamespace
    return SimpleNamespace(source=source, tz=SH, _exchange_tz=SH)


def test_the_afternoon_phase_is_found_while_index_futures_open_first_or_are_already_trading(tmp_path):
    """`symbols: all` 把股指（13:00 午盘）和商品（13:30 午盘）放进同一张品种表：取最早开市的话，
    13:00 前看到 13:00、之后看到股指的下一分钟，13:30 的午盘研究只在 13:29 露面一分钟。"""
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["IF0", "RB0"], {s: specs[s] for s in ("IF0", "RB0")}, holidays,
                          md_front="tcp://127.0.0.1:10131", broker_id="0001", user_id="8", password="p",
                          discovery_grace_sec=0, flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        day = date(2026, 9, 22)
        afternoon = datetime.combine(day, clock(13, 30), tzinfo=SH)
        for at in (clock(12, 50), clock(13, 10)):        # 股指开盘前 / 股指已在交易
            now = datetime.combine(day, at, tzinfo=SH)
            assert src.next_open(now) < afternoon, "前提：最早开市的不是 13:30"
            target, phase = Engine._next_research_open(_engine_view(src), now)
            assert target == afternoon and phase[0] == "afternoon_preopen", (at, target)
    finally:
        src.close()


def test_a_dce_night_tick_stamped_with_the_trading_day_is_kept(tmp_path):
    """大商所夜盘的 ActionDay 是交易日（周五夜盘写成下周一）：按它构造的时刻超前好几天，未来时刻闸门会
    把整个夜盘丢光。大商所且确实超前时改用接收日期；其它交易所写到未来的 tick 照旧丢弃。"""
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["I0", "RB0"], {s: specs[s] for s in ("I0", "RB0")}, holidays,
                          md_front="tcp://127.0.0.1:10131", broker_id="0001", user_id="8", password="p",
                          discovery_grace_sec=0, flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        src.start()
        spi = src._sdk.api.spi
        now = datetime.now(SH)
        trading_day = (now + timedelta(days=3)).strftime("%Y%m%d")
        for symbol, price in (("I0", 800.0), ("RB0", 3400.0)):
            spi.OnRtnDepthMarketData(tick(src.dominant_contract(symbol), price, volume=1, open_interest=100,
                                          clock=now.strftime("%H:%M:%S"), day=trading_day))
        quotes = src.latest(["I0", "RB0"])
        assert quotes["I0"].price == 800.0 and abs(quotes["I0"].ts - now.timestamp()) < 1.0
        assert "RB0" not in quotes, "非大商所写到未来的 tick 仍然必须丢弃"
    finally:
        src.close()


def test_a_numeric_provider_error_code_does_not_crash_the_classifier():
    """兼容服务的 400 可能带数字 code（1001）：`.strip()` 会在处理原始错误时再抛 AttributeError，
    看板拿到的是内部异常而不是能照做的 400 提示。"""
    import httpx
    import openai

    from arena.config import LlmCfg
    from arena.traders.llm import _rejected_token_param, classify_error

    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    e = openai.BadRequestError("Error code: 400 - max_tokens is not supported",
                               response=httpx.Response(400, request=request),
                               body={"message": "max_tokens is not supported", "code": 1001, "param": 7})
    assert _rejected_token_param(e, str(e)) == "max_tokens"
    hint, retryable = classify_error(e, TraderCfg(id="x", name="x", model="m", api_key="k"), LlmCfg())
    assert hint and not retryable


def _login_source(tmp_path, api_class, monkeypatch):
    from arena.market import ctp as ctp_mod
    monkeypatch.setattr(ctp_mod, "LOGIN_RETRY_BASE_SEC", 0.01)
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:10131",
                           broker_id="0001", user_id="8", password="p", discovery_grace_sec=0,
                           flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(api_class), connect_timeout_sec=2)


def test_a_login_request_rejected_synchronously_is_retried(tmp_path, monkeypatch):
    from .test_ctp import FakeApi

    class BusyOnceApi(FakeApi):
        def ReqUserLogin(self, field, request_id):
            if not getattr(self, "busy_seen", False):
                self.busy_seen = True
                return -3                     # 每秒请求数超限：不会有 OnRspUserLogin
            return super().ReqUserLogin(field, request_id)

    src = _login_source(tmp_path, BusyOnceApi, monkeypatch)
    try:
        src.start()
        assert src._logged_in.is_set() and len(src._sdk.api.logins) == 1
    finally:
        src.close()


def test_a_login_request_that_keeps_being_rejected_fails_with_that_reason(tmp_path, monkeypatch):
    import pytest

    from arena.market.base import SourceError
    from .test_ctp import FakeApi

    class AlwaysBusyApi(FakeApi):
        def ReqUserLogin(self, field, request_id):
            self.logins.append(request_id)
            return -2

    src = _login_source(tmp_path, AlwaysBusyApi, monkeypatch)
    try:
        with pytest.raises(SourceError, match="返回码 -2"):
            src.start()
        assert len(src._sdk.api.logins) == 3
    finally:
        src.close()


def test_a_contract_rejected_as_unlisted_is_resubscribed_when_the_month_advances(tmp_path, monkeypatch):
    """尚未挂牌的远月第一次订阅会被永久拒绝（16 找不到合约）。它留在 `_owner` 里，跨月补订时被当成
    "已订过"跳过——连接不断的话这个合约永远不会再订，等手上的合约到期，品种就没了报价。"""
    from arena.market import ctp as ctp_mod
    from .test_ctp import FakeApi

    class UnlistedApi(FakeApi):
        unlisted = {"IF2604"}

        def SubscribeMarketData(self, ids, count):
            self.subscribe_calls += 1
            self.subscribed.append(list(ids))
            self.respond_subscriptions(list(ids), error_for={cid: 16 for cid in ids if cid in self.unlisted})
            return 0

    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["IF0"], {"IF0": specs["IF0"]}, holidays, md_front="tcp://127.0.0.1:10131",
                          broker_id="0001", user_id="8", password="p", discovery_grace_sec=0,
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(UnlistedApi), auto_months=1)
    try:
        src.start()
        assert src._candidates["IF0"] == ["IF2603", "IF2604"] and "IF2604" not in src._pending_subscriptions
        UnlistedApi.unlisted = set()                         # 4 月它挂牌了

        monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 4, 1), raising=False)
        src.latest(["IF0"])
        assert "IF2604" in src._sdk.api.subscribed[-1], src._sdk.api.subscribed
        assert not src._pending_subscriptions, "重订之后应当被前置确认"
    finally:
        src.close()
