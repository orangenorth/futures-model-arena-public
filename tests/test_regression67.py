"""Regression 67：instruments 键归一化、落库 bar 按接入分开存、盘前逐品种新鲜度。

源把映射的键大写之后才用，写成 `rb0: rb2601` 同样把 RB0 钉死了；而竞赛身份签名拿原始键去筛，
`rb0` 被当成"没配"——改成 `rb2605` 时改动前后两份签名都是空映射，竞赛照旧沿用同一张排行榜，
价格序列却已经换了一条。

换了行情前置/改钉了合约确实会另起一场竞赛了，但 `md_bars` 只按 (品种, 周期, 时间戳) 存：新竞赛
的指标仍然被旧接入累出来的 bar 预热，之后两段历史还会被当成一条继续写下去。排行榜重开了，
实验其实没有隔离。

CTP 是订阅推送，`health().last_ok` 是**任意**一个已订阅合约最后一条 tick 的到达时刻。集合竞价里
一个品种报了价就能把它刷新，其余品种缓存的已发布报价可以任意陈旧——自动研究照样预约、照样花
四次模型调用，喂进去的是新鲜价与陈旧价掺在一起的快照。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from arena.db import Database
from arena.engine.loop import market_feed_signature
from arena.market.base import SourceError
from arena.market.ctp import CtpMarketSource
from arena.models import Bar

from .test_ctp import SH, FakeSdk, build_engine, feed, make_ctp_cfg

ROOT = Path(__file__).resolve().parents[1]



def restart(tmp_path, monkeypatch, **ctp):
    from arena.engine import loop as loop_mod

    def fake_load(self):
        self._sdk = FakeSdk()
        return self._sdk

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    cfg = make_ctp_cfg(tmp_path, **ctp)
    db = Database(cfg.db_path)
    return loop_mod.Engine(cfg, db, accelerate=True), db


def test_the_source_really_pins_a_lowercase_key(tmp_path):
    """先把前提钉住：小写键在行情源里是生效的，所以签名漏掉它才是缺陷。"""
    from arena.exchange.contracts import load_cn_contracts

    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = CtpMarketSource(["RB0"], {"RB0": specs["RB0"]}, holidays, md_front="tcp://127.0.0.1:1",
                          broker_id="0001", user_id="8", password="p", auto_months=2,
                          instruments={"rb0": "rb2601"}, discovery_grace_sec=0,
                          flow_dir=str(tmp_path / "flow"), sdk=FakeSdk())
    try:
        src.start()
        assert src.dominant_contract("RB0") == "rb2601", "小写键没能钉住合约，这条用例的前提不成立"
    finally:
        src.close()


def test_the_signature_normalizes_instrument_keys(tmp_path):
    lower = make_ctp_cfg(tmp_path, instruments={"rb0": "rb2601"})
    upper = make_ctp_cfg(tmp_path, instruments={"RB0": "rb2601"})
    row = market_feed_signature(lower.market_data, "ctp", lower.symbols)
    assert row["instruments"] == {"RB0": "rb2601"}, "小写键被当成没配，改它不会换竞赛"
    assert row == market_feed_signature(upper.market_data, "ctp", upper.symbols), \
        "同一个钉法写成两种大小写却算成两场实验"


def test_repinning_through_a_lowercase_key_starts_a_new_competition(tmp_path, monkeypatch):
    eng, db = restart(tmp_path, monkeypatch, instruments={"rb0": "rb2601"})
    first = eng.comp_id
    eng.stop()
    db.close()

    eng2, db2 = restart(tmp_path, monkeypatch, instruments={"rb0": "rb2605"})
    try:
        assert eng2.comp_id != first, "换了钉死的合约却仍然沿用同一张排行榜"
    finally:
        eng2.stop()
        db2.close()


# ---------- 2. 落库 bar 按行情接入分开存 ----------

def seeded_bars(source, symbol):
    """预热/实时累出来的 bar；一根都没有时 CTP 源是抛异常而不是返回空表。"""
    try:
        return source.klines(symbol, "1m", 100)
    except SourceError:
        return []


def bars(n=3, start=1_700_000_000.0):
    return [Bar(ts=start + 60 * i, open=3400.0 + i, high=3410.0, low=3390.0,
                close=3400.0 + i, volume=10.0, hold=500.0) for i in range(n)]


def test_two_feeds_keep_separate_bar_histories(tmp_path):
    db = Database(str(tmp_path / "x.db"))
    try:
        db.save_md_bars("RB0", bars(), bar_seconds=60, feed="aaaa")
        db.save_md_bars("RB0", [Bar(ts=b.ts, open=9000.0, high=9000.0, low=9000.0,
                                    close=9000.0, volume=1.0, hold=1.0) for b in bars()],
                        bar_seconds=60, feed="bbbb")
        first = db.md_bars("RB0", 100, bar_seconds=60, feed="aaaa")
        second = db.md_bars("RB0", 100, bar_seconds=60, feed="bbbb")
        assert len(first) == len(second) == 3
        assert first[0]["close"] == 3400.0 and second[0]["close"] == 9000.0, "两套接入的 bar 互相覆盖了"
        assert len(db.md_bars("RB0", 100, bar_seconds=60)) == 6, "两份历史都还在"
    finally:
        db.close()


def test_trimming_only_touches_its_own_feed(tmp_path):
    db = Database(str(tmp_path / "x.db"))
    try:
        db.save_md_bars("RB0", bars(5), bar_seconds=60, feed="aaaa")
        db.save_md_bars("RB0", bars(5), bar_seconds=60, keep=2, feed="bbbb")
        assert len(db.md_bars("RB0", 100, bar_seconds=60, feed="aaaa")) == 5, "写另一套接入时把这一份裁掉了"
        assert len(db.md_bars("RB0", 100, bar_seconds=60, feed="bbbb")) == 2
    finally:
        db.close()


def test_a_changed_feed_is_not_seeded_from_the_old_one(tmp_path, monkeypatch):
    eng, db = restart(tmp_path, monkeypatch)
    old_ns = eng._md_feed("RB0")
    try:
        feed(eng, "RB0", [3400.0, 3405.0, 3410.0])
        eng._persist_md_bars()
        assert db.md_bars("RB0", 100, bar_seconds=60, feed=old_ns), "这条用例需要先落库出 bar"
    finally:
        eng.stop()
        db.close()

    eng2, db2 = restart(tmp_path, monkeypatch, md_front="tcp://127.0.0.1:20131")
    try:
        assert eng2._md_feed("RB0") != old_ns
        eng2._seed_md_bars()
        assert not seeded_bars(eng2.source, "RB0"), "新接入被旧前置累出来的 bar 预热了"
        assert db2.md_bars("RB0", 100, bar_seconds=60, feed=old_ns), "旧接入的历史不该被删掉"
    finally:
        eng2.stop()
        db2.close()


def test_switching_back_finds_its_history_again(tmp_path, monkeypatch):
    eng, db = restart(tmp_path, monkeypatch)
    try:
        feed(eng, "RB0", [3400.0, 3405.0, 3410.0])
        eng._persist_md_bars()
    finally:
        eng.stop()
        db.close()

    eng2, db2 = restart(tmp_path, monkeypatch, md_front="tcp://127.0.0.1:20131")
    eng2.stop()
    db2.close()

    eng3, db3 = restart(tmp_path, monkeypatch)       # 换回原前置
    try:
        eng3._seed_md_bars()
        assert seeded_bars(eng3.source, "RB0"), "换回原接入之后那一份历史找不回来了"
    finally:
        eng3.stop()
        db3.close()


def test_adding_a_pinned_symbol_does_not_orphan_other_symbols_history(tmp_path, monkeypatch):
    """指纹按品种算：给 AP0 钉的合约不改变 RB0 的 bar 是怎么来的。

    按整份配置算的话，`instruments` 的子集会随 symbols 变——把一个本来就钉好、只是还没参赛的
    品种放进 symbols，就会把**所有**品种攒下的历史一次性弃掉，而且不报错。
    """
    eng, db = restart(tmp_path, monkeypatch, instruments={"RB0": "rb2601", "AP0": "AP601"})
    try:
        before = eng._md_feed("RB0")
        assert "AP0" not in eng.symbols
        eng.symbols = ["RB0", "I0", "AP0"]          # AP0 开始参赛，它的钉法进了生效集合
        assert eng._md_feed("RB0") == before, "加一个已钉好的品种把 RB0 的历史弃掉了"
        assert eng._md_feed("AP0") != before, "不同品种的钉法应当落在不同命名空间"
    finally:
        eng.stop()
        db.close()


@pytest.mark.parametrize("legacy_sql", [
    # 最早一版：没有 bar_seconds 这一列
    """CREATE TABLE md_bars (symbol TEXT NOT NULL, ts REAL NOT NULL, open REAL NOT NULL,
         high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
         volume REAL NOT NULL DEFAULT 0, hold REAL NOT NULL DEFAULT 0,
         PRIMARY KEY (symbol, ts))""",
    # 上一版：有 bar_seconds 进主键，但没有 feed
    """CREATE TABLE md_bars (symbol TEXT NOT NULL, ts REAL NOT NULL, open REAL NOT NULL,
         high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
         volume REAL NOT NULL DEFAULT 0, hold REAL NOT NULL DEFAULT 0,
         bar_seconds INTEGER NOT NULL DEFAULT 60,
         PRIMARY KEY (symbol, bar_seconds, ts))""",
])
def test_the_migration_keeps_old_rows_under_the_empty_namespace(tmp_path, legacy_sql):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(legacy_sql)
    cols = "symbol, ts, open, high, low, close, volume, hold" + (
        ", bar_seconds" if "bar_seconds" in legacy_sql else "")
    values = "?,?,?,?,?,?,?,?" + (",?" if "bar_seconds" in legacy_sql else "")
    row = ("RB0", 1_700_000_000.0, 3400.0, 3410.0, 3390.0, 3405.0, 10.0, 500.0)
    raw.execute(f"INSERT INTO md_bars ({cols}) VALUES ({values})",
                row + ((60,) if "bar_seconds" in legacy_sql else ()))
    raw.commit()
    raw.close()

    db = Database(str(path))
    try:
        pk = [r["name"] for r in db.conn().execute("PRAGMA table_info(md_bars)").fetchall() if r["pk"]]
        assert set(pk) == {"symbol", "feed", "bar_seconds", "ts"}, f"主键没升上去：{pk}"
        rows = db.md_bars("RB0", 100, bar_seconds=60, feed="")
        assert len(rows) == 1 and rows[0]["close"] == 3405.0, "升级把历史弄丢了"
    finally:
        db.close()


def test_a_feed_change_says_so_in_the_rollover_reason(tmp_path, monkeypatch):
    """切换原因要指到真正变了的那一处。

    此前把行情接入加进了续跑条件，却没给它一条原因分支：改了前置的使用者会看到
    "参赛品种（含顺序）或其合约条款已变更"，然后去翻一个根本没动过的地方。
    """
    eng, db = restart(tmp_path, monkeypatch)
    first = eng.comp_id
    eng.stop()
    db.close()

    eng2, db2 = restart(tmp_path, monkeypatch, md_front="tcp://127.0.0.1:20131")
    try:
        assert eng2.comp_id != first
        messages = [e["message"] for e in db2.events(first, 20)]
        assert any("行情接入设置" in m for m in messages), f"切换原因没指到行情接入：{messages}"
        assert not any("参赛品种" in m for m in messages), "把行情接入变更说成了品种/条款变更"
    finally:
        eng2.stop()
        db2.close()


# ---------- 3. 盘前逐品种新鲜度 ----------

def stale_quote(engine, symbol, age_sec):
    from arena.models import Quote

    return Quote(symbol=symbol, price=3400.0, ts=time.time() - age_sec, open_interest=1.0)


def test_one_ticking_contract_does_not_vouch_for_the_rest(tmp_path, monkeypatch):
    """集合竞价里一个品种报了价就把源级 last_ok 刷新了，其余品种的缓存价可以任意陈旧。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        opens_at = time.time() + 900
        monkeypatch.setattr(eng.source, "is_open", lambda at, symbol=None: True)
        limit = eng.cfg.preopen_debate.max_quote_age_sec
        quotes = {"RB0": stale_quote(eng, "RB0", 1.0), "I0": stale_quote(eng, "I0", limit + 60)}
        assert eng._preopen_unready_symbols(quotes, opens_at) == ["I0"]
        quotes["I0"] = stale_quote(eng, "I0", 1.0)
        assert eng._preopen_unready_symbols(quotes, opens_at) == []
    finally:
        eng.stop()
        db.close()


