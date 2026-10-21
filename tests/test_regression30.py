"""第三十轮审查：回放位置写不进数据库时，回放不前进、主循环不开新一轮，每个 tick 与关闭时重试；
持久化的位置最多落后一根已结算的 bar，重启重放它也不会重复成交。"""
import json
import sqlite3
import time
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
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


def durable_steps(db, cid):
    raw = db.kv_get(f"replay_pos:{cid}")
    return json.loads(raw)["steps"] if raw else None


def break_cursor(db):
    """只让回放位置写不进去（其他 kv、账户落库照常）。返回原函数。"""
    real = db.kv_set

    def kv_set(key, value):
        if key.startswith("replay_pos:"):
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real(key, value)

    db.kv_set = kv_set  # type: ignore[method-assign]
    return real


def test_replay_does_not_advance_until_cursor_is_durable(tmp_path):
    eng, db = make_engine(tmp_path)
    for _ in range(3):
        eng._tick()
    steps = eng.source.position()["steps"]
    assert durable_steps(db, eng.comp_id) == steps
    real = break_cursor(db)
    eng._tick()                                   # 取到新 bar 并结算，但位置写不进去
    assert eng.source.position()["steps"] == steps + 1 and eng._cursor_pending
    assert durable_steps(db, eng.comp_id) == steps, "写入失败：持久化位置停在上一根"
    for _ in range(3):
        eng._tick()                               # 写入仍失败：不取下一根 bar
    assert eng.source.position()["steps"] == steps + 1, "位置未持久化期间回放不得前进"
    assert eng._cursor_pending and durable_steps(db, eng.comp_id) == steps
    assert any("回放位置" in e["message"] for e in db.events(eng.comp_id, 20))
    db.kv_set = real  # type: ignore[method-assign]
    eng._tick()                                   # 先补写位置，本 tick 不取新 bar
    assert not eng._cursor_pending and durable_steps(db, eng.comp_id) == steps + 1 == eng.source.position()["steps"]
    eng._tick()                                   # 之后正常前进
    assert durable_steps(db, eng.comp_id) == steps + 2 == eng.source.position()["steps"]
    eng.stop()


def test_main_loop_does_not_start_rounds_while_cursor_is_pending(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    real = break_cursor(db)
    eng._tick()
    assert eng._cursor_pending
    eng.start()
    time.sleep(2.5)
    assert eng.round_no == 0, "回放位置未持久化期间不得开始新一轮决策"
    assert "回放位置" in eng.status_text
    db.kv_set = real  # type: ignore[method-assign]
    deadline = time.time() + 5
    while time.time() < deadline and eng.round_no == 0:
        time.sleep(0.2)
    assert eng.round_no >= 1 and not eng._cursor_pending
    eng.stop()
    assert durable_steps(db, eng.comp_id) == eng.source.position()["steps"], "停止后持久化位置与内存位置一致"


def test_restart_replays_at_most_one_settled_bar_without_duplicate_fills(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.accounts["ok"].positions["RB0"].stop_loss = 1e9    # 下一根必触发
    db.save_account(eng.comp_id, eng.accounts["ok"])       # 持久化必触发的止损，重启后仍成立
    steps = eng.source.position()["steps"]
    real = break_cursor(db)
    eng._tick()                                   # 止损成交落库成功，但回放位置写不进去
    assert "RB0" not in eng.accounts["ok"].positions and eng._cursor_pending
    assert durable_steps(db, eng.comp_id) == steps and eng.source.position()["steps"] == steps + 1
    eng.stop()                                    # 关闭前重试仍失败
    assert durable_steps(db, eng.comp_id) == steps
    db.close()
    # 重启：从 steps 衔接，重放已结算的那根 bar；持仓早已平掉，不会再成交一次
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(eng.cfg, db2, accelerate=True)
    assert eng2.source.position()["steps"] == steps
    eng2._tick()
    assert eng2.source.position()["steps"] == steps + 1 == durable_steps(db2, eng2.comp_id)
    assert sum(t["action"] == "stop_loss" for t in db2.trades(eng2.comp_id, "ok", limit=10)) == 1, "不得重复成交"
    eng2.stop()
    db2.close()


def test_reset_seeds_the_new_competition_cursor_atomically(tmp_path):
    eng, db = make_engine(tmp_path)
    for _ in range(4):
        eng._tick()
    pos = eng.source.position()
    real = break_cursor(db)                       # 单独的 kv 写入失败也无妨：位置与新竞赛在同一事务里写入
    eng.reset()
    cid = eng.comp_id
    assert json.loads(db.kv_get(f"replay_pos:{cid}")) == pos and db.kv_get(f"paused:{cid}") == "0"
    assert not eng._cursor_pending and eng.source.position() == pos, "重置后不能把已在的位置再快进一次"
    db.kv_set = real  # type: ignore[method-assign]
    eng._tick()
    assert eng.source.position()["steps"] == pos["steps"] + 1 == durable_steps(db, cid)
    eng.stop()


def test_stop_retries_cursor_write(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    real = break_cursor(db)
    eng._tick()
    steps = eng.source.position()["steps"]
    assert eng._cursor_pending and durable_steps(db, eng.comp_id) == steps - 1
    db.kv_set = real  # type: ignore[method-assign]   # 关闭前数据库恢复
    eng.stop()
    assert durable_steps(db, eng.comp_id) == steps and not eng._cursor_pending
