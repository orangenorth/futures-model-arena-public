"""第十七轮审查（引擎部分）：重置/暂停后立即放弃在飞行中的决策轮；自动补的多头止损必须为正；回放成交按行情时间戳记账。"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arena.config import AppConfig, RiskCfg, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.engine.risk import RiskManager
from arena.models import Account, ActionRequest, Decision

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_engine(tmp_path, mock_response=OPEN_RB, csv_dir=None):
    replay = {"seed": 3, "start_price": {"RB0": 3400}}
    if csv_dir:
        replay["csv_dir"] = str(csv_dir)
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": replay, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=mock_response)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


# ---------- 重置 / 暂停后放弃在飞行中的决策轮 ----------
def test_reset_abandons_in_flight_round_and_frees_worker_capacity(tmp_path):
    eng, db = make_engine(tmp_path, mock_response="__sleep__:2.5")   # 模型 2.5s 才返回
    eng._tick()
    old_cid = eng.comp_id
    old_pool = eng.pool
    th = threading.Thread(target=eng._round_guarded, daemon=True)
    th.start()
    time.sleep(0.4)                       # 模型调用已提交并在睡眠
    t0 = time.time()
    eng.reset()
    th.join(timeout=2.0)
    assert not th.is_alive(), "重置后决策线程应在下一次轮询就退出，而不是等到模型返回/超时"
    assert time.time() - t0 < 1.8
    assert eng._start_round_thread() is True, "新竞赛应能立即开始新一轮"
    assert eng.pool is not old_pool and old_pool._shutdown, "旧竞赛的模型调用不应占用新竞赛的线程池"
    time.sleep(3.2)                       # 旧调用返回后：其结果不得写入任何竞赛
    assert db.decisions(old_cid) == []
    eng.stop()


def test_pause_abandons_in_flight_round(tmp_path):
    eng, db = make_engine(tmp_path, mock_response="__sleep__:2.5")
    eng._tick()
    th = threading.Thread(target=eng._round_guarded, daemon=True)
    th.start()
    time.sleep(0.4)
    t0 = time.time()
    eng.pause(True)
    th.join(timeout=2.0)
    assert not th.is_alive() and time.time() - t0 < 1.8
    assert eng.status_text == "已暂停"
    eng.pause(False)
    eng.stop()
    assert db.decisions(eng.comp_id) == []


# ---------- 自动补止损必须为正且低于现价 ----------
def test_auto_long_stop_is_clamped_positive(cn_specs):
    rm = RiskManager(RiskCfg(auto_stop_atr_mult=2.0), cn_specs)
    acc = Account(trader_id="t", cash=1_000_000, initial_cash=1_000_000, peak_equity=1_000_000)
    v = rm.check(acc, ActionRequest("RB0", "open_long", 0.1), price=100.0, prices={"RB0": 100.0}, atr=80.0)   # 2×ATR = 160 ≥ 现价
    assert v.ok and 0 < v.action.stop_loss < 100.0
    assert v.action.stop_loss == 50.0, "距离最多为现价的一半"
    v2 = rm.check(acc, ActionRequest("RB0", "open_long", 0.1), price=100.0, prices={"RB0": 100.0}, atr=10.0)
    assert v2.ok and v2.action.stop_loss == 80.0                       # 正常情况不受影响
    v3 = rm.check(acc, ActionRequest("RB0", "open_short", 0.1), price=100.0, prices={"RB0": 100.0}, atr=80.0)
    assert v3.ok and v3.action.stop_loss == 260.0                      # 空头止损天然为正
    tick = cn_specs["RB0"].tick
    v4 = rm.check(acc, ActionRequest("RB0", "open_long", 0.1), price=tick, prices={"RB0": tick}, atr=80.0)   # 价格低到无法放下一个 tick 的止损
    assert not v4.ok and "止损" in v4.reason


# ---------- 回放成交按行情时间戳 ----------
def test_replay_fills_use_replay_clock(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    lines = ["datetime,open,high,low,close,volume"] + [f"2024-03-0{d}T09:{i:02d}:00,3400,3410,3390,{3400 + i},100" for d in (1, 2) for i in range(5)]
    (csv_dir / "RB0.csv").write_text("\n".join(lines), encoding="utf-8")
    eng, db = make_engine(tmp_path, csv_dir=csv_dir)
    eng._tick()
    q_ts = eng.quotes["RB0"].ts
    assert datetime.fromtimestamp(q_ts, ZoneInfo("Asia/Shanghai")).year == 2024
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    t_open = db.trades(eng.comp_id, "ok", limit=1)[0]
    assert t_open["ts"] == q_ts, "回放成交必须记在行情时间戳，而不是墙钟"
    assert abs(eng.accounts["ok"].positions["RB0"].opened_at - q_ts) < 1e-6
    hold = [p for p in eng.state()["traders"][0]["positions"]][0]["hold_min"]
    assert 0 <= hold < 60, "持仓时长按回放时钟计算，而不是与今天的差"
    eng.accounts["ok"].positions["RB0"].stop_loss = 1e9    # 下个 tick 触发止损
    eng._tick()
    t_sl = db.trades(eng.comp_id, "ok", limit=1)[0]
    assert t_sl["action"] == "stop_loss" and t_sl["ts"] == eng.quotes["RB0"].ts and t_sl["ts"] > q_ts
    eng.stop()


def test_live_source_keeps_wall_clock(tmp_path, monkeypatch):
    eng, db = make_engine(tmp_path)
    eng._tick()
    monkeypatch.setattr(eng, "_requires_live_quote_freshness", lambda: True)   # 视作实时源
    assert eng._exec_clock() is None and abs(eng._now() - time.time()) < 1.0
    eng.stop()