def test_a_stale_symbol_does_not_burn_the_reservation(tmp_path, monkeypatch):
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        started = []
        monkeypatch.setattr(eng, "_start_debate", lambda *a, **k: started.append(a) or True)
        monkeypatch.setattr(eng.source, "health", lambda: {"ok": True, "last_ok": time.time()})
        monkeypatch.setattr(eng.source, "is_open", lambda at, symbol=None: True)
        limit = eng.cfg.preopen_debate.max_quote_age_sec
        eng.quotes = {"RB0": stale_quote(eng, "RB0", 1.0), "I0": stale_quote(eng, "I0", limit + 60)}

        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 900))
        assert eng._maybe_start_preopen_debate() is False
        assert started == [], "只有一个品种报过价就把这个开盘时刻的预约占掉了"

        eng.quotes["I0"] = stale_quote(eng, "I0", 1.0)
        assert eng._maybe_start_preopen_debate() is True
        assert started, "所有要开盘的品种都新鲜了仍然不调度"
    finally:
        eng.stop()
        db.close()


def test_a_stale_symbol_still_gets_one_try_at_the_floor(tmp_path, monkeypatch):
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        eng.cfg.preopen_debate.enabled = True
        eng.accelerate = False
        started = []
        monkeypatch.setattr(eng, "_start_debate", lambda *a, **k: started.append(a) or True)
        monkeypatch.setattr(eng.source, "health", lambda: {"ok": True, "last_ok": time.time()})
        monkeypatch.setattr(eng.source, "is_open", lambda at, symbol=None: True)
        limit = eng.cfg.preopen_debate.max_quote_age_sec
        eng.quotes = {"RB0": stale_quote(eng, "RB0", 1.0), "I0": stale_quote(eng, "I0", limit + 60)}
        monkeypatch.setattr(eng, "_preopen_candidate",
                            lambda: ("morning_preopen", "早盘开盘前", time.time() + 30))
        assert eng._maybe_start_preopen_debate() is True
        assert started, "整个开盘时刻会悄无声息地跳过"
    finally:
        eng.stop()
        db.close()


