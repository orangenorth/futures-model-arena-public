"""第四十五轮审查：关闭前记入预写日志的盯市更新（kind=mark）——写入提交前日志不清；记录并恢复高水位；
不经过撮合器写入，风控参数事后变化也不会凭空产生成交；触发重试的无成交路径同样先写入再清条目。"""
import json
import logging
import sqlite3
import time
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision, Quote

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})


def make_cfg(tmp_path, symbols=("RB0",), start=None, **risk):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": {"seed": 3, "start_price": start or {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3, **risk},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


class LiveSource:
    def __init__(self, replay, prices):
        self.replay, self.prices, self.n = replay, list(prices), 0

    def latest(self, symbols):
        if self.n >= len(self.prices):
            return {}
        px = self.prices[self.n]
        self.n += 1
        quotes = px if isinstance(px, dict) else {"RB0": px}
        return {s: Quote(s, p, time.time()) for s, p in quotes.items()}

    def klines(self, symbol, period, limit=120):
        return self.replay.klines(symbol, period, limit)

    def is_open(self, now, symbol=None):
        return True

    def next_open(self, now):
        return now

    def health(self):
        return {"source": "live-test", "ok": True, "errors": 0, "detail": ""}


def broken(*a, **k):
    raise sqlite3.OperationalError("database is locked (simulated)")


def shutdown_with_unwritten_mark(tmp_path, prices):
    """开多 RB0；prices[0] 落库成功，之后的报价盯市更新全部落库失败；数据库仍不可写时关闭。
    返回 (竞赛id, 日志路径, 失败时记住的高水位, 配置)。"""
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, prices)
    eng._tick()
    db.save_account = broken  # type: ignore[method-assign]
    for _ in prices[1:]:
        eng._tick()
    high = eng._mark_save_failed["ok"]
    cid, journal = eng.comp_id, eng._journal_path()
    eng.stop()
    db.close()
    entry = json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]
    assert entry["kind"] == "mark" and entry["peak"] == high and entry["prices"]["RB0"] == prices[-1]
    return cid, journal, high, cfg


def test_journal_survives_a_restart_whose_write_still_fails(tmp_path):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    db = Database(str(tmp_path / "e.db"))
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)                  # 重启时数据库仍不可写：条目与日志必须保留
    assert "ok" in eng._pending_mark_updates and eng._mark_save_failed["ok"] == high and journal.exists()
    assert db.load_accounts(cid)["ok"].positions["RB0"].last_price == 3400.0
    db.save_account = Database(str(tmp_path / "e.db")).save_account  # type: ignore[method-assign]
    eng._tick()                                             # 数据库恢复：本 tick 先写入日志里的更新
    saved = db.load_accounts(cid)["ok"]
    assert saved.peak_equity == high and not eng._pending_mark_updates and not eng._mark_save_failed and not journal.exists()
    eng.stop()


def test_journal_keeps_the_high_water_mark_when_price_fell_afterwards(tmp_path):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0, 3300.0])
    db = Database(str(tmp_path / "e.db"))
    eng = Engine(cfg, db, accelerate=True)                  # 重启：按日志直接写入 3300 的最新价与 3600 时的高水位
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3300.0 and saved.peak_equity == high
    assert not eng._pending_mark_updates and not journal.exists()
    eng.stop()


def test_journaled_mark_update_never_creates_a_trade_under_new_risk_settings(tmp_path):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    strict = make_cfg(tmp_path, liquidation_risk_ratio=0.0001)   # 重启前收紧强平阈值：任何持仓按新规则都会被强平
    db = Database(str(tmp_path / "e.db"))
    eng = Engine(strict, db, accelerate=True)
    saved = db.load_accounts(cid)["ok"]
    assert "RB0" in saved.positions and saved.positions["RB0"].last_price == 3600.0 and saved.peak_equity == high
    assert not any(t["action"] == "liquidation" for t in db.trades(cid, "ok", limit=10)), "写入日志里的盯市更新不得凭空产生强平"
    assert not journal.exists()
    eng.stop()


def test_trigger_retry_keeps_its_journal_until_the_write_commits(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    # 一条触发已不再成立的重试条目（价格不触发止损，但最新价与库中不同）
    eng._pending_marks["ok"] = ({"RB0": 3450.0}, time.time(), None)
    eng._unsettled.add("ok")
    eng._write_journal()
    journal = eng._journal_path()
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                             # 重试：无成交，写入失败 → 条目与日志都保留
    assert "ok" in eng._pending_marks and "ok" in eng._unsettled and journal.exists()
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                             # 写入提交后才解除
    assert not eng._pending_marks and not eng._unsettled and not journal.exists()
    eng.stop()


def test_shutdown_before_any_tick_writes_the_journaled_prices(tmp_path):
    cid, journal, high, cfg = shutdown_with_unwritten_mark(tmp_path, [3400.0, 3600.0])
    db = Database(str(tmp_path / "e.db"))
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)                  # 启动时仍写不进去，且还没来过任何报价（缓存价为空）
    assert "ok" in eng._pending_mark_updates
    db.save_account = real_save  # type: ignore[method-assign]
    eng.stop()                                              # 关闭时数据库恢复：写入的必须是日志里的最新价与高水位
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3600.0 and saved.peak_equity == high, "不得只写高水位而丢掉日志里的最新价"
    assert not journal.exists()


