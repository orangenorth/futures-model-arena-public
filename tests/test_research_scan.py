"""Offline acceptance tests for the independent full-universe research scan."""
from __future__ import annotations

import json
import copy
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pytest

from arena.db import Database
from arena.models import Bar, Quote
from arena.preopen_universe import build_preopen_universe_research
from arena.research_scan import (
    DEFAULT_HISTORY_WORKERS,
    FullUniverseScanner,
    ResearchScanService,
    SinaResearchReader,
    canonical_manifest_hash,
)
from arena.universe import content_hash, load_catalogue


NOW = 1_700_000_000.0
ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def symbol_at(index: int) -> str:
    return chr(ord("A") + (index // 26)) + chr(ord("A") + (index % 26)) + "0"


def candidate(symbol: str, *, name: str | None = None, exchange: str = "SHFE") -> dict:
    return {
        "continuous_symbol": symbol,
        "name": name or f"{symbol} name",
        "exchange": exchange,
        "source_group": "fixture",
        "source_node": symbol,
        "quote_ok": True,
        "history_5m_ok": True,
        "history_15m_ok": True,
        "history_1h_ok": True,
        "probe_status": "ok",
        "probe_error": "",
        "probed_at": "2026-09-19T00:00:00+08:00",
    }


def write_catalogue(path: Path, candidates: Mapping[str, dict], *, snapshot_status: str = "complete") -> None:
    payload = {
        "schema_version": 1,
        "source": "offline fixture",
        "source_url": "https://example.invalid/fixture",
        "fetched_at": "2026-09-19T00:00:00+08:00",
        "snapshot_status": snapshot_status,
        "count": len(candidates),
        "candidates": dict(candidates),
        "content_hash": content_hash(dict(candidates)),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def bars(*, close: float = 100.0, invalid: bool = False) -> list[Bar]:
    rows = []
    for index in range(70):
        price = close + index * 0.2
        rows.append(Bar(ts=NOW - (69 - index) * 300, open=price - 0.1, high=price + 0.3,
                        low=price - 0.3, close=price, volume=100 + index, hold=1000 + index))
    if invalid:
        rows[-1] = Bar(ts=rows[-1].ts, open=100, high=99, low=101, close=100, volume=1, hold=1)
    return rows


def china_timestamp(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI).timestamp()


def bars_at(last_ts: float, *, close: float = 100.0) -> list[Bar]:
    rows = []
    for index in range(70):
        price = close + index * 0.2
        rows.append(Bar(ts=last_ts - (69 - index) * 300, open=price - 0.1, high=price + 0.3,
                        low=price - 0.3, close=price, volume=100 + index, hold=1000 + index))
    return rows


def write_calendar(path: Path, *, valid_until: str = "2026-12-31", holidays: Sequence[str] = ()) -> None:
    path.write_text(json.dumps({"calendar_valid_until": valid_until, "holidays": list(holidays)}), encoding="utf-8")


class FakeReader:
    def __init__(self, quotes: Mapping[str, Quote], histories: Mapping[str, Sequence[Bar]]):
        self.quotes = dict(quotes)
        self.histories = dict(histories)
        self.quote_batches: list[tuple[str, ...]] = []
        self.history_calls: list[str] = []

    def latest_batch(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        self.quote_batches.append(tuple(symbols))
        return {symbol: self.quotes[symbol] for symbol in symbols if symbol in self.quotes}

    def history_5m(self, symbol: str) -> Sequence[Bar]:
        self.history_calls.append(symbol)
        return self.histories.get(symbol, [])


def make_candidates(count: int) -> Dict[str, dict]:
    return {symbol_at(index): candidate(symbol_at(index)) for index in range(count)}


def test_committed_catalogue_baseline_is_83_with_four_known_unquotable_rows():
    catalogue = load_catalogue(ROOT / "config" / "contracts_universe_cn.json")
    unavailable = {item["symbol"] for item in catalogue["candidates"] if item.get("quote_ok") is False}

    assert catalogue["available"] is True
    assert catalogue["snapshot_status"] == "complete"
    assert catalogue["count"] == len(catalogue["candidates"]) == 83
    assert unavailable == {"JR0", "LR0", "RI0", "WH0"}


def make_reader(symbols: Iterable[str], *, omitted: Iterable[str] = ()) -> FakeReader:
    omitted_set = set(omitted)
    quotes = {symbol: Quote(symbol=symbol, price=100.0, ts=NOW - 30, change_pct=0.5)
              for symbol in symbols if symbol not in omitted_set}
    histories = {symbol: bars() for symbol in quotes}
    return FakeReader(quotes, histories)


def run(path: Path, reader: FakeReader, *, now: float = NOW, **kwargs) -> dict:
    return FullUniverseScanner(path, reader, clock=lambda: now, quote_budget_sec=1.0,
                               history_budget_sec=1.0, **kwargs).run()


def test_complete_catalogue_keeps_all_83_in_denominator_and_excludes_unproven_rows_before_market_reads(tmp_path):
    candidates = make_candidates(83)
    path = tmp_path / "catalogue.json"
    missing = {symbol_at(index) for index in (3, 17, 51, 79)}
    for symbol in missing:
        candidates[symbol]["quote_ok"] = False
    write_catalogue(path, candidates)
    reader = make_reader(candidates)

    manifest = run(path, reader)

    assert manifest["counts"] == {"discovered": 83, "eligible": 79, "scanned": 79, "excluded": 4}
    assert manifest["status"] == "partial"
    assert manifest["coverage"] == {"status": "incomplete", "ratio": round(79 / 83, 6), "denominator_known": True}
    assert sorted(len(batch) for batch in reader.quote_batches) == [29, 50]
    assert set(symbol for batch in reader.quote_batches for symbol in batch) == set(candidates) - missing
    excluded = {item["symbol"]: item for item in manifest["items"] if item["outcome"] == "excluded"}
    assert set(excluded) == missing
    assert {item["stage"] for item in excluded.values()} == {"catalogue"}
    assert {item["reason"] for item in excluded.values()} == {"catalogue_quote_proof_missing"}
    assert len(reader.history_calls) == 79
    assert all(item["outcome"] == "scanned" or item["reason"] for item in manifest["items"])


def test_expanding_catalogue_changes_the_denominator_without_a_production_constant(tmp_path):
    candidates = make_candidates(83)
    candidates["ZZ0"] = candidate("ZZ0", name="new item")
    path = tmp_path / "catalogue.json"
    missing = {symbol_at(index) for index in (3, 17, 51, 79)}
    for symbol in missing:
        candidates[symbol]["quote_ok"] = False
    write_catalogue(path, candidates)

    manifest = run(path, make_reader(candidates))

    assert manifest["counts"] == {"discovered": 84, "eligible": 80, "scanned": 80, "excluded": 4}
    assert {item["symbol"] for item in manifest["items"]} == set(candidates)


def test_candidate_proof_flags_gate_market_reads_with_fixed_catalogue_reasons(tmp_path):
    candidates = make_candidates(4)
    candidates["AA0"]["quote_ok"] = False
    candidates["AB0"]["probe_status"] = "quote_only"
    candidates["AC0"]["history_15m_ok"] = False
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)
    reader = make_reader(candidates)

    manifest = run(path, reader)

    exclusions = {item["symbol"]: item for item in manifest["items"] if item["outcome"] == "excluded"}
    assert manifest["counts"] == {"discovered": 4, "eligible": 1, "scanned": 1, "excluded": 3}
    assert manifest["coverage"] == {"status": "incomplete", "ratio": 0.25, "denominator_known": True}
    assert {symbol for batch in reader.quote_batches for symbol in batch} == {"AD0"}
    assert reader.history_calls == ["AD0"]
    assert {symbol: (item["stage"], item["reason"]) for symbol, item in exclusions.items()} == {
        "AA0": ("catalogue", "catalogue_quote_proof_missing"),
        "AB0": ("catalogue", "catalogue_probe_incomplete"),
        "AC0": ("catalogue", "catalogue_history_proof_missing"),
    }


def test_partial_or_tampered_catalogue_never_starts_market_reads_or_claims_coverage(tmp_path):
    candidates = make_candidates(3)
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates, snapshot_status="partial")
    reader = make_reader(candidates)

    partial = run(path, reader)

    assert partial["status"] == "incomplete"
    assert partial["counts"] == {"discovered": 3, "eligible": 0, "scanned": 0, "excluded": 3}
    assert partial["coverage"] == {"status": "incomplete", "ratio": None, "denominator_known": True}
    assert not reader.quote_batches and not reader.history_calls
    assert {item["reason"] for item in partial["items"]} == {"catalogue_incomplete"}

    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["snapshot_status"] = "complete"
    corrupted["candidates"][symbol_at(0)]["name"] = "tampered"
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    reader = make_reader(candidates)
    unavailable = run(path, reader)
    assert unavailable["status"] == "unavailable"
    assert unavailable["coverage"] == {"status": "unavailable", "ratio": None, "denominator_known": False}
    assert not reader.quote_batches and not reader.history_calls


def test_invalid_history_stays_excluded_with_a_fixed_reason(tmp_path):
    candidates = make_candidates(2)
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)
    reader = make_reader(candidates)
    reader.histories[symbol_at(1)] = bars(invalid=True)

    manifest = run(path, reader)

    excluded = {item["symbol"]: item for item in manifest["items"] if item["outcome"] == "excluded"}
    assert excluded[symbol_at(1)]["stage"] == "history"
    assert excluded[symbol_at(1)]["reason"] == "history_invalid"
    assert manifest["counts"] == {"discovered": 2, "eligible": 2, "scanned": 1, "excluded": 1}


@pytest.mark.parametrize(("quote", "expected"), [
    (Quote(symbol="AA0", price=float("nan"), ts=NOW - 10), "quote_invalid"),
    (Quote(symbol="AA0", price=100, ts=NOW - 901), "quote_stale"),
    (Quote(symbol="AA0", price=100, ts=NOW + 61), "quote_future"),
])
def test_invalid_stale_and_future_quotes_are_explicitly_excluded(tmp_path, quote, expected):
    candidates = {"AA0": candidate("AA0")}
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)
    reader = FakeReader({"AA0": quote}, {"AA0": bars()})

    manifest = run(path, reader)

    assert manifest["items"] == [{"symbol": "AA0", "name": "AA0 name", "exchange": "SHFE", "outcome": "excluded",
                                   "stage": "quote", "reason": expected, "latest_price": None, "change_pct": None,
                                   "trend": None, "score": None}]
    assert not reader.history_calls


