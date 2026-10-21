"""Read-only candidate contract universe (discovery output, never a trading pool).

The catalogue produced by ``scripts/discover_contract_universe.py`` can prove
exactly three things about a candidate: its Sina continuous code, its Chinese
name and the exchange that lists it.  It can prove nothing about multiplier,
tick, margin ratio, fee or trading sessions, so this module refuses to carry
those keys at all: anything outside ``CANDIDATE_FIELDS`` is a hard validation
failure, and ``FORBIDDEN_SPEC_KEYS`` makes the refusal explicit.

``validate_catalogue`` is the single gate for both directions -- discovery calls
it before writing, ``sanitize_catalogue`` calls it before exposing -- so a
hand-edited catalogue can never be mistaken for a verified specification by the
engine, the matching layer or the dashboard.  A document that fails the gate on
read is reported as unavailable in full; it is never partially projected, since
dropping the offending field and serving the rest would hide the edit.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

DEFAULT_UNIVERSE_FILE = "config/contracts_universe_cn.json"
SCHEMA_VERSION = 1

# Matching parameters are the exclusive property of the verified pool
# (config/contracts_cn.json) and must be sourced from exchange documents.
# Common aliases are listed too: the field whitelist already rejects every
# unknown key, but naming the alias makes the refusal say *why* it was refused
# instead of the vaguer "unsupported field".
FORBIDDEN_SPEC_KEYS = frozenset({
    "multiplier", "contract_multiplier", "contract_size",
    "tick", "tick_size", "price_tick", "min_tick",
    "margin", "margin_ratio", "margin_rate", "max_leverage",
    "fee", "fee_per_lot", "fee_rate", "commission", "commission_rate",
    "sessions", "trading_hours", "lot_step", "holidays", "verified",
})

# Availability flags are tri-state: True / False / None ("not probed").
AVAILABILITY_FIELDS = ("quote_ok", "history_5m_ok", "history_15m_ok", "history_1h_ok")
# Where the row came from upstream, kept so a committed snapshot stays auditable:
# the exchange board it was listed under and the node id the code was resolved from.
PROVENANCE_FIELDS = ("source_group", "source_node")
# The continuous code is stored explicitly rather than left implicit in the map
# key, so a record always carries the identifier the upstream quote row gave it.
CANDIDATE_FIELDS = (("continuous_symbol", "name", "exchange") + PROVENANCE_FIELDS
                    + AVAILABILITY_FIELDS + ("probe_status", "probe_error", "probed_at"))

SYMBOL_PATTERN = r"^[A-Z]{1,3}0$"
_SYMBOL_RE = re.compile(SYMBOL_PATTERN)

# A snapshot is complete only when every listed node resolved and every candidate
# finished its probes.  Anything less stays "partial" and must not overwrite a
# complete predecessor -- see scripts/discover_contract_universe.py:write_catalogue.
SNAPSHOT_COMPLETE = "complete"
SNAPSHOT_PARTIAL = "partial"
SNAPSHOT_UNKNOWN = "unknown"
SNAPSHOT_STATUSES = (SNAPSHOT_COMPLETE, SNAPSHOT_PARTIAL, SNAPSHOT_UNKNOWN)

# No candidate is exchange-verified in this phase.  The vocabulary only says how
# much local material exists behind a code, never that it may be traded:
#   legacy_unverified -- an older local spec exists in config/contracts_cn.json
#   unknown           -- nothing local at all
VERIFICATION_LEGACY = "legacy_unverified"
VERIFICATION_UNKNOWN = "unknown"

DISCLAIMER = (
    "候选目录是对公开代码表的只读发现结果，不是默认交易池，也不代表交易所核验或可下单授权。"
    "本期未对任何候选做交易所条款核验；目录不含合约乘数、最小变动价位、保证金比例、手续费与交易时段。"
)


def verification_status(symbol: str, configured: Set[str]) -> str:
    """Say what local material backs a code -- never that it is tradable."""
    return VERIFICATION_LEGACY if symbol in configured else VERIFICATION_UNKNOWN


def content_hash(candidates: Any) -> str:
    """Fingerprint the candidate block so a committed snapshot can be re-checked.

    Only the candidates are hashed: timestamps change on every run and would
    make the digest useless for spotting an edited or truncated catalogue.
    """
    canonical = json.dumps(candidates, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def forbidden_keys_in(payload: Any) -> Set[str]:
    """Collect every matching-parameter key anywhere in a catalogue document."""
    found: Set[str] = set()
    if isinstance(payload, dict):
        found |= FORBIDDEN_SPEC_KEYS & set(payload)
        for value in payload.values():
            found |= forbidden_keys_in(value)
    elif isinstance(payload, list):
        for value in payload:
            found |= forbidden_keys_in(value)
    return found


def validate_catalogue(doc: Any) -> None:
    """Prove a whole catalogue document is an identity record set and nothing more.

    Both ends of the pipeline go through this one function: the discovery writer
    calls it before persisting, and the reader calls it before exposing anything.
    Sanitising field by field is not enough on the read side -- a local edit can
    add a multiplier and recompute a perfectly valid ``content_hash``, and a
    per-field projection would quietly drop the multiplier and still serve the
    document as available.  A file that cannot prove it is identity-only is
    refused whole.

    Field-level refusals run *before* the count/status/hash checks: a record
    carrying a multiplier is wrong in a way no amount of re-hashing can fix, and
    reporting it as "content_hash does not match" would both understate the
    failure and invite someone to recompute the digest over the polluted block.

    Raises ``ValueError`` describing the first violation found.
    """
    if not isinstance(doc, dict):
        raise ValueError("catalogue must be a document mapping")
    candidates = doc.get("candidates")
    # A non-mapping candidates value is rejected here rather than treated as an
    # empty list: ``candidates: []`` hashes perfectly well and would otherwise
    # be served as an available, "complete" catalogue of nothing.
    if not isinstance(candidates, dict):
        raise ValueError("catalogue must carry a candidates mapping")
    for symbol, entry in candidates.items():
        _validate_candidate(str(symbol), entry)
    # Anything outside the candidate records is swept too, so a matching
    # parameter cannot hide at document level or nested inside a stray value.
    banned = forbidden_keys_in(doc)
    if banned:
        raise ValueError(f"catalogue carries matching parameters: {sorted(banned)}")
    if doc.get("count") != len(candidates):
        raise ValueError("catalogue count must equal the number of candidates")
    if doc.get("snapshot_status") not in SNAPSHOT_STATUSES:
        raise ValueError(f"unknown snapshot_status: {doc.get('snapshot_status')!r}")
    if doc.get("content_hash") != content_hash(candidates):
        raise ValueError("content_hash does not match the candidates block")


def _validate_candidate(symbol: str, entry: Any) -> None:
    """Prove one entry identifies a candidate and says nothing about trading it."""
    if not isinstance(entry, dict):
        raise ValueError(f"{symbol} is not a candidate record")
    # Matching parameters are reported first: they are the failure this guard
    # exists for, and "unsupported field" would understate them.
    banned = FORBIDDEN_SPEC_KEYS & set(entry)
    if banned:
        raise ValueError(f"{symbol} carries matching parameters: {sorted(banned)}")
    extra = set(entry) - set(CANDIDATE_FIELDS)
    if extra:
        raise ValueError(f"{symbol} carries unsupported fields: {sorted(extra)}")
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"invalid continuous symbol: {symbol!r}")
    # The stored code is the record's own identity, not an echo of the map key.
    if entry.get("continuous_symbol") != symbol:
        raise ValueError(f"{symbol} disagrees with its continuous_symbol: "
                         f"{entry.get('continuous_symbol')!r}")


def _flag(value: Any) -> Optional[bool]:
    """Keep the tri-state honest: only a real boolean means "probed"."""
    return value if isinstance(value, bool) else None


def _text(value: Any, limit: int = 120) -> str:
    return "" if value is None else str(value)[:limit]


def sanitize_candidate(key: str, raw: Any) -> Optional[Dict[str, Any]]:
    """Project one raw entry onto the allowed identity/availability fields.

    The public ``symbol`` is read back from the stored ``continuous_symbol`` --
    the code the upstream quote row carried -- so the dashboard shows the
    persisted identifier rather than a second guess made from the map key, the
    Chinese name or the source node.  A record that cannot produce that code, or
    that disagrees with the key it is filed under, is not identifiable and is
    dropped.
    """
    if not isinstance(raw, dict):
        return None
    symbol = str(raw.get("continuous_symbol", "")).strip().upper()
    if not _SYMBOL_RE.match(symbol) or symbol != str(key).strip().upper():
        return None
    out: Dict[str, Any] = {"symbol": symbol, "name": _text(raw.get("name")) or symbol,
                           "exchange": _text(raw.get("exchange"), 16)}
    for field in PROVENANCE_FIELDS:
        out[field] = _text(raw.get(field), 32)
    for field in AVAILABILITY_FIELDS:
        out[field] = _flag(raw.get(field))
    out["probe_status"] = _text(raw.get("probe_status"), 16)
    out["probe_error"] = _text(raw.get("probe_error"), 40)
    out["probed_at"] = _text(raw.get("probed_at"), 40)
    return out


def sanitize_catalogue(payload: Any) -> Dict[str, Any]:
    """Build the public view of a catalogue file, dropping anything unexpected.

    Nothing is exposed until ``validate_catalogue`` has proved the *whole*
    document: structure, per-record fields and the content hash.  Both an edited
    file and a self-consistently re-hashed one degrade to an empty, clearly
    unavailable catalogue -- never to a partial view, and never to a stale
    ``complete`` status.  Refusing costs nothing here: the reader is offline and
    simply reports an unknown catalogue.

    The projection below then narrows each record to the public fields, so the
    dashboard cannot render an unknown key as if discovery had produced it.
    """
    try:
        validate_catalogue(payload)
    except ValueError:
        return _empty_catalogue()
    raw_candidates = payload.get("candidates")
    candidates: List[Dict[str, Any]] = []
    if isinstance(raw_candidates, dict):
        for symbol, entry in raw_candidates.items():
            item = sanitize_candidate(str(symbol), entry)
            if item is not None:
                candidates.append(item)
    candidates.sort(key=lambda c: (c["exchange"], c["symbol"]))
    status = _text(payload.get("snapshot_status"), 16)
    return {"available": True, "source": _text(payload.get("source"), 200),
            # The schema the file declares, reported as 0 when it declares
            # nothing usable.  Downstream artefacts bind to this value, so a
            # catalogue whose schema moves can be told apart from one that did
            # not -- see arena/admission.py:catalogue_binding.
            "schema_version": _declared_schema_version(payload),
            "source_url": _text(payload.get("source_url"), 200),
            "fetched_at": _text(payload.get("fetched_at"), 40),
            # An unrecognised status is reported as unknown rather than assumed
            # complete: a hand-edited file must not be able to claim coverage.
            "snapshot_status": status if status in SNAPSHOT_STATUSES else SNAPSHOT_UNKNOWN,
            "content_hash": _text(payload.get("content_hash"), 80),
            # Derived from what is actually present: a stale stored count must
            # never be able to overstate the catalogue.
            "count": len(candidates), "candidates": candidates}


def _declared_schema_version(payload: Any) -> int:
    """The catalogue's own schema version, or 0 when it does not declare one."""
    value = payload.get("schema_version") if isinstance(payload, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _empty_catalogue() -> Dict[str, Any]:
    return {"available": False, "source": "", "schema_version": 0, "source_url": "", "fetched_at": "",
            "snapshot_status": SNAPSHOT_UNKNOWN, "content_hash": "", "count": 0, "candidates": []}


def load_catalogue(path: str | Path) -> Dict[str, Any]:
    """Load the catalogue, or a safe empty one when it is absent or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return _empty_catalogue()
    return sanitize_catalogue(payload)


def load_configured_symbols(path: str | Path) -> Set[str]:
    """Symbols that already have a local specification in the contracts table."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        contracts = raw.get("contracts")
    except (OSError, ValueError):
        return set()
    return set(contracts) if isinstance(contracts, dict) else set()


class CatalogueCache:
    """Reload the catalogue only when the file changes; the API path is low frequency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp: Any = None
        self._value: Optional[Dict[str, Any]] = None

    def get(self, path: str | Path) -> Dict[str, Any]:
        try:
            st = Path(path).stat()
            stamp = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = (str(path), None, None)
        with self._lock:
            if self._value is not None and stamp == self._stamp:
                return self._value
        value = load_catalogue(path)
        with self._lock:
            self._stamp, self._value = stamp, value
        return value
