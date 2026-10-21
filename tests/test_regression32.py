"""第三十二轮审查：启动时先补做恢复的触发成交，再做配置对齐；按新配置切换竞赛前也先按旧竞赛的持久化配置补做，补不了则拒绝切换。"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine, RolloverBlocked
from arena.models import ActionRequest, Decision, Quote
from tests.conftest import restamp_competition_universe

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})
OK = TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)
OTHER = TraderCfg(id="other", name="OTHER", provider="mock", mock_response=OPEN_RB)


def make_cfg(tmp_path, traders=(OK, OTHER), symbols=("RB0", "I0"), market="replay", initial_cash=1_000_000):
    return AppConfig(
        competition={"name": "t", "market": market, "initial_cash": initial_cash, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=list(traders),
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


class LiveSource:
    """把回放源包装成按给定价格序列报价的实时源：便于制造一根穿越持久化止损价的行情。"""

    def __init__(self, replay, prices):
        self.replay, self.prices, self.n = replay, list(prices), 0

    def latest(self, symbols):
        px = self.prices[min(self.n, len(self.prices) - 1)]
        self.n += 1
        return {"RB0": Quote("RB0", px, time.time())}

    def klines(self, symbol, period, limit=120):
        return self.replay.klines(symbol, period, limit)

    def is_open(self, now, symbol=None):
        return True

    def next_open(self, now):
        return now

    def health(self):
        return {"source": "live-test", "ok": True, "errors": 0, "detail": ""}


def leave_pending_stop(tmp_path):
    """ok 开多 RB0（持久化止损 2000），行情跌到 1000 触发止损但落库失败，然后关闭进程：
    预写日志里留着待补做的止损（原价 1000）。返回 (竞赛id, 持仓手数, 日志路径)。"""
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    qty = eng.accounts["ok"].positions["RB0"].qty
    eng.source = LiveSource(eng.source, [3400.0, 1000.0])
    eng._tick()                                   # 3400：未触发
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                   # 1000：触发止损，落库失败 → 预写日志
    assert "ok" in eng._pending_marks and eng._pending_marks["ok"][0]["RB0"] == 1000.0
    cid, journal = eng.comp_id, eng._journal_path()
    eng.stop()
    db.close()
    assert journal.exists()
    return cid, qty, journal


def stop_trades(db, cid, tid="ok"):
    return [t for t in db.trades(cid, tid, limit=20) if t["symbol"] == "RB0" and t["action"] != "open_long"]


# ---------- 同一竞赛恢复：先补做，再对齐 ----------
def test_restored_trigger_settles_before_removed_symbol_is_reconciled(tmp_path):
    cid, qty, journal = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    restamp_competition_universe(db, cid, ["I0"])   # resume under the reduced universe, so reconciliation runs
    eng = Engine(make_cfg(tmp_path, symbols=["I0"]), db, accelerate=True)   # RB0 已从配置移除
    assert eng.comp_id == cid
    closes = stop_trades(db, cid)
    assert [t["action"] for t in closes] == ["stop_loss"] and closes[0]["qty"] == qty, "必须是原触发价的止损，而不是按最后价的移除平仓"
    assert "RB0" not in eng.accounts["ok"].positions and not eng._pending_marks and not journal.exists()
    assert not any("移除" in e["message"] and "RB0" in e["message"] for e in db.events(cid, 50))
    eng.stop()


def test_restored_trigger_settles_before_removed_trader_is_dropped(tmp_path):
    cid, qty, journal = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    eng = Engine(make_cfg(tmp_path, traders=[OTHER]), db, accelerate=True)   # 交易员 ok 已从配置移除
    assert eng.comp_id == cid and "ok" not in eng.accounts, "补做之后仍按配置移除"
    assert [t["action"] for t in stop_trades(db, cid)] == ["stop_loss"]
    assert db.load_accounts(cid)["ok"].positions == {} and not journal.exists()
    eng.stop()


def test_reconcile_is_deferred_while_restored_trigger_cannot_be_settled(tmp_path):
    cid, qty, journal = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(make_cfg(tmp_path, traders=[OTHER]), db, accelerate=True)   # 数据库仍不可写
    assert "ok" in eng.accounts and "RB0" in eng.accounts["ok"].positions, "结算前不能删账户/平持仓"
    assert "ok" in eng._pending_marks and eng._deferred_reconcile == {"ok"} and journal.exists()
    eng._tick()                                                            # 仍失败：状态不变
    assert "ok" in eng.accounts and "ok" in eng._pending_marks
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                                            # 恢复：先补做止损，再执行推迟的对齐
    assert [t["action"] for t in stop_trades(db, cid)] == ["stop_loss"]
    assert "ok" not in eng.accounts and not eng._deferred_reconcile and not eng._pending_marks and not journal.exists()
    assert "ok" in db.load_accounts(cid) and db.load_accounts(cid)["ok"].positions == {}
    eng.stop()


# ---------- 配置变化切换竞赛：归档前先按旧配置补做，补不了则拒绝 ----------
def test_rollover_settles_old_journal_before_archiving(tmp_path):
    old_cid, qty, journal = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    eng = Engine(make_cfg(tmp_path, initial_cash=500_000), db, accelerate=True)   # 起始资金变化 → 新竞赛
    assert eng.comp_id != old_cid and db.active_competition()["id"] == eng.comp_id
    closes = stop_trades(db, old_cid)
    assert [t["action"] for t in closes] == ["stop_loss"] and closes[0]["qty"] == qty, "止损记在旧竞赛名下"
    assert db.load_accounts(old_cid)["ok"].positions == {}
    assert not journal.exists() and not eng._pending_marks and not eng._unsettled
    assert all(not a.positions and a.cash == 500_000 for a in eng.accounts.values())
    assert db.trades(eng.comp_id, limit=5) == []
    eng.stop()


def test_rollover_across_markets_settles_with_the_old_contract_specs(tmp_path):
    old_cid, qty, journal = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    cfg = make_cfg(tmp_path, market="crypto", symbols=["BTC-USDT-SWAP"])       # 市场变化：新规格里没有 RB0
    eng = Engine(cfg, db, accelerate=True)
    assert eng.comp_id != old_cid
    closes = stop_trades(db, old_cid)
    assert [t["action"] for t in closes] == ["stop_loss"] and closes[0]["qty"] == qty
    assert db.load_accounts(old_cid)["ok"].positions == {} and not journal.exists()
    eng.stop()


def test_rollover_is_refused_while_old_journal_cannot_be_settled(tmp_path):
    old_cid, qty, journal = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    with pytest.raises(RolloverBlocked, match="尚未落库"):
        Engine(make_cfg(tmp_path, initial_cash=500_000), db, accelerate=True)
    assert db.active_competition()["id"] == old_cid, "补不了就不能归档旧竞赛"
    assert stop_trades(db, old_cid) == [] and journal.exists()
    db.save_account = real_save  # type: ignore[method-assign]
    eng = Engine(make_cfg(tmp_path, initial_cash=500_000), db, accelerate=True)   # 数据库恢复后再启动：补做并切换
    assert eng.comp_id != old_cid and [t["action"] for t in stop_trades(db, old_cid)] == ["stop_loss"]
    assert not journal.exists()
    eng.stop()
