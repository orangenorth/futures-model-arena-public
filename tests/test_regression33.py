"""第三十三轮审查：预写日志记录触发时生效的撮合参数与合约规格，重试（重启后 / 切换竞赛前）按当时条款补做，
不受重启后风控参数变化或合约表事后编辑的影响。"""
import json
import shutil
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import ActionRequest, Decision, Quote
from tests.conftest import restamp_competition_universe

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})
OK = TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)


def make_cfg(tmp_path, contracts, slippage_ticks=1, initial_cash=1_000_000):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": initial_cash, "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": str(contracts)}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3, "slippage_ticks": slippage_ticks},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[OK],
        storage={"db_path": str(tmp_path / "e.db")},
        base_dir=ROOT,
    )


class LiveSource:
    def __init__(self, replay, prices):
        self.replay, self.prices, self.n = replay, list(prices), 0

    def latest(self, symbols):
        px = self.prices[min(self.n, len(self.prices) - 1)]
        self.n += 1
        return {"RB0": Quote("RB0", px, time.time())}

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


def contracts_copy(tmp_path):
    dst = tmp_path / "contracts.json"
    shutil.copy(ROOT / "config" / "contracts_cn.json", dst)
    return dst


def edit_rb0(path, **changes):
    data = json.loads(path.read_text(encoding="utf-8"))
    table = data["contracts"] if "contracts" in data and "RB0" in data["contracts"] else data
    table["RB0"].update(changes)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def leave_pending_stop(tmp_path, contracts, slippage_ticks=1):
    """ok 开多 RB0（止损 2000），行情跌到 1000 触发止损但落库失败，然后关闭进程。
    返回 (竞赛id, 均价, 手数, 触发时撮合器给出的成交价, 触发时的手续费, 日志路径)。"""
    cfg = make_cfg(tmp_path, contracts, slippage_ticks)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    eng._tick()
    snap = eng._build_snapshot()
    eng._finish_decision("ok", Decision("ok", 1, 0, actions=[ActionRequest("RB0", "open_long", 0.1, stop_loss=2000.0)]), snap, eng._generation)
    pos = eng.accounts["ok"].positions["RB0"]
    avg, qty, spec = pos.avg_price, pos.qty, eng.specs["RB0"]
    assert spec.multiplier == 10
    fill = eng.exchange.fill_price(spec, 1000.0, "sell")
    fee = eng.exchange.fee(spec, fill, qty)
    eng.source = LiveSource(eng.source, [3400.0, 1000.0])
    eng._tick()                                   # 3400：未触发
    db.save_account = broken  # type: ignore[method-assign]
    eng._tick()                                   # 1000：触发止损，落库失败 → 预写日志
    assert "ok" in eng._pending_marks
    cid, journal = eng.comp_id, eng._journal_path()
    eng.stop()
    db.close()
    return cid, avg, qty, fill, fee, journal


def stop_trade(db, cid):
    sls = [t for t in db.trades(cid, "ok", limit=20) if t["action"] == "stop_loss"]
    assert len(sls) == 1, sls
    return sls[0]


def test_journal_records_the_terms_in_effect(tmp_path):
    contracts = contracts_copy(tmp_path)
    _, _, _, _, _, journal = leave_pending_stop(tmp_path, contracts, slippage_ticks=2)
    terms = json.loads(journal.read_text(encoding="utf-8"))["marks"]["ok"]["terms"]
    assert terms["risk"]["slippage_ticks"] == 2 and terms["specs"]["RB0"]["multiplier"] == 10


def test_retry_uses_the_slippage_in_effect_when_the_trigger_fired(tmp_path):
    contracts = contracts_copy(tmp_path)
    cid, avg, qty, fill, fee, journal = leave_pending_stop(tmp_path, contracts, slippage_ticks=1)
    db = Database(str(tmp_path / "e.db"))
    eng = Engine(make_cfg(tmp_path, contracts, slippage_ticks=5), db, accelerate=True)   # 重启后滑点改为 5 tick，同一竞赛
    assert eng.comp_id == cid and eng.exchange.slippage_ticks == 5
    assert stop_trade(db, cid)["price"] == fill == 999.0, "按触发时的 1 tick 滑点成交，而不是重启后的 5 tick"
    assert not journal.exists()
    eng.stop()


