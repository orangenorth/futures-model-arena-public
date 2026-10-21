"""第二十三轮审查：实时行情下触发成交落库失败也要保留并按原价重试；重启后仍会补做。"""
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


class LiveSource:
    """模拟实时行情源：价格按脚本给出，时间戳为墙钟（通过新鲜度准入）。"""

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


def _setup_live_with_failed_trigger(eng, db, prices=(3400.0, 1000.0, 5000.0, 5000.0)):
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.accounts["ok"].positions["RB0"].stop_loss = 2000.0
    eng.source = LiveSource(eng.source, prices)
    eng._tick()                        # 3400：未触发
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    t_fail = time.time()
    eng._tick()                        # 1000：触发止损，但落库失败
    db.save_account = real_save  # type: ignore[method-assign]
    assert "RB0" in eng.accounts["ok"].positions and "ok" in eng._unsettled and "ok" in eng._pending_marks
    assert eng._replay_retry is None, "实时源没有回放游标"
    return t_fail


def test_live_failed_trigger_is_retried_at_original_price(tmp_path):
    eng, db = make_engine(tmp_path)
    t_fail = _setup_live_with_failed_trigger(eng, db)
    # 期间的模型决策不得执行（否则触发条件会被改掉）
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=[ActionRequest("RB0", "adjust_sl_tp", stop_loss=1)]), snap, eng._generation)
    assert eng.accounts["ok"].positions["RB0"].stop_loss == 2000.0
    assert db.decisions(eng.comp_id, "ok", limit=1)[0]["results"][0]["accepted"] is False
    eng._tick()                        # 下一根 5000：已不再穿越触发价，但必须先按 1000 补做原触发
    assert "RB0" not in eng.accounts["ok"].positions and not eng._unsettled and not eng._pending_marks
    sl = next(t for t in db.trades(eng.comp_id, "ok", limit=5) if t["action"] == "stop_loss")
    assert sl["price"] <= 1000.0, "成交价应为原触发时的价格，而不是重试时的新价格"
    assert abs(sl["ts"] - t_fail) < 2.0, "成交时间应为原触发时刻"
    assert eng.prices["RB0"] == 5000.0
    assert not eng._journal_path().exists(), "结算成功后预写日志应清除"
    # 之后决策恢复
    eng._finish_decision("ok", Decision("ok", 3, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    eng.stop()


def test_live_failed_trigger_survives_restart(tmp_path):
    eng, db = make_engine(tmp_path)
    _setup_live_with_failed_trigger(eng, db)
    cid = eng.comp_id
    eng.stop()
    assert "ok" in json.loads(eng._journal_path().read_text(encoding="utf-8"))["marks"], "触发在落库前已写入预写日志"
    db.close()
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(eng.cfg, db2, accelerate=True)
    # 重启即按原价补做（不等第一个 tick，且先于配置对齐）
    assert eng2.comp_id == cid and not eng2._unsettled and "RB0" not in eng2.accounts["ok"].positions
    eng2.source = LiveSource(eng2.source, [5000.0])   # 重启后的行情早已远离触发价
    eng2._tick()
    assert "RB0" not in eng2.accounts["ok"].positions
    sls = [t for t in db2.trades(cid, "ok", limit=5) if t["action"] == "stop_loss"]
    assert len(sls) == 1 and sls[0]["price"] <= 1000.0
    eng2.stop()
    db2.close()


def test_live_retry_keeps_waiting_while_database_stays_down(tmp_path):
    eng, db = make_engine(tmp_path)
    _setup_live_with_failed_trigger(eng, db, prices=(3400.0, 1000.0, 5000.0, 5000.0, 5000.0))
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                         # 重试仍失败：保持待重试，不用新价格盯市
    assert "RB0" in eng.accounts["ok"].positions and "ok" in eng._unsettled
    real = Database(str(tmp_path / "e.db"))
    db.save_account = real.save_account  # type: ignore[method-assign]
    eng._tick()                         # 数据库恢复：补做
    assert "RB0" not in eng.accounts["ok"].positions and not eng._unsettled
    eng.stop()
    real.close()