def build_night_engine(tmp_path, monkeypatch):
    """RB0（有夜盘）+ IF0（中金所，只有日盘）的真实引擎，用合约表里的真实时段。"""
    from arena.config import AppConfig, TraderCfg
    from arena.engine import loop as loop_mod

    def fake_load(self):
        self._sdk = FakeSdk()
        return self._sdk

    monkeypatch.setattr(CtpMarketSource, "_load_sdk", fake_load)
    cfg = AppConfig(
        competition={"name": "night", "market": "ctp", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1,
                     "timezone": "Asia/Shanghai"},
        symbols=["RB0", "IF0"],
        market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json"},
                     "ctp": {"md_front": "tcp://127.0.0.1:10131", "broker_id": "0001",
                             "user_id": "88888888", "password": "secret",
                             "flow_dir": str(tmp_path / "flow"), "auto_months": 2,
                             "discovery_grace_sec": 0}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="rule", fallback_rule="trend")],
        storage={"db_path": str(tmp_path / "night.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    return loop_mod.Engine(cfg, db, accelerate=True), db


def test_a_day_only_symbol_does_not_block_the_night_session(tmp_path, monkeypatch):
    """反向护栏：夜盘只有一部分品种上市。

    拿"全部配置品种"要求新鲜报价的话，IF0（中金所，09:30-11:30 / 13:00-15:00）在 21:00
    挂的还是 15:00 的收盘价，永远超窗，夜盘研究一次都跑不了——那是把一个真缺陷换成另一个。
    这里用的是合约表里真实的时段，不是替身。
    """
    eng, db = build_night_engine(tmp_path, monkeypatch)
    try:
        night = datetime(2026, 9, 23, 21, 0, tzinfo=SH).timestamp()     # 周三夜盘，非节假日
        opening = eng._preopen_session_symbols(night)
        assert "RB0" in opening and "IF0" not in opening, f"夜盘开市品种判错了：{opening}"

        quotes = {"RB0": stale_quote(eng, "RB0", 1.0),
                  "IF0": stale_quote(eng, "IF0", 6 * 3600)}     # 还挂着 15:00 的收盘价
        assert eng._preopen_unready_symbols(quotes, night) == [], "日盘品种的收盘价把夜盘研究挡死了"

        day = datetime(2026, 9, 23, 9, 30, tzinfo=SH).timestamp()
        assert "IF0" in eng._preopen_session_symbols(day), "日盘 IF0 应当在要求之列"
        assert eng._preopen_unready_symbols(quotes, day) == ["IF0"]
    finally:
        eng.stop()
        db.close()


def test_unreadable_quote_times_block_automatic_admission(tmp_path, monkeypatch):
    """读不出的报价时间在这道闸门上就得拦下：自动会话的准入不再经过 `_manual_quote_only_reason`，
    这里放过去就等于放进模型（第 21 轮审查）。缺报价仍由覆盖度那一条判。"""
    eng, db = build_engine(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(eng.source, "is_open", lambda at, symbol=None: True)
        good = stale_quote(eng, "RB0", 1.0)
        broken = stale_quote(eng, "I0", 1.0)
        object.__setattr__(broken, "ts", float("nan"))
        assert eng._preopen_unready_symbols({"RB0": good, "I0": broken}, time.time() + 900) == ["I0"]
        assert eng._preopen_unready_symbols({"RB0": good}, time.time() + 900) == ["I0"], \
            "还没报过价的品种也没就绪：预约会被永久占掉（第 22 轮审查）"
    finally:
        eng.stop()
        db.close()


def test_manual_research_requires_every_configured_symbol(tmp_path, monkeypatch):
    """手动研究没有"这次要开的盘"，不能按"此刻开市"缩小新鲜度要求的范围。

    快照覆盖的是全部配置品种、花的也是四次模型调用：夜盘里一个只有日盘的品种挂着下午的
    收盘价照样会被送进模型；休市时"此刻开市"更是空集，等于这道闸门根本不存在。
    """
    eng, db = build_night_engine(tmp_path, monkeypatch)
    try:
        assert sorted(eng._preopen_session_symbols(None)) == ["IF0", "RB0"]
        quotes = {"RB0": stale_quote(eng, "RB0", 1.0),
                  "IF0": stale_quote(eng, "IF0", 6 * 3600)}
        assert eng._preopen_unready_symbols(quotes, None) == ["IF0"], \
            "手动研究会拿一个陈旧品种去调四个付费模型"

        # 全市场休市时同样要求：此刻开市是空集，缩小范围等于不设闸门
        monkeypatch.setattr(eng.source, "is_open", lambda at, symbol=None: False)
        assert eng._preopen_unready_symbols(quotes, None) == ["IF0"]
    finally:
        eng.stop()
        db.close()


def test_unlabelled_legacy_bars_are_never_read_back(tmp_path, monkeypatch):
    """升级前那批没有来源标记的行永远不参与预热。

    曾经据配置反推过它们属于哪套接入，三种依据全被证伪：升级与换前置可能在同一次重启里发生；
    上一场竞赛可能根本没跑过这个品种；`persist_bars` 关着时判定会被推迟到另一套接入之后才做
    （最后这条是 实测出来的：rb2601 → rb2605 关着持久化 → 同一套 rb2605 打开持久化，
    四根 rb2601 的行全部被认领成了 rb2605 的历史）。反推错了不报错，只是把另一条价格序列喂进
    指标，所以不再反推。行留着不删——删的是使用者的数据。
    """
    eng0, db0 = restart(tmp_path, monkeypatch)      # 先建出一场竞赛，配置一个字没动
    eng0.stop()
    db0.close()

    eng, db = restart(tmp_path, monkeypatch)        # 上一场跑的就是这套接入——曾经的认领条件
    ns = eng._md_feed("RB0")
    db.save_md_bars("RB0", bars(4), bar_seconds=60, feed="")
    try:
        eng._seed_md_bars()
        assert not seeded_bars(eng.source, "RB0"), "没有来源标记的历史被拿去预热了"
        assert len(db.md_bars("RB0", 100, bar_seconds=60, feed="")) == 4, "旧行不该被删掉"
        assert not db.md_bars("RB0", 100, bar_seconds=60, feed=ns), "旧行不该被贴上当前接入的指纹"
    finally:
        eng.stop()
        db.close()


def test_the_deferred_persistence_sequence_no_longer_adopts(tmp_path, monkeypatch):
    """实测的那条序列：钉 rb2601 → 改钉 rb2605 且关掉持久化 → 同一套 rb2605 打开持久化。

    打开持久化的那一刻，"紧邻的上一场竞赛"跑的确实是 rb2605，所以按上一场比对会通过；
    而库里那四根行其实是 rb2601 累出来的。
    """
    eng1, db1 = restart(tmp_path, monkeypatch, instruments={"RB0": "rb2601"})
    ns1 = eng1._md_feed("RB0")
    db1.save_md_bars("RB0", bars(4), bar_seconds=60, feed="")      # 升级前留下的、无来源标记的行
    eng1.stop()
    db1.close()

    eng2, db2 = restart(tmp_path, monkeypatch, instruments={"RB0": "rb2605"}, persist_bars=False)
    eng2._seed_md_bars()                                            # 持久化关着，什么都不做
    eng2.stop()
    db2.close()

    eng3, db3 = restart(tmp_path, monkeypatch, instruments={"RB0": "rb2605"})
    try:
        assert eng3._md_feed("RB0") != ns1
        eng3._seed_md_bars()
        assert not seeded_bars(eng3.source, "RB0"), "rb2601 累出来的行被当成 rb2605 的历史喂了进去"
        assert len(db3.md_bars("RB0", 100, bar_seconds=60, feed="")) == 4
    finally:
        eng3.stop()
        db3.close()
