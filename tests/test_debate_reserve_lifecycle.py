"""Regression 65: do not reserve research after pause/reset/stop during the full-universe scan."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine


ROOT = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path):
    traders = [
        TraderCfg(id=trader_id, name=name, provider="openai_compatible", base_url="https://example.invalid/v1",
                  model=f"{trader_id}-test", api_key="test-key")
        for trader_id, name in (("deepseek", "DeepSeek"), ("qwen", "通义千问"), ("kimi", "Kimi K3"), ("glm", "智谱 GLM"))
    ]
    return AppConfig(
        competition={"name": "research", "market": "replay", "initial_cash": 80_000, "decision_interval_sec": 1,
                     "tick_interval_sec": 1, "snapshot_interval_sec": 1, "timezone": "Asia/Shanghai"},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "history": 4000, "start_price": {"RB0": 3400, "I0": 800}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"max_margin_risk_ratio": 0.95, "warning_margin_risk_ratio": 0.90, "min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0, "breaker_failures": 2, "breaker_skip_rounds": 2},
        preopen_debate={"enabled": False, "stage_timeout_sec": 5, "max_output_tokens": 128},
        traders=traders,
        storage={"db_path": str(tmp_path / "review65.db")},
        base_dir=ROOT,
    )


@pytest.mark.parametrize("action", ("pause", "reset", "stop"))
def test_lifecycle_during_universe_scan_does_not_create_a_session(tmp_path, action):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    worker = None
    released = threading.Event()
    try:
        old_comp_id = engine.comp_id
        generation, epoch = engine._generation, engine._decision_epoch
        snap, health = engine._preopen_snapshot(engine.round_no, allow_quote_only=True)
        assert snap is not None
        started = threading.Event()

        def stall_scan(*_a, **_k):
            started.set()
            assert released.wait(timeout=5)
            return None

        engine._preopen_snapshot = lambda *_a, **_k: (snap, health)  # type: ignore[method-assign]
        engine._preopen_universe_research = stall_scan  # type: ignore[method-assign]
        worker = threading.Thread(
            target=engine._run_preopen_debate,
            args=(old_comp_id, generation, epoch, "manual_research", "手动赛前研究", None, "manual"),
            daemon=True,
        )
        worker.start()
        assert started.wait(timeout=5)
        if action == "pause":
            engine.pause(True)
        elif action == "reset":
            engine.reset()
        else:
            engine.stop()
        released.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert db.debate_sessions(old_comp_id) == []
        if action == "reset":
            assert engine.comp_id != old_comp_id
            assert db.debate_sessions(engine.comp_id) == []
    finally:
        released.set()
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        engine.stop()
        db.close()


def test_reserve_refuses_ended_competition(tmp_path):
    db = Database(tmp_path / "ended.db")
    try:
        ended_id = db.create_competition("c1", "replay", 1000, "{}")
        running_id = db.create_competition("c2", "replay", 1000, "{}")
        manifest = {"analysis_id": "a", "snapshot_hash": "b", "market_as_of": 1, "global_analysis": {}}
        assert db.reserve_debate_session(ended_id, "ended", "manual", "手动", None, manifest) is None
        assert db.debate_sessions(ended_id) == []
        reserved = db.reserve_debate_session(running_id, "running", "manual", "手动", None, manifest)
        assert reserved is not None
    finally:
        db.close()
