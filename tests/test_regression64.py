"""Regression 64：全品种放开后暴露的三条——执行闸门的口径、批量键的规范化、冷启动请求突发。

三条都是前一轮修复没做干净的地方：
- 决策轮已经把取不到行情的品种移出本轮，执行闸门却还在按**全部开市品种**判定，
  于是有效决策在出口处被整批拒掉——上一轮的端到端用例只断言"产生了决策"，
  没断言"动作被接受"，所以它照样通过。
- 批量报价按大写后的代码回键，调用方用原键（小写配置）取不到。
- 分档缓存只能让后续轮次少发请求；进程刚启动缓存是空的，第一轮仍是每品种三次历史请求，
  而且派生门槛按 limit 去卡时，在新浪真实的返回上限下永远派生不出 1h。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_cn_contracts
from arena.market.sina_cn import SinaFuturesSource
from arena.models import Bar, Quote

ROOT = Path(__file__).resolve().parents[1]
SYMS = ["RB0", "I0", "AU0"]
ROW = [["2026-09-16 09:05:00", "3400", "3410", "3390", "3405", "120", "100000"]]


def make_sina(symbols, **kw):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return SinaFuturesSource(symbols, specs, holidays, **kw)


# ---------- 1) 执行闸门按本轮品种判定 ----------

class OpenRb:
    """固定对 RB0 开多的交易员：用来断言动作真的被**接受**，而不是只产生了一条决策记录。"""
    mode = "rule"
    id = "ok"

    def decide(self, snap, view, rules):
        from arena.models import ActionRequest, Decision
        return Decision(trader_id="ok", round_no=snap.round_no, ts=time.time(), mode="rule",
                        analysis="x",
                        actions=[ActionRequest(symbol="RB0", action="open_long", size_pct=0.1,
                                               stop_loss=3000.0)])


class PartialFeed:
    """只给部分品种报价、K 线全给：模拟停滞合约"开市但没有新 bar"。"""
    name = "partial"

    def __init__(self, specs, quoted):
        self._specs = specs
        self.quoted = set(quoted)

    def specs(self):
        return self._specs

    def latest(self, symbols):
        now = time.time()
        return {s: Quote(symbol=s, price=3400.0, ts=now, change_pct=0.0)
                for s in symbols if s in self.quoted}

    def klines(self, symbol, period, limit=120):
        now = time.time()
        step = {"5m": 300, "15m": 900, "1h": 3600}.get(period, 300)
        return [Bar(ts=now - (limit - i) * step, open=3400, high=3410, low=3390,
                    close=3400 + (i % 3), volume=100, hold=1000) for i in range(limit)]

    def is_open(self, now, symbol=None):
        return True

    def next_open(self, now):
        return now

    def health(self):
        return {"source": self.name, "ok": True, "last_ok": time.time()}

    def close(self):
        pass


@pytest.fixture
def engine(tmp_path):
    cfg = AppConfig(
        competition={"name": "r64", "market": "cn_futures", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1,
                     "timezone": "Asia/Shanghai"},
        symbols=list(SYMS),
        market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "require_stop_loss": True},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="rule", fallback_rule="trend")],
        storage={"db_path": str(tmp_path / "r64.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=False)
    yield eng, db
    eng.stop()
    db.close()


def test_a_round_narrowed_by_a_dead_symbol_still_executes_its_actions(engine):
    """AU0 始终没有行情时，RB0 上的开仓必须**被接受并成交**。

    还原成按全部开市品种判定后本用例失败：动作全部 accepted=False，理由是"行情不新鲜"。
    上一轮我只断言了"有决策记录"，那条断言在两种实现下都通过——等于没测到。
    """
    eng, db = engine
    eng.source = PartialFeed(eng.specs, {"RB0", "I0"})
    eng.traders["ok"] = OpenRb()
    eng._tick()                                  # 取行情：AU0 没有报价
    eng._round(eng._tradable_now(SYMS)[0])

    decisions = db.decisions(eng.comp_id, limit=10)
    assert decisions, "本轮应当产生决策"
    results = decisions[0]["results"]
    assert results, "决策里没有任何动作结果"
    assert all(r["accepted"] for r in results), f"动作被拒：{[r.get('reason') for r in results]}"
    assert db.trades(eng.comp_id, limit=10), "动作被接受却没有成交"


def test_a_stale_symbol_inside_the_round_still_blocks_execution(engine):
    """本轮**确实参与**的品种在模型返回期间变陈旧时，仍然必须拒绝执行。

    这条直接打执行闸门：快照用新鲜行情建好、模型也返回了，然后报价才过期——
    正是闸门存在的理由。改窄口径不能把它一起改没了。
    """
    eng, db = engine
    eng.source = PartialFeed(eng.specs, {"RB0", "I0"})
    eng._tick()
    snap = eng._build_snapshot(symbols=eng._tradable_now(SYMS)[0])
    assert snap is not None and "RB0" in snap.symbols

    decision = OpenRb().decide(snap, None, "")
    for symbol in list(eng.quotes):              # 模型返回期间行情停了
        q = eng.quotes[symbol]
        eng.quotes[symbol] = Quote(symbol=symbol, price=q.price, ts=q.ts - 7200, change_pct=0.0)
    eng._finish_decision("ok", decision, snap, eng._generation)

    decisions = db.decisions(eng.comp_id, limit=10)
    assert decisions and decisions[0]["results"]
    assert all(not r["accepted"] for r in decisions[0]["results"])
    assert "行情不新鲜" in (decisions[0]["results"][0]["reason"] or "")
    assert not db.trades(eng.comp_id, limit=10)


# ---------- 2) 批量报价的键 ----------

def test_batched_quotes_are_keyed_by_the_requested_symbol():
    """配置里写成小写时，按大写回键会让该品种永远没有报价。

    还原成按规范化后的代码回键后本用例失败。
    """
    src = make_sina(["rb0"])
    text = ('var hq_str_nf_RB0="螺纹钢连续,090501,3400,3410,3390,0,0,0,3405,0,'
            '3395,0,0,2200000,1500000,0,0,20260916";')

    class Resp:
        content = text.encode("gb18030")

        @staticmethod
        def raise_for_status():
            return None

    src.client.get = lambda url, **kw: Resp()  # type: ignore[method-assign]
    try:
        quotes = src.latest(["rb0"])
        assert set(quotes) == {"rb0"}, f"按原键取不到报价：{list(quotes)}"
        assert quotes["rb0"].price == 3405.0
    finally:
        src.close()


def test_mixed_case_symbols_resolve_to_the_same_contract():
    src = make_sina(["Rb0"])
    urls = []
    text = ('var hq_str_nf_RB0="螺纹钢连续,090501,3400,3410,3390,0,0,0,3405,0,'
            '3395,0,0,2200000,1500000,0,0,20260916";')

    class Resp:
        content = text.encode("gb18030")

        @staticmethod
        def raise_for_status():
            return None

    def fake_get(url, **kw):
        urls.append(url)
        return Resp()

    src.client.get = fake_get  # type: ignore[method-assign]
    try:
        assert src.latest(["Rb0"])["Rb0"].price == 3405.0
        assert urls[0].endswith("list=nf_RB0"), urls   # 查询仍用规范化后的代码
    finally:
        src.close()


# ---------- 3) 冷启动的历史请求数 ----------

SINA_HISTORY_CAP = 1023   # getFewMinLine 单次返回的上限：测试必须用它，编个够大的数字等于没测


def minute_rows(n: int):
    base = 1_700_000_000
    return [[time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i * 300)),
             "3400", "3410", "3390", str(3400 + i % 7), "10", "1000"] for i in range(n)]


def test_a_cold_snapshot_costs_one_history_request_per_symbol():
    """缓存为空时，5m/15m/1h 三个周期只该发一次请求——慢周期由 5m 重采样。

    还原成每个周期各自请求后本用例失败（会数到 3 次）。
    """
    src = make_sina(["RB0"])
    types = []

    def fake_get(method, params, attempts=3):
        types.append(params.get("type"))
        return minute_rows(SINA_HISTORY_CAP)

    src._get = fake_get  # type: ignore[method-assign]
    try:
        b5 = src.klines("RB0", "5m", 120)
        b15 = src.klines("RB0", "15m", 100)
        b1h = src.klines("RB0", "1h", 100)
        assert types == ["5"], f"冷启动发了 {len(types)} 次历史请求：{types}"
        assert len(b5) == 120 and len(b15) == 100
        # 1023 根 5m 只能派生出 ~85 根 1h。按 limit=100 去卡就永远派生不出来、
        # 每个品种照旧多发一次请求；85 根足够 ema60/macd 全部有值。
        assert 60 <= len(b1h) < 100, f"1h 派生出 {len(b1h)} 根"
        from arena.market.indicators import summarize
        ind = summarize(b1h)
        assert ind["ema60"] is not None and ind["macd_hist"] is not None
    finally:
        src.close()


def test_a_full_universe_costs_one_cold_request_per_symbol():
    """整套配置冷启动的量级：一个品种一次，而不是一个品种三次。

    不写死 71：合约表在不同分支上品种数不同，这里要锁的是"每品种一次"这条不变量。
    """
    import json
    table = json.loads((ROOT / "config" / "contracts_cn.json").read_text(encoding="utf-8"))
    names = sorted(table["contracts"])
    src = make_sina(names)
    calls = []

    def fake_get(method, params, attempts=3):
        calls.append(params.get("symbol"))
        return minute_rows(SINA_HISTORY_CAP)

    src._get = fake_get  # type: ignore[method-assign]
    try:
        for name in names:
            src.klines(name, "5m", 120)
            src.klines(name, "15m", 100)
            src.klines(name, "1h", 100)
        assert len(calls) == len(names), (
            f"{len(names)} 个品种冷启动共发了 {len(calls)} 次历史请求，应当是 {len(names)} 次")
    finally:
        src.close()


def test_a_short_series_falls_back_to_a_direct_request():
    """5m 根数不够时派生出来的序列会明显变短、指标失真，必须退回直接请求该周期。"""
    src = make_sina(["RB0"])
    types = []

    def fake_get(method, params, attempts=3):
        types.append(params.get("type"))
        return minute_rows(30)          # 连 60 根 1h 都派生不出来

    src._get = fake_get  # type: ignore[method-assign]
    try:
        src.klines("RB0", "5m", 120)
        src.klines("RB0", "1h", 100)
        assert types == ["5", "60"], f"未退回直接请求：{types}"
    finally:
        src.close()


def test_deriving_never_fetches_the_fine_series_on_its_own():
    """单独请求 1h 时不该为了尝试派生先去取一次 5m——那会变成两次请求。"""
    src = make_sina(["RB0"])
    types = []

    def fake_get(method, params, attempts=3):
        types.append(params.get("type"))
        return minute_rows(SINA_HISTORY_CAP)

    src._get = fake_get  # type: ignore[method-assign]
    try:
        src.klines("RB0", "1h", 100)
        assert types == ["60"], f"为派生额外取了 5m：{types}"
    finally:
        src.close()
