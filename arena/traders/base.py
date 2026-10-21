"""交易员接口与共享快照结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..models import Bar, ContractSpec, Decision, Quote


@dataclass
class SymbolView:
    symbol: str
    spec: ContractSpec
    quote: Quote
    ind_5m: Dict[str, Optional[float]]
    ind_15m: Dict[str, Optional[float]]
    ind_1h: Dict[str, Optional[float]]
    bars_5m: List[Bar] = field(default_factory=list)


@dataclass
class MarketSnapshot:
    ts: float
    round_no: int
    market: str
    currency: str
    symbols: Dict[str, SymbolView]

    @property
    def prices(self) -> Dict[str, float]:
        return {s: v.quote.price for s, v in self.symbols.items()}


@dataclass
class AccountView:
    trader_id: str
    equity: float
    cash: float
    available: float
    margin_used: float
    realized_pnl: float
    fees: float
    initial_cash: float
    positions: List[dict]        # 每项含 symbol, side, qty, avg_price, last, unrealized, margin, stop_loss, take_profit, hold_min
    recent_trades: List[dict]
    last_decision_summary: str
    round_no: int


class Trader:
    id: str
    name: str
    mode: str = "rule"

    def decide(self, snap: MarketSnapshot, acc: AccountView, rules_text: str) -> Decision:  # pragma: no cover - interface
        raise NotImplementedError
