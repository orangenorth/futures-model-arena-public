"""第六轮审查：行情 I/O 不得持有引擎锁；停止不得被慢行情源或被占用的锁卡死。"""
import json
import threading
import time
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.market.base import SourceError
from arena.market.sina_cn import SinaFuturesSource
from arena.exchange.contracts import load_cn_contracts

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_engine(tmp_path, symbols=("RB0", "I0", "AU0")):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800, "AU0": 560}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return Engine(cfg, db, accelerate=True), db


def test_snapshot_io_runs_outside_engine_lock_and_stop_is_prompt(tmp_path):
    eng, db = make_engine(tmp_path)
    eng._tick()
    real_build_snapshot = eng._build_snapshot

    def slow_build_snapshot(*a, **k):
        time.sleep(0.7)
        return real_build_snapshot(*a, **k)

    eng._build_snapshot = slow_build_snapshot  # type: ignore[method-assign]
    th = threading.Thread(target=eng._round, daemon=True)
    th.start()
    time.sleep(0.3)   # 正在拉取 K 线
    assert eng.lock.acquire(timeout=0.2), "拉取行情期间不应持有引擎锁"
    eng.lock.release()
    t0 = time.time()
    eng.stop()        # 应在当前 klines 调用结束后立即放弃本轮
    assert time.time() - t0 < 3.0
    th.join(timeout=3)
    assert not th.is_alive()
    assert db.decisions(eng.comp_id, "ok") == [], "停止后放弃的轮次不应落库"


def test_stop_does_not_hang_when_lock_is_held(tmp_path):
    eng, db = make_engine(tmp_path)
    eng.persist_lock_timeout = 0.5
    holder_release = threading.Event()

    def hold_lock():
        with eng.lock:
            holder_release.wait(5)

    th = threading.Thread(target=hold_lock, daemon=True)
    th.start()
    time.sleep(0.1)
    t0 = time.time()
    eng.stop()
    assert time.time() - t0 < 3.0, "锁被占用时 stop() 必须在超时后返回"
    assert any("引擎锁" in e["message"] for e in db.events(eng.comp_id, 20))
    holder_release.set()
    th.join(timeout=2)


def test_source_requests_abort_on_stop_event():
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = SinaFuturesSource(["RB0"], specs, holidays, timeout=0.01)
    src.stop_event = threading.Event()
    src.stop_event.set()
    t0 = time.time()
    try:
        src.klines("RB0", "5m", 10)
        assert False, "应抛出 SourceError"
    except SourceError as e:
        assert "停止" in str(e)
    assert time.time() - t0 < 1.0, "停止后不应再重试/退避等待"
    assert src.latest(["RB0"]) == {}
