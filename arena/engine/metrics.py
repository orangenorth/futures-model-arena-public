"""绩效指标。"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence


def total_return(initial: float, equity: float) -> float:
    return (equity / initial - 1) * 100 if initial else 0.0


def max_drawdown(equities: Sequence[float]) -> float:
    """返回最大回撤百分比（正数）。"""
    peak = -math.inf
    mdd = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak * 100)
    return mdd


def sharpe(equities: Sequence[float], periods_per_year: float) -> Optional[float]:
    """基于快照序列的简单收益率计算年化 Sharpe（无风险利率 0）。"""
    if len(equities) < 3:
        return None
    rets = []
    for a, b in zip(equities[:-1], equities[1:]):
        if a > 0:
            rets.append(b / a - 1)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return mean / sd * math.sqrt(periods_per_year)


def trade_stats(trades: Sequence[dict]) -> Dict[str, float]:
    """基于平仓成交（realized_pnl != 0 或 action 为平仓类）计算胜率/盈亏比/持仓统计。"""
    closes = [t for t in trades if t.get("action") in ("close_long", "close_short", "stop_loss", "take_profit", "liquidation")]
    wins = [t["realized_pnl"] for t in closes if t["realized_pnl"] > 0]
    losses = [t["realized_pnl"] for t in closes if t["realized_pnl"] <= 0]
    n = len(closes)
    win_rate = (len(wins) / n * 100) if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = -sum(losses) / len(losses) if losses else 0.0
    profit_factor = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (math.inf if wins else 0.0)
    return {
        "closed_trades": n,
        "total_trades": len(trades),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor if profit_factor != math.inf else 99.0,
        "gross_pnl": sum(t["realized_pnl"] for t in closes),
        "fees": sum(t.get("fee", 0) for t in trades),
    }
