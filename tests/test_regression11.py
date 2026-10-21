"""Regression tests for concurrent rounds, frozen replay snapshots, and per-symbol admission."""
import json
import threading
import time
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision, Quote

ROOT = Path(__file__).resolve().parents[1]
HOLD = json.dumps({"analysis": "hold", "actions": []})
OPEN_BOTH = json.dumps({"analysis": "x", "actions": [
    {"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1},
    {"symbol": "AU0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1},
]})


def make_replay_engine(tmp_path):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="slow", name="SLOW", provider="mock", mock_response=HOLD)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


def make_symbol_engine(tmp_path, accelerate):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0", "AU0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "AU0": 560}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_BOTH)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=accelerate), db


class GateTrader:
    id = "slow"
    name = "SLOW"
    mode = "mock"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def decide(self, snap, account, rules):
        assert account.positions, "the stale decision must observe the pre-stop position"
        self.entered.set()
        assert self.release.wait(5), "test gate was not released"
        price = snap.symbols["RB0"].quote.price
        return Decision(
            trader_id=self.id,
            round_no=snap.round_no,
            ts=time.time(),
            mode=self.mode,
            actions=[ActionRequest(symbol="RB0", action="open_long", size_pct=0.1, stop_loss=price * 0.9)],
        )


def test_risk_close_invalidates_inflight_decision(tmp_path):
    eng, db = make_replay_engine(tmp_path)
    gate = GateTrader()
    eng.traders["slow"] = gate
    try:
        eng._tick()
        acc = eng.accounts["slow"]
        px = eng.prices["RB0"]
        opened = eng.exchange.open(acc, eng.specs["RB0"], "long", 1, px, 1.0, px * 2, None, eng.prices, 0)
        assert opened.accepted and opened.trade
        db.save_account(eng.comp_id, acc, trades=[opened.trade])

        assert eng._start_round_thread()
        assert gate.entered.wait(3), "round never reached the model gate"

        eng._tick()  # The stop above market triggers a durable risk close while the model is waiting.
        assert db.trade_count(eng.comp_id, "slow") == 2
        assert eng.accounts["slow"].positions == {}

        gate.release.set()
        assert eng._round_thread is not None
        eng._round_thread.join(timeout=3)
        assert not eng._round_thread.is_alive()

        assert db.trade_count(eng.comp_id, "slow") == 2, "stale add-on must not reopen after the risk close"
        assert eng.accounts["slow"].positions == {}
        assert db.decisions(eng.comp_id, "slow") == []
    finally:
        gate.release.set()
        eng.stop()


def test_replay_round_uses_a_frozen_quote_and_bar_revision(tmp_path):
    eng, _ = make_replay_engine(tmp_path)
    try:
        eng._tick()
        real_build = eng._build_snapshot
        captured = {}

        def build_after_future_ticks(quotes, round_no, replay_bars=None, symbols=None):
            captured["quote_ts"] = quotes["RB0"].ts
            for _ in range(5):
                eng._tick()  # Simulate ticks racing with snapshot construction.
            snap = real_build(quotes, round_no, replay_bars, symbols)
            captured["snapshot"] = snap
            return snap

        eng._build_snapshot = build_after_future_ticks  # type: ignore[method-assign]
        eng._round()

        snap = captured["snapshot"]
        view = snap.symbols["RB0"]
        assert eng.quotes["RB0"].ts > view.quote.ts
        assert max(bar.ts for bar in view.bars_5m) <= view.quote.ts
        assert view.quote.ts == captured["quote_ts"]
    finally:
        eng.stop()


def test_closed_symbols_are_excluded_by_active_round_subset(tmp_path):
    eng, db = make_symbol_engine(tmp_path, accelerate=False)
    eng._tick()
    replay = eng.source

    class LiveMixedSessionSource:
        def is_open(self, now, symbol=None):
            return symbol != "RB0"

        def klines(self, symbol, period, limit=120):
            return replay.klines(symbol, period, limit)

    eng.source = LiveMixedSessionSource()
    now = time.time()
    eng.quotes = {"RB0": Quote("RB0", 3400.0, ts=now - 30), "AU0": Quote("AU0", 560.0, ts=now - 30)}
    active = eng._active_symbols()
    assert active == ["AU0"]
    assert eng._stale_quotes_reason(eng.quotes, active) is None
    snap = eng._build_snapshot(dict(eng.quotes), 1, symbols=active)
    assert set(snap.symbols) == {"AU0"}, "收盘品种不得进入快照"
    # A model action against RB0 is rejected while the AU0 action can still trade.
    d = Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1), ActionRequest("AU0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("ok", d, snap, eng._generation)
    res = {r["symbol"]: r for r in db.decisions(eng.comp_id, "ok", limit=1)[0]["results"]}
    assert res["RB0"]["accepted"] is False and "休市" in res["RB0"]["reason"]
    assert res["AU0"]["accepted"] is True
    assert set(eng.accounts["ok"].positions) == {"AU0"}
    # Once AU0 is stale too, no symbol may start a decision round.
    eng.quotes["AU0"] = Quote("AU0", 560.0, ts=now - 3600)
    assert "AU0" in (eng._stale_quotes_reason(eng.quotes, active) or "")
    eng.stop()


def test_replay_source_is_not_subject_to_wall_clock_freshness(tmp_path):
    eng, _ = make_symbol_engine(tmp_path, accelerate=False)  # Replay must work without acceleration too.
    eng._tick()
    old = time.time() - 86400 * 30
    eng.quotes = {"RB0": Quote("RB0", 3400.0, ts=old), "AU0": Quote("AU0", 560.0, ts=old)}
    assert eng._requires_live_quote_freshness() is False
    assert eng._stale_quotes_reason() is None
    snap = eng._build_snapshot(dict(eng.quotes), 1)
    assert set(snap.symbols) == {"RB0", "AU0"}
    eng.stop()
