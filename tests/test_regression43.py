"""第四十三轮审查：只来了部分品种报价时，落库失败的盯市更新仍按完整缓存价补写；
失败那次的权益峰值并入之后任何一次成功的账户写入（决策落库、关闭时的最终持久化）。"""
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
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


class LiveSource:
    """按给定的报价序列（每个 tick 一个 {品种: 价格} 字典）报价的实时源。"""

    def __init__(self, replay, ticks):
        self.replay, self.ticks, self.n = replay, list(ticks), 0

    def latest(self, symbols):
        if self.n >= len(self.ticks):
            return {}
        px = self.ticks[self.n]
        self.n += 1
        return {s: Quote(s, p, time.time()) for s, p in px.items()}

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


def open_rb_then_fail_mark(tmp_path, ticks):
    """开多 RB0；按 ticks 报价，第二个 tick 的盯市更新落库失败。返回 (engine, db, real_save, 失败前的快照)。"""
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, ticks)
    eng._tick()                                              # ticks[0]：落库成功
    snap = eng._build_snapshot()                             # 在飞决策在写入失败前发起
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                              # ticks[1]：落库失败
    assert "ok" in eng._mark_save_failed
    return eng, db, real_save, snap


def test_failed_mark_is_rewritten_when_only_other_symbols_quote(tmp_path):
    eng, db, real_save, _ = open_rb_then_fail_mark(tmp_path, [{"RB0": 3450.0, "I0": 800.0}, {"RB0": 3500.0, "I0": 802.0}, {"I0": 805.0}])
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                              # 只来了 I0 的报价：RB0 那笔 3500 仍要按缓存价补写
    assert not eng._mark_save_failed
    assert eng.accounts["ok"].positions["RB0"].last_price == 3500.0
    assert db.load_accounts(eng.comp_id)["ok"].positions["RB0"].last_price == 3500.0
    eng.stop()


def test_decision_write_carries_the_remembered_peak(tmp_path):
    eng, db, real_save, snap = open_rb_then_fail_mark(tmp_path, [{"RB0": 3400.0, "I0": 800.0}, {"RB0": 3600.0, "I0": 800.0}])
    high = eng._mark_save_failed["ok"]
    assert high > eng.accounts["ok"].peak_equity
    db.save_account = real_save  # type: ignore[method-assign]
    # 数据库恢复后、下一次盯市之前，在飞的决策落库：高水位必须随这次写入进库
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=[ActionRequest("RB0", "adjust_sl_tp", stop_loss=3000.0)]), snap, eng._generation)
    assert db.load_accounts(eng.comp_id)["ok"].peak_equity == high
    eng.stop()


def test_shutdown_persists_failed_mark_and_peak(tmp_path):
    eng, db, real_save, _ = open_rb_then_fail_mark(tmp_path, [{"RB0": 3400.0, "I0": 800.0}, {"RB0": 3600.0, "I0": 800.0}])
    high = eng._mark_save_failed["ok"]
    db.save_account = real_save  # type: ignore[method-assign]
    eng.stop()                                               # 关闭：先按缓存价补写失败的盯市更新，再最终持久化
    saved = db.load_accounts(eng.comp_id)["ok"]
    assert saved.peak_equity == high and saved.positions["RB0"].last_price == 3600.0
    assert not eng._mark_save_failed
