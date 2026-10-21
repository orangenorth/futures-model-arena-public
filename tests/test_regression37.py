"""第三十七轮审查：触发成交待落库而推迟对齐期间，已移除品种的持仓与已移除交易员的账户暂留内存，
看板状态、账户视图与其他交易员的决策不能因此 KeyError。"""
import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision, Quote
from arena.server.api import create_app
from tests.conftest import restamp_competition_universe

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})
OK = TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)
OTHER = TraderCfg(id="other", name="OTHER", provider="mock", mock_response=OPEN_RB)


def make_cfg(tmp_path, traders=(OK, OTHER), symbols=("RB0", "I0")):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=list(traders),
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


class LiveSource:
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


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


def leave_pending_stop(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, [3400.0, 1000.0])
    eng._tick()
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()
    assert "ok" in eng._pending_marks
    cid = eng.comp_id
    eng.stop()
    db.close()
    return cid


def test_state_and_views_survive_a_deferred_removed_symbol(tmp_path):
    cid = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    restamp_competition_universe(db, cid, ["I0"])   # resume under the reduced universe, so reconciliation runs
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(make_cfg(tmp_path, symbols=["I0"]), db, accelerate=True)     # RB0 已移除，但止损待落库 → 持仓暂留
    assert eng.comp_id == cid and "RB0" in eng.accounts["ok"].positions and eng._deferred_reconcile == {"ok"}
    st = eng.state()                                                             # 不得 KeyError
    me = next(t for t in st["traders"] if t["id"] == "ok")
    rb = next(p for p in me["positions"] if p["symbol"] == "RB0")
    assert rb["name"] == "螺纹钢" and me["equity"] > 0
    assert TestClient(create_app(eng, ROOT / "web")).get("/api/state").status_code == 200
    snap = eng._build_snapshot()
    view = eng._account_view("ok", snap)
    assert view.equity > 0 and any(p["symbol"] == "RB0" for p in view.positions)
    eng._snapshot()                                                              # 权益快照/日汇总同样不崩
    # 数据库恢复后其他交易员的决策照常执行
    db.save_account = real_save  # type: ignore[method-assign]
    eng._finish_decision("other", Decision("other", 2, 0, actions=[ActionRequest("I0", "open_short", 0.1, stop_loss=99999.0)]), snap, eng._generation)
    assert "I0" in eng.accounts["other"].positions
    eng._tick()                                                                  # 数据库恢复：补做止损，对齐完成
    assert "RB0" not in eng.accounts["ok"].positions and not eng._deferred_reconcile
    assert "RB0" not in eng.specs and eng.state()["traders"]
    eng.stop()


def test_state_omits_a_deferred_removed_trader_without_failing(tmp_path):
    cid = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(make_cfg(tmp_path, traders=[OTHER]), db, accelerate=True)      # 交易员 ok 已移除，但止损待落库 → 账户暂留
    assert eng.comp_id == cid and "ok" in eng.accounts and "ok" not in eng.traders
    st = eng.state()
    assert [t["id"] for t in st["traders"]] == ["other"], "暂留的账户不进排行榜，也不能让整个状态接口失败"
    assert TestClient(create_app(eng, ROOT / "web")).get("/api/state").status_code == 200
    eng._snapshot()
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()
    assert "ok" not in eng.accounts and [t["id"] for t in eng.state()["traders"]] == ["other"]
    eng.stop()
