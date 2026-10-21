"""Deterministic full-universe research projection for pre-open debates.

The candidate catalogue is not an execution universe.  This module receives a
completed read-only scan and produces two different artifacts from it:

* a complete, immutable per-candidate projection kept in the debate snapshot;
* a bounded packet of aggregate and representative facts that may be sent to a
  research model.

Keeping those artifacts separate is intentional.  Every candidate contributes
to the deterministic technical breadth, while the model prompt stays bounded
as the catalogue grows.  Nothing in this module imports execution, risk,
accounts, or an LLM client.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .research_scan import (
    PUBLIC_SCAN_OUTCOMES,
    PUBLIC_SCAN_REASONS,
    PUBLIC_SCAN_STAGE_REASONS,
    PUBLIC_SCAN_STAGES,
    PUBLIC_SCAN_TRENDS,
    SCAN_RULES_VERSION,
    SCAN_SCHEMA_VERSION,
    canonical_manifest_hash,
)


PREOPEN_UNIVERSE_SCHEMA_VERSION = "preopen-universe/v1"
MODEL_PACKET_SCHEMA_VERSION = "preopen-universe-packet/v1"
DEFAULT_MODEL_ROWS_PER_SIDE = 4
MAX_MODEL_EXCHANGE_FACTS = 8
_SYMBOL_RE = re.compile(r"[A-Z]{1,3}0")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: Any, maximum: int, *, allow_empty: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if (not allow_empty and not value) or len(value) > maximum or any(ord(char) < 32 for char in value):
        return None
    return value


def _number(value: Any, digits: int = 6) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(parsed, digits) if math.isfinite(parsed) else None


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _catalogue_projection(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    schema_version = _integer(value.get("schema_version"))
    content_hash = _text(value.get("content_hash"), 80, allow_empty=True)
    snapshot_status = _text(value.get("snapshot_status"), 32)
    fetched_at = _text(value.get("fetched_at"), 80, allow_empty=True)
    if schema_version is None or content_hash is None or snapshot_status is None or fetched_at is None:
        return None
    return {
        "schema_version": schema_version,
        "content_hash": content_hash,
        "snapshot_status": snapshot_status,
        "fetched_at": fetched_at,
    }


def _normalise_item(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    symbol = _text(raw.get("symbol"), 16)
    name = _text(raw.get("name"), 120)
    exchange = _text(raw.get("exchange"), 32)
    outcome = _text(raw.get("outcome"), 16)
    stage = _text(raw.get("stage"), 16)
    reason = _text(raw.get("reason"), 64, allow_empty=True)
    if (symbol is None or _SYMBOL_RE.fullmatch(symbol) is None or name is None or exchange is None
            or outcome not in PUBLIC_SCAN_OUTCOMES or stage not in PUBLIC_SCAN_STAGES or reason is None):
        return None
    if outcome == "scanned":
        price = _number(raw.get("latest_price"))
        change_pct = _number(raw.get("change_pct"))
        score = _number(raw.get("score"))
        trend = _text(raw.get("trend"), 16)
        if stage != "analysis" or reason or price is None or price <= 0 or change_pct is None or score is None:
            return None
        if trend not in PUBLIC_SCAN_TRENDS:
            return None
        return {
            "symbol": symbol, "name": name, "exchange": exchange, "outcome": outcome, "stage": stage,
            "reason": "", "latest_price": price, "change_pct": change_pct, "trend": trend, "score": score,
        }
    if (not reason or reason not in PUBLIC_SCAN_REASONS
            or reason not in PUBLIC_SCAN_STAGE_REASONS.get(stage, frozenset())
            or any(raw.get(key) is not None for key in ("latest_price", "change_pct", "trend", "score"))):
        return None
    return {
        "symbol": symbol, "name": name, "exchange": exchange, "outcome": outcome, "stage": stage,
        "reason": reason, "latest_price": None, "change_pct": None, "trend": None, "score": None,
    }


def _failed_projection(reason: str, scan_manifest_hash: str = "") -> Dict[str, Any]:
    return {
        "schema_version": PREOPEN_UNIVERSE_SCHEMA_VERSION,
        "scope": "full_universe_5m_technical_breadth",
        "scope_status": "unavailable",
        "reason": reason,
        "scan_manifest_hash": scan_manifest_hash,
        "catalogue": {},
        "counts": {
            "discovered": 0, "eligible": 0, "scanned": 0, "excluded": 0,
            "catalogue_excluded": 0, "runtime_excluded": 0,
        },
        "coverage": {
            "all_candidates_dispositioned": False, "eligible_complete": False,
            "eligible_ratio": None, "disposition_ratio": None,
        },
        "technical_breadth": {
            "market_regime": "unknown", "confidence": "low", "technical_score": 0.0,
            "bullish_contracts": 0, "bearish_contracts": 0, "neutral_contracts": 0,
            "coverage": 0, "technical_window": "5m",
        },
        "exchange_breadth": [],
        "items": [],
        "model_packet": {
            "schema_version": MODEL_PACKET_SCHEMA_VERSION,
            "selection_hash": "", "max_rows_per_side": 0, "facts": [], "selected_count": 0,
        },
    }


def _breadth(scanned: Iterable[Mapping[str, Any]], *, scope_complete: bool) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    rows = [dict(item) for item in scanned]
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    total_score = 0.0
    exchanges: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"contracts": 0, "bullish_contracts": 0, "bearish_contracts": 0,
                 "neutral_contracts": 0, "technical_score": 0.0}
    )
    for item in rows:
        trend = str(item["trend"])
        counts[trend] += 1
        score = float(item["score"])
        total_score += score
        exchange = exchanges[str(item["exchange"])]
        exchange["contracts"] += 1
        exchange[f"{trend}_contracts"] += 1
        exchange["technical_score"] += score
    total_score = round(total_score, 6)
    coverage = len(rows)
    if not coverage:
        regime, confidence = "unknown", "low"
    elif counts["bullish"] and counts["bearish"]:
        regime, confidence = "mixed", "low"
    elif total_score >= max(2.0, coverage / 2.0):
        regime, confidence = "bullish", "medium" if scope_complete and coverage >= 3 else "low"
    elif total_score <= -max(2.0, coverage / 2.0):
        regime, confidence = "bearish", "medium" if scope_complete and coverage >= 3 else "low"
    else:
        regime, confidence = "neutral", "low"
    exchange_rows = [
        {"exchange": exchange, **values, "technical_score": round(values["technical_score"], 6)}
        for exchange, values in sorted(exchanges.items())
    ]
    return {
        "market_regime": regime,
        "confidence": confidence,
        "technical_score": total_score,
        "bullish_contracts": counts["bullish"],
        "bearish_contracts": counts["bearish"],
        "neutral_contracts": counts["neutral"],
        "coverage": coverage,
        "technical_window": "5m",
    }, exchange_rows


def _model_packet(
    scanned: Iterable[Mapping[str, Any]],
    counts: Mapping[str, int],
    coverage: Mapping[str, Any],
    breadth: Mapping[str, Any],
    exchange_breadth: Iterable[Mapping[str, Any]],
    *,
    rows_per_side: int,
) -> Dict[str, Any]:
    size = max(1, int(rows_per_side))
    rows = [dict(item) for item in scanned]
    bullish = sorted((row for row in rows if row.get("trend") == "bullish"),
                     key=lambda row: (-float(row["score"]), str(row["symbol"])))[:size]
    bearish = sorted((row for row in rows if row.get("trend") == "bearish"),
                     key=lambda row: (float(row["score"]), str(row["symbol"])))[:size]
    selected = [("bullish", row) for row in bullish] + [("bearish", row) for row in bearish]
    selection = {
        "schema_version": MODEL_PACKET_SCHEMA_VERSION,
        "policy": "top_score_per_direction_then_symbol",
        "max_rows_per_side": size,
        "selected": [
            {"side": side, "symbol": row["symbol"], "name": row["name"], "exchange": row["exchange"],
             "trend": row["trend"], "score": row["score"], "change_pct": row["change_pct"],
             "latest_price": row["latest_price"]}
            for side, row in selected
        ],
    }
    selection_hash = _canonical_hash(selection)
    facts: List[Dict[str, Any]] = [
        {"id": "universe:coverage", "kind": "universe_coverage", "value": {
            "discovered": counts["discovered"], "eligible": counts["eligible"],
            "scanned": counts["scanned"], "catalogue_excluded": counts["catalogue_excluded"],
            "runtime_excluded": counts["runtime_excluded"], "eligible_complete": coverage["eligible_complete"],
        }},
        {"id": "universe:breadth", "kind": "universe_breadth", "value": dict(breadth)},
    ]
    for row in list(exchange_breadth)[:MAX_MODEL_EXCHANGE_FACTS]:
        exchange = str(row["exchange"])
        facts.append({"id": f"universe:exchange:{exchange}", "kind": "universe_exchange_breadth", "value": dict(row)})
    for side, row in selected:
        facts.append({
            "id": f"universe:{side}:{row['symbol']}", "kind": "universe_representative",
            "symbol": row["symbol"], "value": {
                "name": row["name"], "exchange": row["exchange"], "trend": row["trend"],
                "score": row["score"], "change_pct": row["change_pct"], "latest_price": row["latest_price"],
            },
        })
    return {
        **selection,
        "selection_hash": selection_hash,
        "selected_count": len(selected),
        "not_sent_as_rows": max(0, len(rows) - len(selected)),
        "facts": facts,
    }


def build_preopen_universe_research(scan_manifest: Mapping[str, Any], *, rows_per_side: int = DEFAULT_MODEL_ROWS_PER_SIDE) -> Dict[str, Any]:
    """Validate a fresh scan and derive complete storage plus bounded model facts.

    A scanner ``partial`` result is allowed to be full for the *eligible*
    population when every non-scanned row is a catalogue proof exclusion.  The
    public scanner uses ``partial`` for that correct state because it reports
    all discovered rows in one denominator; the pre-open gate must not confuse
    a known catalogue exclusion with a runtime gap.
    """
    if not isinstance(scan_manifest, Mapping):
        return _failed_projection("scan_manifest_invalid")
    supplied_hash = _text(scan_manifest.get("manifest_hash"), 80, allow_empty=True) or ""
    try:
        if not supplied_hash or supplied_hash != canonical_manifest_hash(scan_manifest):
            return _failed_projection("scan_manifest_hash_invalid", supplied_hash)
    except (TypeError, ValueError):
        return _failed_projection("scan_manifest_hash_invalid", supplied_hash)
    catalogue = _catalogue_projection(scan_manifest.get("catalogue"))
    raw_items = scan_manifest.get("items")
    raw_counts = scan_manifest.get("counts")
    raw_coverage = scan_manifest.get("coverage")
    scan_status = _text(scan_manifest.get("status"), 16)
    rules_version = _text(scan_manifest.get("rules_version"), 64)
    scan_schema_version = _integer(scan_manifest.get("schema_version"))
    if (catalogue is None or not isinstance(raw_items, list) or not isinstance(raw_counts, Mapping)
            or not isinstance(raw_coverage, Mapping) or rules_version != SCAN_RULES_VERSION
            or scan_schema_version != SCAN_SCHEMA_VERSION
            or scan_status not in {"complete", "partial", "incomplete", "unavailable"}):
        return _failed_projection("scan_manifest_invalid", supplied_hash)
    if scan_status == "unavailable":
        return _failed_projection("scan_unavailable", supplied_hash)
    items = [_normalise_item(item) for item in raw_items]
    if any(item is None for item in items):
        return _failed_projection("scan_manifest_invalid", supplied_hash)
    rows = sorted((item for item in items if item is not None), key=lambda item: str(item["symbol"]))
    if len({item["symbol"] for item in rows}) != len(rows):
        return _failed_projection("scan_manifest_duplicate_symbol", supplied_hash)
    expected = {key: _integer(raw_counts.get(key)) for key in ("discovered", "eligible", "scanned", "excluded")}
    if any(value is None for value in expected.values()):
        return _failed_projection("scan_manifest_invalid", supplied_hash)
    catalogue_excluded = [item for item in rows if item["outcome"] == "excluded" and item["stage"] == "catalogue"]
    runtime_excluded = [item for item in rows if item["outcome"] == "excluded" and item["stage"] != "catalogue"]
    scanned = [item for item in rows if item["outcome"] == "scanned"]
    counts = {
        "discovered": len(rows),
        "eligible": len(rows) - len(catalogue_excluded),
        "scanned": len(scanned),
        "excluded": len(catalogue_excluded) + len(runtime_excluded),
        "catalogue_excluded": len(catalogue_excluded),
        "runtime_excluded": len(runtime_excluded),
    }
    if any(expected[key] != counts[key] for key in expected):
        return _failed_projection("scan_manifest_count_mismatch", supplied_hash)
    coverage_status = _text(raw_coverage.get("status"), 16)
    denominator_known = raw_coverage.get("denominator_known")
    coverage_ratio = _number(raw_coverage.get("ratio"))
    expected_ratio = round(counts["scanned"] / counts["discovered"], 6) if counts["discovered"] else None
    if scan_status == "complete":
        valid_coverage = (
            coverage_status == "complete" and denominator_known is True and coverage_ratio == 1.0
            and counts["scanned"] == counts["discovered"] and not counts["excluded"]
        )
    elif scan_status == "partial":
        valid_coverage = (
            coverage_status == "incomplete" and denominator_known is True and coverage_ratio == expected_ratio
        )
    else:
        valid_coverage = coverage_status == "incomplete" and isinstance(denominator_known, bool)
    if not valid_coverage:
        return _failed_projection("scan_manifest_coverage_invalid", supplied_hash)
    dispositioned = bool(rows) and counts["scanned"] + counts["excluded"] == counts["discovered"]
    eligible_complete = bool(
        scan_status in {"complete", "partial"}
        and catalogue.get("snapshot_status") == "complete" and counts["eligible"] > 0
        and counts["scanned"] == counts["eligible"] and not runtime_excluded
    )
    scope_complete = dispositioned and eligible_complete
    coverage = {
        "all_candidates_dispositioned": dispositioned,
        "eligible_complete": eligible_complete,
        "eligible_ratio": round(counts["scanned"] / counts["eligible"], 6) if counts["eligible"] else None,
        "disposition_ratio": round((counts["scanned"] + counts["excluded"]) / counts["discovered"], 6)
        if counts["discovered"] else None,
    }
    breadth, exchange_breadth = _breadth(scanned, scope_complete=scope_complete)
    packet = _model_packet(scanned, counts, coverage, breadth, exchange_breadth, rows_per_side=rows_per_side)
    stored_items = []
    for item in rows:
        state = "scanned" if item["outcome"] == "scanned" else (
            "catalogue_excluded" if item["stage"] == "catalogue" else "runtime_excluded"
        )
        stored_items.append({**item, "state": state})
    return {
        "schema_version": PREOPEN_UNIVERSE_SCHEMA_VERSION,
        "scope": "full_universe_5m_technical_breadth",
        "scope_status": "complete" if scope_complete else "incomplete",
        "reason": "" if scope_complete else "eligible_scan_incomplete",
        "scan_manifest_hash": supplied_hash,
        "scan_status": scan_status,
        "scan_started_at": _number(scan_manifest.get("started_at")),
        "scan_completed_at": _number(scan_manifest.get("completed_at")),
        "catalogue": catalogue,
        "counts": counts,
        "coverage": coverage,
        "technical_breadth": breadth,
        "exchange_breadth": exchange_breadth,
        "items": stored_items,
        "model_packet": packet,
    }


def full_universe_global_analysis(universe_research: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the public top-level analysis from full-universe technical breadth."""
    breadth = universe_research.get("technical_breadth") if isinstance(universe_research, Mapping) else {}
    counts = universe_research.get("counts") if isinstance(universe_research, Mapping) else {}
    coverage = universe_research.get("coverage") if isinstance(universe_research, Mapping) else {}
    if not isinstance(breadth, Mapping) or not isinstance(counts, Mapping) or not isinstance(coverage, Mapping):
        return {"scope": "full_universe_5m_technical_breadth", "scope_status": "unavailable",
                "market_regime": "unknown", "confidence": "low",
                "breadth": {}, "technical_score": 0.0, "coverage": 0, "candidate_denominator": 0,
                "invalidation_conditions": ["Full-universe research is unavailable."], "not_trade_instruction": True}
    scope_complete = universe_research.get("scope_status") == "complete"
    return {
        "scope": "full_universe_5m_technical_breadth",
        "scope_status": universe_research.get("scope_status") or "unavailable",
        "market_regime": (breadth.get("market_regime") or "unknown") if scope_complete else "unknown",
        "confidence": (breadth.get("confidence") or "low") if scope_complete else "low",
        "breadth": {
            "bullish_contracts": breadth.get("bullish_contracts", 0),
            "bearish_contracts": breadth.get("bearish_contracts", 0),
            "neutral_contracts": breadth.get("neutral_contracts", 0),
            "missing_contracts": counts.get("runtime_excluded", 0),
        },
        "technical_score": breadth.get("technical_score", 0.0) if scope_complete else None,
        "coverage": breadth.get("coverage", 0),
        "candidate_denominator": counts.get("discovered", 0),
        "eligible_candidates": counts.get("eligible", 0),
        "scanned_candidates": counts.get("scanned", 0),
        "catalogue_excluded": counts.get("catalogue_excluded", 0),
        "runtime_excluded": counts.get("runtime_excluded", 0),
        "eligible_complete": bool(coverage.get("eligible_complete")),
        "universe_manifest_hash": universe_research.get("scan_manifest_hash") or "",
        "selection_hash": ((universe_research.get("model_packet") or {}).get("selection_hash")
                           if isinstance(universe_research.get("model_packet"), Mapping) else ""),
        "technical_window": breadth.get("technical_window") or "5m",
        "invalidation_conditions": [
            "The conclusion is invalid if the frozen full-universe eligible coverage is incomplete.",
            "The research packet is not an execution instruction and never expands the configured execution pool.",
        ] if scope_complete else [
            "Full-universe eligible coverage is incomplete; no all-universe model conclusion may be produced.",
        ],
        "not_trade_instruction": True,
    }


