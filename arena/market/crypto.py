"""加密永续合约公共行情：OKX（默认）或 Binance USDT-M。无需 API Key。"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from ..exchange.contracts import crypto_spec
from ..models import Bar, ContractSpec, Quote
from .base import SourceError

log = logging.getLogger("arena.market.crypto")

OKX_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D"}
BN_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}
MAX_FUTURE_BAR_SKEW_SEC = 60.0


def to_binance_symbol(sym: str) -> str:
    # BTC-USDT-SWAP -> BTCUSDT
    return sym.replace("-SWAP", "").replace("-", "")


def _finite(x) -> Optional[float]:
    """Return a finite numeric value while rejecting booleans and non-numbers."""
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _price(x) -> Optional[float]:
    f = _finite(x)
    return f if f is not None and f > 0 else None


def _make_bar(row) -> Optional[Bar]:
    """Build a safe OHLCV bar from an OKX/Binance row, or reject it."""
    if not isinstance(row, (list, tuple)) or len(row) < 5 or isinstance(row[0], bool):
        return None
    ts_ms = _finite(row[0])
    if ts_ms is None:
        return None
    ts = ts_ms / 1000
    if ts > time.time() + MAX_FUTURE_BAR_SKEW_SEC:
        return None
    o, h, l, c = (_price(v) for v in row[1:5])
    if None in (o, h, l, c):
        return None
    v = _finite(row[5]) if len(row) > 5 else 0.0
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v if v is not None else 0.0)


class CryptoSource:
    name = "crypto"

    def __init__(self, symbols: List[str], provider: str = "okx", timeout: float = 10, cache_sec: float = 15, max_leverage: float = 20):
        self.symbols = symbols
        self.provider = provider
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": "futures-arena/0.1"})
        self.cache_sec = cache_sec
        self._cache: Dict[str, tuple] = {}
        self._specs: Dict[str, ContractSpec] = {s: crypto_spec(s, max_leverage=max_leverage) for s in symbols}
        self._errors = 0
        self._last_ok: Optional[float] = None
        self._last_err = ""
        self._loaded_instruments = False
        self.stop_event: Optional[threading.Event] = None   # 引擎注入；停止时立即放弃请求与重试
        # 资金费率 / 持仓量是可选增强信息：缓存 + 后台线程刷新，绝不拖慢价格返回（价格决定止损/止盈/强平）
        self.enrich_interval = 60.0
        self._enrich: Dict[str, dict] = {}
        self._enrich_at = 0.0
        self._enrich_lock = threading.Lock()
        self._enrich_thread: Optional[threading.Thread] = None
        # _get 可能被多个线程并发调用（tick 线程、K 线并行拉取、enrich 后台线程）：
        # 计数器/健康状态的读写用独立小锁保护（不用 _enrich_lock，避免与 enrich 流程耦合）
        self._stats_lock = threading.Lock()

    def _stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    # ---- HTTP ----
    def _get(self, url: str, params: dict) -> object:
        last: Optional[Exception] = None
        for attempt in range(3):
            if self._stopping():
                raise SourceError("引擎停止，取消行情请求")
            try:
                r = self.client.get(url, params=params)
                r.raise_for_status()
                with self._stats_lock:
                    self._last_ok = time.time()
                return r.json()
            except Exception as e:  # noqa: BLE001
                last = e
                desc = self._describe_error(e)
                with self._stats_lock:
                    self._errors += 1
                    self._last_err = desc
                if self._blocked_status(e) is not None:
                    break   # 地区限制是确定性的：重试只会把盯市拖慢，直接报出提示
                delay = 0.5 * (2 ** attempt)
                if self.stop_event is not None:
                    self.stop_event.wait(delay)
                else:
                    time.sleep(delay)
        raise SourceError(f"{self.provider} 请求失败: {self._last_err or last}")

    @staticmethod
    def _blocked_status(e: Exception) -> Optional[int]:
        """HTTP 451/403：交易所对当前网络/地区拒绝服务（Binance 在部分地区返回 451）。"""
        code = getattr(getattr(e, "response", None), "status_code", None)
        return code if code in (403, 451) else None

    def _describe_error(self, e: Exception) -> str:
        code = self._blocked_status(e)
        if code is None:
            return str(e)
        who = "Binance" if self.provider == "binance" else self.provider.upper()
        return f"HTTP {code}：{who} 在当前网络/地区不可用（地区限制），请改用 provider: okx 或更换可访问的网络"

    def _load_instruments(self):
        if self._loaded_instruments:
            return
        try:
            if self.provider == "okx":
                data = self._get("https://www.okx.com/api/v5/public/instruments", {"instType": "SWAP"})
                for it in data.get("data", []):
                    s = it.get("instId")
                    if s in self._specs:
                        sp = self._specs[s]
                        sp.tick = float(it.get("tickSz", sp.tick))
                        sp.lot_step = float(it.get("lotSz", 1)) * float(it.get("ctVal", 1)) if it.get("ctVal") else sp.lot_step
                        sp.lot_step = max(sp.lot_step, 1e-6)
            else:
                data = self._get("https://fapi.binance.com/fapi/v1/exchangeInfo", {})
                for it in data.get("symbols", []):
                    s = next((k for k in self._specs if to_binance_symbol(k) == it.get("symbol")), None)
                    if s:
                        for f in it.get("filters", []):
                            if f.get("filterType") == "PRICE_FILTER":
                                self._specs[s].tick = float(f.get("tickSize", self._specs[s].tick))
                            if f.get("filterType") == "LOT_SIZE":
                                self._specs[s].lot_step = float(f.get("stepSize", self._specs[s].lot_step))
            self._loaded_instruments = True
        except Exception as e:  # noqa: BLE001
            log.warning("加载合约信息失败，使用默认规格: %s", e)

    # ---- MarketSource ----
    def specs(self) -> Dict[str, ContractSpec]:
        self._load_instruments()
        return self._specs

    def klines(self, symbol: str, period: str, limit: int = 120) -> List[Bar]:
        key = f"{symbol}:{period}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cache_sec:
            return cached[1][-limit:]
        if self.provider == "okx":
            data = self._get("https://www.okx.com/api/v5/market/candles", {"instId": symbol, "bar": OKX_BAR.get(period, "5m"), "limit": min(limit, 300)})
            rows = data.get("data", [])
        else:
            rows = self._get("https://fapi.binance.com/fapi/v1/klines", {"symbol": to_binance_symbol(symbol), "interval": BN_BAR.get(period, "5m"), "limit": min(limit, 1000)})
        bars = [b for b in (_make_bar(row) for row in rows) if b is not None]
        bars.sort(key=lambda b: b.ts)
        if not bars:
            raise SourceError(f"{symbol} {period} 无数据")
        self._cache[key] = (time.time(), bars)
        return bars[-limit:]

    def latest(self, symbols: List[str]) -> Dict[str, Quote]:
        """只做一次行情请求就返回价格；资金费率/持仓量来自缓存，由后台线程按 enrich_interval 刷新。"""
        out: Dict[str, Quote] = {}
        try:
            if self.provider == "okx":
                data = self._get("https://www.okx.com/api/v5/market/tickers", {"instType": "SWAP"})
                by_id = {d["instId"]: d for d in data.get("data", [])}
                for s in symbols:
                    d = by_id.get(s)
                    px = _price(d.get("last")) if d else None
                    if px is None:
                        if d:
                            log.warning("%s 最新价异常，跳过: %r", s, d.get("last"))
                        continue
                    o24 = _price(d.get("open24h")) or px
                    out[s] = Quote(symbol=s, price=px, ts=int(d.get("ts", time.time() * 1000)) / 1000,
                                   change_pct=(px / o24 - 1) * 100)
            else:
                data = self._get("https://fapi.binance.com/fapi/v1/ticker/24hr", {})
                by_sym = {d["symbol"]: d for d in data}
                for s in symbols:
                    d = by_sym.get(to_binance_symbol(s))
                    px = _price(d.get("lastPrice")) if d else None
                    if px is None:
                        if d:
                            log.warning("%s 最新价异常，跳过: %r", s, d.get("lastPrice"))
                        continue
                    out[s] = Quote(symbol=s, price=px, ts=int(d.get("closeTime", time.time() * 1000)) / 1000,
                                   change_pct=_finite(d.get("priceChangePercent")) or 0.0)
        except Exception as e:  # noqa: BLE001
            log.warning("%s tickers 失败: %s", self.provider, e)
            return out
        self._maybe_refresh_enrichment(list(symbols))
        with self._enrich_lock:
            for s, q in out.items():
                e = self._enrich.get(s)
                if e:
                    q.funding_rate = e.get("funding_rate")
                    q.open_interest = e.get("open_interest")
        return out

    def _maybe_refresh_enrichment(self, symbols: List[str]):
        if time.time() - self._enrich_at < self.enrich_interval or self._stopping():
            return
        if self._enrich_thread is not None and self._enrich_thread.is_alive():
            return
        self._enrich_thread = threading.Thread(target=self._refresh_enrichment, args=(symbols,), name="crypto-enrich", daemon=True)
        self._enrich_thread.start()

    def _refresh_enrichment(self, symbols: List[str]):
        fresh: Dict[str, dict] = {}
        try:
            if self.provider == "okx":
                for s in symbols:
                    if self._stopping():
                        break
                    item: dict = {"funding_rate": None, "open_interest": None}
                    try:
                        fr = self._get("https://www.okx.com/api/v5/public/funding-rate", {"instId": s})
                        item["funding_rate"] = _finite(fr["data"][0].get("fundingRate")) if fr.get("data") else None
                        oi = self._get("https://www.okx.com/api/v5/public/open-interest", {"instType": "SWAP", "instId": s})
                        item["open_interest"] = _finite(oi["data"][0].get("oi")) if oi.get("data") else None
                    except Exception as e:  # noqa: BLE001
                        log.debug("OKX 增强数据 %s 失败: %s", s, e)
                    fresh[s] = item
            else:
                prem = {d["symbol"]: d for d in self._get("https://fapi.binance.com/fapi/v1/premiumIndex", {})}
                for s in symbols:
                    fresh[s] = {"funding_rate": _finite(prem.get(to_binance_symbol(s), {}).get("lastFundingRate")), "open_interest": None}
        except Exception as e:  # noqa: BLE001
            log.warning("%s 增强数据刷新失败: %s", self.provider, e)
        finally:
            with self._enrich_lock:
                self._enrich.update(fresh)
                self._enrich_at = time.time()

    def is_open(self, now: datetime, symbol: Optional[str] = None) -> bool:
        return True

    def next_open(self, now: datetime) -> Optional[datetime]:
        return now

    def health(self) -> dict:
        with self._stats_lock:
            last_ok, errors, last_err = self._last_ok, self._errors, self._last_err
        return {"source": f"{self.name}:{self.provider}", "ok": last_ok is not None and time.time() - last_ok < 300,
                "errors": errors, "last_ok": last_ok, "detail": last_err[:200]}