def test_partial_quotes_after_restart_keep_the_journaled_price_of_the_absent_symbol(tmp_path):
    cfg = make_cfg(tmp_path, symbols=("RB0", "I0"), start={"RB0": 3400, "I0": 800})
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1),
                                                            ActionRequest("I0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    assert set(eng.accounts["ok"].positions) == {"RB0", "I0"}
    eng.source = LiveSource(eng.source, [{"RB0": 3400.0, "I0": 800.0}, {"RB0": 3600.0, "I0": 850.0}])
    eng._tick()                                             # 落库成功
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                             # 两个品种的盯市更新落库失败
    high = eng._mark_save_failed["ok"]
    cid, journal = eng.comp_id, eng._journal_path()
    eng.stop()
    db.close()
    assert json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]["prices"] == {"RB0": 3600.0, "I0": 850.0}
    db = Database(str(tmp_path / "e.db"))
    real_save = db.save_account

    def until_new_quote(comp_id, acc, **kw):   # 日志更新的直接写入（RB0 还是 3600）失败；带新报价的盯市写入成功
        if acc.positions["RB0"].last_price != 3650.0:
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real_save(comp_id, acc, **kw)

    db.save_account = until_new_quote  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)
    assert "ok" in eng._pending_mark_updates
    eng.source = LiveSource(eng.source, [{"RB0": 3650.0}])  # 重启后只来了 RB0 的报价
    eng._tick()
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3650.0
    assert saved.positions["I0"].last_price == 850.0, "缺席品种沿用日志里的最新价，不能退回重启前数据库里的 800"
    assert saved.peak_equity >= high
    assert not eng._pending_mark_updates and not eng._mark_save_failed and not journal.exists()
    eng.stop()


def test_pending_mark_update_seeds_an_absent_price_before_account_liquidation(tmp_path):
    """A recovered write on the same partial tick must not liquidate an absent symbol at its stale DB price."""
    cfg = make_cfg(tmp_path, symbols=("RB0", "I0"), start={"RB0": 3400, "I0": 800},
                   slippage_ticks=0, liquidation_risk_ratio=1.0)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    acc = eng.accounts["ok"]
    both = {"RB0": 3400.0, "I0": 800.0}
    assert eng.exchange.open(acc, eng.specs["RB0"], "long", 100, 3400.0, 1.0, None, None, both, 1).accepted
    assert eng.exchange.open(acc, eng.specs["I0"], "long", 1, 800.0, 1.0, None, None, both, 1).accepted
    db.save_account(eng.comp_id, acc)

    # The I0 mark was journaled but its ordinary database write has not committed yet.
    eng._pending_mark_updates["ok"] = {"prices": {"I0": 850.0}, "peak": acc.peak_equity}
    eng._mark_save_failed["ok"] = acc.peak_equity
    assert eng._write_journal()
    journal = eng._journal_path()
    eng.quotes.clear()
    real_save = db.save_account

    def save_only_liquidation(comp_id, candidate, **kw):
        # The direct journal update still fails; the later triggered liquidation write recovers in this same tick.
        if candidate.positions:
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real_save(comp_id, candidate, **kw)

    db.save_account = save_only_liquidation  # type: ignore[method-assign]
    eng.source = LiveSource(eng.source, [{"RB0": 2400.0}])
    eng._tick()

    closed = {t["symbol"]: t for t in db.trades(eng.comp_id, "ok", limit=10) if t["action"] == "liquidation"}
    assert set(closed) == {"RB0", "I0"}
    assert closed["I0"]["price"] == 850.0, "The absent symbol must use its pending journaled mark, not the stale DB value"
    assert not eng.accounts["ok"].positions and not eng._pending_mark_updates and not journal.exists()
    eng.stop()


