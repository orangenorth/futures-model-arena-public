"""第二十八轮审查：结算后旧预写日志副本删不掉且墓碑也写不进去时，账户保持封锁并每个 tick 重试，清理成功后才恢复决策。"""
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
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB),
                 TraderCfg(id="other", name="OTHER", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


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


def test_account_stays_blocked_until_stale_journal_is_cleaned(tmp_path, monkeypatch):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    primary = tmp_path / "primary" / "j.json"
    fallback = tmp_path / "fallback" / "j.json"
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: [primary, fallback])
    eng.source = LiveSource(eng.source, [3400.0, 1000.0, 5000.0, 5000.0, 5000.0])
    eng._tick()                                   # 3400
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                   # 1000：触发，落库失败 → 主位置日志 {ok @1000}
    assert set(json.loads(primary.read_text(encoding="utf-8"))["marks"]) == {"ok"}
    # 此后整块磁盘不可写：主位置删不掉、两处都写不进去
    real_write = Engine._write_json_atomic
    real_unlink = Path.unlink
    blocked = {"on": True}

    def write(path, text):
        if blocked["on"] or path == primary:
            raise PermissionError("disk unavailable (simulated)")
        return real_write(path, text)

    def unlink(self, *a, **k):
        if self == primary:
            raise PermissionError("cannot delete (simulated)")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Engine, "_write_json_atomic", staticmethod(write))
    monkeypatch.setattr(Path, "unlink", unlink)
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                   # 5000：按原价 1000 补做，落库成功；但旧副本删不掉、墓碑写不进去
    assert "RB0" not in eng.accounts["ok"].positions and not eng._pending_marks
    assert eng._journal_stale == {"ok"} and "ok" in eng._unsettled, "日志未清理：账户必须保持封锁"
    assert any("墓碑" in e["message"] for e in db.events(eng.comp_id, 20))
    # 封锁期间：ok 的新开仓被拒绝；other 不受影响
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2500.0)]), snap, eng._generation)
    assert "RB0" not in eng.accounts["ok"].positions
    assert db.decisions(eng.comp_id, "ok", limit=1)[0]["results"][0]["accepted"] is False
    eng._finish_decision("other", Decision("other", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2500.0)]), snap, eng._generation)
    assert "RB0" in eng.accounts["other"].positions
    # 封锁期间崩溃重启：旧副本被恢复，但 ok 没有新持仓 → 重试无成交 → 解除，不产生凭空止损
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(eng.cfg, db2, accelerate=True)
    monkeypatch.setattr(eng2, "_journal_paths", lambda cid=None: [primary, fallback])
    eng2._restore_pending_marks(eng2.comp_id)
    assert "ok" in eng2._unsettled
    eng2.source = LiveSource(eng2.source, [5000.0])
    eng2._tick()
    assert sum(t["action"] == "stop_loss" for t in db2.trades(eng2.comp_id, "ok", limit=10)) == 1, "不得重复成交"
    eng2.stop()
    db2.close()
    # 磁盘恢复（备用位置可写）：下个 tick 写入墓碑，封锁解除，决策恢复
    blocked["on"] = False
    eng._tick()
    assert not eng._journal_stale and "ok" not in eng._unsettled
    tomb = json.loads(fallback.read_text(encoding="utf-8"))
    assert tomb["marks"] == {} and tomb["version"] > json.loads(primary.read_text(encoding="utf-8"))["version"]
    eng._finish_decision("ok", Decision("ok", 3, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2500.0)]), snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    monkeypatch.setattr(Path, "unlink", real_unlink)
    eng.stop()


def test_stop_retries_tombstone_for_stale_journal(tmp_path, monkeypatch):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    primary = tmp_path / "p" / "j.json"
    fallback = tmp_path / "f" / "j.json"
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: [primary, fallback])
    eng.source = LiveSource(eng.source, [3400.0, 1000.0, 5000.0])
    eng._tick()
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()
    real_write = Engine._write_json_atomic
    real_unlink = Path.unlink
    blocked = {"on": True}
    monkeypatch.setattr(Engine, "_write_json_atomic", staticmethod(lambda path, text: (_ for _ in ()).throw(PermissionError("ro")) if (blocked["on"] or path == primary) else real_write(path, text)))
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("ro")) if self == primary else real_unlink(self, *a, **k))
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()
    assert eng._journal_stale == {"ok"} and not fallback.exists()
    blocked["on"] = False                         # 关闭前磁盘恢复
    eng.stop()
    assert json.loads(fallback.read_text(encoding="utf-8"))["marks"] == {}, "stop() 应再尝试写入墓碑"
    monkeypatch.setattr(Path, "unlink", real_unlink)
