"""第十五轮审查：实时行情解析拒绝非有限数/非正价格；导出时间按竞赛时区而非主机时区。"""
import csv
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_cn_contracts
from arena.market.crypto import CryptoSource
from arena.market.sina_cn import SinaFuturesSource
from arena.models import ActionRequest, Decision
from arena.server.api import create_app

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})
SH = ZoneInfo("Asia/Shanghai")


def make_engine(tmp_path):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


# ---------- 实时行情解析 ----------
def test_sina_parser_rejects_non_finite_and_non_positive_rows():
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = SinaFuturesSource(["RB0"], specs, holidays)
    rows = [
        ["2026-09-16 09:01:00", "NaN", "3410", "3390", "3405", "1", "1"],
        ["2026-09-16 09:02:00", "3400", "Infinity", "3390", "3405", "1", "1"],
        ["2026-09-16 09:03:00", "3400", "3410", "-Infinity", "3405", "1", "1"],
        ["2026-09-16 09:04:00", "3400", "3410", "3390", "0", "1", "1"],
        ["2026-09-16 09:05:00", "3400", "3410", "3390", "NaN", "1", "1"],
        ["2026-09-16 09:06:00", "3400", "3410", "3390", "3405", "NaN", "Infinity"],   # 量/持仓异常只清零，不丢价格
        ["2026-09-16 09:07:00", "3400", "3410", "3390", "3405", "120", "100000"],
    ]
    bars = src._parse_minute(rows)
    assert [b.close for b in bars] == [3405.0, 3405.0]
    assert bars[0].volume == 0.0 and bars[0].hold == 0.0 and bars[1].volume == 120.0
    daily = src._parse_daily([["2026-09-15", "3380", "3420", "3370", "Infinity", "5000", "1", "3401"],
                              ["2026-09-16", "3380", "3420", "3370", "3400", "5000", "1", "3401"]])
    assert [b.close for b in daily] == [3400.0]
    # 全部非法 → klines 报无数据，而不是发布 0 价/NaN 报价
    src._get = lambda method, params, attempts=3: [["2026-09-16 09:05:00", "3400", "3410", "3390", "NaN", "1", "1"]]  # type: ignore[method-assign]
    assert src.latest(["RB0"]) == {}
    src.close()


def test_sina_lightweight_quote_payload_normalizes_before_numeric_validation():
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = SinaFuturesSource(["RB0"], specs, holidays)
    try:
        # 字段 13 是持仓量、14 是成交量（见 tests/test_regression59.py 的专项用例）
        text = ('var hq_str_nf_RB0="螺纹钢连续,090501,3400,3410,3390,0,0,0,3405,0,'
                '3395,0,0,2200000,1500000,0,0,20260916";')
        bars = src._parse_minute(src._spot_to_minute_rows(text, "RB0"))
        assert len(bars) == 1
        assert bars[0].close == 3405.0
        assert bars[0].volume == 1500000.0
        assert bars[0].hold == 2200000.0
        assert datetime.fromtimestamp(bars[0].ts, SH).strftime("%Y-%m-%d %H:%M:%S") == "2026-09-16 09:05:01"
    finally:
        src.close()


def test_sina_lightweight_quote_change_uses_prev_settle_not_today_open():
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = SinaFuturesSource(["RB0"], specs, holidays)
    try:
        # Sina's live nf payload places today's open at index 2, last at index
        # 8, and prior settlement at index 10.  They deliberately differ here.
        text = ('var hq_str_nf_RB0="螺纹钢连续,090501,3400,3410,3390,0,0,0,3405,0,'
                '3395,0,0,120,100000,0,0,20260916";')
        rows = src._spot_to_minute_rows(text, "RB0")
        src._get = lambda method, params, attempts=3: rows  # type: ignore[method-assign]
        quote = src._fetch_quote("RB0")
        assert quote.change_pct == pytest.approx((3405 / 3395 - 1) * 100)
        assert quote.change_pct != pytest.approx((3405 / 3400 - 1) * 100)

        # A blank prior-settlement field, or a legacy normalized row without
        # that field, is unknown rather than a reason to reuse today's open.
        bad_rows = src._spot_to_minute_rows(text.replace(",3395,", ",NaN,"), "RB0")
        src._get = lambda method, params, attempts=3: bad_rows  # type: ignore[method-assign]
        assert src._fetch_quote("RB0").change_pct == 0.0
        legacy_rows = [rows[0][:7]]
        src._get = lambda method, params, attempts=3: legacy_rows  # type: ignore[method-assign]
        assert src._fetch_quote("RB0").change_pct == 0.0
    finally:
        src.close()


