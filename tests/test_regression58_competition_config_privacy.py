"""Regression 58: /api/competitions never ships the stored competition config."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database, competition_timezone
from arena.engine.loop import Engine
from arena.server.api import create_app

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "TEST_COMPETITION_CONFIG_SENTINEL"


def make_engine(tmp_path):
    cfg = AppConfig(
        competition={"name": "config-privacy", "market": "replay", "initial_cash": 80_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="model", name="Model", provider="mock")],
        storage={"db_path": str(tmp_path / "review58.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


def test_public_competitions_drop_stored_config(tmp_path):
    engine, db = make_engine(tmp_path)
    try:
        # A historical row may carry anything the config held when it was created.
        stored = json.dumps({"competition": {"timezone": "UTC"}, "note": SENTINEL})
        db._exec("UPDATE competitions SET config_json=? WHERE id=?", (stored, engine.comp_id))

        response = TestClient(create_app(engine, ROOT / "web")).get("/api/competitions")

        assert response.status_code == 200
        assert SENTINEL not in response.text
        row = next(item for item in response.json()["items"] if item["id"] == engine.comp_id)
        assert "config_json" not in row
        assert row["name"] == "config-privacy"
        assert row["market"] == "replay"
        assert row["status"] == "running"
        assert row["initial_cash"] == 80_000
        assert row["ended_at"] is None
        assert {"started_at", "round_no"} <= set(row)

        # Internal readers keep the raw configuration they depend on.
        internal = next(item for item in db.list_competitions() if item["id"] == engine.comp_id)
        assert internal["config_json"] == stored
        assert competition_timezone(internal) == "UTC"
    finally:
        engine.stop()
        db.close()
