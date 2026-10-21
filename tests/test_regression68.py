"""Regression 68：

- 自动盘前准入把无效/未来时间戳的报价当成陈旧：`_preopen_unready_symbols`（原 `_preopen_stale_symbols`）
  原来直接跳过它们，
  而自动会话的准入不再过 `_manual_quote_only_reason`，一条畸形的 CTP 报价照样预约开盘、花四次模型调用。
- `instruments` 钉的合约必须属于该品种、且不能两个品种钉同一个合约；否则黄金的 tick 会以 RB0 的名义
  发布落库，而估值仍按螺纹钢算。
- 候选合约按交易所时区的日期枚举：主机时区落后于上海时，月初会只订到上个月。
"""
from __future__ import annotations

import math
import time
from dataclasses import replace
from datetime import date, datetime

import pytest

from arena.exchange.contracts import load_cn_contracts
from arena.market import ctp as ctp_mod
from arena.market.ctp import CtpMarketSource, candidate_contracts, pinned_instrument_problem
from arena.models import Bar, Quote

from .test_ctp import ROOT, SH, FakeApi, FakeSdk, build_engine, tick


def _quote(ts):
    return Quote(symbol="RB0", price=3400.0, ts=ts, open_interest=1.0)


def test_invalid_and_future_quote_timestamps_fail_preopen_admission(tmp_path, monkeypatch):
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        now = time.time()
        fresh_i0 = Quote(symbol="I0", price=800.0, ts=now - 1.0, open_interest=1.0)

        def unready(rb_quote):
            return eng._preopen_unready_symbols({"RB0": rb_quote, "I0": fresh_i0}, None, now=now)

        assert unready(_quote(now - 1)) == []
        assert unready(_quote(now - 100_000)) == ["RB0"]
        assert unready(_quote(math.nan)) == ["RB0"]
        assert unready(_quote("not-a-time")) == ["RB0"]
        assert unready(_quote(True)) == ["RB0"]
        too_far = now + eng.max_future_quote_skew_sec + 1
        assert unready(_quote(too_far)) == ["RB0"]
        assert unready(_quote(now + eng.max_future_quote_skew_sec - 1)) == []
    finally:
        eng.stop()
        db.close()


def _source(tmp_path, symbols, **kw):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return CtpMarketSource(symbols, {s: specs[s] for s in symbols}, holidays,
                           md_front="tcp://127.0.0.1:10131", broker_id="0001", user_id="8", password="p",
                           discovery_grace_sec=0, flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(), **kw)


def test_a_pin_that_belongs_to_another_variety_is_refused(tmp_path):
    with pytest.raises(ValueError, match=r"RB0.*au2612.*AU.*RB"):
        _source(tmp_path, ["RB0"], instruments={"RB0": "au2612"})
    with pytest.raises(ValueError, match=r"不是合约代码"):
        _source(tmp_path, ["RB0"], instruments={"RB0": "rebar-main"})
    with pytest.raises(ValueError, match=r"CZCE.*1 位"):
        _source(tmp_path, ["AP0"], instruments={"AP0": "AP2601"})
    src = _source(tmp_path, ["RB0", "AP0"], instruments={"rb0": "rb2605", "AP0": "AP601"})
    try:
        assert src._candidates["RB0"] == ["rb2605"] and src._candidates["AP0"] == ["AP601"]
    finally:
        src.close()


def test_two_symbols_cannot_pin_the_same_contract(tmp_path):
    """两个品种钉同一个合约：品种校验先拦下跨品种的那个；同品种的重复由第二道检查兜底。"""
    with pytest.raises(ValueError, match=r"HC0.*rb2601"):
        _source(tmp_path, ["RB0", "HC0"], instruments={"RB0": "rb2601", "HC0": "rb2601"})
    assert pinned_instrument_problem("HC0", "rb2601") == "属于品种 RB，不是 HC"
    assert pinned_instrument_problem("RB0", "rb2601", "SHFE") is None
    assert pinned_instrument_problem("AP0", "AP601", "CZCE") is None
    assert pinned_instrument_problem("IF0", "IF2612", "CFFEX") is None


def test_candidate_months_follow_the_exchange_date_not_the_host_date(tmp_path, monkeypatch):
    """主机时区落后于上海（UTC 主机在 2026-09-30 16:30，上海已是 10 月 1 日 00:30）。"""

    class HostDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 30)

    monkeypatch.setattr(ctp_mod, "date", HostDate)
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 10, 1), raising=False)

    assert candidate_contracts("RB0", "SHFE", 1) == ["rb2610", "rb2611"]
    assert candidate_contracts("AP0", "CZCE", 2) == ["AP610", "AP611"]
    src = _source(tmp_path, ["RB0"], auto_months=1)
    try:
        assert src._candidates["RB0"] == ["rb2610", "rb2611"]
    finally:
        src.close()


# ---------- 第 22 轮 ----------

