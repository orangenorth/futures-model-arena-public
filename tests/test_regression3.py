"""第三轮审查：权益接口使用竞赛起始资金；落库失败时内存账户不被污染。"""
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision
from arena.server.api import create_app

ROOT = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path, traders, initial_cash=1_000_000):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": initial_cash, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"breaker_failures": 2, "breaker_skip_rounds": 2, "max_retries": 0, "timeout_sec": 5},
        traders=traders,
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def test_equity_endpoint_uses_new_competition_initial_cash_after_config_change(tmp_path):
    traders = [TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)]
    cfg = make_cfg(tmp_path, traders, 1_000_000)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick(); eng._snapshot(); eng.stop(); db.close()
    cfg2 = make_cfg(tmp_path, traders, 500_000)
    db2 = Database(cfg2.db_path)
    eng2 = Engine(cfg2, db2, accelerate=True)
    client = TestClient(create_app(eng2, ROOT / "web"))
    assert client.get("/api/equity").json()["initial_cash"] == 500_000
    assert client.get("/api/state").json()["competition"]["initial_cash"] == 500_000
    eng2.stop()


class _FailOnce:
    def __init__(self, real):
        self.real, self.calls = real, 0

    def __call__(self, *a, **k):
        self.calls += 1
        if self.calls == 1:
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        return self.real(*a, **k)


class _GateTrader:
    mode = "mock"

    def __init__(self, trader_id: str):
        self.id = trader_id
        self.entered = threading.Event()
        self.release = threading.Event()

    def decide(self, snap, acc, rules):
        self.entered.set()
        assert self.release.wait(2), "test must release the in-flight decision"
        return Decision(self.id, snap.round_no, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])


def test_decision_persist_failure_leaves_memory_untouched(tmp_path):
    traders = [TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)]
    cfg = make_cfg(tmp_path, traders)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    failing = _FailOnce(db.save_account)
    db.save_account = failing  # type: ignore[method-assign]
    d = Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_safely("ok", d, snap, eng._generation)   # 第一次落库失败 → 记事件，不抛
    acc = eng.accounts["ok"]
    assert acc.positions == {} and acc.cash == 1_000_000, "落库失败后内存账户不应带有未提交的成交"
    assert db.trade_count(eng.comp_id, "ok") == 0 and db.decisions(eng.comp_id, "ok") == []
    # 第二次正常：成交与账户一致
    d2 = Decision("ok", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_safely("ok", d2, snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    assert db.trade_count(eng.comp_id, "ok") == 1
    assert set(db.load_accounts(eng.comp_id)["ok"].positions) == {"RB0"}
    eng.stop()


def test_tick_persist_failure_keeps_position_until_committed(tmp_path):
    traders = [TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)]
    cfg = make_cfg(tmp_path, traders)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick(); eng._round()
    pos = eng.accounts["ok"].positions["RB0"]
    # 把止损设到必然触发的位置
    pos.stop_loss = pos.last_price * 2
    db.save_account(eng.comp_id, eng.accounts["ok"])
    failing = _FailOnce(db.save_account)
    db.save_account = failing  # type: ignore[method-assign]
    eng._tick()   # 触发止损但落库失败 → 内存持仓保留
    assert "RB0" in eng.accounts["ok"].positions
    assert db.trade_count(eng.comp_id, "ok") == 1
    eng._tick()   # 再次触发并成功落库
    assert "RB0" not in eng.accounts["ok"].positions
    assert db.trade_count(eng.comp_id, "ok") == 2
    assert db.load_accounts(eng.comp_id)["ok"].positions == {}
    eng.stop()


def test_pause_blocks_new_rounds_and_discards_in_flight_decisions(tmp_path):
    traders = [TraderCfg(id="ok", name="OK", provider="rule")]
    cfg = make_cfg(tmp_path, traders)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    gate = _GateTrader("ok")
    eng.traders["ok"] = gate

    round_thread = threading.Thread(target=eng._round, daemon=True)
    round_thread.start()
    assert gate.entered.wait(1), "the decision should be in flight before pausing"
    eng.pause(True)
    gate.release.set()
    round_thread.join(timeout=2)
    assert not round_thread.is_alive()
    assert eng.accounts["ok"].positions == {}
    assert db.trade_count(eng.comp_id, "ok") == 0
    assert db.decisions(eng.comp_id, "ok") == []
    assert db.kv_get(f"paused:{eng.comp_id}") == "1"

    round_no = eng.round_no
    eng._round()
    assert eng.round_no == round_no and db.decisions(eng.comp_id, "ok") == []
    eng.stop()


def test_transaction_begin_failure_releases_writer_lock(tmp_path, monkeypatch):
    db = Database(tmp_path / "begin-failure.db")

    class _BeginFailure:
        def execute(self, sql):
            assert sql == "BEGIN IMMEDIATE"
            raise sqlite3.OperationalError("simulated begin failure")

    with monkeypatch.context() as patch:
        patch.setattr(db, "conn", lambda: _BeginFailure())
        with pytest.raises(sqlite3.OperationalError, match="begin failure"):
            with db._transaction():
                pass

    completed = threading.Event()

    def write_after_failure():
        db.add_event(None, "info", "test", "writer lock released")
        completed.set()

    worker = threading.Thread(target=write_after_failure, daemon=True)
    worker.start()
    assert completed.wait(1), "a failed BEGIN must not retain the writer lock"
    worker.join(timeout=1)
    db.close()


def test_transaction_commit_failure_rolls_back_and_releases_writer_lock(tmp_path, monkeypatch):
    db = Database(tmp_path / "commit-failure.db")

    class _CommitFailure:
        def __init__(self):
            self.commands = []

        def execute(self, sql):
            self.commands.append(sql)
            if sql == "COMMIT":
                raise sqlite3.OperationalError("simulated commit failure")

    fake = _CommitFailure()
    with monkeypatch.context() as patch:
        patch.setattr(db, "conn", lambda: fake)
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            with db._transaction():
                pass
    assert fake.commands == ["BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"]

    completed = threading.Event()
    worker = threading.Thread(target=lambda: (db.add_event(None, "info", "test", "writer lock released"), completed.set()), daemon=True)
    worker.start()
    assert completed.wait(1), "a failed COMMIT must not retain the writer lock"
    worker.join(timeout=1)
    db.close()