def test_midday_close_is_a_prior_session_snapshot_not_a_stale_market_tick(tmp_path):
    now = china_timestamp("2026-09-22 12:53:00")
    quote_ts = china_timestamp("2026-09-22 11:30:00")
    bar_ts = china_timestamp("2026-09-22 11:25:00")
    candidates = {"AA0": candidate("AA0", exchange="SHFE")}
    catalogue_path = tmp_path / "catalogue.json"
    calendar_path = tmp_path / "calendar.json"
    write_catalogue(catalogue_path, candidates)
    write_calendar(calendar_path)
    reader = FakeReader({"AA0": Quote(symbol="AA0", price=100, ts=quote_ts, change_pct=0.5)},
                        {"AA0": bars_at(bar_ts)})

    manifest = run(catalogue_path, reader, now=now, calendar_path=calendar_path)

    assert manifest["status"] == "complete"
    assert manifest["counts"] == {"discovered": 1, "eligible": 1, "scanned": 1, "excluded": 0}
    assert manifest["market"] == {
        "quote_as_of_min": quote_ts, "quote_as_of_max": quote_ts,
        "history_as_of_min": bar_ts, "history_as_of_max": bar_ts,
        "freshness_mode": "prior_session", "prior_session_count": 1, "live_session_count": 0,
    }

    db = Database(tmp_path / "midday.db")
    try:
        db.append_research_scan(manifest)
        latest = db.latest_research_scan()
    finally:
        db.close()
    assert latest and latest["market"] == manifest["market"]
    assert latest["schema_version"] == manifest["schema_version"]
    assert latest["rules_version"] == manifest["rules_version"]
    assert build_preopen_universe_research(latest)["scope_status"] == "complete"


