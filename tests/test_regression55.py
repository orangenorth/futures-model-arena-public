"""Regression 55: sanitized research failure categories and transcript privacy."""
from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from arena.config import AppConfig, TraderCfg
from arena.db import Database, PUBLIC_DEBATE_FAILURE_CATEGORY_BY_ERROR
from arena.debate import referee_packet
from arena.engine.loop import Engine
from arena.server.api import create_app
from arena.traders.llm import LlmTrader


ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "TEST_SECRET_SENTINEL"


def make_cfg(tmp_path):
    traders = [
        TraderCfg(id=trader_id, name=name, provider="openai_compatible", base_url="https://example.invalid/v1",
                  model=f"{trader_id}-test", api_key="test-key")
        for trader_id, name in (("deepseek", "DeepSeek"), ("qwen", "通义千问"), ("kimi", "Kimi K3"), ("glm", "智谱 GLM"))
    ]
    return AppConfig(
        competition={"name": "research", "market": "replay", "initial_cash": 80_000, "decision_interval_sec": 1,
                     "tick_interval_sec": 1, "snapshot_interval_sec": 1, "timezone": "Asia/Shanghai"},
        symbols=["RB0", "I0"],
        # history 要够深：1h 指标（ema60/rsi14/macd_hist）需要几十根小时线，否则这份快照凑不齐完整证据，
        # 研究会按"证据不全"失败关闭，根本不会创建会话
        market_data={"replay": {"seed": 3, "history": 4000, "start_price": {"RB0": 3400, "I0": 800}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"max_margin_risk_ratio": 0.95, "warning_margin_risk_ratio": 0.90, "min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0, "breaker_failures": 2, "breaker_skip_rounds": 2},
        preopen_debate={"enabled": False, "stage_timeout_sec": 5, "max_output_tokens": 128},
        traders=traders,
        storage={"db_path": str(tmp_path / "review55.db")},
        base_dir=ROOT,
    )


def _request():
    return httpx.Request("POST", f"https://provider.invalid/v1?token={SENTINEL}")


def _status_error(status_code: int):
    return APIStatusError(
        f"Authorization: Bearer {SENTINEL}",
        response=httpx.Response(status_code, request=_request()),
        body={"error": {"message": SENTINEL}},
    )


class _CreditsError(RuntimeError):
    def __init__(self):
        super().__init__(f"request failed with {SENTINEL}")
        self.body = {"error": {"message": f"no credits remaining: {SENTINEL}"}}


@pytest.mark.parametrize(
    ("exc_factory", "error_code", "failure_category"),
    [
        pytest.param(lambda: TimeoutError(SENTINEL), "ResearchRequestTimedOut", "timeout", id="builtin-timeout"),
        pytest.param(lambda: APITimeoutError(request=_request()), "ResearchRequestTimedOut", "timeout", id="sdk-timeout-before-connection"),
        pytest.param(lambda: APIConnectionError(message=SENTINEL, request=_request()), "ResearchConnectionFailed", "connection", id="sdk-connection"),
        pytest.param(lambda: _status_error(401), "ResearchAuthorizationFailed", "authorization", id="http-401"),
        pytest.param(lambda: _status_error(403), "ResearchAuthorizationFailed", "authorization", id="http-403"),
        pytest.param(lambda: RateLimitError(SENTINEL, response=httpx.Response(429, request=_request()), body={"error": SENTINEL}), "ResearchRateLimited", "rate_limited", id="http-429"),
        pytest.param(lambda: _status_error(400), "ResearchUpstream4xx", "upstream_4xx", id="http-4xx"),
        pytest.param(lambda: _status_error(502), "ResearchUpstream5xx", "upstream_5xx", id="http-5xx"),
        pytest.param(_CreditsError, "ResearchQuotaExhausted", "quota_or_credits", id="explicit-credit-exhaustion"),
        pytest.param(lambda: ValueError(SENTINEL), "ResearchRequestFailed", "request_failed", id="client-value-error-is-not-parse-error"),
        pytest.param(lambda: RuntimeError(SENTINEL), "ResearchRequestFailed", "request_failed", id="unknown"),
    ],
)
def test_debate_request_failures_are_classified_without_retaining_provider_details(tmp_path, exc_factory, error_code, failure_category):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        trader = engine.traders["glm"]
        assert isinstance(trader, LlmTrader)
        calls = []

        def fail_research(*args, **kwargs):
            calls.append((args, kwargs))
            raise exc_factory()

        trader.research = fail_research  # type: ignore[method-assign]
        result = engine._call_debate_model("glm", "fixture prompt", "opening", {"fact-1"}, {"glm", "qwen"})

        assert len(calls) == 1, "research failures must not trigger a billable retry"
        assert result["status"] == "failed"
        assert result["error"] == error_code
        assert PUBLIC_DEBATE_FAILURE_CATEGORY_BY_ERROR[result["error"]] == failure_category
        assert SENTINEL not in repr(result)
    finally:
        engine.stop()
        db.close()


@pytest.mark.parametrize(
    ("stage", "text", "expected_error", "expected_category"),
    [
        pytest.param("opening", "not valid JSON", "ResearchResponseMalformedJson", "malformed_json", id="malformed-json"),
        pytest.param(
            "opening",
            json.dumps({"stance": "bullish", "confidence": 0.5, "thesis": "t", "counter_argument": "c",
                        "invalidation": "i", "supporting_fact_ids": ["outside"], "counter_fact_ids": ["fact-1"]}),
            "ResearchResponseInvalidFactCitation", "invalid_fact_citation", id="unknown-fact-id",
        ),
        pytest.param(
            "rebuttal",
            json.dumps({"stance": "neutral", "confidence": 0.5, "target_trader_id": "glm", "challenge": "c",
                        "challenge_fact_ids": ["fact-1"], "revised_thesis": "t", "invalidation": "i"}),
            "ResearchResponseInvalidTarget", "invalid_target", id="self-target",
        ),
        pytest.param(
            "opening",
            json.dumps({"stance": "bullish", "confidence": 0.5, "counter_argument": "c", "invalidation": "i",
                        "supporting_fact_ids": ["fact-1"], "counter_fact_ids": ["fact-1"]}),
            "ResearchResponseMissingRequiredField", "missing_required_field", id="missing-required-field",
        ),
        pytest.param(
            "opening",
            json.dumps({"stance": "sideways", "confidence": 0.5, "thesis": "t", "counter_argument": "c",
                        "invalidation": "i", "supporting_fact_ids": ["fact-1"], "counter_fact_ids": ["fact-1"]}),
            "ResearchResponseInvalidSchema", "invalid_schema", id="invalid-schema",
        ),
    ],
)
def test_parser_value_errors_have_safe_diagnostic_subtypes(tmp_path, stage, text, expected_error, expected_category):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        trader = engine.traders["glm"]
        assert isinstance(trader, LlmTrader)
        trader.research = lambda *args, **kwargs: SimpleNamespace(text=text)  # type: ignore[method-assign]

        result = engine._call_debate_model("glm", "fixture prompt", stage, {"fact-1"}, {"glm", "qwen"})

        assert result["status"] == "failed"
        assert result["error"] == expected_error
        assert PUBLIC_DEBATE_FAILURE_CATEGORY_BY_ERROR[result["error"]] == expected_category
        assert "raw_response" not in result
    finally:
        engine.stop()
        db.close()


def test_stage_deadline_marks_pending_workers_with_a_fixed_category(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.preopen_debate.stage_timeout_sec = 0
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    release_worker = Event()
    try:
        engine._tick()
        manifest = {"analysis_id": "a", "snapshot_hash": "b", "market_as_of": 1, "global_analysis": {}}
        debate_id = db.reserve_debate_session(engine.comp_id, "manual:review55-deadline", "manual", "手动", None, manifest)
        assert debate_id is not None
        assert db.set_debate_session_status(debate_id, "running")

        def wait_for_release(*args, **kwargs):
            release_worker.wait(timeout=2)
            return {"status": "complete", "payload": _opening_payload("glm", "fact-1"), "latency_ms": 1}

        engine._call_debate_model = wait_for_release  # type: ignore[method-assign]
        rows, aborted = engine._run_debate_stage(
            engine.comp_id, engine._generation, engine._decision_epoch, debate_id,
            "opening", ["glm"], {"glm": "fixture prompt"}, {"fact-1"},
        )

        assert not aborted
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "ResearchStageTimedOut"
        detail = db.debate_session(engine.comp_id, debate_id, include_raw=False)
        assert detail["turns"][0]["failure_category"] == "stage_deadline"
    finally:
        release_worker.set()
        engine.stop()
        db.close()


def _opening_payload(trader_id: str, fact_id: str):
    return {
        "stance": "bullish" if trader_id in {"deepseek", "qwen"} else "bearish",
        "confidence": 0.6,
        "thesis": f"{trader_id} opening thesis",
        "supporting_fact_ids": [fact_id],
        "counter_fact_ids": [fact_id],
        "counter_argument": "known counter case",
        "invalidation": "a cited fact changes",
        "uncertainties": ["one uncertainty"],
    }


def test_failed_opening_remains_fail_closed_and_exposes_only_a_safe_category(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    calls = []
    try:
        engine._tick()

        def fake_call(trader_id, prompt, stage, valid_fact_ids, participant_ids):
            calls.append((trader_id, stage))
            if stage == "opening" and trader_id == "glm":
                return {"status": "failed", "payload": {"status": "complete", "thesis": SENTINEL,
                                                            "supporting_fact_ids": [SENTINEL]},
                        "error": "ResearchRequestTimedOut", "prompt": f"prompt {SENTINEL}",
                        "raw_response": f"response {SENTINEL}", "latency_ms": 1}
            assert stage == "opening", "an incomplete opening must never enter rebuttal"
            return {"status": "complete", "payload": _opening_payload(trader_id, sorted(valid_fact_ids)[0]),
                    "raw_response": "{fixture}", "prompt": prompt, "latency_ms": 1, "tokens_in": 10, "tokens_out": 20}

        engine._call_debate_model = fake_call  # type: ignore[method-assign]
        engine._run_preopen_debate(
            engine.comp_id, engine._generation, engine._decision_epoch,
            "manual:review55", "手动赛前研究", None, "manual",
        )

        assert len(calls) == 4 and {stage for _, stage in calls} == {"opening"}
        latest = db.latest_debate_session(engine.comp_id)
        assert latest and latest["status"] == "incomplete"
        detail = db.debate_session(engine.comp_id, latest["id"], include_raw=False)
        failed = next(turn for turn in detail["turns"] if turn["trader_id"] == "glm")
        assert failed["failure_category"] == "timeout"
        assert "error" not in failed and "prompt" not in failed and "raw_response" not in failed
        assert "本轮请求在本地时限内未获得模型响应" in detail["referee_packet"]
        assert SENTINEL not in str(detail)
        assert db.decisions(engine.comp_id, limit=10) == []
        assert db.trades(engine.comp_id, limit=10) == []

        response = TestClient(create_app(engine, ROOT / "web")).get(f"/api/debates/{latest['id']}")
        assert response.status_code == 200
        assert SENTINEL not in response.text
        assert response.json()["turns"][-1]["failure_category"] == "timeout"
    finally:
        engine.stop()
        db.close()


def test_api_whitelists_internal_failure_codes_and_drops_failed_payload_content(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        manifest = {"analysis_id": "a", "snapshot_hash": "b", "market_as_of": 1, "global_analysis": {}}
        debate_id = db.reserve_debate_session(engine.comp_id, "manual:review55-api", "manual", "手动", None, manifest)
        assert debate_id is not None
        db.add_debate_turn(
            debate_id, "opening", "glm", 1, "failed",
            payload={"failure_category": SENTINEL, "thesis": SENTINEL}, raw_response=SENTINEL,
            prompt=SENTINEL, error=SENTINEL,
        )
        db.add_debate_turn(debate_id, "opening", "kimi", 2, "failed", error="ResearchStageTimedOut")
        db.add_debate_turn(
            debate_id, "opening", "qwen", 3, "complete",
            payload={"failure_category": "timeout", "thesis": "safe completed thesis"}, error="ResearchRequestTimedOut",
        )
        db.add_debate_turn(debate_id, "opening", "deepseek", 4, "failed", error="ResearchRequestFailed")

        response = TestClient(create_app(engine, ROOT / "web")).get(f"/api/debates/{debate_id}")
        assert response.status_code == 200
        detail = response.json()
        failed = next(turn for turn in detail["turns"] if turn["trader_id"] == "glm")
        deadline = next(turn for turn in detail["turns"] if turn["trader_id"] == "kimi")
        complete = next(turn for turn in detail["turns"] if turn["trader_id"] == "qwen")
        legacy = next(turn for turn in detail["turns"] if turn["trader_id"] == "deepseek")
        assert "failure_category" not in failed and "thesis" not in failed
        assert deadline["failure_category"] == "stage_deadline"
        assert legacy["failure_category"] == "request_failed"
        assert "failure_category" not in complete and complete["thesis"] == "safe completed thesis"
        assert SENTINEL not in response.text
    finally:
        engine.stop()
        db.close()


def test_referee_packet_never_echoes_a_raw_failure_error():
    packet = referee_packet(
        {"phase_label": "手动", "status": "incomplete", "snapshot": {}, "global_analysis": {}},
        [
            {"stage": "opening", "trader_id": "glm", "status": "failed", "error": f"Authorization: Bearer {SENTINEL}",
             "thesis": SENTINEL, "supporting_fact_ids": [SENTINEL]},
            {"stage": "rebuttal", "trader_id": "glm", "status": "failed", "error": f"Authorization: Bearer {SENTINEL}",
             "challenge": SENTINEL, "challenge_fact_ids": [SENTINEL], "revised_thesis": SENTINEL},
        ],
        {"glm": "智谱 GLM"},
    )

    assert SENTINEL not in packet
    assert "未取得可验证的研究结论" in packet
