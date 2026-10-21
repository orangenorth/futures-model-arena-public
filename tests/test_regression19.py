"""第十九轮审查：回放位置跨重启持久化；日汇总以上一日最后快照为新一天基线。"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision

ROOT = Path(__file__).resolve().parents[1]
SH = ZoneInfo("Asia/Shanghai")
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_cfg(tmp_path, csv_dir=None, history=300):
    replay = {"seed": 3, "start_price": {"RB0": 3400}, "history": history}
    if csv_dir:
        replay["csv_dir"] = str(csv_dir)
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": replay, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


def fmt(ts):
    return datetime.fromtimestamp(ts, SH).strftime("%Y-%m-%d %H:%M")


def test_csv_replay_resumes_from_persisted_position(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    lines = ["datetime,open,high,low,close,volume"] + [f"2024-03-01T09:{m:02d}:00,3400,3410,3390,{3400 + m},100" for m in range(30)]
    (csv_dir / "RB0.csv").write_text("\n".join(lines), encoding="utf-8")
    cfg = make_cfg(tmp_path, csv_dir, history=5)      # 前 5 行为历史，之后逐 tick 回放
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    for _ in range(6):
        eng._tick()
    assert fmt(eng.quotes["RB0"].ts) == "2024-03-01 09:10"
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    open_ts = db.trades(eng.comp_id, "ok", limit=1)[0]["ts"]
    cid = eng.comp_id
    eng.stop()
    db.close()

    db2 = Database(cfg.db_path)
    eng2 = Engine(make_cfg(tmp_path, csv_dir, history=5), db2, accelerate=True)
    assert eng2.comp_id == cid and "RB0" in eng2.accounts["ok"].positions
    eng2._tick()
    assert fmt(eng2.quotes["RB0"].ts) == "2024-03-01 09:11", "重启后应从上次位置继续，而不是再放一遍 09:05"
    assert eng2.quotes["RB0"].ts > open_ts
    eng2.accounts["ok"].positions["RB0"].stop_loss = 1e9    # 重启后触发的平仓必须晚于重启前的开仓
    eng2._tick()
    trades = db2.trades(cid, "ok", limit=5)
    sl = next(t for t in trades if t["action"] == "stop_loss")
    assert sl["ts"] > open_ts and fmt(sl["ts"]) == "2024-03-01 09:12"
    eng2.stop()
    db2.close()


def test_csv_replay_resume_after_exhaustion_continues_forward(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    lines = ["datetime,open,high,low,close,volume"] + [f"2024-03-01T09:{m:02d}:00,3400,3410,3390,{3400 + m},100" for m in range(5)]
    (csv_dir / "RB0.csv").write_text("\n".join(lines), encoding="utf-8")
    cfg = make_cfg(tmp_path, csv_dir, history=5)      # CSV 立即用尽 → 合成续接
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    for _ in range(10):
        eng._tick()
    last_ts, last_px = eng.quotes["RB0"].ts, eng.quotes["RB0"].price
    assert fmt(last_ts) == "2024-03-01 09:14"
    eng.stop()
    db.close()
    db2 = Database(cfg.db_path)
    eng2 = Engine(make_cfg(tmp_path, csv_dir, history=5), db2, accelerate=True)
    eng2._tick()
    q = eng2.quotes["RB0"]
    assert fmt(q.ts) == "2024-03-01 09:15", "合成续接阶段重启：时间从上次时刻继续，不倒退"
    assert abs(q.price / last_px - 1) < 0.05, "价格从上次收盘衔接"
    eng2.stop()
    db2.close()


def test_synthetic_replay_resumes_timeline_and_price(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    for _ in range(20):
        eng._tick()
    last_ts, last_px = eng.quotes["RB0"].ts, eng.quotes["RB0"].price
    eng.stop()
    db.close()
    db2 = Database(cfg.db_path)
    eng2 = Engine(make_cfg(tmp_path), db2, accelerate=True)
    hist = eng2.source.klines("RB0", "1m", 5)
    assert hist[-1].ts == last_ts and abs(hist[-1].close - last_px) < 1e-6, "历史应平移/缩放到上次的时刻与价格"
    eng2._tick()
    q = eng2.quotes["RB0"]
    assert q.ts == last_ts + 60 and abs(q.price / last_px - 1) < 0.05
    eng2.stop()
    db2.close()


def test_reset_does_not_rewind_replay(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    for _ in range(5):
        eng._tick()
    before = eng.quotes["RB0"].ts
    eng.reset()
    eng._tick()
    assert eng.quotes["RB0"].ts == before + 60, "重置新建竞赛，但回放时间轴继续前进"
    eng.stop()


def test_daily_summary_carries_previous_days_last_snapshot_as_baseline(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    today = datetime.fromtimestamp(eng._now(), eng.tz).strftime("%Y-%m-%d")
    # 模拟：上一日（2020-01-01）最后一次快照时权益 999_000、0 笔成交、0 手续费；之后跨日期间发生了一笔开仓
    eng._day_start["ok"] = {"day": "2020-01-01", "equity": 1_000_000, "trades": 0, "fees": 0.0,
                            "last_equity": 999_000.0, "last_trades": 0, "last_fees": 0.0}
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng._snapshot()      # 新一天的第一次快照
    row = next(d for d in db.daily(eng.comp_id, 10) if d["day"] == today)
    assert row["start_equity"] == 999_000.0, "新一天的基线应是上一日最后一次快照的权益"
    assert row["trades"] == 1 and row["fees"] > 0, "跨日期间的成交与手续费计入新的一天"
    eq = eng.accounts["ok"].equity(eng.prices, eng.specs)
    assert abs(row["pnl"] - (eq - 999_000.0)) < 1e-6
    # 基线持久化：重启后（仍是同一天）沿用，不会被本次快照的值覆盖
    saved = json.loads(db.kv_get(f"day_start:{eng.comp_id}"))["ok"]
    assert saved["equity"] == 999_000.0 and saved["last_trades"] == 1
    eng.stop()
