"""第十轮审查：逐品种行情新鲜度、日历有效期与异步增强数据。"""
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_calendar_valid_until, load_cn_contracts, session_open
from arena.market.crypto import CryptoSource
from arena.models import ActionRequest, Bar, Decision, Quote

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_engine(tmp_path, accelerate=True, tick=1, dec=1, symbols=("RB0",)):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": dec, "tick_interval_sec": tick, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=accelerate), db


class _LiveTestSource:
    """In-memory live-like source: timestamps use wall time, unlike ReplaySource."""

    def __init__(self, symbols):
        self.quotes = {}
        base = time.time() - 180 * 60
        self._bars = {}
        for symbol in symbols:
            price = 3400.0 if symbol == "RB0" else 800.0
            self._bars[symbol] = [
                Bar(ts=base + i * 60, open=price, high=price + 1, low=price - 1, close=price, volume=100)
                for i in range(180)
            ]

    def latest(self, symbols):
        return {symbol: self.quotes[symbol] for symbol in symbols if symbol in self.quotes}

    def klines(self, symbol, period, limit=120):
        return self._bars[symbol][-limit:]

    def is_open(self, now, symbol=None):
        return True

    def next_open(self, now):
        return now


def make_live_engine(tmp_path, tick=1, dec=1, symbols=("RB0",), accelerate=False):
    eng, db = make_engine(tmp_path, accelerate=accelerate, tick=tick, dec=dec, symbols=symbols)
    eng.source = _LiveTestSource(eng.symbols)
    return eng, db


