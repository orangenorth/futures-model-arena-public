"""scripts/check_llm.py：空回复或非 JSON 回复算失败（软件里同样会记为解析失败）；拼错的交易员 id 不能静默通过；
无 Key、接口错误、正常回复各自的输出与退出码。"""
import importlib.util
from pathlib import Path

import httpx
import openai

from arena.config import AppConfig, TraderCfg
from arena.traders.jev import JevTrader
from arena.traders.llm import LlmTrader, _CallResult

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_llm", ROOT / "scripts" / "check_llm.py")
check_llm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_llm)


def make_cfg(**trader):
    t = dict(id="deepseek", name="DS", base_url="https://api.deepseek.com/v1", model="deepseek-chat", api_key="sk-test-key-000000")
    t.update(trader)
    return AppConfig(traders=[TraderCfg(**t)], base_dir=ROOT)


def run(cfg, ids=(), reply=None, monkeypatch=None):
    lines = []
    if reply is not None:
        monkeypatch.setattr(LlmTrader, "_call", lambda self, prompt: reply if not isinstance(reply, Exception) else (_ for _ in ()).throw(reply))
    code = check_llm.check(cfg, ids, out=lines.append)
    return code, "\n".join(lines)


def result(text, finish="stop"):
    return _CallResult(text, 10, 3, finish, 1500, "max_tokens", 3)


def test_unknown_trader_id_fails_instead_of_passing_silently(monkeypatch):
    code, text = run(make_cfg(), ["definitely-not-a-trader"], reply=result('{"ok": true}'), monkeypatch=monkeypatch)
    assert code == 1 and "未知的交易员 id" in text and "definitely-not-a-trader" in text and "deepseek" in text


def test_empty_or_non_json_reply_is_a_failure(monkeypatch):
    code, text = run(make_cfg(), ["deepseek"], reply=result(""), monkeypatch=monkeypatch)
    assert code == 1 and "空回复" in text and "成功" not in text
    code, text = run(make_cfg(), ["deepseek"], reply=result("抱歉，我无法输出 JSON"), monkeypatch=monkeypatch)
    assert code == 1 and "不是 JSON 对象" in text and "成功" not in text
    code, text = run(make_cfg(), ["deepseek"], reply=result('{"analysis": "太长了', finish="length"), monkeypatch=monkeypatch)
    assert code == 1 and "finish_reason=length" in text


def test_valid_json_reply_passes(monkeypatch):
    code, text = run(make_cfg(), ["deepseek"], reply=result('```json\n{"ok": true}\n```'), monkeypatch=monkeypatch)
    assert code == 0 and "成功" in text and "api_key 字段" in text and "sk-test-key-000000" not in text, "不打印 Key 本身"


def test_key_pasted_into_api_key_env_is_never_echoed(monkeypatch):
    """把 Key 本身误填进 api_key_env 时（配置解析容忍并告警），来源说明不能回显该字段。"""
    secret = "sk-pasted-into-env-field-0000000"
    code, text = run(make_cfg(api_key="", api_key_env=secret), ["deepseek"], reply=result('{"ok": true}'), monkeypatch=monkeypatch)
    assert code == 0 and secret not in text and "api_key_env 字段里直接写了 Key" in text and "sk-pas" in text
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-environment-111111")
    code, text = run(make_cfg(api_key="", api_key_env="DEEPSEEK_API_KEY"), ["deepseek"], reply=result('{"ok": true}'), monkeypatch=monkeypatch)
    assert code == 0 and "环境变量 DEEPSEEK_API_KEY" in text and "sk-from-environment-111111" not in text


def test_missing_key_and_api_error_are_reported(monkeypatch):
    code, text = run(make_cfg(api_key=""), ["deepseek"])
    assert code == 1 and "未配置 Key" in text
    req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    err = openai.APIStatusError("Error code: 402 - {'error': {'message': 'Insufficient Balance'}}",
                                response=httpx.Response(402, request=req), body=None)
    code, text = run(make_cfg(), ["deepseek"], reply=err, monkeypatch=monkeypatch)
    assert code == 1 and "余额不足" in text and "Insufficient Balance" in text


def make_jev_cfg(**trader):
    t = dict(id="jev", name="Jev", provider="jev", base_url="https://jev.example/v1", model="system-one",
             api_key="jev-test-key-000000000")
    t.update(trader)
    return AppConfig(traders=[TraderCfg(**t)], base_dir=ROOT)


def run_jev(cfg, ids, reply, monkeypatch):
    lines = []
    calls = []

    def fake_call(self, state, questions):
        calls.append((state, questions))
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(JevTrader, "_call", fake_call)
    code = check_llm.check(cfg, ids, out=lines.append)
    return code, "\n".join(lines), calls


def test_jev_seat_is_really_probed_instead_of_skipped(monkeypatch):
    """`check_llm.py jev` 原来打印"不是模型接口，跳过"并返回成功：鉴权、端点、响应结构全都查不出来。"""
    reply = ({"probe": {"choice": "ok"}}, 7, 0, '{"answers": {"probe": {"choice": "ok"}}}')
    code, text, calls = run_jev(make_jev_cfg(), ["jev"], reply, monkeypatch)
    assert code == 0 and "成功" in text and "跳过" not in text and "jev-test-key" not in text
    assert len(calls) == 1 and calls[0][1]["probe"]["type"] == "choice", "必须真的走 Jev 的传输层"

    req = httpx.Request("POST", "https://jev.example/v1/systemone")
    err = httpx.HTTPStatusError("401 Unauthorized", request=req, response=httpx.Response(401, request=req))
    code, text, _ = run_jev(make_jev_cfg(), ["jev"], err, monkeypatch)
    assert code == 1 and "失败" in text and "401" in text

    code, text, _ = run_jev(make_jev_cfg(), ["jev"], ({"other": {}}, 1, 0, "{}"), monkeypatch)
    assert code == 1 and "不合契约" in text and "缺失" in text

    code, text, calls = run_jev(make_jev_cfg(base_url=""), ["jev"], reply, monkeypatch)
    assert code == 1 and "未配置 Jev 端点" in text and not calls


def test_an_explicitly_requested_non_model_seat_is_a_failure_not_a_skip():
    lines = []
    cfg = AppConfig(traders=[TraderCfg(id="demo", name="Demo", provider="rule", fallback_rule="trend")], base_dir=ROOT)
    assert check_llm.check(cfg, ["demo"], out=lines.append) == 1
    assert "无法检查" in "\n".join(lines)
    lines.clear()
    assert check_llm.check(cfg, (), out=lines.append) == 0, "没点名时仍然只是跳过"
    assert "跳过" in "\n".join(lines)


def test_jev_probe_answer_must_satisfy_the_decision_contract(monkeypatch):
    """`{"answers": {"probe": null}}` 在正式决策里会被 `_choice()` 拒掉，探活不能把它报成健康。"""
    for bad in ({"probe": None}, {"probe": {}}, {"probe": {"choice": ""}}, {"probe": {"choice": "maybe"}},
                {"probe": {"choice": "ok", "confidence": "bad"}}, {"probe": {"choice": "ok", "confidence": 1.5}}):
        code, text, _ = run_jev(make_jev_cfg(), ["jev"], (bad, 1, 0, "{}"), monkeypatch)
        assert code == 1 and "不合契约" in text and "成功" not in text, bad
    code, text, _ = run_jev(make_jev_cfg(), ["jev"], ({"probe": {"choice": "ok", "confidence": 0.9}}, 1, 0, "{}"), monkeypatch)
    assert code == 0 and "成功" in text and "confidence=0.90" in text