def prompt_projection(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the bounded model-facing projection of an immutable debate snapshot.

    ``snapshot_hash`` remains the hash of the full stored snapshot, including all
    candidate rows.  The prompt deliberately excludes those rows and contains
    only the deterministic aggregate plus fixed-size representative facts.
    """
    result = {
        key: manifest.get(key)
        for key in ("schema_version", "market", "currency", "round_no", "market_as_of", "source_health",
                    "facts", "symbol_scores", "execution_pool_analysis", "global_analysis",
                    "snapshot_hash", "analysis_id")
    }
    universe = manifest.get("universe_research")
    if isinstance(universe, Mapping):
        packet = universe.get("model_packet") if isinstance(universe.get("model_packet"), Mapping) else {}
        result["universe_research"] = {
            "scope": universe.get("scope"), "scope_status": universe.get("scope_status"),
            "scan_manifest_hash": universe.get("scan_manifest_hash"), "catalogue": universe.get("catalogue"),
            "counts": universe.get("counts"), "coverage": universe.get("coverage"),
            "technical_breadth": universe.get("technical_breadth"),
            "exchange_breadth": universe.get("exchange_breadth", [])[:MAX_MODEL_EXCHANGE_FACTS],
            "model_packet": {
                "schema_version": packet.get("schema_version"), "selection_hash": packet.get("selection_hash"),
                "policy": packet.get("policy"), "max_rows_per_side": packet.get("max_rows_per_side"),
                "selected_count": packet.get("selected_count"), "not_sent_as_rows": packet.get("not_sent_as_rows"),
            },
            "model_limit": "The aggregate includes every scanned candidate. Only bounded representative rows are listed as facts; do not claim to have read unlisted rows.",
        }
    return result
