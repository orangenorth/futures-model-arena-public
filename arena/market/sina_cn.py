"""国内期货行情：新浪 InnerFuturesNewService 接口（与 akshare futures_zh_minute_sina / futures_zh_daily_sina 同源）。

- 分钟线：getFewMinLine?symbol=RB0&type=1|5|15|30|60 → [[datetime, open, high, low, close, volume, hold], ...]
- 日线：  getDailyKLine?symbol=RB0 → [[date, open, high, low, close, volume, hold, settle], ...]
当前价取 hq.sinajs.cn 的轻量连续合约报价；技术指标仍仅基于分钟线端点。
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from ..exchange.contracts import next_open as _next_open, session_open
from ..models import Bar, ContractSpec, Quote
from .base import SourceError
from .indicators import resample

log = logging.getLogger("arena.market.sina")

BASE = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService.{method}"
SPOT_BASE = "https://hq.sinajs.cn/list={codes}"
HEADERS = {
    "Accept": "*/*",
    "Referer": "https://finance.sina.com.cn/futures/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
PERIOD_MAP = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60"}
MAX_FUTURE_BAR_SKEW_SEC = 60.0
# 每个周期的 K 线缓存时长。同一个 TTL 套在所有周期上时，一根 1h bar 在它成形的那一小时里会被
# 反复重取：82 个品种 × 5m/15m/1h = 246 次请求，每个决策轮重来一遍。慢周期在其周期内不会变，
# 按周期长度分档即可，代价是慢周期最多陈旧这么久——都远小于该周期本身。
PERIOD_CACHE_SEC = {"1m": 15.0, "5m": 60.0, "15m": 300.0, "30m": 600.0, "1h": 900.0, "1d": 1800.0}
SPOT_BATCH_SIZE = 50     # 轻量报价一次请求带多少个合约：URL 与响应体都还很轻，82 个品种 = 2 次请求
# 慢周期由哪个细粒度周期重采样得到（周期 → (细粒度周期, 秒数)）：冷启动时省掉两次请求/品种
DERIVE_FROM = {"15m": ("5m", 900), "30m": ("5m", 1800), "1h": ("5m", 3600)}
# 派生出来的根数至少要够算齐 `indicators.summarize` 里最长的那个指标（ema60）。
# 不要求够 `limit`：新浪的 getFewMinLine 单次最多返回约 1023 根 5m，派生 1h 只有 ~85 根，
# 按 limit=100 去卡就永远派生不出来、每个品种照旧多发一次请求。
INDICATOR_MIN_BARS = 60
SYMBOL_RE = re.compile(r"[A-Z0-9]+")


class SinaFuturesSource:
    name = "sina_cn"

    def __init__(self, symbols: List[str], specs: Dict[str, ContractSpec], holidays: List[str], timeout: float = 10, cache_sec: float = 15,
                 tz: str = "Asia/Shanghai", max_workers: int = 8):
        self.symbols = symbols
        self._specs = specs
        self.holidays = holidays
        # 新浪返回的是交易所本地时间（东八区），必须按该时区解析，不能依赖主机时区
        self.tz = ZoneInfo(tz)
        self.timeout = float(timeout)
        self.client = httpx.Client(timeout=timeout, headers=HEADERS)
        self.cache_sec = cache_sec
        self._cache: Dict[str, tuple] = {}   # key -> (ts, bars)
        self._errors = 0
        self._last_ok: Optional[float] = None
        self._last_err = ""
        self.stop_event: Optional[threading.Event] = None   # 引擎注入；停止时立即放弃请求与重试
        # latest() 分批并发抓取：整轮耗时以 latest_budget_sec 为上限，而不是 超时 × 批数
        self.latest_budget_sec = self.timeout + 1.0
        self.spot_batch_size = SPOT_BATCH_SIZE   # 一次 `list=` 请求带多少个合约
        self._pool = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(symbols) or 1)), thread_name_prefix="sina-quote")
        self._inflight: Dict[str, Future] = {}   # 品种 -> 仍在进行的抓取；下个 tick 复用而不是再起一个
        self._lock = threading.Lock()

    def _stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _get(self, method: str, params: dict, attempts: int = 3) -> list:
        url = BASE.format(method=method)
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            if self._stopping():
                raise SourceError("引擎停止，取消行情请求")
            try:
                if method == "getRealtimeQuote":
                    # 一次请求可以带多个合约（`list=nf_A,nf_B,…`）。逐个校验代码再拼接：
                    # 对拼好的串整体做格式校验等于没有校验，逗号会把任何东西放进去。
                    requested = params.get("symbols")
                    requested = [params.get("symbol")] if requested is None else list(requested)
                    # 键用调用方给的原值，查询用大写后的代码：配置里写成小写 rb0 时，
                    # 若按规范化后的 RB0 回键，调用方用原键取不到，该品种就永远没有报价
                    wanted: List[tuple] = []
                    for raw in requested:
                        key = str(raw or "")
                        symbol = key.upper()
                        if not SYMBOL_RE.fullmatch(symbol):
                            log.warning("跳过格式异常的期货合约代码: %r", raw)
                            continue
                        wanted.append((key, symbol))
                    if not wanted:
                        raise SourceError("期货合约代码格式异常")
                    r = self.client.get(SPOT_BASE.format(codes=",".join(f"nf_{code}" for _, code in wanted)))
                    r.raise_for_status()
                    text = r.content.decode("gb18030", errors="replace")
                    # 单个合约解析失败只丢它自己：一个下市/改名的合约不能让整批报价作废
                    rows_by_symbol: Dict[str, list] = {}
                    for key, code in wanted:
                        try:
                            rows_by_symbol[key] = self._spot_to_minute_rows(text, code)
                        except SourceError as e:
                            log.warning("实时行情 %s 解析失败: %s", code, e)
                    if not rows_by_symbol:
                        raise SourceError("实时行情响应为空或合约不匹配")
                    data = rows_by_symbol if "symbols" in params else rows_by_symbol[wanted[0][0]]
                else:
                    r = self.client.get(url, params=params)
                    r.raise_for_status()
                    text = r.text
                    start, end = text.find("(["), text.rfind("])")
                    if start < 0 or end < 0:
                        raise SourceError(f"响应格式异常: {text[:80]}")
                    data = json.loads(text[start + 1:end + 1])
                with self._lock:
                    self._last_ok = time.time()
                return data
            except Exception as e:  # noqa: BLE001
                last_exc = e
                with self._lock:
                    self._errors += 1
                    self._last_err = str(e)
                if attempt + 1 >= attempts:
                    break
                delay = 0.5 * (2 ** attempt)
                if self.stop_event is not None:
                    self.stop_event.wait(delay)
                else:
                    time.sleep(delay)
        raise SourceError(f"新浪行情请求失败: {last_exc}")

    @staticmethod
    def _spot_to_minute_rows(text: str, symbol: str) -> list:
        """Normalize Sina's lightweight futures quote payload into one minute-bar row.

        This endpoint is deliberately used only for the current quote. Historical
        multi-timeframe bars retain the documented InnerFutures endpoint, so an
        upstream history outage cannot silently turn a decision round into a
        quote-only technical analysis.
        """
        match = re.search(
            rf'var\s+hq_str_nf_{re.escape(symbol)}="([^"]*)";', text, re.IGNORECASE
        )
        if not match or not match.group(1):
            raise SourceError("实时行情响应为空或合约不匹配")
        fields = match.group(1).split(",")
        # Sina futures quote layout: name,time,open,high,low,...,last,...,
        # prev_settle,...,open_interest(13),volume(14),...,date.  The normalized
        # row is [datetime, open, high, low, close, volume, hold, prev_settle],
        # so field 14 supplies volume and field 13 supplies open interest —
        # swapping them reports turnover as position and vice versa.  Keep
        # prev_settle as an eighth value: Bar parsing intentionally ignores it,
        # while _fetch_quote uses it for the displayed daily change.
        if len(fields) <= 17:
            raise SourceError("实时行情字段不足")
        day = re.sub(r"\D", "", fields[17])
        clock = re.sub(r"\D", "", fields[1])
        if len(day) != 8 or len(clock) < 4:
            raise SourceError("实时行情时间字段异常")
        clock = (clock + "000000")[:6]
        stamp = f"{day[:4]}-{day[4:6]}-{day[6:8]} {clock[:2]}:{clock[2:4]}:{clock[4:6]}"
        return [[stamp, fields[2], fields[3], fields[4], fields[8], fields[14], fields[13], fields[10]]]

    @staticmethod
    def _split_row(r) -> tuple:
        if isinstance(r, dict):
            return r.get("d"), r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v"), r.get("p", 0)
        return r[0], r[1], r[2], r[3], r[4], r[5], (r[6] if len(r) > 6 else 0)

    @staticmethod
    def _finite(value) -> Optional[float]:
        """Return a finite numeric value while rejecting booleans and malformed fields."""
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _make_bar(cls, ts: float, o, h, l, c, v, hold) -> Optional[Bar]:
        """Build a safe OHLCV bar or reject invalid and future-dated upstream data."""
        timestamp = cls._finite(ts)
        o, h, l, c = (cls._finite(value) for value in (o, h, l, c))
        if (timestamp is None or None in (o, h, l, c)
                or timestamp > time.time() + MAX_FUTURE_BAR_SKEW_SEC):
            return None
        if min(o, h, l, c) <= 0:
            return None
        volume = cls._finite(v)
        open_interest = cls._finite(hold)
        return Bar(ts=timestamp, open=o, high=h, low=l, close=c,
                   volume=volume if volume is not None else 0.0,
                   hold=open_interest if open_interest is not None else 0.0)

    def _parse_rows(self, rows: list, fmt: str, width: int) -> List[Bar]:
        out: List[Bar] = []
        for r in rows:
            try:
                dt, o, h, l, c, v, hold = self._split_row(r)
                ts = datetime.strptime(str(dt)[:width], fmt).replace(tzinfo=self.tz).timestamp()
                bar = self._make_bar(ts, o, h, l, c, v, hold)
            except Exception:  # noqa: BLE001
                continue
            if bar is not None:
                out.append(bar)
        out.sort(key=lambda b: b.ts)
        return out

    def _parse_minute(self, rows: list) -> List[Bar]:
        return self._parse_rows(rows, "%Y-%m-%d %H:%M:%S", 19)

    def _parse_daily(self, rows: list) -> List[Bar]:
        return self._parse_rows(rows, "%Y-%m-%d", 10)

    # ---- MarketSource ----
    def specs(self) -> Dict[str, ContractSpec]:
        return self._specs

    def _cache_ttl(self, period: str) -> float:
        """该周期的 K 线缓存时长：基础 `cache_sec` 与周期分档取大者。

        `cache_sec <= 0` 是"显式关掉缓存"，分档不得把它重新打开。
        """
        if self.cache_sec <= 0:
            return 0.0
        return max(float(self.cache_sec), PERIOD_CACHE_SEC.get(period, float(self.cache_sec)))

    def _cached_series(self, symbol: str, period: str) -> Optional[List[Bar]]:
        """仅查缓存，绝不发请求。派生慢周期时只认已经在手的细粒度序列。"""
        cached = self._cache.get(f"{symbol}:{period}")
        if cached and time.time() - cached[0] < self._cache_ttl(period):
            return cached[1]
        return None

    def _series(self, symbol: str, period: str, attempts: int = 3) -> List[Bar]:
        """该周期的完整序列（带缓存）。`klines()` 只负责切片与派生。"""
        key = f"{symbol}:{period}"
        cached = self._cached_series(symbol, period)
        if cached is not None:
            return cached
        if period == "1d":
            bars = self._parse_daily(self._get("getDailyKLine", {"symbol": symbol}, attempts))
        else:
            bars = self._parse_minute(self._get("getFewMinLine", {"symbol": symbol, "type": PERIOD_MAP.get(period, "5")}, attempts))
        if not bars:
            raise SourceError(f"{symbol} {period} 无数据")
        self._cache[key] = (time.time(), bars)
        return bars

    def klines(self, symbol: str, period: str, limit: int = 120, attempts: int = 3) -> List[Bar]:
        """慢周期优先由 5m 序列重采样得到，拿不够根数时才单独请求该周期。

        分档缓存只能让**后续**轮次少发请求：进程刚启动时缓存是空的，`_build_snapshot` 仍会为
        每个品种顺序取 5m/15m/1h 三次，71 个品种就是第一轮决策前 213 次请求——正是会触发限流、
        把一轮拖成几分钟的那种突发。5m 序列够长时 15m/1h 本来就能由它算出来，
        于是冷启动从"每个品种三次"降到"每个品种一次"。

        代价：重采样的分块从序列末尾对齐，边界不一定落在整点上（与回放路径同一套 `aggregate`）。
        指标看的是最近若干根的形态，这点偏移可以接受；但根数不够时派生出来的序列会明显变短、
        指标失真，所以那种情况退回直接请求该周期。

        派生**只认已经缓存的** 5m 序列，绝不为了派生而去取一次 5m：那样在 5m 太短时
        反而变成两次请求。快照本来就先取 5m 再取 15m/1h，所以到这一步它一定已经在手，
        派生是白送的；单独调用 `klines("1h")` 则照常直接请求，不多花一次。

        门槛是"够算齐指标"而不是"够 limit 根"：新浪单次最多给约 1023 根 5m，派生 1h 只有
        ~85 根，按 limit=100 去卡就永远派生不出来——每个品种仍会多发一次请求，冷启动根本没降下来。
        85 根足够 ema60/macd 全部有值，比多发一次请求换那 15 根划算。
        """
        base = DERIVE_FROM.get(period)
        if base:
            base_period, seconds = base
            fine = self._cached_series(symbol, base_period)
            if fine is not None:
                derived = resample(fine, seconds)
                if len(derived) >= min(limit, INDICATOR_MIN_BARS):
                    return derived[-limit:]
        return self._series(symbol, period, attempts)[-limit:]

    def _fetch_quote(self, symbol: str) -> Quote:
        # 每个 tick 都会再来一次，因此这里不做多次重试：失败就等下个 tick，沿用上次报价。
        # 当前价走轻量报价端点；K 线仍由 klines() 单独提供给多周期快照。
        return self._quote_from_rows(symbol, self._get("getRealtimeQuote", {"symbol": symbol}, attempts=1))

    def _fetch_quotes(self, symbols: tuple) -> Dict[str, Quote]:
        """一次请求取回整批合约的轻量报价。

        每个品种各发一次请求的话，82 个品种每 20s 一个 tick 就是 ~4.1 req/s：被限流之后
        大批品种等不到 `latest_budget_sec`，报价变陈旧，新鲜度闸门直接卡住决策。
        `list=` 本来就支持逗号拼接，一次请求解决一批。
        单个品种解析失败只丢它自己——一个合约的响应异常不能让整批报价作废。
        """
        rows_by_symbol = self._get("getRealtimeQuote", {"symbols": list(symbols)}, attempts=1)
        out: Dict[str, Quote] = {}
        for symbol in symbols:
            rows = rows_by_symbol.get(symbol)
            if not rows:
                continue
            try:
                out[symbol] = self._quote_from_rows(symbol, rows)
            except SourceError as e:
                log.warning("latest %s 无有效报价: %s", symbol, e)
        if not out:
            raise SourceError("实时行情无有效报价")
        return out

    def _quote_from_rows(self, symbol: str, rows: list) -> Quote:
        bars = self._parse_minute(rows)
        if not bars:
            raise SourceError(f"{symbol} 实时行情无数据")
        last = bars[-1]
        # A lightweight quote is one current bar, so its open is today's open,
        # not the prior settlement.  The normalizer retains the source's
        # prev_settle at index 7.  A missing or malformed reference must not be
        # substituted with today's open, which would report the wrong daily move.
        raw_prev_settle = None
        if rows and isinstance(rows[-1], (list, tuple)) and len(rows[-1]) > 7:
            raw_prev_settle = rows[-1][7]
        prev_settle = self._finite(raw_prev_settle)
        if prev_settle is None or prev_settle <= 0:
            change_pct = 0.0
        else:
            change_pct = (last.close / prev_settle - 1) * 100
            if not math.isfinite(change_pct):
                change_pct = 0.0
        return Quote(symbol=symbol, price=last.close, ts=last.ts, change_pct=change_pct, open_interest=last.hold)

    def latest(self, symbols: List[str]) -> Dict[str, Quote]:
        """各品种并发抓取最新 1 分钟 bar；整体等待不超过 latest_budget_sec。

        盯市线程每个 tick 调用一次：新浪变慢或不可用时，不能让止损/止盈/强平检查被
        『超时 × 品种数』拖住。超过预算仍未返回的品种本次跳过（引擎沿用上次报价，由新鲜度闸门
        决定是否还能决策），其抓取继续在后台完成。_inflight 只保存『结果尚未交付』的抓取：
        下个 tick 若它仍在进行则继续等它，若已完成则先交付该结果再发起新请求——迟到的成功
        响应绝不能因为被替换而丢失，否则慢行情源下引擎可能永远拿不到新价。

        一个 future 覆盖一批（最多 spot_batch_size 个）品种，对应一次 `list=` 请求：
        82 个品种每 tick 两次请求，而不是 82 次。`_inflight` 仍按品种登记（同一批的品种
        指向同一个 future），上面那套『在途复用 / 迟到交付』的语义因此完全不变。
        """
        out: Dict[str, Quote] = {}
        if self._stopping():
            return out
        deadline = time.time() + self.latest_budget_sec
        futs: Dict[Future, tuple] = {}     # future -> 本次要等它的品种
        with self._lock:
            pending: List[str] = []        # 需要新发起抓取的品种，按批打包
            waiting: List[str] = []        # 其中本次要等结果的（未交付过迟到报价）
            for s in symbols:
                f = self._inflight.get(s)
                delivered_late = False
                if f is not None and f.done():
                    # 上个 tick 预算内没等到、之后才完成的结果：先收下，再发起新的抓取
                    try:
                        result = f.result()
                        if s in result:
                            out[s] = result[s]
                            delivered_late = True
                        else:
                            log.warning("latest %s 无报价（同批其他品种已返回）", s)
                    except Exception as e:  # noqa: BLE001
                        log.warning("latest %s 失败: %s", s, e)
                    self._inflight.pop(s, None)
                    f = None
                if f is None:
                    pending.append(s)
                    # 已交付迟到报价的品种，本次不再等待其替换请求：行情源持续偏慢时每个 tick 都能立即返回，
                    # 替换请求在后台流水线式进行，下个 tick 交付。没有可用报价时才在预算内等待。
                    if not delivered_late:
                        waiting.append(s)
                elif not delivered_late:
                    futs.setdefault(f, ())
                    futs[f] = futs[f] + (s,)
            wait_set = set(waiting)
            size = max(1, int(self.spot_batch_size))
            for i in range(0, len(pending), size):
                batch = tuple(pending[i:i + size])
                try:
                    f = self._pool.submit(self._fetch_quotes, batch)
                except RuntimeError:   # 线程池已关闭（close() 之后）
                    for s in batch:
                        self._inflight.pop(s, None)
                    return out
                for s in batch:
                    self._inflight[s] = f
                awaited = tuple(s for s in batch if s in wait_set)
                if awaited:
                    futs[f] = futs.get(f, ()) + awaited
        for f, batch in futs.items():
            if self._stopping():
                break
            try:
                result = f.result(timeout=max(0.0, deadline - time.time()))   # 更新的结果覆盖迟到交付的旧结果
                for s in batch:
                    if s in result:
                        out[s] = result[s]
                    else:
                        log.warning("latest %s 无报价（同批其他品种已返回）", s)
            except FutTimeout:
                log.warning("latest %s 超过 %.0fs 未返回，本次跳过（后台继续，完成后下个 tick 交付）",
                            "/".join(batch), self.latest_budget_sec)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("latest %s 失败: %s", "/".join(batch), e)
            with self._lock:
                for s in batch:
                    if self._inflight.get(s) is f:
                        del self._inflight[s]   # 结果已交付（成功或失败），不再保留
        return out

    def _exchange_time(self, now: datetime) -> datetime:
        """Interpret session boundaries in the exchange timezone, not the competition display timezone."""
        return now.replace(tzinfo=self.tz) if now.tzinfo is None else now.astimezone(self.tz)

    def is_open(self, now: datetime, symbol: Optional[str] = None) -> bool:
        exchange_now = self._exchange_time(now)
        syms = [symbol] if symbol else self.symbols
        return any(session_open(self._specs[s], exchange_now, self.holidays) for s in syms if s in self._specs)

    def next_open(self, now: datetime) -> Optional[datetime]:
        cands = self.next_opens(now)
        return cands[0] if cands else None

    def next_opens(self, now: datetime) -> List[datetime]:
        """每个品种各自的下一次开市时刻（去重、升序）。

        盘前研究要的是"下一个受支持的研究相位"，不是最早的那个开市：股指 13:00 开、商品 13:30 开，
        取最小值的话 13:30 永远被 13:00（或股指已在交易时的下一分钟）挡住。
        """
        exchange_now = self._exchange_time(now)
        cands = {_next_open(self._specs[s], exchange_now, self.holidays) for s in self.symbols if s in self._specs}
        out = []
        for candidate in sorted(c for c in cands if c):
            out.append(candidate.replace(tzinfo=None) if now.tzinfo is None else candidate.astimezone(now.tzinfo))
        return out

    def health(self) -> dict:
        with self._lock:
            last_ok, errors, last_err = self._last_ok, self._errors, self._last_err
        return {"source": self.name, "ok": last_ok is not None and time.time() - last_ok < 300,
                "errors": errors, "last_ok": last_ok, "detail": last_err[:200]}

    def close(self):
        """停止后台抓取线程池；进行中的请求在超时或停止检查点自行退出。"""
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
