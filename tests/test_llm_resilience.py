from types import SimpleNamespace

import pytest

from arena.config import LlmCfg, TraderCfg
from arena.traders.base import AccountView, MarketSnapshot
from arena.traders.llm import LlmTrader


def _snapshot():
    return MarketSnapshot(ts=1.0, round_no=1, market="replay", currency="CNY", symbols={})


def _account():
    return AccountView(
        trader_id="t", equity=1_000_000, cash=1_000_000, available=1_000_000, margin_used=0,
        realized_pnl=0, fees=0, initial_cash=1_000_000, positions=[], recent_trades=[],
        last_decision_summary="", round_no=1,
    )


def _chat_response(content, completion_tokens=32, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=completion_tokens),
    )


class _FakeCompletions:
    def __init__(self, replies=(), error=None):
        self.replies = list(replies)
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _chat_trader(**overrides):
    model = overrides.pop("model", "test")
    cfg = TraderCfg(
        id="t", name="T", provider="openai_compatible", base_url="https://example.invalid/v1", model=model, **overrides
    )
    return LlmTrader(cfg, LlmCfg(max_tokens=1500, max_retries=2))


def _decide(trader):
    return trader.decide(_snapshot(), _account(), "rules")


def test_kimi_chat_uses_completion_cap_and_reasoning_effort():
    trader = _chat_trader(model="kimi-k3", max_completion_tokens=4096, reasoning_effort="low", temperature=1.0)
    completions = _FakeCompletions([_chat_response('{"analysis":"ok","actions":[]}')])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    assert not decision.error
    assert len(completions.calls) == 1
    kwargs = completions.calls[0]
    assert kwargs["max_completion_tokens"] == 4096
    assert "max_tokens" not in kwargs
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["temperature"] == 1.0
    assert kwargs["response_format"] == {"type": "json_object"}


def test_chat_uses_global_temperature_without_a_trader_override():
    trader = _chat_trader()
    completions = _FakeCompletions([_chat_response('{"analysis":"ok","actions":[]}')])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    assert not decision.error
    assert completions.calls[0]["temperature"] == 0.3


