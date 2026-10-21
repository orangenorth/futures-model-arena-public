"""Fixed, non-sensitive projections for provider failures shown in the UI."""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional


PUBLIC_DECISION_FAILURE_MESSAGES = {
    "timeout": "模型请求超时，已停止本轮重试",
    "rate_limited": "模型请求频率或可用额度受限，已停止本轮重试",
    "unknown": "模型调用失败，未展示供应商原始错误",
    "circuit_breaker": "熔断恢复中，暂不发起模型请求",
}

PUBLIC_RUNTIME_EVENT_MESSAGES = {
    "replay_position": "回放位置未能持久化，已暂停前进直到写入成功；详细诊断仅保留在本机日志",
    "mark_persistence": "盯市成交未能持久化，本次触发已回滚，将在下个 tick 按原触发价重试；详细诊断仅保留在本机日志",
    "mark_persistence_without_journal": "盯市成交未能持久化，本次触发已回滚；预写日志也未能写入，触发暂存于内存，将在下个 tick 按原触发价重试；详细诊断仅保留在本机日志",
    "shutdown_persistence": "停止时最终持久化失败；详细诊断仅保留在本机日志",
    "main_loop": "主循环异常；详细诊断仅保留在本机日志",
    "round": "决策轮异常；详细诊断仅保留在本机日志",
    "settlement": "结算异常，详细诊断仅保留在本机日志",
}
PUBLIC_ACTION_EXECUTION_FAILURE = "动作执行失败，已拒绝；详细诊断仅保留在本机日志"
# Shown when a spec is not admitted to price a new position.  Risk and matching
# both project the same wording, so the dashboard reads the same either way.
PUBLIC_SPEC_RECOVERY_ONLY = "{symbol} 不在合约表中（仅按降级规格善后），不接受开仓"
PUBLIC_SPEC_NOT_OPEN_ADMITTED = "{symbol} 的合约条款未通过本地合约表校验，不接受开仓"


def public_spec_open_refusal(spec: Any) -> str:
    """User-facing reason a spec may not open, without leaking internal state."""
    template = PUBLIC_SPEC_RECOVERY_ONLY if getattr(spec, "is_recovery_fallback", False) else PUBLIC_SPEC_NOT_OPEN_ADMITTED
    return template.format(symbol=getattr(spec, "symbol", ""))
PUBLIC_RESET_PERSISTENCE_FAILURE = "重置失败：新竞赛未能写入数据库，旧竞赛保持进行中；详细诊断仅保留在本机日志"
PUBLIC_SOURCE_UNAVAILABLE_MESSAGE = "行情源暂不可用，详细诊断仅保留在本机日志"

_LEGACY_RUNTIME_EVENT_PREFIXES = (
    ("回放位置未能持久化，已暂停前进直到写入成功:", PUBLIC_RUNTIME_EVENT_MESSAGES["replay_position"]),
    ("盯市成交落库失败，本次触发已回滚，下个 tick 按原触发价重试:", PUBLIC_RUNTIME_EVENT_MESSAGES["mark_persistence"]),
    ("停止时最终持久化失败:", PUBLIC_RUNTIME_EVENT_MESSAGES["shutdown_persistence"]),
    ("主循环异常:", PUBLIC_RUNTIME_EVENT_MESSAGES["main_loop"]),
    ("决策轮异常:", PUBLIC_RUNTIME_EVENT_MESSAGES["round"]),
)
_LEGACY_SETTLEMENT_EVENT = re.compile(r"^第\s*(?P<round>\d+)\s*轮结算异常\s*[:：]")


def decision_failure_category(error: str, *, mode: str = "") -> str:
    """Classify an internal decision error without returning any source text."""
    text = (error or "").casefold()
    if mode == "skipped" and ("熔断" in error or "breaker" in text):
        return "circuit_breaker"
    if any(marker in text for marker in ("timeout", "timed out", "超时")):
        return "timeout"
    if any(marker in text for marker in (
        "rate_limited", "rate limit", "credit_balance_exhausted", "no credits remaining",
        "ratelimiterror", "http 429", "error code: 429", "error code 429", "status code: 429",
        "429 rate limit", "provider code 1302", "code 1302", "请求频率", "可用额度",
    )):
        return "rate_limited"
    return "unknown"


def public_decision_failure_message(category: str) -> str:
    """Return the allowlisted Chinese display string for a failure category."""
    return PUBLIC_DECISION_FAILURE_MESSAGES.get(category, PUBLIC_DECISION_FAILURE_MESSAGES["unknown"])


def public_runtime_event_message(message: str) -> Optional[str]:
    """Return a fixed projection for a legacy event that embedded an exception."""
    for prefix, projection in _LEGACY_RUNTIME_EVENT_PREFIXES:
        if message.startswith(prefix):
            return projection
    match = _LEGACY_SETTLEMENT_EVENT.match(message)
    if match:
        return f"第 {match.group('round')} 轮{PUBLIC_RUNTIME_EVENT_MESSAGES['settlement']}"
    return None


def public_execution_result_reason(reason: str) -> str:
    """Replace only the historical runtime-exception result reason."""
    if reason.startswith(("执行异常:", "执行异常：")):
        return PUBLIC_ACTION_EXECUTION_FAILURE
    return reason


def public_source_health(health: Mapping[str, Any]) -> dict:
    """Remove provider diagnostics from the browser-facing market-health view."""
    result = dict(health)
    if not result.get("ok"):
        result["detail"] = PUBLIC_SOURCE_UNAVAILABLE_MESSAGE
    elif result.get("source") == "replay" and result.get("detail") == "合成行情":
        result["detail"] = "合成行情"
    else:
        result.pop("detail", None)
    return result
