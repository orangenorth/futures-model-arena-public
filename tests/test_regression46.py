"""第四十六轮审查：关闭前未写入的盯市更新落库后日志清不掉（旧副本删不掉、墓碑写不进去）时账户同样封锁；
回放位置先写进 kv 再清日志，只剩位置的空日志/墓碑也能恢复位置；比 kv 旧的日志位置作废、不让回放倒退或停住。"""
import json
import sqlite3
from pathlib import Path

from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision
from tests.test_regression45 import (LiveSource, broken, liquidations, make_cfg, shutdown_with_retained_replay_bar,
                                 shutdown_with_unwritten_mark)


def test_mark_update_journal_cleanup_failure_blocks_the_account(tmp_path, monkeypatch):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    db = Database(str(tmp_path / "e.db"))
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)                  # 重启时仍写不进去：条目保留
    assert "ok" in eng._pending_mark_updates
    real_write = Engine._write_json_atomic
    real_unlink = Path.unlink
    blocked = {"on": True}
    monkeypatch.setattr(Engine, "_write_json_atomic", staticmethod(
        lambda path, text: (_ for _ in ()).throw(PermissionError("ro")) if blocked["on"] else real_write(path, text)))
    monkeypatch.setattr(Path, "unlink",
                        lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("ro")) if self == journal else real_unlink(self, *a, **k))
    db.save_account = Database(str(tmp_path / "e.db")).save_account  # type: ignore[method-assign]
    eng.source = LiveSource(eng.source, [])                 # 没有新报价：本 tick 只写入日志里的更新
    eng._tick()                                             # 更新写入数据库成功；旧日志删不掉、墓碑写不进去
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3600.0 and saved.peak_equity == high and not eng._pending_mark_updates
    assert eng._journal_stale == {"ok"} and "ok" in eng._unsettled and journal.exists(), "日志未清理：账户必须保持封锁"
    assert any("墓碑" in e["message"] for e in db.events(cid, 20))
    snap = eng._build_snapshot()                            # 封锁期间：决策动作只记拒绝审计
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=[ActionRequest("RB0", "close", 1.0)]), snap, eng._generation)
    assert "RB0" in eng.accounts["ok"].positions and db.decisions(cid, "ok", limit=1)[0]["results"][0]["accepted"] is False
    blocked["on"] = False                                   # 磁盘恢复：下个 tick 写入墓碑，封锁解除
    eng._tick()
    assert not eng._journal_stale and "ok" not in eng._unsettled
    assert json.loads(journal.read_text(encoding="utf-8"))["marks"] == {}
    monkeypatch.setattr(Path, "unlink", real_unlink)
    eng.stop()


def test_replay_cursor_survives_a_crash_between_the_last_journal_write_and_the_kv_write(tmp_path):
    cid, journal, high, pos3, cfg = shutdown_with_retained_replay_bar(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    real_kv_set = db.kv_set

    def kv_broken(key, value):
        if key.startswith("replay_pos:"):
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real_kv_set(key, value)

    db.kv_set = kv_broken  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)                  # 日志里的更新写入成功，但回放位置写不进 kv
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == pos3["symbols"]["RB0"]["close"] and saved.peak_equity == high
    assert eng._cursor_pending and eng._journal_replay_pos is not None and not eng._pending_mark_updates
    data = json.loads(journal.read_text(encoding="utf-8"))
    assert data["marks"] == {} and data["replay_pos"]["steps"] == pos3["steps"], "位置还没进 kv：只剩位置的日志不能删"
    assert json.loads(db.kv_get(f"replay_pos:{cid}"))["steps"] == pos3["steps"] - 1
    # 此刻崩溃（不 stop）：重启后从空日志里的位置恢复，写进 kv，不重放 bar 3
    strict = make_cfg(tmp_path, liquidation_risk_ratio=0.0001)
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(strict, db2, accelerate=True)
    assert eng2.source.position()["steps"] == pos3["steps"]
    assert json.loads(db2.kv_get(f"replay_pos:{cid}"))["steps"] == pos3["steps"]
    assert eng2._journal_replay_pos is None and not eng2._cursor_pending and not journal.exists()
    eng2._tick()                                            # 取 bar 4：新规则下的强平不落在 bar 3 上
    assert eng2.source.position()["steps"] == pos3["steps"] + 1
    liq = liquidations(db2, cid)
    assert liq and all(t["ts"] > pos3["symbols"]["RB0"]["ts"] for t in liq)
    eng2.stop()
    db2.close()


def test_stale_journal_cursor_never_rewinds_or_holds_the_replay(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    early = eng.source.position()
    eng._tick()
    eng._tick()
    latest = eng.source.position()
    cid, journal = eng.comp_id, eng._journal_path()
    eng.stop()
    db.close()
    assert json.loads(Database(str(tmp_path / "e.db")).kv_get(f"replay_pos:{cid}"))["steps"] == latest["steps"]
    journal.write_text(json.dumps({"version": 1.0, "marks": {}, "replay_pos": early}), encoding="utf-8")   # 残留的旧墓碑
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(cfg, db2, accelerate=True)
    assert eng2.source.position()["steps"] == latest["steps"], "不能被旧位置拉回去"
    assert eng2._journal_replay_pos is None and not eng2._cursor_pending and not journal.exists()
    eng2._tick()
    assert eng2.source.position()["steps"] == latest["steps"] + 1, "正常前进，没有被旧位置停住"
    eng2.stop()
    db2.close()