def test_midday_snapshot_requires_the_immediately_preceding_close_and_a_current_calendar(tmp_path):
    now = china_timestamp("2026-09-22 12:53:00")
    calendar_path = tmp_path / "calendar.json"
    write_calendar(calendar_path)
    candidates = {"AA0": candidate("AA0", exchange="SHFE")}
    catalogue_path = tmp_path / "catalogue.json"
    write_catalogue(catalogue_path, candidates)

    too_old = FakeReader(
        {"AA0": Quote(symbol="AA0", price=100, ts=china_timestamp("2026-09-22 11:14:59"), change_pct=0.5)},
        {"AA0": bars_at(china_timestamp("2026-09-22 11:10:00"))},
    )
    stale = run(catalogue_path, too_old, now=now, calendar_path=calendar_path)
    assert stale["items"][0]["reason"] == "quote_stale"

    no_calendar = run(catalogue_path, FakeReader(
        {"AA0": Quote(symbol="AA0", price=100, ts=china_timestamp("2026-09-22 11:30:00"), change_pct=0.5)},
        {"AA0": bars_at(china_timestamp("2026-09-22 11:25:00"))},
    ), now=now)
    assert no_calendar["items"][0]["reason"] == "session_calendar_unavailable"

    saturday = run(catalogue_path, FakeReader(
        {"AA0": Quote(symbol="AA0", price=100, ts=china_timestamp("2026-09-26 11:30:00"), change_pct=0.5)},
        {"AA0": bars_at(china_timestamp("2026-09-26 11:25:00"))},
    ), now=china_timestamp("2026-09-26 12:53:00"), calendar_path=calendar_path)
    assert saturday["items"][0]["reason"] == "quote_stale"


