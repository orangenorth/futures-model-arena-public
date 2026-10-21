"""Regression 57: browser APIs expose fixed runtime-failure messages only."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import Decision
from arena.public_errors import (
    PUBLIC_ACTION_EXECUTION_FAILURE,
    PUBLIC_RESET_PERSISTENCE_FAILURE,
    PUBLIC_RUNTIME_EVENT_MESSAGES,
    PUBLIC_SOURCE_UNAVAILABLE_MESSAGE,
)
from arena.server.api import create_app

ROOT = Path(__file__).resolve().parents[1]
# 写接口按 Host 判定拒绝非看板地址，TestClient 默认的 http://testserver 不是：用真实的回环看板地址
LOOPBACK_BASE = "http://127.0.0.1:8765"
SENTINEL = "TEST_BROWSER_ERROR_SENTINEL"


def make_cfg(tmp_path):
    return AppConfig(
        competition={"name": "browser-error-privacy", "market": "replay", "initial_cash": 80_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0, "breaker_failures": 1, "breaker_skip_rounds": 2},
        traders=[TraderCfg(id="model", name="Model", provider="mock")],
        storage={"db_path": str(tmp_path / "review57.db")},
        base_dir=ROOT,
    )


def make_engine(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


@pytest.mark.parametrize(
    ("legacy_message", "projection"),
    [
        (f"回放位置未能持久化，已暂停前进直到写入成功: {SENTINEL}", PUBLIC_RUNTIME_EVENT_MESSAGES["replay_position"]),
        (f"盯市成交落库失败，本次触发已回滚，下个 tick 按原触发价重试: {SENTINEL}", PUBLIC_RUNTIME_EVENT_MESSAGES["mark_persistence"]),
        (f"停止时最终持久化失败: {SENTINEL}", PUBLIC_RUNTIME_EVENT_MESSAGES["shutdown_persistence"]),
        (f"主循环异常: {SENTINEL}", PUBLIC_RUNTIME_EVENT_MESSAGES["main_loop"]),
        (f"决策轮异常: {SENTINEL}", PUBLIC_RUNTIME_EVENT_MESSAGES["round"]),
        (f"第 17 轮结算异常: {SENTINEL}", f"第 17 轮{PUBLIC_RUNTIME_EVENT_MESSAGES['settlement']}"),
    ],
)
def test_public_events_redact_historical_runtime_exception_text(tmp_path, legacy_message, projection):
    engine, db = make_engine(tmp_path)
    try:
        db.add_event(engine.comp_id, "error", "engine", legacy_message)
        db.add_event(engine.comp_id, "info", "user", "普通审计事件")

        response = TestClient(create_app(engine, ROOT / "web")).get("/api/events")

        assert response.status_code == 200
        assert SENTINEL not in response.text
        messages = [item["message"] for item in response.json()["items"]]
        assert projection in messages
        assert "普通审计事件" in messages
    finally:
        engine.stop()
        db.close()


def test_public_decision_results_redact_only_runtime_exception_reason(tmp_path):
    engine, db = make_engine(tmp_path)
    try:
        decision_id = db.add_decision(
            engine.comp_id,
            Decision(
                trader_id="model",
                round_no=1,
                ts=1.0,
                mode="mock",
                results=[
                    {"symbol": "RB0", "action": "open_long", "accepted": False,
                     "reason": f"执行异常: RuntimeError: {SENTINEL}"},
                    {"symbol": "I0", "action": "open_short", "accepted": False,
                     "reason": "风险度超过上限"},
                ],
            ),
        )
        client = TestClient(create_app(engine, ROOT / "web"), base_url=LOOPBACK_BASE)
        listing = client.get("/api/decisions?trader=model")
        detail = client.get(f"/api/decisions/{decision_id}")

        assert listing.status_code == detail.status_code == 200
        assert SENTINEL not in listing.text and SENTINEL not in detail.text
        for payload in (listing.json()["items"][0], detail.json()):
            assert payload["results"][0]["reason"] == PUBLIC_ACTION_EXECUTION_FAILURE
            assert payload["results"][1]["reason"] == "风险度超过上限"
    finally:
        engine.stop()
        db.close()


@pytest.mark.parametrize("ok", [False, True])
def test_public_state_never_exposes_market_source_detail(tmp_path, monkeypatch, ok):
    engine, db = make_engine(tmp_path)
    try:
        monkeypatch.setattr(
            engine.source,
            "health",
            lambda: {"source": "test", "ok": ok, "errors": 1, "detail": SENTINEL},
        )

        response = TestClient(create_app(engine, ROOT / "web")).get("/api/state")

        assert response.status_code == 200
        assert SENTINEL not in response.text
        health = response.json()["status"]["source"]
        if ok:
            assert "detail" not in health
        else:
            assert health["detail"] == PUBLIC_SOURCE_UNAVAILABLE_MESSAGE
    finally:
        engine.stop()
        db.close()


def test_reset_response_redacts_database_exception_text(tmp_path, monkeypatch):
    engine, db = make_engine(tmp_path)
    try:
        def broken_create_competition(*_args, **_kwargs):
            raise RuntimeError(SENTINEL)

        monkeypatch.setattr(db, "create_competition", broken_create_competition)
        response = TestClient(create_app(engine, ROOT / "web"),
                              base_url=LOOPBACK_BASE).post("/api/control", json={"action": "reset"})

        assert response.status_code == 409
        assert SENTINEL not in response.text
        assert response.json()["detail"] == PUBLIC_RESET_PERSISTENCE_FAILURE
    finally:
        engine.stop()
        db.close()
