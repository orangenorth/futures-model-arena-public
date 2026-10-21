"""第二十二轮审查：回放 bar 有账户未结算时，其模型动作不执行、主循环不开新一轮，直到重放结算。"""
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
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB),
                 TraderCfg(id="other", name="OTHER", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


def _open_and_fail_stop(eng, db):
    """ok 开多、止损设为必触发；下一根 bar 的触发成交落库失败 → bar 未结算。返回失败前的持仓快照。"""
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    pos = eng.accounts["ok"].positions["RB0"]
    pos.stop_loss = 1e9
    qty, sl = pos.qty, pos.stop_loss
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()
    assert "ok" in eng._unsettled and eng._replay_retry is not None
    db.save_account = real_save  # type: ignore[method-assign]
    return qty, sl


def test_decisions_for_unsettled_account_are_blocked_until_replay_settles(tmp_path):
    eng, db = make_engine(tmp_path)
    qty, sl = _open_and_fail_stop(eng, db)
    snap = eng._build_snapshot()
    # 在飞行中/新开始的决策想加仓并把止损改到保留报价之下：必须被拒绝，持仓与止损不变
    d = Decision("ok", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1),
                                       ActionRequest("RB0", "adjust_sl_tp", stop_loss=1)])
    eng._finish_decision("ok", d, snap, eng._generation)
    res = db.decisions(eng.comp_id, "ok", limit=1)[0]["results"]
    assert res and all(r["accepted"] is False and "尚未持久化" in r["reason"] for r in res)
    assert eng.accounts["ok"].positions["RB0"].qty == qty and eng.accounts["ok"].positions["RB0"].stop_loss == sl
    # bar 被保留期间（持久化位置停在上一根），其他账户迟到的决策同样只记零成交的拒绝审计：
    # 否则成交会记在持久化位置之后的 bar 上，崩溃重启重放该 bar 时又对已改变的账户再决策一次
    eng._finish_decision("other", Decision("other", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    assert "RB0" not in eng.accounts["other"].positions
    assert all(r["accepted"] is False and "回放 bar" in r["reason"] for r in db.decisions(eng.comp_id, "other", limit=1)[0]["results"])
    # 重放结算：原来的止损在保留的 bar 上触发成交，之后决策恢复
    eng._tick()
    assert "RB0" not in eng.accounts["ok"].positions and eng._replay_retry is None and not eng._unsettled
    assert any(t["action"] == "stop_loss" for t in db.trades(eng.comp_id, "ok", limit=5))
    eng._finish_decision("ok", Decision("ok", 3, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    eng._finish_decision("other", Decision("other", 3, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    assert "RB0" in eng.accounts["other"].positions
    eng.stop()


def test_main_loop_does_not_start_rounds_while_replay_bar_is_unsettled(tmp_path):
    eng, db = make_engine(tmp_path)
    _open_and_fail_stop(eng, db)
    db.save_account = broken  # type: ignore[method-assign]   # 重放仍失败：一直未结算
    eng.start()
    time.sleep(2.5)
    assert eng.round_no == 0, "bar 未结算期间不得开始新一轮决策"
    assert "未结算" in eng.status_text
    real = Database(str(tmp_path / "e.db"))
    db.save_account = real.save_account  # type: ignore[method-assign]   # 数据库恢复 → 重放结算 → 决策恢复
    deadline = time.time() + 5
    while time.time() < deadline and eng.round_no == 0:
        time.sleep(0.2)
    assert eng.round_no >= 1 and not eng._unsettled
    eng.stop()
    real.close()