def test_crypto_parser_rejects_non_finite_and_non_positive_prices():
    src = CryptoSource(["BTC-USDT-SWAP", "ETH-USDT-SWAP"], provider="okx", timeout=1)
    src.enrich_interval = 1e12   # 本测试不触发后台增强数据线程
    now_ms = str(int(time.time() * 1000))

    def fake_get(url, params):
        if "market/tickers" in url:
            return {"data": [{"instId": "BTC-USDT-SWAP", "last": "NaN", "open24h": "49000", "ts": now_ms},
                             {"instId": "ETH-USDT-SWAP", "last": "3000", "open24h": "Infinity", "ts": now_ms}]}
        if "market/candles" in url:
            return {"data": [[now_ms, "50000", "51000", "49000", "Infinity", "10"],
                             [now_ms, "50000", "51000", "49000", "0", "10"],
                             [str(int(now_ms) - 60000), "50000", "51000", "49000", "50500", "NaN"]]}
        raise AssertionError(url)

    src._get = fake_get  # type: ignore[method-assign]
    q = src.latest(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    assert set(q) == {"ETH-USDT-SWAP"}, "NaN 最新价的品种必须跳过"
    assert q["ETH-USDT-SWAP"].price == 3000 and q["ETH-USDT-SWAP"].change_pct == 0.0   # open24h 非法 → 以最新价代替
    bars = src.klines("BTC-USDT-SWAP", "5m", 10)
    assert [b.close for b in bars] == [50500.0] and bars[0].volume == 0.0


# ---------- 导出时间按竞赛时区 ----------
@pytest.mark.skipif(not hasattr(time, "tzset"), reason="需要 time.tzset")
def test_exports_format_times_in_competition_timezone(tmp_path, monkeypatch):
    eng, db = make_engine(tmp_path)
    eng._tick()
    eng._snapshot()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    trade_ts = db.trades(eng.comp_id, "ok", limit=1)[0]["ts"]
    eq_ts = db.equity_series(eng.comp_id)["ok"][0][0]
    expected_trade = datetime.fromtimestamp(trade_ts, SH).strftime("%Y-%m-%d %H:%M:%S")
    expected_eq = datetime.fromtimestamp(eq_ts, SH).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setenv("TZ", "UTC")   # 无头服务器常见：主机 UTC，竞赛 Asia/Shanghai
    time.tzset()
    try:
        host_trade = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trade_ts))
        assert host_trade != expected_trade
        client = TestClient(create_app(eng, ROOT / "web"))
        rows = list(csv.DictReader(io.StringIO(client.get("/api/export/trades.csv").text.lstrip("﻿"))))
        assert rows and rows[0]["time"] == expected_trade
        eq_rows = list(csv.DictReader(io.StringIO(client.get("/api/export/equity.csv").text.lstrip("﻿"))))
        assert eq_rows and eq_rows[0]["time"] == expected_eq
        eng.stop()
        # 离线导出：使用竞赛创建时持久化的时区
        sys.path.insert(0, str(ROOT / "scripts"))
        import export_results  # noqa: E402
        monkeypatch.setattr(sys, "argv", ["export_results.py", "--db", str(tmp_path / "e.db"), "--out", str(tmp_path / "results")])
        export_results.main()
        with open(tmp_path / "results" / "trades.csv", encoding="utf-8-sig") as f:
            assert list(csv.DictReader(f))[0]["time"] == expected_trade
        with open(tmp_path / "results" / "equity.csv", encoding="utf-8-sig") as f:
            assert list(csv.DictReader(f))[0]["time"] == expected_eq
        assert export_results.competition_tz({"config_json": None}).key == "Asia/Shanghai"
        assert export_results.competition_tz({"config_json": json.dumps({"competition": {"timezone": "UTC"}})}).key == "UTC"
        assert export_results.competition_tz({"config_json": json.dumps({"competition": {"timezone": "Mars/Olympus"}})}).key == "Asia/Shanghai"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
