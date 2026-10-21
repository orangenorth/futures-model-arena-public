"""合约规格与交易时段。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..models import (SPEC_ORIGIN_LOCAL_TABLE, SPEC_ORIGIN_RECOVERY_FALLBACK, SPEC_ORIGIN_UNSPECIFIED,
                      SPEC_ORIGIN_VENUE, ContractSpec)

log = logging.getLogger("arena.contracts")

# Terms that change simulation semantics.  When any of them moves, results
# recorded before and after are no longer comparable on one leaderboard.
# name / exchange are display-only and stay out of the digest; origin records
# provenance rather than a contract term.
SIMULATION_TERM_FIELDS = ("multiplier", "tick", "margin_ratio", "fee_per_lot",
                          "fee_rate", "sessions", "lot_step", "is_crypto")

# Domestic futures always trade in whole lots.  This is the one term the loader
# is allowed to supply, because it is a property of the market rather than of an
# individual contract; every other term must be written out in the table.
CN_LOT_STEP = 1.0

REQUIRED_POSITIVE_TERMS = ("multiplier", "tick", "margin_ratio")
FEE_TERMS = ("fee_per_lot", "fee_rate")
# Overnight windows are written with 24+ hours (21:00..26:30 closes at 02:30 the
# next day), so a session boundary may exceed one day but never two.
MAX_SESSION_MINUTE = 48 * 60
_SESSION_TIME_RE = re.compile(r"(\d{1,2}):([0-5]\d)")
_MISSING = object()


class ContractSpecError(ValueError):
    """One or more active symbols have no usable local specification.

    Raised before any market source, exchange, risk manager, model call,
    competition rollover or database write, and it always reports every
    offending symbol and field at once: failing one at a time would make an
    operator repair the table across as many restarts as there are bad rows.
    """

    def __init__(self, problems: Mapping[str, Iterable[str]], contracts_file: str | Path):
        self.problems: Dict[str, Tuple[str, ...]] = {
            str(sym): tuple(sorted(set(issues))) for sym, issues in sorted(problems.items())
        }
        self.symbols: List[str] = list(self.problems)
        self.contracts_file = str(contracts_file)
        detail = "; ".join(f"{sym} [{', '.join(issues)}]" for sym, issues in self.problems.items())
        super().__init__(
            f"{len(self.symbols)} active symbol(s) lack a usable specification in {self.contracts_file}: {detail}. "
            "Every active symbol needs an explicit multiplier, tick and margin_ratio, at least one fee term "
            "(fee_per_lot and/or fee_rate) and a non-empty sessions list. No term is defaulted, and the candidate "
            "catalogue is display-only: listing a code there never makes it tradable."
        )


def _load_contract_document(path: str | Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw if isinstance(raw, dict) else {}


def _spec_from_row(sym: str, c: Mapping[str, Any]) -> ContractSpec:
    """Build a spec from a row, recording whether that row was actually complete.

    Reading stays lenient -- an archived competition's table and a leftover
    position both need rows nobody trades today -- but a row that would not pass
    validation is marked ``unspecified`` rather than dressed up in defaults.  Only
    a row that carries every execution term explicitly earns ``local_table``, and
    only that origin may price a new position.
    """
    origin = SPEC_ORIGIN_LOCAL_TABLE if not _row_problems(c) else SPEC_ORIGIN_UNSPECIFIED
    return ContractSpec(
        symbol=sym, name=c.get("name", sym), exchange=c.get("exchange", ""),
        multiplier=float(_finite(c.get("multiplier")) or 1), tick=float(_finite(c.get("tick")) or 1),
        margin_ratio=float(_finite(c.get("margin_ratio")) or 0.1),
        fee_per_lot=float(_finite(c.get("fee_per_lot")) or 0),
        fee_rate=float(_finite(c.get("fee_rate")) or 0),
        sessions=c.get("sessions") if isinstance(c.get("sessions"), list) else [],
        lot_step=CN_LOT_STEP, is_crypto=False, origin=origin,
    )


def load_cn_contracts(path: str | Path) -> Tuple[Dict[str, ContractSpec], List[str]]:
    """Read the whole table leniently.

    Rows are also read for symbols nobody trades today -- an archived
    competition's own table, or a leftover position -- so this stays tolerant.
    Anything that will actually be traded must go through
    ``load_active_cn_contracts`` / ``validate_active_cn_contracts`` first, which
    refuse to let a defaulted term reach matching.
    """
    raw = _load_contract_document(path)
    contracts = raw.get("contracts")
    rows = contracts if isinstance(contracts, dict) else {}
    specs = {sym: _spec_from_row(sym, c) for sym, c in rows.items() if isinstance(c, dict)}
    holidays = raw.get("holidays")
    return specs, list(holidays) if isinstance(holidays, list) else []


def _finite(value: Any) -> Optional[float]:
    """Return the value as a float only when it is a real, finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _session_minute(value: Any) -> Optional[int]:
    if not isinstance(value, str):
        return None
    match = _SESSION_TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    minute = int(match.group(1)) * 60 + int(match.group(2))
    return minute if 0 <= minute <= MAX_SESSION_MINUTE else None