def test_a_symbol_that_has_not_ticked_yet_does_not_burn_the_reservation(tmp_path, monkeypatch):
    """集合竞价里 RB0 先报价把源级健康刷新了，I0 一条 tick 都没来：这时预约会被永久占掉，
    工作线程按覆盖度记成 unavailable，后到的 tick 再也触发不了重试。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        started = []
        monkeypatch.setattr(eng, "_start_debate", lambda *a, **k: started.append(a) or True)
        monkeypatch.setattr(eng.source, "health", lambda: {"ok": True, "last_ok": time.time()})
        monkeypatch.setattr(eng.source, "is_open", lambda at, symbol=None: True)
        eng.quotes = {"RB0": _quote(time.time() - 1.0)}
        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 900))

        assert eng._maybe_start_preopen_debate() is False
        assert started == [], "只有 RB0 报过价就把这个开盘时刻的预约占掉了"

        eng.quotes["I0"] = Quote(symbol="I0", price=800.0, ts=time.time() - 1.0, open_interest=1.0)
        assert eng._maybe_start_preopen_debate() is True and started

        started.clear()
        eng.quotes = {"RB0": _quote(time.time() - 1.0)}
        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 30))
        assert eng._maybe_start_preopen_debate() is True and started, "底线那一次仍要留下审计记录"
    finally:
        eng.stop()
        db.close()


def test_pins_are_subscribed_in_their_canonical_form(tmp_path):
    from arena.market.ctp import canonical_contract_id, normalize_instrument_overrides

    assert normalize_instrument_overrides({"rb0": " rb2601 "}) == {"RB0": "rb2601"}
    assert canonical_contract_id(" RB2601 ", "SHFE") == "rb2601"
    assert canonical_contract_id("ap601", "CZCE") == "AP601"
    assert canonical_contract_id("if2612", "CFFEX") == "IF2612"
    src = _source(tmp_path, ["RB0", "AP0"], instruments={"RB0": " RB2601 ", "AP0": "ap601 "})
    try:
        assert src._candidates == {"RB0": ["rb2601"], "AP0": ["AP601"]}
        assert src._owner == {"rb2601": "RB0", "AP601": "AP0"}
        src.start()
        assert sorted(src._sdk.api.subscribed[0]) == ["AP601", "rb2601"]
    finally:
        src.close()


# ---------- 第 23 轮 ----------

def test_pin_spelling_does_not_change_the_feed_identity(tmp_path, monkeypatch):
    """源订阅的是规范化之后的 rb2601：把配置从 RB2601 改写成 rb2601 不能掀掉竞赛、换掉 bar 命名空间。"""
    from arena.engine.loop import market_feed_namespace, market_feed_signature
    from .test_ctp import make_ctp_cfg
    from .test_regression67 import restart

    specs, _holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    upper = make_ctp_cfg(tmp_path, instruments={"RB0": " RB2601 "})
    lower = make_ctp_cfg(tmp_path, instruments={"RB0": "rb2601"})
    assert market_feed_signature(upper.market_data, "ctp", upper.symbols, specs)["instruments"] == {"RB0": "rb2601"}
    assert (market_feed_signature(upper.market_data, "ctp", upper.symbols, specs)
            == market_feed_signature(lower.market_data, "ctp", lower.symbols, specs))
    assert (market_feed_namespace(upper.market_data, "ctp", "RB0", specs)
            == market_feed_namespace(lower.market_data, "ctp", "RB0", specs))

    eng, db = restart(tmp_path, monkeypatch, instruments={"RB0": "RB2601"})
    first, namespace = eng.comp_id, eng._md_feed("RB0")
    eng.stop()
    db.close()
    eng2, db2 = restart(tmp_path, monkeypatch, instruments={"RB0": "rb2601"})
    try:
        assert eng2.comp_id == first, "只是改了大小写就另起了一场竞赛"
        assert eng2._md_feed("RB0") == namespace, "只是改了大小写就换掉了落库 bar 的命名空间"
    finally:
        eng2.stop()
        db2.close()


# ---------- 第 24 轮 ----------

def _table_with_exchange(tmp_path, symbol, exchange):
    import json
    raw = json.loads((ROOT / "config" / "contracts_cn.json").read_text(encoding="utf-8"))
    raw["contracts"][symbol]["exchange"] = exchange
    path = tmp_path / f"contracts-{symbol}-{exchange}.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _restart_with_table(tmp_path, monkeypatch, table, **ctp):
    from arena.db import Database
    from arena.engine import loop as loop_mod
    from .test_ctp import make_ctp_cfg

    def fake_load(self):
        self._sdk = FakeSdk()
        return self._sdk

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    cfg = make_ctp_cfg(tmp_path, **ctp)
    cfg.market_data.cn_futures.contracts_file = str(table)
    db = Database(cfg.db_path)
    return loop_mod.Engine(cfg, db, accelerate=True), db


def test_the_symbol_exchange_is_part_of_the_feed_identity(tmp_path, monkeypatch):
    """CTP 靠合约表里的交易所枚举候选合约、定年份位数和大小写：改正交易所就是另一条行情。"""
    from arena.engine.loop import market_feed_namespace, market_feed_signature
    from .test_ctp import make_ctp_cfg

    specs, _holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    cfg = make_ctp_cfg(tmp_path)
    assert market_feed_signature(cfg.market_data, "ctp", cfg.symbols, specs)["exchanges"] == {"RB0": "SHFE", "I0": "DCE"}
    moved = dict(specs)
    moved["RB0"] = replace(specs["RB0"], exchange="DCE")
    assert (market_feed_signature(cfg.market_data, "ctp", cfg.symbols, moved)
            != market_feed_signature(cfg.market_data, "ctp", cfg.symbols, specs))
    assert (market_feed_namespace(cfg.market_data, "ctp", "RB0", moved)
            != market_feed_namespace(cfg.market_data, "ctp", "RB0", specs))
    assert (market_feed_namespace(cfg.market_data, "ctp", "I0", moved)
            == market_feed_namespace(cfg.market_data, "ctp", "I0", specs)), "别的品种不受影响"

    eng, db = _restart_with_table(tmp_path, monkeypatch, ROOT / "config" / "contracts_cn.json")
    first, namespace = eng.comp_id, eng._md_feed("RB0")
    eng.stop()
    db.close()
    eng2, db2 = _restart_with_table(tmp_path, monkeypatch, _table_with_exchange(tmp_path, "RB0", "DCE"))
    try:
        assert eng2.comp_id != first, "合约表里改正了 RB0 的交易所，竞赛却照旧沿用"
        assert eng2._md_feed("RB0") != namespace, "改正交易所后仍用同一个 bar 命名空间预热"
    finally:
        eng2.stop()
        db2.close()


def test_bars_stored_under_the_previous_signature_scheme_are_claimed_on_upgrade(tmp_path, monkeypatch):
    """升级前按旧写法（钉合约原始拼写、不带交易所）落库的 bar，升级后要能预热同一套接入。"""
    from arena.db import Database
    from arena.engine.loop import legacy_market_feed_namespaces, market_feed_namespace
    from arena.models import Bar
    from .test_ctp import make_ctp_cfg
    from .test_regression67 import restart, seeded_bars

    cfg = make_ctp_cfg(tmp_path, instruments={"RB0": "RB2601"})
    legacy = market_feed_namespace(cfg.market_data, "ctp", "RB0")      # 不给 specs = 旧写法
    db = Database(cfg.db_path)
    try:
        base = 1_700_000_000.0
        db.save_md_bars("RB0", [Bar(ts=base + 60 * i, open=3400.0, high=3410.0, low=3390.0, close=3400.0 + i,
                                    volume=10.0, hold=500.0) for i in range(5)], bar_seconds=60, feed=legacy)
    finally:
        db.close()

    eng, db = restart(tmp_path, monkeypatch, instruments={"RB0": "RB2601"})
    try:
        current = eng._md_feed("RB0")
        assert current != legacy
        assert legacy in legacy_market_feed_namespaces(cfg.market_data, "ctp", "RB0", eng._all_specs)
        eng._seed_md_bars()
        assert len(seeded_bars(eng.source, "RB0")) == 5, "升级后旧写法下的历史没有被认领，指标冷启动"
        assert len(db.md_bars("RB0", 100, bar_seconds=60, feed=current)) == 5
        assert len(db.md_bars("RB0", 100, bar_seconds=60, feed=legacy)) == 5, "认领是复制，不删旧行"
    finally:
        eng.stop()
        db.close()


# ---------- 第 25 轮 ----------

def test_every_legacy_namespace_is_merged_with_the_newer_history_winning(tmp_path, monkeypatch):
    """跨过两个旧版本的装机：老的一段在原始拼写的命名空间，新的一段在规范化的命名空间。
    只认领第一个非空的（老的那段）会让指标从缺口之前接着算。"""
    from arena.db import Database
    from arena.engine.loop import legacy_market_feed_namespaces, market_feed_namespace
    from arena.models import Bar
    from .test_ctp import make_ctp_cfg
    from .test_regression67 import restart, seeded_bars

    cfg = make_ctp_cfg(tmp_path, instruments={"RB0": "RB2601"})
    raw_scheme = market_feed_namespace(cfg.market_data, "ctp", "RB0")               # 最老：原始拼写
    specs, _holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    legacy = legacy_market_feed_namespaces(cfg.market_data, "ctp", "RB0", specs)
    assert legacy[-1] == raw_scheme and len(legacy) == 2, legacy
    canonical_scheme = legacy[0]                                                   # 较新：规范化、不带交易所

    def bar(i, close):
        return Bar(ts=1_700_000_000.0 + 60 * i, open=3400.0, high=3410.0, low=3390.0, close=close,
                   volume=10.0, hold=500.0)

    db = Database(cfg.db_path)
    try:
        db.save_md_bars("RB0", [bar(i, 1000.0 + i) for i in range(0, 5)], bar_seconds=60, feed=raw_scheme)
        db.save_md_bars("RB0", [bar(i, 2000.0 + i) for i in range(3, 8)], bar_seconds=60, feed=canonical_scheme)
    finally:
        db.close()

    eng, db = restart(tmp_path, monkeypatch, instruments={"RB0": "RB2601"})
    try:
        eng._seed_md_bars()
        seeded = seeded_bars(eng.source, "RB0")
        assert len(seeded) == 8, "两段旧历史没有全部并进来"
        closes = {b.ts: b.close for b in db.md_bars("RB0", 100, bar_seconds=60, feed=eng._md_feed("RB0"))
                  for b in [Bar(**{k: b[k] for k in ("ts", "open", "high", "low", "close", "volume", "hold")})]}
        assert closes[1_700_000_000.0 + 60 * 3] == 2003.0 and closes[1_700_000_000.0 + 60 * 4] == 2004.0, \
            "重叠的时间戳没有以较新的一段为准"
        assert closes[1_700_000_000.0] == 1000.0 and closes[1_700_000_000.0 + 60 * 7] == 2007.0
    finally:
        eng.stop()
        db.close()


def test_a_ctp_competition_without_a_recorded_feed_signature_is_not_resumed(tmp_path, monkeypatch):
    """升级前创建的 CTP 竞赛存了配置段却没存生效签名：合约表里的交易所若在同一次升级里被改正，
    按存档配置重算两边都用新表、看不出区别。拿不出证据就不续跑。"""
    import json
    import sqlite3
    from arena.engine.loop import MARKET_FEED_KEY
    from .test_regression67 import restart

    eng, db = restart(tmp_path, monkeypatch)
    first = eng.comp_id
    eng.stop()
    db.close()
    raw = sqlite3.connect(str(tmp_path / "ctp.db"))
    try:
        snapshot = json.loads(raw.execute("SELECT config_json FROM competitions WHERE id=?", (first,)).fetchone()[0])
        assert MARKET_FEED_KEY in snapshot and "market_data" in snapshot
        del snapshot[MARKET_FEED_KEY]                     # 模拟上一版创建的记录
        raw.execute("UPDATE competitions SET config_json=? WHERE id=?", (json.dumps(snapshot, ensure_ascii=False), first))
        raw.commit()
    finally:
        raw.close()

    eng2, db2 = restart(tmp_path, monkeypatch)
    try:
        assert eng2.comp_id != first, "没记录生效签名的 CTP 竞赛被当成同一条行情续跑了"
        events = db2._q("SELECT message FROM events WHERE competition_id=? ORDER BY id", (first,))
        assert any("未记录生效的行情接入签名" in str(dict(r).get("message", "")) for r in events), [dict(r) for r in events]
    finally:
        eng2.stop()
        db2.close()


# ---------- 第 26 轮 ----------

def _seat(provider, **overrides):
    from arena.config import TraderCfg
    if provider == "jev":
        base = dict(id="seat", name="Seat", provider="jev", base_url="https://jev.example/v1", model="system-one",
                    fallback_rule="trend")
    else:
        base = dict(id="seat", name="Seat", provider="openai_compatible", base_url="https://example.invalid/v1",
                    model="deepseek", api_key="test-key", fallback_rule="trend")
    base.update(overrides)
    return TraderCfg(**base)


@pytest.mark.parametrize(("provider", "degraded"), [
    ("openai_compatible", {"api_key": ""}),     # 重启时 Key 的环境变量没了
    ("jev", {"base_url": ""}),                  # Jev 的端点被删了
])
def test_a_model_seat_that_degrades_to_a_rule_starts_a_fresh_competition(tmp_path, monkeypatch, provider, degraded):
    from arena.db import Database
    from arena.engine.loop import Engine
    from .test_engine import make_cfg

    monkeypatch.delenv("SEAT_API_KEY", raising=False)
    cfg = make_cfg(tmp_path, [_seat(provider)])
    db = Database(cfg.db_path)
    original = Engine(cfg, db, accelerate=True)
    first = original.comp_id
    assert original.traders["seat"].mode != "rule"
    original.stop()

    replacement = Engine(make_cfg(tmp_path, [_seat(provider, **degraded)]), db, accelerate=True)
    try:
        assert replacement.traders["seat"].mode == "rule", "前提：席位确实被降级成了演示策略"
        assert replacement.comp_id != first, "降级成演示策略的席位还在往模型的账户里记成绩"
    finally:
        replacement.stop()
        db.close()


def test_a_seat_that_was_always_a_rule_still_resumes(tmp_path, monkeypatch):
    """反向保护：从一开始就没配 Key 的席位两次都是演示策略，竞赛照常续跑。"""
    from arena.db import Database
    from arena.engine.loop import Engine
    from .test_engine import make_cfg

    monkeypatch.delenv("SEAT_API_KEY", raising=False)
    db = Database(make_cfg(tmp_path, [_seat("openai_compatible", api_key="")]).db_path)
    original = Engine(make_cfg(tmp_path, [_seat("openai_compatible", api_key="")]), db, accelerate=True)
    first = original.comp_id
    original.stop()
    again = Engine(make_cfg(tmp_path, [_seat("openai_compatible", api_key="")]), db, accelerate=True)
    try:
        assert again.comp_id == first
    finally:
        again.stop()
        db.close()


# ---------- 第 27 轮 ----------

@pytest.mark.parametrize("new_provider", ["rule", "mock"])
def test_a_model_seat_rewritten_as_a_rule_or_mock_seat_starts_a_fresh_competition(tmp_path, monkeypatch, new_provider):
    """保留 ID、把 provider 从模型改成 rule/mock：只看当前 provider 会把它漏掉，演示策略接着用前身模型的账户。"""
    from arena.config import TraderCfg
    from arena.db import Database
    from arena.engine.loop import Engine
    from .test_engine import make_cfg

    db = Database(make_cfg(tmp_path, [_seat("openai_compatible")]).db_path)
    original = Engine(make_cfg(tmp_path, [_seat("openai_compatible")]), db, accelerate=True)
    first = original.comp_id
    original.stop()

    rewritten = TraderCfg(id="seat", name="Seat", provider=new_provider, fallback_rule="trend",
                          mock_response='{"analysis":"ok","actions":[]}')
    replacement = Engine(make_cfg(tmp_path, [rewritten]), db, accelerate=True)
    try:
        assert replacement.comp_id != first, f"模型席位改成 {new_provider} 后仍在用前身模型的账户"
    finally:
        replacement.stop()
        db.close()


# ---------- 第 28 轮 ----------

def test_the_candidate_window_reaches_the_next_listed_delivery_month():
    """`auto_months: 1` 只订当月：股指第三个周五到期后到月底一笔 tick 都没有；国债只有季月、
    苹果没有 6～9 月，2、3 个月的预算照样会撞上空档。窗口至少要延到当月之后下一个挂牌的交割月。"""
    from arena.market.ctp import listed_delivery_months

    # 中金所股指：IF2609 于 9 月 18 日（第三个周五）到期，9 月 21 日起必须有 IF2610 在窗口里
    assert candidate_contracts("IF0", "CFFEX", 1, today=date(2026, 9, 21)) == ["IF2609", "IF2610"]
    # 国债只有季月：T2603 于 3 月 13 日（第二个周五）到期，下一个挂牌月是 6 月
    assert candidate_contracts("T0", "CFFEX", 1, today=date(2026, 3, 16)) == ["T2603", "T2604", "T2605", "T2606"]
    # 苹果没有 6～9 月合约：5 月中 AP605 到期之后，最近的下一个挂牌月是 10 月
    assert candidate_contracts("AP0", "CZCE", 2, today=date(2026, 5, 20))[-1] == "AP610"
    assert candidate_contracts("PK0", "CZCE", 1, today=date(2026, 4, 20))[-1] == "PK610"
    # 连续挂牌的品种：当月之后紧跟下月，最小窗口就是两个月
    assert candidate_contracts("RB0", "SHFE", 1, today=date(2026, 3, 5)) == ["rb2603", "rb2604"]
    # 预算够大时一根都不多订、一根都不少订
    assert len(candidate_contracts("IF0", "CFFEX", 12, today=date(2026, 9, 21))) == 12
    assert len(candidate_contracts("T0", "CFFEX", 6, today=date(2026, 3, 16))) == 6
    assert listed_delivery_months("RB0") == frozenset(range(1, 13))


def test_a_small_budget_still_subscribes_a_live_forward_contract(tmp_path, monkeypatch):
    """订阅列表是从候选表来的：窗口延长了，前置那边也得真的订上 T2606。"""
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    src = _source(tmp_path, ["T0"], auto_months=1)
    try:
        assert src._candidates["T0"] == ["T2603", "T2604", "T2605", "T2606"]
        src.start()
        assert "T2606" in src._sdk.api.subscribed[0]
    finally:
        src.close()


def _ctp_engine(tmp_path, monkeypatch, symbols):
    """真实 Engine + 真实 CtpMarketSource（假 SDK），品种由用例指定（要有一个只有日盘的）。"""
    from arena.config import AppConfig, TraderCfg
    from arena.db import Database
    from arena.engine import loop as loop_mod

    def fake_load(self):
        self._sdk = FakeSdk()
        return self._sdk

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    cfg = AppConfig(
        competition={"name": "ctp-night", "market": "ctp", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1,
                     "timezone": "Asia/Shanghai"},
        symbols=list(symbols),
        market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json"},
                     "ctp": {"md_front": "tcp://127.0.0.1:10131", "broker_id": "0001", "user_id": "88888888",
                             "password": "secret", "flow_dir": str(tmp_path / "flow"), "auto_months": 2,
                             "discovery_grace_sec": 0}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="rule", fallback_rule="trend")],
        storage={"db_path": str(tmp_path / "ctp.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return loop_mod.Engine(cfg, db, accelerate=True), db


def _day_session_bars(day: date, base_price: float):
    """一个交易日的 1m bar（09:30–11:30、13:00–15:00，股指的日盘；螺纹钢也在这些分钟里开市）。"""
    from datetime import datetime as dt, time as clock, timedelta

    bars = []
    i = 0
    for (h0, m0, h1, m1) in ((9, 30, 11, 30), (13, 0, 15, 0)):
        at = dt.combine(day, clock(h0, m0), tzinfo=SH)
        end = dt.combine(day, clock(h1, m1), tzinfo=SH)
        while at < end:
            price = base_price + (i % 7) * 0.5
            bars.append(Bar(ts=at.timestamp(), open=price, high=price + 1, low=price - 1, close=price,
                            volume=10, hold=1000 + i))
            at += timedelta(seconds=60)
            i += 1
    return bars


def _shanghai(day: date, hour: int, minute: int, second: int = 0) -> float:
    from datetime import datetime as dt, time as clock
    return dt.combine(day, clock(hour, minute, second), tzinfo=SH).timestamp()


def test_a_day_only_symbol_keeps_its_prior_close_for_night_research_after_a_restart(tmp_path, monkeypatch):
    """15:00 之后、21:00 之前重启：IF0 当晚不会再有 tick，`latest()` 按设计不返回预热的历史，
    而 21:00 的自动研究要求每个品种都有报价——混合日夜盘的品种表在午后重启后就永远出不了夜盘研究。
    只给研究补上 IF0 上一节的收盘价；实盘路径一条预热数据都不能拿到。"""
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        history = {symbol: [bar for day in (date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22))
                            for bar in _day_session_bars(day, base)]
                   for symbol, base in (("RB0", 3400.0), ("IF0", 4200.0))}
        for symbol, bars in history.items():
            eng.source.seed_bars(symbol, bars)          # 重启：库里的历史预热进来，还没有一笔 tick
        prior_close = history["IF0"][-1]
        # 夜盘集合竞价：只有 RB0 报了价
        spi = eng.source._sdk.api.spi
        spi.OnRtnDepthMarketData(tick(eng.source.dominant_contract("RB0"), 3450.0, volume=10, open_interest=1000,
                                      clock="20:59:30", day="20260922"))
        now = _shanghai(date(2026, 9, 22), 20, 59, 40)
        opens_at = _shanghai(date(2026, 9, 22), 21, 0)

        assert eng.source.latest(["RB0", "IF0"]).keys() == {"RB0"}, "前提：IF0 确实没有实时报价"
        snap, health = eng._preopen_snapshot(1, opens_at=opens_at, now=now)
        assert snap is not None and health["history_quality"] == "full", health
        assert snap.symbols["IF0"].quote.price == prior_close.close
        assert snap.symbols["IF0"].quote.ts == prior_close.ts
        assert snap.symbols["RB0"].quote.price == 3450.0
        assert eng._preopen_unready_symbols({s: v.quote for s, v in snap.symbols.items()}, opens_at, now=now) == []
        # 补进研究快照的报价没有泄漏到实盘路径
        assert eng.source.latest(["IF0"]) == {}
        assert "IF0" not in eng.quotes and "IF0" not in eng.prices
    finally:
        eng.stop()
        db.close()


def test_closed_session_quotes_only_fill_symbols_that_do_not_open_this_session(tmp_path, monkeypatch):
    """这次要开盘的品种必须有新鲜的实时报价；手动研究要求全部品种新鲜——都不能拿预热的历史充数。"""
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        for symbol, base in (("RB0", 3400.0), ("IF0", 4200.0)):
            eng.source.seed_bars(symbol, [bar for day in (date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22))
                                          for bar in _day_session_bars(day, base)])
        now = _shanghai(date(2026, 9, 22), 20, 59, 40)
        opens_at = _shanghai(date(2026, 9, 22), 21, 0)
        # RB0 21:00 开盘却一笔 tick 都没有：不能拿 15:00 的库里价冒充，只有 IF0 拿到上一节收盘
        assert eng._closed_session_research_quotes({}, opens_at, now=now).keys() == {"IF0"}
        snap, health = eng._preopen_snapshot(1, opens_at=opens_at, now=now)
        assert snap is None and "RB0" in health["detail"], health
        # 手动研究没有"这次要开的盘"，全部品种都要新鲜：IF0 同样不补
        assert eng._closed_session_research_quotes({}, None, now=now) == {}
        snap, _health = eng._preopen_snapshot(1, allow_quote_only=True, opens_at=None, now=now)
        assert snap is None
    finally:
        eng.stop()
        db.close()


def test_a_close_from_before_a_missed_trading_day_is_not_a_prior_close(tmp_path, monkeypatch):
    """库里最后一根 bar 是 9 月 21 日的，而 9 月 22 日 IF0 开过市却一笔 tick 都没收到：
    那不是"上一节的收盘"，宁可让研究报无行情，也不把前天的价格当昨天的收盘送进模型。"""
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        eng.source.seed_bars("RB0", [bar for day in (date(2026, 9, 21), date(2026, 9, 22))
                                     for bar in _day_session_bars(day, 3400.0)])
        eng.source.seed_bars("IF0", [bar for day in (date(2026, 9, 18), date(2026, 9, 21))
                                     for bar in _day_session_bars(day, 4200.0)])
        spi = eng.source._sdk.api.spi
        spi.OnRtnDepthMarketData(tick(eng.source.dominant_contract("RB0"), 3450.0, volume=10, open_interest=1000,
                                      clock="20:59:30", day="20260922"))
        now = _shanghai(date(2026, 9, 22), 20, 59, 40)
        assert eng.source.closed_session_quotes(["IF0"], now=now) == {}
        snap, health = eng._preopen_snapshot(1, opens_at=_shanghai(date(2026, 9, 22), 21, 0), now=now)
        assert snap is None and "IF0" in health["detail"], health
        # 反过来：22 日早上 08:30（当天日盘还没开）问，21 日的收盘就是上一节的收盘
        morning = _shanghai(date(2026, 9, 22), 8, 30)
        assert eng.source.closed_session_quotes(["IF0"], now=morning)["IF0"].price == pytest.approx(
            _day_session_bars(date(2026, 9, 21), 4200.0)[-1].close)
    finally:
        eng.stop()
        db.close()


# ---------- 第 29 轮 ----------

def test_a_bar_that_stops_before_the_session_close_is_not_a_prior_close(tmp_path, monkeypatch):
    """行情源 14:00 掉线、重启前没接回来：库里最后一根 bar 是盘中价，不是收盘价，研究不能拿它当收盘。"""
    from arena.market.ctp import CLOSED_SESSION_QUOTE_GRACE_SEC

    history = [bar for day in (date(2026, 9, 18), date(2026, 9, 21)) for bar in _day_session_bars(day, 4200.0)]
    full_day = _day_session_bars(date(2026, 9, 22), 4200.0)
    close_ts = _shanghai(date(2026, 9, 22), 15, 0)
    now = _shanghai(date(2026, 9, 22), 20, 59, 40)
    opens_at = _shanghai(date(2026, 9, 22), 21, 0)

    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        # IF0 停在 13:59（盘中价）；RB0 历史齐全并有夜盘 tick
        eng.source.seed_bars("IF0", history + [bar for bar in full_day if bar.ts < _shanghai(date(2026, 9, 22), 14, 0)])
        eng.source.seed_bars("RB0", history + full_day)
        spi = eng.source._sdk.api.spi
        spi.OnRtnDepthMarketData(tick(eng.source.dominant_contract("RB0"), 3450.0, volume=10, open_interest=1000,
                                      clock="20:59:30", day="20260922"))
        assert eng.source.closed_session_quotes(["IF0"], now=now) == {}
        snap, health = eng._preopen_snapshot(1, opens_at=opens_at, now=now)
        assert snap is None and "IF0" in health["detail"], health
    finally:
        eng.stop()
        db.close()

    # 刚好在提前量之外的最后一根同样不算（预热只能往前补历史，所以换一个新的源来试）
    src = _source(tmp_path / "edge", ["IF0"])
    try:
        too_early = [bar for bar in full_day if bar.ts < close_ts - CLOSED_SESSION_QUOTE_GRACE_SEC]
        src.seed_bars("IF0", history + too_early)
        assert src.bars_1m("IF0")[-1].ts == too_early[-1].ts
        assert src.closed_session_quotes(["IF0"], now=now) == {}
    finally:
        src.close()


def test_a_thin_symbol_whose_last_update_is_minutes_before_the_bell_still_counts(tmp_path):
    """收盘前最后几分钟没有报价更新（清淡品种，CTP 没有变化就不推）仍算收盘；收在 15:00:00 那个桶里的
    收盘 tick（桶起点等于收盘时刻）也算。"""
    from arena.market.ctp import CLOSED_SESSION_QUOTE_GRACE_SEC

    full_day = _day_session_bars(date(2026, 9, 22), 4200.0)
    close_ts = _shanghai(date(2026, 9, 22), 15, 0)
    now = _shanghai(date(2026, 9, 22), 20, 59, 40)
    within = [bar for bar in full_day if bar.ts >= close_ts - CLOSED_SESSION_QUOTE_GRACE_SEC]

    src = _source(tmp_path / "thin", ["IF0"])
    try:
        src.seed_bars("IF0", [bar for bar in full_day if bar.ts <= within[0].ts])   # 最后一根刚好压在提前量上
        quote = src.closed_session_quotes(["IF0"], now=now)["IF0"]
        assert quote.ts == within[0].ts and quote.price == within[0].close
    finally:
        src.close()

    src = _source(tmp_path / "bell", ["IF0"])
    try:
        src.seed_bars("IF0", full_day + [Bar(ts=close_ts, open=4201.0, high=4201.0, low=4201.0, close=4201.0)])
        assert src.closed_session_quotes(["IF0"], now=now)["IF0"].price == 4201.0
    finally:
        src.close()


def test_candidates_roll_forward_when_the_exchange_month_advances(tmp_path, monkeypatch):
    """进程跨月不重启：3 月启动、`auto_months: 1` 订的是 IF2603/IF2604，4 月 17 日 IF2604 到期后
    5 月合约没人订，这个品种就没了报价。每个交易所日历月按今天重新枚举、补订新进入窗口的合约。"""
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    src = _source(tmp_path, ["IF0", "RB0"], auto_months=1, instruments={"RB0": "rb2605"})
    try:
        src.start()
        assert src._candidates["IF0"] == ["IF2603", "IF2604"]
        assert src.latest(["IF0"]) == {} and len(src._sdk.api.subscribed) == 1, "同一个月内不该重订"

        monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 4, 20), raising=False)
        assert src.latest(["IF0"]) == {}                        # 引擎每个 tick 都会问一次报价：跨月由它触发
        assert src._candidates["IF0"] == ["IF2603", "IF2604", "IF2605"]
        assert src._sdk.api.subscribed[-1] == ["IF2605"], src._sdk.api.subscribed
        assert src._candidates["RB0"] == ["rb2605"], "钉死的品种不参与自动枚举"
        assert src.latest(["IF0"]) == {} and len(src._sdk.api.subscribed) == 2, "同一个月只补订一次"

        # 新订的合约真的能供数：它的 tick 以 IF0 的名义发布
        src._sdk.api.spi.OnRtnDepthMarketData(tick("IF2605", 4300.0, volume=1, open_interest=100,
                                                   clock="09:35:00", day="20260420"))
        assert src.latest(["IF0"])["IF0"].price == 4300.0
        assert src.publishing_contract("IF0") == "IF2605"
    finally:
        src.close()


def test_candidates_added_while_logged_out_are_subscribed_at_the_next_login(tmp_path, monkeypatch):
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    src = _source(tmp_path, ["IF0"], auto_months=1)
    try:
        monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 4, 20), raising=False)
        assert src.refresh_candidates() == ["IF2605"]           # 还没 start()：只记进候选表
        assert "IF2605" in src._owner
        src.start()
        assert "IF2605" in src._sdk.api.subscribed[0]
    finally:
        src.close()


def test_an_exchange_correction_only_changes_the_spelling_of_the_same_instrument_ids():
    """`_seed_md_bars` 认领不带交易所的旧命名空间之所以安全：CTP 只按合约代码订阅，交易所字段从不
    发给前置，它只影响代码的拼写。同一拼写规则的交易所之间改来改去，订的是同一批合约；拼写规则
    变了，新拼写在前置那里不存在（一个合约只有一种代码），一笔 tick 都不会来。"""
    today = date(2026, 9, 21)
    lower = [candidate_contracts("RB0", exchange, 3, today=today) for exchange in ("SHFE", "DCE", "INE", "GFEX")]
    assert all(ids == lower[0] for ids in lower)
    assert candidate_contracts("RB0", "CFFEX", 3, today=today) == [ids.upper() for ids in lower[0]]
    assert candidate_contracts("RB0", "CZCE", 3, today=today) != candidate_contracts("RB0", "CFFEX", 3, today=today)
    assert candidate_contracts("RB0", "CZCE", 3, today=today)[0] == "RB609"


# ---------- 第 30 轮 ----------

def _live_ticks(eng, symbol, bars, *, day: str):
    """把一段 1m bar 当成实时 tick 灌给该品种的主力合约（进程一直开着，不是重启）。"""
    spi = eng.source._sdk.api.spi
    instrument = eng.source.dominant_contract(symbol)
    for i, bar in enumerate(bars):
        at = datetime.fromtimestamp(bar.ts + 30, SH)
        spi.OnRtnDepthMarketData(tick(instrument, bar.close, volume=10 * (i + 1), open_interest=1000,
                                      clock=at.strftime("%H:%M:%S"), day=day, settlement=4000.0))


def test_a_cached_quote_that_died_before_the_close_is_not_a_prior_close(tmp_path, monkeypatch):
    """进程一直开着，IF0 的行情源 14:00 断了：`latest()` 缓存里挂着 13:59 的报价。它不在预热那条路上，
    却同样是盘中价——21:00 研究不能因为它"在缓存里"就免检、把它当成收盘送进模型。"""
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        today = _day_session_bars(date(2026, 9, 22), 4200.0)
        cutoff = _shanghai(date(2026, 9, 22), 14, 0)
        for symbol, base in (("RB0", 3400.0), ("IF0", 4200.0)):
            eng.source.seed_bars(symbol, [bar for day in (date(2026, 9, 18), date(2026, 9, 21))
                                          for bar in _day_session_bars(day, base)])
        _live_ticks(eng, "IF0", [bar for bar in today if bar.ts < cutoff], day="20260922")
        _live_ticks(eng, "RB0", _day_session_bars(date(2026, 9, 22), 3400.0), day="20260922")
        eng.source._sdk.api.spi.OnRtnDepthMarketData(
            tick(eng.source.dominant_contract("RB0"), 3450.0, volume=10_000, open_interest=1000,
                 clock="20:59:30", day="20260922"))
        now = _shanghai(date(2026, 9, 22), 20, 59, 40)
        opens_at = _shanghai(date(2026, 9, 22), 21, 0)

        cached = eng.source.latest(["IF0"])["IF0"]
        assert cached.ts < cutoff, "前提：缓存里确实挂着一条 14:00 之前的报价"
        assert eng.source.closed_session_quotes(["IF0"], now=now) == {}
        assert "IF0" not in eng._closed_session_research_quotes({"IF0": cached}, opens_at, now=now)
        snap, health = eng._preopen_snapshot(1, opens_at=opens_at, now=now)
        assert snap is None and "IF0" in health["detail"], health
    finally:
        eng.stop()
        db.close()


def test_a_cached_quote_that_reached_the_close_keeps_its_real_tick_time(tmp_path, monkeypatch):
    """正常的一天：IF0 的 tick 一直到 14:59。缓存里那条报价通过收盘校验后原样使用——真实 tick 时刻、
    相对昨结算的涨跌幅，而不是退化成预热那种桶起点、涨跌幅为 0 的报价。"""
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        for symbol, base in (("RB0", 3400.0), ("IF0", 4200.0)):
            eng.source.seed_bars(symbol, [bar for day in (date(2026, 9, 18), date(2026, 9, 21))
                                          for bar in _day_session_bars(day, base)])
            _live_ticks(eng, symbol, _day_session_bars(date(2026, 9, 22), base), day="20260922")
        eng.source._sdk.api.spi.OnRtnDepthMarketData(
            tick(eng.source.dominant_contract("RB0"), 3450.0, volume=10_000, open_interest=1000,
                 clock="20:59:30", day="20260922"))
        now = _shanghai(date(2026, 9, 22), 20, 59, 40)
        opens_at = _shanghai(date(2026, 9, 22), 21, 0)

        cached = eng.source.latest(["IF0"])["IF0"]
        assert cached.ts == _shanghai(date(2026, 9, 22), 14, 59, 30) and cached.change_pct != 0.0
        snap, health = eng._preopen_snapshot(1, opens_at=opens_at, now=now)
        assert snap is not None and health["history_quality"] == "full", health
        assert snap.symbols["IF0"].quote == cached
    finally:
        eng.stop()
        db.close()


# ---------- 第 31 轮 ----------

@pytest.mark.parametrize("failure", ["raise", "rc"])
def test_a_failed_incremental_subscription_is_retried_on_later_ticks(tmp_path, monkeypatch, failure):
    """跨月补订时 `SubscribeMarketData` 失败：合约已经记进 `_owner`、月份也翻过去了，连接不断就没有
    下一次登录来补——原来这个合约就永远订不上。现在失败的留在待订里，之后的 tick 按退避间隔重试。"""
    from arena.market.ctp import SUBSCRIBE_RETRY_SEC

    clock = [1000.0]
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    src = _source(tmp_path, ["IF0"], auto_months=1, clock=lambda: clock[0])
    try:
        src.start()
        api = src._sdk.api
        original = api.SubscribeMarketData
        attempts = []

        def failing(ids, count):
            attempts.append(list(ids))
            if failure == "raise":
                raise RuntimeError("front busy")
            return -3       # CTP：每秒请求数超限之类的非零返回码，不抛异常

        api.SubscribeMarketData = failing
        monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 4, 20), raising=False)
        assert src.latest(["IF0"]) == {}
        assert attempts == [["IF2605"]] and "IF2605" in src._owner
        assert src.pending_subscriptions == ["IF2605"], "失败的订阅被当成订上了"
        assert "订阅" in src.health()["detail"]
        # 退避期内的下一个 tick 不重复轰前置
        src.latest(["IF0"])
        assert attempts == [["IF2605"]]
        # 退避期过后重试，这次成功；之后不再重订
        api.SubscribeMarketData = original
        clock[0] += SUBSCRIBE_RETRY_SEC
        src.latest(["IF0"])
        assert api.subscribed[-1] == ["IF2605"] and src.pending_subscriptions == []
        src.latest(["IF0"])
        assert len(api.subscribed) == 2
    finally:
        src.close()


def test_a_failed_login_time_subscription_is_retried_without_a_new_login(tmp_path):
    """登录后的整批订阅同样可能失败：不能只靠下一次登录，连接不断时由之后的 tick 补上。"""
    from arena.market.ctp import SUBSCRIBE_RETRY_SEC

    class FlakySubscribeApi(FakeApi):
        def SubscribeMarketData(self, ids, count):
            self.subscribe_calls += 1
            if self.subscribe_calls == 1:
                return -1
            return super().SubscribeMarketData(ids, count)

    clock = [1000.0]
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["IF0"], {"IF0": specs["IF0"]}, holidays, md_front="tcp://127.0.0.1:10131",
                          broker_id="0001", user_id="8", password="p", discovery_grace_sec=0, auto_months=2,
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(api_class=FlakySubscribeApi),
                          clock=lambda: clock[0])
    try:
        src.start()
        api = src._sdk.api
        assert api.subscribed == [] and sorted(src.pending_subscriptions) == sorted(src._owner)
        src.latest(["IF0"])
        assert api.subscribed == [], "退避期内不该重试"
        clock[0] += SUBSCRIBE_RETRY_SEC
        src.latest(["IF0"])
        assert api.subscribed == [sorted(src._owner)] and src.pending_subscriptions == []
    finally:
        src.close()


# ---------- 第 32 轮 ----------

class _RespondingApi(FakeApi):
    """按 CTP 的方式逐个应答订阅：`error_for` 里的合约以该错误码拒绝，`silent` 里的合约不应答。"""
    error_for: dict = {}
    silent: set = set()

    def SubscribeMarketData(self, ids, count):
        assert count == len(ids)
        self.subscribe_calls += 1
        self.subscribed.append(list(ids))
        self.respond_subscriptions([cid for cid in ids if cid not in self.silent], error_for=self.error_for)
        return 0


def _source_with(tmp_path, symbols, api_class, clock, **kw):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return CtpMarketSource(symbols, {s: specs[s] for s in symbols}, holidays, md_front="tcp://127.0.0.1:10131",
                           broker_id="0001", user_id="8", password="p", discovery_grace_sec=0,
                           flow_dir=str(tmp_path / "flow"), sdk=FakeSdk(api_class=api_class), clock=clock, **kw)


def test_a_subscription_is_confirmed_per_instrument_not_per_request(tmp_path, monkeypatch):
    """`SubscribeMarketData` 返回 0 只表示请求排进了队列；前置随后对每个合约各回一条应答。
    一个合约被临时拒绝（前置忙）时，不能因为整批请求"成功"就当它订上了——要留在待确认里重发。"""
    from arena.market.ctp import SUBSCRIBE_RETRY_SEC

    class Api(_RespondingApi):
        error_for = {"IF2604": 90}

    clock = [1000.0]
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    src = _source_with(tmp_path, ["IF0"], Api, lambda: clock[0], auto_months=2)
    try:
        src.start()
        api = src._sdk.api
        assert api.subscribed == [["IF2603", "IF2604"]]
        assert src.pending_subscriptions == ["IF2604"], "被前置拒绝的合约被当成订上了"
        assert "1 个待前置确认订阅" in src.health()["detail"]
        src.latest(["IF0"])
        assert len(api.subscribed) == 1, "退避期内不重发"
        Api.error_for = {}
        clock[0] += SUBSCRIBE_RETRY_SEC
        src.latest(["IF0"])
        assert api.subscribed[-1] == ["IF2604"] and src.pending_subscriptions == []
        assert "待前置确认" not in src.health()["detail"]
    finally:
        src.close()


def test_a_permanently_rejected_candidate_is_dropped_without_retries(tmp_path, monkeypatch):
    """没挂牌的候选（国债只有季月，T2604/T2605 不存在）被前置以"找不到合约"拒绝：属于预期内，
    直接放弃，不占着待确认、不反复轰前置。"""
    from arena.market.ctp import SUBSCRIBE_RETRY_SEC

    class Api(_RespondingApi):
        error_for = {"T2604": 16, "T2605": 16}

    clock = [1000.0]
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    src = _source_with(tmp_path, ["T0"], Api, lambda: clock[0], auto_months=1)
    try:
        src.start()
        api = src._sdk.api
        assert api.subscribed == [["T2603", "T2604", "T2605", "T2606"]]
        assert src.pending_subscriptions == []
        clock[0] += SUBSCRIBE_RETRY_SEC
        src.latest(["T0"])
        assert len(api.subscribed) == 1, "永久拒绝的合约被重发了"
        assert src.health()["errors"] == 0, "预期内的拒绝不算错误"
    finally:
        src.close()


def test_an_unanswered_subscription_gives_up_after_bounded_attempts(tmp_path, monkeypatch):
    """前置对某个合约始终不应答：按退避重发到上限后放弃，不无限重发；之后迟到的应答不出错。"""
    from arena.market.ctp import SUBSCRIBE_MAX_ATTEMPTS, SUBSCRIBE_RETRY_SEC

    class Api(_RespondingApi):
        silent = {"IF2604"}

    clock = [1000.0]
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    src = _source_with(tmp_path, ["IF0"], Api, lambda: clock[0], auto_months=2)
    try:
        src.start()
        api = src._sdk.api
        assert src.pending_subscriptions == ["IF2604"]
        for _ in range(SUBSCRIBE_MAX_ATTEMPTS + 2):
            clock[0] += SUBSCRIBE_RETRY_SEC
            src.latest(["IF0"])
        sends = [ids for ids in api.subscribed if "IF2604" in ids]
        assert len(sends) == SUBSCRIBE_MAX_ATTEMPTS, api.subscribed
        assert src.pending_subscriptions == []
        # 迟到的成功应答：早已放弃，忽略即可
        api.respond_subscriptions(["IF2604"])
        assert src.pending_subscriptions == []
    finally:
        src.close()


# ---------- 第 33 轮 ----------

def test_a_future_dated_tick_does_not_freeze_the_contract(tmp_path):
    """一条日期写错、指向未来的 tick 原来会被记成该合约的"最新时刻"：引擎拒掉那条报价，之后每一条
    正常的 tick 都因为"比它旧"被丢掉，合约冻住直到重启。超前本机时钟太多的 tick 要在进任何状态之前丢弃。"""
    from datetime import timedelta
    from arena.market.ctp import MAX_TICK_FUTURE_SKEW_SEC

    src = _source(tmp_path, ["RB0"])
    try:
        src.start()
        spi = src._sdk.api.spi
        rb = src.dominant_contract("RB0")
        tomorrow = datetime.now(SH) + timedelta(days=1)
        spi.OnRtnDepthMarketData(tick(rb, 3400.0, volume=1, open_interest=100,
                                      clock=tomorrow.strftime("%H:%M:%S"), day=tomorrow.strftime("%Y%m%d")))
        assert src.latest(["RB0"]) == {}, "未来时刻的 tick 被当成报价发布了"
        assert rb not in src._contract_tick_ts

        now = datetime.now(SH)
        spi.OnRtnDepthMarketData(tick(rb, 3410.0, volume=2, open_interest=100,
                                      clock=now.strftime("%H:%M:%S"), day=now.strftime("%Y%m%d")))
        quote = src.latest(["RB0"])["RB0"]
        assert quote.price == 3410.0 and abs(quote.ts - now.timestamp()) < 1.0, "合约被未来的 tick 冻住了"

        # 本机时钟慢几十秒是正常现象：偏差在上限之内的 tick 照常接受
        slightly_ahead = now + timedelta(seconds=MAX_TICK_FUTURE_SKEW_SEC / 2)
        spi.OnRtnDepthMarketData(tick(rb, 3420.0, volume=3, open_interest=100,
                                      clock=slightly_ahead.strftime("%H:%M:%S"), day=slightly_ahead.strftime("%Y%m%d")))
        assert src.latest(["RB0"])["RB0"].price == 3420.0
    finally:
        src.close()


# ---------- 同步订阅失败也计入重试上限 ----------

@pytest.mark.parametrize("failure", ["raise", "rc"])
def test_a_persistently_failing_subscribe_call_stops_after_bounded_attempts(tmp_path, monkeypatch, failure):
    """`SubscribeMarketData` 一直抛异常或返回非零码：原来尝试次数只在返回 0 时才加，这批合约永远到不了
    SUBSCRIBE_MAX_ATTEMPTS，整批每 30 秒重发一次直到进程退出。现在同步失败也算一次，到上限就放弃。"""
    from arena.market.ctp import SUBSCRIBE_MAX_ATTEMPTS, SUBSCRIBE_RETRY_SEC

    class Api(_RespondingApi):
        def SubscribeMarketData(self, ids, count):
            self.subscribe_calls += 1
            self.subscribed.append(list(ids))
            if failure == "raise":
                raise RuntimeError("front busy")
            return -3

    clock = [1000.0]
    monkeypatch.setattr(ctp_mod, "exchange_today", lambda: date(2026, 3, 16), raising=False)
    src = _source_with(tmp_path, ["IF0"], Api, lambda: clock[0], auto_months=2)
    try:
        src.start()
        api = src._sdk.api
        assert sorted(src.pending_subscriptions) == ["IF2603", "IF2604"], "同步失败的订阅不该当成订上了"
        for _ in range(SUBSCRIBE_MAX_ATTEMPTS + 3):
            clock[0] += SUBSCRIBE_RETRY_SEC
            src.latest(["IF0"])
        assert len(api.subscribed) == SUBSCRIBE_MAX_ATTEMPTS, api.subscribed
        assert src.pending_subscriptions == []
        assert "订阅" in src.health()["detail"], "放弃前的同步失败要留在健康状态里"
    finally:
        src.close()