def test_responses_uses_its_own_output_and_reasoning_parameters():
    trader = _chat_trader(api_style="responses", max_tokens=4096, reasoning_effort="low")
    responses = _FakeCompletions([SimpleNamespace(
        output_text='{"analysis":"ok","actions":[]}',
        usage=SimpleNamespace(input_tokens=12, output_tokens=34), status="completed",
    )])
    trader._client = SimpleNamespace(responses=responses)

    decision = _decide(trader)

    assert not decision.error
    kwargs = responses.calls[0]
    assert kwargs["max_output_tokens"] == 4096
    assert kwargs["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in kwargs


def test_truncated_json_stops_same_round_retry_and_keeps_evidence():
    trader = _chat_trader()
    completions = _FakeCompletions([_chat_response("{\n", completion_tokens=1500, finish_reason="length")])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    assert len(completions.calls) == 1
    assert decision.raw_response == "{\n"
    assert decision.tokens_out == 1500
    assert decision.actions == []
    assert "truncated" in decision.error
    assert "max_tokens=1500" in decision.error


def test_token_cap_parse_failure_stops_retry_without_finish_reason():
    trader = _chat_trader()
    completions = _FakeCompletions([_chat_response("{", completion_tokens=1500, finish_reason=None)])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    assert len(completions.calls) == 1
    assert "truncated" in decision.error


class _BodyOnlyCreditsError(RuntimeError):
    def __init__(self):
        super().__init__("request failed")
        self.body = {"error": {"message": "You have no credits remaining"}}


class _ZhipuRateLimitError(RuntimeError):
    def __init__(self):
        super().__init__("request rate limited")
        self.status_code = 429
        self.body = {"error": {"code": "1302", "message": "rate limited"}}


@pytest.mark.parametrize("error", [RuntimeError("credit_balance_exhausted"), _BodyOnlyCreditsError()])
def test_credit_exhaustion_stops_same_round_retry(error):
    trader = _chat_trader()
    completions = _FakeCompletions(error=error)
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    assert len(completions.calls) == 1
    assert decision.actions == []
    assert decision.error


def test_provider_rate_limit_stops_same_round_retry_and_preserves_failure():
    trader = _chat_trader()
    completions = _FakeCompletions(error=_ZhipuRateLimitError())
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    assert len(completions.calls) == 1
    assert decision.actions == []
    assert "rate_limited" in decision.error


def test_trader_retry_override_can_disable_same_round_retries():
    trader = _chat_trader(max_retries=0)
    completions = _FakeCompletions(error=RuntimeError("transient timeout"))
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    assert len(completions.calls) == 1
    assert decision.actions == []
    assert decision.error


def test_trader_retry_override_inherits_global_value_when_absent():
    trader = _chat_trader()

    assert trader._effective_max_retries() == 2


def test_decision_timeout_budget_reserves_one_json_fallback_for_the_whole_loop():
    # 3 outer attempts + a single JSON-mode fallback request = 4 * 60s, plus 1+2s backoff.
    chat = _chat_trader()
    assert chat.decision_timeout_budget_sec() == 243

    # Once the fallback has fired, _call cannot make another one: 3 requests only.
    chat._json_mode_ok = False
    assert chat.decision_timeout_budget_sec() == 183

    # Responses never sends response_format, so it has no fallback request at all.
    responses = _chat_trader(api_style="responses", max_retries=3)
    assert responses.decision_timeout_budget_sec() == 247


def test_json_fallback_is_not_retried_after_the_plain_call_fails(monkeypatch):
    # The budget reserves one fallback for the whole loop; this is the worst case
    # that spends it: reject response_format, fail the plain fallback, retry once.
    trader = _chat_trader()
    completions = _FakeCompletions([
        RuntimeError("400 unsupported parameter: response_format"),
        RuntimeError("transient upstream timeout"),
        _chat_response('{"analysis":"ok","actions":[]}'),
    ])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr("arena.traders.llm.time.sleep", lambda _: None)

    decision = _decide(trader)

    assert len(completions.calls) == 3
    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert all("response_format" not in kwargs for kwargs in completions.calls[1:])
    assert trader._json_mode_ok is False
    assert not decision.error


def test_decision_timeout_budget_matches_the_exact_retry_backoff_above_three_retries():
    # decide() sleeps min(2 ** attempt, 8) before each retry: 1+2+4+8+8 for 5 retries.
    assert LlmTrader.retry_backoff_seconds(0) == 0
    assert LlmTrader.retry_backoff_seconds(4) == 15
    assert LlmTrader.retry_backoff_seconds(5) == 23

    chat = _chat_trader(max_retries=4)
    assert chat.decision_timeout_budget_sec() == 6 * 60 + 15

    responses = _chat_trader(api_style="responses", max_retries=5)
    assert responses.decision_timeout_budget_sec() == 6 * 60 + 23


def test_generic_rate_limit_keeps_configured_retry(monkeypatch):
    trader = _chat_trader()
    completions = _FakeCompletions(error=RuntimeError("429 rate limit"))
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr("arena.traders.llm.time.sleep", lambda _: None)

    decision = _decide(trader)

    assert len(completions.calls) == 3
    assert decision.actions == []


def test_research_uses_its_own_plain_json_boundary_without_downgrading_decisions():
    trader = _chat_trader()
    completions = _FakeCompletions([
        _chat_response('{"stance":"neutral"}'),
        _chat_response('{"analysis":"ok","actions":[]}'),
    ])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    research = trader.research("research system", "research prompt", 128, timeout_sec=2.5)
    decision = _decide(trader)

    assert research.text == '{"stance":"neutral"}'
    assert completions.calls[0]["timeout"] == 2.5
    assert "response_format" not in completions.calls[0]
    assert completions.calls[1]["response_format"] == {"type": "json_object"}
    assert not decision.error


def test_qwen_research_cap_does_not_replace_the_normal_decision_cap():
    trader = _chat_trader(max_completion_tokens=3584, research_max_completion_tokens=1536,
                          reasoning_effort="low", max_retries=0)
    completions = _FakeCompletions([
        _chat_response('{"stance":"neutral"}'),
        _chat_response('{"analysis":"ok","actions":[]}'),
    ])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    research = trader.research("research system", "research prompt", 700, timeout_sec=47.0)
    decision = _decide(trader)

    assert research.text == '{"stance":"neutral"}'
    assert not decision.error
    research_kwargs, decision_kwargs = completions.calls
    assert research_kwargs["max_completion_tokens"] == 1536
    assert "max_tokens" not in research_kwargs
    assert research_kwargs["reasoning_effort"] == "low"
    assert research_kwargs["timeout"] == 47.0
    assert "response_format" not in research_kwargs
    assert decision_kwargs["max_completion_tokens"] == 3584
    assert "max_tokens" not in decision_kwargs


def test_missing_usage_remains_none_not_false_zero():
    trader = _chat_trader(max_retries=0)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"analysis":"ok","actions":[]}'),
                                 finish_reason="stop")],
        usage=None,
    )
    completions = _FakeCompletions([response])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    assert not decision.error
    assert decision.tokens_in is None
    assert decision.tokens_out is None


def test_empty_reply_fallback_is_spent_once_per_decision_loop():
    """每次外层重试都回退一次的话，两次重试会发 6 个请求，而轮超时预算只为整个循环预留了 4 个。"""
    trader = _chat_trader(max_retries=2)
    completions = _FakeCompletions([
        _chat_response(""), _chat_response("not json at all"),   # 第一次：JSON 模式空回复 → 普通模式回退，仍解析失败
        _chat_response(""), _chat_response("not json at all"),   # 第二次：不允许再回退，多出来的回复不应被消费
        _chat_response(""), _chat_response("not json at all"),
    ])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = _decide(trader)

    budgeted_requests = 2 + 1 + 1   # retries + 1 次首发 + 整个循环一次回退
    assert decision.error and "解析失败" in decision.error
    assert len(completions.calls) == budgeted_requests, [
        "response_format" in call for call in completions.calls
    ]
    assert ["response_format" in call for call in completions.calls] == [True, False, True, True]
    assert trader._json_mode_ok is True, "空回复不是不支持，JSON 模式不能被永久关掉"
    assert decision.tokens_in == 12 * budgeted_requests
    assert trader.decision_timeout_budget_sec() >= budgeted_requests * trader.lcfg.timeout_sec


def test_a_failed_empty_reply_fallback_still_spends_the_single_fallback(monkeypatch):
    """回退那次请求抛异常也算用掉了预算里的那一个请求：后面的尝试不能再回退。"""
    trader = _chat_trader(max_retries=1)
    completions = _FakeCompletions([
        _chat_response(""), RuntimeError("upstream reset"),   # 首发空回复 → 回退时连接断了（可重试）
        _chat_response(""), _chat_response('{"analysis":"ok","actions":[]}'),
    ])
    trader._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr("arena.traders.llm.time.sleep", lambda _: None)

    decision = _decide(trader)

    assert ["response_format" in call for call in completions.calls] == [True, False, True]
    assert decision.error and "解析失败" in decision.error
