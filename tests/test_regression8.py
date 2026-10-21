"""第八轮审查：模型任务提交与暂停检查同锁；新浪时间戳按交易所时区解析。"""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_cn_contracts
from arena.market.sina_cn import SinaFuturesSource

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def test_model_submission_happens_under_engine_lock(tmp_path):
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
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    real_submit = eng.pool.submit
    seen = {"held": None}

    def submit_checking_lock(*a, **k):
        seen["held"] = eng.lock._is_owned()   # 提交时必须持有引擎锁，pause() 无法插入
        return real_submit(*a, **k)

    eng.pool.submit = submit_checking_lock  # type: ignore[method-assign]
    eng._round()
    assert seen["held"] is True
    assert "RB0" in eng.accounts["ok"].positions
    eng.stop()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="需要 time.tzset")
def test_sina_timestamps_are_exchange_local_regardless_of_host_tz(monkeypatch):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = SinaFuturesSource(["RB0"], specs, holidays)
    minute_rows = [["2026-09-16 09:05:00", "3400", "3410", "3390", "3405", "120", "100000"]]
    daily_rows = [["2026-09-15", "3380", "3420", "3370", "3400", "5000", "100000", "3401"]]
    expected_min = datetime(2026, 9, 16, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    expected_day = datetime(2026, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    for host_tz in ("UTC", "America/New_York", "Asia/Shanghai"):
        monkeypatch.setenv("TZ", host_tz)
        time.tzset()
        assert src._parse_minute(minute_rows)[0].ts == expected_min, host_tz
        assert src._parse_daily(daily_rows)[0].ts == expected_day, host_tz
    monkeypatch.delenv("TZ")
    time.tzset()
