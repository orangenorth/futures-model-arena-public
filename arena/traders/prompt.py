"""统一 prompt 模板与模型输出解析。四个模型使用完全相同的模板。"""
from __future__ import annotations

import json
import math
import re
from typing import List, Optional, Tuple

from ..config import RiskCfg
from ..models import ActionRequest
from .base import AccountView, MarketSnapshot

SYSTEM_PROMPT = (
    "你是一名参加模拟竞赛的期货交易员，目标是在控制回撤的前提下最大化账户权益。"
    "你只能根据给定的行情与账户信息做决策，并且必须严格按 JSON 格式输出，不要输出任何其他文字。"
)

ACTIONS_DOC = """可用动作（每个品种最多一个动作；未提及的品种视为 hold）：
- open_long / open_short：开仓或加仓。size_pct = 本次占用的权益比例（0~max_size_pct），必须给出 stop_loss（可选 take_profit）。
- close：全部平仓。
- adjust_sl_tp：只修改现有持仓的 stop_loss / take_profit。
- hold：不操作。
crypto 市场可给 leverage（1~max_leverage）；国内期货忽略 leverage。
"""

OUTPUT_SCHEMA = """输出 JSON 格式（严格遵守，数字不要带引号）：
{
  "analysis": "不超过 120 字的整体判断",
  "actions": [
    {"symbol": "RB0", "action": "open_long", "size_pct": 0.15, "leverage": 1, "stop_loss": 3350, "take_profit": 3520, "confidence": 0.65, "reason": "简短理由"}
  ]
}
"""


def _f(v: Optional[float], nd: int = 2) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.{max(nd-2,0)}f}"
    return f"{v:.{nd}f}"


def rules_text(cfg: RiskCfg, market: str, currency: str, initial_cash: float, interval_sec: int) -> str:
    lev = f"杠杆上限 {cfg.max_leverage:g}x；" if market == "crypto" else "按交易所保证金比例交易，无额外杠杆；"
    risk_target = ""
    if cfg.target_margin_risk_min_ratio is not None and cfg.target_margin_risk_max_ratio is not None:
        midpoint = (cfg.target_margin_risk_min_ratio + cfg.target_margin_risk_max_ratio) / 2
        risk_target = (
            f"常规总风险度目标为 {cfg.target_margin_risk_min_ratio:.0%}–{cfg.target_margin_risk_max_ratio:.0%}："
            "充分证据至少包括 1h 与 15m 方向一致、5m 有同向入场确认、报价新鲜且止损可定义；"
            f"仅在满足这些条件时，才可通过开仓、加仓或调仓把风险度靠近约 {midpoint:.0%}；"
            "没有有效信号时允许 hold，严禁为凑目标仓位强开；"
            f"风险度达到目标上沿 {cfg.target_margin_risk_max_ratio:.0%} 后不得继续增加风险，并应评估降风险；"
        )
    return (
        f"竞赛规则：起始资金 {initial_cash:,.0f} {currency}；每 {interval_sec//60} 分钟决策一次；"
        f"市价成交并扣除手续费与滑点；{lev}"
        f"最多同时持有 {cfg.max_positions} 个品种；单次开仓 size_pct ≤ {cfg.max_size_pct:.2f}；"
        f"单品种占用 ≤ 权益的 {cfg.max_notional_pct:.0%}；开仓必须带止损（缺失时引擎按 {cfg.auto_stop_atr_mult:g}×ATR 自动补）；"
        f"同一品种不能同时持有多空；"
        + risk_target
        + (f"新策略总风险度（保证金/权益）不得超过 {cfg.max_margin_risk_ratio:.0%}；" if cfg.max_margin_risk_ratio is not None else "")
        + f"风险度达 {cfg.liquidation_risk_ratio:.0%} 将被强平。"
    )


