"""Regression 66：盘前预约的就绪判定，以及行情接入设置与竞赛身份的绑定。

1. 一个盘前刚断线的 CTP 源，`health().ok` 已经是假、缓存的 `last_ok` 还在窗口内。只看时间戳
   的闸门会放它过去：worker 建出完整的缓存快照、占掉这个开盘时刻的唯一预约，然后因为
   `fresh_ok` 也看 `ok` 而把它钉成"不可用"——之后重连成功也没有第二次机会。
2. 在跑的装机把 RB0 从一个合约换钉到另一个、或者改了 `bar_seconds`，价格序列与指标就变了
   （落库的 md_bars 还会拿旧设置的 bar 去给新设置预热），而市场、合约宇宙、选手名单都没变，
   于是旧排行榜继续累加，两场性质不同的实验被混在一起。
"""
from __future__ import annotations

import json
import time

import pytest

from arena.engine.loop import (QUOTES_FRESH, QUOTES_UNKNOWN, QUOTES_UNREADY, market_feed_signature,
                               preopen_quote_readiness)

from .test_ctp import build_engine, make_ctp_cfg


MAX_AGE = 600.0


# ---------- 1. 就绪度三态 ----------

def test_a_disconnected_source_is_unready_even_with_a_fresh_cached_timestamp():
    """自报 not ok 与时间戳超窗是同一个问题的两种说法，闸门必须都认。"""
    now = time.time()
    assert preopen_quote_readiness({"ok": False, "last_ok": now}, MAX_AGE, now=now) == QUOTES_UNREADY
    assert preopen_quote_readiness({"ok": False}, MAX_AGE, now=now) == QUOTES_UNREADY
    assert preopen_quote_readiness({"ok": True, "last_ok": now - MAX_AGE - 1}, MAX_AGE, now=now) == QUOTES_UNREADY
    assert preopen_quote_readiness({"ok": True, "last_ok": now}, MAX_AGE, now=now) == QUOTES_FRESH
    assert preopen_quote_readiness({"ok": True}, MAX_AGE, now=now) == QUOTES_FRESH


@pytest.mark.parametrize("health", [
    None,                                   # 源没有 health()
    "not-a-dict",
    {},                                     # 有 health() 但什么都没报
    {"last_ok": 0.0},                       # 只报时间戳且是 1970：陈旧，但下面单独校验
    {"ok": True, "last_ok": "just now"},    # 声称记录了刷新时刻，却给不出可用的值
    {"ok": True, "last_ok": True},          # bool 是 int 的子类，float(True) 会变成 1970
    {"ok": True, "last_ok": float("nan")},
    {"ok": True, "last_ok": float("inf")},
])
def test_unreadable_health_is_neither_fresh_nor_unready(health):
    """三态里的 unknown 不能并进另外两个。

    当成 fresh：证据不足的快照会被拿去调四个付费模型。
    当成 unready：不提供 health()（或不报这些字段）的行情源，自动盘前研究被整条挡掉。
    """
    now = time.time()
    verdict = preopen_quote_readiness(health, MAX_AGE, now=now)
    if health == {"last_ok": 0.0}:
        assert verdict == QUOTES_UNREADY, "正面报出来的 1970 就是陈旧"
        return
    assert verdict == QUOTES_UNKNOWN


def test_a_disconnected_ctp_feed_does_not_burn_the_preopen_reservation(tmp_path, monkeypatch):
    """预热过、盘前才断线的源：last_ok 还新鲜，但这个开盘时刻不能被它占掉。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        started = []
        monkeypatch.setattr(eng, "_start_debate", lambda *a, **k: started.append(a) or True)
        # 刚断线：ok 已经是假，缓存的 last_ok 还在允许窗口内（每个要开盘的品种都有新鲜报价，只考源级就绪度）
        monkeypatch.setattr(eng.source, "health", lambda: {"ok": False, "last_ok": time.time()})
        from arena.models import Quote
        eng.quotes = {s: Quote(symbol=s, price=3400.0, ts=time.time() - 1.0, open_interest=1.0) for s in ("RB0", "I0")}

        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 900))
        assert eng._maybe_start_preopen_debate() is False
        assert started == [], "源自报不可用时就把这个开盘时刻的预约占掉了，重连也不会再跑"

        # 重连上了：窗口还没走完，这次要照常调度
        monkeypatch.setattr(eng.source, "health", lambda: {"ok": True, "last_ok": time.time()})
        assert eng._maybe_start_preopen_debate() is True
        assert started, "重连之后仍然不调度"
    finally:
        eng.stop()
        db.close()


def test_a_still_disconnected_feed_still_gets_one_try_at_the_floor(tmp_path, monkeypatch):
    """一直没重连上也要在窗口末尾试一次，留下"始终没等到行情"的审计记录。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        started = []
        monkeypatch.setattr(eng, "_start_debate", lambda *a, **k: started.append(a) or True)
        monkeypatch.setattr(eng.source, "health", lambda: {"ok": False, "last_ok": time.time()})
        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 30))
        assert eng._maybe_start_preopen_debate() is True
        assert started, "整个开盘时刻会悄无声息地跳过"
    finally:
        eng.stop()
        db.close()


