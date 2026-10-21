"""硬风控：对模型提议的动作做合法性与限额校验，必要时修正（补止损、截断 size）。"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, Optional

from ..config import RiskCfg
from ..models import Account, ActionRequest, ContractSpec
from ..public_errors import public_spec_open_refusal


@dataclass
class RiskVerdict:
    ok: bool
    reason: str = ""
    action: Optional[ActionRequest] = None  # 修正后的动作


class RiskManager:
    def __init__(self, cfg: RiskCfg, specs: Dict[str, ContractSpec]):
        self.cfg = cfg
        self.specs = specs

    def check(self, acc: Account, req: ActionRequest, price: float, prices: Dict[str, float],
              atr: Optional[float] = None) -> RiskVerdict:
        cfg = self.cfg
        spec = self.specs.get(req.symbol)
        if spec is None:
            return RiskVerdict(False, f"未知品种 {req.symbol}")
        # Parsing is the normal entry point, but custom traders/tests can construct ActionRequest directly.
        # Keep the risk boundary finite so no NaN/Infinity can reach sizing, persistence, or matching.
        required_numbers = {"price": price, "size_pct": req.size_pct, "leverage": req.leverage, "confidence": req.confidence}
        for field, value in required_numbers.items():
            if not _is_finite_number(value):
                return RiskVerdict(False, f"{field} 必须是有限数字")
        for field, value in {"stop_loss": req.stop_loss, "take_profit": req.take_profit}.items():
            if value is not None and not _is_finite_number(value):
                return RiskVerdict(False, f"{field} 必须是有限数字")
        if price <= 0:
            return RiskVerdict(False, "价格必须大于 0")
        if atr is not None and (not _is_finite_number(atr) or float(atr) <= 0):
            atr = None
        if req.action == "hold":
            return RiskVerdict(True, "hold", req)
        if req.action == "close":
            if req.symbol not in acc.positions:
                return RiskVerdict(False, "无持仓可平")
            return RiskVerdict(True, "", req)
        if req.action == "adjust_sl_tp":
            pos = acc.positions.get(req.symbol)
            if not pos:
                return RiskVerdict(False, "无持仓可调整")
            fixed = self._validate_sl_tp(pos.side, price, req.stop_loss, req.take_profit)
            if fixed is None:
                return RiskVerdict(False, "止损/止盈方向不合法")
            req.stop_loss, req.take_profit = fixed
            return RiskVerdict(True, "", req)
        if req.action not in ("open_long", "open_short"):
            return RiskVerdict(False, f"不支持的动作 {req.action}")
        # Only a spec whose terms are locally complete may price a new position.
        # A recovery fallback carries generic terms, and an "unspecified" spec is
        # carrying dataclass placeholders, so neither may size an order.  The
        # close and adjust_sl_tp branches above return before this point, so a
        # legacy position can still be wound down.
        if not spec.admits_new_positions:
            return RiskVerdict(False, public_spec_open_refusal(spec))

        side = "long" if req.action == "open_long" else "short"
        existing = acc.positions.get(req.symbol)
        if existing and existing.side != side:
            return RiskVerdict(False, "已有反向持仓，需先平仓")
        if not existing and len(acc.positions) >= cfg.max_positions:
            return RiskVerdict(False, f"持仓品种数已达上限 {cfg.max_positions}")
        if req.size_pct <= 0:
            return RiskVerdict(False, "size_pct 必须大于 0")
        # 杠杆限制
        lev = req.leverage or 1.0
        if spec.is_crypto:
            if lev > cfg.max_leverage:
                req.leverage = cfg.max_leverage
            req.leverage = max(1.0, req.leverage)
        else:
            req.leverage = 1.0
        # 单次仓位上限
        if req.size_pct > cfg.max_size_pct:
            req.size_pct = cfg.max_size_pct
        # 单品种占用上限：首次开仓与加仓都受限；占用按累计保证金计算（兼容不同杠杆分批加仓）
        equity = acc.equity(prices, self.specs)
        if equity <= 0:
            return RiskVerdict(False, "权益为 0")
        # The per-symbol cap alone can still permit several positions to consume
        # almost all account margin.  Apply the normal-operation ceiling before
        # sizing; SimExchange repeats the check after fill, slippage, and fees.
        # The target band is never a reason to manufacture an order: it only
        # limits a risk-increasing action the trader has already proposed.
        opening_cap = cfg.opening_margin_risk_cap
        if opening_cap is not None:
            aggregate_used = acc.margin_used() / equity
            aggregate_room = opening_cap - aggregate_used
            if aggregate_room <= 1e-9:
                label = "operating target ceiling" if cfg.target_margin_risk_max_ratio is not None else "configured cap"
                return RiskVerdict(False, f"Projected aggregate margin risk is already at the configured {label}")
            req.size_pct = min(req.size_pct, aggregate_room)
        used_pct = (existing.margin / equity) if existing else 0.0
        room = cfg.max_notional_pct - used_pct
        if room <= 0.01:
            return RiskVerdict(False, f"该品种占用已达上限 {cfg.max_notional_pct:.0%}")
        req.size_pct = min(req.size_pct, room)
        # 止损处理
        fixed = self._validate_sl_tp(side, price, req.stop_loss, req.take_profit)
        if fixed is None:
            return RiskVerdict(False, "止损/止盈方向不合法（多头止损须低于现价、止盈高于现价；空头相反）")
        req.stop_loss, req.take_profit = fixed
        if req.stop_loss is None and cfg.require_stop_loss:
            dist = (atr or price * 0.01) * cfg.auto_stop_atr_mult
            if side == "long":
                # 波动异常大（ATR×倍数 ≥ 现价）时 price - dist ≤ 0，这样的止损永远不会触发，等于没有保护：
                # 多头自动止损的距离最多为现价的一半，且不低于一个最小变动价位；仍不合法则拒绝开仓
                sl = max(price - dist, price * 0.5, spec.tick)
                if not (0 < sl < price):
                    return RiskVerdict(False, "无法自动生成合法的多头止损（价格过低或波动异常）")
                req.stop_loss = sl
            else:
                req.stop_loss = price + dist
            req.reason = (req.reason + " [引擎自动补止损]").strip()
        return RiskVerdict(True, "", req)

    @staticmethod
    def _validate_sl_tp(side: str, price: float, sl: Optional[float], tp: Optional[float]):
        if sl is not None and sl <= 0:
            sl = None
        if tp is not None and tp <= 0:
            tp = None
        if side == "long":
            if sl is not None and sl >= price:
                return None
            if tp is not None and tp <= price:
                return None
        else:
            if sl is not None and sl <= price:
                return None
            if tp is not None and tp >= price:
                return None
        return sl, tp


def _is_finite_number(value) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
