"""第二轮审查发现的回归测试：暂停恢复时序、风控上限、非法数值、原子落库、起始资金变化。"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from arena.config import AppConfig, RiskCfg, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.engine.risk import RiskManager
from arena.exchange.contracts import crypto_spec
from arena.exchange.sim import SimExchange
from arena.models import Account, ActionRequest, Decision, Position, Trade
from arena.traders.prompt import parse_response

ROOT = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path, traders, symbols=("RB0", "I0"), initial_cash=1_000_000):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": initial_cash, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"breaker_failures": 2, "breaker_skip_rounds": 2, "max_retries": 0, "timeout_sec": 5},
        traders=traders,
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def test_paused_state_restored_before_threads_start(tmp_path):
    traders = [TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)]
    cfg = make_cfg(tmp_path, traders)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng.pause(True)
    eng.stop()
    db.close()
    db2 = Database(cfg.db_path)
    eng2 = Engine(cfg, db2, accelerate=True)
    assert eng2.paused is True, "暂停标志应在构造时即恢复"
    eng2.start()
    time.sleep(1.5)
    eng2.stop()
    assert db2.decisions(eng2.comp_id) == [], "暂停中的竞赛重启后不应执行决策"
    assert eng2.accounts["ok"].positions == {}


def test_first_open_capped_by_symbol_limit(cn_specs, account):
    rm = RiskManager(RiskCfg(max_size_pct=0.5, max_notional_pct=0.2), cn_specs)
    v = rm.check(account, ActionRequest("RB0", "open_long", size_pct=0.5), 3400.0, {"RB0": 3400.0}, atr=10.0)
    assert v.ok and v.action.size_pct == pytest.approx(0.2)


def test_mixed_leverage_occupancy_uses_margin():
    spec = crypto_spec("BTC-USDT-SWAP", max_leverage=20)
    specs = {"BTC-USDT-SWAP": spec}
    acc = Account("t", 10_000, 10_000)
    # 已有仓位：名义 7,500，累计保证金 1,800（分批不同杠杆），名义/原杠杆会低估占用
    acc.positions["BTC-USDT-SWAP"] = Position("t", "BTC-USDT-SWAP", "long", 0.15, 50_000, margin=1_800, leverage=5)
    rm = RiskManager(RiskCfg(max_size_pct=0.3, max_notional_pct=0.2, max_leverage=20), specs)
    v = rm.check(acc, ActionRequest("BTC-USDT-SWAP", "open_long", size_pct=0.3, leverage=1), 50_000.0, {"BTC-USDT-SWAP": 50_000.0}, atr=100.0)
    assert v.ok and v.action.size_pct == pytest.approx(0.2 - 0.18)
    acc.positions["BTC-USDT-SWAP"].margin = 2_000
    v2 = rm.check(acc, ActionRequest("BTC-USDT-SWAP", "open_long", size_pct=0.1, leverage=1), 50_000.0, {"BTC-USDT-SWAP": 50_000.0}, atr=100.0)
    assert not v2.ok


def test_exchange_tracks_effective_leverage_on_add():
    spec = crypto_spec("BTC-USDT-SWAP", max_leverage=20)
    ex = SimExchange({"BTC-USDT-SWAP": spec}, slippage_ticks=0)
    acc = Account("t", 10_000, 10_000)
    prices = {"BTC-USDT-SWAP": 50_000.0}
    assert ex.open(acc, spec, "long", 0.1, 50_000.0, 5, None, None, prices, 1).accepted   # 保证金 1000
    assert ex.open(acc, spec, "long", 0.02, 50_000.0, 1, None, None, prices, 1).accepted  # 保证金 1000
    pos = acc.positions["BTC-USDT-SWAP"]
    assert pos.margin == pytest.approx(2_000)
    assert pos.leverage == pytest.approx(0.12 * 50_000 / 2_000)


def test_parse_rejects_non_finite_values():
    syms = ["RB0", "I0"]
    txt = '{"analysis":"x","actions":[{"symbol":"RB0","action":"open_long","size_pct":NaN,"stop_loss":1},' \
          '{"symbol":"I0","action":"open_short","size_pct":"NaN"}]}'
    a, acts, err = parse_response(txt, syms)
    assert acts == [] and "非法" in err
    txt2 = '{"analysis":"x","actions":[{"symbol":"RB0","action":"open_long","size_pct":0.1,"leverage":Infinity},' \
           '{"symbol":"I0","action":"open_short","size_pct":0.1,"stop_loss":"Infinity","take_profit":"abc"}]}'
    a, acts, err = parse_response(txt2, syms)
    assert acts == [] and "RB0" in err and "I0" in err
    txt3 = '{"analysis":"x","actions":[{"symbol":"RB0","action":"open_long","size_pct":0.1,"confidence":NaN},' \
           '{"symbol":"I0","action":"open_short","size_pct":0.1,"confidence":Infinity}]}'
    a, acts, err = parse_response(txt3, syms)
    assert acts == [] and "confidence" in err
    a, acts, err = parse_response('{"analysis":"x","actions":[{"symbol":"RB0","action":"hold"}]}', syms)
    assert len(acts) == 1 and not err


@pytest.mark.parametrize("field", ["size_pct", "leverage", "stop_loss", "take_profit", "confidence"])
def test_risk_rejects_non_finite_action_request(field, cn_specs, account):
    req = ActionRequest("RB0", "open_long", size_pct=0.1, stop_loss=3300)
    setattr(req, field, float("nan"))
    verdict = RiskManager(RiskCfg(), cn_specs).check(account, req, 3400.0, {"RB0": 3400.0}, atr=10.0)
    assert not verdict.ok and field in verdict.reason


def test_risk_uses_default_stop_distance_when_atr_is_non_finite(cn_specs, account):
    req = ActionRequest("RB0", "open_long", size_pct=0.1)
    verdict = RiskManager(RiskCfg(), cn_specs).check(account, req, 3400.0, {"RB0": 3400.0}, atr=float("nan"))
    assert verdict.ok and verdict.action is not None
    assert verdict.action.stop_loss == pytest.approx(3400.0 - 3400.0 * 0.01 * RiskCfg().auto_stop_atr_mult)


def test_round_survives_bad_action_and_isolated_failures(tmp_path):
    bad_then_good = '{"analysis":"x","actions":[{"symbol":"RB0","action":"open_long","size_pct":NaN},' \
                    '{"symbol":"I0","action":"open_short","size_pct":0.1,"stop_loss":99999}]}'
    traders = [TraderCfg(id="a", name="A", provider="mock", mock_response=bad_then_good),
               TraderCfg(id="b", name="B", provider="mock", mock_response=OPEN_RB)]
    cfg = make_cfg(tmp_path, traders)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick(); eng._round()
    assert "I0" in eng.accounts["a"].positions and "RB0" not in eng.accounts["a"].positions
    assert "RB0" in eng.accounts["b"].positions
    d = db.decisions(eng.comp_id, "a", limit=1)[0]
    assert "非法" in d["error"] and len(d["actions"]) == 1
    # 直接注入会在执行阶段抛异常的动作：只记为拒绝，账户仍正常落库
    snap = eng._build_snapshot()
    weird = Decision(trader_id="b", round_no=2, ts=0, actions=[ActionRequest("RB0", "open_long", size_pct=float("nan"), stop_loss=1)])
    eng._finish_decision("b", weird, snap, eng._generation)
    res = db.decisions(eng.comp_id, "b", limit=1)[0]["results"]
    assert res and res[0]["accepted"] is False
    eng.stop()


def test_trades_decision_account_saved_atomically(tmp_path):
    db = Database(tmp_path / "a.db")
    cid = db.create_competition("c", "replay", 1000, "{}")
    acc = Account("a", 1000, 1000)
    db.save_account(cid, acc)
    acc.cash = 990
    acc.positions["RB0"] = Position("a", "RB0", "long", 1, 3400, 4420)
    good = Trade("a", "RB0", "buy", "open_long", 1, 3400, 1, 0, 1.0, 1)
    d = Decision("a", 1, 1.0, analysis="ok", actions=[ActionRequest("RB0", "open_long", 0.1)])
    db.save_account(cid, acc, trades=[good], decision=d)
    assert db.trade_count(cid, "a") == 1 and len(db.decisions(cid, "a")) == 1
    assert db.load_accounts(cid)["a"].cash == 990
    # 失败：成交非法 → 决策、成交、账户全部回滚
    acc.cash = 500
    acc.positions["I0"] = Position("a", "I0", "short", 1, 800, 100)
    bad = Trade("a", "I0", "sell", "open_short", None, 800, 1, 0, 2.0, 2)  # type: ignore[arg-type]
    d2 = Decision("a", 2, 2.0, analysis="bad")
    with pytest.raises(sqlite3.IntegrityError):
        db.save_account(cid, acc, trades=[bad], decision=d2)
    assert db.trade_count(cid, "a") == 1 and len(db.decisions(cid, "a")) == 1
    loaded = db.load_accounts(cid)["a"]
    assert loaded.cash == 990 and set(loaded.positions) == {"RB0"}
    db.close()


def test_initial_cash_change_ends_old_competition_and_starts_a_new_one(tmp_path):
    traders = [TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)]
    cfg = make_cfg(tmp_path, traders, initial_cash=1_000_000)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick(); eng._round()
    cid = eng.comp_id
    eng.pause(True)
    eng.stop(); db.close()

    cfg2 = make_cfg(tmp_path, traders + [TraderCfg(id="new", name="NEW", provider="rule")], initial_cash=500_000)
    db2 = Database(cfg2.db_path)
    eng2 = Engine(cfg2, db2, accelerate=True)
    assert eng2.comp_id != cid
    assert eng2.initial_cash == 500_000
    assert {acc.initial_cash for acc in eng2.accounts.values()} == {500_000}
    assert not eng2.paused and all(not acc.positions for acc in eng2.accounts.values())
    assert db2.trade_count(eng2.comp_id) == 0 and db2.decisions(eng2.comp_id) == []
    assert eng2.state()["competition"]["initial_cash"] == 500_000
    assert "起始资金" in eng2.rules and "500,000" in eng2.rules
    old = next(row for row in db2.list_competitions() if row["id"] == cid)
    assert old["status"] == "ended"
    assert any("结束竞赛" in e["message"] for e in db2.events(cid, 20))
    eng2.stop()
