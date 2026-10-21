"""第十二轮审查：执行前再校验品种可交易性；结算异常路径的事件写入尽力而为。"""
import json
import sqlite3
import time
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision, Quote

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_engine(tmp_path, accelerate=False):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=accelerate), db


def test_delayed_decision_is_revalidated_at_execution(tmp_path):
    eng, db = make_engine(tmp_path)
    replay = eng.source

    class LiveSource:
        def __init__(self):
            self.open = True

        def latest(self, symbols):
            return replay.latest(symbols)

        def klines(self, symbol, period, limit=120):
            return replay.klines(symbol, period, limit)

        def is_open(self, now, symbol=None):
            return self.open

        def next_open(self, now):
            return now

    source = LiveSource()
    eng.source = source
    eng._tick()
    now = time.time()
    eng.quotes = {"RB0": Quote("RB0", 3400.0, ts=now - 30)}
    snap = eng._build_snapshot(dict(eng.quotes), 1)
    assert "RB0" in snap.symbols                       # 快照时可交易
    source.open = False  # 模型返回时已收盘
    d = Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("ok", d, snap, eng._generation)
    res = db.decisions(eng.comp_id, "ok", limit=1)[0]["results"][0]
    assert res["accepted"] is False and "休市" in res["reason"]
    assert eng.accounts["ok"].positions == {}
    # 行情在等待期间过期，同样拒绝
    source.open = True
    eng.quotes["RB0"] = Quote("RB0", 3400.0, ts=now - 3600)
    d2 = Decision("ok", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("ok", d2, snap, eng._generation)
    res2 = db.decisions(eng.comp_id, "ok", limit=1)[0]["results"][0]
    assert res2["accepted"] is False and "未更新" in res2["reason"]
    # 恢复正常 → 成交
    eng.quotes["RB0"] = Quote("RB0", 3400.0, ts=time.time())
    d3 = Decision("ok", 3, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("ok", d3, snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    eng.stop()


def test_settlement_failure_with_broken_db_does_not_abort_round(tmp_path):
    eng, db = make_engine(tmp_path, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked (simulated)")

    db.save_account = broken  # type: ignore[method-assign]
    db.add_event = broken     # type: ignore[method-assign]
    d = Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_safely("ok", d, snap, eng._generation)   # 落库与审计都失败：不得抛出
    assert eng.accounts["ok"].positions == {}
    # 熔断分支与丢弃分支的审计写入同样尽力而为
    eng._breaker["ok"]["fails"] = eng.cfg.llm.breaker_failures - 1
    bad = Decision("ok", 2, 0, error="boom")
    eng._finish_safely("ok", bad, snap, eng._generation)
    assert eng._breaker["ok"]["status"] == "breaker"
    eng._finish_safely("ok", Decision("ok", 3, 0), snap, eng._generation - 1)   # 过期代次 → 丢弃分支
    eng.stop()