def _session_problems(sessions: Any) -> List[str]:
    """An absent or empty session list would read as "always open" to session_open()."""
    if sessions is _MISSING:
        return ["sessions is missing (an absent list would be read as 24/7 trading)"]
    if not isinstance(sessions, list) or not sessions:
        return ["sessions must be a non-empty list of [start, end] pairs"]
    issues: List[str] = []
    for index, window in enumerate(sessions):
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            issues.append(f"sessions[{index}] must be a [start, end] pair")
            continue
        bounds = []
        for label, value in zip(("start", "end"), window):
            minute = _session_minute(value)
            if minute is None:
                issues.append(f"sessions[{index}] {label} must be HH:MM within 00:00..48:00 (got {value!r})")
            bounds.append(minute)
        if None not in bounds and bounds[0] >= bounds[1]:
            issues.append(f"sessions[{index}] must end after it starts (got {list(window)!r})")
    return issues


def _row_problems(row: Mapping[str, Any]) -> List[str]:
    issues: List[str] = []
    for field in REQUIRED_POSITIVE_TERMS:
        if field not in row:
            issues.append(f"{field} is missing")
            continue
        number = _finite(row[field])
        if number is None or number <= 0:
            issues.append(f"{field} must be a finite positive number (got {row[field]!r})")
    declared_fees = [field for field in FEE_TERMS if field in row]
    if not declared_fees:
        issues.append("no fee term declared (set fee_per_lot and/or fee_rate, zero is allowed but must be explicit)")
    for field in declared_fees:
        number = _finite(row[field])
        if number is None or number < 0:
            issues.append(f"{field} must be a finite non-negative number (got {row[field]!r})")
    issues.extend(_session_problems(row.get("sessions", _MISSING)))
    # lot_step is the loader's invariant; a row that declares a different value
    # would be silently ignored, so say so instead of trading under the wrong one.
    if "lot_step" in row and _finite(row["lot_step"]) != CN_LOT_STEP:
        issues.append(f"lot_step must be {CN_LOT_STEP:g} for domestic futures (got {row['lot_step']!r})")
    return issues


def validate_active_cn_contracts(symbols: Iterable[str], contracts_file: str | Path,
                                 document: Optional[Mapping[str, Any]] = None) -> None:
    """Prove every active symbol carries an explicit, valid specification.

    No term is defaulted here: a row that omits a multiplier or a session list
    is an error, not an invitation to invent one.  Pass ``document`` to validate
    a table that has already been read, so the check and the load cannot see two
    different versions of the file.
    """
    active = list(dict.fromkeys(str(s) for s in symbols))
    if document is None:
        try:
            document = _load_contract_document(contracts_file)
        except (OSError, ValueError) as exc:
            raise ContractSpecError({sym: [f"contract table could not be read ({exc})"] for sym in active},
                                    contracts_file) from exc
    contracts = document.get("contracts")
    rows = contracts if isinstance(contracts, dict) else {}
    problems: Dict[str, List[str]] = {}
    for sym in active:
        row = rows.get(sym)
        if not isinstance(row, dict):
            problems[sym] = ["not listed in the contract table"]
            continue
        issues = _row_problems(row)
        if issues:
            problems[sym] = issues
    if problems:
        raise ContractSpecError(problems, contracts_file)


