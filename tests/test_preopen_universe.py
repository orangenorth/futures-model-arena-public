"""Offline acceptance tests for the full-universe pre-open research boundary."""
from __future__ import annotations

import copy
import json

import pytest

from arena.debate import build_market_manifest, opening_prompt
from arena.models import ContractSpec, Quote
from arena.preopen_universe import build_preopen_universe_research, prompt_projection
from arena.research_scan import SCAN_RULES_VERSION, SCAN_SCHEMA_VERSION, canonical_manifest_hash
from arena.traders.base import MarketSnapshot, SymbolView


def _symbol(index: int) -> str:
    return chr(ord("A") + (index // 26)) + chr(ord("A") + (index % 26)) + "0"


def _scanned(symbol: str, *, trend: str = "neutral", score: float = 0.0, name: str | None = None) -> dict:
    return {
        "symbol": symbol,
        "name": name or f"{symbol} name",
        "exchange": "SHFE" if symbol[0] < "N" else "DCE",
        "outcome": "scanned",
        "stage": "analysis",
        "reason": "",
        "latest_price": 100.0 + score,
        "change_pct": score / 10.0,
        "trend": trend,
        "score": score,
    }


def _excluded(symbol: str, *, stage: str, reason: str) -> dict:
    return {
        "symbol": symbol,
        "name": f"{symbol} name",
        "exchange": "SHFE",
        "outcome": "excluded",
        "stage": stage,
        "reason": reason,
        "latest_price": None,
        "change_pct": None,
        "trend": None,
        "score": None,
    }


def _manifest(items: list[dict], *, status: str = "partial", rules_version: str = SCAN_RULES_VERSION,
              schema_version: int = SCAN_SCHEMA_VERSION, coverage: dict | None = None) -> dict:
    catalogue_excluded = sum(1 for item in items if item["outcome"] == "excluded" and item["stage"] == "catalogue")
    scanned = sum(1 for item in items if item["outcome"] == "scanned")
    excluded = sum(1 for item in items if item["outcome"] == "excluded")
    counts = {
        "discovered": len(items),
        "eligible": len(items) - catalogue_excluded,
        "scanned": scanned,
        "excluded": excluded,
    }
    if coverage is None:
        if status == "complete":
            coverage = {"status": "complete", "ratio": 1.0, "denominator_known": True}
        elif status == "unavailable":
            coverage = {"status": "unavailable", "ratio": None, "denominator_known": False}
        else:
            coverage = {
                "status": "incomplete",
                "ratio": round(scanned / len(items), 6) if items and status == "partial" else None,
                "denominator_known": True,
            }
    manifest = {
        "schema_version": schema_version,
        "rules_version": rules_version,
        "status": status,
        "catalogue": {
            "schema_version": 1,
            "content_hash": "sha256:catalogue-fixture",
            "snapshot_status": "complete",
            "fetched_at": "2026-09-22T09:00:00+08:00",
        },
        "counts": counts,
        "coverage": coverage,
        "started_at": 1_700_000_000.0,
        "completed_at": 1_700_000_001.0,
        "items": items,
    }
    manifest["manifest_hash"] = canonical_manifest_hash(manifest)
    return manifest


def _rehash(manifest: dict) -> dict:
    manifest["manifest_hash"] = canonical_manifest_hash(manifest)
    return manifest


def _execution_snapshot() -> MarketSnapshot:
    spec = ContractSpec(symbol="RB0", name="Rebar", exchange="SHFE")
    view = SymbolView(
        symbol="RB0",
        spec=spec,
        quote=Quote(symbol="RB0", price=3400.0, ts=1_700_000_000.0, change_pct=0.2),
        ind_5m={"ema5": 3401.0, "ema20": 3399.0, "rsi14": 56.0, "atr14": 10.0, "chg5": 0.1,
                "chg20": 0.2, "high20": 3410.0, "low20": 3370.0, "macd_hist": 0.2},
        ind_15m={"ema5": 3400.0, "ema20": 3390.0, "rsi14": 55.0, "chg5": 0.2, "chg20": 0.4,
                 "macd_hist": 0.3},
        ind_1h={"ema20": 3380.0, "ema60": 3360.0, "rsi14": 54.0, "chg5": 0.2, "chg20": 0.5,
                "macd_hist": 0.4},
    )
    return MarketSnapshot(ts=1_700_000_000.0, round_no=1, market="cn_futures", currency="CNY", symbols={"RB0": view})


def test_catalogue_exclusions_can_complete_the_eligible_scope():
    items = [
        _scanned(_symbol(index), trend=("bullish" if index % 3 == 0 else "bearish" if index % 3 == 1 else "neutral"),
                 score=float((index % 7) - 3))
        for index in range(79)
    ]
    items.extend(
        _excluded(_symbol(index), stage="catalogue", reason="catalogue_quote_proof_missing")
        for index in range(79, 83)
    )

    research = build_preopen_universe_research(_manifest(items))

    assert research["scope_status"] == "complete"
    assert research["counts"] == {
        "discovered": 83,
        "eligible": 79,
        "scanned": 79,
        "excluded": 4,
        "catalogue_excluded": 4,
        "runtime_excluded": 0,
    }
    assert research["coverage"] == {
        "all_candidates_dispositioned": True,
        "eligible_complete": True,
        "eligible_ratio": 1.0,
        "disposition_ratio": 1.0,
    }
    assert len(research["items"]) == 83
    assert {item["state"] for item in research["items"]} == {"scanned", "catalogue_excluded"}


def test_runtime_exclusion_never_claims_full_scope():
    items = [
        _scanned("AA0", trend="bullish", score=3.0),
        _scanned("AB0", trend="bearish", score=-3.0),
        _excluded("AC0", stage="history", reason="history_timeout"),
    ]

    research = build_preopen_universe_research(_manifest(items, status="incomplete"))

    assert research["scope_status"] == "incomplete"
    assert research["coverage"]["eligible_complete"] is False
    assert research["counts"]["runtime_excluded"] == 1
    assert {item["state"] for item in research["items"]} == {"scanned", "runtime_excluded"}
    manifest = build_market_manifest(_execution_snapshot(), {"source": "fixture", "ok": True}, universe_research=research)
    assert manifest["global_analysis"]["scope_status"] == "incomplete"
    assert manifest["global_analysis"]["market_regime"] == "unknown"
    assert manifest["global_analysis"]["technical_score"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update(status="unavailable", coverage={"status": "unavailable", "ratio": None, "denominator_known": False}),
        lambda manifest: manifest.update(rules_version="research-scan-v999"),
        lambda manifest: manifest.update(schema_version=999),
        lambda manifest: manifest.update(coverage={"status": "complete", "ratio": 1.0, "denominator_known": True}),
        lambda manifest: manifest["items"].__setitem__(1, _excluded("AB0", stage="catalogue", reason="not_a_public_reason")),
        lambda manifest: manifest["items"].__setitem__(1, _excluded("AB0", stage="catalogue", reason="history_timeout")),
    ],
)
def test_invalid_or_unavailable_scanner_manifests_fail_closed(mutate):
    manifest = _manifest([
        _scanned("AA0", trend="bullish", score=2.0),
        _excluded("AB0", stage="catalogue", reason="catalogue_quote_proof_missing"),
    ])
    mutate(manifest)
    _rehash(manifest)

    research = build_preopen_universe_research(manifest)

    assert research["scope_status"] == "unavailable"
    assert research["coverage"]["eligible_complete"] is False
    assert research["items"] == []


def test_packet_is_bounded_while_snapshot_keeps_all_rows_and_hashes_them():
    items = [
        *[_scanned(_symbol(index), trend="bullish", score=float(index + 1)) for index in range(7)],
        *[_scanned(_symbol(index + 7), trend="bearish", score=-float(index + 1)) for index in range(7)],
        _scanned("ZZ0", trend="neutral", score=0.0, name="UNSELECTED-MARKER"),
    ]
    raw = _manifest(items, status="complete")
    research = build_preopen_universe_research(raw, rows_per_side=2)
    manifest = build_market_manifest(_execution_snapshot(), {"source": "fixture", "ok": True}, universe_research=research)
    projected = prompt_projection(manifest)
    prompt = opening_prompt(manifest, "morning_preopen", "早盘开盘前", None)

    assert research["scope_status"] == "complete"
    assert len(research["items"]) == 15
    assert research["model_packet"]["selected_count"] == 4
    assert len(research["model_packet"]["facts"]) <= 2 + 8 + 4
    assert projected["universe_research"].get("items") is None
    encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True)
    assert "UNSELECTED-MARKER" not in encoded and "UNSELECTED-MARKER" not in prompt
    assert any(fact["id"] == "universe:coverage" for fact in manifest["facts"])
    assert manifest["global_analysis"]["scope"] == "full_universe_5m_technical_breadth"
    assert manifest["global_analysis"]["execution_pool_coverage"] == 1

    changed = copy.deepcopy(raw)
    changed["items"][-1]["score"] = 0.25
    changed["items"][-1]["change_pct"] = 0.025
    _rehash(changed)
    changed_research = build_preopen_universe_research(changed, rows_per_side=2)
    changed_manifest = build_market_manifest(
        _execution_snapshot(), {"source": "fixture", "ok": True}, universe_research=changed_research,
    )
    assert changed_research["scan_manifest_hash"] != research["scan_manifest_hash"]
    assert changed_manifest["snapshot_hash"] != manifest["snapshot_hash"]
