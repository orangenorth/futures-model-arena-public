"""第二十五轮审查：预写日志写入必须被验证；数据目录不可写时退到备用位置；两处都失败仍尝试落库并保持待重试。"""
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


def _armed_engine(tmp_path):
    """开多（止损 2000 已落库），切到实时源：3400 → 1000（触发）。"""
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, [3400.0, 1000.0])
    eng._tick()
    return eng, db


def test_journal_falls_back_when_data_directory_is_unwritable(tmp_path, monkeypatch):
    eng, db = _armed_engine(tmp_path)
    primary = tmp_path / "nowhere" / "pending_marks.json"          # 目录不存在且不会创建成功（用只读/写满模拟）
    fallback = tmp_path / "fallback" / "pending_marks.json"
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **k):
        if self == primary.parent:
            raise PermissionError("read-only data directory (simulated)")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: [primary, fallback])
    db.save_account = broken  # type: ignore[method-assign]
    db.kv_set = broken        # type: ignore[method-assign]
    eng._tick()                                   # 1000：触发，主位置不可写 → 备用位置；落库失败
    assert not primary.exists() and fallback.exists()
    assert json.loads(fallback.read_text(encoding="utf-8"))["marks"]["ok"]["prices"]["RB0"] == 1000.0
    # 崩溃重启：从备用位置恢复
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(eng.cfg, db2, accelerate=True)
    monkeypatch.setattr(eng2, "_journal_paths", lambda cid=None: [primary, fallback])
    eng2._restore_pending_marks(eng2.comp_id)
    assert "ok" in eng2._unsettled
    eng2.source = LiveSource(eng2.source, [5000.0])
    eng2._tick()
    assert "RB0" not in eng2.accounts["ok"].positions
    assert next(t for t in db2.trades(eng2.comp_id, "ok", limit=5) if t["action"] == "stop_loss")["price"] <= 1000.0
    assert not fallback.exists(), "结算后备用位置的日志也清除"
    eng2.stop()
    db2.close()


def test_journal_failure_everywhere_still_attempts_settlement_and_keeps_retrying(tmp_path, monkeypatch):
    eng, db = _armed_engine(tmp_path)
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: [tmp_path / "ro1" / "j.json", tmp_path / "ro2" / "j.json"])
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("disk full (simulated)")))
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                   # 1000：日志两处都失败，落库也失败 → 仅存内存、待重试
    assert "RB0" in eng.accounts["ok"].positions and "ok" in eng._unsettled
    assert any("预写日志也未能写入" in e["message"] for e in db.events(eng.comp_id, 20))
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                   # 数据库恢复（日志仍写不进去）：落库成功本身即持久化
    assert "RB0" not in eng.accounts["ok"].positions and not eng._unsettled
    assert next(t for t in db.trades(eng.comp_id, "ok", limit=5) if t["action"] == "stop_loss")["price"] <= 1000.0
    eng.stop()


def test_journal_failure_with_working_database_settles_durably(tmp_path, monkeypatch):
    eng, db = _armed_engine(tmp_path)
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: [tmp_path / "ro1" / "j.json", tmp_path / "ro2" / "j.json"])
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("disk full (simulated)")))
    eng._tick()                                   # 日志写不进去，但落库成功：成交已持久化，无待重试
    assert "RB0" not in eng.accounts["ok"].positions and not eng._pending_marks and not eng._unsettled
    assert any(t["action"] == "stop_loss" for t in db.trades(eng.comp_id, "ok", limit=5))
    eng.stop()


def test_stop_retries_journal_for_pending_marks(tmp_path, monkeypatch):
    eng, db = _armed_engine(tmp_path)
    paths = [tmp_path / "ro" / "j.json"]
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: paths)
    blocked = {"on": True}
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **k):
        if blocked["on"]:
            raise PermissionError("disk full (simulated)")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                   # 日志与落库都失败
    assert "ok" in eng._pending_marks and not paths[0].exists()
    blocked["on"] = False                         # 关闭前磁盘恢复
    eng.stop()
    assert paths[0].exists() and "ok" in json.loads(paths[0].read_text(encoding="utf-8"))["marks"], "stop() 应再尝试写入预写日志"