def load_active_cn_contracts(symbols: Iterable[str],
                             contracts_file: str | Path) -> Tuple[Dict[str, ContractSpec], List[str]]:
    """Validate the active rows and return the whole table, from a single read."""
    try:
        document = _load_contract_document(contracts_file)
    except (OSError, ValueError) as exc:
        raise ContractSpecError({str(sym): [f"contract table could not be read ({exc})"] for sym in symbols},
                                contracts_file) from exc
    validate_active_cn_contracts(symbols, contracts_file, document=document)
    contracts = document.get("contracts")
    rows = contracts if isinstance(contracts, dict) else {}
    specs = {sym: _spec_from_row(sym, c) for sym, c in rows.items() if isinstance(c, dict)}
    holidays = document.get("holidays")
    return specs, list(holidays) if isinstance(holidays, list) else []


def preflight_contract_specs(market: str, symbols: Iterable[str], contracts_file: str | Path) -> None:
    """Entry-point gate: refuse a bad configuration before anything is created.

    The CLI calls this before it takes the instance lock or opens the database,
    so merely attempting to start with an unusable table cannot create a schema
    or disturb a competition that is already running.  Crypto specs come from the
    venue rather than this table, so there is nothing local to check there.
    """
    if market == "crypto":
        return
    validate_active_cn_contracts(symbols, contracts_file)


def contract_terms_digest(specs: Mapping[str, ContractSpec]) -> str:
    """Deterministic fingerprint of the terms in force, independent of file path.

    Competition identity hangs on this: when the table is edited in place the
    path is unchanged, and results from before and after the edit must not share
    a leaderboard.
    """
    rows = {sym: {field: getattr(specs[sym], field) for field in SIMULATION_TERM_FIELDS}
            for sym in sorted(specs)}
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_cn_symbols(path: str | Path, include_inactive: bool = False) -> List[str]:
    """合约表里的品种代码，按表中的交易所分组顺序返回。

    默认跳过 `active: false` 的品种：交易所时段表说它们开市，行情源却可能一直给不出新的
    分钟 bar。把它们配进 `symbols` 的代价不只是白占 prompt 体积——放开品种时正是它们让
    新鲜度闸门整轮停下（见 `docs/design.md`）。
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [sym for sym, c in raw.get("contracts", {}).items()
            if include_inactive or c.get("active", True)]


def load_calendar_valid_until(path: str | Path) -> Optional[date]:
    """合约表中内置节假日表的有效期（calendar_valid_until），缺省取 holidays 中的最后一天。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        val = raw.get("calendar_valid_until")
        if not val:
            hol = sorted(raw.get("holidays", []))
            val = hol[-1] if hol else None
        return date.fromisoformat(val) if val else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def legacy_fallback_cn_spec(sym: str, reason: str) -> ContractSpec:
    """Recovery-only spec for a code the table no longer lists but the database still holds.

    These are generic conservative values, not this product's real terms, so the
    result carries ``origin="recovery_fallback"`` and matching/risk will close a
    position under it but never open one.  An active symbol can never reach this
    path: ``validate_active_cn_contracts`` stops an unusable configuration at
    construction.  Callers must state a reason so the degradation is traceable.
    """
    log.warning("%s is absent from the contract table; using a recovery-only spec (%s). "
                "These are not the product's real terms and may only close out or replay a fill.", sym, reason)
    return ContractSpec(symbol=sym, name=sym, multiplier=10, tick=1, margin_ratio=0.12, fee_rate=0.0001,
                        sessions=[["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"], ["21:00", "23:00"]],
                        origin=SPEC_ORIGIN_RECOVERY_FALLBACK)


def crypto_spec(sym: str, tick: float = 0.1, lot_step: float = 0.001, fee_rate: float = 0.0005, max_leverage: float = 20) -> ContractSpec:
    # Origin "venue": every term is supplied deliberately from the perpetual's own
    # rules, and an empty sessions list is correct here because the venue trades
    # continuously -- unlike a domestic row, where an empty list means "unstated".
    base = sym.split("-")[0].split("USDT")[0] if sym else sym
    return ContractSpec(symbol=sym, name=f"{base} 永续", exchange="OKX", multiplier=1.0, tick=tick,
                        margin_ratio=1.0 / max_leverage, fee_per_lot=0, fee_rate=fee_rate, sessions=[],
                        lot_step=lot_step, is_crypto=True, origin=SPEC_ORIGIN_VENUE)