def make_cn_session_engine(tmp_path):
    cfg = AppConfig(
        competition={"name": "t", "market": "cn_futures", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0", "AU0"],
        market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json", "timeout_sec": 0.01}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "cn.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=False), db


def test_stale_quotes_block_decisions(tmp_path):
    eng, db = make_live_engine(tmp_path)
    assert eng._stale_quotes_reason() is not None, "无行情时不应决策"
    eng.quotes["RB0"] = Quote("RB0", 3400.0, ts=time.time() - 3600)
    assert "未更新" in (eng._stale_quotes_reason() or "")
    eng.quotes["RB0"] = Quote("RB0", 3400.0, ts=time.time() - 30)
    assert eng._stale_quotes_reason() is None
    # 主循环：行情源持续返回一小时前的报价 → 不启动决策轮，只记一次告警
    eng.quotes.clear()
    eng.source.quotes = {"RB0": Quote("RB0", 3400.0, ts=time.time() - 3600)}
    eng.start()
    time.sleep(2.5)
    eng.stop()
    assert eng.round_no == 0 and db.decisions(eng.comp_id) == []
    assert "无报价" in eng.status_text
    assert sum("无报价" in e["message"] for e in db.events(eng.comp_id, 50)) == 1


def test_stale_quotes_require_every_active_symbol(tmp_path):
    eng, _ = make_live_engine(tmp_path, symbols=("RB0", "I0"))
    now = time.time()
    eng.quotes = {
        "RB0": Quote("RB0", 3400.0, ts=now - 1),
        "I0": Quote("I0", 800.0, ts=now - 3600),
    }
    reason = eng._stale_quotes_reason()
    assert reason is not None and "I0" in reason
    eng.quotes.pop("I0")
    reason = eng._stale_quotes_reason()
    assert reason is not None and "I0" in reason
    eng.quotes["I0"] = Quote("I0", 800.0, ts=now - 1)
    assert eng._stale_quotes_reason() is None
    eng.stop()


def test_execution_rejects_decision_when_quotes_expire_mid_round(tmp_path):
    eng, db = make_live_engine(tmp_path)
    eng.source.quotes = {"RB0": Quote("RB0", 3400.0, ts=time.time())}
    eng._tick()
    snap = eng._build_snapshot()
    assert snap is not None
    eng.quotes["RB0"] = Quote("RB0", eng.prices["RB0"], ts=time.time() - 3600)
    decision = Decision("ok", 1, time.time(), actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("ok", decision, snap, eng._generation)
    records = db.decisions(eng.comp_id, "ok")
    assert db.trade_count(eng.comp_id, "ok") == 0 and eng.accounts["ok"].positions == {}
    assert len(records) == 1 and records[0]["results"][0]["accepted"] is False
    assert "未更新" in records[0]["results"][0]["reason"]
    eng.stop()


def test_round_skips_before_model_submission_when_snapshot_quotes_expire(tmp_path):
    eng, db = make_live_engine(tmp_path)
    eng.source.quotes = {"RB0": Quote("RB0", 3400.0, ts=time.time())}
    eng._tick()
    eng.quotes["RB0"] = Quote("RB0", eng.prices["RB0"], ts=time.time() - 3600)
    eng._round()
    assert db.decisions(eng.comp_id, "ok") == []
    assert "未更新" in eng.status_text
    eng.stop()


def test_round_rechecks_freshness_after_slow_kline_snapshot(tmp_path):
    eng, db = make_live_engine(tmp_path)
    eng.cfg.competition.tick_interval_sec = 0
    eng.stale_quote_sec = 0.05
    eng.source.quotes = {"RB0": Quote("RB0", 3400.0, ts=time.time() - 0.01)}
    eng._tick()
    real_klines = eng.source.klines

    def slow_klines(symbol, period, limit=120):
        time.sleep(0.06)
        return real_klines(symbol, period, limit)

    eng.source.klines = slow_klines
    eng._round()
    assert db.decisions(eng.comp_id, "ok") == []
    assert "未更新" in eng.status_text
    eng.stop()


def test_future_and_invalid_quotes_are_not_admitted_to_live_rounds(tmp_path):
    eng, _ = make_live_engine(tmp_path)
    now = time.time()
    invalid = (
        Quote("RB0", 0.0, ts=now),
        Quote("RB0", float("nan"), ts=now),
        Quote("RB0", 3400.0, ts=now + 61),
        Quote("RB0", 3400.0, ts=float("nan")),
    )
    for quote in invalid:
        assert eng._stale_quotes_reason({"RB0": quote}, ["RB0"]) is not None
    eng.stop()


def test_invalid_live_quote_cannot_trigger_risk_close_in_accelerated_mode(tmp_path):
    eng, db = make_live_engine(tmp_path, accelerate=True)
    now = time.time()
    eng.source.quotes = {"RB0": Quote("RB0", 3400.0, ts=now)}
    eng._tick()
    acc = eng.accounts["ok"]
    opened = eng.exchange.open(acc, eng.specs["RB0"], "long", 1, 3400.0, 1.0, 3300.0, None, eng.prices, 0)
    assert opened.accepted and opened.trade
    db.save_account(eng.comp_id, acc, trades=[opened.trade])
    accepted_ts = eng.quotes["RB0"].ts

    eng.source.quotes = {"RB0": Quote("RB0", 1.0, ts=now + 3600)}
    eng._tick()

    assert "RB0" in eng.accounts["ok"].positions
    assert db.trade_count(eng.comp_id, "ok") == 1
    assert eng.quotes["RB0"].ts == accepted_ts
    eng.stop()


def test_closed_live_symbol_cannot_trigger_stop_in_accelerated_mode(tmp_path):
    eng, db = make_live_engine(tmp_path, symbols=("RB0", "AU0"), accelerate=True)
    eng.prices["RB0"] = 3400.0
    acc = eng.accounts["ok"]
    opened = eng.exchange.open(acc, eng.specs["RB0"], "long", 1, 3400.0, 1.0, 3300.0, None, eng.prices, 0)
    assert opened.accepted and opened.trade
    db.save_account(eng.comp_id, acc, trades=[opened.trade])

    eng.source.is_open = lambda now, symbol=None: symbol != "RB0"  # type: ignore[method-assign]
    eng.source.quotes = {
        "RB0": Quote("RB0", 3200.0, ts=time.time()),
        "AU0": Quote("AU0", 800.0, ts=time.time()),
    }
    eng._tick()

    assert "RB0" in eng.accounts["ok"].positions
    assert db.trade_count(eng.comp_id, "ok") == 1
    assert "RB0" not in eng.quotes
    eng.stop()


def test_closed_stale_live_quote_is_silent_but_not_admitted(tmp_path, caplog):
    eng, _ = make_live_engine(tmp_path, accelerate=True)
    eng.source.is_open = lambda now, symbol=None: False  # type: ignore[method-assign]
    eng.source.quotes = {"RB0": Quote("RB0", 3400.0, ts=time.time() - 3600)}

    with caplog.at_level(logging.WARNING, logger="arena.engine"):
        eng._tick()

    assert "RB0" not in eng.quotes
    assert not any("Dropping unusable live quote" in record.getMessage() for record in caplog.records)
    eng.stop()


def test_open_stale_live_quote_is_warned_and_not_admitted(tmp_path, caplog):
    eng, _ = make_live_engine(tmp_path, accelerate=True)
    eng.source.quotes = {"RB0": Quote("RB0", 3400.0, ts=time.time() - 3600)}

    with caplog.at_level(logging.WARNING, logger="arena.engine"):
        eng._tick()

    assert "RB0" not in eng.quotes
    assert any("Dropping unusable live quote" in record.getMessage() for record in caplog.records)
    eng.stop()


def test_stale_live_quote_cannot_trigger_stop_in_accelerated_mode(tmp_path):
    eng, db = make_live_engine(tmp_path, accelerate=True)
    eng.prices["RB0"] = 3400.0
    acc = eng.accounts["ok"]
    opened = eng.exchange.open(acc, eng.specs["RB0"], "long", 1, 3400.0, 1.0, 3300.0, None, eng.prices, 0)
    assert opened.accepted and opened.trade
    db.save_account(eng.comp_id, acc, trades=[opened.trade])

    eng.source.quotes = {"RB0": Quote("RB0", 3200.0, ts=time.time() - 3600)}
    eng._tick()

    assert "RB0" in eng.accounts["ok"].positions
    assert db.trade_count(eng.comp_id, "ok") == 1
    assert "RB0" not in eng.quotes
    eng.stop()


def test_live_acceleration_does_not_bypass_quote_freshness(tmp_path):
    eng, db = make_live_engine(tmp_path, accelerate=True)
    eng.cfg.competition.tick_interval_sec = 0
    eng.stale_quote_sec = 0.05
    eng.quotes = {"RB0": Quote("RB0", 3400.0, ts=time.time() - 1)}
    eng._round()
    assert db.decisions(eng.comp_id, "ok") == []
    assert "未更新" in eng.status_text
    eng.stop()


def test_nonaccelerated_replay_uses_its_own_clock_without_freshness_rejection(tmp_path):
    eng, db = make_engine(tmp_path, accelerate=False, tick=20)
    for _ in range(8):
        eng._tick()
    assert eng._stale_quotes_reason() is None
    eng._round()
    assert len(db.decisions(eng.comp_id, "ok")) == 1
    eng.stop()


def test_cn_freshness_only_requires_currently_tradable_symbols(tmp_path):
    eng, _ = make_cn_session_engine(tmp_path)
    active = eng._active_symbols(datetime(2026, 9, 29, 23, 30))
    now = time.time()
    quotes = {
        "RB0": Quote("RB0", 3400.0, ts=now - 3600),
        "AU0": Quote("AU0", 800.0, ts=now - 1),
    }
    assert active == ["AU0"]
    assert eng._stale_quotes_reason(quotes, active) is None
    eng.stop()


def test_round_snapshot_excludes_closed_symbols_and_execution_rejects_them(tmp_path):
    eng, db = make_live_engine(tmp_path, symbols=("RB0", "AU0"))
    eng.source.quotes = {
        "RB0": Quote("RB0", 3400.0, ts=time.time()),
        "AU0": Quote("AU0", 800.0, ts=time.time()),
    }
    eng._tick()
    eng._active_symbols = lambda now=None: ["AU0"]  # type: ignore[method-assign]
    eng._round()
    round_record = db.decisions(eng.comp_id, "ok", with_prompt=True)[0]
    assert "AU0(" in round_record["prompt"] and "RB0(" not in round_record["prompt"]

    snap = eng._build_snapshot(symbols=["AU0"])
    assert snap is not None and set(snap.symbols) == {"AU0"}
    decision = Decision("ok", 2, time.time(), actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("ok", decision, snap, eng._generation)
    records = db.decisions(eng.comp_id, "ok")
    assert db.trade_count(eng.comp_id, "ok") == 0
    assert records[0]["results"][0]["accepted"] is False
    assert "休市" in records[0]["results"][0]["reason"]
    eng.stop()


def test_calendar_expiry_warns_once_per_day(tmp_path):
    eng, db = make_engine(tmp_path, accelerate=False)
    assert eng.calendar_valid_until == date(2027, 1, 1)
    eng.calendar_valid_until = date(2020, 1, 1)
    eng._check_calendar_expiry()
    eng._check_calendar_expiry()
    assert sum("日历" in e["message"] for e in db.events(eng.comp_id, 50)) == 1
    eng.stop()


def test_calendar_meta_and_verified_boundaries():
    path = ROOT / "config" / "contracts_cn.json"
    assert load_calendar_valid_until(path) == date(2027, 1, 1)
    specs, holidays = load_cn_contracts(path)
    assert "2026-02-23" in holidays and "2026-10-08" not in holidays and "2027-01-01" in holidays
    assert not session_open(specs["RB0"], datetime(2026, 2, 23, 10, 0), holidays)
    assert not session_open(specs["RB0"], datetime(2026, 2, 20, 21, 30), holidays)
    assert session_open(specs["RB0"], datetime(2026, 10, 8, 10, 0), holidays)
    assert not session_open(specs["RB0"], datetime(2026, 12, 31, 21, 30), holidays)
    assert not session_open(specs["RB0"], datetime(2027, 1, 1, 10, 0), holidays)


def test_round_status_resets_when_round_aborts(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()

    def boom(*a, **k):
        raise RuntimeError("kline outage")

    eng._build_snapshot = boom  # type: ignore[method-assign]
    eng._round_guarded()          # 快照失败 → 本轮放弃
    assert not eng.status_text.startswith("第 ")
    eng._tick()
    assert eng.status_text == "运行中"
    eng.stop()


def test_crypto_latest_returns_prices_without_waiting_for_enrichment():
    src = CryptoSource(["BTC-USDT-SWAP"], provider="okx", timeout=1)
    src.enrich_interval = 0.0

    def fake_get(url, params):
        if "market/tickers" in url:
            return {"data": [{"instId": "BTC-USDT-SWAP", "last": "50000", "open24h": "49000", "ts": str(int(time.time() * 1000))}]}
        if "funding-rate" in url:
            time.sleep(1.5)   # 慢的可选端点
            return {"data": [{"fundingRate": "0.0001"}]}
        if "open-interest" in url:
            return {"data": [{"oi": "12345"}]}
        raise AssertionError(url)

    src._get = fake_get  # type: ignore[method-assign]
    t0 = time.time()
    q = src.latest(["BTC-USDT-SWAP"])
    assert time.time() - t0 < 0.5, "价格返回不应等待增强数据"
    assert q["BTC-USDT-SWAP"].price == 50000 and q["BTC-USDT-SWAP"].funding_rate is None
    src._enrich_thread.join(timeout=3)
    q2 = src.latest(["BTC-USDT-SWAP"])
    assert q2["BTC-USDT-SWAP"].funding_rate == 0.0001 and q2["BTC-USDT-SWAP"].open_interest == 12345
