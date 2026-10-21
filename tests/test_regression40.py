"""第四十轮审查：无成交盯市更新落库失败后，在飞的决策平掉持仓并落库时重放不会永远保留该 bar；
重置前先补写失败的盯市更新，写不进去则拒绝重置。"""
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


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


def durable_steps(db, cid):
    return json.loads(db.kv_get(f"replay_pos:{cid}"))["steps"]


def fail_one_mark(eng, db):
    """开多 RB0，随后一个 tick 的无成交盯市更新落库失败 → bar 被保留。返回真正的 save_account。"""
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng._tick()
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()
    assert "ok" in eng._mark_save_failed and eng._replay_retry is not None
    return real_save, snap


def test_in_flight_close_is_rejected_and_the_bar_is_released_after_replay(tmp_path):
    eng, db = make_engine(tmp_path)
    real_save, snap = fail_one_mark(eng, db)
    steps = eng.source.position()["steps"]
    db.save_account = real_save  # type: ignore[method-assign]
    # 写入失败前发起的决策此时返回想平仓：bar 被保留期间只记零成交的拒绝审计，持仓不动（否则成交会记在持久化位置之后的 bar 上）
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=[ActionRequest("RB0", "close")]), snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    assert all(r["accepted"] is False and "回放 bar" in r["reason"] for r in db.decisions(eng.comp_id, "ok", limit=1)[0]["results"])
    eng._tick()                                   # 重放保留的 bar：盯市更新写入成功 → 解除保留，位置前进
    assert not eng._mark_save_failed and eng._replay_retry is None
    assert durable_steps(db, eng.comp_id) == steps == eng.source.position()["steps"]
    eng._finish_decision("ok", Decision("ok", 3, 0, actions=[ActionRequest("RB0", "close")]), snap, eng._generation)
    assert not eng.accounts["ok"].positions, "放行后平仓正常执行"
    eng._tick()
    assert eng.source.position()["steps"] == steps + 1 == durable_steps(db, eng.comp_id), "回放继续前进"
    eng.stop()


def test_reset_writes_failed_mark_updates_first_or_refuses(tmp_path):
    eng, db = make_engine(tmp_path)
    real_save, _ = fail_one_mark(eng, db)
    old_cid, steps = eng.comp_id, eng.source.position()["steps"]
    with pytest.raises(ResetBlocked, match="盯市更新"):
        eng.reset()                               # 数据库仍不可写：拒绝
    assert db.active_competition()["id"] == old_cid and "ok" in eng._mark_save_failed
    db.save_account = real_save  # type: ignore[method-assign]
    eng.reset()                                   # 数据库恢复：先补写保留 bar 的最新价/权益峰值，再归档
    old = db.load_accounts(old_cid)["ok"]
    assert old.positions["RB0"].last_price == eng.prices["RB0"], "旧竞赛归档时的最新价是保留那根 bar 的价格"
    assert not eng._mark_save_failed and eng._replay_retry is None and eng.comp_id != old_cid
    assert durable_steps(db, eng.comp_id) == steps == eng.source.position()["steps"], "新竞赛从这根 bar 之后开始，不跳过它"
    eng.stop()
