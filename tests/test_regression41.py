"""第四十一轮审查：实时源没有新报价时也按缓存价补写失败的无成交盯市更新；失败那次算出的权益峰值在重试时带上，
即使在飞的决策已平掉持仓，高水位也不会丢。"""
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
    """按给定价格序列报价的实时源；序列用完后不再有报价（收盘 / 行情源故障）。"""

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


def test_failed_live_mark_is_rewritten_from_cached_prices_without_a_new_quote(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, [3450.0, 3500.0])    # 之后再无报价
    eng._tick()                                              # 3450：落库成功
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                              # 3500：最后一个报价，盯市更新落库失败
    assert "ok" in eng._mark_save_failed and eng.accounts["ok"].positions["RB0"].last_price == 3450.0
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                              # 没有新报价：按缓存的 3500 补写
    assert not eng._mark_save_failed
    assert eng.accounts["ok"].positions["RB0"].last_price == 3500.0
    assert db.load_accounts(eng.comp_id)["ok"].positions["RB0"].last_price == 3500.0
    eng.stop()


def test_high_water_mark_survives_an_in_flight_close(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, [3400.0, 3600.0, 3400.0])
    eng._tick()                                              # 3400
    snap = eng._build_snapshot()                             # 决策在写入失败前发起
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                              # 3600：权益创新高，但落库失败
    acc = eng.accounts["ok"]
    high = acc.cash + acc.positions["RB0"].unrealized(3600.0, eng.specs["RB0"].multiplier)
    assert eng._mark_save_failed["ok"] == high > acc.peak_equity, "失败那次的高水位被记住，内存里的峰值仍是旧值"
    db.save_account = real_save  # type: ignore[method-assign]
    # 在飞的决策此时返回并平掉持仓、落库：平仓扣了手续费，之后的权益永远算不出 3600 时的高水位
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=[ActionRequest("RB0", "close")]), snap, eng._generation)
    assert not eng.accounts["ok"].positions
    assert db.load_accounts(eng.comp_id)["ok"].peak_equity == high, "决策落库这一次写入就已带上高水位"
    eng._tick()                                              # 3400：重试确认高水位仍在并解除标记
    assert not eng._mark_save_failed
    assert eng.accounts["ok"].peak_equity == high and db.load_accounts(eng.comp_id)["ok"].peak_equity == high
    eng.stop()