def test_a_source_without_health_is_not_blocked(tmp_path, monkeypatch):
    """读不出就绪度的源不能被新闸门挡掉。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        started = []
        monkeypatch.setattr(eng, "_start_debate", lambda *a, **k: started.append(a) or True)
        monkeypatch.setattr(eng.source, "health", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        from arena.models import Quote
        eng.quotes = {s: Quote(symbol=s, price=3400.0, ts=time.time() - 1.0, open_interest=1.0) for s in ("RB0", "I0")}
        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 900))
        assert eng._maybe_start_preopen_debate() is True
        assert started
    finally:
        eng.stop()
        db.close()


# ---------- 2. 行情接入设置进竞赛身份 ----------

def restart(tmp_path, monkeypatch, **ctp):
    from arena.db import Database
    from arena.engine import loop as loop_mod
    from arena.market.ctp import CtpMarketSource
    from .test_ctp import FakeSdk

    def fake_load(self):
        self._sdk = FakeSdk()
        return self._sdk

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    cfg = make_ctp_cfg(tmp_path, **ctp)
    db = Database(cfg.db_path)
    return loop_mod.Engine(cfg, db, accelerate=True), db


@pytest.mark.parametrize("changed", [
    {"instruments": {"RB0": "rb2605"}},     # 把 RB0 钉到另一个合约：换了一条价格序列
    {"bar_seconds": 30},                    # 聚合周期：K 线与全部指标都变
    {"md_front": "tcp://127.0.0.1:20131"},  # 仿真前置与生产前置给的是完全不同的行情
    {"broker_id": "9999"},
    {"auto_months": 6},                     # 主力候选范围变了，选出来的合约可能不同
    {"dominant_stale_sec": 900.0},          # 主力何时换手 → 发布的是哪个合约的价
    {"discovery_grace_sec": 30.0},
    {"max_bars": 20_000},                   # 保留根数决定慢指标算不算得出
    {"persist_bars": False},                # 决定重启后是否用旧 bar 预热
])
def test_changing_the_ctp_feed_starts_a_new_competition(tmp_path, monkeypatch, changed):
    eng, db = restart(tmp_path, monkeypatch)
    first = eng.comp_id
    eng.stop()
    db.close()

    eng2, db2 = restart(tmp_path, monkeypatch, **changed)
    try:
        assert eng2.comp_id != first, f"改了 {changed} 之后仍然沿用同一张排行榜"
    finally:
        eng2.stop()
        db2.close()


@pytest.mark.parametrize("unchanged", [
    {},
    {"connect_timeout_sec": 45.0},          # 只影响连不连得上，不改价格序列
    {"flow_dir_suffix": True},              # 本地流文件目录换个位置也不该掀掉竞赛
])
def test_an_unchanged_feed_resumes_the_same_competition(tmp_path, monkeypatch, unchanged):
    eng, db = restart(tmp_path, monkeypatch)
    first = eng.comp_id
    eng.stop()
    db.close()

    kwargs = dict(unchanged)
    if kwargs.pop("flow_dir_suffix", False):
        kwargs["flow_dir"] = str(tmp_path / "flow2")
    eng2, db2 = restart(tmp_path, monkeypatch, **kwargs)
    try:
        assert eng2.comp_id == first, f"{unchanged} 不改价格序列，不该另起一场竞赛"
    finally:
        eng2.stop()
        db2.close()


def test_the_feed_signature_carries_no_credentials(tmp_path):
    cfg = make_ctp_cfg(tmp_path, user_id="88888888", password="secret", password_env="CTP_PASSWORD")
    row = market_feed_signature(cfg.market_data, "ctp", cfg.symbols)
    blob = json.dumps(row, ensure_ascii=False, default=str)
    assert "secret" not in blob and "88888888" not in blob and "CTP_PASSWORD" not in blob
    assert set(row) & {"user_id", "user_id_env", "password", "password_env"} == set()


def test_the_signature_only_covers_symbols_actually_in_play(tmp_path):
    """"生效"而不是"配置里写了"：钉给一个没参赛的品种的合约不该掀掉竞赛。"""
    cfg = make_ctp_cfg(tmp_path, instruments={"RB0": "rb2601", "AP0": "AP601"})
    assert "AP0" not in cfg.symbols
    row = market_feed_signature(cfg.market_data, "ctp", cfg.symbols)
    assert row["instruments"] == {"RB0": "rb2601"}


def test_max_bars_is_compared_at_its_effective_value(tmp_path):
    """源会按 bar_seconds 反比把配小的 max_bars 抬上去，抬到同一处的是同一场实验。"""
    from arena.market.ctp import retention_floor

    floor = retention_floor(60)
    low = make_ctp_cfg(tmp_path, bar_seconds=60, max_bars=10)
    lower = make_ctp_cfg(tmp_path, bar_seconds=60, max_bars=11)
    assert 11 < floor, "这条用例依赖两个配置值都低于保留下限"
    assert (market_feed_signature(low.market_data, "ctp", low.symbols)
            == market_feed_signature(lower.market_data, "ctp", lower.symbols))
    assert market_feed_signature(low.market_data, "ctp", low.symbols)["max_bars"] == floor


def test_a_snapshot_without_a_market_section_still_resumes(tmp_path, monkeypatch):
    """旧快照（以及测试里直接写的空 config_json）没有行情段：拿不出证据就不掀竞赛。"""
    eng, db = restart(tmp_path, monkeypatch)
    try:
        assert eng._same_market_feed({"id": 1, "config_json": "{}"}) is True
        assert eng._same_market_feed({"id": 1, "config_json": None}) is True
        assert eng._same_market_feed({"id": 1, "config_json": "not json"}) is False
        snapshot = json.loads(eng._config_snapshot_json())
        assert eng._same_market_feed({"id": 1, "config_json": json.dumps(snapshot)}) is True
        snapshot["market_data"]["ctp"]["bar_seconds"] = 30
        assert eng._same_market_feed({"id": 1, "config_json": json.dumps(snapshot)}) is False
    finally:
        eng.stop()
        db.close()
