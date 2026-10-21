"""第十六轮审查：已交付迟到报价时不再等待替换请求；竞赛时区变化视同配置变更，新建竞赛，导出口径一致。"""
import csv
import io
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_cn_contracts
from arena.market.sina_cn import SinaFuturesSource
from arena.models import ActionRequest, Decision
from arena.server.api import create_app

ROOT = Path(__file__).resolve().parents[1]
ROW = [["2026-09-16 09:05:00", "3400", "3410", "3390", "3405", "120", "100000"]]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_sina(symbols, timeout):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return SinaFuturesSource(symbols, specs, holidays, timeout=timeout)


def make_cfg(tmp_path, timezone="Asia/Shanghai"):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1,
                     "snapshot_interval_sec": 1, "timezone": timezone},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


def test_sina_latest_returns_late_quote_without_waiting_for_replacement():
    src = make_sina(["RB0"], timeout=0.2)      # 预算 1.2s
    src.cache_sec = 0
    gate = threading.Event()

    def gated_get(method, params, attempts=3):
        gate.wait(5)
        return {s: ROW for s in params["symbols"]}

    src._get = gated_get  # type: ignore[method-assign]
    assert src.latest(["RB0"]) == {}           # 行情源持续偏慢：预算内未返回
    gate.set()
    src._inflight["RB0"].result(timeout=3)
    gate.clear()
    t0 = time.time()
    q = src.latest(["RB0"])
    assert q["RB0"].price == 3405
    assert time.time() - t0 < 0.3, "已有可用报价时必须立即返回，不能再等替换请求的预算"
    assert "RB0" in src._inflight and not src._inflight["RB0"].done(), "替换请求在后台继续"
    gate.set()
    src.close()


def _read_csv(text: str):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def test_timezone_change_starts_new_competition_and_exports_stay_consistent(tmp_path, monkeypatch):
    from arena.db import competition_timezone
    cfg = make_cfg(tmp_path, "Asia/Shanghai")
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    eng._snapshot()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    old_cid = eng.comp_id
    old_trade_ts = db.trades(old_cid, "ok", limit=1)[0]["ts"]
    assert competition_timezone(db.active_competition()) == "Asia/Shanghai"
    eng.stop()
    db.close()

    cfg2 = make_cfg(tmp_path, "UTC")            # 重启前改了竞赛时区
    db2 = Database(cfg2.db_path)
    eng2 = Engine(cfg2, db2, accelerate=True)
    assert eng2.comp_id != old_cid, "时区变化必须新建竞赛，历史成交不能被按新时区重新划分"
    assert any("时区" in e["message"] for e in db2.events(old_cid, 20))
    assert competition_timezone(db2.active_competition()) == "UTC"
    eng2._tick()
    eng2._snapshot()
    snap2 = eng2._build_snapshot()
    eng2._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap2, eng2._generation)
    new_trade_ts = db2.trades(eng2.comp_id, "ok", limit=1)[0]["ts"]
    client = TestClient(create_app(eng2, ROOT / "web"))
    rows = _read_csv(client.get("/api/export/trades.csv").text)
    assert [r["time"] for r in rows] == [datetime.fromtimestamp(new_trade_ts, ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")]
    eng2.stop()

    # 离线导出：旧竞赛仍按其创建时的 Asia/Shanghai，新竞赛按 UTC —— 与 API 口径一致
    sys.path.insert(0, str(ROOT / "scripts"))
    import export_results  # noqa: E402
    monkeypatch.setattr(sys, "argv", ["export_results.py", "--db", str(tmp_path / "e.db"), "--out", str(tmp_path / "old"), "--competition", str(old_cid)])
    export_results.main()
    with open(tmp_path / "old" / "trades.csv", encoding="utf-8-sig") as f:
        assert [r["time"] for r in csv.DictReader(f)] == [datetime.fromtimestamp(old_trade_ts, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")]
    monkeypatch.setattr(sys, "argv", ["export_results.py", "--db", str(tmp_path / "e.db"), "--out", str(tmp_path / "new")])
    export_results.main()
    with open(tmp_path / "new" / "trades.csv", encoding="utf-8-sig") as f:
        assert [r["time"] for r in csv.DictReader(f)] == [datetime.fromtimestamp(new_trade_ts, ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")]
    # 旧库没有记录时区（config_json 缺失/异常）视为相同，不新建竞赛
    assert competition_timezone({"config_json": None}) is None
    assert competition_timezone({"config_json": "{not json"}) is None
    db2.close()


def test_same_timezone_keeps_competition_across_restart(tmp_path):
    cfg = make_cfg(tmp_path, "Asia/Shanghai")
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    cid = eng.comp_id
    eng.stop()
    db.close()
    db2 = Database(cfg.db_path)
    eng2 = Engine(make_cfg(tmp_path, "Asia/Shanghai"), db2, accelerate=True)
    assert eng2.comp_id == cid
    eng2.stop()
