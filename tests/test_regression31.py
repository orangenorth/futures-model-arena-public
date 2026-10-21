"""第三十一轮审查：重置前先补做旧竞赛尚未落库的触发成交，补不了则拒绝重置；回放 history / bar_seconds 必须为正。"""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from arena.config import AppConfig, ReplayCfg, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine, ResetBlocked
from arena.market.replay import ReplaySource
from arena.models import ActionRequest, Decision
from arena.server.api import create_app

ROOT = Path(__file__).resolve().parents[1]
# 写接口按 Host 判定拒绝非看板地址，TestClient 默认的 http://testserver 不是：用真实的回环看板地址
LOOPBACK_BASE = "http://127.0.0.1:8765"
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_cfg(tmp_path, **replay):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}, **replay}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


def make_engine(tmp_path, **replay):
    cfg = make_cfg(tmp_path, **replay)
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


def _open_and_fail_stop(eng, db):
    """ok 开多、止损设为必触发；下一根 bar 的触发成交落库失败 → 进入待重试（预写日志已写）。返回真正的 save_account。"""
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.accounts["ok"].positions["RB0"].stop_loss = 1e9
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()
    assert "ok" in eng._pending_marks and eng._journal_path().exists()
    return real_save


# ---------- 重置：先补做待重试的触发成交，补不了则拒绝 ----------
def test_reset_settles_pending_triggers_first_or_refuses(tmp_path):
    eng, db = make_engine(tmp_path)
    real_save = _open_and_fail_stop(eng, db)
    old_cid, journal = eng.comp_id, eng._journal_path()
    with pytest.raises(ResetBlocked, match="尚未落库"):
        eng.reset()
    assert db.active_competition()["id"] == old_cid, "触发成交未落库时不得归档旧竞赛"
    assert "ok" in eng._pending_marks and journal.exists(), "待重试状态与预写日志都必须保留"
    assert "RB0" in eng.accounts["ok"].positions
    # 数据库恢复：重置先按原价补做止损（记在旧竞赛名下），再新建竞赛
    db.save_account = real_save  # type: ignore[method-assign]
    eng.reset()
    new_cid = eng.comp_id
    assert new_cid != old_cid and db.active_competition()["id"] == new_cid
    assert any(t["action"] == "stop_loss" for t in db.trades(old_cid, "ok", limit=5)), "补做的止损成交属于旧竞赛"
    assert db.load_accounts(old_cid)["ok"].positions == {}, "旧竞赛归档时持仓已被补做的止损平掉"
    assert not eng._pending_marks and not eng._unsettled and not journal.exists()
    assert not eng.accounts["ok"].positions and eng.accounts["ok"].cash == 1_000_000
    assert db.trades(new_cid, "ok", limit=5) == [], "新竞赛不带旧成交"
    eng.stop()


def test_control_reset_reports_conflict_while_triggers_are_pending(tmp_path):
    eng, db = make_engine(tmp_path)
    real_save = _open_and_fail_stop(eng, db)
    old_cid = eng.comp_id
    # 看板开在回环地址上；写接口会校验 Host（防 DNS 重绑定），所以测试也要用真实的本机地址
    client = TestClient(create_app(eng, ROOT / "web"), base_url=LOOPBACK_BASE)
    r = client.post("/api/control", json={"action": "reset"})
    assert r.status_code == 409 and "尚未落库" in r.json()["detail"]
    assert db.active_competition()["id"] == old_cid
    db.save_account = real_save  # type: ignore[method-assign]
    r = client.post("/api/control", json={"action": "reset"})
    assert r.status_code == 200 and eng.comp_id != old_cid
    eng.stop()


# ---------- 回放配置：history / bar_seconds 必须为正 ----------
@pytest.mark.parametrize("bad", [0, -1])
def test_replay_history_must_be_positive(tmp_path, bad, cn_specs):
    with pytest.raises(ValidationError):
        ReplayCfg(history=bad)
    with pytest.raises(ValidationError):
        make_cfg(tmp_path, history=bad)
    with pytest.raises(ValueError):
        ReplaySource(["RB0"], cn_specs, seed=1, start_price={"RB0": 3400}, history=bad)


def test_replay_bar_seconds_must_be_positive():
    with pytest.raises(ValidationError):
        ReplayCfg(bar_seconds=0)


def test_replay_with_single_history_bar_keeps_ticking(tmp_path, cn_specs):
    src = ReplaySource(["RB0"], cn_specs, seed=1, start_price={"RB0": 3400}, history=1)
    for _ in range(3):
        assert src.latest(["RB0"])["RB0"].price > 0
    assert len(src.klines("RB0", "1m", 10)) == 4
    eng, db = make_engine(tmp_path, history=1)
    for _ in range(3):
        eng._tick()
    assert eng.quotes["RB0"].price > 0
    for _ in range(10):        # 历史不足 5 分钟时快照为空（无 5m bar，不决策），bar 积累后恢复
        eng._tick()
    snap = eng._build_snapshot()
    assert snap is not None and "RB0" in snap.symbols
    eng.stop()
