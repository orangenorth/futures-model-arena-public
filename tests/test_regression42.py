"""第四十二轮：回放 bar 因结算/盯市更新未落库而被保留期间，任何账户迟到的决策动作都不执行，
_round 也不再发起模型调用；写入成功、bar 放行后决策恢复。"""
import json
import sqlite3
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


def durable_steps(db, cid):
    return json.loads(db.kv_get(f"replay_pos:{cid}"))["steps"]


def test_late_decision_is_rejected_while_bar_is_retained_for_a_failed_mark(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng._tick()
    snap = eng._build_snapshot()                  # 迟到的决策在写入失败之前发起
    steps = eng.source.position()["steps"]
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                   # ok 的无成交盯市更新落库失败 → bar 保留，位置停在上一根
    assert "ok" in eng._mark_save_failed and eng._replay_retry is not None
    assert durable_steps(db, eng.comp_id) == steps == eng.source.position()["steps"] - 1
    db.save_account = real_save  # type: ignore[method-assign]
    # 另一个健康账户此时返回可开仓的决策：只能记零成交的拒绝审计，不得写入成交
    d = Decision("other", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("other", d, snap, eng._generation)
    assert not eng.accounts["other"].positions and db.trades(eng.comp_id, "other", limit=5) == []
    res = db.decisions(eng.comp_id, "other", limit=1)[0]["results"]
    assert res and all(r["accepted"] is False and "回放 bar" in r["reason"] for r in res)
    assert durable_steps(db, eng.comp_id) == steps, "持久化位置仍在上一根"
    eng._tick()                                   # 重放：盯市更新写入成功，bar 放行，位置前进
    assert eng._replay_retry is None and durable_steps(db, eng.comp_id) == steps + 1
    eng._finish_decision("other", Decision("other", 3, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    assert "RB0" in eng.accounts["other"].positions
    eng.stop()


def test_round_is_skipped_while_bar_is_retained_or_cursor_pending(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    rn = eng.round_no
    eng._replay_retry = (dict(eng.quotes), {s: list(b) for s, b in eng._replay_bars.items()})   # 模拟 bar 被保留
    eng._round()
    assert eng.round_no == rn and db.decisions(eng.comp_id) == [] and "未结算" in eng.status_text
    eng._replay_retry = None
    eng._cursor_pending = True
    eng._round()
    assert eng.round_no == rn and db.decisions(eng.comp_id) == [] and "回放位置" in eng.status_text
    eng._cursor_pending = False
    eng._round()
    assert eng.round_no == rn + 1 and len(db.decisions(eng.comp_id)) == 2, "放行后正常出轮次"
    eng.stop()
