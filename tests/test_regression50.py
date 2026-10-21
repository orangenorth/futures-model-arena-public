"""第五十轮审查：日志里的盯市更新还没写进去时，按新报价的无成交盯市写入同样先把并入后的值写进日志、再写数据库；
日志写不进去则本次不写数据库——否则数据库领先于日志，清理前退出的话重启会把日志里的旧价盖回去。"""
import json
import sqlite3

import pytest

from arena.db import Database
from arena.engine.loop import Engine
from tests.test_regression45 import LiveSource, shutdown_with_unwritten_mark


def fail_unless_fresh(db):
    """直接写入（RB0 还是日志里的 3600）失败；带新报价 3700 的盯市写入成功。"""
    real_save = db.save_account

    def save(comp_id, acc, **kw):
        if acc.positions["RB0"].last_price != 3700.0:
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real_save(comp_id, acc, **kw)

    db.save_account = save  # type: ignore[method-assign]


def test_fallback_mark_save_journals_the_merged_update_first(tmp_path, monkeypatch):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    db = Database(str(tmp_path / "e.db"))
    fail_unless_fresh(db)
    eng = Engine(cfg, db, accelerate=True)                  # 启动时直接写入失败
    assert "ok" in eng._pending_mark_updates
    # 1) 预写日志写不进去：带新报价的盯市不能只写数据库
    real_write = Engine._write_json_atomic
    blocked = {"on": True}
    monkeypatch.setattr(Engine, "_write_json_atomic", staticmethod(
        lambda path, text: (_ for _ in ()).throw(PermissionError("ro")) if blocked["on"] else real_write(path, text)))
    eng.source = LiveSource(eng.source, [3700.0])
    eng._tick()
    assert db.load_accounts(cid)["ok"].positions["RB0"].last_price == 3400.0 and "ok" in eng._pending_mark_updates
    assert json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]["prices"]["RB0"] == 3600.0
    peak_3700 = eng._mark_save_failed["ok"]
    assert peak_3700 > high
    blocked["on"] = False
    # 2) 重启后同样的情形（重启时没有报价，直接写入仍是 3600 而失败），日志可写，但数据库提交后、清理前退出
    db2 = Database(str(tmp_path / "e.db"))
    fail_unless_fresh(db2)
    eng2 = Engine(cfg, db2, accelerate=True)
    assert "ok" in eng2._pending_mark_updates
    eng2.source = LiveSource(eng2.source, [3700.0])
    monkeypatch.setattr(Engine, "_settle_journal", lambda self, tid: (_ for _ in ()).throw(RuntimeError("crash (simulated)")))
    with pytest.raises(RuntimeError):
        eng2._tick()
    entry = json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]
    assert entry["prices"]["RB0"] == 3700.0 and entry["peak"] == peak_3700, "数据库提交前日志里已是并入后的值"
    saved = db2.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3700.0 and saved.peak_equity == peak_3700
    monkeypatch.undo()
    # 3) 再次重启：重放这条日志与数据库一致，不会把 3600 盖回去
    db3 = Database(str(tmp_path / "e.db"))
    eng3 = Engine(cfg, db3, accelerate=True)
    saved = db3.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3700.0 and saved.peak_equity == peak_3700
    assert not eng3._pending_mark_updates and not journal.exists()
    eng3.stop()
    db3.close()
