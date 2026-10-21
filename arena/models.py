"""核心数据结构。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


def now_ts() -> float:
    return time.time()


@dataclass
class Bar:
    ts: float  # 秒级时间戳（bar 开始时间）
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    hold: float = 0.0  # 持仓量 / 未平仓合约


@dataclass
class Quote:
    symbol: str
    price: float
    ts: float
    change_pct: float = 0.0     # 相对前一日收盘/24h 涨跌幅
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None


# Where a ContractSpec's terms came from.  This records local provenance only and
# is deliberately NOT a statement that anything was checked against exchange
# documents -- "complete and explicit locally" is a much weaker claim than
# "officially verified", and the names here must never be read as the latter.
#
#   unspecified        -- built ad hoc, or from a table row that failed validation.
#                         Its dataclass defaults are placeholders, not terms.
#   local_table        -- every execution term was present and valid in the local
#                         contract table when the row was read.
#   venue              -- derived from the trading venue's own contract rules
#                         (crypto perpetuals), intentionally complete.
#   recovery_fallback  -- invented so a legacy position can be wound down; the
#                         terms are generic, not this product's.
SPEC_ORIGIN_UNSPECIFIED = "unspecified"
SPEC_ORIGIN_LOCAL_TABLE = "local_table"
SPEC_ORIGIN_VENUE = "venue"
SPEC_ORIGIN_RECOVERY_FALLBACK = "recovery_fallback"

# Only these may price a NEW position.  The default is deliberately absent from
# the set: an incomplete spec must fail closed without anyone opting out.
OPEN_ADMITTED_SPEC_ORIGINS = frozenset({SPEC_ORIGIN_LOCAL_TABLE, SPEC_ORIGIN_VENUE})


@dataclass
class ContractSpec:
    symbol: str
    name: str
    exchange: str = ""
    multiplier: float = 1.0      # 每手/每张对应的标的数量
    tick: float = 0.01
    margin_ratio: float = 0.1    # cn: 保证金比例；crypto: 1/max_leverage 的下限
    fee_per_lot: float = 0.0
    fee_rate: float = 0.0        # 按名义比例
    sessions: List[List[str]] = field(default_factory=list)  # [["09:00","10:15"],...]
    lot_step: float = 1.0        # 数量步长：cn 为 1 手，crypto 可为 0.001
    is_crypto: bool = False
    # Defaults to "unspecified" so a directly constructed ContractSpec, and any
    # spec restored from a journal written before this field existed, is refused
    # for new positions rather than trading on the placeholder values above.
    origin: str = SPEC_ORIGIN_UNSPECIFIED

    @property
    def admits_new_positions(self) -> bool:
        """Whether these terms are complete enough to price a new position.

        Execution readiness from local provenance only; it asserts nothing about
        agreement with the exchange's published contract.
        """
        return self.origin in OPEN_ADMITTED_SPEC_ORIGINS

    @property
    def is_recovery_fallback(self) -> bool:
        """True for a spec invented to wind down a legacy position."""
        return self.origin == SPEC_ORIGIN_RECOVERY_FALLBACK


@dataclass
class Position:
    trader_id: str
    symbol: str
    side: str                 # long | short
    qty: float
    avg_price: float
    margin: float
    leverage: float = 1.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    opened_at: float = field(default_factory=now_ts)
    last_price: float = 0.0

    def unrealized(self, price: float, multiplier: float) -> float:
        d = (price - self.avg_price) if self.side == "long" else (self.avg_price - price)
        return d * self.qty * multiplier

    def notional(self, price: float, multiplier: float) -> float:
        return price * self.qty * multiplier

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Account:
    trader_id: str
    cash: float               # 现金（含已实现盈亏，扣手续费）
    initial_cash: float
    realized_pnl: float = 0.0
    fees: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    peak_equity: float = 0.0
    liquidations: int = 0

    def margin_used(self) -> float:
        return sum(p.margin for p in self.positions.values())

    def unrealized(self, prices: Dict[str, float], specs: Dict[str, ContractSpec]) -> float:
        total = 0.0
        for p in self.positions.values():
            px = prices.get(p.symbol, p.last_price or p.avg_price)
            total += p.unrealized(px, specs[p.symbol].multiplier)
        return total

    def equity(self, prices: Dict[str, float], specs: Dict[str, ContractSpec]) -> float:
        return self.cash + self.unrealized(prices, specs)

    def available(self, prices: Dict[str, float], specs: Dict[str, ContractSpec]) -> float:
        return self.equity(prices, specs) - self.margin_used()


@dataclass
class Trade:
    trader_id: str
    symbol: str
    side: str            # buy | sell
    action: str          # open_long | open_short | close_long | close_short | stop_loss | take_profit | liquidation
    qty: float
    price: float
    fee: float
    realized_pnl: float
    ts: float
    round_no: int
    reason: str = ""
    id: Optional[int] = None


@dataclass
class ActionRequest:
    symbol: str
    action: str                  # open_long | open_short | close | adjust_sl_tp | hold
    size_pct: float = 0.0
    leverage: float = 1.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: float = 0.5
    reason: str = ""


@dataclass
class Decision:
    trader_id: str
    round_no: int
    ts: float
    analysis: str = ""
    actions: List[ActionRequest] = field(default_factory=list)
    raw_response: str = ""
    prompt: str = ""
    latency_ms: int = 0
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    error: str = ""
    results: List[dict] = field(default_factory=list)  # 每个 action 的执行结果 {symbol, action, accepted, reason}
    mode: str = "llm"   # llm | jev | rule | mock | skipped


@dataclass
class EquityPoint:
    trader_id: str
    ts: float
    equity: float
    cash: float
    margin_used: float
    unrealized: float
