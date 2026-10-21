"""第四十九轮审查：直接写入日志里的盯市更新时并入了重启后收到的报价/更高的高水位，这些值必须先进日志、再进数据库——
否则数据库提交后、日志清理前退出，重启会把日志里的旧价盖回新状态；日志写不进去则本次不写数据库。
（按新报价的盯市写入路径见 tests/test_regression50.py。）"""
import json
import time

import pytest

from arena.db import Database
from arena.engine.loop import Engine
from arena.models import Quote
from tests.test_regression45 import broken, shutdown_with_unwritten_mark


def test_merged_quotes_reach_the_journal_before_the_database(tmp_path, monkeypatch):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    db = Database(str(tmp_path / "e.db"))
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)                  # 重启时仍写不进去
    eng.quotes["RB0"] = Quote("RB0", 3700.0, time.time())   # 之后收到的报价（已在它的 tick 评估过、无成交）
    eng.prices["RB0"] = 3700.0
    assert json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]["prices"]["RB0"] == 3600.0
    # 预写日志写不进去：不能只把并入的报价写进数据库
    real_write = Engine._write_json_atomic
    blocked = {"on": True}
    monkeypatch.setattr(Engine, "_write_json_atomic", staticmethod(
        lambda path, text: (_ for _ in ()).throw(PermissionError("ro")) if blocked["on"] else real_write(path, text)))
    db.save_account = real_save  # type: ignore[method-assign]
    assert eng._apply_mark_update("ok") is False
    assert db.load_accounts(cid)["ok"].positions["RB0"].last_price == 3400.0
    assert eng._pending_mark_updates["ok"]["prices"]["RB0"] == 3600.0, "内存条目与磁盘一致"
    assert json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]["prices"]["RB0"] == 3600.0
    blocked["on"] = False
    # 数据库提交后、日志清理前退出：日志里已是并入后的价
    monkeypatch.setattr(Engine, "_settle_journal", lambda self, tid: (_ for _ in ()).throw(RuntimeError("crash (simulated)")))
    with pytest.raises(RuntimeError):
        eng._apply_mark_update("ok")
    entry = json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]
    assert entry["prices"]["RB0"] == 3700.0 and entry["peak"] == high
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3700.0 and saved.peak_equity == high
    monkeypatch.undo()
    # 重启重放这条日志：与数据库一致，不会把 3600 盖回去
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(cfg, db2, accelerate=True)
    saved = db2.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3700.0 and saved.peak_equity == high
    assert not eng2._pending_mark_updates and not journal.exists()
    eng2.stop()
    db2.close()
