"""第四十四轮审查：关闭时补写失败的盯市更新仍写不进去，不能用兜底保存写出"旧最新价 + 新峰值"的半截状态；
这些更新转入预写日志，重启后按缓存价补做。"""
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
        if self.n >= len(self.prices):
            return {}
        px = self.prices[self.n]
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


def test_shutdown_journals_a_mark_it_still_cannot_write_instead_of_saving_half_state(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, [3400.0, 3600.0])
    eng._tick()                                              # 3400：落库成功
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                              # 3600：盯市更新落库失败
    high = eng._mark_save_failed["ok"]
    cid, journal = eng.comp_id, eng._journal_path()
    # 关闭：第一次补写仍失败，随后数据库恢复（兜底保存本可成功）
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real_save(*a, **k)

    db.save_account = flaky  # type: ignore[method-assign]
    eng.stop()
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3400.0 and saved.peak_equity < high, "不得写出旧最新价 + 新峰值的半截状态"
    assert journal.exists() and "ok" in json.loads(journal.read_text(encoding="utf-8"))["marks"], "转入预写日志"
    db.close()
    # 重启：按日志里的缓存价补做，最新价与高水位一起进库，日志清除
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(eng.cfg, db2, accelerate=True)
    acc2 = db2.load_accounts(cid)["ok"]
    assert acc2.positions["RB0"].last_price == 3600.0 and acc2.peak_equity == high
    assert not eng2._pending_marks and not eng2._unsettled and not journal.exists()
    eng2.stop()
    db2.close()
