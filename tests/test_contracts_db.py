from datetime import datetime

from arena.db import Database
from arena.exchange.contracts import session_open, next_open
from arena.models import Account, Position, Trade, Decision, ActionRequest, EquityPoint


def test_sessions(cn_specs):
    rb = cn_specs["RB0"]
    hol = ["2026-10-01"]
    assert session_open(rb, datetime(2026, 9, 16, 9, 30), hol)        # 周三日盘
    assert not session_open(rb, datetime(2026, 9, 16, 12, 0), hol)    # 午休
    assert session_open(rb, datetime(2026, 9, 16, 21, 30), hol)       # 夜盘
    assert not session_open(rb, datetime(2026, 9, 19, 10, 0), hol)    # 周六
    assert not session_open(rb, datetime(2026, 10, 1, 10, 0), hol)    # 节假日
    au = cn_specs["AU0"]
    assert session_open(au, datetime(2026, 9, 17, 1, 30), hol)        # 黄金夜盘跨日至 02:30
    assert not session_open(rb, datetime(2026, 9, 17, 1, 30), hol)    # 螺纹 23:00 收
    assert not session_open(au, datetime(2026, 9, 20, 1, 30), hol)    # 周日凌晨（周六无夜盘）
    nx = next_open(rb, datetime(2026, 9, 19, 10, 0), hol)
    assert nx == datetime(2026, 9, 21, 9, 0)


def test_db_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    cid = db.create_competition("c", "replay", 1000, "{}")
    assert db.active_competition()["id"] == cid
    acc = Account("a", 990, 1000, realized_pnl=-5, fees=5)
    acc.positions["RB0"] = Position("a", "RB0", "long", 2, 3400, 8840, stop_loss=3300)
    db.save_account(cid, acc)
    loaded = db.load_accounts(cid)["a"]
    assert loaded.cash == 990 and loaded.positions["RB0"].stop_loss == 3300
    db.add_trade(cid, Trade("a", "RB0", "buy", "open_long", 2, 3400, 1, 0, 1.0, 1))
    assert db.trade_count(cid, "a") == 1
    d = Decision("a", 1, 1.0, analysis="x", actions=[ActionRequest("RB0", "open_long", 0.1)], results=[{"ok": True}])
    db.add_decision(cid, d)
    ds = db.decisions(cid, "a")
    assert ds[0]["actions"][0]["symbol"] == "RB0" and ds[0]["results"][0]["ok"]
    db.add_equity(cid, [EquityPoint("a", 1.0, 1000, 990, 0, 10), EquityPoint("a", 2.0, 1010, 990, 0, 20)])
    assert db.equity_values(cid, "a") == [1000, 1010]
    # 新竞赛后旧竞赛结束
    cid2 = db.create_competition("c2", "replay", 1000, "{}")
    assert db.active_competition()["id"] == cid2
    assert [c["status"] for c in db.list_competitions()] == ["running", "ended"]
    db.close()
