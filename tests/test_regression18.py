"""第十八轮审查：回放各品种各自的成交时钟；权益快照/日汇总按回放时间轴；回放 CSV 拒绝非正 OHLC；提交模型前复核时段。"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.market.replay import ReplaySource
from arena.models import ActionRequest, Decision
from arena.server.api import create_app

ROOT = Path(__file__).resolve().parents[1]
SH = ZoneInfo("Asia/Shanghai")
OPEN_BOTH = json.dumps({"analysis": "x", "actions": [
    {"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1},
    {"symbol": "I0", "action": "open_short", "size_pct": 0.1, "stop_loss": 99999}]})


def make_engine(tmp_path, symbols=("RB0",), csv_dir=None, mock_response=OPEN_BOTH):
    replay = {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}
    if csv_dir:
        replay["csv_dir"] = str(csv_dir)
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": replay, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=mock_response)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


def write_csv(csv_dir, symbol, day_hours, price):
    lines = ["datetime,open,high,low,close,volume"]
    for day, hour in day_hours:
        for i in range(5):
            lines.append(f"2024-03-{day:02d}T{hour:02d}:{i:02d}:00,{price},{price + 10},{price - 10},{price + i},100")
    (csv_dir / f"{symbol}.csv").write_text("\n".join(lines), encoding="utf-8")


# ---------- 各品种各自的成交时钟 ----------
def test_triggered_fills_use_their_own_symbols_replay_time(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    write_csv(csv_dir, "RB0", [(1, 9), (1, 10)], 3400)     # RB0 的时间轴
    write_csv(csv_dir, "I0", [(2, 21), (2, 22)], 800)      # I0 晚一天多：数据流未对齐
    eng, db = make_engine(tmp_path, symbols=("RB0", "I0"), csv_dir=csv_dir)
    eng._tick()
    ts_rb, ts_i = eng.quotes["RB0"].ts, eng.quotes["I0"].ts
    assert ts_i > ts_rb + 86400
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1),
                                                             ActionRequest("I0", "open_short", 0.1, stop_loss=99999)]), snap, eng._generation)
    opens = {t["symbol"]: t for t in db.trades(eng.comp_id, "ok", limit=10)}
    assert opens["RB0"]["ts"] == ts_rb and opens["I0"]["ts"] == ts_i, "开仓按各自品种的行情时刻"
    eng.accounts["ok"].positions["RB0"].stop_loss = 1e9     # 下个 tick 两个品种同时触发止损
    eng.accounts["ok"].positions["I0"].stop_loss = 0.5
    eng._tick()
    fills = {t["symbol"]: t for t in db.trades(eng.comp_id, "ok", limit=10) if t["action"] == "stop_loss"}
    assert fills["RB0"]["ts"] == eng.quotes["RB0"].ts, "RB0 的止损记在 RB0 自己的报价时刻"
    assert fills["I0"]["ts"] == eng.quotes["I0"].ts, "I0 的止损记在 I0 自己的报价时刻"
    assert fills["RB0"]["ts"] != fills["I0"]["ts"]
    eng.stop()


# ---------- 权益快照与日汇总按回放时间轴 ----------
def test_replay_snapshots_and_daily_use_replay_clock(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    # 历史 bar 到 2024-03-01 23:54；之后每个 tick 合成续接一根（1 分钟），第 6 个 tick 跨到 03-02
    lines = ["datetime,open,high,low,close,volume"] + [f"2024-03-01T23:{m}:00,3400,3410,3390,{3400 + m},100" for m in range(50, 55)]
    (csv_dir / "RB0.csv").write_text("\n".join(lines), encoding="utf-8")
    eng, db = make_engine(tmp_path, csv_dir=csv_dir)
    eng._tick()
    eng._snapshot()
    q_ts = eng.quotes["RB0"].ts
    assert datetime.fromtimestamp(q_ts, SH).strftime("%Y-%m-%d %H:%M") == "2024-03-01 23:55"
    pts = db.equity_series(eng.comp_id)["ok"]
    assert pts and pts[-1][0] == q_ts, "权益快照时间戳应为回放时间，而不是墙钟"
    days = {d["day"] for d in db.daily(eng.comp_id, 10)}
    assert days == {"2024-03-01"}, "日汇总按回放日期分组"
    for _ in range(6):     # 回放推进到 03-02 00:01
        eng._tick()
    eng._snapshot()
    assert {d["day"] for d in db.daily(eng.comp_id, 10)} == {"2024-03-01", "2024-03-02"}
    # 看板时间范围相对最新快照：range_sec 只取最近一段，而不是相对浏览器时间
    client = TestClient(create_app(eng, ROOT / "web"))
    latest = db.equity_latest_ts(eng.comp_id)
    assert latest == eng.quotes["RB0"].ts
    all_pts = client.get("/api/equity").json()["series"]["ok"]
    recent = client.get("/api/equity?range_sec=60").json()["series"]["ok"]
    assert len(all_pts) == 2 and len(recent) == 1 and recent[0][0] == latest
    eng.stop()


# ---------- 回放 CSV：开/高/低/收任一非正即丢弃 ----------
def test_replay_csv_rejects_non_positive_ohlc(tmp_path, cn_specs):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    rows = ["datetime,open,high,low,close,volume",
            "2026-09-16 09:00:00,3400,3410,3390,3401,100",
            "2026-09-16 09:01:00,0,3410,3390,3402,100",        # open = 0
            "2026-09-16 09:02:00,3400,-1,3390,3403,100",       # high < 0
            "2026-09-16 09:03:00,3400,3410,-3390,3404,100",    # low < 0（会放大 ATR）
            "2026-09-16 09:04:00,3400,3410,3390,3405,NaN",     # volume 非有限
            "2026-09-16 09:05:00,3400,3410,3390,3406,100"]
    (csv_dir / "RB0.csv").write_text("\n".join(rows), encoding="utf-8")
    src = ReplaySource(["RB0"], cn_specs, seed=1, csv_dir=str(csv_dir), history=300)
    assert [b.close for b in src.klines("RB0", "1m", 10)] == [3401, 3406]


# ---------- 提交模型前复核时段 ----------
def _close_after_snapshot(eng, closed_symbols):
    real_build = eng._build_snapshot

    def build_then_close(*a, **k):
        snap = real_build(*a, **k)
        eng.source.is_open = lambda now, symbol=None: symbol not in closed_symbols  # type: ignore[method-assign]
        return snap

    eng._build_snapshot = build_then_close  # type: ignore[method-assign]


def test_symbols_closed_during_snapshot_are_dropped_before_model_calls(tmp_path):
    eng, db = make_engine(tmp_path, symbols=("RB0", "I0"))
    eng._tick()
    seen = {}
    real_decide = eng.traders["ok"].decide

    def spy(snap, view, rules):
        seen["symbols"] = set(snap.symbols)
        return real_decide(snap, view, rules)

    eng.traders["ok"].decide = spy  # type: ignore[method-assign]
    _close_after_snapshot(eng, {"RB0"})       # 拉取 K 线期间 RB0 收盘
    eng._round()
    assert seen["symbols"] == {"I0"}, "收盘品种不得进入模型看到的快照"
    res = {r["symbol"]: r for r in db.decisions(eng.comp_id, "ok", limit=1)[0]["results"]}
    # 模型回复里对 RB0 的动作因不在快照内被解析层丢弃（或执行层拒绝），只有 I0 成交
    assert res["I0"]["accepted"] is True and not res.get("RB0", {}).get("accepted")
    assert set(eng.accounts["ok"].positions) == {"I0"}
    eng.stop()


def test_round_is_skipped_when_all_symbols_close_during_snapshot(tmp_path):
    eng, db = make_engine(tmp_path, symbols=("RB0", "I0"))
    eng._tick()
    _close_after_snapshot(eng, {"RB0", "I0"})
    eng._round()
    assert eng.traders["ok"].calls == 0, "全部收盘：不得调用模型"
    assert db.decisions(eng.comp_id) == [] and eng.status_text == "休市"
    assert any("已收盘" in e["message"] for e in db.events(eng.comp_id, 20))
    eng.stop()