def test_retry_uses_the_contract_terms_in_effect_when_the_trigger_fired(tmp_path):
    contracts = contracts_copy(tmp_path)
    cid, avg, qty, fill, fee, journal = leave_pending_stop(tmp_path, contracts)
    edit_rb0(contracts, multiplier=100, fee_rate=0.001)          # 合约表事后被编辑
    db = Database(str(tmp_path / "e.db"))
    # Resume the competition under the edited terms so the subject under test is
    # "a resumed run replays the journaled terms".  The rollover that an edit
    # normally causes is covered by test_rollover_settles_with_the_contract_terms_in_effect.
    restamp_competition_universe(db, cid, ["RB0"], contracts_file=contracts)
    eng = Engine(make_cfg(tmp_path, contracts), db, accelerate=True)
    assert eng.comp_id == cid and eng.specs["RB0"].multiplier == 100
    t = stop_trade(db, cid)
    assert abs(t["realized_pnl"] - round((fill - avg) * 10 * qty, 4)) < 1e-6, "盈亏按触发时的乘数 10，而不是编辑后的 100"
    assert abs(t["fee"] - fee) < 1e-9
    eng.stop()


def test_rollover_settles_with_the_contract_terms_in_effect(tmp_path):
    contracts = contracts_copy(tmp_path)
    cid, avg, qty, fill, fee, journal = leave_pending_stop(tmp_path, contracts)
    edit_rb0(contracts, multiplier=100, fee_rate=0.001)
    db = Database(str(tmp_path / "e.db"))
    eng = Engine(make_cfg(tmp_path, contracts, initial_cash=500_000), db, accelerate=True)   # 起始资金变化 → 归档旧竞赛
    assert eng.comp_id != cid
    t = stop_trade(db, cid)
    assert abs(t["realized_pnl"] - round((fill - avg) * 10 * qty, 4)) < 1e-6 and abs(t["fee"] - fee) < 1e-9
    assert not journal.exists()
    eng.stop()


def test_pending_exchange_is_rebuilt_from_journaled_terms(tmp_path):
    contracts = contracts_copy(tmp_path)
    cfg = make_cfg(tmp_path, contracts)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    spec = asdict(eng.specs["RB0"])
    spec["multiplier"] = 7
    eng._pending_terms["ok"] = {"risk": {"slippage_ticks": 3, "min_hold_sec": 0, "liquidation_risk_ratio": 0.7, "maintenance_margin_ratio": 0.4},
                               "specs": {"RB0": spec}}
    ex = eng._pending_exchange("ok", eng.accounts["ok"])
    assert (ex.slippage_ticks, ex.liq_ratio, ex.maint_ratio, ex.specs["RB0"].multiplier) == (3, 0.7, 0.4, 7)
    assert ex is not eng.exchange and eng.exchange.specs["RB0"].multiplier == 10, "主撮合器不受影响"
    eng._pending_terms.pop("ok")
    assert eng._pending_exchange("ok", eng.accounts["ok"]) is eng.exchange, "无条款（旧格式日志）退回当前撮合器"
    eng.stop()


def test_legacy_journal_without_terms_still_settles(tmp_path):
    contracts = contracts_copy(tmp_path)
    cid, avg, qty, fill, fee, journal = leave_pending_stop(tmp_path, contracts)
    data = json.loads(journal.read_text(encoding="utf-8"))
    data["marks"]["ok"].pop("terms")
    journal.write_text(json.dumps(data), encoding="utf-8")
    db = Database(str(tmp_path / "e.db"))
    eng = Engine(make_cfg(tmp_path, contracts), db, accelerate=True)
    assert stop_trade(db, cid)["price"] == fill and not journal.exists()
    eng.stop()