def test_post_day_close_is_a_prior_session_snapshot_before_the_night_session(tmp_path):
    now = china_timestamp("2026-09-22 19:11:00")
    commodity_quote_ts = china_timestamp("2026-09-22 15:04:59")
    commodity_bar_ts = china_timestamp("2026-09-22 14:55:00")
    cffex_quote_ts = china_timestamp("2026-09-22 15:15:00")
    cffex_bar_ts = china_timestamp("2026-09-22 15:10:00")
    candidates = {
        "AA0": candidate("AA0", exchange="SHFE"),
        "AB0": candidate("AB0", exchange="CFFEX"),
    }
    catalogue_path = tmp_path / "catalogue.json"
    calendar_path = tmp_path / "calendar.json"
    write_catalogue(catalogue_path, candidates)
    write_calendar(calendar_path)
    reader = FakeReader(
        {
            "AA0": Quote(symbol="AA0", price=100, ts=commodity_quote_ts, change_pct=0.5),
            "AB0": Quote(symbol="AB0", price=100, ts=cffex_quote_ts, change_pct=0.5),
        },
        {"AA0": bars_at(commodity_bar_ts), "AB0": bars_at(cffex_bar_ts)},
    )

    manifest = run(catalogue_path, reader, now=now, calendar_path=calendar_path)

    assert manifest["counts"] == {"discovered": 2, "eligible": 2, "scanned": 2, "excluded": 0}
    assert manifest["rules_version"] == "research-scan-v6"
    assert manifest["market"]["freshness_mode"] == "prior_session"
    assert manifest["market"]["prior_session_count"] == 2


def test_post_day_close_snapshot_fails_closed_without_a_valid_calendar_or_outside_the_close_grace(tmp_path):
    quote_ts = china_timestamp("2026-09-22 15:00:00")
    bar_ts = china_timestamp("2026-09-22 14:55:00")
    candidates = {"AA0": candidate("AA0", exchange="SHFE")}
    catalogue_path = tmp_path / "catalogue.json"
    calendar_path = tmp_path / "calendar.json"
    write_catalogue(catalogue_path, candidates)
    write_calendar(calendar_path)
    reader = FakeReader(
        {"AA0": Quote(symbol="AA0", price=100, ts=quote_ts, change_pct=0.5)},
        {"AA0": bars_at(bar_ts)},
    )

    # The final day close stays the immediately preceding closed session all
    # the way to the next morning open, including through the night session:
    # a candidate without a night session has no newer observation to offer.
    into_the_night = run(catalogue_path, reader, now=china_timestamp("2026-09-22 21:00:00"),
                         calendar_path=calendar_path)
    assert into_the_night["items"][0]["outcome"] == "scanned"
    assert into_the_night["market"]["freshness_mode"] == "prior_session"

    no_calendar = run(catalogue_path, reader, now=china_timestamp("2026-09-22 19:11:00"))
    assert no_calendar["items"][0]["reason"] == "session_calendar_unavailable"

    expired_calendar_path = tmp_path / "expired-calendar.json"
    write_calendar(expired_calendar_path, valid_until="2026-09-21")
    expired_calendar = run(catalogue_path, reader, now=china_timestamp("2026-09-22 19:11:00"),
                           calendar_path=expired_calendar_path)
    assert expired_calendar["items"][0]["reason"] == "session_calendar_unavailable"

    too_old = FakeReader(
        {"AA0": Quote(symbol="AA0", price=100, ts=china_timestamp("2026-09-22 14:44:59"), change_pct=0.5)},
        {"AA0": bars_at(china_timestamp("2026-09-22 14:40:00"))},
    )
    stale = run(catalogue_path, too_old, now=china_timestamp("2026-09-22 19:11:00"), calendar_path=calendar_path)
    assert stale["items"][0]["reason"] == "quote_stale"

    post_close_source_stamp = FakeReader(
        {"AA0": Quote(symbol="AA0", price=100, ts=china_timestamp("2026-09-22 15:05:01"), change_pct=0.5)},
        {"AA0": bars_at(bar_ts)},
    )
    source_stamp_stale = run(catalogue_path, post_close_source_stamp, now=china_timestamp("2026-09-22 19:11:00"),
                             calendar_path=calendar_path)
    assert source_stamp_stale["items"][0]["reason"] == "quote_stale"


