"""模拟交易所：市价撮合、手续费、保证金、止损止盈、强平。

数量单位：cn_futures 为"手"，crypto 为"币"。所有盈亏 = 价差 × 数量 × 乘数。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..models import Account, ContractSpec, Position, Trade
from ..public_errors import public_spec_open_refusal
from .contracts import round_to_tick


@dataclass
class ExecResult:
    accepted: bool
    reason: str = ""
    trade: Optional[Trade] = None


class SimExchange:
    def __init__(self, specs: Dict[str, ContractSpec], slippage_ticks: int = 1, min_hold_sec: int = 0,
                 liquidation_risk_ratio: float = 1.0, maintenance_margin_ratio: float = 0.5,
                 max_margin_risk_ratio: Optional[float] = None,
                 target_margin_risk_max_ratio: Optional[float] = None):
        self.specs = specs
        self.slippage_ticks = slippage_ticks
        self.min_hold_sec = min_hold_sec
        self.liq_ratio = liquidation_risk_ratio
        self.maint_ratio = maintenance_margin_ratio
        self.max_margin_risk_ratio = max_margin_risk_ratio
        self.target_margin_risk_max_ratio = target_margin_risk_max_ratio

    def opening_margin_risk_cap(self) -> Optional[float]:
        """Return the safest configured ceiling for risk-increasing fills."""
        caps = [cap for cap in (self.target_margin_risk_max_ratio, self.max_margin_risk_ratio) if cap is not None]
        return min(caps) if caps else None

    # ---- 工具 ----
    def fee(self, spec: ContractSpec, price: float, qty: float) -> float:
        notional = price * qty * spec.multiplier
        f = 0.0
        if spec.fee_per_lot:
            f += spec.fee_per_lot * qty
        if spec.fee_rate:
            f += notional * spec.fee_rate
        return round(f, 4)

    def fill_price(self, spec: ContractSpec, price: float, side: str) -> float:
        slip = self.slippage_ticks * spec.tick
        px = price + slip if side == "buy" else price - slip
        return round_to_tick(max(px, spec.tick), spec.tick)

    def margin_for(self, spec: ContractSpec, price: float, qty: float, leverage: float) -> float:
        notional = price * qty * spec.multiplier
        if spec.is_crypto:
            lev = max(1.0, leverage)
            return notional / lev
        return notional * spec.margin_ratio

    def qty_for_pct(self, spec: ContractSpec, price: float, equity: float, size_pct: float, leverage: float) -> float:
        """按权益比例计算开仓数量。cn: size_pct 指保证金占权益的比例；crypto: 指名义占权益的比例 × 杠杆内。"""
        if not all(math.isfinite(x) for x in (price, equity, size_pct, leverage)) or price <= 0 or equity <= 0 or size_pct <= 0:
            return 0.0
        budget = equity * size_pct
        if spec.is_crypto:
            notional = budget * max(1.0, leverage)
            qty = notional / (price * spec.multiplier)
        else:
            per_lot_margin = price * spec.multiplier * spec.margin_ratio
            qty = budget / per_lot_margin if per_lot_margin > 0 else 0
        step = spec.lot_step or 1.0
        qty = math.floor(qty / step) * step
        return round(qty, 6)

    # ---- 开平仓 ----
    def open(self, acc: Account, spec: ContractSpec, side: str, qty: float, price: float, leverage: float,
             stop_loss: Optional[float], take_profit: Optional[float], prices: Dict[str, float],
             round_no: int, reason: str = "", ts: Optional[float] = None) -> ExecResult:
        ts = ts or time.time()
        # The same boundary as RiskManager, enforced again at the matching layer so
        # a direct caller cannot bypass it: a recovery fallback exists only to close
        # out or replay a fill, and an "unspecified" spec is still carrying dataclass
        # placeholders.  Pricing a new position off either would be fabrication.
        # Checked before sizing, fill, fees and any account mutation.
        if not spec.admits_new_positions:
            return ExecResult(False, public_spec_open_refusal(spec))
        if qty <= 0:
            return ExecResult(False, "数量为 0（资金不足以开 1 手或 size_pct 过小）")
        existing = acc.positions.get(spec.symbol)
        if existing and existing.side != side:
            return ExecResult(False, f"已有{('多' if existing.side=='long' else '空')}头持仓，需先平仓再反向开仓")
        fill = self.fill_price(spec, price, "buy" if side == "long" else "sell")
        margin = self.margin_for(spec, fill, qty, leverage)
        fee = self.fee(spec, fill, qty)
        available = acc.available(prices, self.specs)
        if margin + fee > available:
            return ExecResult(False, f"可用资金不足：需保证金 {margin:,.0f} + 手续费 {fee:,.0f}，可用 {available:,.0f}")
        opening_cap = self.opening_margin_risk_cap()
        if opening_cap is not None:
            # Price slippage and fees can make a nominally valid size breach its
            # normal-operation ceiling at the actual fill.  Use a conservative
            # post-fill equity estimate and keep equality admissible, matching
            # the configured <= threshold.
            fill_impact = abs(fill - price) * qty * spec.multiplier
            projected_equity = acc.equity(prices, self.specs) - fee - fill_impact
            projected_margin = acc.margin_used() + margin
            projected_ratio = projected_margin / projected_equity if projected_equity > 0 else math.inf
            if projected_ratio > opening_cap + 1e-9:
                label = "operating target ceiling" if self.target_margin_risk_max_ratio is not None and opening_cap == self.target_margin_risk_max_ratio else "configured cap"
                return ExecResult(
                    False,
                    f"Projected margin risk {projected_ratio:.1%} exceeds {label} {opening_cap:.1%}",
                )
        acc.cash -= fee
        acc.fees += fee
        if existing:
            total_qty = existing.qty + qty
            existing.avg_price = (existing.avg_price * existing.qty + fill * qty) / total_qty
            existing.qty = total_qty
            existing.margin += margin
            if spec.is_crypto and existing.margin > 0:
                # 分批不同杠杆加仓后记录有效杠杆 = 名义 / 累计保证金
                existing.leverage = round(existing.avg_price * existing.qty * spec.multiplier / existing.margin, 4)
            if stop_loss is not None:
                existing.stop_loss = stop_loss
            if take_profit is not None:
                existing.take_profit = take_profit
            existing.last_price = fill
        else:
            acc.positions[spec.symbol] = Position(
                trader_id=acc.trader_id, symbol=spec.symbol, side=side, qty=qty, avg_price=fill, margin=margin,
                leverage=max(1.0, leverage), stop_loss=stop_loss, take_profit=take_profit, opened_at=ts, last_price=fill,
            )
        trade = Trade(trader_id=acc.trader_id, symbol=spec.symbol, side="buy" if side == "long" else "sell",
                      action=f"open_{side}", qty=qty, price=fill, fee=fee, realized_pnl=0.0, ts=ts, round_no=round_no, reason=reason)
        return ExecResult(True, "成交", trade)

    def close(self, acc: Account, spec: ContractSpec, price: float, round_no: int, qty: Optional[float] = None,
              action: str = "", reason: str = "", ts: Optional[float] = None, force: bool = False) -> ExecResult:
        ts = ts or time.time()
        pos = acc.positions.get(spec.symbol)
        if not pos:
            return ExecResult(False, "无该品种持仓")
        if not force and self.min_hold_sec and ts - pos.opened_at < self.min_hold_sec:
            return ExecResult(False, f"未满最短持仓时间 {self.min_hold_sec}s")
        close_qty = pos.qty if qty is None or qty >= pos.qty else max(qty, 0)
        step = spec.lot_step or 1.0
        close_qty = math.floor(close_qty / step + 1e-9) * step
        if close_qty <= 0:
            return ExecResult(False, "平仓数量为 0")
        fill = self.fill_price(spec, price, "sell" if pos.side == "long" else "buy")
        pnl = pos.unrealized(fill, spec.multiplier) * (close_qty / pos.qty)
        fee = self.fee(spec, fill, close_qty)
        released = pos.margin * (close_qty / pos.qty)
        acc.cash += pnl - fee
        acc.realized_pnl += pnl
        acc.fees += fee
        pos.qty -= close_qty
        pos.margin -= released
        pos.last_price = fill
        if pos.qty <= 1e-9:
            del acc.positions[spec.symbol]
        act = action or f"close_{pos.side}"
        trade = Trade(trader_id=acc.trader_id, symbol=spec.symbol, side="sell" if pos.side == "long" else "buy",
                      action=act, qty=close_qty, price=fill, fee=fee, realized_pnl=round(pnl, 4), ts=ts, round_no=round_no, reason=reason)
        return ExecResult(True, "成交", trade)

    def adjust_sl_tp(self, acc: Account, symbol: str, stop_loss: Optional[float], take_profit: Optional[float]) -> ExecResult:
        pos = acc.positions.get(symbol)
        if not pos:
            return ExecResult(False, "无该品种持仓")
        if stop_loss is not None:
            pos.stop_loss = stop_loss
        if take_profit is not None:
            pos.take_profit = take_profit
        return ExecResult(True, "已更新止损止盈")

    # ---- 盯市 ----
    def _liquidate_all(self, acc: Account, prices: Dict[str, float], round_no: int, reason: str, ts: float,
                       per_symbol: Dict[str, float]) -> List[Trade]:
        """全账户强平：一次平掉全部持仓。本次没有报价的品种按其持久化的最新价平仓——账户估值（Account.unrealized）
        对这些品种用的就是这个价，强平既然按它判定，也按它成交；否则这些持仓留着敞口，强平却已计数、被视为已结算。"""
        trades: List[Trade] = []
        for sym in list(acc.positions.keys()):
            pos = acc.positions[sym]
            px = prices.get(sym)
            why = reason
            if px is None:
                px = pos.last_price or pos.avg_price
                why = f"{reason}（本次无报价，按最后价 {px:g}）"
            r = self.close(acc, self.specs[sym], px, round_no, action="liquidation", reason=why,
                           ts=per_symbol.get(sym, ts), force=True)
            if r.accepted and r.trade:
                trades.append(r.trade)
        return trades

    def mark(self, acc: Account, prices: Dict[str, float], round_no: int, ts: Optional[float] = None,
             ts_by_symbol: Optional[Dict[str, float]] = None) -> List[Trade]:
        """更新持仓最新价，触发止损/止盈/强平，返回产生的成交。

        ts_by_symbol：各品种报价自身的时间戳（回放数据流未对齐时，某品种的触发成交必须记在它自己的行情时刻，
        而不是其他品种更晚的时刻）；缺省品种退回 ts。
        """
        ts = ts or time.time()
        per_symbol = ts_by_symbol or {}
        trades: List[Trade] = []
        for sym in list(acc.positions.keys()):
            pos = acc.positions[sym]
            px = prices.get(sym)
            if px is None:
                continue
            sym_ts = per_symbol.get(sym, ts)
            pos.last_price = px
            spec = self.specs[sym]
            hit = None
            if pos.side == "long":
                if pos.stop_loss is not None and px <= pos.stop_loss:
                    hit = ("stop_loss", "触发止损")
                elif pos.take_profit is not None and px >= pos.take_profit:
                    hit = ("take_profit", "触发止盈")
            else:
                if pos.stop_loss is not None and px >= pos.stop_loss:
                    hit = ("stop_loss", "触发止损")
                elif pos.take_profit is not None and px <= pos.take_profit:
                    hit = ("take_profit", "触发止盈")
            if hit is None and spec.is_crypto:
                loss = -pos.unrealized(px, spec.multiplier)
                if loss >= pos.margin * (1 - self.maint_ratio):
                    hit = ("liquidation", "逐仓保证金耗尽强平")
            if hit:
                r = self.close(acc, spec, px, round_no, action=hit[0], reason=hit[1], ts=sym_ts, force=True)
                if r.accepted and r.trade:
                    trades.append(r.trade)
                    if hit[0] == "liquidation":
                        acc.liquidations += 1
        # 账户级风险度强平（cn 与 crypto 全仓兜底）
        eq = acc.equity(prices, self.specs)
        mu = acc.margin_used()
        # 全账户强平必须一次平掉全部持仓：本 tick 没有报价的品种按其持久化的最新价（估值用的就是它）平仓，
        # 否则强平已计数、引擎视为已结算，而这些持仓还敞着，之后的部分报价 tick 还会再算一次强平
        if acc.positions and eq > 0 and mu / eq >= self.liq_ratio:
            trades += self._liquidate_all(acc, prices, round_no, f"风险度 {mu/eq:.0%} 触发强平", ts, per_symbol)
            acc.liquidations += 1
        elif acc.positions and eq <= 0:
            trades += self._liquidate_all(acc, prices, round_no, "权益归零强平", ts, per_symbol)
            acc.liquidations += 1
        acc.peak_equity = max(acc.peak_equity, acc.equity(prices, self.specs))
        return trades
