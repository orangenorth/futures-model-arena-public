"""Night-session and overnight closed-window coverage for the full-universe scan.

Before this change the scanner accepted a closed-session snapshot only during
the intra-day recesses and between the final day close and 21:00.  During any
night session that excluded every candidate without a night session, and the
still-forming 5-minute bar (the public feed stamps it with its closing minute)
turned every actively trading candidate into ``history_invalid``.  The pre-open
gate requires zero runtime exclusions, so no full-universe research could be
produced at night or before the morning open.  These tests pin the repaired
behaviour and its limits.
"""
from __future__ import annotations

from arena.db import Database
from arena.models import Bar, Quote
from arena.preopen_universe import build_preopen_universe_research
from arena.research_scan import HISTORY_BAR_INTERVAL_SEC, MAX_FUTURE_SKEW_SEC
from tests.test_research_scan import (
    FakeReader,
    bars_at,
    candidate,
    china_timestamp,
    run,
    write_calendar,
    write_catalogue,
)


def quote(symbol: str, stamp: str) -> Quote:
    return Quote(symbol=symbol, price=100.0, ts=china_timestamp(stamp), change_pct=0.5)


def scan(tmp_path, candidates: dict, quotes: dict, histories: dict, *, now: str, holidays=()):
    catalogue_path = tmp_path / "catalogue.json"
    calendar_path = tmp_path / "calendar.json"
    write_catalogue(catalogue_path, candidates)
    write_calendar(calendar_path, holidays=holidays)
    manifest = run(catalogue_path, FakeReader(quotes, histories), now=china_timestamp(now),
                   calendar_path=calendar_path)
    return manifest, {item["symbol"]: item for item in manifest["items"]}


def test_day_close_snapshots_stay_usable_during_the_night_session(tmp_path):
    # 2026-09-23 is a Wednesday; the night session is live for part of the catalogue only.
    candidates = {
        "AA0": candidate("AA0", exchange="SHFE"),    # no night session: last observation is the 15:00 close
        "AB0": candidate("AB0", exchange="CFFEX"),   # CFFEX closes at 15:15 and never trades at night
        "AC0": candidate("AC0", exchange="SHFE"),    # trading right now
    }
    quotes = {"AA0": quote("AA0", "2026-09-23 15:00:00"), "AB0": quote("AB0", "2026-09-23 15:15:00"),
              "AC0": quote("AC0", "2026-09-23 21:35:20")}
    histories = {"AA0": bars_at(china_timestamp("2026-09-23 15:00:00")),
                 "AB0": bars_at(china_timestamp("2026-09-23 15:15:00")),
                 "AC0": bars_at(china_timestamp("2026-09-23 21:35:00"))}

    manifest, rows = scan(tmp_path, candidates, quotes, histories, now="2026-09-23 21:35:32")

    assert {symbol: row["outcome"] for symbol, row in rows.items()} == {"AA0": "scanned", "AB0": "scanned", "AC0": "scanned"}
    assert manifest["status"] == "complete"
    assert manifest["rules_version"] == "research-scan-v6"
    assert manifest["market"]["freshness_mode"] == "mixed"
    assert manifest["market"]["prior_session_count"] == 2
    assert manifest["market"]["live_session_count"] == 1
    assert manifest["market"]["quote_as_of_min"] == china_timestamp("2026-09-23 15:00:00")
    assert manifest["market"]["quote_as_of_max"] == china_timestamp("2026-09-23 21:35:20")

    db = Database(tmp_path / "night.db")
    try:
        db.append_research_scan(manifest)
        latest = db.latest_research_scan()
    finally:
        db.close()
    assert latest and latest["market"] == manifest["market"]
    research = build_preopen_universe_research(latest)
    assert research["scope_status"] == "complete"
    assert research["counts"]["runtime_excluded"] == 0


def test_overnight_closes_stay_usable_until_the_next_morning_open(tmp_path):
    candidates = {
        "AA0": candidate("AA0", exchange="SHFE"),    # night session ends 23:00
        "AB0": candidate("AB0", exchange="SHFE"),    # night session ends 01:00
        "AC0": candidate("AC0", exchange="INE"),     # night session ends 02:30
        "AD0": candidate("AD0", exchange="DCE"),     # no night session
        "AE0": candidate("AE0", exchange="CFFEX"),
    }
    stamps = {"AA0": "2026-09-23 23:00:00", "AB0": "2026-09-24 01:00:00", "AC0": "2026-09-24 02:30:00",
              "AD0": "2026-09-23 15:00:00", "AE0": "2026-09-23 15:15:00"}
    quotes = {symbol: quote(symbol, stamp) for symbol, stamp in stamps.items()}
    histories = {symbol: bars_at(china_timestamp(stamp)) for symbol, stamp in stamps.items()}

    manifest, rows = scan(tmp_path, candidates, quotes, histories, now="2026-09-24 08:45:00")
    assert all(row["outcome"] == "scanned" for row in rows.values()), rows
    assert manifest["market"]["freshness_mode"] == "prior_session"
    assert manifest["market"]["prior_session_count"] == 5
    assert build_preopen_universe_research(manifest)["scope_status"] == "complete"

    # Once the commodity exchanges reopen at 09:00 the same observations are stale;
    # CFFEX only opens at 09:30, so its close is still the immediately preceding session.
    _manifest, reopened = scan(tmp_path, candidates, quotes, histories, now="2026-09-24 09:05:00")
    assert {symbol: row["reason"] for symbol, row in reopened.items() if symbol != "AE0"} == {
        "AA0": "quote_stale", "AB0": "quote_stale", "AC0": "quote_stale", "AD0": "quote_stale",
    }
    assert reopened["AE0"]["outcome"] == "scanned"

    _manifest, cffex_open = scan(tmp_path, {"AE0": candidates["AE0"]}, quotes, histories, now="2026-09-24 09:35:00")
    assert cffex_open["AE0"]["reason"] == "quote_stale"