def test_cffex_reopens_before_commodity_afternoon_session(tmp_path):
    now = china_timestamp("2026-09-22 13:05:00")
    quote_ts = china_timestamp("2026-09-22 11:30:00")
    bar_ts = china_timestamp("2026-09-22 11:25:00")
    candidates = {
        "AA0": candidate("AA0", exchange="SHFE"),
        "AB0": candidate("AB0", exchange="CFFEX"),
    }
    catalogue_path = tmp_path / "catalogue.json"
    calendar_path = tmp_path / "calendar.json"
    write_catalogue(catalogue_path, candidates)
    write_calendar(calendar_path)
    reader = FakeReader(
        {symbol: Quote(symbol=symbol, price=100, ts=quote_ts, change_pct=0.5) for symbol in candidates},
        {symbol: bars_at(bar_ts) for symbol in candidates},
    )

    manifest = run(catalogue_path, reader, now=now, calendar_path=calendar_path)

    rows = {item["symbol"]: item for item in manifest["items"]}
    assert rows["AA0"]["outcome"] == "scanned"
    assert rows["AB0"]["reason"] == "quote_stale"
    assert manifest["market"]["freshness_mode"] == "prior_session"


def test_scanner_passes_cffex_source_node_only_to_a_reader_that_opted_in(tmp_path):
    candidates = {"IF0": candidate("IF0", exchange="CFFEX")}
    candidates["IF0"]["source_node"] = "qz_qh"
    catalogue_path = tmp_path / "catalogue.json"
    write_catalogue(catalogue_path, candidates)

    class ConfiguringReader(FakeReader):
        configured = None

        def configure_candidates(self, rows):
            self.configured = [dict(row) for row in rows]

    reader = ConfiguringReader(
        {"IF0": Quote(symbol="IF0", price=100, ts=NOW - 20, change_pct=0.5)},
        {"IF0": bars()},
    )
    manifest = run(catalogue_path, reader)

    assert manifest["counts"]["scanned"] == 1
    assert reader.configured is not None and len(reader.configured) == 1
    assert reader.configured[0]["symbol"] == "IF0"
    assert reader.configured[0]["exchange"] == "CFFEX"
    assert reader.configured[0]["source_node"] == "qz_qh"


def test_all_eligible_candidates_can_use_the_same_morning_close_snapshot_for_preopen_research(tmp_path):
    now = china_timestamp("2026-09-22 12:53:00")
    quote_ts = china_timestamp("2026-09-22 11:30:00")
    bar_ts = china_timestamp("2026-09-22 11:25:00")
    candidates = make_candidates(83)
    catalogue_missing = {symbol_at(index) for index in (3, 17, 51, 79)}
    for symbol in catalogue_missing:
        candidates[symbol]["quote_ok"] = False
    catalogue_path = tmp_path / "catalogue.json"
    calendar_path = tmp_path / "calendar.json"
    write_catalogue(catalogue_path, candidates)
    write_calendar(calendar_path)
    eligible = set(candidates) - catalogue_missing
    reader = FakeReader(
        {symbol: Quote(symbol=symbol, price=100, ts=quote_ts, change_pct=0.5) for symbol in eligible},
        {symbol: bars_at(bar_ts) for symbol in eligible},
    )

    manifest = run(catalogue_path, reader, now=now, calendar_path=calendar_path)
    research = build_preopen_universe_research(manifest)

    assert manifest["counts"] == {"discovered": 83, "eligible": 79, "scanned": 79, "excluded": 4}
    assert manifest["market"]["freshness_mode"] == "prior_session"
    assert manifest["market"]["prior_session_count"] == 79
    assert research["scope_status"] == "complete"
    assert research["counts"]["runtime_excluded"] == 0


