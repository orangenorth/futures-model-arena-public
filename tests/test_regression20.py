"""第二十轮审查：回放状态精确恢复（随机序列/滚动 bar/趋势状态）；重置时为新竞赛写回放位置；位置只在 bar 结算成功后前进。"""
import json
import sqlite3
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.market.replay import ReplaySource

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_cfg(tmp_path, csv_dir=None, history=300, symbols=("RB0", "I0")):
    replay = {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}, "history": history}
    if csv_dir:
        replay["csv_dir"] = str(csv_dir)
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": replay, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


def bars_tuple(src, sym, n):
    return [(b.ts, b.open, b.high, b.low, b.close) for b in src.klines(sym, "1m", n)]


def test_synthetic_replay_restores_exact_state(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    for _ in range(25):
        eng._tick()
    before = {s: bars_tuple(eng.source, s, 400) for s in ("RB0", "I0")}     # 完整滚动窗口（含初始历史）
    trend, vol = dict(eng.source._trend), dict(eng.source._vol)
    eng.stop()
    db.close()
    # 对照：原来的源继续前进一步，应与重启后的第一根 bar 完全一致
    control = eng.source.latest(["RB0", "I0"])

    db2 = Database(cfg.db_path)
    eng2 = Engine(make_cfg(tmp_path), db2, accelerate=True)
    assert eng2.source.steps == 25
    for s in ("RB0", "I0"):
        assert bars_tuple(eng2.source, s, 400) == before[s], f"{s} 的滚动 bar（含时间戳）必须与重启前完全一致"
    assert eng2.source._trend == trend and eng2.source._vol == vol, "趋势/波动状态一致"
    eng2._tick()
    for s in ("RB0", "I0"):
        assert eng2.quotes[s].ts == control[s].ts and eng2.quotes[s].price == control[s].price, "随机序列从上次位置继续"
    assert not any("近似衔接" in e["message"] for e in db2.events(eng2.comp_id, 20))
    eng2.stop()
    db2.close()


def test_csv_replay_in_synthetic_continuation_restores_exact_state(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    lines = ["datetime,open,high,low,close,volume"] + [f"2024-03-01T09:{m:02d}:00,3400,3410,3390,{3400 + m},100" for m in range(8)]
    (csv_dir / "RB0.csv").write_text("\n".join(lines), encoding="utf-8")
    cfg = make_cfg(tmp_path, csv_dir, history=5, symbols=("RB0",))
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    for _ in range(12):        # 3 行 CSV 回放完毕后进入合成续接
        eng._tick()
    before = bars_tuple(eng.source, "RB0", 50)
    eng.stop()
    db.close()
    control = eng.source.latest(["RB0"])["RB0"]
    db2 = Database(cfg.db_path)
    eng2 = Engine(make_cfg(tmp_path, csv_dir, history=5, symbols=("RB0",)), db2, accelerate=True)
    assert bars_tuple(eng2.source, "RB0", 50) == before
    eng2._tick()
    assert (eng2.quotes["RB0"].ts, eng2.quotes["RB0"].price) == (control.ts, control.price)
    eng2.stop()
    db2.close()


def test_changed_replay_data_falls_back_to_approximate_resume(tmp_path):
    cfg = make_cfg(tmp_path, symbols=("RB0",))
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    for _ in range(5):
        eng._tick()
    last_ts, last_px = eng.quotes["RB0"].ts, eng.quotes["RB0"].price
    eng.stop()
    db.close()
    # 配置变了（seed 不同）：确定性快进得到的序列与记录不符 → 近似衔接，时间轴仍不倒退、价格连续
    cfg2 = make_cfg(tmp_path, symbols=("RB0",))
    cfg2.market_data.replay.seed = 99
    db2 = Database(cfg2.db_path)
    eng2 = Engine(cfg2, db2, accelerate=True)
    hist = eng2.source.klines("RB0", "1m", 3)
    assert hist[-1].ts == last_ts and abs(hist[-1].close - last_px) < 1e-6
    assert any("近似衔接" in e["message"] for e in db2.events(eng2.comp_id, 20))
    eng2._tick()
    assert eng2.quotes["RB0"].ts == last_ts + 60
    eng2.stop()
    db2.close()


def test_reset_persists_replay_position_for_new_competition(tmp_path):
    cfg = make_cfg(tmp_path, symbols=("RB0",))
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    for _ in range(5):
        eng._tick()
    ts_before = eng.quotes["RB0"].ts
    eng.reset()
    new_cid = eng.comp_id
    saved = json.loads(db.kv_get(f"replay_pos:{new_cid}"))
    assert saved["steps"] == 5 and saved["symbols"]["RB0"]["ts"] == ts_before, "重置后立即为新竞赛写入当前回放位置"
    eng.stop()      # 重置后没有再 tick 就退出
    db.close()
    db2 = Database(cfg.db_path)
    eng2 = Engine(make_cfg(tmp_path, symbols=("RB0",)), db2, accelerate=True)
    assert eng2.comp_id == new_cid
    eng2._tick()
    assert eng2.quotes["RB0"].ts == ts_before + 60, "重启后不能回到起点"
    eng2.stop()
    db2.close()


def test_replay_position_only_advances_after_bar_is_settled(tmp_path):
    cfg = make_cfg(tmp_path, symbols=("RB0",))
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    from arena.models import ActionRequest, Decision
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    pos_before = json.loads(db.kv_get(f"replay_pos:{eng.comp_id}"))
    eng.accounts["ok"].positions["RB0"].stop_loss = 1e9      # 下一根 bar 触发止损
    real_save = db.save_account

    def failing_save(*a, **k):
        raise sqlite3.OperationalError("database is locked (simulated)")

    db.save_account = failing_save  # type: ignore[method-assign]
    eng._tick()                                              # bar N：触发成交落库失败 → 内存回滚
    ts_n = eng.quotes["RB0"].ts
    assert "RB0" in eng.accounts["ok"].positions
    assert json.loads(db.kv_get(f"replay_pos:{eng.comp_id}")) == pos_before, "bar 未结算成功：回放位置不得前进"
    assert eng._replay_retry is not None
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                              # 重放同一根 bar N（不前进）：成交成功，位置才前进到 N
    assert eng.quotes["RB0"].ts == ts_n, "未结算的 bar 必须重放，而不是跳到下一根（否则只在 N 上触发的止损会永远丢失）"
    assert "RB0" not in eng.accounts["ok"].positions
    sl = next(t for t in db.trades(eng.comp_id, "ok", limit=5) if t["action"] == "stop_loss")
    assert sl["ts"] == ts_n
    assert json.loads(db.kv_get(f"replay_pos:{eng.comp_id}"))["steps"] == pos_before["steps"] + 1
    assert eng._replay_retry is None
    eng._tick()                                              # 之后正常前进
    assert eng.quotes["RB0"].ts == ts_n + 60
    assert json.loads(db.kv_get(f"replay_pos:{eng.comp_id}"))["steps"] == pos_before["steps"] + 2
    eng.stop()
    db.close()


def test_unsettled_replay_bar_is_replayed_even_if_next_bar_would_not_trigger(tmp_path):
    """止损只在 bar N 上被短暂击穿、N+1 回到止损之上：N 的落库失败后必须重放 N，而不是在 N+1 上什么都不发生地越过去。"""
    cfg = make_cfg(tmp_path, symbols=("RB0",))
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    from arena.models import ActionRequest, Decision
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    real_save = db.save_account

    def failing_save(*a, **k):
        raise sqlite3.OperationalError("database is locked (simulated)")

    # 让下一根 bar 的价格恰好击穿止损，再下一根回升：通过临时替换 latest_with_snapshot 构造
    real_latest = eng.source.latest_with_snapshot
    seq = {"n": 0}

    def scripted(symbols):
        quotes, bars = real_latest(symbols)
        seq["n"] += 1
        px = 1000.0 if seq["n"] == 1 else 5000.0        # N：击穿；N+1：回升
        q = quotes["RB0"]
        q.price = px
        return quotes, bars

    eng.source.latest_with_snapshot = scripted  # type: ignore[method-assign]
    eng.accounts["ok"].positions["RB0"].stop_loss = 2000.0
    db.save_account = failing_save  # type: ignore[method-assign]
    eng._tick()                                              # N：触发但落库失败
    assert "RB0" in eng.accounts["ok"].positions
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                              # 必须重放 N（价格 1000）而不是取 N+1（价格 5000）
    assert "RB0" not in eng.accounts["ok"].positions, "重放 N 后止损成交"
    assert db.trades(eng.comp_id, "ok", limit=1)[0]["price"] <= 1000.0
    eng._tick()                                              # 现在才前进到 N+1
    assert eng.prices["RB0"] == 5000.0
    eng.stop()
    db.close()


def test_resume_is_deterministic_for_source_alone():
    from arena.exchange.contracts import load_cn_contracts
    specs = {"RB0": load_cn_contracts(ROOT / "config" / "contracts_cn.json")[0]["RB0"]}
    a = ReplaySource(["RB0"], specs, seed=7, history=20)
    for _ in range(30):
        a.latest(["RB0"])
    pos = a.position()
    b = ReplaySource(["RB0"], specs, seed=7, history=20)
    assert b.resume(pos) is True
    assert [(x.ts, x.close) for x in b.klines("RB0", "1m", 100)] == [(x.ts, x.close) for x in a.klines("RB0", "1m", 100)]
    assert b.latest(["RB0"])["RB0"].price == a.latest(["RB0"])["RB0"].price
