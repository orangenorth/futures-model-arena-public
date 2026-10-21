"""API boundary tests for the independent research-scan projection."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import Bar, Quote
from arena.research_scan import canonical_manifest_hash
from arena.server.api import create_app
from arena.universe import content_hash


ROOT = Path(__file__).resolve().parents[1]
LOCAL = "http://127.0.0.1:8765"


def make_cfg(tmp_path):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1,
                     "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=json.dumps({"analysis": "x", "actions": []}))],
        storage={"db_path": str(tmp_path / "research-api.db")},
        base_dir=ROOT,
    )


def sample_manifest(now: float = 1_700_000_000.0) -> dict:
    manifest = {
        "schema_version": 1,
        "rules_version": "research-scan-v1",
        "status": "partial",
        "catalogue": {"schema_version": 1, "content_hash": "sha256:" + "a" * 64,
                      "snapshot_status": "complete", "fetched_at": "2026-09-19T00:00:00+08:00"},
        "counts": {"discovered": 2, "eligible": 2, "scanned": 1, "excluded": 1},
        "coverage": {"status": "incomplete", "ratio": 0.5, "denominator_known": True},
        "started_at": now - 1,
        "completed_at": now,
        "items": [
            {"symbol": "AA0", "name": "AA", "exchange": "SHFE", "outcome": "scanned", "stage": "analysis",
             "reason": "", "latest_price": 100.0, "change_pct": 0.2, "trend": "bullish", "score": 1.1},
            {"symbol": "AB0", "name": "AB", "exchange": "SHFE", "outcome": "excluded", "stage": "quote",
             "reason": "quote_missing", "latest_price": None, "change_pct": None, "trend": None, "score": None},
        ],
    }
    manifest["manifest_hash"] = canonical_manifest_hash(manifest)
    return manifest


def test_research_scan_read_is_safe_and_stays_out_of_state_poll(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        db.append_research_scan(sample_manifest())
        app = create_app(engine, ROOT / "web")
        app.state.research_scan._reader_factory = lambda: (_ for _ in ()).throw(AssertionError("GET must not build a reader"))
        client = TestClient(app, base_url=LOCAL)

        response = client.get("/api/research-scan")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["running"] is False
        assert body["latest"]["counts"] == {"discovered": 2, "eligible": 2, "scanned": 1, "excluded": 1}
        assert body["latest"]["items"][1]["reason"] == "quote_missing"
        assert "source_url" not in response.text and "SECRET" not in response.text
        state = client.get("/api/state").json()
        assert "research_scan" not in state and "research_scan_items" not in state
    finally:
        engine.stop()
        db.close()


def test_research_scan_post_is_guarded_and_same_origin_uses_only_injected_public_reader(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        execution_calls = []
        before_symbols = list(engine.symbols)
        before_specs = dict(engine.specs)
        engine._tick = lambda: execution_calls.append("tick")
        engine._build_snapshot = lambda *args, **kwargs: execution_calls.append("snapshot")
        app = create_app(engine, ROOT / "web")
        client = TestClient(app, base_url=LOCAL)
        service = app.state.research_scan
        blocked = client.post("/api/research-scan/run", headers={"Sec-Fetch-Site": "cross-site"})
        assert blocked.status_code == 403
        assert service.public_status()["running"] is False

        now = datetime(2026, 9, 22, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        candidates = {
            "AA0": {
                "continuous_symbol": "AA0", "name": "AA", "exchange": "SHFE", "source_group": "fixture", "source_node": "AA0",
                "quote_ok": True, "history_5m_ok": True, "history_15m_ok": True, "history_1h_ok": True,
                "probe_status": "ok", "probe_error": "", "probed_at": "2026-09-19T00:00:00+08:00",
            }
        }
        catalogue_path = tmp_path / "catalogue.json"
        catalogue_path.write_text(json.dumps({
            "schema_version": 1, "source": "fixture", "source_url": "https://example.invalid", "fetched_at": "2026-09-19T00:00:00+08:00",
            "snapshot_status": "complete", "count": 1, "candidates": candidates, "content_hash": content_hash(candidates),
        }), encoding="utf-8")

        class Reader:
            def latest_batch(self, symbols):
                return {"AA0": Quote(symbol="AA0", price=100, ts=now - 10, change_pct=0.1)}

            def history_5m(self, symbol):
                return [Bar(ts=now - (69 - index) * 300, open=100 + index, high=101 + index,
                            low=99 + index, close=100 + index, volume=1, hold=1) for index in range(70)]

        service._catalogue_path = catalogue_path
        service._reader_factory = Reader
        service._clock = lambda: now
        response = client.post("/api/research-scan/run", headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 200 and response.json()["analysis_only"] is True
        assert service._thread is not None
        service._thread.join(timeout=3)
        latest = client.get("/api/research-scan").json()["latest"]
        assert latest["counts"] == {"discovered": 1, "eligible": 1, "scanned": 1, "excluded": 0}
        assert latest["status"] == "complete"
        assert not execution_calls
        assert engine.symbols == before_symbols
        assert dict(engine.specs) == before_specs
    finally:
        engine.stop()
        db.close()