def test_manifest_identity_is_deterministic_for_the_same_logical_facts(tmp_path):
    candidates = make_candidates(4)
    path_a = tmp_path / "a.json"
    path_b = tmp_path / "b.json"
    write_catalogue(path_a, candidates)
    write_catalogue(path_b, dict(reversed(list(candidates.items()))))

    first = run(path_a, make_reader(candidates))
    second = run(path_b, make_reader(candidates))

    assert first["items"] == second["items"]
    assert first["manifest_hash"] == second["manifest_hash"]


def test_history_workers_are_bounded_even_when_the_caller_requests_more(tmp_path):
    candidates = make_candidates(12)
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)

    class SlowReader(FakeReader):
        def __init__(self):
            super().__init__({symbol: Quote(symbol=symbol, price=100, ts=NOW - 20) for symbol in candidates},
                             {symbol: bars() for symbol in candidates})
            self._lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def history_5m(self, symbol: str) -> Sequence[Bar]:
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.01)
                return super().history_5m(symbol)
            finally:
                with self._lock:
                    self.active -= 1

    reader = SlowReader()
    manifest = run(path, reader, history_workers=999)

    assert manifest["counts"]["scanned"] == len(candidates)
    assert reader.max_active <= 8
    assert DEFAULT_HISTORY_WORKERS == 4


def test_sina_reader_reuses_only_recent_history_and_not_quotes():
    class Response:
        text = 'cb([["2023-11-14 12:00:00", "99", "101", "98", "100", "1", "2"]])'

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return Response()

        def close(self):
            return None

    reader = SinaResearchReader(timeout_sec=1)
    client = Client()
    reader._client.close()
    reader._client = client
    try:
        first = reader.history_5m("AA0")
        second = reader.history_5m("AA0")
    finally:
        reader.close()

    assert first == second
    assert client.calls == 1


@pytest.mark.parametrize(("symbol", "node"), [
    ("IF0", "qz_qh"), ("IC0", "zzgz_qh"), ("IH0", "szgz_qh"),
    ("IM0", "im_qh"), ("TF0", "gz_qh"), ("TS0", "engz_qh"),
])
def test_sina_reader_uses_catalogue_provenance_for_cffex_quote_layout(symbol, node):
    class Response:
        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs.get("params")))
            if "Market_Center.getHQFuturesData" in url:
                return Response([{
                    "symbol": symbol, "trade": "4485.60", "preclose": "4464.80", "position": "156831",
                    "tradedate": "2026-09-22", "ticktime": "11:30:00", "changepercent": "0.0054255",
                }])
            fields = ["0"] * 50
            fields[1] = "093001"
            fields[8] = "9999"
            fields[10] = "100"
            fields[13] = "100"
            fields[17] = "20260922"
            return Response(content=f'var hq_str_nf_{symbol}="{",".join(fields)}";'.encode("gb18030"))

        def close(self):
            return None

    reader = SinaResearchReader(timeout_sec=1)
    client = Client()
    reader._client.close()
    reader._client = client
    reader.configure_candidates([{"symbol": symbol, "exchange": "CFFEX", "source_node": node}])
    try:
        quotes = reader.latest_batch([symbol])
    finally:
        reader.close()

    quote = quotes[symbol]
    assert quote.price == 4485.60
    assert quote.open_interest == 156831
    assert quote.ts == china_timestamp("2026-09-22 11:30:00")
    assert quote.change_pct == pytest.approx((4485.60 / 4464.80 - 1) * 100)
    assert client.calls[1][1] == {"page": "1", "num": "40", "sort": "position", "asc": "0", "node": node, "base": "futures"}