def build_prompt(snap: MarketSnapshot, acc: AccountView, rules: str) -> str:
    lines: List[str] = []
    lines.append(rules)
    lines.append("")
    lines.append(f"【行情快照】第 {snap.round_no} 轮，市场：{snap.market}")
    lines.append("品种 | 最新价 | 涨跌% | 5m:EMA5/EMA20/RSI14/ATR14/MACD柱 | 15m:EMA20/RSI14/涨跌20根% | 1h:EMA20/EMA60/RSI14 | 20根高/低 | 资金费率")
    for s, v in snap.symbols.items():
        a, b, c = v.ind_5m, v.ind_15m, v.ind_1h
        fr = f"{v.quote.funding_rate*100:.4f}%" if v.quote.funding_rate is not None else "-"
        lines.append(
            f"{s}({v.spec.name}) | {_f(v.quote.price)} | {_f(v.quote.change_pct)}% | "
            f"{_f(a.get('ema5'))}/{_f(a.get('ema20'))}/{_f(a.get('rsi14'),1)}/{_f(a.get('atr14'))}/{_f(a.get('macd_hist'),3)} | "
            f"{_f(b.get('ema20'))}/{_f(b.get('rsi14'),1)}/{_f(b.get('chg20'))}% | "
            f"{_f(c.get('ema20'))}/{_f(c.get('ema60'))}/{_f(c.get('rsi14'),1)} | "
            f"{_f(a.get('high20'))}/{_f(a.get('low20'))} | {fr}"
        )
        closes = [b_.close for b_ in v.bars_5m[-12:]]
        if closes:
            lines.append(f"  近12根5m收盘: {', '.join(_f(x) for x in closes)}")
    lines.append("")
    lines.append("【账户状态】")
    current_risk = (acc.margin_used / acc.equity) if acc.equity > 0 else None
    current_risk_label = f"{current_risk:.1%}" if current_risk is not None else "未知（权益非正）"
    lines.append(
        f"权益 {acc.equity:,.0f} | 现金 {acc.cash:,.0f} | 可用 {acc.available:,.0f} | 占用保证金 {acc.margin_used:,.0f} | "
        f"当前组合风险度（保证金/权益）{current_risk_label} | 已实现盈亏 {acc.realized_pnl:,.0f} | "
        f"累计手续费 {acc.fees:,.0f} | 总收益率 {(acc.equity/acc.initial_cash-1)*100:+.2f}%"
    )
    if acc.positions:
        lines.append("当前持仓：")
        for p in acc.positions:
            lines.append(
                f"- {p['symbol']} {'多' if p['side']=='long' else '空'} {p['qty']:g} @ {_f(p['avg_price'])} 现价 {_f(p['last'])} "
                f"浮盈 {p['unrealized']:+,.0f} 保证金 {p['margin']:,.0f} 止损 {_f(p.get('stop_loss'))} 止盈 {_f(p.get('take_profit'))} 持仓 {p['hold_min']:.0f} 分钟"
            )
    else:
        lines.append("当前无持仓。")
    if acc.recent_trades:
        lines.append("最近成交：")
        for t in acc.recent_trades[:8]:
            lines.append(f"- 第{t['round_no']}轮 {t['symbol']} {t['action']} {t['qty']:g} @ {_f(t['price'])} 盈亏 {t['realized_pnl']:+,.0f} {t.get('reason','')[:40]}")
    if acc.last_decision_summary:
        lines.append(f"上一轮决策：{acc.last_decision_summary}")
    lines.append("")
    lines.append(ACTIONS_DOC)
    lines.append(OUTPUT_SCHEMA)
    lines.append("请给出本轮决策。")
    return "\n".join(lines)


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


_CONTRACT_SUFFIXES = {"SWAP", "PERP", "PERPETUAL", "FUTURES", "FUTURE"}


def _symbol_key(sym: str) -> Tuple[str, bool]:
    """归一化的品种键，返回 (key, is_pair)。

    - 国内品种：字母代码。RB2510 / rb0 / RB → ("RB", False)；IF0 → ("IF", False)，不会与 I0 混淆。
    - 加密合约：基础币 + 计价币的完整交易对（合约类型后缀 SWAP/PERP 忽略）。
      BTC-USDT-SWAP / BTC/USDT / btcusdt → ("BTCUSDT", ...)；BTC-USD-SWAP → ("BTCUSD", True)，不会映射到 USDT 合约。
    """
    parts = [p for p in re.findall(r"[A-Z]+", sym.upper()) if p not in _CONTRACT_SUFFIXES]
    if not parts:
        return "", False
    if len(parts) == 1:
        return parts[0], False
    return "".join(parts), True


