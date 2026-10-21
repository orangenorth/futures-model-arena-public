"""离线演示交易员：四套简单规则策略，用于无 API Key 时或沙箱测试。"""
from __future__ import annotations

import time
from typing import List

from ..models import ActionRequest, Decision
from .base import AccountView, MarketSnapshot, Trader


class RuleTrader(Trader):
    mode = "rule"

    def __init__(self, trader_id: str, name: str, style: str = "trend"):
        self.id = trader_id
        self.name = name
        self.style = style

    def decide(self, snap: MarketSnapshot, acc: AccountView, rules_text: str) -> Decision:
        t0 = time.time()
        actions: List[ActionRequest] = []
        held = {p["symbol"]: p for p in acc.positions}
        for sym, v in snap.symbols.items():
            a5, a15, a1h = v.ind_5m, v.ind_15m, v.ind_1h
            px = v.quote.price
            atr = a5.get("atr14") or px * 0.005
            pos = held.get(sym)
            act = None
            if self.style == "trend":
                up = a15.get("ema20") and a1h.get("ema20") and px > a15["ema20"] > a1h["ema20"] and (a5.get("macd_hist") or 0) > 0
                dn = a15.get("ema20") and a1h.get("ema20") and px < a15["ema20"] < a1h["ema20"] and (a5.get("macd_hist") or 0) < 0
                if pos is None:
                    if up:
                        act = ActionRequest(sym, "open_long", 0.15, 2, px - 2 * atr, px + 4 * atr, 0.6, "多周期均线多头排列")
                    elif dn:
                        act = ActionRequest(sym, "open_short", 0.15, 2, px + 2 * atr, px - 4 * atr, 0.6, "多周期均线空头排列")
                else:
                    if pos["side"] == "long" and a15.get("ema20") and px < a15["ema20"]:
                        act = ActionRequest(sym, "close", reason="跌破 15m EMA20")
                    elif pos["side"] == "short" and a15.get("ema20") and px > a15["ema20"]:
                        act = ActionRequest(sym, "close", reason="站上 15m EMA20")
            elif self.style == "meanrev":
                rsi = a5.get("rsi14")
                bu, bl = a5.get("boll_upper"), a5.get("boll_lower")
                if pos is None and rsi is not None and bu and bl:
                    if rsi < 28 and px <= bl:
                        act = ActionRequest(sym, "open_long", 0.12, 2, px - 1.5 * atr, a5.get("boll_mid"), 0.55, "RSI 超卖且触及布林下轨")
                    elif rsi > 72 and px >= bu:
                        act = ActionRequest(sym, "open_short", 0.12, 2, px + 1.5 * atr, a5.get("boll_mid"), 0.55, "RSI 超买且触及布林上轨")
                elif pos is not None and a5.get("boll_mid"):
                    mid = a5["boll_mid"]
                    if (pos["side"] == "long" and px >= mid) or (pos["side"] == "short" and px <= mid):
                        act = ActionRequest(sym, "close", reason="回归布林中轨")
            elif self.style == "breakout":
                hi, lo = a5.get("high20"), a5.get("low20")
                if pos is None and hi and lo:
                    if px >= hi and (a15.get("chg20") or 0) > 0:
                        act = ActionRequest(sym, "open_long", 0.18, 3, px - 2.5 * atr, None, 0.6, "突破 20 根高点")
                    elif px <= lo and (a15.get("chg20") or 0) < 0:
                        act = ActionRequest(sym, "open_short", 0.18, 3, px + 2.5 * atr, None, 0.6, "跌破 20 根低点")
                elif pos is not None:
                    # 移动止损：跟随 2.5 ATR
                    if pos["side"] == "long":
                        new_sl = px - 2.5 * atr
                        if pos.get("stop_loss") is None or new_sl > pos["stop_loss"]:
                            act = ActionRequest(sym, "adjust_sl_tp", stop_loss=new_sl, reason="上移止损")
                    else:
                        new_sl = px + 2.5 * atr
                        if pos.get("stop_loss") is None or new_sl < pos["stop_loss"]:
                            act = ActionRequest(sym, "adjust_sl_tp", stop_loss=new_sl, reason="下移止损")
            else:  # conservative
                rsi1h = a1h.get("rsi14")
                if pos is None and len(held) < 2 and rsi1h is not None and a1h.get("ema60"):
                    if px > a1h["ema60"] and 40 < rsi1h < 60 and (a15.get("chg20") or 0) > 0.2:
                        act = ActionRequest(sym, "open_long", 0.08, 1, px - 1.5 * atr, px + 3 * atr, 0.5, "1h 趋势向上且 RSI 中性")
                    elif px < a1h["ema60"] and 40 < rsi1h < 60 and (a15.get("chg20") or 0) < -0.2:
                        act = ActionRequest(sym, "open_short", 0.08, 1, px + 1.5 * atr, px - 3 * atr, 0.5, "1h 趋势向下且 RSI 中性")
                elif pos is not None and pos["hold_min"] > 240:
                    act = ActionRequest(sym, "close", reason="持仓超过 4 小时，落袋")
            if act:
                actions.append(act)
        d = Decision(trader_id=self.id, round_no=snap.round_no, ts=time.time(), mode="rule",
                     analysis=f"[演示策略:{self.style}] 生成 {len(actions)} 个动作", actions=actions,
                     latency_ms=int((time.time() - t0) * 1000))
        return d
