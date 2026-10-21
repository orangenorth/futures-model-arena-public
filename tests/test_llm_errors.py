"""LLM 交易员对接口错误的处理：Key/余额/模型名错误直接给出中文提示且不重试；只有服务端明确拒绝 JSON 模式才回退；
JSON 模式空回复本次改普通模式重试；超时可重试；输出被 max_tokens 截断时提示。"""
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from arena.config import LlmCfg, TraderCfg
from arena.traders.llm import LlmTrader, _json_mode_rejected, _rejected_token_param, classify_error

OK_JSON = json.dumps({"analysis": "ok", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 3300}]})
REQ = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")


class FakeClient:
    """按顺序返回预设结果（异常则抛出）；记录每次请求的参数。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def reply(content, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def status_error(code, message, cls=openai.APIStatusError):
    return cls(f"Error code: {code} - {{'error': {{'message': '{message}'}}}}", response=httpx.Response(code, request=REQ), body=None)


def make_trader(outcomes, **lcfg):
    tcfg = TraderCfg(id="deepseek", name="DS", base_url="https://api.deepseek.com/v1", model="deepseek-chat", api_key="sk-test")
    tr = LlmTrader(tcfg, LlmCfg(**{"max_retries": 1, "timeout_sec": 5, **lcfg}))
    tr._client = FakeClient(outcomes)
    return tr


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr("arena.traders.llm.build_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr("arena.traders.llm.time.sleep", lambda s: None)


SNAP = SimpleNamespace(round_no=1, symbols={"RB0": None})


def test_insufficient_balance_is_reported_in_chinese_without_retry():
    tr = make_trader([status_error(402, "Insufficient Balance")])
    d = tr.decide(SNAP, None, "")
    assert not d.actions and "余额不足" in d.error and "402" in d.error and "Insufficient Balance" in d.error
    assert len(tr._client.calls) == 1, "余额不足重试也不会好，不该再发一次"


def test_invalid_key_is_reported_and_does_not_disable_json_mode():
    tr = make_trader([status_error(401, "Authentication Fails, Your api key is invalid", openai.AuthenticationError)])
    d = tr.decide(SNAP, None, "")
    assert "API Key 无效" in d.error and len(tr._client.calls) == 1
    assert tr._json_mode_ok is True, "“invalid” 出现在 Key 错误里，不能被当成“不支持 JSON 模式”"


def test_bad_model_name_and_max_tokens_hints():
    tr = make_trader([status_error(400, "Model Not Exist", openai.BadRequestError)])
    assert "模型名无效" in tr.decide(SNAP, None, "").error
    tr = make_trader([status_error(400, "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]", openai.BadRequestError)])
    d = tr.decide(SNAP, None, "")
    assert "llm.max_tokens=1500" in d.error and "调小" in d.error and tr._json_mode_ok is True and len(tr._client.calls) == 1


TOKEN_400 = "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]"


def test_token_limit_hint_names_the_trader_override_that_is_in_effect():
    """交易员级覆盖生效时，提示不能再让用户去改 llm.max_tokens（改了也不会改变请求）。"""
    tr = make_trader([status_error(400, TOKEN_400, openai.BadRequestError)])
    tr.tcfg = tr.tcfg.model_copy(update={"max_completion_tokens": 4096})     # Kimi 默认配置的写法
    d = tr.decide(SNAP, None, "")
    assert "交易员 deepseek 的 max_completion_tokens=4096" in d.error and "llm.max_tokens" not in d.error
    tr = make_trader([status_error(400, TOKEN_400, openai.BadRequestError)])
    tr.tcfg = tr.tcfg.model_copy(update={"max_tokens": 3000})
    assert "交易员 deepseek 的 max_tokens=3000" in tr.decide(SNAP, None, "").error
    # 截断提示同样指向生效的配置项
    tr = make_trader([reply('{"analysis": "太长了', finish="length")])
    tr.tcfg = tr.tcfg.model_copy(update={"max_completion_tokens": 4096})
    d = tr.decide(SNAP, None, "")
    assert "max_completion_tokens=4096" in d.error and "请增大 交易员 deepseek 的 max_completion_tokens" in d.error


UNSUPPORTED_MAX_TOKENS = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
                          "Use 'max_completion_tokens' instead.")


def test_unsupported_token_parameter_is_not_reported_as_an_oversized_limit():
    """"该模型不收 max_tokens" 与 "max_tokens 值越界" 都是 400，处理方式却相反：
    前者再小的值也发不出去，必须改用模型认的那个参数。"""
    tr = make_trader([status_error(400, UNSUPPORTED_MAX_TOKENS, openai.BadRequestError)])
    d = tr.decide(SNAP, None, "")
    assert "不支持 max_tokens" in d.error and "max_completion_tokens" in d.error
    assert "超出该模型允许范围" not in d.error and "调小输出上限没有用" in d.error, "调小上限不会让这类请求变成功"
    assert len(tr._client.calls) == 1 and tr._json_mode_ok is True

    # 反方向：交易员配了 max_completion_tokens，但这个服务端只认 max_tokens
    tr = make_trader([status_error(400, "Unsupported parameter: 'max_completion_tokens'", openai.BadRequestError)])
    tr.tcfg = tr.tcfg.model_copy(update={"max_completion_tokens": 4096})
    d = tr.decide(SNAP, None, "")
    assert "不支持 max_completion_tokens" in d.error and "删掉交易员 deepseek 的 max_completion_tokens" in d.error


def test_oversized_limit_is_still_reported_as_oversized():
    """值越界的原文里也带参数名，不能被上一条的判定顺手吃掉。"""
    hint, retryable = classify_error(status_error(400, TOKEN_400, openai.BadRequestError),
                                     TraderCfg(id="x", name="x", model="m", api_key="k"), LlmCfg())
    assert "超出该模型允许范围" in hint and "调小" in hint and not retryable


def test_rejected_token_param_is_taken_from_the_sdk_metadata_when_present():
    """原文里同时出现被拒的参数和建议改用的参数，只按出现顺序会挑错；有结构化 param 就以它为准。"""
    e = openai.BadRequestError("Error code: 400 - use max_tokens; max_completion_tokens is not supported",
                               response=httpx.Response(400, request=REQ),
                               body={"message": "...", "param": "max_completion_tokens", "code": "unsupported_parameter"})
    assert _rejected_token_param(e, str(e)) == "max_completion_tokens"
    ok = openai.BadRequestError("Error code: 400 - " + TOKEN_400, response=httpx.Response(400, request=REQ),
                                body={"message": TOKEN_400, "param": "max_tokens", "code": "invalid_value"})
    assert _rejected_token_param(ok, str(ok)) == "", "值越界时 param 同样填 max_tokens，不能据此判成“不支持”"


def test_credit_exhaustion_wins_over_the_generic_rate_limit_hint():
    """额度耗尽常以 429 返回。429 的通用提示是"稍后自动重试"，但充值之前重试永远不会成功——
    实际也确实没有重试，看板却留着那句相反的话；命令行自检走同一个 classify_error，之前也印这句。"""
    e = openai.RateLimitError("Error code: 429 - {'error': {'code': 'credit_balance_exhausted'}}",
                              response=httpx.Response(429, request=REQ),
                              body={"code": "credit_balance_exhausted"})
    hint, retryable = classify_error(e, TraderCfg(id="x", name="x", model="m", api_key="k"), LlmCfg())
    assert "额度已用尽" in hint and "充值" in hint and not retryable
    assert "稍后自动重试" not in hint

    tr = make_trader([e])
    d = tr.decide(SNAP, None, "")
    assert "额度已用尽" in d.error and "稍后自动重试" not in d.error
    assert len(tr._client.calls) == 1, "充值前重试不会成功"

    # 普通 429（不是额度耗尽）仍然是"稍后自动重试"且可重试
    hint, retryable = classify_error(status_error(429, "Rate limit reached"),
                                     TraderCfg(id="x", name="x", model="m", api_key="k"), LlmCfg())
    assert "稍后自动重试" in hint and retryable


def test_content_filter_empty_reply_is_not_retried_as_a_json_mode_quirk():
    """空内容 + finish_reason=content_filter 不是 JSON 模式的偶发问题：再要一次仍会被拦，
    还会用第二次响应盖掉第一次的 finish_reason / usage，最后只剩一句看不懂的"解析失败"。
    按默认两次外层重试，一次被拦的决策原本能发出六次付费请求。"""
    tr = make_trader([reply("", finish="content_filter")] + [reply(OK_JSON)] * 5)
    d = tr.decide(SNAP, None, "")
    assert len(tr._client.calls) == 1, "被内容安全策略拦截后不该再发任何请求"
    assert tr._json_mode_ok is True and "response_format" in tr._client.calls[0]
    assert "内容安全策略拦截" in d.error and "content_filter" in d.error and not d.actions


def test_an_unknown_terminal_finish_reason_also_ends_the_round():
    tr = make_trader([reply("", finish="tool_calls")] + [reply(OK_JSON)] * 5)
    d = tr.decide(SNAP, None, "")
    assert len(tr._client.calls) == 1 and "tool_calls" in d.error and "重试不会改变结果" in d.error


def test_unsupported_max_tokens_hint_replaces_an_existing_trader_override():
    """两者不能同时配（TraderCfg._check_output_token_overrides），照"再加一个"去改会卡在配置校验上。"""
    tr = make_trader([status_error(400, UNSUPPORTED_MAX_TOKENS, openai.BadRequestError)])
    tr.tcfg = tr.tcfg.model_copy(update={"max_tokens": 3000})
    d = tr.decide(SNAP, None, "")
    assert "把交易员 deepseek 的 max_tokens 换成 max_completion_tokens" in d.error and "两者不能同时配" in d.error
    # 没有交易员级覆盖时仍然是"配上"
    tr = make_trader([status_error(400, UNSUPPORTED_MAX_TOKENS, openai.BadRequestError)])
    assert "给交易员 deepseek 配上 max_completion_tokens" in tr.decide(SNAP, None, "").error


def test_insufficient_quota_is_recognized_as_exhausted_credit():
    """`insufficient_quota` 是 OpenAI 兼容接口最常见的额度耗尽标记（429 的 code / type 都填它）。
    漏掉它就会落进通用的 429"稍后自动重试"：重试跑满、看板还让用户等，而充值之前重试永远不会成功。"""
    for body in ({"message": "You exceeded your current quota, please check your plan and billing details.",
                  "type": "insufficient_quota", "code": "insufficient_quota"},
                 {"message": "You exceeded your current quota."}):          # 只有原文、没有结构化字段
        e = openai.RateLimitError("Error code: 429", response=httpx.Response(429, request=REQ), body=body)
        hint, retryable = classify_error(e, TraderCfg(id="x", name="x", model="m", api_key="k"), LlmCfg())
        assert "额度已用尽" in hint and "充值" in hint and not retryable
        assert "稍后自动重试" not in hint

    tr = make_trader([openai.RateLimitError("Error code: 429", response=httpx.Response(429, request=REQ),
                                            body={"code": "insufficient_quota"})])
    d = tr.decide(SNAP, None, "")
    assert "额度已用尽" in d.error and len(tr._client.calls) == 1, "充值前重试不会成功"


def test_a_plain_rate_limit_is_still_retried():
    """真正的限流必须继续走"稍后自动重试"，不能被额度标记顺手吃掉。"""
    e = openai.RateLimitError("Error code: 429 - Rate limit reached for requests per min",
                              response=httpx.Response(429, request=REQ),
                              body={"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"})
    hint, retryable = classify_error(e, TraderCfg(id="x", name="x", model="m", api_key="k"), LlmCfg())
    assert "稍后自动重试" in hint and retryable and "额度已用尽" not in hint


def test_json_mode_rejection_falls_back_to_plain_mode():
    tr = make_trader([status_error(400, "response_format is not supported for this model", openai.BadRequestError), reply(OK_JSON)])
    d = tr.decide(SNAP, None, "")
    assert d.actions and not d.error
    assert "response_format" in tr._client.calls[0] and "response_format" not in tr._client.calls[1]
    assert tr._json_mode_ok is False
    assert _json_mode_rejected("Unsupported parameter: 'response_format'") and not _json_mode_rejected("Invalid API key")


def test_empty_reply_that_was_truncated_is_not_retried_in_plain_mode():
    """空内容也可能是输出上限耗尽（推理模型把预算花在思考上）：那是截断，
    再要一次同样会被截断，还会丢掉 finish_reason / usage 与"调大上限"的提示。"""
    tr = make_trader([reply("", finish="length"), reply(OK_JSON)])
    d = tr.decide(SNAP, None, "")
    assert len(tr._client.calls) == 1, "截断不该触发第二次付费请求"
    assert "response_format" in tr._client.calls[0] and tr._json_mode_ok is True
    assert not d.actions and "截断" in d.error and "llm.max_tokens" in d.error


def test_empty_json_mode_reply_is_retried_once_in_plain_mode():
    tr = make_trader([reply(""), reply(OK_JSON)])
    d = tr.decide(SNAP, None, "")
    assert d.actions and not d.error and len(tr._client.calls) == 2
    assert "response_format" not in tr._client.calls[1] and tr._json_mode_ok is True, "空内容不是“不支持”，不永久关闭 JSON 模式"
    # 两次都是付费调用：用量要累加，不能让第二次的覆盖第一次的（reply() 每次给 10/5）
    assert (d.tokens_in, d.tokens_out) == (20, 10), f"少算了一整次请求：{d.tokens_in}/{d.tokens_out}"


def test_aggregated_usage_does_not_fake_a_truncation():
    """output_limit 是**每次调用**的上限。把两次调用的合计拿去比，会把两次正常完成的短回复
    误判成"被截断"，本轮该有的解析重试也被跳过——计费口径与截断判定必须分开。"""
    tr = make_trader([reply(""), reply("不是 JSON")], max_tokens=8)   # 每次 5 个输出 token，合计 10 > 8
    d = tr.decide(SNAP, None, "")
    assert (d.tokens_in, d.tokens_out) == (20, 10), "计费口径仍是两次的合计"
    assert "截断" not in d.error, f"两次都正常完成，不该报截断：{d.error}"
    assert len(tr._client.calls) > 2, "没被误判成截断，本轮的解析重试应当照常进行"


def test_a_real_truncation_is_still_detected_after_a_retry():
    """最后一次调用确实把上限吃满时仍要判成截断。"""
    tr = make_trader([reply(""), reply('{"analysis": "太长了', finish="length")], max_tokens=8)
    d = tr.decide(SNAP, None, "")
    assert "截断" in d.error and len(tr._client.calls) == 2


def test_an_empty_reply_that_filled_the_token_limit_is_not_retried():
    """有些实现不返回 finish_reason，截断只能靠"输出 token 顶满上限"认出来。
    闸门只看 finish_reason 的话，这种空回复会再发一次注定同样被截断的付费请求，
    还会把截断证据换掉，最后报成看不懂的"解析失败"。"""
    tr = make_trader([reply("", finish=""), reply(OK_JSON)], max_tokens=5)   # 输出 5 token，上限也是 5
    d = tr.decide(SNAP, None, "")
    assert len(tr._client.calls) == 1, "已经顶满上限，不该再发一次"
    assert "截断" in d.error and "max_tokens" in d.error


def test_usage_survives_a_failed_fallback_request():
    """第一次调用成功并计费了，只是回复为空；回退请求失败不该让那笔开销凭空消失。"""
    tr = make_trader([reply(""), openai.APITimeoutError(request=REQ)], max_retries=0)
    d = tr.decide(SNAP, None, "")
    assert "超时" in d.error
    assert (d.tokens_in, d.tokens_out) == (10, 5), f"第一次已计费的用量丢了：{d.tokens_in}/{d.tokens_out}"


def test_usage_accumulates_across_outer_retries():
    """外层每次重试都是真实发生的付费调用，不能被最后一次的用量覆盖。"""
    tr = make_trader([reply(""), openai.APITimeoutError(request=REQ), reply(OK_JSON)], max_retries=1)
    d = tr.decide(SNAP, None, "")
    assert d.actions and not d.error
    assert (d.tokens_in, d.tokens_out) == (20, 10), f"少算了失败那次尝试里的调用：{d.tokens_in}/{d.tokens_out}"


def test_single_call_usage_is_not_double_counted():
    """没有触发重试时用量就是那一次的，别把累加写成翻倍。"""
    tr = make_trader([reply(OK_JSON)])
    d = tr.decide(SNAP, None, "")
    assert len(tr._client.calls) == 1 and (d.tokens_in, d.tokens_out) == (10, 5)


def test_timeout_is_retried_and_hinted():
    tr = make_trader([openai.APITimeoutError(request=REQ), reply(OK_JSON)])
    assert tr.decide(SNAP, None, "").actions
    tr = make_trader([openai.APITimeoutError(request=REQ), openai.APITimeoutError(request=REQ)])
    d = tr.decide(SNAP, None, "")
    assert "超时" in d.error and "timeout_sec" in d.error and len(tr._client.calls) == 2


def test_truncated_output_points_at_max_tokens():
    tr = make_trader([reply('{"analysis": "太长了', finish="length"), reply('{"analysis": "太长了', finish="length")])
    d = tr.decide(SNAP, None, "")
    assert "解析失败" in d.error and "截断" in d.error and "max_tokens" in d.error


def test_responses_api_truncation_is_reported_with_its_cap():
    tcfg = TraderCfg(id="codex", name="C", base_url="https://api.openai.com/v1", model="gpt-5.3-codex", api_key="sk-test",
                     api_style="responses", max_tokens=64)
    tr = LlmTrader(tcfg, LlmCfg(max_retries=1))
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text='{"analysis": "太长了', status="incomplete",
                               incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                               usage=SimpleNamespace(input_tokens=10, output_tokens=64))

    tr._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    d = tr.decide(SNAP, None, "")
    assert len(calls) == 1, "被截断的输出再要一次仍会被截断，本轮不重试"
    assert "截断" in d.error and "max_output_tokens=64" in d.error and not d.actions


def test_classify_connection_error():
    tcfg = TraderCfg(id="x", name="x", base_url="https://api.deepseek.com/v1", model="deepseek-chat", api_key="k")
    hint, retryable = classify_error(openai.APIConnectionError(request=REQ), tcfg, LlmCfg())
    assert "无法连接" in hint and "api.deepseek.com" in hint and retryable


def refusal_reply(text, finish="stop"):
    """安全拒答的响应形状：content 是 None，拒答文本在 message.refusal 里，finish_reason 仍是 stop。"""
    message = SimpleNamespace(content=None, refusal=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=0))


def test_a_structured_refusal_is_not_retried_as_an_empty_reply():
    """安全拒答走 message.refusal：content 是 None、finish_reason 还是 "stop"，
    三项加起来和"JSON 模式偶发空回复"长得一模一样。

    当成偶发空回复去重试的话，默认外层重试下会变成多次付费调用，最后报一句看不懂的"解析失败"。
    还原成"不看 refusal"后本用例失败（会数到多次调用）。
    """
    trader = make_trader([refusal_reply("I can't help with that request.")] * 6, max_retries=2)
    d = trader.decide(SNAP, None, "")
    assert len(trader._client.calls) == 1, f"拒答被当成偶发空回复重试了 {len(trader._client.calls)} 次"
    assert "拒绝回答" in (d.error or "") and "I can't help" in (d.error or "")
    assert d.tokens_in == 10, "已经发生的付费调用没有如实记账"
