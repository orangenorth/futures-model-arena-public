"""第四轮审查：盯市成交提交后事件写入失败不影响发布；空 CSV 回放。"""
import json
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.market.replay import ReplaySource

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_cfg(tmp_path):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"max_retries": 0, "timeout_sec": 5},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


def test_tick_publishes_committed_state_even_if_event_logging_fails(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick(); eng._round()
    pos = eng.accounts["ok"].positions["RB0"]
    pos.stop_loss = pos.last_price * 2   # 必然触发止损
    db.save_account(eng.comp_id, eng.accounts["ok"])
    real_add_event = db.add_event

    def flaky_event(*a, **k):
        if a[1] == "info":
            raise RuntimeError("event write failed (simulated)")
        return real_add_event(*a, **k)

    db.add_event = flaky_event  # type: ignore[method-assign]
    eng._tick()   # 止损成交已提交，事件写入失败
    assert "RB0" not in eng.accounts["ok"].positions, "已提交的平仓必须发布到内存"
    assert db.trade_count(eng.comp_id, "ok") == 2
    eng._tick()   # 不应产生重复平仓
    assert db.trade_count(eng.comp_id, "ok") == 2
    assert db.load_accounts(eng.comp_id)["ok"].positions == {}
    eng.stop()


def test_replay_empty_csv_falls_back_to_synthetic(tmp_path, cn_specs):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "RB0.csv").write_text("datetime,open,high,low,close,volume\n", encoding="utf-8")
    (csv_dir / "I0.csv").write_text("garbage,,,\n1,2\n", encoding="utf-8")
    src = ReplaySource(["RB0", "I0"], cn_specs, seed=1, csv_dir=str(csv_dir), start_price={"RB0": 3400, "I0": 800}, history=50)
    for _ in range(5):
        q = src.latest(["RB0", "I0"])
        assert q["RB0"].price > 0 and q["I0"].price > 0
    assert len(src.klines("RB0", "1m", 100)) == 55
    assert abs(src.klines("RB0", "1m", 1)[0].close / 3400 - 1) < 0.2
