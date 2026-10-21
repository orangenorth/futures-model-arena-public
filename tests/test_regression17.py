"""Regressions for exchange-timezone boundaries and unsafe market-data ingress."""
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_cn_contracts
from arena.market.crypto import _make_bar as make_crypto_bar
from arena.market.sina_cn import SinaFuturesSource
from arena.models import Bar, Quote


ROOT = Path(__file__).resolve().parents[1]
UTC = ZoneInfo("UTC")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_sina(symbols=("RB0",)):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return SinaFuturesSource(list(symbols), specs, holidays, timeout=0.1)


def make_cfg(tmp_path, timezone="Asia/Shanghai"):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1,
                     "snapshot_interval_sec": 1, "timezone": timezone},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock")],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


def test_cn_session_uses_exchange_timezone_and_returns_next_open_in_caller_timezone():
    src = make_sina()
    try:
        assert src.is_open(datetime(2026, 9, 29, 1, 30, tzinfo=UTC), "RB0")
        assert not src.is_open(datetime(2026, 9, 29, 9, 30, tzinfo=UTC), "RB0")
        assert src.next_open(datetime(2026, 9, 29, 9, 30, tzinfo=UTC)) == datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
    finally:
        src.close()


def test_legacy_competition_without_timezone_isolated_from_new_timezone(tmp_path):
    cfg = make_cfg(tmp_path, "UTC")
    db = Database(cfg.db_path)
    legacy_id = db.create_competition(
        "legacy", "replay", 1_000_000, json.dumps({"competition": {"market": "replay"}})
    )
    engine = Engine(cfg, db, accelerate=True)
    try:
        assert engine.comp_id != legacy_id
        old = next(comp for comp in db.list_competitions() if comp["id"] == legacy_id)
        assert old["status"] == "ended"
        assert any("Asia/Shanghai" in event["message"] for event in db.events(legacy_id, 20))
    finally:
        engine.stop()
        db.close()


def test_sources_reject_boolean_prices_and_future_bars():
    src = make_sina()
    now = time.time()
    future_text = datetime.fromtimestamp(now + 3600, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    try:
        assert src._parse_minute([
            ["2026-09-16 09:01:00", True, "3410", "3390", "3405", "1", "1"],
            ["2026-09-16 09:02:00", "3400", "3410", "3390", False, "1", "1"],
            [future_text, "3400", "3410", "3390", "3405", "1", "1"],
        ]) == []
    finally:
        src.close()

    now_ms = int(now * 1000)
    assert make_crypto_bar([now_ms, "50000", "51000", "49000", True, "10"]) is None
    assert make_crypto_bar([now_ms, "50000", "51000", "49000", False, "10"]) is None
    assert make_crypto_bar([now_ms + 3_600_000, "50000", "51000", "49000", "50500", "10"]) is None


def test_engine_drops_boolean_live_quote_before_risk_mark(tmp_path):
    cfg = make_cfg(tmp_path)
    engine = Engine(cfg, Database(cfg.db_path), accelerate=True)
    engine.prices["RB0"] = 3400
    account = engine.accounts["ok"]
    opened = engine.exchange.open(
        account, engine.specs["RB0"], "long", 1, 3400, 1.0, 3300, None, engine.prices, 0
    )
    assert opened.accepted and opened.trade
    engine.db.save_account(engine.comp_id, account, trades=[opened.trade])

    class BooleanQuoteSource:
        def latest(self, symbols):
            return {"RB0": Quote("RB0", True, time.time())}

        def is_open(self, when, symbol=None):
            return True

    engine.source = BooleanQuoteSource()
    try:
        engine._tick()
        assert "RB0" in account.positions
        assert engine.prices["RB0"] == 3400
        assert "RB0" not in engine.quotes
    finally:
        engine.stop()
        engine.db.close()


def test_live_snapshot_discards_future_bars_before_indicator_calculation(tmp_path):
    cfg = make_cfg(tmp_path)
    engine = Engine(cfg, Database(cfg.db_path), accelerate=True)
    now = time.time()

    class FutureBarSource:
        def is_open(self, when, symbol=None):
            return True

        def klines(self, symbol, period, limit=120):
            return [
                Bar(ts=now - 600, open=3400, high=3410, low=3390, close=3400, volume=10),
                # 仍在形成中的 bar（开盘 60 秒，收盘时刻在未来）同样不进入指标
                Bar(ts=now - 60, open=3405, high=3406, low=3404, close=3405, volume=10),
                Bar(ts=now + 3600, open=99999, high=100000, low=99998, close=99999, volume=10),
            ]

    engine.source = FutureBarSource()
    try:
        snap = engine._build_snapshot({"RB0": Quote("RB0", 3400, now)}, round_no=1)
        assert snap is not None
        view = snap.symbols["RB0"]
        assert view.ind_5m["last"] == 3400
        assert all(bar.ts + 300 <= now for bar in view.bars_5m)
    finally:
        engine.stop()
        engine.db.close()
