"""Regression 56: provider errors stay local while the dashboard gets fixed categories."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import Decision
from arena.server.api import create_app


ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "TEST_PROVIDER_ERROR_SENTINEL"


def make_cfg(tmp_path):
    return AppConfig(
        competition={"name": "privacy", "market": "replay", "initial_cash": 80_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0, "breaker_failures": 1, "breaker_skip_rounds": 2},
        traders=[TraderCfg(id="model", name="Model", provider="mock")],
        storage={"db_path": str(tmp_path / "review56.db")},
        base_dir=ROOT,
    )


@pytest.mark.parametrize(
    ("error", "failure_category"),
    [
        (f"APITimeoutError: {SENTINEL}", "timeout"),
        (f"RateLimitError: HTTP 429 / provider code 1302 {SENTINEL}", "rate_limited"),
        (f"RateLimitError: Error code: 429 {SENTINEL}", "rate_limited"),
        (f"ProviderError: {SENTINEL}", "unknown"),
    ],
)
def test_public_decision_apis_redact_provider_error_text(tmp_path, error, failure_category):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        engine._tick()
        snapshot = engine._build_snapshot()
        assert snapshot is not None
        engine._finish_safely(
            "model",
            Decision(trader_id="model", round_no=snapshot.round_no, ts=1.0, mode="llm", error=error),
            snapshot,
            engine._generation,
        )
        raw = db.decisions(engine.comp_id, "model", limit=1)[0]
        assert SENTINEL in raw["error"], "local diagnostics retain the original provider error"
        db.add_event(engine.comp_id, "warn", "model", f"连续失败触发熔断，跳过 2 轮：{error}")

        client = TestClient(create_app(engine, ROOT / "web"))
        state = client.get("/api/state")
        decisions = client.get("/api/decisions?trader=model")
        detail = client.get(f"/api/decisions/{raw['id']}")
        events = client.get("/api/events")
        assert all(response.status_code == 200 for response in (state, decisions, detail, events))
        assert all(SENTINEL not in response.text for response in (state, decisions, detail, events))

        item = decisions.json()["items"][0]
        assert "error" not in item and item["failure_category"] == failure_category
        assert "error" not in detail.json() and detail.json()["failure_category"] == failure_category
        breaker = next(item for item in state.json()["traders"] if item["id"] == "model")["breaker"]
        assert "last_error" not in breaker and breaker["failure_category"] == failure_category
        assert all(SENTINEL not in item["message"] for item in events.json()["items"])
    finally:
        engine.stop()
        db.close()


def test_dashboard_only_uses_fixed_failure_categories():
    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "decisionFailureMessage(x.failure_category)" in app_js
    assert "decisionFailureMessage(br.failure_category)" in app_js
    assert "br.last_error" not in app_js
    assert "esc(x.error)" not in app_js
    assert "模型本轮失败，未形成有效决策" in app_js
    assert "无动作（观望）" in app_js
    assert "tokens ${x.tokens_in ?? '-'}/${x.tokens_out ?? '-'}" in app_js
