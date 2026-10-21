"""第二十六轮审查：预写日志两处并存时恢复取最新版本；备用位置按数据库隔离。"""
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


def make_engine(tmp_path, db_name="e.db"):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="a", name="A", provider="mock", mock_response=OPEN_RB),
                 TraderCfg(id="b", name="B", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / db_name)},
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


def test_restore_prefers_newest_journal_copy_over_stale_primary(tmp_path, monkeypatch):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    # a 的止损 2000，b 的止损 500：价格 1000 只触发 a，价格 100 再触发 b
    eng._finish_decision("a", Decision("a", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    eng._finish_decision("b", Decision("b", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=500.0)]), snap, eng._generation)
    primary = tmp_path / "primary" / "j.json"
    fallback = tmp_path / "fallback" / "j.json"
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: [primary, fallback])
    eng.source = LiveSource(eng.source, [3400.0, 1000.0, 100.0])
    eng._tick()                                   # 3400
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                   # 1000：a 触发，落库失败 → 主位置日志 {a}
    assert set(json.loads(primary.read_text(encoding="utf-8"))["marks"]) == {"a"} and not fallback.exists()
    # 之后主位置变得不可写（且旧文件删不掉）
    real_write = Engine._write_json_atomic

    def write(path, text):
        if path == primary:
            raise PermissionError("primary unwritable (simulated)")
        return real_write(path, text)

    monkeypatch.setattr(Engine, "_write_json_atomic", staticmethod(write))
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self == primary:
            raise PermissionError("cannot delete stale primary (simulated)")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    eng._tick()                                   # 100：a 重试仍失败、b 也触发失败 → 备用位置日志 {a, b}，主位置仍是旧的 {a}
    assert set(json.loads(fallback.read_text(encoding="utf-8"))["marks"]) == {"a", "b"}
    assert set(json.loads(primary.read_text(encoding="utf-8"))["marks"]) == {"a"}
    assert json.loads(fallback.read_text(encoding="utf-8"))["version"] > json.loads(primary.read_text(encoding="utf-8"))["version"]
    # 崩溃重启：必须以最新的备用副本为准，b 的触发不能丢
    monkeypatch.setattr(Path, "unlink", real_unlink)
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(eng.cfg, db2, accelerate=True)
    monkeypatch.setattr(eng2, "_journal_paths", lambda cid=None: [primary, fallback])
    eng2._restore_pending_marks(eng2.comp_id)
    assert eng2._unsettled == {"a", "b"}
    assert eng2._pending_marks["b"][0]["RB0"] == 100.0 and eng2._pending_marks["a"][0]["RB0"] == 1000.0
    eng2.source = LiveSource(eng2.source, [5000.0])
    eng2._tick()
    fills = {t["trader_id"]: t for t in db2.trades(eng2.comp_id, limit=10) if t["action"] == "stop_loss"}
    assert fills["a"]["price"] <= 1000.0 and fills["b"]["price"] <= 100.0
    assert not eng2._unsettled
    eng2.stop()
    db2.close()


def test_equal_versions_are_merged_and_legacy_format_still_restores(tmp_path, monkeypatch):
    eng, db = make_engine(tmp_path)
    eng._tick()
    primary = tmp_path / "p" / "j.json"
    fallback = tmp_path / "f" / "j.json"
    monkeypatch.setattr(eng, "_journal_paths", lambda cid=None: [primary, fallback])
    entry = {"prices": {"RB0": 1000.0}, "clock": time.time(), "clocks": None}
    primary.parent.mkdir()
    fallback.parent.mkdir()
    primary.write_text(json.dumps({"version": 5.0, "marks": {"a": entry}}), encoding="utf-8")
    fallback.write_text(json.dumps({"version": 5.0, "marks": {"b": entry}}), encoding="utf-8")
    eng._restore_pending_marks(eng.comp_id)
    assert eng._unsettled == {"a", "b"}
    eng._pending_marks.clear()
    eng._unsettled.clear()
    primary.write_text(json.dumps({"a": entry}), encoding="utf-8")   # 旧格式：无版本号
    fallback.unlink()
    eng._restore_pending_marks(eng.comp_id)
    assert eng._unsettled == {"a"}
    eng.stop()


def test_fallback_journal_is_namespaced_by_database(tmp_path):
    eng1, db1 = make_engine(tmp_path / "one")
    eng2, db2 = make_engine(tmp_path / "two")
    f1, f2 = eng1._journal_paths()[1], eng2._journal_paths()[1]
    assert eng1.comp_id == eng2.comp_id, "两个数据库的竞赛 id 相同，只靠 id 会撞名"
    assert f1 != f2 and f1.name == f2.name
    assert f1.parent.parent == f2.parent.parent
    eng3, db3 = make_engine(tmp_path / "one")    # 同一数据库 → 同一备用位置
    assert eng3._journal_paths()[1] == f1
    for e in (eng1, eng2, eng3):
        e.stop()