def _matches_known(sym: str, known: str) -> bool:
    """模型写的品种是否是已知品种的拼写变体：必须是完整的品种代码 / 完整的交易对，不能只是前缀或基础币。"""
    mk, mp = _symbol_key(sym)
    kk, kp = _symbol_key(known)
    if not mk or not kk:
        return False
    if kp:                       # 已知品种是交易对：模型必须给出同一交易对（分隔或连写均可）
        return mk == kk
    return not mp and mk == kk   # 国内品种：字母代码完全相同，且模型没有写成交易对


def parse_response(text: str, known_symbols: List[str]) -> Tuple[str, List[ActionRequest], str]:
    """返回 (analysis, actions, error)。解析失败时 actions 为空并给出 error。"""
    if not text or not text.strip():
        return "", [], "空响应"
    raw = text.strip()
    # 去掉 ```json 包裹
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.M).strip()
    obj = None
    try:
        obj = json.loads(raw)
    except Exception:
        m = _JSON_BLOCK.search(raw)
        if m:
            cand = m.group(0)
            for attempt in (cand, _strip_trailing_commas(cand)):
                try:
                    obj = json.loads(attempt)
                    break
                except Exception:
                    continue
    if not isinstance(obj, dict):
        return "", [], "无法解析为 JSON 对象"
    analysis = str(obj.get("analysis", ""))[:500]
    acts_raw = obj.get("actions", [])
    if isinstance(acts_raw, dict):
        acts_raw = [acts_raw]
    if not isinstance(acts_raw, list):
        return analysis, [], "actions 不是数组"
    actions: List[ActionRequest] = []
    errors: List[str] = []
    seen = set()
    for a in acts_raw:
        if not isinstance(a, dict):
            continue
        sym = str(a.get("symbol", "")).strip().upper()
        # 允许模型写 RB / rb0 / RB2510 / BTC/USDT 这类拼写变体：按完整的品种代码或完整交易对匹配
        # （IF0 不能因为以 I 开头就映射成铁矿石 I0；BTC-USD-SWAP 不能映射成 BTC-USDT-SWAP）
        if sym not in known_symbols:
            cand = [k for k in known_symbols if _matches_known(sym, k)]
            if len(cand) == 1:
                sym = cand[0]
            else:
                errors.append(f"未知品种 {sym}")
                continue
        if sym in seen:
            continue
        seen.add(sym)
        action = str(a.get("action", "hold")).strip().lower()
        alias = {"buy": "open_long", "long": "open_long", "sell": "open_short", "short": "open_short",
                 "close_long": "close", "close_short": "close", "exit": "close", "flat": "close", "none": "hold", "wait": "hold"}
        action = alias.get(action, action)
        # Reject non-numeric, NaN, and Infinity values instead of silently using defaults.
        numeric_fields = {
            "size_pct": a.get("size_pct"),
            "leverage": a.get("leverage"),
            "stop_loss": a.get("stop_loss"),
            "take_profit": a.get("take_profit"),
            "confidence": a.get("confidence"),
        }
        numeric_values = {name: _num(value) for name, value in numeric_fields.items()}
        invalid = [name for name, value in numeric_fields.items()
                   if not _is_missing_number(value) and numeric_values[name] is None]
        if invalid:
            errors.append(f"{sym} 数值字段非法: {', '.join(invalid)}")
            continue
        req = ActionRequest(
            symbol=sym, action=action, size_pct=numeric_values["size_pct"] or 0.0,
            leverage=numeric_values["leverage"] or 1.0,
            stop_loss=numeric_values["stop_loss"], take_profit=numeric_values["take_profit"],
            confidence=numeric_values["confidence"] if numeric_values["confidence"] is not None else 0.5,
            reason=str(a.get("reason", ""))[:300],
        )
        if req.size_pct > 1:  # 模型可能给百分数
            req.size_pct = req.size_pct / 100.0
        if req.size_pct < 0 or req.leverage < 0:
            errors.append(f"{sym} size_pct/leverage 不能为负")
            continue
        actions.append(req)
    return analysis, actions, "; ".join(errors)


def _num(v) -> Optional[float]:
    """转为有限浮点数；缺失、非数字、NaN、±Infinity 均返回 None。"""
    if v is None or v == "" or v == "null" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _is_missing_number(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null"))


def _strip_trailing_commas(s: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", s)
