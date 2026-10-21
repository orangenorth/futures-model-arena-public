"""Regression 62: 全品种配置下，个别品种取不到行情不能让整轮决策停摆。

合约表里有长期几乎无成交的品种（线材、纤维板、胶合板、部分郑商所小品种）：交易所时段表说
它们在开市，行情源却可能一直给不出新的分钟 bar。原来 `_stale_quotes_reason()` 是全有或全无——
只要一个这样的品种在列，整轮就不决策，默认安装会永远停在"行情缺失…暂不决策"，一次模型都不调。
合成回放行情给每个品种都造 bar，所以沙箱验证暴露不出这条。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from arena.config import AppConfig, TraderCfg, load_config
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import Bar, Quote

ROOT = Path(__file__).resolve().parents[1]
SYMS = ["RB0", "I0", "AU0"]


def make_cfg(tmp_path):
    return AppConfig(
        competition={"name": "r62", "market": "cn_futures", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1,
                     "timezone": "Asia/Shanghai"},
        symbols=list(SYMS),
        market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="rule", fallback_rule="trend")],
        storage={"db_path": str(tmp_path / "r62.db")},
        base_dir=ROOT,
    )


@pytest.fixture
def engine(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=False)
    yield eng, db
    eng.stop()
    db.close()


def fresh(symbol, price=3400.0):
    return Quote(symbol=symbol, price=price, ts=time.time(), change_pct=0.0)


def test_a_symbol_without_quotes_does_not_block_the_whole_round(engine):
    """只有 AU0 取不到行情时，RB0 与 I0 照常决策。

    还原成全有或全无后本用例失败：整轮被 AU0 挡下，`tradable` 为空。
    """
    eng, _ = engine
    eng.quotes = {"RB0": fresh("RB0"), "I0": fresh("I0", 800.0)}   # AU0 一直没有报价
    tradable, stale = eng._tradable_now(SYMS)
    assert stale is None, stale
    assert tradable == ["RB0", "I0"]


def test_a_stale_quote_only_removes_that_symbol(engine):
    eng, _ = engine
    old = Quote(symbol="AU0", price=560.0, ts=time.time() - 3600, change_pct=0.0)
    eng.quotes = {"RB0": fresh("RB0"), "I0": fresh("I0", 800.0), "AU0": old}
    tradable, stale = eng._tradable_now(SYMS)
    assert stale is None and tradable == ["RB0", "I0"]


def test_an_unpriceable_symbol_with_an_open_position_still_stops_the_round(engine):
    """持仓品种取不到行情时必须整轮停下：权益与平仓/改止损都会建立在陈旧价格上。"""
    eng, _ = engine
    acc = eng.accounts["ok"]
    eng.prices.update({"RB0": 3400.0, "I0": 800.0, "AU0": 560.0})
    from arena.models import Position
    acc.positions["AU0"] = Position(trader_id="ok", symbol="AU0", side="long", qty=1, avg_price=560.0,
                                    margin=10000.0, opened_at=time.time(), last_price=560.0)
    eng.quotes = {"RB0": fresh("RB0"), "I0": fresh("I0", 800.0)}   # 持仓的 AU0 无报价
    tradable, stale = eng._tradable_now(SYMS)
    assert tradable == []
    assert stale is not None and "持仓" in stale and "AU0" in stale


def test_all_symbols_unpriceable_still_stops_the_round(engine):
    eng, _ = engine
    eng.quotes = {}
    tradable, stale = eng._tradable_now(SYMS)
    assert tradable == [] and stale is not None and "暂不决策" in stale


def test_replay_is_unaffected_because_it_has_no_wall_clock_freshness(tmp_path):
    """回放的时间戳属于它自己的时钟，不适用墙钟新鲜度——取舍逻辑必须原样放行。"""
    cfg = AppConfig(
        competition={"name": "r62r", "market": "replay", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(SYMS),
        market_data={"replay": {"seed": 5, "start_price": {s: 1000 for s in SYMS}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="rule", fallback_rule="trend")],
        storage={"db_path": str(tmp_path / "r62r.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        eng.quotes = {}
        assert eng._tradable_now(SYMS) == (SYMS, None)
    finally:
        eng.stop()
        db.close()


def test_the_message_still_names_the_offending_symbols(engine):
    """把品种移出本轮不等于悄悄吞掉：整轮停下时文案仍要点名，不能只说"行情缺失"。"""
    eng, _ = engine
    eng.quotes = {}
    reason = eng._stale_quotes_reason(symbols=SYMS)
    assert reason is not None and "RB0" in reason and "无报价" in reason


# ---------- 默认配置 ----------

def test_the_default_symbol_set_excludes_dormant_contracts():
    """合约表保留全部 82 个品种，但默认参赛名单不含长期无成交的那 11 个。

    它们在时段表里"开市"却可能一直没有新 bar，白占 prompt 体积；需要时用户自行加回。
    """
    cfg = load_config(ROOT / "config" / "config.example.yaml", base_dir=ROOT)
    dormant = {"WR0", "FB0", "BB0", "RR0", "RS0", "ZC0", "WH0", "PM0", "JR0", "RI0", "LR0"}
    symbols = set(cfg.symbols)
    assert not (symbols & dormant), f"默认名单里仍有停滞品种：{sorted(symbols & dormant)}"
    assert len(symbols) == 71
    # 但它们仍在合约表里，加回去就能用
    import json
    specs = json.load((ROOT / "config" / "contracts_cn.json").open(encoding="utf-8"))["contracts"]
    assert dormant <= set(specs) and len(specs) == 82


# ---------- 端到端：部分品种有行情时整轮照常进行 ----------

class PartialFeed:
    """假行情源：只给部分品种报价，K 线全给——模拟停滞合约"开市但没有新 bar"。"""
    name = "partial"

    def __init__(self, specs, quoted):
        self._specs = specs
        self.quoted = set(quoted)

    def specs(self):
        return self._specs

    def latest(self, symbols):
        return {s: fresh(s, 3400.0) for s in symbols if s in self.quoted}

    def klines(self, symbol, period, limit=120):
        now = time.time()
        step = {"5m": 300, "15m": 900, "1h": 3600}.get(period, 300)
        return [Bar(ts=now - (limit - i) * step, open=3400, high=3410, low=3390,
                    close=3400 + (i % 5), volume=100, hold=1000) for i in range(limit)]

    def is_open(self, now, symbol=None):
        return True

    def next_open(self, now):
        return now

    def health(self):
        return {"source": self.name, "ok": True, "last_ok": time.time()}

    def close(self):
        pass


def test_a_round_runs_on_the_symbols_that_do_have_quotes(engine):
    """AU0 始终没有报价时，本轮仍然进行，且只对 RB0 / I0 决策。

    还原成全有或全无后本用例失败：整轮被跳过，一条决策都不会落库。
    """
    eng, db = engine
    eng.source = PartialFeed(eng.specs, {"RB0", "I0"})
    eng.quotes = {"RB0": fresh("RB0"), "I0": fresh("I0", 800.0)}
    eng.prices.update({"RB0": 3400.0, "I0": 800.0})

    eng._round(list(SYMS))

    decisions = db.decisions(eng.comp_id, limit=100)
    assert decisions, "本轮应当产生决策，而不是整轮跳过"
    assert eng.round_no >= 1
