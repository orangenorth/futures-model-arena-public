"""第七轮审查：拉取行情期间暂停，不得再发起模型调用或改动熔断计数。"""
import json
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def test_pause_during_snapshot_skips_model_calls_and_breaker(tmp_path):
    cfg = AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB),
                 TraderCfg(id="tripped", name="T", provider="mock", mock_response="not json")],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    eng._breaker["tripped"]["skip"] = 2
    real_build_snapshot = eng._build_snapshot
    state = {"paused_once": False}

    def snapshot_then_pause(*a, **k):
        if not state["paused_once"]:
            state["paused_once"] = True
            eng.pause(True)          # 用户在行情快照构建期间点了暂停
        return real_build_snapshot(*a, **k)

    eng._build_snapshot = snapshot_then_pause  # type: ignore[method-assign]
    eng._round()
    assert eng.traders["ok"].calls == 0, "暂停后不应再调用模型"
    assert eng.traders["tripped"].calls == 0
    assert eng._breaker["tripped"]["skip"] == 2, "熔断跳过计数不应被消耗"
    assert db.decisions(eng.comp_id) == []
    assert eng.accounts["ok"].positions == {}
    # 恢复后正常决策
    eng._build_snapshot = real_build_snapshot  # type: ignore[method-assign]
    eng.pause(False)
    eng._round()
    assert eng.traders["ok"].calls == 1 and "RB0" in eng.accounts["ok"].positions
    assert eng._breaker["tripped"]["skip"] == 1
    eng.stop()
