"""Independent, read-only technical scans for the discovered futures universe.

The candidate catalogue is an identity record.  It intentionally contains no
contract terms, and this module never adds any.  A scan uses the public quote
and minute-bar endpoints only to produce a research table: it has no order,
account, risk, or model-prompt integration.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple
from zoneinfo import ZoneInfo

import httpx

from .models import Bar, Quote
from .universe import SNAPSHOT_COMPLETE, load_catalogue


SCAN_SCHEMA_VERSION = 1
SCAN_RULES_VERSION = "research-scan-v6"
SPOT_BATCH_SIZE = 50
MAX_QUOTE_WORKERS = 8
DEFAULT_HISTORY_WORKERS = 4
MAX_HISTORY_WORKERS = 8
DEFAULT_HISTORY_BUDGET_SEC = 120.0
HISTORY_CACHE_SEC = 60.0
MAX_HISTORY_CACHE_ENTRIES = 128
MAX_FUTURE_SKEW_SEC = 60.0
# The public 5-minute feed labels each bar with its closing minute, so the bar
# that is still forming carries a stamp up to one interval ahead of the clock.
HISTORY_BAR_INTERVAL_SEC = 300.0
DEFAULT_QUOTE_MAX_AGE_SEC = 900.0
DEFAULT_HISTORY_MAX_AGE_SEC = 3 * 24 * 60 * 60.0
MIN_HISTORY_BARS = 60

# The candidate catalogue remains an identity-only discovery record.  These
# exchange-level session-close boundaries are a research freshness guard, not
# contract terms: they decide whether a public observation can be described as
# the close of the immediately preceding closed session while that exchange is
# in a scheduled recess or in the overnight gap.  They must never reach the
# execution, matching, or admission paths.
_RESEARCH_TIMEZONE = ZoneInfo("Asia/Shanghai")
_COMMODITY_DAY_SESSIONS = ((9 * 60, 10 * 60 + 15), (10 * 60 + 30, 11 * 60 + 30), (13 * 60 + 30, 15 * 60))
_CFFEX_DAY_SESSIONS = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60 + 15))
# Night sessions end at 23:00, 01:00 or 02:30 depending on the product.  Which
# product trades which night session is a contract term the scanner must not
# know, so every recognised night close of an exchange is a candidate
# closed-session boundary from the moment it has passed until the next morning
# open.  A product that is still trading proves it with a live observation
# before these boundaries are ever consulted.
_COMMODITY_NIGHT_CLOSES = (23 * 60, 25 * 60, 26 * 60 + 30)
# A public quote feed can stamp its end-of-session publication a few minutes
# after the final tradable minute.  It remains a prior-session observation, not
# proof that the market remained open.  Keep this narrowly bounded and apply it
# only to a verified closed-session boundary.
_RESEARCH_CLOSE_TIMESTAMP_GRACE_SEC = 5 * 60
# Calendar walks stay bounded: no exchange recess spans this many days.
_RESEARCH_CALENDAR_LOOKUP_DAYS = 21
_RESEARCH_DAY_SESSIONS = {
    "CFFEX": _CFFEX_DAY_SESSIONS,
    "CZCE": _COMMODITY_DAY_SESSIONS,
    "DCE": _COMMODITY_DAY_SESSIONS,
    "GFEX": _COMMODITY_DAY_SESSIONS,
    "INE": _COMMODITY_DAY_SESSIONS,
    "SHFE": _COMMODITY_DAY_SESSIONS,
}
_RESEARCH_NIGHT_CLOSES = {
    "CZCE": _COMMODITY_NIGHT_CLOSES,
    "DCE": _COMMODITY_NIGHT_CLOSES,
    "GFEX": _COMMODITY_NIGHT_CLOSES,
    "INE": _COMMODITY_NIGHT_CLOSES,
    "SHFE": _COMMODITY_NIGHT_CLOSES,
}


def _local_minute(local_day: date, minute_of_day: int) -> datetime:
    """Return the aware local datetime ``minute_of_day`` minutes after midnight of ``local_day``."""
    return datetime.combine(local_day, dtime(0, 0), tzinfo=_RESEARCH_TIMEZONE) + timedelta(minutes=int(minute_of_day))


def _looks_like_session_close(exchange: str, observed_at: float, grace_before_sec: float) -> bool:
    """Whether an observation sits at a recognised close minute of the exchange, calendar-free.

    This only decides which exclusion reason an unprovable observation gets: a
    stamp that could be a session close needs the calendar to say so, while any
    other old stamp is simply stale.
    """
    key = str(exchange).upper()
    closes = [close for _open, close in _RESEARCH_DAY_SESSIONS.get(key, ())]
    closes += [offset % (24 * 60) for offset in _RESEARCH_NIGHT_CLOSES.get(key, ())]
    if not closes:
        return False
    local = datetime.fromtimestamp(observed_at, _RESEARCH_TIMEZONE)
    seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
    for close in closes:
        # Signed distance to the close minute, wrapping at midnight for the
        # 01:00 and 02:30 night closes.
        delta = (seconds_of_day - close * 60 + 43200) % 86400 - 43200
        if -grace_before_sec <= delta <= _RESEARCH_CLOSE_TIMESTAMP_GRACE_SEC:
            return True
    return False

_SINA_HISTORY_BASE = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService.getFewMinLine"
_SINA_SPOT_BASE = "https://hq.sinajs.cn/list={codes}"
_SINA_CFFEX_QUOTE_BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQFuturesData"
_SINA_HEADERS = {
    "Accept": "*/*",
    "Referer": "https://finance.sina.com.cn/futures/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
_SYMBOL_RE = re.compile(r"[A-Z]{1,3}0")
_SOURCE_NODE_RE = re.compile(r"[A-Za-z0-9_]{1,80}")
PUBLIC_SCAN_OUTCOMES = frozenset({"scanned", "excluded"})
PUBLIC_SCAN_STAGES = frozenset({"catalogue", "quote", "history", "analysis"})
PUBLIC_SCAN_TRENDS = frozenset({"bullish", "bearish", "neutral"})
PUBLIC_SCAN_REASONS = frozenset({
    "catalogue_unavailable", "catalogue_incomplete", "quote_missing", "quote_unavailable",
    "quote_timeout", "quote_invalid", "quote_stale", "quote_future", "history_unavailable",
    "history_timeout", "history_invalid", "history_stale", "history_insufficient", "scan_failed",
    "catalogue_probe_incomplete", "catalogue_quote_proof_missing", "catalogue_history_proof_missing",
    "scan_cancelled", "session_calendar_unavailable",
})
# A terminal exclusion row has a stricter grammar than the public reason
# vocabulary alone.  Keep it beside the scanner emitters so persistence and
# pre-open consumers cannot reinterpret a transient runtime failure as a
# catalogue proof exclusion.
PUBLIC_SCAN_STAGE_REASONS = {
    "catalogue": frozenset({
        "catalogue_incomplete",
        "catalogue_probe_incomplete",
        "catalogue_quote_proof_missing",
        "catalogue_history_proof_missing",
    }),
    "quote": frozenset({
        "quote_missing",
        "quote_unavailable",
        "quote_timeout",
        "quote_invalid",
        "quote_stale",
        "quote_future",
        "session_calendar_unavailable",
        "scan_cancelled",
    }),
    "history": frozenset({
        "history_unavailable",
        "history_timeout",
        "history_invalid",
        "history_stale",
        "history_insufficient",
        "session_calendar_unavailable",
        "scan_cancelled",
    }),
    "analysis": frozenset({"history_insufficient"}),
}


class ResearchSessionCalendar:
    """Read only the shared holiday horizon needed for a closed-window freshness check.

    ``contracts_cn.json`` is deliberately *not* treated as a source of contract
    terms here.  The scanner reads only its public holiday list and expiry date,
    because a weekday alone cannot prove that a noon timestamp belongs to an
    actual trading day.  A missing, malformed, or expired calendar fails closed:
    it never turns a stale quote into a usable research fact.
    """

    def __init__(self, path: str | Path | None):
        self._holidays: frozenset[str] = frozenset()
        self._valid_until: Optional[date] = None
        self._available = False
        if path is None:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                return
            valid_until = date.fromisoformat(str(raw.get("calendar_valid_until") or ""))
            holidays_raw = raw.get("holidays")
            if not isinstance(holidays_raw, list):
                return
            holidays = {date.fromisoformat(str(value)).isoformat() for value in holidays_raw}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return
        self._holidays = frozenset(holidays)
        self._valid_until = valid_until
        self._available = True

    def available_for(self, local_day: date) -> bool:
        return bool(
            self._available
            and self._valid_until is not None
            and local_day <= self._valid_until
        )

    def is_trading_day(self, local_day: date) -> bool:
        return self.available_for(local_day) and local_day.weekday() < 5 and local_day.isoformat() not in self._holidays

    def latest_trading_day_on_or_before(self, local_day: date) -> Optional[date]:
        """Return the most recent trading day at or before ``local_day``; None without calendar proof."""
        day = local_day
        for _ in range(_RESEARCH_CALENDAR_LOOKUP_DAYS):
            if not self.available_for(day):
                return None
            if self.is_trading_day(day):
                return day
            day -= timedelta(days=1)
        return None

    def next_trading_day_after(self, local_day: date) -> Optional[date]:
        """Return the first trading day after ``local_day``; None without calendar proof."""
        day = local_day + timedelta(days=1)
        for _ in range(_RESEARCH_CALENDAR_LOOKUP_DAYS):
            if not self.available_for(day):
                return None
            if self.is_trading_day(day):
                return day
            day += timedelta(days=1)
        return None

    def closed_session_boundaries(self, exchange: str, now: datetime) -> Optional[List[datetime]]:
        """Return the exchange session closes whose scheduled gap still contains ``now``.

        A day-segment close is a live boundary from that close until the next
        day segment opens (an intra-day recess) or, for the final segment, until
        the first day segment of the next trading day.  A night close is a live
        boundary from the moment it has passed until that same next-morning
        open.  Only the latest trading day at or before ``now`` and the trading
        day before it can own a live boundary, so a close that the exchange has
        already traded past can never be promoted.  ``None`` means the calendar
        cannot prove the trading days involved; an empty list means no closed
        window is open right now and only a live observation is usable.
        """
        sessions = _RESEARCH_DAY_SESSIONS.get(str(exchange).upper())
        if not sessions:
            return []
        night_closes = _RESEARCH_NIGHT_CLOSES.get(str(exchange).upper(), ())
        current = self.latest_trading_day_on_or_before(now.date())
        if current is None:
            return None
        previous = self.latest_trading_day_on_or_before(current - timedelta(days=1))
        if previous is None:
            return None
        boundaries: List[datetime] = []
        for trading_day in (current, previous):
            following = self.next_trading_day_after(trading_day)
            if following is None:
                return None
            first_open_after = _local_minute(following, sessions[0][0])
            for index, (_open, close) in enumerate(sessions):
                boundary = _local_minute(trading_day, close)
                next_open = (_local_minute(trading_day, sessions[index + 1][0]) if index + 1 < len(sessions)
                             else first_open_after)
                if boundary <= now < next_open:
                    boundaries.append(boundary)
            for offset in night_closes:
                boundary = _local_minute(trading_day, offset)
                if boundary <= now < first_open_after:
                    boundaries.append(boundary)
        return boundaries


def canonical_manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Hash only normalized public facts; wall-clock completion time is not identity."""
    raw_items = manifest.get("items") if isinstance(manifest.get("items"), list) else []
    items = [dict(item) for item in raw_items if isinstance(item, Mapping)]
    identity = {
        "schema_version": manifest.get("schema_version"),
        "rules_version": manifest.get("rules_version"),
        "status": manifest.get("status"),
        "catalogue": manifest.get("catalogue"),
        "counts": manifest.get("counts"),
        "coverage": manifest.get("coverage"),
        "items": sorted(items, key=lambda item: str(item.get("symbol") or "")),
    }
    # ``market`` was added after the first persisted manifests.  Keep it
    # optional in the identity so historical immutable rows still re-prove
    # their original v1 digest, while every new freshness disclosure is covered
    # by its own digest.
    if isinstance(manifest.get("market"), Mapping):
        identity["market"] = manifest.get("market")
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ResearchSourceError(RuntimeError):
    """A non-sensitive source category used only inside the scan worker."""


