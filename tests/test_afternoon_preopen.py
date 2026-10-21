"""13:30 午盘研究从未真正跑通。

13:30 午盘没有集合竞价，13:30 前商品最新的报价只能是 11:30 的收盘价，超过 `max_quote_age_sec`；
CTP 的源级 `last_ok`（最后一条 tick 的到达时刻）同样停在 11:30。调模型前那道闸门因此把每一场
午盘研究都记成 `unavailable`。修法只放宽这一处：此刻不在交易、且报价正好收在最近一节收盘上的
品种算就绪；源级 `last_ok` 只在此刻没有任何配置品种在交易时不看年龄，`ok` 照旧必须为真。
"""
from __future__ import annotations

from datetime import date

from arena.models import Quote

from .test_regression68 import _ctp_engine, _shanghai

DAY = date(2026, 9, 22)          # 周二，非节假日
OPENS_AT = _shanghai(DAY, 13, 30)
NOW = _shanghai(DAY, 13, 29, 10)


def _quote(symbol, ts, price=3400.0):
    return Quote(symbol=symbol, price=price, ts=ts, open_interest=1.0)


def test_a_commodity_quote_that_closed_the_morning_session_is_ready_for_the_afternoon(tmp_path, monkeypatch):
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        quotes = {"RB0": _quote("RB0", _shanghai(DAY, 11, 29, 58)),
                  "IF0": _quote("IF0", NOW - 5, 4200.0)}
        # 预约前的等待不变：仍按新鲜度等
        assert eng._preopen_unready_symbols(quotes, OPENS_AT, now=NOW) == ["RB0"]
        # 调模型前的闸门：11:30 的收盘价就是 13:30 前能有的最新价
        assert eng._preopen_unready_symbols(quotes, OPENS_AT, now=NOW, allow_prior_close=True) == []
    finally:
        eng.stop()
        db.close()


def test_an_intraday_or_older_quote_is_still_stale(tmp_path, monkeypatch):
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0"])
    try:
        for ts in (_shanghai(DAY, 10, 0),                 # 停在盘中：行情源上午就断了
                   _shanghai(DAY, 11, 20),                # 收盘前十分钟，超出提前量
                   _shanghai(date(2026, 9, 21), 15, 0),   # 昨天的收盘：中间整整漏了一节
                   _shanghai(DAY, 11, 32)):               # 收盘后还在变：不是上一节的收盘
            assert eng._preopen_unready_symbols({"RB0": _quote("RB0", ts)}, OPENS_AT, now=NOW,
                                                allow_prior_close=True) == ["RB0"], ts
        # 手动研究（没有这次要开的盘）不放宽
        assert eng._preopen_unready_symbols({"RB0": _quote("RB0", _shanghai(DAY, 11, 29, 58))}, None, now=NOW,
                                            allow_prior_close=True) == ["RB0"]
    finally:
        eng.stop()
        db.close()


def test_a_symbol_already_trading_needs_a_fresh_quote(tmp_path, monkeypatch):
    """13:00 之后股指在交易：它的 11:30 报价是陈旧，不是收盘价。"""
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        quotes = {"RB0": _quote("RB0", _shanghai(DAY, 11, 29, 58)),
                  "IF0": _quote("IF0", _shanghai(DAY, 11, 29, 58), 4200.0)}
        assert eng._preopen_unready_symbols(quotes, OPENS_AT, now=NOW, allow_prior_close=True) == ["IF0"]
    finally:
        eng.stop()
        db.close()


def test_index_futures_already_trading_keep_their_live_quote_in_the_afternoon_snapshot(tmp_path, monkeypatch):
    """反向护栏：13:00 之后的股指算"这次开盘要交易"的品种，不会被当成收盘品种去换核验收盘价、
    再因 13:xx 的 bar 晚于 11:30 而被丢出快照。"""
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0", "IF0"])
    try:
        assert set(eng._preopen_session_symbols(OPENS_AT)) == {"RB0", "IF0"}
        quotes = {"RB0": _quote("RB0", _shanghai(DAY, 11, 29, 58)),
                  "IF0": _quote("IF0", NOW - 5, 4200.0)}
        assert eng._closed_session_research_quotes(quotes, OPENS_AT, now=NOW) == quotes
    finally:
        eng.stop()
        db.close()


def test_the_source_gate_ignores_last_ok_age_only_while_nothing_is_trading(tmp_path, monkeypatch):
    lunch = {"ok": True, "last_ok": _shanghai(DAY, 11, 29, 59)}
    eng, db = _ctp_engine(tmp_path, monkeypatch, ["RB0"])
    try:
        assert eng._preopen_source_ready(lunch, OPENS_AT, now=NOW), "午休没有推送，last_ok 停在 11:30 是预期内"
        assert not eng._preopen_source_ready({**lunch, "ok": False}, OPENS_AT, now=NOW), "断线仍要挡住"
        assert not eng._preopen_source_ready(lunch, None, now=NOW), "手动研究不放宽"
        # 盘中（有品种在交易）last_ok 超窗就是行情断了
        assert not eng._preopen_source_ready(lunch, OPENS_AT, now=_shanghai(DAY, 14, 0))
    finally:
        eng.stop()
        db.close()

    eng, db = _ctp_engine(tmp_path / "mixed", monkeypatch, ["RB0", "IF0"])
    try:
        # 股指 13:00 已开：没有推送说明源出了问题，不放宽
        assert not eng._preopen_source_ready(lunch, OPENS_AT, now=NOW)
    finally:
        eng.stop()
        db.close()