def test_a_close_the_exchange_has_already_traded_past_is_never_promoted(tmp_path):
    candidates = {symbol: candidate(symbol, exchange="SHFE") for symbol in ("AA0", "AB0", "AC0")}
    stamps = {"AA0": "2026-09-22 23:00:00", "AB0": "2026-09-22 15:00:00", "AC0": "2026-09-23 02:30:00"}
    quotes = {symbol: quote(symbol, stamp) for symbol, stamp in stamps.items()}
    histories = {symbol: bars_at(china_timestamp(stamp)) for symbol, stamp in stamps.items()}

    _manifest, rows = scan(tmp_path, candidates, quotes, histories, now="2026-09-23 21:35:32")

    assert {symbol: row["reason"] for symbol, row in rows.items()} == {
        "AA0": "quote_stale", "AB0": "quote_stale", "AC0": "quote_stale",
    }


def test_weekend_holiday_and_monday_preopen_use_the_last_trading_day_close(tmp_path):
    candidates = {
        "AA0": candidate("AA0", exchange="SHFE"),
        "AB0": candidate("AB0", exchange="SHFE"),
        "AC0": candidate("AC0", exchange="CFFEX"),
    }
    stamps = {"AA0": "2026-09-25 15:00:00", "AB0": "2026-09-26 01:00:00", "AC0": "2026-09-25 15:15:00"}
    quotes = {symbol: quote(symbol, stamp) for symbol, stamp in stamps.items()}
    histories = {symbol: bars_at(china_timestamp(stamp)) for symbol, stamp in stamps.items()}

    for now in ("2026-09-26 12:53:00", "2026-09-27 21:35:00", "2026-09-28 08:45:00"):
        manifest, rows = scan(tmp_path, candidates, quotes, histories, now=now)
        assert all(row["outcome"] == "scanned" for row in rows.values()), (now, rows)
        assert manifest["market"]["freshness_mode"] == "prior_session"

    _manifest, monday_open = scan(tmp_path, candidates, quotes, histories, now="2026-09-28 09:05:00")
    assert monday_open["AA0"]["reason"] == "quote_stale"
    assert monday_open["AB0"]["reason"] == "quote_stale"
    assert monday_open["AC0"]["outcome"] == "scanned"

    holiday_quotes = {"AA0": quote("AA0", "2026-09-23 15:00:00")}
    holiday_histories = {"AA0": bars_at(china_timestamp("2026-09-23 15:00:00"))}
    _manifest, holiday = scan(tmp_path, {"AA0": candidates["AA0"]}, holiday_quotes, holiday_histories,
                              now="2026-09-24 12:00:00", holidays=("2026-09-24",))
    assert holiday["AA0"]["outcome"] == "scanned"


def test_the_forming_bar_is_set_aside_instead_of_invalidating_the_history(tmp_path):
    now = "2026-09-23 21:35:32"
    candidates = {"AA0": candidate("AA0", exchange="SHFE")}
    quotes = {"AA0": quote("AA0", "2026-09-23 21:35:20")}
    completed = bars_at(china_timestamp("2026-09-23 21:35:00"))
    forming = Bar(ts=china_timestamp("2026-09-23 21:40:00"), open=100.0, high=101.0, low=99.0, close=100.5,
                  volume=3.0, hold=1000.0)
    assert china_timestamp(now) + MAX_FUTURE_SKEW_SEC < forming.ts <= china_timestamp(now) + HISTORY_BAR_INTERVAL_SEC + MAX_FUTURE_SKEW_SEC

    baseline, baseline_rows = scan(tmp_path, candidates, quotes, {"AA0": completed}, now=now)
    manifest, rows = scan(tmp_path, candidates, quotes, {"AA0": completed + [forming]}, now=now)

    assert rows["AA0"]["outcome"] == "scanned"
    assert rows["AA0"]["score"] == baseline_rows["AA0"]["score"]
    assert rows["AA0"]["trend"] == baseline_rows["AA0"]["trend"]
    assert manifest["market"]["history_as_of_max"] == china_timestamp("2026-09-23 21:35:00")
    assert manifest["market"] == baseline["market"]

    too_far = Bar(ts=china_timestamp(now) + HISTORY_BAR_INTERVAL_SEC + MAX_FUTURE_SKEW_SEC + 1.0,
                  open=100.0, high=101.0, low=99.0, close=100.5, volume=3.0, hold=1000.0)
    _manifest, invalid = scan(tmp_path, candidates, quotes, {"AA0": completed + [too_far]}, now=now)
    assert invalid["AA0"]["stage"] == "history"
    assert invalid["AA0"]["reason"] == "history_invalid"

    only_forming = completed[-59:] + [forming]
    _manifest, short = scan(tmp_path, candidates, quotes, {"AA0": only_forming}, now=now)
    assert short["AA0"]["reason"] == "history_insufficient"
