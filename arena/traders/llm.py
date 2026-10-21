"""OpenAI 兼容接口的 LLM 交易员。"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, List, Optional

from ..config import LlmCfg, TraderCfg
from ..models import Decision
from .base import AccountView, MarketSnapshot, Trader
from .prompt import SYSTEM_PROMPT, build_prompt, parse_response

log = logging.getLogger("arena.traders.llm")

# 服务端表示"输出被上限截断"的 finish_reason（各家命名不同）
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens", "max_completion_tokens"})
# 表示"模型正常说完了"的 finish_reason。空回复只有在这种情况下才可能是 JSON 模式的偶发问题，
# 值得改普通模式再要一次；其余终止原因（内容安全拦截、工具调用、截断…）本身就是答案，再要一次只是重复花钱。
NORMAL_FINISH_REASONS = frozenset({"", "stop", "end_turn", "stop_sequence", "eos"})
# 内容安全策略拦截：各家命名不同，但都不该被当成"偶发空回复"
BLOCKED_FINISH_REASONS = frozenset({"content_filter", "content_filtered", "safety", "blocked", "recitation"})


def _finish_reason_hint(reason: str) -> str:
    """终止原因本身已经解释了"为什么没有可用内容"时，把它翻译成可照做的中文；否则返回 ""。

    截断由 `_is_output_truncated` 单独处理（它还要带上生效的上限配置项），这里不重复。
    """
    r = (reason or "").casefold()
    if r in BLOCKED_FINISH_REASONS:
        return f"回复被模型的内容安全策略拦截（finish_reason={reason}）：本轮记为 HOLD，重试不会改变结果；持续出现可换用其他模型"
    if r and r not in NORMAL_FINISH_REASONS and r not in TRUNCATED_FINISH_REASONS:
        return f"模型未产出可用内容（finish_reason={reason}）：重试不会改变结果"
    return ""


def _refusal_hint(refusal: str) -> str:
    """把 Chat Completions 的结构化拒答翻译成可照做的中文。

    拒答走的是 `message.refusal` 这个**独立字段**，而 `content` 是 None、`finish_reason` 还是
    "stop"——三项加起来和"JSON 模式偶发空回复"长得一模一样。把它当成偶发空回复去重试，
    默认外层重试下会变成六次付费调用，最后报一句看不懂的"解析失败"。
    """
    text = (refusal or "").strip()
    if not text:
        return ""
    return (f"模型拒绝回答（refusal）：{text[:200]}；本轮记为 HOLD，"
            f"重试不会改变结果——如持续出现，请检查 prompt 是否触发了该模型的安全策略")


def _responses_refusal(resp) -> str:
    """Responses 路由的拒答：它挂在 output[].content[] 里 type == "refusal" 的那一项上。"""
    parts = []
    for item in (getattr(resp, "output", None) or []):
        for block in (getattr(item, "content", None) or []):
            if getattr(block, "type", "") == "refusal":
                value = getattr(block, "refusal", None)
                if value:
                    parts.append(str(value).strip())
    return "；".join(parts)


def _looks_truncated(finish_reason: str, tokens_out: Optional[int], output_limit: int) -> bool:
    """这次调用是否被输出上限截断。

    两种信号缺一不可：服务端声明的 finish_reason，以及"输出 token 已经顶满该次调用的上限"——
    有些实现根本不返回 finish_reason，只能靠后者认出来。判定截断与"要不要重试"必须用同一套信号，
    否则会出现"重试闸门放行、上层又判成截断"这种自相矛盾。
    """
    return finish_reason.casefold() in TRUNCATED_FINISH_REASONS or (
        tokens_out is not None and 0 < output_limit <= tokens_out
    )


def _add_tokens(first: Optional[int], second: Optional[int]) -> Optional[int]:
    """两次付费调用的用量合计：两次都缺失才是 None（不冒充成 0）；只缺一次时给出已知部分，那是下界而不是精确值。"""
    if first is None and second is None:
        return None
    return int(first or 0) + int(second or 0)


def _json_mode_rejected(msg: str) -> bool:
    """服务端是否明确拒绝了 response_format=json_object（只有这时才回退普通模式）。

    以前用 "invalid" / "unsupported" 泛匹配：Key 无效（"Your api key is invalid"）、max_tokens 越界（"Invalid max_tokens"）
    这类错误也被当成"不支持 JSON 模式"，白白重发一次注定失败的请求，日志还写成"不支持 json_object"，把真正原因盖住。
    """
    m = msg.lower()
    return "response_format" in m or "json_object" in m or "json_schema" in m or "json mode" in m


CREDIT_EXHAUSTED_HINT = "账户额度已用尽：请到该模型的开放平台充值后再试"


# 额度耗尽在各家错误体里的标记。`insufficient_quota` 是 OpenAI 兼容接口最常见的那个
# （429 的 error.code / error.type 都填它），漏掉它就会落进通用的 429"稍后自动重试"分支：
# 重试跑满、看板还让用户等，而在充值之前重试永远不会成功。
CREDIT_EXHAUSTED_MARKERS = (
    "credit_balance_exhausted",
    "no credits remaining",
    "insufficient_quota",
    "exceeded your current quota",
)


def is_credit_exhausted(exc: Exception) -> bool:
    """额度耗尽：各家可能挂在 400 / 402 / 429 上，只能按错误体里的标记认。"""
    details = " ".join(
        str(value)
        for value in (exc, getattr(exc, "code", ""), getattr(exc, "type", ""), getattr(exc, "body", ""))
        if value
    ).casefold()
    return any(marker in details for marker in CREDIT_EXHAUSTED_MARKERS)


TOKEN_PARAMS = ("max_completion_tokens", "max_output_tokens", "max_tokens")
_UNSUPPORTED_MARKERS = ("unsupported", "not supported", "unrecognized", "unknown parameter", "不支持")


def _rejected_token_param(e: Exception, msg: str) -> str:
    """服务端把某个输出上限参数**本身**判为"不支持"时返回该参数名；只是值越界则返回 ""。

    两者都是 400，但处理方式相反：值越界调小就能过，参数不被支持时再小的值也发不出去，
    必须换成该模型认的那个参数（新版 OpenAI 系模型只收 `max_completion_tokens`）。

    先要求报错里出现"不支持"字样（或 code=unsupported_parameter），再用 SDK 异常的结构化 `param` 认定是哪一个：
    `param` 在值越界时同样会填 `max_tokens`，单看它会把"调小就能过"误判成"换参数"；
    而原文常常同时出现被拒的参数和建议改用的参数（"'max_tokens' is not supported … Use 'max_completion_tokens' instead"），
    单看文本顺序也会挑错。没有 `param` 的实现（多数 OpenAI 兼容服务）才退回按出现位置取最靠前的那个。
    """
    low = msg.lower()
    # 结构化字段不一定是字符串：有的兼容服务返回数字 code（如 1001），直接 .strip() 会在处理原始错误时再抛一次
    code = str(getattr(e, "code", None) or "").strip().lower()
    if code != "unsupported_parameter" and not any(k in low for k in _UNSUPPORTED_MARKERS):
        return ""
    param = str(getattr(e, "param", None) or "").strip()
    if param in TOKEN_PARAMS:
        return param
    hits = [(low.index(p), p) for p in TOKEN_PARAMS if p in low]
    return min(hits)[1] if hits else ""


def _token_param_hint(param: str, tcfg: TraderCfg) -> str:
    """参数不被支持时，指向能真正改变请求的那一项配置。"""
    if param == "max_tokens":
        if tcfg.max_tokens is not None:
            # 两者不能同时配（TraderCfg._check_output_token_overrides），照"再加一个"去改会卡在配置校验上
            return (f"该模型不支持 max_tokens 参数（400）：把交易员 {tcfg.id} 的 max_tokens 换成 max_completion_tokens"
                    f"（两者不能同时配），调小输出上限没有用")
        return (f"该模型不支持 max_tokens 参数（400）：给交易员 {tcfg.id} 配上 max_completion_tokens"
                f"（新版 OpenAI 系模型只认这个），调小输出上限没有用")
    if param == "max_completion_tokens":
        return (f"该模型不支持 max_completion_tokens 参数（400）：删掉交易员 {tcfg.id} 的 max_completion_tokens，"
                f"改用 max_tokens")
    return (f"该模型不支持 max_output_tokens 参数（400）：把交易员 {tcfg.id} 的 api_style 改成 chat，"
            f"该 base_url 多半不是 Responses API")


def _output_limit_setting(tcfg: TraderCfg, lcfg: LlmCfg) -> tuple[str, int]:
    """该交易员实际生效的输出上限：(配置项名称, 值)。

    提示里必须写生效的那一项：交易员级 `max_completion_tokens`（chat 路由）或 `max_tokens` 覆盖了全局 `llm.max_tokens` 时，
    让用户去改 `llm.max_tokens` 不会改变请求。与 LlmTrader._chat_output_limit / _effective_max_tokens 的取值顺序一致。
    """
    if tcfg.api_style != "responses" and getattr(tcfg, "max_completion_tokens", None) is not None:
        return f"交易员 {tcfg.id} 的 max_completion_tokens", tcfg.max_completion_tokens
    if getattr(tcfg, "max_tokens", None) is not None:
        return f"交易员 {tcfg.id} 的 max_tokens", tcfg.max_tokens
    return "llm.max_tokens", lcfg.max_tokens


def classify_error(e: Exception, tcfg: TraderCfg, lcfg: LlmCfg) -> tuple[str, bool]:
    """把 SDK 异常翻译成看板上能直接照做的中文提示，并判断是否值得重试。返回 (提示, 可重试)。

    Key 无效、余额不足、模型名错误这类错误重试也不会好，直接把原因交给看板，不拖慢本轮。
    """
    low = str(e).lower()
    # 状态码有两种放法：openai 的异常放在 `status_code` 上，httpx 的 HTTPStatusError 放在
    # `response.status_code` 上。Jev 席位走 httpx，两种都要认——否则它的 401/404 会被当成
    # "没有状态码"一路重试，看板上也只剩一行英文原文。
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "response", None), "status_code", None)
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx 是运行依赖
        httpx = None
    if httpx is not None:
        if isinstance(e, httpx.TimeoutException):
            return f"请求超时（llm.timeout_sec={lcfg.timeout_sec:g}）：模型响应过慢，可增大 timeout_sec", True
        if isinstance(e, httpx.ConnectError):
            return f"无法连接 {tcfg.base_url or 'api.openai.com'}：检查网络、代理或证书", True
    try:
        import openai
    except ImportError:  # pragma: no cover - openai 是运行依赖
        return "", True
    if isinstance(e, openai.APITimeoutError):
        return f"请求超时（llm.timeout_sec={lcfg.timeout_sec:g}）：模型响应过慢，可增大 timeout_sec", True
    if isinstance(e, openai.APIConnectionError):
        return f"无法连接 {tcfg.base_url or 'api.openai.com'}：检查网络、代理或证书", True
    # 额度耗尽要排在状态码之前：它常以 429 返回，而 429 的通用提示是"稍后自动重试"——
    # 充值之前重试永远不会成功，看板上那句话只会把人往错的方向带。
    if is_credit_exhausted(e):
        return CREDIT_EXHAUSTED_HINT, False
    if status == 401:
        return "API Key 无效或未授权（401）：检查 api_key，或 api_key_env 指向的环境变量是否已设置", False
    if status == 402 or ("insufficient" in low and "balance" in low):
        return "账户余额不足（402）：请到该模型的开放平台充值后再试", False
    if status == 403:
        return "无权访问（403）：Key 没有该模型的权限，或所在地区/网络被拒绝", False
    if status == 404:
        return f"接口或模型不存在（404）：检查 base_url（{tcfg.base_url}）与 model（{tcfg.model}）", False
    if status == 429:
        return "请求过于频繁或额度用尽（429）：稍后自动重试；持续出现请检查套餐额度", True
    if status == 400:
        param = _rejected_token_param(e, str(e))
        if param:
            return _token_param_hint(param, tcfg), False
        if "max_tokens" in low or "max_completion_tokens" in low or "max_output_tokens" in low:
            setting, value = _output_limit_setting(tcfg, lcfg)
            return f"输出上限 {setting}={value} 超出该模型允许范围（400）：调小它", False
        if "model" in low and any(k in low for k in ("not exist", "not found", "does not exist", "invalid", "无效", "不存在")):
            return f"模型名无效（400）：{tcfg.model}", False
        return "请求参数被拒绝（400）：见后面的服务端原文", False
    if status is not None and status >= 500:
        return f"服务端错误（{status}）：稍后自动重试", True
    return "", True


@dataclass(frozen=True)
class _CallResult:
    text: str
    # 本轮所有付费调用的合计（计费/看板口径）。供应商没报用量就是 None，绝不冒充成 0。
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    finish_reason: str
    output_limit: int
    output_limit_name: str
    refusal: str = ""   # message.refusal：安全拒答走独立字段，content 为空但不是"偶发空回复"
    # 最后一次调用自己的输出 token。截断判定必须用它：output_limit 是**每次调用**的上限，
    # 拿两次调用的合计去比，会把两次正常完成的短回复误判成"被截断"。
    last_tokens_out: Optional[int] = None
    # 这次 _call 里是否已经为 JSON 模式的空回复多发过一次普通模式请求。轮超时预算只为整个
    # decide() 循环预留一次这样的回退，所以 decide() 用它决定后续尝试还能不能再回退。
    empty_fallback_used: bool = False


class LlmTrader(Trader):
    mode = "llm"

    def __init__(self, tcfg: TraderCfg, lcfg: LlmCfg, stop_event: Optional[threading.Event] = None):
        self.id = tcfg.id
        self.name = tcfg.name
        self.tcfg = tcfg
        self.lcfg = lcfg
        self._client = None
        self._json_mode_ok = lcfg.json_mode
        self._stop_event = stop_event

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.tcfg.resolved_api_key(), base_url=self.tcfg.base_url or None,
                                  timeout=self.lcfg.timeout_sec, max_retries=0)
        return self._client

    def _effective_max_tokens(self) -> int:
        return self.tcfg.max_tokens if self.tcfg.max_tokens is not None else self.lcfg.max_tokens

    def _effective_max_retries(self) -> int:
        return self.tcfg.max_retries if self.tcfg.max_retries is not None else self.lcfg.max_retries

    @staticmethod
    def retry_backoff_seconds(retries: int) -> int:
        """Return the exact maximum total outer-retry sleep for ``retries``."""
        if retries <= 0:
            return 0
        if retries <= 3:
            return (1 << retries) - 1
        return 8 * retries - 17

    def decision_timeout_budget_sec(self) -> float:
        """Return an upper bound for one serial, non-overlapping ``decide()`` loop.

        ``decide()`` makes at most ``retries + 1`` outer attempts.  A Chat
        Completions attempt may add one JSON-mode compatibility fallback request,
        but that fallback costs one extra request for the whole loop, not one per
        attempt: a rejected ``response_format`` clears ``_json_mode_ok``, and the
        empty-reply fallback is allowed once per ``decide()`` loop (see
        ``allow_empty_fallback``).  Reserving exactly that many requests keeps the engine from
        writing a synthetic timeout before this worker's configured policy is
        exhausted, without inflating the round deadline past the decision interval.

        The bound covers only that one call sequence, and only while each request
        honours the configured client timeout and no other work runs concurrently
        on this trader.  It is not a process-wide billing or cancellation bound:
        an abandoned request may keep running and billing after the budget elapses.
        """
        retries = self._effective_max_retries()
        requests = retries + 1
        if self.tcfg.api_style == "chat" and self._json_mode_ok:
            requests += 1
        return requests * self.lcfg.timeout_sec + self.retry_backoff_seconds(retries)

    def _chat_output_limit(self) -> tuple[str, int]:
        if self.tcfg.max_completion_tokens is not None:
            return "max_completion_tokens", self.tcfg.max_completion_tokens
        return "max_tokens", self._effective_max_tokens()

    def _effective_temperature(self) -> float:
        """Use an optional trader override without changing other models' global default."""
        return self.tcfg.temperature if self.tcfg.temperature is not None else self.lcfg.temperature

    @staticmethod
    def _usage_tokens(usage: Any, field: str) -> Optional[int]:
        """Keep missing provider telemetry as ``None``, never as a false zero."""
        value = getattr(usage, field, None) if usage is not None else None
        if isinstance(value, bool) or value is None:
            return None
        try:
            count = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return count if count >= 0 else None

    @staticmethod
    def _is_credit_exhausted_error(exc: Exception) -> bool:
        return is_credit_exhausted(exc)

    @staticmethod
    def _is_provider_rate_limited_error(exc: Exception) -> bool:
        """Match the documented 429/1302 response without broad text guessing."""
        if str(getattr(exc, "status_code", "")) != "429":
            return False
        code = getattr(exc, "code", None)
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                code = error.get("code", code)
        return str(code) == "1302"

    @staticmethod
    def _is_output_truncated(result: _CallResult, parse_error: str, actions: List) -> bool:
        if not parse_error or actions:
            return False
        return _looks_truncated(result.finish_reason, result.last_tokens_out, result.output_limit)

    @staticmethod
    def _content(resp) -> str:
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return ""
        return getattr(getattr(choices[0], "message", None), "content", None) or ""

    @staticmethod
    def _refusal(resp) -> str:
        """Chat Completions 的结构化拒答。SDK 把它放在 message.refusal，不在 content 里。"""
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return ""
        value = getattr(getattr(choices[0], "message", None), "refusal", None)
        return str(value).strip() if value else ""

    @staticmethod
    def _chat_usage(resp) -> tuple[Optional[int], Optional[int]]:
        usage = getattr(resp, "usage", None)
        return LlmTrader._usage_tokens(usage, "prompt_tokens"), LlmTrader._usage_tokens(usage, "completion_tokens")

    @staticmethod
    def _finish_reason(resp) -> str:
        choices = getattr(resp, "choices", None) or []
        return (getattr(choices[0], "finish_reason", None) or "") if choices else ""

    def _call(self, prompt: str, *, system_prompt: str = SYSTEM_PROMPT, max_tokens: Optional[int] = None,
               max_completion_tokens: Optional[int] = None,
               json_mode: Optional[bool] = None, timeout_sec: Optional[float] = None,
               persist_json_mode: bool = True, allow_empty_fallback: bool = True) -> _CallResult:
        client = self._get_client()
        if self.tcfg.api_style == "responses":
            output_limit = max_tokens if max_tokens is not None else self._effective_max_tokens()
            kwargs = dict(model=self.tcfg.model, instructions=system_prompt, input=prompt,
                          max_output_tokens=output_limit)
            if timeout_sec is not None:
                kwargs["timeout"] = timeout_sec
            if self.tcfg.reasoning_effort is not None:
                kwargs["reasoning"] = {"effort": self.tcfg.reasoning_effort}
            resp = client.responses.create(**kwargs)
            text = getattr(resp, "output_text", "") or ""
            usage = getattr(resp, "usage", None)
            incomplete_details = getattr(resp, "incomplete_details", None)
            finish_reason = getattr(incomplete_details, "reason", "") if getattr(resp, "status", "") == "incomplete" else ""
            output_tokens = self._usage_tokens(usage, "output_tokens")
            # 全部用关键字传：`_CallResult` 中间新增字段时，位置参数会把 token 数悄悄塞进别的字段
            return _CallResult(text=text, tokens_in=self._usage_tokens(usage, "input_tokens"),
                               tokens_out=output_tokens, finish_reason=finish_reason or "",
                               output_limit=output_limit, output_limit_name="max_output_tokens",
                               refusal=_responses_refusal(resp), last_tokens_out=output_tokens)
        limit_name, output_limit = self._chat_output_limit()
        if max_completion_tokens is not None:
            limit_name, output_limit = "max_completion_tokens", max_completion_tokens
        elif max_tokens is not None:
            output_limit = max_tokens
        kwargs = dict(model=self.tcfg.model, temperature=self._effective_temperature(),
                      messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}])
        if timeout_sec is not None:
            kwargs["timeout"] = timeout_sec
        kwargs[limit_name] = output_limit
        if self.tcfg.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.tcfg.reasoning_effort
        use_json_mode = self._json_mode_ok if json_mode is None else (json_mode and self._json_mode_ok)
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            if self._is_credit_exhausted_error(e):
                raise
            # 判定集中在 _json_mode_rejected 一处（与 classify_error 同一条约定：同一批异常
            # 元数据只判一次）；use_json_mode 才是"这次真的发了 response_format"的前置条件。
            if use_json_mode and "response_format" in kwargs and _json_mode_rejected(str(e)):
                log.info("%s 不支持 json_object，回退普通模式", self.id)
                # A research endpoint may reject JSON mode even though the
                # contest model endpoint accepts it.  Do not let an auxiliary
                # debate downgrade future executable decisions.
                if persist_json_mode:
                    self._json_mode_ok = False
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            else:
                raise
        text = self._content(resp)
        finish_reason = self._finish_reason(resp)
        refusal = self._refusal(resp)
        tokens_in, tokens_out = self._chat_usage(resp)
        last_tokens_out = tokens_out
        # 空内容不一定是 JSON 模式的偶发问题：输出上限耗尽（推理模型把预算花在思考上）会给出空内容，
        # 内容安全拦截（finish_reason=content_filter）也会。这两种再要一次只会重复同样的结果，
        # 白花一次钱，还会用第二次响应覆盖掉第一次的 finish_reason / usage，让上层给不出对症的提示。
        # 所以按"模型正常说完了"白名单放行，而不是排除已知的几种异常原因。
        empty_fallback_used = False
        if (allow_empty_fallback and not text.strip() and "response_format" in kwargs and not refusal
                and finish_reason.casefold() in NORMAL_FINISH_REASONS
                and not _looks_truncated(finish_reason, tokens_out, output_limit)):
            # DeepSeek 等在 JSON 模式下偶发返回空内容（官方文档列为已知问题）：本次改用普通模式再要一次，
            # 不永久关闭 JSON 模式——空内容不是"不支持"。但这一次回退是整个 decide() 循环里唯一的一次：
            # 轮超时预算只为它预留了一个请求，每次外层重试都回退会让请求数超出预算，引擎会先写下
            # 一个合成超时，而已经付费的调用还在跑、结果却被丢掉。
            log.info("%s JSON 模式返回空内容，本次改用普通模式重试", self.id)
            kwargs.pop("response_format", None)
            empty_fallback_used = True
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                # 第一次调用已经成功并计费了，只是回复为空。这次回退失败不该让那笔开销凭空消失，
                # 把它挂在异常上带给上层如实记账（异常本身原样抛出，错误分类不受影响）。
                e.billed_tokens = (tokens_in, tokens_out)
                e.empty_fallback_used = True
                raise
            text = self._content(resp)
            finish_reason = self._finish_reason(resp)
            refusal = self._refusal(resp)
            # 这一路走下来是**两次付费调用**：第一次的用量不能被第二次的覆盖，
            # 否则落库的 Decision 与看板上的 token 统计会少算一整次请求的钱。
            retry_in, retry_out = self._chat_usage(resp)
            tokens_in, tokens_out = _add_tokens(tokens_in, retry_in), _add_tokens(tokens_out, retry_out)
            last_tokens_out = retry_out
        return _CallResult(text=text, tokens_in=tokens_in, tokens_out=tokens_out,
                           finish_reason=finish_reason, output_limit=output_limit,
                           output_limit_name=limit_name, refusal=refusal,
                           last_tokens_out=last_tokens_out, empty_fallback_used=empty_fallback_used)

    def research(self, system_prompt: str, prompt: str, max_tokens: int, *, timeout_sec: Optional[float] = None) -> _CallResult:
        """Run a bounded structured research call without invoking trading prompts.

        The caller owns schema validation, retries, and persistence.  Keeping this
        primitive separate prevents a research debate from consuming the trader's
        decision breaker or producing an ActionRequest.
        """
        if self._stop_event is not None and self._stop_event.is_set():
            raise RuntimeError("Engine is stopping")
        # Use a strict prompt plus local parser instead of response_format.  Some
        # compatible providers reject that option only after a long request;
        # retrying a fallback could overrun the debate stage deadline.
        return self._call(prompt, system_prompt=system_prompt, max_tokens=max_tokens,
                          max_completion_tokens=self.tcfg.research_max_completion_tokens,
                          json_mode=False, timeout_sec=timeout_sec,
                          persist_json_mode=False)

    def decide(self, snap: MarketSnapshot, acc: AccountView, rules_text: str) -> Decision:
        prompt = build_prompt(snap, acc, rules_text)
        d = Decision(trader_id=self.id, round_no=snap.round_no, ts=time.time(), prompt=prompt, mode="llm")
        t0 = time.time()
        last_err = ""
        # 本轮所有已计费调用的合计，含失败尝试里已经发生的那些；供应商不报用量就保持 None，不冒充成 0
        billed_in: Optional[int] = None
        billed_out: Optional[int] = None
        max_retries = self._effective_max_retries()
        # JSON 空回复的普通模式回退整个循环只允许一次：decision_timeout_budget_sec() 只为它预留了一个请求。
        empty_fallback_used = False
        for attempt in range(max_retries + 1):
            if self._stop_event is not None and self._stop_event.is_set():
                last_err = "引擎停止，放弃调用"
                break
            try:
                result = self._call(prompt, allow_empty_fallback=not empty_fallback_used)
                empty_fallback_used = empty_fallback_used or result.empty_fallback_used
                d.raw_response = result.text
                billed_in, billed_out = _add_tokens(billed_in, result.tokens_in), _add_tokens(billed_out, result.tokens_out)
                d.tokens_in, d.tokens_out = billed_in, billed_out
                analysis, actions, err = parse_response(result.text, list(snap.symbols.keys()))
                d.analysis = analysis
                d.actions = actions
                if err and not actions:
                    if self._is_output_truncated(result, err, actions):
                        # 同样的 prompt 再要一次仍会被截断：本轮不再重试，把生效的上限配置项写清楚
                        setting, _ = _output_limit_setting(self.tcfg, self.lcfg)
                        last_err = (f"解析失败: {err}（Model output truncated at {result.output_limit_name}={result.output_limit} "
                                    f"before valid JSON：输出被截断，请增大 {setting}）")
                        break
                    refusal_hint = _refusal_hint(result.refusal)
                    if refusal_hint:
                        # 拒答本身就是答案：同样的 prompt 再要一次仍然会被拒
                        last_err = f"{refusal_hint}｜解析失败: {err}"
                        break
                    reason_hint = _finish_reason_hint(result.finish_reason)
                    if reason_hint:
                        # 终止原因本身就是答案：同样的 prompt 再要一次仍然拿不到内容
                        last_err = f"{reason_hint}｜解析失败: {err}"
                        break
                    last_err = f"解析失败: {err}"
                    continue
                d.error = err
                last_err = ""
                break
            except Exception as e:  # noqa: BLE001
                empty_fallback_used = empty_fallback_used or bool(getattr(e, "empty_fallback_used", False))
                billed = getattr(e, "billed_tokens", None)
                if billed:      # 这次尝试里已经发生过计费调用（例如 JSON 空回复之后的回退失败）
                    billed_in, billed_out = _add_tokens(billed_in, billed[0]), _add_tokens(billed_out, billed[1])
                    d.tokens_in, d.tokens_out = billed_in, billed_out
                if self._is_provider_rate_limited_error(e):
                    # 限流不在本次决策里重试：重试只会把本轮拖慢，还可能再触发一次限流。
                    # 计费合计上面已经记过了，所以这条早退不会丢掉已经发生的费用。
                    last_err = "rate_limited: HTTP 429 / provider code 1302; no same-decision retry"
                    log.warning("%s request rate-limited; ending this decision without retry", self.id)
                    break
                hint, retryable = classify_error(e, self.tcfg, self.lcfg)   # 额度耗尽也在里面判，不再单独覆盖
                detail = f"{type(e).__name__}: {str(e)[:300]}"
                last_err = f"{hint}｜{detail}" if hint else detail
                log.warning("%s 第 %d 次调用失败: %s", self.id, attempt + 1, last_err)
                if not retryable:
                    break
                if attempt < max_retries:
                    delay = min(2 ** attempt, 8)
                    if self._stop_event is None:
                        time.sleep(delay)
                    else:
                        self._stop_event.wait(delay)
        if last_err:
            d.error = last_err
            d.actions = []
        d.latency_ms = int((time.time() - t0) * 1000)
        return d


class MockTrader(Trader):
    """测试用：返回固定文本，走与 LLM 相同的解析路径。"""
    mode = "mock"

    def __init__(self, trader_id: str, name: str, response: str):
        self.id, self.name, self.response = trader_id, name, response
        self.calls = 0

    def decide(self, snap: MarketSnapshot, acc: AccountView, rules_text: str) -> Decision:
        self.calls += 1
        prompt = build_prompt(snap, acc, rules_text)
        d = Decision(trader_id=self.id, round_no=snap.round_no, ts=time.time(), prompt=prompt, mode="mock", raw_response=self.response)
        if self.response == "__raise__":
            raise RuntimeError("mock failure")
        if self.response.startswith("__sleep__"):
            time.sleep(float(self.response.split(":")[1]) if ":" in self.response else 3.0)
            self.response_after_sleep = True
            analysis, actions, err = "slept", [], ""
            d.analysis, d.actions, d.error = analysis, actions, err
            return d
        analysis, actions, err = parse_response(self.response, list(snap.symbols.keys()))
        d.analysis, d.actions, d.error = analysis, actions, err
        return d
