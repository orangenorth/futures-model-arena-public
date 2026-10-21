"""合成/回放行情源：离线演示与测试用。

- 无 csv_dir 时用带趋势切换的几何布朗运动合成分钟 bar（可复现，seed 固定）。
- 有 csv_dir 时读取 {symbol}.csv（datetime,open,high,low,close,volume）按顺序回放。
- 每次调用 latest() 推进一根 bar（clock 由引擎控制，可加速）。
"""
from __future__ import annotations

import bisect
import csv
import math
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from ..models import Bar, ContractSpec, Quote
from ..exchange.contracts import round_to_tick


class ReplaySource:
    name = "replay"

    def __init__(self, symbols: List[str], specs: Dict[str, ContractSpec], seed: int = 42, bar_seconds: int = 60,
                 start_price: Optional[Dict[str, float]] = None, csv_dir: Optional[str] = None, history: int = 300,
                 tz: str = "Asia/Shanghai"):
        if history < 1:
            raise ValueError(f"replay.history 必须 ≥ 1（当前 {history}）：合成行情需要至少一根历史 bar 作为起点")
        self.symbols = symbols
        self._specs = specs
        self.tz = ZoneInfo(tz)   # CSV 中不带时区偏移的时间按此时区解析，不依赖主机时区
        self._lock = threading.RLock()
        self.bar_seconds = bar_seconds
        self.history = history
        self.rng = random.Random(seed)
        self.bars: Dict[str, List[Bar]] = {}
        self._csv_rows: Dict[str, List[Bar]] = {}
        self._csv_idx: Dict[str, int] = {}
        self._trend: Dict[str, float] = {}
        self._vol: Dict[str, float] = {}
        self._errors = 0
        self.steps = 0                     # 自构建以来 advance 的次数：随机数消耗顺序由它唯一决定，重启后可精确快进
        base_ts = time.time() - history * bar_seconds
        self.base_ts = base_ts             # 合成历史的起点时刻；重启恢复时对齐到上次的起点，时间轴才能衔接
        for s in symbols:
            # 所有品种都初始化合成参数：CSV 回放耗尽后继续用合成 bar 续接
            self._trend[s] = self.rng.uniform(-0.0002, 0.0002)
            self._vol[s] = self.rng.uniform(0.0006, 0.0015)
            rows: List[Bar] = []
            if csv_dir and (Path(csv_dir) / f"{s}.csv").exists():
                rows = self._load_csv(Path(csv_dir) / f"{s}.csv")
            if rows:
                self._csv_rows[s] = rows
                self._csv_idx[s] = min(history, len(rows))
                self.bars[s] = rows[: self._csv_idx[s]]
            else:
                # 无 CSV 或 CSV 为空（仅表头/无法解析）：按 start_price 合成历史
                p0 = (start_price or {}).get(s, 1000.0)
                if not math.isfinite(p0) or p0 <= 0:
                    raise ValueError(f"replay.start_price[{s}] 必须是有限的正数（当前 {p0}）")
                tick = specs[s].tick if s in specs else 0.0
                if tick and p0 < tick:
                    # 低于一个最小变动价位的起始价按跳价取整后是 0：第一根 bar 的开/低价就是非法的，指标（如 ATR）随之失真
                    raise ValueError(f"replay.start_price[{s}] 不能低于合约最小变动价位 {tick}（当前 {p0}）")
                bars: List[Bar] = []
                px = p0
                for i in range(history):
                    b = self._synth_bar(s, px, base_ts + i * bar_seconds)
                    bars.append(b)
                    px = b.close
                self.bars[s] = bars

    def _load_csv(self, path: Path) -> List[Bar]:
        """读取 datetime,open,high,low,close[,volume]；无法解析的行跳过，整体不可读时返回空列表。

        datetime 可为 ISO 时间（无偏移则按 self.tz 解析，带偏移则按原偏移）或秒级时间戳。
        """
        out: List[Bar] = []
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    try:
                        raw_dt = row["datetime"]
                        try:
                            dt = datetime.fromisoformat(raw_dt)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=self.tz)
                            ts = dt.timestamp()
                        except (TypeError, ValueError):
                            ts = float(raw_dt)
                        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
                        v = float(row.get("volume") or 0)
                        # NaN / Infinity 能通过 float()，必须显式拒绝；开/高/低/收任一非正也整行丢弃
                        # （负的最低价会放大 ATR，扭曲自动止损与模型看到的指标）
                        if not all(math.isfinite(x) for x in (ts, o, h, l, c, v)) or min(o, h, l, c) <= 0:
                            continue
                        out.append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v))
                    except (KeyError, TypeError, ValueError):
                        continue
        except OSError:
            return []
        out.sort(key=lambda b: b.ts)
        return out

    def _synth_bar(self, s: str, prev_close: float, ts: float) -> Bar:
        spec = self._specs[s]
        # 偶尔切换趋势，制造可交易的波段
        if self.rng.random() < 0.02:
            self._trend[s] = self.rng.uniform(-0.0004, 0.0004)
        if self.rng.random() < 0.01:
            self._vol[s] = self.rng.uniform(0.0005, 0.002)
        r = self.rng.gauss(self._trend[s], self._vol[s])
        close = max(prev_close * (1 + r), spec.tick * 10)
        hi = max(prev_close, close) * (1 + abs(self.rng.gauss(0, self._vol[s] / 2)))
        lo = min(prev_close, close) * (1 - abs(self.rng.gauss(0, self._vol[s] / 2)))
        vol = max(1.0, self.rng.gauss(5000, 1500))
        # 按跳价取整后每个字段都至少一个跳价（价格接近跳价时取整可能得到 0），且高/低仍包住开/收；
        # 只做取整后的钳制，不额外消耗随机数，重启后按步数快进仍能精确重现
        tick = spec.tick
        o = max(round_to_tick(prev_close, tick), tick)
        c = max(round_to_tick(close, tick), tick)
        h = max(round_to_tick(hi, tick), tick, o, c)
        lo_ = min(max(round_to_tick(lo, tick), tick), o, c)
        return Bar(ts=ts, open=o, high=h, low=lo_, close=c, volume=vol, hold=100000)

    def _advance_unlocked(self, n: int = 1):
        # 外层按步、内层按品种：随机数的消耗顺序与逐 tick 调用 advance(1) 完全一致，
        # 重启后 advance(steps) 一次快进才能重现同样的序列
        self.steps += n
        for _ in range(n):
            for s in self.symbols:
                if s in self._csv_rows:
                    rows = self._csv_rows[s]
                    i = self._csv_idx[s]
                    if i < len(rows):
                        self.bars[s].append(rows[i])
                        self._csv_idx[s] = i + 1
                    else:
                        last = self.bars[s][-1]
                        self.bars[s].append(self._synth_bar(s, last.close, last.ts + self.bar_seconds))
                else:
                    last = self.bars[s][-1]
                    self.bars[s].append(self._synth_bar(s, last.close, last.ts + self.bar_seconds))
                if len(self.bars[s]) > 5000:
                    self.bars[s] = self.bars[s][-3000:]

    def advance(self, n: int = 1):
        with self._lock:
            self._advance_unlocked(n)

    @staticmethod
    def bars_for_period(bars: List[Bar], period: str, limit: int = 120, drop_forming: bool = True) -> List[Bar]:
        """Derive a period from an immutable copy of 1-minute bars.

        drop_forming=True（默认）：先丢弃最后一根 1m bar（未走完的当前分钟）再聚合，
        这样最后一根聚合 bar 一定是已收盘的。指标只基于已收盘的 bar 计算，
        与实盘侧（见 Engine._build_snapshot 的 safe_bars）口径一致。
        注意这是口径变更：同一份数据算出的指标与旧版本不同，回放结果不可比。
        """
        mult = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 240}.get(period, 1)
        if mult == 1:
            return bars[-limit:]
        src = bars[:-1] if drop_forming and bars else bars
        return aggregate(src, mult)[-limit:]

    def _snapshot_unlocked(self, symbols: List[str]) -> tuple[Dict[str, Quote], Dict[str, List[Bar]]]:
        """Return quotes and raw bars from one source revision; caller holds _lock."""
        raw = {s: list(self.bars.get(s, [])) for s in symbols}
        quotes: Dict[str, Quote] = {}
        for s, bars in raw.items():
            if not bars:
                continue
            last = bars[-1]
            ref = bars[-240].close if len(bars) > 240 else bars[0].close
            quotes[s] = Quote(symbol=s, price=last.close, ts=last.ts,
                              change_pct=(last.close / ref - 1) * 100 if ref else 0.0)
        return quotes, raw

    def snapshot(self, symbols: List[str]) -> tuple[Dict[str, Quote], Dict[str, List[Bar]]]:
        """Read a stable as-of snapshot without advancing the replay clock."""
        with self._lock:
            return self._snapshot_unlocked(symbols)

    def latest_with_snapshot(self, symbols: List[str]) -> tuple[Dict[str, Quote], Dict[str, List[Bar]]]:
        """Advance once, then return quote and bars from exactly the same revision."""
        with self._lock:
            self._advance_unlocked(1)
            return self._snapshot_unlocked(symbols)

    # ---- 恢复 ----
    def position(self) -> dict:
        """当前回放位置（引擎每个 tick 持久化，重启后 resume）。

        steps/base_ts 足以精确重建：同样的 seed、配置与 CSV 下，从头 advance(steps) 得到完全相同的随机序列、
        滚动 bar、趋势/波动状态与 CSV 游标。symbols 里的时刻/收盘价用于校验（以及配置已变时的近似衔接）。
        """
        with self._lock:
            return {"steps": self.steps, "base_ts": self.base_ts,
                    "symbols": {s: {"ts": bars[-1].ts, "close": bars[-1].close} for s, bars in self.bars.items() if bars}}

    def resume(self, state: dict) -> bool:
        """重启后回到上次回放到的位置，而不是从头再放一遍。返回是否精确恢复。

        精确恢复：把合成历史的起点对齐到上次的 base_ts，再确定性地 advance(steps)——随机数生成器、趋势/波动状态、
        滚动 bar 与 CSV 游标都与上次运行完全一致，指标不会因重启而突变。
        校验失败（CSV 或配置已变）或旧格式记录：退化为按各品种上次时刻/价格衔接（CSV 按时间戳快进，合成行情平移缩放），
        至少保证时间轴不倒退、价格连续。
        """
        symbols_state = state.get("symbols") if isinstance(state.get("symbols"), dict) else state
        steps = state.get("steps")
        base_ts = state.get("base_ts")
        with self._lock:
            if isinstance(steps, int) and steps >= 0 and isinstance(base_ts, (int, float)) and math.isfinite(base_ts):
                shift = float(base_ts) - self.base_ts
                for s in self.symbols:
                    if s not in self._csv_rows:   # CSV 历史自带时间戳；只有合成历史需要对齐起点
                        self.bars[s] = [Bar(ts=b.ts + shift, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume, hold=b.hold)
                                        for b in self.bars[s]]
                self.base_ts = float(base_ts)
                self._advance_unlocked(steps)
                if self._matches_unlocked(symbols_state):
                    return True
                # 快进结果与记录不符（CSV/配置已变）：退回近似衔接
            self._resume_approx_unlocked(symbols_state)
            return False

    def _matches_unlocked(self, symbols_state: dict) -> bool:
        for s, st in (symbols_state or {}).items():
            bars = self.bars.get(s)
            if not bars or not isinstance(st, dict):
                return False
            try:
                if abs(bars[-1].ts - float(st["ts"])) > 1e-6 or abs(bars[-1].close - float(st["close"])) > 1e-6:
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        return True

    def _resume_approx_unlocked(self, symbols_state: dict):
        for s in self.symbols:
            st = (symbols_state or {}).get(s) or {}
            try:
                ts, close = float(st["ts"]), float(st["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(ts) and math.isfinite(close) and close > 0):
                continue
            if s in self._csv_rows:
                rows = self._csv_rows[s]
                idx = bisect.bisect_right([b.ts for b in rows], ts)   # ts 之后的第一行
                if idx == 0:
                    continue   # 持久化的位置早于整个 CSV：视为不同数据集，从头开始
                self._csv_idx[s] = idx
                self.bars[s] = list(rows[max(0, idx - self.history): idx])
                if ts > rows[-1].ts:
                    # 上次已放完 CSV 并在合成续接：从持久化的时刻/价格续接，时间不能倒退
                    self.bars[s].append(Bar(ts=ts, open=close, high=close, low=close, close=close, volume=0.0, hold=self.bars[s][-1].hold))
            else:
                bars = self.bars.get(s)
                if not bars:
                    continue
                last = bars[-1]
                ratio = close / last.close if last.close > 0 else 1.0
                shift = ts - last.ts
                self.bars[s] = [Bar(ts=b.ts + shift, open=b.open * ratio, high=b.high * ratio, low=b.low * ratio, close=b.close * ratio,
                                    volume=b.volume, hold=b.hold) for b in bars]

    # ---- MarketSource ----
    def specs(self) -> Dict[str, ContractSpec]:
        return self._specs

    def latest(self, symbols: List[str]) -> Dict[str, Quote]:
        quotes, _ = self.latest_with_snapshot(symbols)
        return quotes

    def klines(self, symbol: str, period: str, limit: int = 120) -> List[Bar]:
        with self._lock:
            bars = list(self.bars.get(symbol, []))
        return self.bars_for_period(bars, period, limit)

    def is_open(self, now: datetime, symbol: Optional[str] = None) -> bool:
        return True

    def next_open(self, now: datetime) -> Optional[datetime]:
        return now

    def health(self) -> dict:
        return {"source": self.name, "ok": True, "errors": self._errors, "detail": "合成行情"}


def aggregate(bars: List[Bar], n: int) -> List[Bar]:
    out: List[Bar] = []
    # 从末尾对齐，保证最新一根尽量完整
    start = len(bars) % n
    for i in range(start, len(bars), n):
        chunk = bars[i:i + n]
        if not chunk:
            continue
        out.append(Bar(ts=chunk[0].ts, open=chunk[0].open, high=max(b.high for b in chunk), low=min(b.low for b in chunk),
                       close=chunk[-1].close, volume=sum(b.volume for b in chunk), hold=chunk[-1].hold))
    return out
