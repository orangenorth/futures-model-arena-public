"""Jev 席位参加赛前研究与全品种研究。

原来 `_product_research_readiness()` 与 `_call_debate_model()` 只认 `LlmTrader`：四席位里只要有一个
Jev，全品种研究永远就绪不了，盘前辩论也把它记成 ResearchParticipantUnavailable。Jev 不产出文字，
所以走类型化适配：立场、依据、反方依据、反证对象由 Jev 回答，文本由代码组装，仍过同一套 JSON 校验。
"""
from __future__ import annotations

import json

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.debate import parse_opening, parse_rebuttal
from arena.engine.loop import Engine
from arena.traders.jev import TYPED_RESEARCH_NOTE

from .test_jev_trader import ROOT, _StubClient, _trader

FACTS = {"RB0:quote", "RB0:trend", "RB0:score"}
PARTICIPANTS = {"jev", "a", "b", "c"}


class _TimeoutAwareClient(_StubClient):
    def post(self, path, json=None, timeout=None):
        self.timeouts = getattr(self, "timeouts", []) + [timeout]
        return super().post(path, json=json)


def _pick(choice, p, others=()):
    probs = {choice: p}
    rest = (1.0 - p) / max(1, len(others))
    probs.update({o: rest for o in others})
    return {"choice": choice, "probabilities": probs, "confidence": 0.4}


def _payload(answers):
    return {"model": "jev-latest", "answers": answers, "usage": {"input_tokens": 50, "output_tokens": 3}}


def test_an_opening_is_typed_answers_assembled_into_the_debate_contract():
    client = _TimeoutAwareClient(_payload({
        "stance": _pick("bullish", 0.62, ("bearish", "neutral")),
        "support": _pick("RB0:trend", 0.55),
        "counter": _pick("RB0:score", 0.48),
    }))
    trader = _trader(client)
    result = trader.research_turn("opening", "事实集……", FACTS, PARTICIPANTS, timeout_sec=12.0)

    assert len(client.calls) == 1 and client.timeouts == [12.0]
    body = client.calls[0].body
    assert body["state"] == "事实集……", "state 必须原样是四个参与者看到的同一份 prompt"
    assert set(body["questions"]) == {"stance", "support", "counter"}
    assert set(body["questions"]["support"]["criteria"]) == FACTS
    payload = parse_opening(result.text, FACTS)
    assert payload["stance"] == "bullish" and payload["confidence"] == 0.62
    assert payload["supporting_fact_ids"] == ["RB0:trend"] and payload["counter_fact_ids"] == ["RB0:score"]
    assert payload["thesis"].startswith(TYPED_RESEARCH_NOTE)
    assert json.loads(result.text)["generated_by"] == "typed_adapter"
    assert (result.tokens_in, result.tokens_out) == (50, 3)


def test_a_rebuttal_targets_another_participant():
    client = _StubClient(_payload({
        "stance": _pick("neutral", 0.5, ("bullish", "bearish")),
        "target": _pick("b", 0.7),
        "challenge": _pick("RB0:quote", 0.6),
    }))
    trader = _trader(client)
    result = trader.research_turn("rebuttal", "第一轮……", FACTS, PARTICIPANTS)

    targets = client.calls[0].body["questions"]["target"]["criteria"]
    assert set(targets) == {"a", "b", "c"}, "不能让 Jev 反证自己"
    payload = parse_rebuttal(result.text, FACTS, PARTICIPANTS, "jev")
    assert payload["target_trader_id"] == "b" and payload["challenge_fact_ids"] == ["RB0:quote"]


@pytest.mark.parametrize("answers", [
    {"stance": _pick("sideways", 0.9), "support": _pick("RB0:trend", 0.5), "counter": _pick("RB0:score", 0.5)},
    {"stance": _pick("bullish", 0.6), "support": _pick("RB9:made_up", 0.5), "counter": _pick("RB0:score", 0.5)},
    {"stance": _pick("bullish", 1.7), "support": _pick("RB0:trend", 0.5), "counter": _pick("RB0:score", 0.5)},
    {"stance": _pick("bullish", 0.6), "support": _pick("RB0:trend", 0.5)},
])
def test_an_answer_outside_the_contract_is_rejected(answers):
    with pytest.raises(ValueError):
        _trader(_StubClient(_payload(answers))).research_turn("opening", "s", FACTS, PARTICIPANTS)


def _four_seat_engine(tmp_path):
    seats = [TraderCfg(id=i, name=i.upper(), provider="openai_compatible", base_url="https://example.invalid/v1",
                       model="m", api_key="sk-test") for i in ("a", "b", "c")]
    seats.append(TraderCfg(id="jev", name="Jev", provider="jev", base_url="https://api.typesafe.ai",
                           model="jev-latest"))
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=seats,
        storage={"db_path": str(tmp_path / "jev-research.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


def test_a_jev_seat_counts_toward_the_four_research_participants(tmp_path):
    eng, db = _four_seat_engine(tmp_path)
    try:
        readiness = eng._product_research_readiness()
        assert readiness["ready"] and readiness["unavailable"] == [], readiness
    finally:
        eng.stop()
        db.close()


def test_the_debate_call_routes_a_jev_seat_through_the_typed_adapter(tmp_path):
    eng, db = _four_seat_engine(tmp_path)
    try:
        eng.traders["jev"]._client = _TimeoutAwareClient(_payload({
            "stance": _pick("bearish", 0.58, ("bullish", "neutral")),
            "support": _pick("RB0:quote", 0.5),
            "counter": _pick("RB0:trend", 0.5),
        }), _payload({"stance": _pick("bullish", 0.6)}))
        result = eng._call_debate_model("jev", "prompt", "opening", FACTS, PARTICIPANTS)
        assert result["status"] == "complete", result
        assert result["payload"]["stance"] == "bearish" and result["tokens_out"] == 3

        # 答案不合契约：已送达、可判定的结构失败，不是"投递未知"
        broken = eng._call_debate_model("jev", "prompt", "opening", FACTS, PARTICIPANTS)
        assert broken["status"] == "failed" and broken["error"].startswith("ResearchResponse"), broken
    finally:
        eng.stop()
        db.close()


def test_a_rule_seat_is_still_not_a_research_participant(tmp_path):
    eng, db = _four_seat_engine(tmp_path)
    try:
        eng.traders["jev"] = object()
        assert eng._product_research_readiness()["unavailable"] == ["jev"]
        assert eng._call_debate_model("jev", "p", "opening", FACTS, PARTICIPANTS)["error"] == \
            "ResearchParticipantUnavailable"
    finally:
        eng.stop()
        db.close()


def test_httpx_failures_from_a_jev_seat_get_the_same_research_codes():
    import httpx

    request = httpx.Request("POST", "https://api.typesafe.ai/v1/systemone")
    assert Engine._classify_debate_request_failure(httpx.ReadTimeout("slow", request=request)) == \
        "ResearchRequestTimedOut"
    assert Engine._classify_debate_request_failure(httpx.ConnectError("down", request=request)) == \
        "ResearchConnectionFailed"
    for status, code in ((401, "ResearchAuthorizationFailed"), (429, "ResearchRateLimited"),
                         (503, "ResearchUpstream5xx")):
        exc = httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
        assert Engine._classify_debate_request_failure(exc) == code