def test_sina_history_cache_evicts_expired_and_oldest_entries_without_unbounded_growth():
    class Response:
        text = 'cb([["2023-11-14 12:00:00", "99", "101", "98", "100", "1", "2"]])'

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return Response()

        def close(self):
            return None

    ticks = [0.0]
    reader = SinaResearchReader(timeout_sec=1, history_cache_sec=5, history_cache_max_entries=2,
                                monotonic=lambda: ticks[0])
    client = Client()
    reader._client.close()
    reader._client = client
    try:
        for symbol in ("AA0", "AB0", "AC0"):
            reader.history_5m(symbol)
            ticks[0] += 1
        assert len(reader._history_cache) == 2
        reader.history_5m("AA0")  # oldest entry was evicted by the bounded cache.
        assert client.calls == 4
        ticks[0] += 6
        reader.history_5m("AB0")  # TTL expiry is purged before lookup.
    finally:
        reader.close()

    assert client.calls == 5
    assert len(reader._history_cache) <= 2


def test_history_deadline_stops_new_submissions_and_marks_the_run_incomplete(tmp_path):
    candidates = make_candidates(12)
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)

    class BlockingReader(FakeReader):
        def history_5m(self, symbol: str) -> Sequence[Bar]:
            self.history_calls.append(symbol)
            time.sleep(0.08)
            return bars()

    reader = BlockingReader({symbol: Quote(symbol=symbol, price=100, ts=NOW - 20) for symbol in candidates}, {})
    manifest = FullUniverseScanner(path, reader, clock=lambda: NOW, history_budget_sec=0.01).run()

    assert manifest["status"] == "incomplete"
    assert manifest["coverage"] == {"status": "incomplete", "ratio": None, "denominator_known": True}
    assert manifest["counts"] == {"discovered": 12, "eligible": 12, "scanned": 0, "excluded": 12}
    assert len(reader.history_calls) <= DEFAULT_HISTORY_WORKERS
    assert {item["reason"] for item in manifest["items"]} == {"history_timeout"}


def test_completed_scan_is_persisted_as_a_safe_append_only_projection(tmp_path):
    candidates = make_candidates(2)
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)
    manifest = run(path, make_reader(candidates))
    db = Database(tmp_path / "research.db")
    try:
        run_id = db.append_research_scan(manifest)
        latest = db.latest_research_scan()
    finally:
        db.close()

    assert latest and latest["id"] == run_id
    assert latest["counts"] == manifest["counts"]
    assert latest["manifest_hash"] == manifest["manifest_hash"]
    assert {item["symbol"] for item in latest["items"]} == set(candidates)
    assert "error" not in latest and "source_url" not in json.dumps(latest, ensure_ascii=False)


