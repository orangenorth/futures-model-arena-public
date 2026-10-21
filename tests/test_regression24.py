"""第二十四轮审查：触发成交先写预写日志再落库（数据库整体不可用、进程崩溃也不丢）；待重试触发不依赖新行情。"""
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
    def __init__(self, replay, prices):
        self.replay, self.prices, self.n = replay, list(prices), 0

    def latest(self, symbols):
        if not self.prices:
            return {}                     # 收盘 / 行情源故障：没有任何报价
        px = self.prices[min(self.n, len(self.prices) - 1)]
        self.n += 1
        return {"RB0": Quote("RB0", px, time.time())}

    def klines(self, symbol, period, limit=120):
        return self.replay.klines(symbol, period, limit)

    def is_open(self, now, symbol=None):
        return bool(self.prices)

    def next_open(self, now):
        return now

    def health(self):
        return {"source": "live-test", "ok": True, "errors": 0, "detail": ""}


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


def _open_long_with_stop(eng):
    """开多并把止损 2000 一起落库（崩溃重启后从数据库恢复的持仓必须带着这个止损）。"""
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    assert eng.accounts["ok"].positions["RB0"].stop_loss == 2000.0


def test_trigger_is_journaled_before_settlement_and_survives_crash(tmp_path):
    eng, db = make_engine(tmp_path)
    _open_long_with_stop(eng)
    eng.source = LiveSource(eng.source, [3400.0, 1000.0])
    eng._tick()                                  # 3400：未触发
    # 数据库整体不可用：账户落库、kv、事件全部失败
    db.save_account = broken  # type: ignore[method-assign]
    db.kv_set = broken        # type: ignore[method-assign]
    db.add_event = broken     # type: ignore[method-assign]
    t_fail = time.time()
    eng._tick()                                  # 1000：触发止损，落库失败
    journal = eng._journal_path()
    assert journal.exists(), "预写日志必须在落库之前写入，与数据库是否可用无关"
    entry = json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]
    assert entry["prices"]["RB0"] == 1000.0 and abs(entry["clock"] - t_fail) < 2.0
    assert "RB0" in eng.accounts["ok"].positions and "ok" in eng._unsettled
    # 进程崩溃（不调用 stop）：用新连接重建引擎，待重试触发从日志恢复
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(eng.cfg, db2, accelerate=True)
    # 重启即按原价补做（不等第一个 tick，且先于配置对齐）
    assert eng2.comp_id == eng.comp_id and not eng2._unsettled and "RB0" not in eng2.accounts["ok"].positions
    eng2.source = LiveSource(eng2.source, [5000.0])   # 行情早已远离触发价
    eng2._tick()
    assert "RB0" not in eng2.accounts["ok"].positions and not eng2._unsettled
    sls = [t for t in db2.trades(eng2.comp_id, "ok", limit=5) if t["action"] == "stop_loss"]
    assert len(sls) == 1 and sls[0]["price"] <= 1000.0 and abs(sls[0]["ts"] - t_fail) < 2.0
    assert not journal.exists(), "补做成功后日志清除"
    eng2.stop()
    db2.close()


def test_pending_retry_settles_without_a_new_quote(tmp_path):
    eng, db = make_engine(tmp_path)
    _open_long_with_stop(eng)
    eng.source = LiveSource(eng.source, [3400.0, 1000.0])
    eng._tick()
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                  # 1000：触发，落库失败
    assert "ok" in eng._unsettled
    db.save_account = real_save  # type: ignore[method-assign]
    eng.source = LiveSource(eng.source.replay, [])   # 之后收盘 / 行情源不可用：没有新报价
    eng._tick()
    assert "RB0" not in eng.accounts["ok"].positions and not eng._unsettled, "数据库恢复后应立即按原价结算，不必等新行情"
    sl = next(t for t in db.trades(eng.comp_id, "ok", limit=5) if t["action"] == "stop_loss")
    assert sl["price"] <= 1000.0
    assert not eng._journal_path().exists()
    eng.stop()


def test_successful_trigger_leaves_no_journal(tmp_path):
    eng, db = make_engine(tmp_path)
    _open_long_with_stop(eng)
    eng.source = LiveSource(eng.source, [1000.0])
    eng._tick()                                  # 正常触发并落库
    assert "RB0" not in eng.accounts["ok"].positions and not eng._pending_marks
    assert not eng._journal_path().exists()
    eng.stop()
