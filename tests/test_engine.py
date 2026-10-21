import json
import threading

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import Decision
from arena.traders.base import Trader


def make_cfg(tmp_path, traders):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0", "I0", "AU0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800, "AU0": 560}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"breaker_failures": 2, "breaker_skip_rounds": 2, "max_retries": 0, "timeout_sec": 5},
        traders=traders,
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=__import__("pathlib").Path(__file__).resolve().parents[1],
    )


def run_rounds(engine: Engine, n: int):
    for _ in range(n):
        engine._tick()
        engine._round()
        engine._snapshot()


def test_rule_traders_trade_and_persist(tmp_path):
    traders = [TraderCfg(id="a", name="A", provider="rule", fallback_rule="trend"),
               TraderCfg(id="b", name="B", provider="rule", fallback_rule="meanrev"),
               TraderCfg(id="c", name="C", provider="rule", fallback_rule="breakout"),
               TraderCfg(id="d", name="D", provider="rule", fallback_rule="conservative")]
    cfg = make_cfg(tmp_path, traders)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    run_rounds(eng, 60)
    st = eng.state()
    assert st["competition"]["round_no"] == 60
    assert len(st["traders"]) == 4 and st["traders"][0]["rank"] == 1
    total_trades = sum(t.get("total_trades", 0) for t in st["traders"])
    assert total_trades > 0, "60 轮内应至少有一笔成交"
    assert len(db.decisions(eng.comp_id, limit=1000)) == 240
    # 每个账户独立：权益不完全相同
    eqs = {round(t["equity"]) for t in st["traders"]}
    assert len(eqs) > 1
    cid = eng.comp_id
    eng.stop()
    db.close()

    # 重启恢复
    db2 = Database(cfg.db_path)
    eng2 = Engine(cfg, db2, accelerate=True)
    assert eng2.comp_id == cid and eng2.round_no == 60
    for tid, acc in eng2.accounts.items():
        assert abs(acc.cash - eng.accounts[tid].cash) < 1e-6
        assert set(acc.positions) == set(eng.accounts[tid].positions)
    run_rounds(eng2, 5)
    assert eng2.round_no == 65
    eng2.stop()


def test_mock_llm_breaker_and_bad_json(tmp_path):
    good = json.dumps({"analysis": "买", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.2, "stop_loss": 1, "take_profit": 99999}]})
    traders = [TraderCfg(id="ok", name="OK", provider="mock", mock_response=good),
               TraderCfg(id="bad", name="BAD", provider="mock", mock_response="not json at all"),
               TraderCfg(id="boom", name="BOOM", provider="mock", mock_response="__raise__")]
    cfg = make_cfg(tmp_path, traders)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    run_rounds(eng, 5)
    ok_acc = eng.accounts["ok"]
    assert "RB0" in ok_acc.positions and ok_acc.positions["RB0"].side == "long"
    # 止损 1 会被风控接受（低于现价），size 0.2 → 只在第一轮成交，其后加仓受 max_notional_pct 限制
    ds_bad = db.decisions(eng.comp_id, "bad", limit=10)
    assert all(d["error"] for d in ds_bad)
    modes = [d["mode"] for d in db.decisions(eng.comp_id, "boom", limit=10)]
    assert "skipped" in modes, "连续失败应触发熔断"
    assert eng.state()["traders"]
    eng.stop()


class _GateTrader(Trader):
    mode = "mock"

    def __init__(self, trader_id, started, release=None):
        self.id = trader_id
        self.name = trader_id
        self.started = started
        self.release = release

    def decide(self, snap, acc, rules_text):
        self.started.set()
        if self.release is not None:
            assert self.release.wait(3), "test gate was not released"
        return Decision(trader_id=self.id, round_no=snap.round_no, ts=1.0, mode=self.mode, analysis="ok")


def test_round_persists_fast_decision_before_slower_prior_trader_finishes(tmp_path):
    cfg = make_cfg(tmp_path, [
        TraderCfg(id="slow", name="Slow", provider="mock"),
        TraderCfg(id="fast", name="Fast", provider="mock"),
    ])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    slow_started, slow_release, fast_finished = threading.Event(), threading.Event(), threading.Event()
    eng.traders["slow"] = _GateTrader("slow", slow_started, slow_release)
    eng.traders["fast"] = _GateTrader("fast", threading.Event())
    original_finish = eng._finish_safely

    def observe_finish(tid, *args):
        original_finish(tid, *args)
        if tid == "fast":
            fast_finished.set()

    eng._finish_safely = observe_finish
    eng._tick()
    runner = threading.Thread(target=eng._round)
    runner.start()
    try:
        assert slow_started.wait(1)
        assert fast_finished.wait(1), "a completed fast decision must not wait for an earlier slow trader"
        assert runner.is_alive()
        assert db.decisions(eng.comp_id, "fast", limit=1)
    finally:
        slow_release.set()
        runner.join(3)
        eng.stop()


