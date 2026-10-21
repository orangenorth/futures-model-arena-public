"""第三十八轮审查：回放 start_price 必须是有限正数；无成交盯市改动的持仓最新价 / 权益峰值同样先落库再发布，
崩溃后重启（含品种已移除时的对齐平仓）用的才不是陈旧值。"""
import json
import math
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from arena.config import AppConfig, ReplayCfg, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.market.replay import ReplaySource
from arena.models import ActionRequest, Decision
from tests.conftest import restamp_competition_universe

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_cfg(tmp_path, symbols=("RB0", "I0")):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


# ---------- start_price 校验 ----------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 0, -3400])
def test_replay_start_price_must_be_finite_and_positive(tmp_path, bad, cn_specs):
    with pytest.raises(ValidationError):
        ReplayCfg(start_price={"RB0": bad})
    with pytest.raises(ValueError):
        ReplaySource(["RB0"], cn_specs, seed=1, start_price={"RB0": bad}, history=5)


def test_replay_start_price_accepts_positive_values(cn_specs):
    assert ReplayCfg(start_price={"RB0": 3400, "I0": 0.5}).start_price == {"RB0": 3400.0, "I0": 0.5}
    src = ReplaySource(["RB0"], cn_specs, seed=1, start_price={"RB0": 3400}, history=5)
    assert all(math.isfinite(b.close) and min(b.open, b.high, b.low, b.close) > 0 for b in src.klines("RB0", "1m", 10))


def test_start_price_below_one_tick_is_rejected(cn_specs):
    # RB0 跳价 1：0.5 虽为正数，但取整后开/低价为 0
    with pytest.raises(ValueError, match="最小变动价位"):
        ReplaySource(["RB0"], cn_specs, seed=1, start_price={"RB0": 0.5}, history=5)


def test_synthetic_bars_never_round_to_zero(cn_specs):
    tick = cn_specs["RB0"].tick
    src = ReplaySource(["RB0"], cn_specs, seed=5, start_price={"RB0": tick}, history=50)   # 起始价正好一个跳价
    for _ in range(50):
        src.latest(["RB0"])
    bars = src.klines("RB0", "1m", 200)
    assert len(bars) >= 100
    for b in bars:
        assert min(b.open, b.high, b.low, b.close) >= tick, "每个 OHLC 字段至少一个跳价"
        assert b.high >= max(b.open, b.close) and b.low <= min(b.open, b.close)


# ---------- 无成交盯市：先落库再发布 ----------
def _open_rb(eng):
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions


def persisted(db, cid):
    acc = db.load_accounts(cid)["ok"]
    return acc.positions["RB0"].last_price, acc.peak_equity


def test_no_trade_mark_persists_last_price_and_peak_before_publishing(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    _open_rb(eng)
    for _ in range(3):
        eng._tick()
    pos = eng.accounts["ok"].positions["RB0"]
    assert persisted(db, eng.comp_id) == (pos.last_price, eng.accounts["ok"].peak_equity), "每个 tick 后数据库与内存一致"
    assert pos.last_price == eng.prices["RB0"]
    # 数据库不可写：无成交的盯市更新不发布（内存保持上次已落库的值），也不算待重试触发
    before = (pos.last_price, eng.accounts["ok"].peak_equity)
    steps = eng.source.position()["steps"]
    durable = json.loads(db.kv_get(f"replay_pos:{eng.comp_id}"))["steps"]
    assert durable == steps
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()
    assert (eng.accounts["ok"].positions["RB0"].last_price, eng.accounts["ok"].peak_equity) == before
    assert not eng._unsettled and not eng._pending_marks and "ok" in eng._mark_save_failed
    # 回放位置不能越过这根未落库的 bar：保留重放，持久化位置停在上一根
    assert eng._replay_retry is not None
    assert json.loads(db.kv_get(f"replay_pos:{eng.comp_id}"))["steps"] == durable == eng.source.position()["steps"] - 1
    eng._tick()                                                     # 仍失败：同一根 bar 重放，不前进
    assert eng.source.position()["steps"] == steps + 1 and eng._replay_retry is not None
    db.save_account = Database(str(tmp_path / "e.db")).save_account  # type: ignore[method-assign]
    eng._tick()                                                     # 恢复：重放的 bar 落库成功，位置前进
    pos = eng.accounts["ok"].positions["RB0"]
    assert pos.last_price == eng.prices["RB0"] and "ok" not in eng._mark_save_failed and eng._replay_retry is None
    assert persisted(db, eng.comp_id) == (pos.last_price, eng.accounts["ok"].peak_equity)
    assert json.loads(db.kv_get(f"replay_pos:{eng.comp_id}"))["steps"] == steps + 1 == eng.source.position()["steps"]
    eng.stop()


def test_reconcile_after_crash_closes_at_the_last_persisted_mark(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    _open_rb(eng)
    for _ in range(5):
        eng._tick()
    last_px = eng.accounts["ok"].positions["RB0"].last_price
    spec = eng.specs["RB0"]
    expected_fill = eng.exchange.fill_price(spec, last_px, "sell")
    cid = eng.comp_id
    # 进程突然退出（不调用 stop）；重启时 RB0 已从配置移除 → 对齐按最后价平仓
    db2 = Database(str(tmp_path / "e.db"))
    restamp_competition_universe(db2, cid, ["I0"])   # resume under the reduced universe, so reconciliation runs
    eng2 = Engine(make_cfg(tmp_path, symbols=["I0"]), db2, accelerate=True)
    assert eng2.comp_id == cid and "RB0" not in eng2.accounts["ok"].positions
    close = next(t for t in db2.trades(cid, "ok", limit=10) if t["symbol"] == "RB0" and t["action"] == "close_long")
    assert close["price"] == expected_fill, "按崩溃前最后一个 tick 落库的最新价平仓，而不是开仓时的旧值"
    eng2.stop()
    eng.stop()
