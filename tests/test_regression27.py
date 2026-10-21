"""第二十七轮审查：结算后旧的预写日志副本删不掉时，必须写版本更新的空墓碑，重启不得把旧触发价套到新持仓上。"""
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


def test_tombstone_written_when_stale_journal_copy_cannot_be_deleted(tmp_path, monkeypatch):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    primary = tmp_path / "primary" / "j.json"
    fallback = tmp_path / "fallback" / "j.json"
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: [primary, fallback])
    eng.source = LiveSource(eng.source, [3400.0, 1000.0, 5000.0, 5000.0])
    eng._tick()                                   # 3400
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                   # 1000：触发，落库失败 → 主位置日志 {ok @1000}
    assert set(json.loads(primary.read_text(encoding="utf-8"))["marks"]) == {"ok"}
    # 主位置目录此后只读：既写不进去也删不掉
    real_write = Engine._write_json_atomic
    real_unlink = Path.unlink
    monkeypatch.setattr(Engine, "_write_json_atomic", staticmethod(lambda path, text: (_ for _ in ()).throw(PermissionError("ro")) if path == primary else real_write(path, text)))
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("ro")) if self == primary else real_unlink(self, *a, **k))
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                   # 5000：按原价 1000 补做成功 → 待重试清空；主位置旧副本删不掉 → 备用位置写空墓碑
    assert "RB0" not in eng.accounts["ok"].positions and not eng._pending_marks
    assert primary.exists(), "旧副本仍在（删不掉）"
    tomb = json.loads(fallback.read_text(encoding="utf-8"))
    assert tomb["marks"] == {} and tomb["version"] > json.loads(primary.read_text(encoding="utf-8"))["version"], "应写入版本更新的空墓碑"
    # 之后重新开仓（止损 2500），然后崩溃重启：不能把旧副本里的 1000 套到新持仓上
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2500.0)]), snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(eng.cfg, db2, accelerate=True)
    monkeypatch.setattr(eng2, "_journal_paths", lambda cid=None: [primary, fallback])
    eng2._restore_pending_marks(eng2.comp_id)
    assert not eng2._pending_marks and not eng2._unsettled, "墓碑为最新：不得恢复旧的触发"
    eng2.source = LiveSource(eng2.source, [5000.0])
    eng2._tick()
    assert "RB0" in eng2.accounts["ok"].positions, "新持仓不应被旧触发价凭空止损"
    assert sum(t["action"] == "stop_loss" for t in db2.trades(eng2.comp_id, "ok", limit=10)) == 1
    eng2.stop()
    db2.close()


def test_no_tombstone_when_all_copies_are_removed(tmp_path, monkeypatch):
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
    assert primary.exists()
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()
    assert not primary.exists() and not fallback.exists(), "都能删掉时不留任何文件"
    eng.stop()
