"""第四十七轮审查：启动时关闭前未写入的盯市更新仍写不进去，而新配置移除了该品种或交易员：配置对齐同样推迟，
等更新写入后再对齐——否则会按陈旧的库内最后价平仓/删掉账户，随后重试找不到持仓或账户而清掉日志，记录的价永久丢失。"""
import sqlite3

from arena.config import TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from tests.conftest import restamp_competition_universe
from tests.test_regression45 import OPEN_RB, make_cfg, shutdown_with_unwritten_mark


def restart_with_ok_unwritable(tmp_path, cfg, resume_cid=None):
    """Restart under a new config with account "ok" unwritable; returns (db, engine, restore).

    resume_cid: record the identity the new configuration produces, so the engine
    resumes that competition.  A dropped symbol would otherwise roll over --
    correctly -- and the deferred reconciliation under test would never run.
    """
    db = Database(str(tmp_path / "e.db"))
    if resume_cid is not None:
        restamp_competition_universe(db, resume_cid, list(cfg.symbols))
    real_save = db.save_account

    def broken_for_ok(comp_id, acc, **kw):
        if acc.trader_id == "ok":
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real_save(comp_id, acc, **kw)

    db.save_account = broken_for_ok  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)

    def recover():
        db.save_account = real_save  # type: ignore[method-assign]

    return db, eng, recover


def test_symbol_removal_waits_for_the_journaled_mark_update(tmp_path):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    without_rb = make_cfg(tmp_path, symbols=("I0",), start={"I0": 800})   # 新配置移除了 RB0
    # The update is still unwritable at startup, so reconciliation is deferred.
    db, eng, recover = restart_with_ok_unwritable(tmp_path, without_rb, resume_cid=cid)
    assert "ok" in eng._pending_mark_updates and "ok" in eng._deferred_reconcile
    assert "RB0" in eng.accounts["ok"].positions and all(t["action"] == "open_long" for t in db.trades(cid, "ok", limit=5))
    recover()
    eng._tick()                                                            # 数据库恢复：先写入 3600，再按它平掉已移除品种
    saved = db.load_accounts(cid)["ok"]
    closes = [t for t in db.trades(cid, "ok", limit=5) if t["action"] == "close_long"]
    assert "RB0" not in saved.positions and saved.peak_equity == high
    assert len(closes) == 1 and abs(closes[0]["price"] - 3600.0) <= 5, "必须按日志里的最新价平仓，而不是陈旧的 3400"
    assert not eng._deferred_reconcile and not eng._pending_mark_updates and not journal.exists()
    eng.stop()


def test_trader_removal_waits_for_the_journaled_mark_update(tmp_path):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    without_ok = cfg.model_copy(update={"traders": [TraderCfg(id="other", name="O", provider="mock", mock_response=OPEN_RB)]})
    db, eng, recover = restart_with_ok_unwritable(tmp_path, without_ok)   # 启动时更新仍写不进去：不能先删账户
    assert "ok" in eng._pending_mark_updates and "ok" in eng._deferred_reconcile and "ok" in eng.accounts
    assert db.load_accounts(cid)["ok"].positions["RB0"].last_price == 3400.0
    recover()
    eng._tick()                                                            # 数据库恢复：先写入 3600 与高水位，再移除账户
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3600.0 and saved.peak_equity == high, "移除前日志里的价与高水位必须已进库"
    assert "ok" not in eng.accounts and not eng._deferred_reconcile and not eng._pending_mark_updates and not journal.exists()
    eng.stop()
