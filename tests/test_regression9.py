"""第九轮审查：慢决策轮不阻塞盯市；回放 CSV 时间按配置时区解析。"""
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.market.replay import ReplaySource

ROOT = Path(__file__).resolve().parents[1]


def test_stop_loss_fires_while_slow_round_is_in_flight(tmp_path):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 30, "max_retries": 0},
        traders=[TraderCfg(id="slow", name="SLOW", provider="mock", mock_response="__sleep__:4")],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    acc = eng.accounts["slow"]
    spec = eng.specs["RB0"]
    px = eng.prices["RB0"]
    r = eng.exchange.open(acc, spec, "long", 1, px, 1.0, px * 2, None, eng.prices, 0)   # 止损设在必然触发的位置
    assert r.accepted
    db.save_account(eng.comp_id, acc, trades=[r.trade])
    assert db.trade_count(eng.comp_id, "slow") == 1

    eng.start()   # 主循环立即启动一轮 4 秒的慢决策
    deadline = time.time() + 3.0
    fired = False
    while time.time() < deadline:
        if db.trade_count(eng.comp_id, "slow") >= 2:
            fired = True
            break
        time.sleep(0.2)
    round_alive = eng._round_thread is not None and eng._round_thread.is_alive()
    eng.stop()
    assert fired, "慢决策轮进行期间止损仍应被盯市触发"
    assert round_alive, "止损触发时决策轮应仍在进行（证明盯市未被阻塞）"
    assert "RB0" not in eng.accounts["slow"].positions


def test_round_thread_pipelined_skips_inflight_trader(tmp_path):
    """F-A2 新语义：上一轮未结束时仍可开新一轮（流水线），但在途的交易员本轮跳过；
    慢模型不再推迟整轮、绑架全场决策频率。"""
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 30, "max_retries": 0},
        traders=[TraderCfg(id="slow", name="SLOW", provider="mock", mock_response="__sleep__:2")],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    assert eng._start_round_thread() is True
    # 等第 1 轮把 slow 的决策提交出去（在途槽位已占用）
    deadline = time.time() + 5.0
    while eng._inflight.get("slow") != 1 and time.time() < deadline:
        time.sleep(0.1)
    assert eng._inflight.get("slow") == 1
    # slow 的第 1 轮决策（sleep 2s）仍在途：第 2 轮照常启动，但 slow 本轮被跳过
    assert eng._start_round_thread() is True
    deadline = time.time() + 5.0
    while eng.round_no != 2 and time.time() < deadline:
        time.sleep(0.1)
    assert eng.round_no == 2
    assert eng._inflight.get("slow") == 1, "慢交易员仍占用第 1 轮的在途槽位，不应被第 2 轮重复提交"
    eng.stop()
    # 停止后在途槽位应被释放（后台线程需要一点时间看到停止信号，轮询等待）
    deadline = time.time() + 5.0
    while eng._inflight and time.time() < deadline:
        time.sleep(0.1)
    assert eng._inflight == {}


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="需要 time.tzset")
def test_replay_csv_naive_times_use_configured_tz(tmp_path, cn_specs, monkeypatch):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    rows = ["datetime,open,high,low,close,volume",
            "2026-09-16 09:00:00,3400,3410,3390,3401,100",            # 无偏移 → 按配置时区
            "2026-09-16 09:01:00+00:00,3400,3410,3390,3402,100",      # 显式 UTC 偏移 → 保留
            "1789600000,3400,3410,3390,3403,100"]                       # 秒级时间戳 → 原样
    (csv_dir / "RB0.csv").write_text("\n".join(rows), encoding="utf-8")
    expected = [
        datetime(2026, 9, 16, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(),
        datetime(2026, 9, 16, 9, 1, tzinfo=ZoneInfo("UTC")).timestamp(),
        1789600000.0,
    ]
    for host_tz in ("UTC", "America/New_York", "Asia/Shanghai"):
        monkeypatch.setenv("TZ", host_tz)
        time.tzset()
        src = ReplaySource(["RB0"], cn_specs, seed=1, csv_dir=str(csv_dir), history=300, tz="Asia/Shanghai")
        got = sorted(b.ts for b in src.klines("RB0", "1m", 10))
        assert got == sorted(expected), host_tz
    monkeypatch.delenv("TZ")
    time.tzset()
