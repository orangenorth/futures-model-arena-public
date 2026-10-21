"""第三十五轮审查：回放位置写入失败期间，写入失败前发起的决策也不执行；重置时新竞赛与其回放位置/暂停标志同一事务写入，
写不进去则整体回滚、拒绝重置。"""
import json
import sqlite3
from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine, ResetBlocked
from arena.models import ActionRequest, Decision

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_engine(tmp_path):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


def break_cursor(db):
    real = db.kv_set

    def kv_set(key, value):
        if key.startswith("replay_pos:"):
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real(key, value)

    db.kv_set = kv_set  # type: ignore[method-assign]
    return real


class FailKvInsert:
    """包装事务连接：kv 表的写入失败（模拟锁死），其余语句照常。"""

    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        self.c = self.inner.__enter__()
        return self

    def __exit__(self, *a):
        return self.inner.__exit__(*a)

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("INSERT INTO KV"):
            raise sqlite3.OperationalError("database is locked (simulated)")
        return self.c.execute(sql, params)


def test_in_flight_decision_is_rejected_while_cursor_is_pending(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()                  # 决策在写入失败之前发起
    real = break_cursor(db)
    eng._tick()                                   # 这根 bar 结算了，但位置写不进去
    assert eng._cursor_pending
    d = Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("ok", d, snap, eng._generation)
    assert "RB0" not in eng.accounts["ok"].positions, "位置未持久化前不能执行在飞的决策"
    res = db.decisions(eng.comp_id, "ok", limit=1)[0]["results"]
    assert res and all(r["accepted"] is False and "回放位置" in r["reason"] for r in res)
    db.kv_set = real  # type: ignore[method-assign]
    eng._tick()                                   # 补写成功
    assert not eng._cursor_pending
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    eng.stop()


def test_reset_is_refused_when_the_new_competition_cannot_be_written(tmp_path):
    eng, db = make_engine(tmp_path)
    for _ in range(2):
        eng._tick()
    old, n, pos = eng.comp_id, len(db.list_competitions()), eng.source.position()
    real_txn = db._transaction
    db._transaction = lambda: FailKvInsert(real_txn())  # type: ignore[method-assign]
    with pytest.raises(ResetBlocked, match="重置失败"):
        eng.reset()
    db._transaction = real_txn  # type: ignore[method-assign]
    assert db.active_competition()["id"] == old and len(db.list_competitions()) == n, "整体回滚：旧竞赛仍在进行中，没有新竞赛"
    assert eng.comp_id == old and eng.source.position() == pos
    eng._tick()                                   # 旧竞赛照常继续
    assert eng.comp_id == old and json.loads(db.kv_get(f"replay_pos:{old}"))["steps"] == pos["steps"] + 1
    eng.stop()


def test_create_competition_rolls_back_when_the_seed_cannot_be_written(tmp_path):
    db = Database(str(tmp_path / "x.db"))
    cid = db.create_competition("a", "replay", 1.0, "{}")
    real_txn = db._transaction
    db._transaction = lambda: FailKvInsert(real_txn())  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        db.create_competition("b", "replay", 1.0, "{}", kv_prefixed={"replay_pos": "{}", "paused": "0"})
    db._transaction = real_txn  # type: ignore[method-assign]
    assert db.active_competition()["id"] == cid and len(db.list_competitions()) == 1, "旧竞赛未被结束，也没有新竞赛"
    assert db.kv_get(f"replay_pos:{cid + 1}") is None and db.kv_get(f"paused:{cid + 1}") is None
    cid2 = db.create_competition("b", "replay", 1.0, "{}", kv_prefixed={"replay_pos": "{}", "paused": "0"})
    assert db.kv_get(f"replay_pos:{cid2}") == "{}" and db.kv_get(f"paused:{cid2}") == "0"
    db.close()
