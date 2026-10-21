"""恢复/重置/持久化原子性相关回归测试（对应 PR 审查发现）。"""
import json
import sqlite3
from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.market.replay import ReplaySource
from arena.models import Account, Decision, ActionRequest, Position
from tests.conftest import restamp_competition_universe

ROOT = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path, traders, symbols):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=symbols,
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800, "AU0": 560}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"breaker_failures": 2, "breaker_skip_rounds": 2, "max_retries": 0, "timeout_sec": 5},
        traders=traders,
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


OPEN_BOTH = json.dumps({"analysis": "x", "actions": [
    {"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1},
    {"symbol": "I0", "action": "open_short", "size_pct": 0.1, "stop_loss": 99999}]})


def test_reconcile_removed_trader_and_symbol(tmp_path):
    traders = [TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_BOTH),
               TraderCfg(id="gone", name="GONE", provider="mock", mock_response=OPEN_BOTH)]
    cfg = make_cfg(tmp_path, traders, ["RB0", "I0"])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick(); eng._round(); eng._snapshot()
    assert set(eng.accounts["ok"].positions) == {"RB0", "I0"}
    cash_before = eng.accounts["ok"].cash
    cid = eng.comp_id
    eng.stop(); db.close()

    # 配置变化：移除交易员 gone、移除品种 I0
    cfg2 = make_cfg(tmp_path, [traders[0]], ["RB0"])
    db2 = Database(cfg2.db_path)
    # Resume the competition under the reduced universe so reconciliation runs
    # instead of a rollover; the I0 position from before is what it must clean up.
    restamp_competition_universe(db2, cid, ["RB0"])
    eng2 = Engine(cfg2, db2, accelerate=True)
    assert eng2.comp_id == cid
    assert set(eng2.accounts) == {"ok"}
    assert set(eng2.accounts["ok"].positions) == {"RB0"}
    # I0 被按最后价平仓并记录成交，现金随之变化（含手续费）
    assert eng2.accounts["ok"].cash != cash_before
    closes = [t for t in db2.trades(cid, "ok", limit=50) if t["symbol"] == "I0" and t["action"] == "close_short"]
    assert closes and "移除" in closes[0]["reason"]
    # 数据库中被移除交易员的账户仍保留
    assert "gone" in db2.load_accounts(cid)
    # state() 与后续轮次不再崩溃
    st = eng2.state()
    assert [t["id"] for t in st["traders"]] == ["ok"]
    eng2._tick(); eng2._round(); eng2._snapshot()
    eng2.stop()


def test_reset_discards_in_flight_decision(tmp_path):
    traders = [TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_BOTH)]
    cfg = make_cfg(tmp_path, traders, ["RB0", "I0"])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    gen = eng._generation
    old_cid = eng.comp_id
    eng.reset()
    assert eng.comp_id != old_cid
    stale = Decision(trader_id="ok", round_no=1, ts=0, actions=[ActionRequest("RB0", "open_long", 0.2, stop_loss=1)])
    eng._finish_decision("ok", stale, snap, gen)
    assert eng.accounts["ok"].positions == {}
    assert db.decisions(eng.comp_id) == []
    assert eng.accounts["ok"].cash == 1_000_000
    # 新一代的决策正常执行
    fresh = Decision(trader_id="ok", round_no=1, ts=0, actions=[ActionRequest("RB0", "open_long", 0.2, stop_loss=1)])
    eng._finish_decision("ok", fresh, snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    eng.stop()


def test_save_account_is_atomic(tmp_path):
    db = Database(tmp_path / "a.db")
    cid = db.create_competition("c", "replay", 1000, "{}")
    acc = Account("a", 990, 1000)
    acc.positions["RB0"] = Position("a", "RB0", "long", 2, 3400, 8840)
    db.save_account(cid, acc)
    # 第二个持仓非法（qty=None 违反 NOT NULL）→ 整个保存回滚，旧持仓保留
    acc.positions["I0"] = Position("a", "I0", "short", None, 800, 100)  # type: ignore[arg-type]
    acc.cash = 123
    with pytest.raises(sqlite3.IntegrityError):
        db.save_account(cid, acc)
    loaded = db.load_accounts(cid)["a"]
    assert loaded.cash == 990 and set(loaded.positions) == {"RB0"}
    # 连接仍可正常写入
    del acc.positions["I0"]
    db.save_account(cid, acc)
    assert db.load_accounts(cid)["a"].cash == 123
    db.close()


def test_competition_snapshot_redacts_all_trader_credential_fields(tmp_path):
    marker = "test-literal-key.value"
    traders = [TraderCfg(id="provider", name="Provider", api_key=marker, api_key_env=marker)]
    cfg = make_cfg(tmp_path, traders, ["RB0"])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    snapshot = db.active_competition()["config_json"]
    stored = json.loads(snapshot)
    assert marker not in snapshot
    assert "api_key" not in stored["traders"][0]
    assert "api_key_env" not in stored["traders"][0]
    eng.stop()
    db.close()


def test_replay_csv_exhaustion_continues(tmp_path, cn_specs):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    lines = ["datetime,open,high,low,close,volume"]
    for i in range(5):
        lines.append(f"2026-09-16 09:0{i}:00,3400,3410,3390,{3400 + i},100")
    (csv_dir / "RB0.csv").write_text("\n".join(lines), encoding="utf-8")
    src = ReplaySource(["RB0"], cn_specs, seed=1, csv_dir=str(csv_dir), history=300)
    prices = []
    for _ in range(10):
        q = src.latest(["RB0"])
        prices.append(q["RB0"].price)
    assert len(prices) == 10 and all(p > 0 for p in prices)
    assert len(src.klines("RB0", "1m", 100)) >= 15