def test_round_timeout_uses_the_largest_live_trader_retry_budget(tmp_path):
    cfg = make_cfg(tmp_path, [
        TraderCfg(
            id="slow", name="Slow", provider="openai_compatible",
            base_url="https://example.invalid/v1", model="slow", api_style="responses",
            api_key="test-key", max_retries=3,
        ),
    ])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        # timeout=5, responses requests=1, retries=3: 5*4 + (1+2+4) + 15 reserve.
        assert eng._decision_round_timeout_sec(["slow"]) == 42
    finally:
        eng.stop()
        db.close()


def test_default_chat_round_timeout_stays_inside_the_decision_interval(tmp_path):
    # Mirror the public template: timeout_sec=60, max_retries=2, chat + json_mode,
    # decision_interval_sec=300.  The round deadline must stay under the interval,
    # otherwise a timed-out round would overlap the next one.
    cfg = make_cfg(tmp_path, [
        TraderCfg(
            id="chat", name="Chat", provider="openai_compatible",
            base_url="https://example.invalid/v1", model="chat", api_style="chat",
            api_key="test-key",
        ),
    ])
    cfg.competition.decision_interval_sec = 300
    cfg.llm.timeout_sec = 60
    cfg.llm.max_retries = 2
    assert cfg.llm.json_mode
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        # 4 requests * 60s + (1+2)s backoff + 15s reserve.
        assert eng._decision_round_timeout_sec(["chat"]) == 258
        assert eng._decision_round_timeout_sec(["chat"]) < cfg.competition.decision_interval_sec
    finally:
        eng.stop()
        db.close()


def test_trader_roster_change_starts_a_fresh_competition(tmp_path):
    original_cfg = make_cfg(tmp_path, [
        TraderCfg(id="deepseek", name="DeepSeek", provider="openai_compatible", base_url="https://example.invalid/v1", model="deepseek", api_key="test-key"),
        TraderCfg(id="codex", name="Codex", provider="openai_compatible", base_url="https://example.invalid/v1", model="codex", api_key="test-key"),
    ])
    db = Database(original_cfg.db_path)
    original = Engine(original_cfg, db, accelerate=True)
    original_id = original.comp_id
    original.stop()

    kimi_cfg = make_cfg(tmp_path, [
        TraderCfg(id="deepseek", name="DeepSeek", provider="openai_compatible", base_url="https://example.invalid/v1", model="deepseek", api_key="test-key"),
        TraderCfg(id="kimi", name="Kimi K3", provider="openai_compatible", base_url="https://example.invalid/v1", model="kimi-k3", api_key="test-key"),
    ])
    replacement = Engine(kimi_cfg, db, accelerate=True)
    try:
        assert replacement.comp_id != original_id
        assert set(replacement.accounts) == {"deepseek", "kimi"}
        assert {account.initial_cash for account in replacement.accounts.values()} == {1_000_000}
    finally:
        replacement.stop()


def test_live_model_change_with_same_trader_id_starts_a_fresh_competition(tmp_path):
    original_cfg = make_cfg(tmp_path, [
        TraderCfg(
            id="kimi",
            name="Kimi K3",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            model="kimi-k3",
            api_key="test-key",
        ),
    ])
    db = Database(original_cfg.db_path)
    original = Engine(original_cfg, db, accelerate=True)
    original_id = original.comp_id
    original.stop()

    changed_cfg = make_cfg(tmp_path, [
        TraderCfg(
            id="kimi",
            name="Kimi K3",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            model="kimi-k3-next",
            api_key="test-key",
        ),
    ])
    changed = Engine(changed_cfg, db, accelerate=True)
    try:
        assert changed.comp_id != original_id
        assert set(changed.accounts) == {"kimi"}
    finally:
        changed.stop()


def test_live_temperature_change_with_same_trader_id_starts_a_fresh_competition(tmp_path):
    original_cfg = make_cfg(tmp_path, [
        TraderCfg(
            id="kimi",
            name="Kimi K3",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            model="kimi-k3",
            api_key="test-key",
        ),
    ])
    db = Database(original_cfg.db_path)
    original = Engine(original_cfg, db, accelerate=True)
    original_id = original.comp_id
    original.stop()

    changed_cfg = make_cfg(tmp_path, [
        TraderCfg(
            id="kimi",
            name="Kimi K3",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            model="kimi-k3",
            temperature=1.0,
            api_key="test-key",
        ),
    ])
    changed = Engine(changed_cfg, db, accelerate=True)
    try:
        assert changed.comp_id != original_id
        assert set(changed.accounts) == {"kimi"}
    finally:
        changed.stop()
