"""行情源接口。"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Protocol

from ..models import Bar, ContractSpec, Quote


class MarketSource(Protocol):
    name: str

    def specs(self) -> Dict[str, ContractSpec]: ...

    def latest(self, symbols: List[str]) -> Dict[str, Quote]:
        """最新报价（可能为上一分钟 bar 收盘）。缺失的品种不返回。"""
        ...

    def klines(self, symbol: str, period: str, limit: int = 120) -> List[Bar]:
        """period: '1m' | '5m' | '15m' | '1h' | '1d'，旧→新。"""
        ...

    def is_open(self, now: datetime, symbol: Optional[str] = None) -> bool: ...

    def next_open(self, now: datetime) -> Optional[datetime]: ...

    def health(self) -> dict: ...


class SourceError(Exception):
    pass