class ResearchMarketReader(Protocol):
    """Narrow public-market boundary used by the scanner and its offline tests."""

    def latest_batch(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        """Return the current quotes for one bounded batch."""

    def history_5m(self, symbol: str) -> Sequence[Bar]:
        """Return one source request's 5-minute history for a symbol."""


class SinaResearchReader:
    """Public Sina reader with no contract metadata or execution dependencies."""

    def __init__(
        self,
        timeout_sec: float = 10.0,
        tz: str = "Asia/Shanghai",
        *,
        history_cache_sec: float = HISTORY_CACHE_SEC,
        history_cache_max_entries: int = MAX_HISTORY_CACHE_ENTRIES,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.timeout_sec = max(1.0, float(timeout_sec))
        self._tz = ZoneInfo(tz)
        self._client = httpx.Client(timeout=self.timeout_sec, headers=_SINA_HEADERS)
        self._cache_lock = threading.Lock()
        self._history_cache: Dict[str, Tuple[float, Tuple[Bar, ...]]] = {}
        self._history_cache_sec = max(0.0, float(history_cache_sec))
        self._history_cache_max_entries = max(1, int(history_cache_max_entries))
        self._monotonic = monotonic
        self._cffex_nodes: Dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def configure_candidates(self, candidates: Sequence[Mapping[str, Any]]) -> None:
        """Accept only discovery provenance needed by the CFFEX quote fallback.

        CFFEX uses a different public Sina quote layout from the commodity
        continuous-code feed.  Its catalogue node is provenance, not a trading
        specification, and is retained only inside this public reader.
        """
        nodes: Dict[str, str] = {}
        for candidate in candidates:
            symbol = str(candidate.get("symbol") or "").upper()
            node = str(candidate.get("source_node") or "")
            if (str(candidate.get("exchange") or "").upper() == "CFFEX"
                    and _SYMBOL_RE.fullmatch(symbol) and _SOURCE_NODE_RE.fullmatch(node)):
                nodes[symbol] = node
        self._cffex_nodes = nodes

    @staticmethod
    def _finite(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None

    def latest_batch(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        wanted = [str(symbol).upper() for symbol in symbols if _SYMBOL_RE.fullmatch(str(symbol).upper())]
        if not wanted:
            return {}
        try:
            response = self._client.get(_SINA_SPOT_BASE.format(codes=",".join(f"nf_{symbol}" for symbol in wanted)))
            response.raise_for_status()
            payload = response.content.decode("gb18030", errors="replace")
        except httpx.HTTPError as exc:
            raise ResearchSourceError("quote_unavailable") from exc
        result: Dict[str, Quote] = {}
        for symbol in wanted:
            # ``nf_`` CFFEX rows use a different, undocumented positional
            # layout.  Never allow an accidental generic parse to override the
            # catalogue-bound endpoint with named CFFEX fields below.
            if symbol in self._cffex_nodes:
                continue
            quote = self._parse_quote(payload, symbol)
            if quote is not None:
                result[symbol] = quote
        # ``nf_IF0`` carries a valid CFFEX row but its field layout differs from
        # commodity futures, so the generic parser intentionally refuses it.
        # Ask the same public Sina market-centre endpoint that discovery already
        # uses, but only for configured CFFEX provenance nodes.  This is the
        # primary reader for known CFFEX layouts, not an unbounded fallback.
        for symbol in wanted:
            if symbol in self._cffex_nodes:
                quote = self._cffex_quote(symbol, self._cffex_nodes[symbol])
                if quote is not None:
                    result[symbol] = quote
        return result

    def _cffex_quote(self, symbol: str, source_node: str) -> Optional[Quote]:
        try:
            response = self._client.get(
                _SINA_CFFEX_QUOTE_BASE,
                params={"page": "1", "num": "40", "sort": "position", "asc": "0", "node": source_node, "base": "futures"},
            )
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, TypeError, ValueError):
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, Mapping) or str(row.get("symbol") or "").upper() != symbol:
                continue
            price = self._finite(row.get("trade"))
            previous = self._finite(row.get("preclose"))
            if price is None or price <= 0:
                return None
            day = str(row.get("tradedate") or "").strip()
            clock = str(row.get("ticktime") or "").strip()
            try:
                ts = datetime.strptime(f"{day} {clock[:8]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=self._tz).timestamp()
            except ValueError:
                return None
            change_pct = (price / previous - 1.0) * 100.0 if previous and previous > 0 else 0.0
            return Quote(symbol=symbol, price=price, ts=ts, change_pct=change_pct,
                         open_interest=self._finite(row.get("position")))
        return None

    def history_5m(self, symbol: str) -> Sequence[Bar]:
        code = str(symbol).upper()
        if not _SYMBOL_RE.fullmatch(code):
            raise ResearchSourceError("history_unavailable")
        now = self._monotonic()
        with self._cache_lock:
            self._prune_history_cache(now)
            cached = self._history_cache.get(code)
            if cached is not None and now - cached[0] < self._history_cache_sec:
                return list(cached[1])
        try:
            response = self._client.get(_SINA_HISTORY_BASE, params={"symbol": code, "type": "5"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ResearchSourceError("history_unavailable") from exc
        text = response.text
        start, end = text.find("(["), text.rfind("])")
        if start < 0 or end < 0:
            raise ResearchSourceError("history_unavailable")
        try:
            rows = json.loads(text[start + 1:end + 1])
        except (TypeError, ValueError) as exc:
            raise ResearchSourceError("history_unavailable") from exc
        bars: List[Bar] = []
        for row in rows if isinstance(rows, list) else []:
            bar = self._parse_history_row(row)
            if bar is not None:
                bars.append(bar)
        bars.sort(key=lambda item: item.ts)
        with self._cache_lock:
            self._history_cache[code] = (self._monotonic(), tuple(bars))
            self._prune_history_cache(self._monotonic())
        return bars

    def _prune_history_cache(self, now: float) -> None:
        """Keep the research reader's shared history cache finite and TTL-bound."""
        expired = [key for key, value in self._history_cache.items()
                   if now - value[0] >= self._history_cache_sec]
        for key in expired:
            self._history_cache.pop(key, None)
        overflow = len(self._history_cache) - self._history_cache_max_entries
        if overflow > 0:
            for key, _value in sorted(self._history_cache.items(), key=lambda item: item[1][0])[:overflow]:
                self._history_cache.pop(key, None)

    def _parse_quote(self, payload: str, symbol: str) -> Optional[Quote]:
        match = re.search(rf'var\s+hq_str_nf_{re.escape(symbol)}="([^"]*)";', payload, re.IGNORECASE)
        if not match or not match.group(1):
            return None
        fields = match.group(1).split(",")
        if len(fields) <= 17:
            return None
        day = re.sub(r"\D", "", fields[17])
        clock = re.sub(r"\D", "", fields[1])
        if len(day) != 8 or len(clock) < 4:
            return None
        try:
            stamp = f"{day[:4]}-{day[4:6]}-{day[6:8]} {(clock + '000000')[:2]}:{(clock + '000000')[2:4]}:{(clock + '000000')[4:6]}"
            ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=self._tz).timestamp()
        except ValueError:
            return None
        price = self._finite(fields[8])
        previous = self._finite(fields[10])
        if price is None:
            return None
        change_pct = (price / previous - 1.0) * 100.0 if previous and previous > 0 else 0.0
        if not math.isfinite(change_pct):
            change_pct = 0.0
        return Quote(symbol=symbol, price=price, ts=ts, change_pct=change_pct,
                     open_interest=self._finite(fields[13]) if len(fields) > 13 else None)

    def _parse_history_row(self, row: Any) -> Optional[Bar]:
        if isinstance(row, dict):
            fields = (row.get("d"), row.get("o"), row.get("h"), row.get("l"), row.get("c"), row.get("v"), row.get("p", 0))
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            fields = (row[0], row[1], row[2], row[3], row[4], row[5] if len(row) > 5 else 0, row[6] if len(row) > 6 else 0)
        else:
            return None
        stamp, o, h, low, close, volume, hold = fields
        try:
            ts = datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=self._tz).timestamp()
        except ValueError:
            return None
        values = [self._finite(value) for value in (o, h, low, close, volume, hold)]
        if any(value is None for value in values[:4]):
            return None
        return Bar(ts=ts, open=values[0], high=values[1], low=values[2], close=values[3],
                   volume=values[4] if values[4] is not None else 0.0,
                   hold=values[5] if values[5] is not None else 0.0)


class FullUniverseScanner:
    """Build one immutable research manifest from a freshly validated catalogue."""

    def __init__(
        self,
        catalogue_path: str | Path,
        reader: ResearchMarketReader,
        *,
        clock: Callable[[], float] = time.time,
        quote_batch_size: int = SPOT_BATCH_SIZE,
        quote_workers: int = MAX_QUOTE_WORKERS,
        quote_budget_sec: float = 11.0,
        quote_max_age_sec: float = DEFAULT_QUOTE_MAX_AGE_SEC,
        history_workers: int = DEFAULT_HISTORY_WORKERS,
        history_budget_sec: float = DEFAULT_HISTORY_BUDGET_SEC,
        history_max_age_sec: float = DEFAULT_HISTORY_MAX_AGE_SEC,
        calendar_path: str | Path | None = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.catalogue_path = Path(catalogue_path)
        self.reader = reader
        self.clock = clock
        self.quote_batch_size = max(1, min(int(quote_batch_size), SPOT_BATCH_SIZE))
        self.quote_workers = max(1, min(int(quote_workers), MAX_QUOTE_WORKERS))
        self.quote_budget_sec = max(0.01, float(quote_budget_sec))
        self.quote_max_age_sec = max(0.0, float(quote_max_age_sec))
        self.history_workers = max(1, min(int(history_workers), MAX_HISTORY_WORKERS))
        self.history_budget_sec = max(0.01, float(history_budget_sec))
        self.history_max_age_sec = max(0.0, float(history_max_age_sec))
        self._session_calendar = ResearchSessionCalendar(calendar_path)
        self._cancel_event = cancel_event

    def run(self) -> Dict[str, Any]:
        started_at = float(self.clock())
        catalogue = load_catalogue(self.catalogue_path)
        catalogue_meta = self._catalogue_meta(catalogue)
        candidates = self._candidates(catalogue)
        if not catalogue.get("available"):
            return self._finish("unavailable", catalogue_meta, [], started_at, denominator_known=False, eligible_count=0)
        if catalogue.get("snapshot_status") != SNAPSHOT_COMPLETE:
            items = [self._excluded(candidate, "catalogue", "catalogue_incomplete") for candidate in candidates]
            return self._finish("incomplete", catalogue_meta, items, started_at, denominator_known=True, eligible_count=0)

        # Candidate identity is the denominator.  Market reads are allowed only
        # after the individual catalogue probe proved quote and all declared
        # history windows usable; top-level snapshot completeness alone is not
        # enough to make a row safe to analyse.
        items: List[Dict[str, Any]] = []
        catalogue_eligible: List[Dict[str, Any]] = []
        for candidate in candidates:
            reason = self._catalogue_reason(candidate)
            if reason:
                items.append(self._excluded(candidate, "catalogue", reason))
            else:
                catalogue_eligible.append(candidate)
        configure_candidates = getattr(self.reader, "configure_candidates", None)
        if callable(configure_candidates):
            try:
                configure_candidates(catalogue_eligible)
            except Exception:  # noqa: BLE001 - a provenance helper must not block the generic source path
                pass
        if self._cancelled():
            items.extend(self._excluded(candidate, "quote", "scan_cancelled") for candidate in catalogue_eligible)
            return self._finish("incomplete", catalogue_meta, items, started_at,
                                denominator_known=True, eligible_count=len(catalogue_eligible))

        symbols = [candidate["symbol"] for candidate in catalogue_eligible]
        quotes, quote_failures = self._load_quotes(symbols)
        quote_ready: List[Tuple[Dict[str, Any], Quote, str]] = []
        now = float(self.clock())
        for candidate in catalogue_eligible:
            symbol = candidate["symbol"]
            quote = quotes.get(symbol)
            reason, quote_mode = self._quote_reason(quote, now, candidate["exchange"])
            reason = quote_failures.get(symbol) or reason
            if reason:
                items.append(self._excluded(candidate, "quote", reason))
            else:
                quote_ready.append((candidate, quote, quote_mode))

        histories, history_failures = self._load_histories(quote_ready)
        now = float(self.clock())
        market_observations: List[Tuple[float, float, str]] = []
        for candidate, quote, quote_mode in quote_ready:
            symbol = candidate["symbol"]
            reason = history_failures.get(symbol)
            bars = histories.get(symbol)
            history_mode = ""
            completed: List[Bar] = []
            if not reason:
                reason, history_mode, completed = self._history_reason(bars, now, candidate["exchange"])
            if reason:
                items.append(self._excluded(candidate, "history", reason))
                continue
            metrics = self._metrics(completed)
            if metrics is None:
                items.append(self._excluded(candidate, "analysis", "history_insufficient"))
                continue
            items.append({
                "symbol": symbol, "name": candidate["name"], "exchange": candidate["exchange"],
                "outcome": "scanned", "stage": "analysis", "reason": "", "latest_price": self._round(quote.price),
                "change_pct": self._round(quote.change_pct), "trend": metrics["trend"], "score": metrics["score"],
            })
            market_observations.append((float(quote.ts), float(completed[-1].ts),
                                        "prior_session" if "prior_session" in {quote_mode, history_mode}
                                        else "live_session"))
        if self._cancelled():
            status = "incomplete"
        elif len(items) == len(candidates) and all(item["outcome"] == "scanned" for item in items):
            status = "complete"
        elif any(item.get("reason", "").endswith("_timeout") or item.get("reason") == "scan_cancelled"
                 for item in items):
            # A deadline proves that this is not merely a measured exclusion:
            # some source work could not finish within the bounded scan window.
            status = "incomplete"
        else:
            status = "partial"
        return self._finish(status, catalogue_meta, items, started_at, denominator_known=True,
                            eligible_count=len(catalogue_eligible), market=self._market_summary(market_observations))

    @staticmethod
    def _catalogue_meta(catalogue: Mapping[str, Any]) -> Dict[str, Any]:
        schema = catalogue.get("schema_version")
        return {
            "schema_version": int(schema) if isinstance(schema, int) and not isinstance(schema, bool) else 0,
            "content_hash": str(catalogue.get("content_hash") or "")[:80],
            "snapshot_status": str(catalogue.get("snapshot_status") or "unknown")[:32],
            "fetched_at": str(catalogue.get("fetched_at") or "")[:80],
        }

    @staticmethod
    def _candidates(catalogue: Mapping[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for raw in catalogue.get("candidates") or []:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or "").upper()
            if not _SYMBOL_RE.fullmatch(symbol):
                continue
            rows.append({
                "symbol": symbol,
                "name": str(raw.get("name") or symbol)[:120],
                "exchange": str(raw.get("exchange") or "")[:32],
                "source_node": str(raw.get("source_node") or "")[:80],
                "quote_ok": raw.get("quote_ok") is True,
                "history_5m_ok": raw.get("history_5m_ok") is True,
                "history_15m_ok": raw.get("history_15m_ok") is True,
                "history_1h_ok": raw.get("history_1h_ok") is True,
                "probe_status": str(raw.get("probe_status") or "")[:32],
            })
        return sorted(rows, key=lambda item: item["symbol"])

    @staticmethod
    def _catalogue_reason(candidate: Mapping[str, Any]) -> Optional[str]:
        if candidate.get("probe_status") != "ok":
            return "catalogue_probe_incomplete"
        if candidate.get("quote_ok") is not True:
            return "catalogue_quote_proof_missing"
        if any(candidate.get(key) is not True for key in ("history_5m_ok", "history_15m_ok", "history_1h_ok")):
            return "catalogue_history_proof_missing"
        return None

    def _cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def _load_quotes(self, symbols: Sequence[str]) -> Tuple[Dict[str, Quote], Dict[str, str]]:
        if not symbols:
            return {}, {}
        batches = [tuple(symbols[index:index + self.quote_batch_size]) for index in range(0, len(symbols), self.quote_batch_size)]
        results: Dict[str, Quote] = {}
        failures: Dict[str, str] = {}
        executor = ThreadPoolExecutor(max_workers=min(self.quote_workers, len(batches)), thread_name_prefix="research-quote")
        scheduled = iter(batches)
        active: Dict[Future, Tuple[str, ...]] = {}
        exhausted = False
        deadline = time.monotonic() + self.quote_budget_sec

        def submit_until_full() -> None:
            nonlocal exhausted
            while not exhausted and not self._cancelled() and len(active) < self.quote_workers and time.monotonic() < deadline:
                try:
                    batch = next(scheduled)
                except StopIteration:
                    exhausted = True
                    return
                active[executor.submit(self.reader.latest_batch, batch)] = batch

        try:
            submit_until_full()
            while active and not self._cancelled():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _pending = wait(tuple(active), timeout=remaining, return_when=FIRST_COMPLETED)
                if not done:
                    break
                for future in done:
                    batch = active.pop(future)
                    try:
                        response = future.result()
                    except Exception:  # noqa: BLE001 - public projection uses a fixed reason only
                        for symbol in batch:
                            failures[symbol] = "quote_unavailable"
                        continue
                    for symbol, quote in (response or {}).items():
                        key = str(symbol).upper()
                        if key in batch and isinstance(quote, Quote):
                            results[key] = quote
                submit_until_full()
            pending_reason = "scan_cancelled" if self._cancelled() else "quote_timeout"
            for future, batch in active.items():
                for symbol in batch:
                    failures[symbol] = pending_reason
                future.cancel()
            for batch in scheduled:
                for symbol in batch:
                    failures[symbol] = pending_reason
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return results, failures

    def _load_histories(self, candidates: Sequence[Tuple[Dict[str, Any], Quote, str]]) -> Tuple[Dict[str, Sequence[Bar]], Dict[str, str]]:
        if not candidates:
            return {}, {}
        results: Dict[str, Sequence[Bar]] = {}
        failures: Dict[str, str] = {}
        executor = ThreadPoolExecutor(max_workers=self.history_workers, thread_name_prefix="research-history")
        scheduled = iter(candidates)
        active: Dict[Future, str] = {}
        exhausted = False

        def submit_until_full() -> None:
            nonlocal exhausted
            while (not exhausted and not self._cancelled() and len(active) < self.history_workers
                   and time.monotonic() < deadline):
                try:
                    candidate, _quote, _mode = next(scheduled)
                except StopIteration:
                    exhausted = True
                    return
                active[executor.submit(self.reader.history_5m, candidate["symbol"])] = candidate["symbol"]

        deadline = time.monotonic() + self.history_budget_sec
        try:
            submit_until_full()
            while active and not self._cancelled():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _pending = wait(tuple(active), timeout=remaining, return_when=FIRST_COMPLETED)
                if not done:
                    break
                for future in done:
                    symbol = active.pop(future)
                    try:
                        result = future.result()
                    except Exception:  # noqa: BLE001 - do not retain upstream exception text
                        failures[symbol] = "history_unavailable"
                    else:
                        results[symbol] = result
                submit_until_full()
            pending_reason = "scan_cancelled" if self._cancelled() else "history_timeout"
            for future, symbol in active.items():
                failures[symbol] = pending_reason
                future.cancel()
            for candidate, _quote, _mode in scheduled:
                failures[candidate["symbol"]] = pending_reason
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return results, failures

    def _quote_reason(self, quote: Optional[Quote], now: float, exchange: str) -> Tuple[Optional[str], str]:
        if quote is None:
            return "quote_missing", ""
        values = (quote.price, quote.ts, quote.change_pct)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) for value in values):
            return "quote_invalid", ""
        if float(quote.price) <= 0:
            return "quote_invalid", ""
        if float(quote.ts) > now + MAX_FUTURE_SKEW_SEC:
            return "quote_future", ""
        return self._timestamp_reason(
            float(quote.ts), now, exchange, self.quote_max_age_sec, "quote_stale",
            session_close_grace_sec=self.quote_max_age_sec,
        )

    def _history_reason(self, bars: Optional[Sequence[Bar]], now: float, exchange: str) -> Tuple[Optional[str], str, List[Bar]]:
        """Validate one history series and return the completed bars it may analyse.

        The public feed labels each bar with its closing minute, so while a
        session is live the newest row is the bar still forming and its stamp
        is up to one interval ahead of the clock.  That row is not a completed
        fact: it is set aside, never analysed and never treated as corrupt
        data.  A stamp further ahead than one interval is still ``history_invalid``.
        """
        if not bars:
            return "history_unavailable", "", []
        if len(bars) < MIN_HISTORY_BARS:
            return "history_insufficient", "", []
        previous_ts = -math.inf
        completed: List[Bar] = []
        forming_limit = now + HISTORY_BAR_INTERVAL_SEC + MAX_FUTURE_SKEW_SEC
        for bar in bars:
            if not isinstance(bar, Bar):
                return "history_invalid", "", []
            values = (bar.ts, bar.open, bar.high, bar.low, bar.close)
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) for value in values):
                return "history_invalid", "", []
            if min(float(bar.open), float(bar.high), float(bar.low), float(bar.close)) <= 0:
                return "history_invalid", "", []
            if float(bar.low) > min(float(bar.open), float(bar.close)) or float(bar.high) < max(float(bar.open), float(bar.close)):
                return "history_invalid", "", []
            if float(bar.ts) <= previous_ts or float(bar.ts) > forming_limit:
                return "history_invalid", "", []
            previous_ts = float(bar.ts)
            if float(bar.ts) <= now + MAX_FUTURE_SKEW_SEC:
                completed.append(bar)
        if len(completed) < MIN_HISTORY_BARS:
            return "history_insufficient", "", []
        reason, mode = self._timestamp_reason(
            float(completed[-1].ts), now, exchange, self.history_max_age_sec, "history_stale",
            session_close_grace_sec=self.quote_max_age_sec,
        )
        return reason, mode, completed

    def _timestamp_reason(self, observed_at: float, now: float, exchange: str, max_age_sec: float,
                          stale_reason: str, *, session_close_grace_sec: float) -> Tuple[Optional[str], str]:
        """Validate one quote/bar timestamp without confusing a closed snapshot for a live tick.

        A recent observation is a live-session fact.  An older one is usable
        only as ``prior_session`` when it sits at an exchange session close
        whose scheduled gap still contains ``now``: the morning and midday
        recesses, the final day close until the next morning open, and the
        recognised night closes until that same open.  Nothing here infers a
        product's own session terms from the catalogue: a product that is still
        trading proves it with a live observation, and a stuck feed on a
        product that trades overnight can only ever be labelled
        ``prior_session``, never live.
        """
        if now - observed_at <= max_age_sec:
            return None, "live_session"
        local_now = datetime.fromtimestamp(now, _RESEARCH_TIMEZONE)
        boundaries = self._session_calendar.closed_session_boundaries(exchange, local_now)
        if boundaries is None:
            # Without a current trading calendar, an older observation must
            # not be promoted to a presumed session close.
            if _looks_like_session_close(exchange, observed_at, session_close_grace_sec):
                return "session_calendar_unavailable", ""
            return stale_reason, ""
        for boundary in boundaries:
            end_ts = boundary.timestamp()
            if end_ts - session_close_grace_sec <= observed_at <= end_ts + _RESEARCH_CLOSE_TIMESTAMP_GRACE_SEC:
                return None, "prior_session"
        return stale_reason, ""

    def _market_summary(self, observations: Sequence[Tuple[float, float, str]]) -> Optional[Dict[str, Any]]:
        """Return an auditable, bounded as-of disclosure for successfully scanned rows."""
        if not observations:
            return None
        quote_times = [quote_ts for quote_ts, _history_ts, _mode in observations]
        history_times = [history_ts for _quote_ts, history_ts, _mode in observations]
        prior_count = sum(mode == "prior_session" for _quote_ts, _history_ts, mode in observations)
        live_count = len(observations) - prior_count
        mode = "prior_session" if prior_count == len(observations) else (
            "live_session" if live_count == len(observations) else "mixed"
        )
        return {
            "quote_as_of_min": self._round(min(quote_times)),
            "quote_as_of_max": self._round(max(quote_times)),
            "history_as_of_min": self._round(min(history_times)),
            "history_as_of_max": self._round(max(history_times)),
            "freshness_mode": mode,
            "prior_session_count": prior_count,
            "live_session_count": live_count,
        }

    @staticmethod
    def _ema(values: Sequence[float], length: int) -> float:
        alpha = 2.0 / (length + 1.0)
        value = float(values[0])
        for current in values[1:]:
            value = alpha * float(current) + (1.0 - alpha) * value
        return value

    def _metrics(self, bars: Sequence[Bar]) -> Optional[Dict[str, Any]]:
        if len(bars) < MIN_HISTORY_BARS:
            return None
        closes = [float(bar.close) for bar in bars]
        latest = closes[-1]
        reference = closes[-21]
        if reference <= 0:
            return None
        change_20 = (latest / reference - 1.0) * 100.0
        ema_fast = self._ema(closes[-30:], 5)
        ema_slow = self._ema(closes[-60:], 20)
        if not all(math.isfinite(value) for value in (change_20, ema_fast, ema_slow)):
            return None
        if change_20 > 0 and ema_fast >= ema_slow:
            trend = "bullish"
        elif change_20 < 0 and ema_fast <= ema_slow:
            trend = "bearish"
        else:
            trend = "neutral"
        return {"trend": trend, "score": self._round(change_20)}

    @staticmethod
    def _round(value: float) -> float:
        return round(float(value), 6)

    @staticmethod
    def _excluded(candidate: Mapping[str, Any], stage: str, reason: str) -> Dict[str, Any]:
        return {"symbol": str(candidate["symbol"]), "name": str(candidate["name"]), "exchange": str(candidate["exchange"]),
                "outcome": "excluded", "stage": stage, "reason": reason, "latest_price": None,
                "change_pct": None, "trend": None, "score": None}

    def _finish(self, status: str, catalogue: Mapping[str, Any], items: Iterable[Mapping[str, Any]], started_at: float,
                *, denominator_known: bool, eligible_count: int,
                market: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        ordered = [dict(item) for item in sorted(items, key=lambda item: str(item.get("symbol") or ""))]
        scanned = sum(1 for item in ordered if item.get("outcome") == "scanned")
        excluded = sum(1 for item in ordered if item.get("outcome") == "excluded")
        discovered = len(ordered) if denominator_known else 0
        eligible = max(0, min(int(eligible_count), discovered))
        # A partially trusted catalogue has no trustworthy denominator.  Runtime
        # exclusions from an otherwise complete catalogue may still report the
        # observed ratio, but a partial/unknown catalogue must never look like a
        # measured zero-percent scan.
        ratio = self._round(scanned / discovered) if discovered and status in {"complete", "partial"} else None
        coverage_status = "complete" if status == "complete" else ("unavailable" if status == "unavailable" else "incomplete")
        manifest: Dict[str, Any] = {
            "schema_version": SCAN_SCHEMA_VERSION,
            "rules_version": SCAN_RULES_VERSION,
            "status": status,
            "catalogue": dict(catalogue),
            "counts": {"discovered": discovered, "eligible": eligible, "scanned": scanned, "excluded": excluded},
            "coverage": {"status": coverage_status, "ratio": ratio, "denominator_known": bool(denominator_known)},
            "started_at": self._round(started_at),
            "completed_at": self._round(float(self.clock())),
            "items": ordered,
        }
        if market is not None:
            manifest["market"] = dict(market)
        manifest["manifest_hash"] = canonical_manifest_hash(manifest)
        return manifest

    @staticmethod
    def _manifest_hash(manifest: Mapping[str, Any]) -> str:
        return canonical_manifest_hash(manifest)

    @classmethod
    def failed_manifest(cls, catalogue_path: str | Path, *, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
        now = float(clock())
        scanner = cls(catalogue_path, _NoopReader(), clock=clock)
        return scanner._finish(
            "unavailable",
            {"schema_version": 0, "content_hash": "", "snapshot_status": "unknown", "fetched_at": ""},
            [],
            now,
            denominator_known=False,
            eligible_count=0,
        )


class _NoopReader:
    def latest_batch(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        return {}

    def history_5m(self, symbol: str) -> Sequence[Bar]:
        return []


class ResearchScanService:
    """Single-flight background runner; completed results are append-only in the database."""

    def __init__(
        self,
        db: Any,
        catalogue_path: str | Path,
        *,
        reader_factory: Optional[Callable[[], ResearchMarketReader]] = None,
        timeout_sec: float = 10.0,
        calendar_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._db = db
        self._catalogue_path = Path(catalogue_path)
        self._calendar_path = Path(calendar_path) if calendar_path is not None else None
        self._reader_factory = reader_factory or (lambda: SinaResearchReader(timeout_sec=timeout_sec))
        self._clock = clock
        self._lock = threading.Lock()
        self._running = False
        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self._reader: Optional[ResearchMarketReader] = None
        self._cancel_event: Optional[threading.Event] = None

    def public_status(self) -> Dict[str, Any]:
        with self._lock:
            running = self._running
        return {"running": running, "latest": self._db.latest_research_scan()}

    def start(self) -> Tuple[bool, str]:
        with self._lock:
            if self._stopped:
                return False, "扫描服务已停止"
            if self._running:
                return False, "全量扫描正在进行中"
            self._cancel_event = threading.Event()
            self._running = True
            self._thread = threading.Thread(target=self._run, args=(self._cancel_event,),
                                            name="research-universe-scan", daemon=True)
            self._thread.start()
        return True, "已启动全量研究扫描；不会调用模型或参与下单"

    def stop(self) -> None:
        """Cancel a run before database shutdown without racing an in-flight reader close.

        A network request already inside its client timeout cannot be force-killed
        safely.  We therefore cancel scheduling, prevent persistence under the
        service lock, and intentionally leave the shared reader open for process
        teardown rather than closing it while a worker may still be using it.
        """
        with self._lock:
            self._stopped = True
            if self._cancel_event is not None:
                self._cancel_event.set()
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)

    def _run(self, cancel_event: threading.Event) -> None:
        manifest: Optional[Dict[str, Any]] = None
        try:
            with self._lock:
                if self._stopped:
                    return
                if self._reader is None:
                    self._reader = self._reader_factory()
                reader = self._reader
            scanner = FullUniverseScanner(
                self._catalogue_path,
                reader,
                calendar_path=self._calendar_path,
                cancel_event=cancel_event,
                clock=self._clock,
            )
            manifest = scanner.run()
        except Exception:  # noqa: BLE001 - never retain runtime/source details in a public result
            manifest = FullUniverseScanner.failed_manifest(self._catalogue_path)
        finally:
            with self._lock:
                # Keep the append and stopped check in the same critical section
                # as stop(), so application shutdown cannot close the database
                # between a successful check and a background append.
                try:
                    if not self._stopped and manifest is not None:
                        self._db.append_research_scan(manifest)
                except Exception:  # noqa: BLE001 - a rejected public projection must not strand single-flight state
                    pass
                finally:
                    self._running = False
