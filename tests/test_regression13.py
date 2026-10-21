"""第十三轮审查：新浪行情并发抓取且有整体预算；主循环/看门狗的审计写入尽力而为；看板按数据版本刷新。"""
import json
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import load_cn_contracts
from arena.market.base import SourceError
from arena.market.sina_cn import SinaFuturesSource
from arena.models import ActionRequest, Decision

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})
SYMS = ["RB0", "I0", "AU0", "AG0", "CU0", "SC0", "M0", "TA0"]
ROW = [["2026-09-16 09:05:00", "3400", "3410", "3390", "3405", "120", "100000"]]


def make_engine(tmp_path, accelerate=True):
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
    return Engine(cfg, db, accelerate=accelerate), db


def make_sina(symbols, timeout):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return SinaFuturesSource(symbols, specs, holidays, timeout=timeout)


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


# ---------- 新浪 latest()：并发 + 整体预算 ----------
def test_sina_latest_is_bounded_by_budget_not_symbol_count():
    src = make_sina(SYMS, timeout=0.5)
    src.spot_batch_size = 1      # 一个品种一次请求：本用例考察的是并发，不是批量

    def slow_get(method, params, attempts=3):
        time.sleep(0.4)          # 每个请求 0.4s：串行需 3.2s
        return {s: ROW for s in params["symbols"]}

    src._get = slow_get  # type: ignore[method-assign]
    t0 = time.time()
    q = src.latest(SYMS)
    assert time.time() - t0 < 1.2, "八个品种应并发抓取"
    assert set(q) == set(SYMS) and q["RB0"].price == 3405
    src.close()


def test_sina_latest_skips_hung_symbols_and_reuses_inflight_fetch():
    src = make_sina(SYMS, timeout=0.3)          # 预算 = 超时 + 1s
    src.spot_batch_size = 1                     # 逐品种发请求，才能数出"同一品种是否被重复请求"
    release = threading.Event()
    calls = []
    lock = threading.Lock()

    def hung_get(method, params, attempts=3):
        with lock:
            calls.extend(params["symbols"])
        release.wait(5)
        return {s: ROW for s in params["symbols"]}

    src._get = hung_get  # type: ignore[method-assign]
    t0 = time.time()
    assert src.latest(SYMS) == {}               # 全部挂起：本次跳过，盯市线程继续
    elapsed = time.time() - t0
    assert 1.0 <= elapsed < 2.5, f"应以整体预算为界，而不是 超时×品种数：{elapsed:.1f}s"
    assert src.latest(SYMS) == {}               # 仍挂起：复用进行中的抓取
    assert len(calls) == len(SYMS), "挂起期间不得为同一品种重复发请求"
    release.set()
    for f in list(src._inflight.values()):
        f.result(timeout=3)
    q = src.latest(SYMS)                        # 后台完成后下个 tick 拿到结果（走缓存，不再请求）
    assert set(q) == set(SYMS) and len(calls) == len(SYMS)
    src.close()


def test_sina_latest_does_not_retry_within_a_tick_but_klines_still_does():
    src = make_sina(["RB0"], timeout=0.5)
    n = []

    def failing(url, params=None, **kw):
        n.append(1)
        raise httpx.ConnectError("down")

    src.client.get = failing  # type: ignore[method-assign]
    t0 = time.time()
    assert src.latest(["RB0"]) == {}
    assert len(n) == 1 and time.time() - t0 < 0.5, "盯市抓取失败不退避重试，下个 tick 再取"
    n.clear()
    with pytest.raises(SourceError):
        src.klines("RB0", "5m", 10)              # 快照用 K 线仍保留三次重试
    assert len(n) == 3
    src.close()


def test_sina_latest_returns_immediately_after_stop_or_close():
    src = make_sina(SYMS, timeout=0.5)
    src.stop_event = threading.Event()
    src.stop_event.set()
    assert src.latest(SYMS) == {}
    src.stop_event.clear()
    src.close()
    assert src.latest(SYMS) == {}               # 线程池已关闭：不抛出


# ---------- 主循环与看门狗：审计写入不能杀死恢复路径 ----------
def test_main_loop_survives_audit_write_failure(tmp_path):
    eng, db = make_engine(tmp_path)
    db.add_event = broken       # type: ignore[method-assign]
    eng._snapshot = broken      # type: ignore[method-assign]   # 主循环内异常，且审计写入同样失败
    eng.start()
    time.sleep(2.5)
    assert eng._thread is not None and eng._thread.is_alive(), "审计写入失败不能终止主循环"
    eng.stop()


def test_watchdog_restarts_engine_even_when_event_write_fails(tmp_path):
    eng, db = make_engine(tmp_path)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    eng._thread = dead
    eng._stop.clear()
    db.add_event = broken  # type: ignore[method-assign]
    assert eng._watch_once() is True
    assert eng._thread is not dead and eng._thread.is_alive(), "重启不得依赖审计写入成功"
    assert eng._watch_once() is False
    eng.stop()


# ---------- 数据版本：结果落库后才变化 ----------
def test_data_version_changes_when_results_are_persisted(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    snap = eng._build_snapshot()
    v0 = eng.state()["data_version"]
    with eng.lock:
        eng.round_no += 1                       # 轮次开始 ≠ 结果已落库：版本不变
    assert eng.state()["data_version"] == v0
    d = Decision("ok", eng.round_no, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)])
    eng._finish_decision("ok", d, snap, eng._generation)
    v1 = eng.state()["data_version"]
    assert v1 != v0, "决策结算后版本应变化"
    eng._event_best_effort("info", "engine", "x")
    v2 = eng.state()["data_version"]
    assert v2 != v1, "事件写入后版本应变化"
    eng.accounts["ok"].positions["RB0"].stop_loss = 1e9   # 下个 tick 触发止损成交
    eng._tick()
    assert "RB0" not in eng.accounts["ok"].positions
    v3 = eng.state()["data_version"]
    assert v3 != v2, "盯市成交后版本应变化"
    eng.pause(True)
    assert eng.state()["data_version"] != v3
    eng.stop()