def test_research_manifest_db_rejects_hash_invariant_and_sensitive_projection_tampering(tmp_path):
    candidates = make_candidates(2)
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)
    manifest = run(path, make_reader(candidates))
    db = Database(tmp_path / "research.db")
    try:
        wrong_hash = copy.deepcopy(manifest)
        wrong_hash["manifest_hash"] = "sha256:" + "0" * 64
        with pytest.raises(ValueError, match="hash"):
            db.append_research_scan(wrong_hash)

        bad_count = copy.deepcopy(manifest)
        bad_count["counts"]["eligible"] = 0
        bad_count["manifest_hash"] = canonical_manifest_hash(bad_count)
        with pytest.raises(ValueError, match="counts"):
            db.append_research_scan(bad_count)

        false_eligible = copy.deepcopy(manifest)
        false_eligible["items"][0].update({
            "outcome": "excluded", "stage": "catalogue", "reason": "catalogue_quote_proof_missing",
            "latest_price": None, "change_pct": None, "trend": None, "score": None,
        })
        false_eligible["status"] = "partial"
        false_eligible["counts"] = {"discovered": 2, "eligible": 2, "scanned": 1, "excluded": 1}
        false_eligible["coverage"] = {"status": "incomplete", "ratio": 0.5, "denominator_known": True}
        false_eligible["manifest_hash"] = canonical_manifest_hash(false_eligible)
        with pytest.raises(ValueError, match="eligible"):
            db.append_research_scan(false_eligible)

        hidden_cancel = copy.deepcopy(manifest)
        hidden_cancel["items"][0].update({
            "outcome": "excluded", "stage": "quote", "reason": "scan_cancelled",
            "latest_price": None, "change_pct": None, "trend": None, "score": None,
        })
        hidden_cancel["status"] = "partial"
        hidden_cancel["counts"] = {"discovered": 2, "eligible": 2, "scanned": 1, "excluded": 1}
        hidden_cancel["coverage"] = {"status": "incomplete", "ratio": 0.5, "denominator_known": True}
        hidden_cancel["manifest_hash"] = canonical_manifest_hash(hidden_cancel)
        with pytest.raises(ValueError, match="incomplete"):
            db.append_research_scan(hidden_cancel)

        wrong_stage = copy.deepcopy(manifest)
        wrong_stage["items"][0].update({
            "outcome": "excluded", "stage": "quote", "reason": "catalogue_quote_proof_missing",
            "latest_price": None, "change_pct": None, "trend": None, "score": None,
        })
        wrong_stage["status"] = "partial"
        wrong_stage["counts"] = {"discovered": 2, "eligible": 2, "scanned": 1, "excluded": 1}
        wrong_stage["coverage"] = {"status": "incomplete", "ratio": 0.5, "denominator_known": True}
        wrong_stage["manifest_hash"] = canonical_manifest_hash(wrong_stage)
        with pytest.raises(ValueError, match="invalid stage"):
            db.append_research_scan(wrong_stage)

        unsafe = copy.deepcopy(manifest)
        unsafe["items"][0]["name"] = "SECRET https://example.invalid/token"
        unsafe["manifest_hash"] = canonical_manifest_hash(unsafe)
        with pytest.raises(ValueError, match="unsafe"):
            db.append_research_scan(unsafe)

        run_id = db.append_research_scan(manifest)
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            db.conn().execute("UPDATE research_scan_runs SET status='partial' WHERE id=?", (run_id,))
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            db.conn().execute("DELETE FROM research_scan_items WHERE run_id=?", (run_id,))
        assert db.latest_research_scan() is not None
    finally:
        db.close()


def test_research_manifest_read_rechecks_actual_stored_items(tmp_path):
    candidates = make_candidates(1)
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)
    manifest = run(path, make_reader(candidates))
    db = Database(tmp_path / "research.db")
    try:
        run_id = db.append_research_scan(manifest)
        # A privileged local administrator could remove a trigger.  The reader
        # must still refuse a projection whose stored rows no longer match its hash.
        db.conn().execute("DROP TRIGGER research_scan_items_no_update")
        db.conn().execute("UPDATE research_scan_items SET name='changed' WHERE run_id=?", (run_id,))
        assert db.latest_research_scan() is None
    finally:
        db.close()


def test_service_stop_cancels_pending_work_and_never_writes_after_stop(tmp_path):
    candidates = make_candidates(12)
    path = tmp_path / "catalogue.json"
    write_catalogue(path, candidates)
    now = china_timestamp("2026-09-22 14:50:00")

    class BlockingReader(FakeReader):
        def __init__(self):
            super().__init__({symbol: Quote(symbol=symbol, price=100, ts=now - 10) for symbol in candidates}, {})
            self.started = threading.Event()

        def history_5m(self, symbol: str) -> Sequence[Bar]:
            self.history_calls.append(symbol)
            self.started.set()
            time.sleep(0.05)
            return [Bar(ts=now - (69 - index) * 300, open=100 + index, high=101 + index,
                        low=99 + index, close=100 + index, volume=1, hold=1) for index in range(70)]

    reader = BlockingReader()
    db = Database(tmp_path / "research.db")
    service = ResearchScanService(db, path, reader_factory=lambda: reader, clock=lambda: now)
    try:
        started, _message = service.start()
        assert started and reader.started.wait(timeout=1)
        service.stop()
        calls_after_stop = len(reader.history_calls)
        time.sleep(0.12)
        assert len(reader.history_calls) == calls_after_stop <= DEFAULT_HISTORY_WORKERS
        assert service.public_status()["running"] is False
        assert db.latest_research_scan() is None
    finally:
        service.stop()
        db.close()


def test_scanner_module_has_no_execution_or_prompt_dependency():
    source = (Path(__file__).resolve().parents[1] / "arena" / "research_scan.py").read_text(encoding="utf-8")
    for forbidden in ("ContractSpec", "SimExchange", "RiskManager", "Engine", "arena.debate", "debate_sessions"):
        assert forbidden not in source
