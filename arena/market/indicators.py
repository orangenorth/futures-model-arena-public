"""纯 Python 技术指标。输入为收盘价列表（旧→新）。"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..models import Bar


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def ema_series(values: Sequence[float], n: int) -> List[float]:
    if not values or n <= 0:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return ema_series(values, n)[-1]


def rsi(values: Sequence[float], n: int = 14) -> Optional[float]:
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[Dict[str, float]]:
    if len(values) < slow + signal:
        return None
    ef = ema_series(values, fast)
    es = ema_series(values, slow)
    diff = [a - b for a, b in zip(ef, es)]
    dea = ema_series(diff, signal)
    return {"dif": diff[-1], "dea": dea[-1], "hist": (diff[-1] - dea[-1]) * 2}


def atr(bars: Sequence[Bar], n: int = 14) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def bollinger(values: Sequence[float], n: int = 20, k: float = 2.0) -> Optional[Dict[str, float]]:
    if len(values) < n:
        return None
    window = values[-n:]
    m = sum(window) / n
    var = sum((v - m) ** 2 for v in window) / n
    sd = var ** 0.5
    return {"mid": m, "upper": m + k * sd, "lower": m - k * sd, "width": (2 * k * sd / m) if m else 0.0}


def pct_change(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) <= n or values[-1 - n] == 0:
        return None
    return (values[-1] / values[-1 - n] - 1) * 100


def summarize(bars: Sequence[Bar]) -> Dict[str, Optional[float]]:
    """对一组 bar 输出一份指标摘要，供 prompt 与规则策略使用。"""
    closes = [b.close for b in bars]
    out: Dict[str, Optional[float]] = {
        "last": closes[-1] if closes else None,
        "ema5": ema(closes, 5), "ema20": ema(closes, 20), "ema60": ema(closes, 60),
        "rsi14": rsi(closes, 14),
        "atr14": atr(bars, 14),
        "chg5": pct_change(closes, 5), "chg20": pct_change(closes, 20),
        "high20": max(b.high for b in bars[-20:]) if len(bars) >= 20 else None,
        "low20": min(b.low for b in bars[-20:]) if len(bars) >= 20 else None,
        "vol_mean20": (sum(b.volume for b in bars[-20:]) / 20) if len(bars) >= 20 else None,
    }
    m = macd(closes)
    if m:
        out.update({"macd_dif": m["dif"], "macd_dea": m["dea"], "macd_hist": m["hist"]})
    else:
        out.update({"macd_dif": None, "macd_dea": None, "macd_hist": None})
    b = bollinger(closes)
    if b:
        out.update({"boll_upper": b["upper"], "boll_lower": b["lower"], "boll_mid": b["mid"]})
    else:
        out.update({"boll_upper": None, "boll_lower": None, "boll_mid": None})
    return out


def resample(bars: List[Bar], seconds: int) -> List[Bar]:
    """按**时间**把 bar 重采样到目标周期，而不是按根数分块。

    按根数分块只在"每根都是 1 分钟且一分钟不落"时才等于目标周期。实际上两条都不成立：
    `bar_seconds` 可配（设成 300 时"5 根"就是 25 分钟而不是 5 分钟），而且没有成交的分钟
    根本不会产生 bar，日盘夜盘之间还隔着好几个小时——按根数分出来的"1 小时"可以横跨大半天。
    那样喂给指标的 OHLC 是贴错标签的数据，模型据此得出的结论无从谈起。

    按时间戳分桶则天然正确：缺失的分钟只是让那个桶变薄或不存在，时段间断自然落在不同桶里。
    """
    out: List[Bar] = []
    for bar in bars:
        bucket = bar.ts - (bar.ts % seconds)
        if out and out[-1].ts == bucket:
            last = out[-1]
            last.high = max(last.high, bar.high)
            last.low = min(last.low, bar.low)
            last.close = bar.close
            last.volume += bar.volume
            last.hold = bar.hold
        else:
            out.append(Bar(ts=bucket, open=bar.open, high=bar.high, low=bar.low,
                           close=bar.close, volume=bar.volume, hold=bar.hold))
    return out