def _hm(s: str) -> Tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def session_open(spec: ContractSpec, now: datetime, holidays: Optional[List[str]] = None) -> bool:
    """判断 now（本地时区）是否处于合约交易时段。夜盘跨日以 24+ 小时表示，归属前一交易日。"""
    if spec.is_crypto or not spec.sessions:
        return True
    hol = set(holidays or [])
    minutes = now.hour * 60 + now.minute
    today = now.date()
    for s_start, s_end in spec.sessions:
        sh, sm = _hm(s_start)
        eh, em = _hm(s_end)
        start, end = sh * 60 + sm, eh * 60 + em
        is_night = start >= NIGHT_START_MIN
        if end <= 24 * 60:
            # 同日时段；夜盘（含 23:00 前收盘的）还需满足"次一交易日不是节假日"
            if start <= minutes < end and _is_trading_day(today, hol) and (not is_night or _night_allowed(today, hol)):
                return True
        else:
            # 跨日夜盘：当日 start..24:00 属当日；次日 0:00..end-24h 属前一日
            if minutes >= start and _is_trading_day(today, hol) and _night_allowed(today, hol):
                return True
            prev = today - timedelta(days=1)
            if minutes < end - 24 * 60 and _is_trading_day(prev, hol) and _night_allowed(prev, hol):
                return True
    return False


NIGHT_START_MIN = 18 * 60  # 18:00 以后开始的时段视为夜盘


# 研究用的上一节收盘价：最后一笔行情必须落在最近一节收盘前这么多秒之内。行情源在盘中
# 掉线、重启前又没接回来的话，停在 14:00 的那笔不是收盘价，不能当收盘价送进研究。
# 留 5 分钟是因为清淡品种收盘前最后几分钟可能一笔报价更新都没有，CTP 没有变化就不推。
CLOSED_SESSION_QUOTE_GRACE_SEC = 300.0


def latest_session_close(spec: ContractSpec, current: float, tz, holidays: Optional[List[str]] = None,
                         lookback_days: int = 20) -> Optional[float]:
    """该品种最近一节**已经结束**的交易时段的收盘时刻（≤ current）。跨日夜盘按其实际结束的那一刻算。

    `tz` 是交易所时区：时段表写的是交易所本地时刻。
    """
    now_dt = datetime.fromtimestamp(current, tz)
    day = now_dt.date()
    best: Optional[float] = None
    for _ in range(lookback_days):
        midnight = datetime.combine(day, dtime(0, 0), tzinfo=tz)
        for session in spec.sessions or []:
            try:
                hour_text, minute_text = str(session[1]).split(":")
                end_minute = int(hour_text) * 60 + int(minute_text)
            except (TypeError, ValueError, IndexError):
                continue
            end_dt = midnight + timedelta(minutes=end_minute)
            if end_dt > now_dt or not session_open(spec, end_dt - timedelta(minutes=1), holidays):
                continue        # 还没收盘，或者这一天根本没有这一节（周末、节假日、节前无夜盘）
            end_ts = end_dt.timestamp()
            if best is None or end_ts > best:
                best = end_ts
        if best is not None:
            return best
        day -= timedelta(days=1)
    return None


def _is_trading_day(d: date, hol: set) -> bool:
    return d.weekday() < 5 and d.isoformat() not in hol


def _night_allowed(d: date, hol: set) -> bool:
    """节假日前一个交易日无夜盘：交易日 d 之后的下一个工作日若是节假日，则 d 当晚无夜盘。"""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.isoformat() not in hol


def next_open(spec: ContractSpec, now: datetime, holidays: Optional[List[str]] = None, horizon_days: int = 15) -> Optional[datetime]:
    if spec.is_crypto or not spec.sessions:
        return now
    t = now.replace(second=0, microsecond=0)
    for _ in range(horizon_days * 24 * 60):
        t += timedelta(minutes=1)
        if session_open(spec, t, holidays):
            return t
    return None


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 10)
