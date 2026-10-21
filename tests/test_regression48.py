"""第四十八轮审查：实时源重启后日志里的盯市更新仍写不进去时，该账户的决策动作与触发待落库时一样只记拒绝审计——
否则数据库恰在决策落库时恢复、决策改了持仓，之后写入的日志价会盖掉更新后的状态；直接写入时并入重启后收到的报价与高水位。"""
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision
from tests.test_regression45 import LiveSource, broken, shutdown_with_unwritten_mark


def open_rb():
    return [ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]


def test_decisions_wait_for_the_journaled_mark_update(tmp_path):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    db = Database(str(tmp_path / "e.db"))
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)                  # 重启时仍写不进去
    eng.source = LiveSource(eng.source, [3700.0])
    eng._tick()                                             # 新报价 3700 的盯市写入也失败
    assert "ok" in eng._pending_mark_updates and eng._mark_save_failed["ok"] > high
    peak_3700 = eng._mark_save_failed["ok"]
    qty = eng.accounts["ok"].positions["RB0"].qty
    db.save_account = real_save  # type: ignore[method-assign]   # 数据库恰在决策落库时恢复
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 2, 0, actions=open_rb()), snap, eng._generation)
    dec = db.decisions(cid, "ok", limit=1)[0]
    assert dec["results"][0]["accepted"] is False and "盯市更新" in dec["results"][0]["reason"]
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].qty == qty and saved.positions["RB0"].last_price == 3400.0, "写入前不能改持仓"
    assert len(db.trades(cid, "ok", limit=5)) == 1
    eng._tick()                                             # 无新报价：写入日志里的更新，并入之后收到的 3700 与其高水位
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3700.0 and saved.peak_equity == peak_3700
    assert not eng._pending_mark_updates and not eng._mark_save_failed and not journal.exists()
    eng._finish_decision("ok", Decision("ok", 3, 0, actions=open_rb()), snap, eng._generation)   # 解除后照常执行
    assert db.decisions(cid, "ok", limit=1)[0]["results"][0]["accepted"] is True
    assert db.load_accounts(cid)["ok"].positions["RB0"].qty > qty
    eng.stop()