def test_shutdown_keeps_the_recorded_trigger_price_and_its_remembered_peak(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, [3400.0, 3600.0, 3450.0, 3480.0])
    eng._tick()                                             # 3400：落库成功
    eng.accounts["ok"].positions["RB0"].stop_loss = 3500.0
    db.save_account(eng.comp_id, eng.accounts["ok"])        # 止损 3500 已持久化
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                             # 3600：无成交盯市更新落库失败，高水位记住
    high = eng._mark_save_failed["ok"]
    eng._tick()                                             # 3450：止损触发，落库失败 → 触发条目（原价 3450）进日志
    assert eng._pending_marks["ok"][0]["RB0"] == 3450.0
    eng._tick()                                             # 3480：重试仍失败；缓存价已变
    cid, journal = eng.comp_id, eng._journal_path()
    eng.stop()                                              # 关闭：不得用 3480 覆盖记录的触发价；高水位随触发条目进日志
    db.close()
    entry = json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]
    assert entry.get("kind") != "mark" and entry["prices"]["RB0"] == 3450.0 and entry["peak"] == high
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(cfg, db2, accelerate=True)                # 重启：按原触发价 3450 补做止损，记住的高水位一起进库
    saved = db2.load_accounts(cid)["ok"]
    assert "RB0" not in saved.positions and saved.peak_equity == high
    stops = [t for t in db2.trades(cid, "ok", limit=10) if t["action"] == "stop_loss"]
    assert len(stops) == 1 and stops[0]["price"] <= 3455.0, "止损必须按记录的原价 3450 成交，而不是关闭时的 3480"
    assert not eng2._pending_marks and not eng2._mark_save_failed and not journal.exists()
    eng2.stop()
    db2.close()


def liquidations(db, cid):
    return [t for t in db.trades(cid, "ok", limit=20) if t["action"] == "liquidation"]


def shutdown_with_retained_replay_bar(tmp_path):
    """纯回放：开多 RB0（止损 1，永不触发）；bar 2 落库成功；bar 3 的无成交盯市更新落库失败（bar 被保留），
    数据库仍不可写时关闭。返回 (竞赛id, 日志路径, 高水位, bar 3 之后的回放位置, 配置)。"""
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng._tick()                                             # bar 2：落库成功，回放位置写入
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                             # bar 3：无成交盯市更新落库失败，bar 被保留
    assert eng._replay_retry is not None and "ok" in eng._mark_save_failed
    high = eng._mark_save_failed["ok"]
    pos3 = eng.source.position()
    cid, journal = eng.comp_id, eng._journal_path()
    assert json.loads(db.kv_get(f"replay_pos:{cid}"))["steps"] == pos3["steps"] - 1, "持久化的位置停在 bar 2"
    eng.stop()
    db.close()
    data = json.loads(journal.read_text(encoding="utf-8"))
    assert data["marks"]["ok"]["kind"] == "mark" and data["replay_pos"]["steps"] == pos3["steps"], "日志记下 bar 3 之后的位置"
    return cid, journal, high, pos3, cfg


def test_replay_skips_the_bar_whose_mark_only_update_was_journaled(tmp_path):
    cid, journal, high, pos3, cfg = shutdown_with_retained_replay_bar(tmp_path)
    strict = make_cfg(tmp_path, liquidation_risk_ratio=0.0001)   # 重启前收紧强平阈值：按新规则任何持仓一盯市就强平
    db = Database(str(tmp_path / "e.db"))
    eng = Engine(strict, db, accelerate=True)                     # 启动即写入日志里的更新，回放位置越过 bar 3
    saved = db.load_accounts(cid)["ok"]
    assert "RB0" in saved.positions and saved.peak_equity == high and not liquidations(db, cid)
    assert json.loads(db.kv_get(f"replay_pos:{cid}"))["steps"] == pos3["steps"] and not journal.exists()
    assert eng._journal_replay_pos is None and eng.source.position()["steps"] == pos3["steps"]
    eng._tick()                                                   # 取的是 bar 4，而不是按新规则重放 bar 3
    assert eng.source.position()["steps"] == pos3["steps"] + 1
    liq = liquidations(db, cid)
    assert liq and all(t["ts"] > pos3["symbols"]["RB0"]["ts"] for t in liq), "新规则下的强平只能落在之后的 bar 上，不能落在关闭时已评估过的 bar 3"
    eng.stop()


