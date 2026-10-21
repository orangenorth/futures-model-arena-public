"""检查各交易员的模型接口是否可用（排查"软件里某个模型报错"）。

用法：
    python scripts/check_llm.py                      # 检查 config/config.yaml 里全部 LLM 交易员
    python scripts/check_llm.py deepseek             # 只检查 deepseek
    python scripts/check_llm.py --config 路径 ...     # 指定配置文件

对每个交易员发一条最小请求，打印 Key 的来源（脱敏，不打印 Key 本身）、base_url、model、耗时，
以及成功的回复或失败原因（与看板上显示的中文提示一致）。回复必须是可解析的 JSON 对象才算成功——
空回复或非 JSON 在软件里同样会记为解析失败。退出码：全部成功 0，否则 1（含拼错的交易员 id）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arena.config import AppConfig, TraderCfg, load_config  # noqa: E402
from arena.traders.jev import JevTrader  # noqa: E402
from arena.traders.llm import LlmTrader, classify_error  # noqa: E402

PING = '这是连通性测试，不涉及交易。请只输出 JSON：{"ok": true}'
# Jev（System One）不是文本模型接口，探活要走它自己的传输层：同一个端点、同一种问题结构，
# 只是问题本身与交易无关。鉴权失败、端点不存在、响应结构不对都会在这里暴露出来。
JEV_PROBE_STATE = "这是连通性测试，不涉及交易，也没有任何仓位。"
JEV_PROBE_QUESTIONS = {
    "probe": {
        "type": "choice",
        "instructions": "这是连通性测试，请任选其一。",
        "criteria": {"ok": "连通正常", "fail": "连通异常"},
    },
}


def mask(key: str) -> str:
    return f"{key[:6]}…{key[-3:]}" if len(key) > 12 else "***"


def _json_object(text: str) -> Optional[dict]:
    """回复是否是一个 JSON 对象（容忍 ``` 包裹与前后杂文）；不是则返回 None。"""
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    for cand in (raw, m.group(0) if m else ""):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            return obj
    return None


def check(cfg: AppConfig, trader_ids: Iterable[str] = (), out: Callable[[str], None] = print) -> int:
    """检查配置里的交易员（trader_ids 为空则全部）。返回退出码：全部成功 0，否则 1。"""
    wanted = list(trader_ids)
    known = [t.id for t in cfg.traders]
    unknown = [i for i in wanted if i not in known]
    all_ok = True
    if unknown:
        out(f"未知的交易员 id：{', '.join(unknown)}（配置里的 id：{', '.join(known) or '无'}）")
        all_ok = False
    for t in cfg.traders:
        if wanted and t.id not in wanted:
            continue
        if t.provider == "jev":
            if not _check_jev(cfg, t, out):
                all_ok = False
            continue
        if t.provider != "openai_compatible":
            if wanted:
                # 点名要查的席位不能以"跳过"收场：退出码 0 会被当成"配置健康"。
                out(f"== {t.id}（{t.name}）provider={t.provider}：不是可探测的模型接口，无法检查")
                all_ok = False
            else:
                out(f"== {t.id}（{t.name}）provider={t.provider}：不是模型接口，跳过")
            continue
        out(f"== {t.id}（{t.name}） base_url={t.base_url} model={t.model} api_style={t.api_style}")
        key = t.resolved_api_key()
        if not key:
            out(f"   未配置 Key：api_key 为空，环境变量 {t.api_key_env or '(未指定)'} 也未设置 → 软件里会降级为演示策略")
            all_ok = False
            continue
        out(f"   Key 来源：{_key_source(t, key)}（{mask(key)}）")
        trader = LlmTrader(t, cfg.llm)
        t0 = time.time()
        try:
            r = trader._call(PING)
        except Exception as e:  # noqa: BLE001
            hint, _ = classify_error(e, t, cfg.llm)
            out(f"   失败（{time.time() - t0:.1f}s）：{hint or '未识别的错误'}")
            out(f"   原文：{type(e).__name__}: {str(e)[:400]}")
            all_ok = False
            continue
        elapsed = time.time() - t0
        if _json_object(r.text) is None:
            what = "空回复" if not (r.text or "").strip() else "不是 JSON 对象的内容"
            out(f"   失败（{elapsed:.1f}s，tokens {r.tokens_in}/{r.tokens_out}）：模型返回了{what}，软件里会记为解析失败"
                + (f"（finish_reason={r.finish_reason}）" if r.finish_reason else ""))
            out(f"   原文：{(r.text or '').strip()[:200]!r}")
            all_ok = False
            continue
        out(f"   成功（{elapsed:.1f}s，tokens {r.tokens_in}/{r.tokens_out}）：{r.text.strip()[:120]!r}")
    return 0 if all_ok else 1


def _key_source(t: TraderCfg, key: str) -> str:
    env = (t.api_key_env or "").strip()
    if (t.api_key or "").strip():
        return "api_key 字段"
    if env and os.environ.get(env, "").strip() == key:
        return f"环境变量 {env}"
    # resolved_api_key() 容忍把 Key 本身误填进 api_key_env：这里绝不能回显该字段，否则整串 Key 会被打印出来
    return "api_key_env 字段里直接写了 Key（请改到 api_key 字段，且不要提交到仓库）"


def _check_jev(cfg: AppConfig, t: TraderCfg, out: Callable[[str], None]) -> bool:
    """真的向 Jev 端点发一条探活请求；返回是否成功。"""
    out(f"== {t.id}（{t.name}）provider=jev base_url={t.base_url} model={t.model or '(默认)'}")
    if not (t.base_url or "").strip():
        out("   未配置 Jev 端点（base_url）→ 软件里会降级为演示策略")
        return False
    key = t.resolved_api_key()
    if key:
        out(f"   Key 来源：{_key_source(t, key)}（{mask(key)}）")
    else:
        out("   未配置 Key：请求不带 Authorization；端点若要求鉴权，下面会报 401/403")
    trader = JevTrader(t, cfg.llm)
    t0 = time.time()
    try:
        answers, tokens_in, tokens_out, raw = trader._call(JEV_PROBE_STATE, JEV_PROBE_QUESTIONS)
    except Exception as e:  # noqa: BLE001
        hint, _ = classify_error(e, t, cfg.llm)
        out(f"   失败（{time.time() - t0:.1f}s）：{hint or '未识别的错误'}")
        out(f"   原文：{type(e).__name__}: {str(e)[:400]}")
        return False
    elapsed = time.time() - t0
    # 用真实决策同一套契约校验：对象、非空 choice、可选但必须合法的 confidence，且 choice 必须是
    # 我们给出的候选之一。`{"answers": {"probe": null}}` 这类回复在正式决策里会被拒，探活也不能放行。
    try:
        choice, confidence, _probs = trader._choice(answers, "probe")
        if choice not in JEV_PROBE_QUESTIONS["probe"]["criteria"]:
            raise ValueError(f"answers.probe.choice={choice!r} 不在候选里")
    except ValueError as e:
        out(f"   失败（{elapsed:.1f}s，tokens {tokens_in}/{tokens_out}）：探活问题的回答不合契约（{e}），软件里会记为无效回复")
        out(f"   原文：{raw[:200]!r}")
        return False
    shown = f"choice={choice!r}" + (f" confidence={confidence:.2f}" if confidence is not None else "")
    out(f"   成功（{elapsed:.1f}s，tokens {tokens_in}/{tokens_out}）：{shown}")
    return True


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="检查模型接口连通性")
    ap.add_argument("--config", default=None, help="配置文件路径（默认与 run.py 相同）")
    ap.add_argument("traders", nargs="*", help="只检查这些交易员 id；不填则检查全部")
    args = ap.parse_args(argv)
    cfg = load_config(args.config, base_dir=ROOT)
    return check(cfg, args.traders)


if __name__ == "__main__":
    sys.exit(main())
