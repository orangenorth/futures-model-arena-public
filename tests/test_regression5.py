"""第五轮审查：CSV 非有限数值、停止时放弃在飞决策、节前夜盘判断。"""
import threading
import time
from datetime import datetime
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_cn_contracts, session_open
from arena.market.replay import ReplaySource

ROOT = Path(__file__).resolve().parents[1]


def test_replay_csv_skips_non_finite_rows(tmp_path, cn_specs):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    rows = ["datetime,open,high,low,close,volume",
            "2026-09-16 09:00:00,3400,3410,3390,3401,100",
            "2026-09-16 09:01:00,NaN,3410,3390,3402,100",
            "2026-09-16 09:02:00,3400,Infinity,3390,3403,100",
            "2026-09-16 09:03:00,3400,3410,3390,3404,100",
            "2026-09-16 09:04:00,3400,3410,3390,NaN,100"]
    (csv_dir / "RB0.csv").write_text("\n".join(rows), encoding="utf-8")
    src = ReplaySource(["RB0"], cn_specs, seed=1, csv_dir=str(csv_dir), history=300)
    kept = src.klines("RB0", "1m", 10)
    assert [b.close for b in kept] == [3401, 3404]
    for _ in range(5):   # 最后一根有效 bar 之后继续合成，不再抛异常
        q = src.latest(["RB0"])
        assert q["RB0"].price > 0


def test_night_session_before_holiday_is_closed():
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    rb, au = specs["RB0"], specs["AU0"]
    assert "2026-10-01" in holidays and "2026-04-06" in holidays
    assert session_open(rb, datetime(2026, 9, 29, 21, 30), holidays)          # 普通周二夜盘
    assert not session_open(rb, datetime(2026, 9, 30, 21, 30), holidays)      # 国庆前夜，23:00 前收盘的夜盘也休
    assert not session_open(au, datetime(2026, 9, 30, 22, 0), holidays)       # 跨日夜盘同样休
    assert not session_open(au, datetime(2026, 10, 1, 1, 0), holidays)
    assert not session_open(rb, datetime(2026, 4, 3, 21, 30), holidays)       # 周五夜盘，下一工作日（周一）为清明假期
    assert session_open(rb, datetime(2026, 4, 3, 10, 0), holidays)            # 当天日盘正常
    assert session_open(rb, datetime(2026, 9, 18, 21, 30), holidays)          # 普通周五夜盘正常


def test_stop_abandons_in_flight_decisions(tmp_path):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 30, "max_retries": 0},
        traders=[TraderCfg(id="slow", name="SLOW", provider="mock", mock_response="__sleep__:4")],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    th = threading.Thread(target=eng._round, daemon=True)
    th.start()
    time.sleep(0.8)                       # 决策在线程池中进行
    t0 = time.time()
    eng.stop()
    assert time.time() - t0 < 3.0, "stop() 不应等待在飞的决策完成"
    th.join(timeout=3)
    assert not th.is_alive()
    assert db.decisions(eng.comp_id, "slow") == [], "停止后完成的决策不应落库"
    assert eng.pool._shutdown