def test_replay_holds_until_the_journaled_update_is_written(tmp_path):
    cid, journal, high, pos3, cfg = shutdown_with_retained_replay_bar(tmp_path)
    strict = make_cfg(tmp_path, liquidation_risk_ratio=0.0001)
    db = Database(str(tmp_path / "e.db"))
    real_save = db.save_account
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(strict, db, accelerate=True)                     # 启动时仍写不进去：回放停在 bar 3 之后，位置不写 kv
    assert "ok" in eng._pending_mark_updates and eng._journal_replay_pos is not None
    assert eng.source.position()["steps"] == pos3["steps"]
    assert json.loads(db.kv_get(f"replay_pos:{cid}"))["steps"] == pos3["steps"] - 1
    eng._tick()                                                   # 仍写不进去：不取新 bar、不盯市、没有成交
    assert eng.source.position()["steps"] == pos3["steps"] and not liquidations(db, cid) and journal.exists()
    db.save_account = real_save  # type: ignore[method-assign]
    eng._tick()                                                   # 数据库恢复：写入更新、位置越过 bar 3；本 tick 仍不取新 bar
    saved = db.load_accounts(cid)["ok"]
    assert "RB0" in saved.positions and saved.peak_equity == high and not liquidations(db, cid)
    assert json.loads(db.kv_get(f"replay_pos:{cid}"))["steps"] == pos3["steps"]
    assert eng._journal_replay_pos is None and not journal.exists() and eng.source.position()["steps"] == pos3["steps"]
    eng._tick()                                                   # 下一根 bar 4 才按新规则评估
    liq = liquidations(db, cid)
    assert liq and all(t["ts"] > pos3["symbols"]["RB0"]["ts"] for t in liq)
    eng.stop()


def test_shutdown_reports_a_mark_update_it_can_journal_nowhere(tmp_path, monkeypatch, caplog):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, [3400.0, 3600.0])
    eng._tick()
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()
    high = eng._mark_save_failed["ok"]
    cid = eng.comp_id
    blocked = {"on": True}
    real_write = Engine._write_json_atomic
    monkeypatch.setattr(Engine, "_write_json_atomic", staticmethod(
        lambda path, text: (_ for _ in ()).throw(PermissionError("ro")) if blocked["on"] else real_write(path, text)))
    caplog.set_level(logging.WARNING, logger="arena.engine")
    eng.stop()                                                    # 数据库与预写日志（主/备用位置）都写不进去
    assert eng.shutdown_errors and "未持久化" in eng.shutdown_errors[0]
    assert any(r.levelno == logging.ERROR and "未持久化" in r.getMessage() for r in caplog.records)
    assert not any("已记入预写日志" in r.getMessage() for r in caplog.records), "写不进日志不能报成功"
    assert not any(p.exists() for p in eng._journal_paths())
    saved = db.load_accounts(cid)["ok"]
    assert saved.positions["RB0"].last_price == 3400.0 and saved.peak_equity < high, "不写半截状态"
    assert eng._pending_mark_updates["ok"]["prices"]["RB0"] == 3600.0 and eng._mark_save_failed["ok"] == high, "状态保留在内存，可恢复"
    blocked["on"] = False
    assert eng._write_journal() and eng._journal_path().exists()  # 磁盘恢复后同一份状态仍能写出去
    assert json.loads(eng._journal_path().read_text(encoding="utf-8"))["marks"]["ok"]["peak"] == high


def test_journal_keeps_both_a_trigger_and_the_mark_update_of_one_account(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=1)]), snap, eng._generation)
    eng.source = LiveSource(eng.source, [3400.0, 3600.0])
    eng._tick()
    eng.accounts["ok"].positions["RB0"].stop_loss = 3500.0
    db.save_account(eng.comp_id, eng.accounts["ok"])        # 止损 3500 已持久化
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                             # 3600：无成交盯市更新落库失败
    high = eng._mark_save_failed["ok"]
    cid, journal = eng.comp_id, eng._journal_path()
    eng.stop()
    db.close()
    db = Database(str(tmp_path / "e.db"))
    db.save_account = broken  # type: ignore[method-assign]
    eng = Engine(cfg, db, accelerate=True)                  # 重启时仍写不进去：日志里的更新待写入
    eng.source = LiveSource(eng.source, [3450.0])
    eng._tick()                                             # 新报价 3450 触发止损，落库又失败：触发条目与更新都要保留
    assert "ok" in eng._pending_marks and "ok" in eng._pending_mark_updates
    entry = json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]
    assert entry.get("kind") != "mark" and entry["prices"]["RB0"] == 3450.0
    assert entry["mark_update"]["prices"]["RB0"] == 3600.0 and entry["mark_update"]["peak"] == high
    eng.stop()
    db.close()
    db2 = Database(str(tmp_path / "e.db"))
    eng2 = Engine(cfg, db2, accelerate=True)                # 重启：先写入更新，再按原价 3450 补做止损，高水位不丢
    saved = db2.load_accounts(cid)["ok"]
    stops = [t for t in db2.trades(cid, "ok", limit=10) if t["action"] == "stop_loss"]
    assert "RB0" not in saved.positions and saved.peak_equity == high and len(stops) == 1 and stops[0]["price"] <= 3455.0
    assert not eng2._pending_marks and not eng2._pending_mark_updates and not eng2._mark_save_failed and not journal.exists()
    eng2.stop()
    db2.close()
