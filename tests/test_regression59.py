"""Regression 59: 赛前研究的证据完整性与生命周期，以及归档辩论、行情字段的还原。

覆盖 六条发现：
1. `history_quality="full"` 必须每个品种都有可用的 5m/15m/1h 证据；
2. `quote_only` 清单不得发布整条全 null 的指标 fact；
3. 完成事件只在终态 CAS 命中后发布，且写进 worker 启动时捕获的竞赛；
4. 重置要等新竞赛写入成功后才中止在飞研究；
5. 交易员级 `max_retries` 属于竞赛身份的一部分；
6. 归档辩论按当时的参与者渲染，而不是当前配置名单。
另含新浪 `nf_` 报价成交量/持仓量字段的还原。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.debate import _fact_ids, build_market_manifest, has_usable_indicators
from arena.engine.loop import Engine, ResetBlocked
from arena.exchange.contracts import load_cn_contracts
from arena.market.sina_cn import SinaFuturesSource
from arena.preopen_universe import build_preopen_universe_research
from arena.research_scan import SCAN_RULES_VERSION, SCAN_SCHEMA_VERSION, canonical_manifest_hash

ROOT = Path(__file__).resolve().parents[1]
FULL_HEALTH = {"source": "replay", "ok": True, "history_quality": "full"}
MANIFEST = {"analysis_id": "a", "snapshot_hash": "h", "facts": [], "symbol_scores": [], "global_analysis": {}}


def make_cfg(tmp_path, **overrides):
    traders = [
        TraderCfg(id=tid, name=name, provider="openai_compatible", base_url="https://example.invalid/v1",
                  model=f"{tid}-test", api_key="test-key", **overrides.pop(f"trader_{tid}", {}))
        for tid, name in (("deepseek", "DeepSeek"), ("qwen", "通义千问"), ("kimi", "Kimi"), ("glm", "GLM"))
    ]
    return AppConfig(
        competition={"name": "r59", "market": "replay", "initial_cash": 80_000, "decision_interval_sec": 1,
                     "tick_interval_sec": 1, "snapshot_interval_sec": 1, "timezone": "Asia/Shanghai"},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        preopen_debate={"enabled": False, "stage_timeout_sec": 5},
        traders=traders,
        storage={"db_path": str(tmp_path / "r59.db")},
        base_dir=ROOT,
    )


@pytest.fixture
def engine(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    yield eng, db
    eng.stop()
    db.close()


def full_evidence_snapshot(eng):
    """回放行情只有几小时历史，1h 指标键齐全但值全是 None；补上可核验的值当作真实行情的完整快照。"""
    snap = eng.__class__._build_snapshot(eng)
    for view in snap.symbols.values():
        view.ind_1h = {"ema20": 3400.0, "ema60": 3390.0, "rsi14": 51.0, "chg5": 0.2, "chg20": 0.4, "macd_hist": 0.1}
    return snap


def full_universe_scan_manifest(*, runtime_gap: bool = False, invalid_catalogue_reason: bool = False):
    items = [
        {"symbol": "AA0", "name": "AA0 name", "exchange": "SHFE", "outcome": "scanned", "stage": "analysis",
         "reason": "", "latest_price": 101.0, "change_pct": 0.5, "trend": "bullish", "score": 3.0},
        {"symbol": "AB0", "name": "AB0 name", "exchange": "SHFE", "outcome": "scanned", "stage": "analysis",
         "reason": "", "latest_price": 99.0, "change_pct": -0.4, "trend": "bearish", "score": -3.0},
        {"symbol": "AC0", "name": "AC0 name", "exchange": "DCE", "outcome": "scanned", "stage": "analysis",
         "reason": "", "latest_price": 100.0, "change_pct": 0.0, "trend": "neutral", "score": 0.0},
        {"symbol": "AD0", "name": "AD0 name", "exchange": "SHFE", "outcome": "excluded",
         "stage": "catalogue", "reason": "catalogue_quote_proof_missing", "latest_price": None,
         "change_pct": None, "trend": None, "score": None},
    ]
    if runtime_gap:
        items[-1] = {"symbol": "AD0", "name": "AD0 name", "exchange": "SHFE", "outcome": "excluded",
                     "stage": "history", "reason": "history_timeout", "latest_price": None,
                     "change_pct": None, "trend": None, "score": None}
    if invalid_catalogue_reason:
        items[-1] = {"symbol": "AD0", "name": "AD0 name", "exchange": "SHFE", "outcome": "excluded",
                     "stage": "catalogue", "reason": "history_timeout", "latest_price": None,
                     "change_pct": None, "trend": None, "score": None}
    scanned = sum(item["outcome"] == "scanned" for item in items)
    catalogue_excluded = sum(item["outcome"] == "excluded" and item["stage"] == "catalogue" for item in items)
    manifest = {
        "schema_version": SCAN_SCHEMA_VERSION,
        "rules_version": SCAN_RULES_VERSION,
        "status": "incomplete" if runtime_gap else "partial",
        "catalogue": {"schema_version": 1, "content_hash": "sha256:review59", "snapshot_status": "complete",
                      "fetched_at": "2026-09-22T09:00:00+08:00"},
        "counts": {"discovered": len(items), "eligible": len(items) - catalogue_excluded,
                   "scanned": scanned, "excluded": len(items) - scanned},
        "coverage": {"status": "incomplete", "ratio": round(scanned / len(items), 6) if not runtime_gap else None,
                     "denominator_known": True},
        "started_at": 1_700_000_000.0,
        "completed_at": 1_700_000_001.0,
        "items": items,
    }
    manifest["manifest_hash"] = canonical_manifest_hash(manifest)
    return manifest


def full_universe_sidecar(*, runtime_gap: bool = False, invalid_catalogue_reason: bool = False):
    return build_preopen_universe_research(
        full_universe_scan_manifest(runtime_gap=runtime_gap, invalid_catalogue_reason=invalid_catalogue_reason)
    )


# ---------- 1) 完整证据 ----------

def test_full_evidence_requires_every_symbol_on_every_timeframe(engine, monkeypatch):
    """`_build_snapshot` 只要任意一个品种拿到 5m K 线就返回快照，所以"非 None"远不等于"证据完整"。
    按 full 去调模型，等于拿缺失的多周期证据当完整证据用。"""
    eng, _ = engine
    assert eng._incomplete_evidence(full_evidence_snapshot(eng)) is None

    monkeypatch.setattr(eng, "_build_snapshot", lambda **k: full_evidence_snapshot(eng))
    snap, health = eng._preopen_snapshot(1)
    assert snap is not None and health["history_quality"] == "full"

    def without_15m_and_1h(**kwargs):
        partial = full_evidence_snapshot(eng)
        for view in partial.symbols.values():
            view.ind_15m, view.ind_1h = {}, {}
        return partial

    monkeypatch.setattr(eng, "_build_snapshot", without_15m_and_1h)
    snap, health = eng._preopen_snapshot(1)
    assert snap is None and health["history_quality"] == "incomplete", "缺 15m/1h 不能标成 full"
    assert "15m" in health["detail"] and "1h" in health["detail"]


def test_a_snapshot_missing_one_symbol_is_not_full_evidence(engine, monkeypatch):
    eng, _ = engine

    def partial(**kwargs):
        snap = full_evidence_snapshot(eng)
        snap.symbols.pop("I0", None)
        return snap

    monkeypatch.setattr(eng, "_build_snapshot", partial)
    snap, health = eng._preopen_snapshot(1)
    assert snap is None and "无行情：I0" in health["detail"] and health["history_quality"] == "incomplete"


def test_indicator_keys_present_but_all_null_do_not_count_as_evidence(engine):
    """字典非空不等于有可核验的证据：回放行情的 1h 指标键齐全、值却全是 None。
    引擎的完整性判定必须与清单实际发布 fact 的口径一致，否则会出现"标成 full 却没有 1h fact"。"""
    eng, _ = engine
    snap, health = eng._preopen_snapshot(1)
    assert snap is None and "1h" in health["detail"], "回放行情凑不齐 1h 证据，应当失败关闭"


def test_incomplete_evidence_names_what_is_missing(engine):
    eng, _ = engine
    view = SimpleNamespace(ind_5m={"ema5": 1.0}, ind_15m={}, ind_1h={"ema20": 2.0})
    gap = eng._incomplete_evidence(SimpleNamespace(symbols={"RB0": view}))
    assert "无行情：I0" in gap and "RB0 缺 15m" in gap


# ---------- 2) quote_only 清单 ----------

def test_quote_only_manifest_omits_all_null_indicator_facts(engine):
    """整条全 null 的指标 fact 等于告诉模型"证据在这里"，它却无法从中核验任何东西；
    驱动 id 还会引用这些空 fact。"""
    eng, _ = engine
    snap = eng.__class__._build_snapshot(eng)
    for view in snap.symbols.values():
        view.ind_5m, view.ind_15m, view.ind_1h = {}, {}, {}
    manifest = build_market_manifest(snap, {**FULL_HEALTH, "history_quality": "quote_only"})
    assert {fact["kind"] for fact in manifest["facts"]} == {"quote"}
    for row in manifest["symbol_scores"]:
        assert row["driver_fact_ids"] == [f"{row['symbol']}:quote"]


def test_manifest_publishes_exactly_the_periods_that_carry_values(engine):
    """有值就发布、全 null 就不发布；驱动 id 始终只引用真正发布了的 fact。"""
    eng, _ = engine
    snap = full_evidence_snapshot(eng)
    manifest = build_market_manifest(snap, FULL_HEALTH)
    emitted = {fact["id"] for fact in manifest["facts"]}
    for symbol, view in snap.symbols.items():
        for period in ("5m", "15m", "1h"):
            usable = has_usable_indicators(getattr(view, f"ind_{period}"), period)
            assert (f"{symbol}:{period}" in emitted) is usable
    for row in manifest["symbol_scores"]:
        assert set(row["driver_fact_ids"]) <= emitted and len(row["driver_fact_ids"]) == 4


# ---------- 3) 完成事件 ----------

def _stub_debate(eng, monkeypatch):
    monkeypatch.setattr(eng, "_preopen_snapshot", lambda *a, **k: (full_evidence_snapshot(eng), dict(FULL_HEALTH)))

    def rows(stage):
        return [{"trader_id": t.id, "stage": stage, "status": "complete", "stance": "neutral", "confidence": 0.5,
                 "thesis": "t", "supporting_fact_ids": [], "counter_argument": "c", "invalidation": "i",
                 "challenge": "x", "challenge_fact_ids": [], "revised_thesis": "r", "target_trader_id": t.id,
                 "raw_response": "", "prompt": "", "latency_ms": 1, "tokens_in": 1, "tokens_out": 1, "error": ""}
                for t in eng.cfg.traders]

    monkeypatch.setattr(eng, "_run_debate_stage",
                        lambda comp_id, gen, epoch, did, stage, *a, **k: (rows(stage), False))


def test_completion_event_is_suppressed_when_the_session_is_already_terminal(engine, monkeypatch):
    """重置/重启会把在飞会话定成终态并记下真正的原因。终态 CAS 没命中还照常宣布"完成"，
    会让事件流与会话状态互相矛盾。"""
    eng, db = engine
    _stub_debate(eng, monkeypatch)
    took = []
    real_complete = db.complete_debate_session

    def complete(debate_id, status, *a, **k):
        ok = real_complete(debate_id, status, *a, **k)
        took.append((status, ok))
        return ok

    monkeypatch.setattr(db, "complete_debate_session", complete)
    monkeypatch.setattr(db, "set_debate_session_status", lambda *a, **k: True)
    original_reserve = db.reserve_debate_session

    def reserve(*a, **k):
        debate_id = original_reserve(*a, **k)
        db.abort_running_debates(eng.comp_id, "aborted elsewhere")   # 别处已经定了终态
        return debate_id

    monkeypatch.setattr(db, "reserve_debate_session", reserve)
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘", None, "manual",
                            allow_paused=True)
    assert ("ready_for_codex_review", False) in took, "终态 CAS 应当没命中"
    messages = [e["message"] for e in db.events(eng.comp_id, 50)]
    assert not any("辩论完成" in m for m in messages), f"不该宣布完成：{messages}"


def test_completion_event_is_published_when_the_cas_takes(engine, monkeypatch):
    eng, db = engine
    _stub_debate(eng, monkeypatch)
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘", None, "manual",
                            allow_paused=True)
    assert any("辩论完成" in e["message"] for e in db.events(eng.comp_id, 50))


def test_worker_events_go_to_the_competition_it_started_in(engine):
    """重置之后 self.comp_id 已是新竞赛；旧竞赛的在飞 worker 再用它写事件，
    新竞赛的事件流里会冒出一场它从未跑过的研究。"""
    eng, db = engine
    old_comp = eng.comp_id
    eng.reset()
    assert eng.comp_id != old_comp
    eng._event_best_effort("info", "debate", "迟到的旧竞赛事件", comp_id=old_comp)
    assert "迟到的旧竞赛事件" in [e["message"] for e in db.events(old_comp, 50)]
    assert "迟到的旧竞赛事件" not in [e["message"] for e in db.events(eng.comp_id, 50)]


# ---------- 4) 重置与在飞研究 ----------

def test_reset_keeps_research_alive_when_the_new_competition_cannot_be_written(engine, monkeypatch):
    """aborted_restarted 是终态：重置失败、旧竞赛继续进行时提前打上去，
    仍然有效的 worker 再也写不回结果——那次付费研究白花。"""
    eng, db = engine
    debate_id = db.reserve_debate_session(eng.comp_id, "manual:1", "day", "日盘", None, MANIFEST)
    assert debate_id is not None

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "create_competition", boom)
    with pytest.raises(ResetBlocked):
        eng.reset()
    assert db.debate_session(eng.comp_id, debate_id)["status"] == "running", "重置没成功就不该中止在飞研究"


def test_reset_aborts_research_once_the_new_competition_exists(engine):
    eng, db = engine
    old_comp = eng.comp_id
    debate_id = db.reserve_debate_session(old_comp, "manual:2", "day", "日盘", None, MANIFEST)
    eng.reset()
    assert db.debate_session(old_comp, debate_id)["status"] == "aborted_restarted"


# ---------- 5) 竞赛身份 ----------

def test_changing_a_trader_retry_override_starts_a_new_competition(tmp_path):
    """重试次数不同，同一轮可能拿到不同决策：不能在同一张排行榜下接着跑。"""
    base = make_cfg(tmp_path)
    changed = make_cfg(tmp_path, trader_deepseek={"max_retries": 3})
    assert changed.traders[0].max_retries == 3 and base.traders[0].max_retries is None
    base_sig = Engine._configured_roster_signature(base)
    changed_sig = Engine._configured_roster_signature(changed)
    assert base_sig["deepseek"] != changed_sig["deepseek"]
    assert base_sig["qwen"] == changed_sig["qwen"], "没改的选手不受影响"


# ---------- 6) 归档辩论的参与者 ----------

def _debate_participant_ids(turns, current_ids):
    """在 node 里跑看板里那个函数的本体，而不是复刻它的逻辑。"""
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function debateParticipantIds\(d, detail\) \{.*?\n  \}", source, re.S)
    assert match, "web/app.js 里找不到 debateParticipantIds"
    script = (match.group(0) + "\nconsole.log(JSON.stringify(debateParticipantIds("
              + json.dumps({"traders": [{"id": tid} for tid in current_ids]}) + ", "
              + json.dumps({"turns": turns}) + ")));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_archived_debate_renders_the_participants_that_took_part():
    """交易员被移出配置后竞赛仍会续跑（`_same_roster` 有意如此），
    但归档的四条回合不能因此少显示一个模型，时间线也不能把它判成未完成。"""
    turns = [{"stage": "opening", "trader_id": tid, "status": "complete"}
             for tid in ("deepseek", "qwen", "kimi", "glm")]
    assert _debate_participant_ids(turns, ["deepseek", "qwen", "kimi"]) == ["deepseek", "qwen", "kimi", "glm"]


def test_participants_fall_back_to_the_current_roster_before_any_turn_exists():
    assert _debate_participant_ids([], ["deepseek", "qwen"]) == ["deepseek", "qwen"]


def test_timeline_counts_against_the_archived_participants():
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    timeline = source[source.index("function renderDebateTimeline"):]
    assert "debateParticipantIds(state.data, detail)" in timeline[:timeline.index("\n  }")], \
        "完成度必须按归档参与者数判定"


# ---------- 新浪 nf_ 报价字段 ----------

def test_sina_quote_maps_field_14_to_volume_and_13_to_open_interest():
    """nf_ 报价里 13 是持仓量、14 是成交量（与代码已经在用的 1/2/3/4/8/10/17 出自同一套字段表）。
    颠倒会把成交量当持仓量报给模型与看板。"""
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    src = SinaFuturesSource(["RB0"], specs, holidays)
    try:
        text = ('var hq_str_nf_RB0="螺纹钢连续,090501,3400,3410,3390,0,0,0,3405,0,'
                '3395,0,0,2200000,1500000,0,0,20260916";')
        bar = src._parse_minute(src._spot_to_minute_rows(text, "RB0"))[0]
        assert bar.volume == 1500000.0, "成交量取字段 14"
        assert bar.hold == 2200000.0, "持仓量取字段 13"
    finally:
        src.close()


# ---------- 7) 阶段落库前的生命周期复查（发现） ----------

def test_turns_are_not_appended_to_a_session_that_went_terminal(engine):
    """收完最后一个 future 到落库之间发生暂停/重置/停止时，abort_running_debates() 已把会话定成终态；
    再追加回合，归档记录就会在"已中止"之后继续长出模型输出。"""
    eng, db = engine
    debate_id = db.reserve_debate_session(eng.comp_id, "manual:7", "day", "日盘", None, MANIFEST)
    assert db.add_debate_turn(debate_id, "opening", "deepseek", 1, "complete", {"stance": "neutral"},
                              only_active=True) is True
    db.abort_running_debates(eng.comp_id, "reset during the stage")
    assert db.add_debate_turn(debate_id, "opening", "qwen", 2, "complete", {"stance": "neutral"},
                              only_active=True) is False, "终态会话不该再接受回合"
    stages = [t["trader_id"] for t in db.debate_session(eng.comp_id, debate_id)["turns"]]
    assert stages == ["deepseek"], f"只有中止之前那条该留下：{stages}"
    # 不带 only_active 的调用（历史路径）行为不变
    assert db.add_debate_turn(debate_id, "opening", "kimi", 3, "complete", {"stance": "neutral"}) is True


def test_stage_rechecks_the_lifecycle_before_persisting(engine, monkeypatch):
    """`while pending:` 收完最后一个 future 就退出，不会再走一次循环顶部的复查——
    暂停/重置/停止恰好落在这个缺口里时，落库循环此前是无条件写的。
    用单个参与者让所有 future 在同一轮收完，正好打在这个缺口上。"""
    eng, db = engine
    debate_id = db.reserve_debate_session(eng.comp_id, "manual:8", "day", "日盘", None, MANIFEST)
    calls = {"n": 0}

    def current_until_persistence(*a, **k):
        calls["n"] += 1
        return calls["n"] < 3        # 1=提交闸门，2=循环顶部，3=落库前的复查

    monkeypatch.setattr(eng, "_debate_is_current", current_until_persistence)
    monkeypatch.setattr(eng, "_call_debate_model",
                        lambda *a, **k: {"status": "complete", "payload": {"stance": "neutral"}, "prompt": "p",
                                         "raw_response": "r", "latency_ms": 1})
    rows, aborted = eng._run_debate_stage(eng.comp_id, eng._generation, eng._decision_epoch, debate_id, "opening",
                                          ["deepseek"], {"deepseek": "prompt"}, set(), allow_paused=True)
    assert calls["n"] == 3, "落库前必须再复查一次生命周期"
    assert rows == [] and aborted is True
    assert db.debate_session(eng.comp_id, debate_id)["turns"] == [], "被中止的阶段不该留下任何回合"


# ---------- 8) 启动时配置切换的同一条约束 ----------

def test_rollover_keeps_research_alive_when_the_new_competition_cannot_be_written(tmp_path, monkeypatch):
    """启动时市场/名单/资金/时区/保证金策略变更会新建竞赛。这条路径与 reset() 受同一条约束：
    `create_competition()` 失败时切换没有发生、旧竞赛仍然进行中，此时旧竞赛的在飞研究不该已被定成终态。"""
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    old_comp = eng.comp_id
    eng.stop()          # stop() 本身会中止在飞研究，所以会话要在停机之后再登记
    debate_id = db.reserve_debate_session(old_comp, "manual:9", "day", "日盘", None, MANIFEST)

    changed = make_cfg(tmp_path)
    changed.competition.initial_cash = 123_456          # 触发"起始资金变更 → 新建竞赛"

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "create_competition", boom)
    with pytest.raises(RuntimeError):
        Engine(changed, db, accelerate=True)
    assert db.debate_session(old_comp, debate_id)["status"] == "running", "切换没成功就不该中止旧竞赛的在飞研究"
    db.close()


def test_rollover_aborts_research_once_the_new_competition_exists(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    old_comp = eng.comp_id
    eng.stop()
    debate_id = db.reserve_debate_session(old_comp, "manual:10", "day", "日盘", None, MANIFEST)

    changed = make_cfg(tmp_path)
    changed.competition.initial_cash = 123_456
    eng2 = Engine(changed, db, accelerate=True)
    assert eng2.comp_id != old_comp
    assert db.debate_session(old_comp, debate_id)["status"] == "aborted_restarted"
    eng2.stop()
    db.close()


# ---------- 9) 自动盘前研究不得在开盘后才发出付费调用 ----------

def _count_calls(eng, monkeypatch):
    """记录实际发出的模型调用次数，并给出一份完整证据快照。"""
    calls = []
    monkeypatch.setattr(eng, "_preopen_snapshot", lambda *a, **k: (full_evidence_snapshot(eng), dict(FULL_HEALTH)))
    monkeypatch.setattr(eng, "_call_debate_model",
                        lambda trader_id, *a, **k: (calls.append(trader_id) or
                                                    {"status": "complete", "payload": {"stance": "neutral"},
                                                     "prompt": "p", "raw_response": "r", "latency_ms": 1}))
    return calls


def test_cn_preopen_full_scope_runs_eight_calls_without_mutating_the_execution_pool(engine, monkeypatch):
    eng, db = engine
    eng.cfg.competition.market = "cn_futures"
    sidecar = full_universe_sidecar()
    execution_symbols = list(eng.symbols)
    execution_spec_keys = set(eng.specs)
    exchange_spec_keys = set(eng.exchange.specs)
    risk_spec_keys = set(eng.risk.specs)
    calls = _count_calls(eng, monkeypatch)
    monkeypatch.setattr(eng, "_preopen_universe_research", lambda *args, **kwargs: sidecar)

    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘", None, "manual",
                            allow_paused=True)

    assert len(calls) == 8, f"Only two stages for four models may run: {calls}"
    detail = db.debate_session(eng.comp_id, db.debate_sessions(eng.comp_id)[0]["id"])
    snapshot = detail["snapshot"]
    assert snapshot["global_analysis"]["scope"] == "full_universe_5m_technical_breadth"
    assert snapshot["global_analysis"]["scope_status"] == "complete"
    assert len(snapshot["universe_research"]["items"]) == 4
    assert {item["state"] for item in snapshot["universe_research"]["items"]} == {"scanned", "catalogue_excluded"}
    assert any(item["id"] == "universe:coverage" for item in snapshot["facts"])
    assert [row["symbol"] for row in snapshot["symbol_scores"]] == sorted(execution_symbols)
    assert eng.symbols == execution_symbols
    assert set(eng.specs) == execution_spec_keys
    assert set(eng.exchange.specs) == exchange_spec_keys
    assert set(eng.risk.specs) == risk_spec_keys


def test_cn_preopen_incomplete_scope_persists_the_snapshot_but_calls_no_models(engine, monkeypatch):
    eng, db = engine
    eng.cfg.competition.market = "cn_futures"
    sidecar = full_universe_sidecar(runtime_gap=True)
    calls = []
    monkeypatch.setattr(eng, "_preopen_snapshot", lambda *args, **kwargs: (full_evidence_snapshot(eng), dict(FULL_HEALTH)))
    monkeypatch.setattr(eng, "_preopen_universe_research", lambda *args, **kwargs: sidecar)
    monkeypatch.setattr(eng, "_call_debate_model", lambda *args, **kwargs: calls.append(args[0]))

    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘", None, "manual",
                            allow_paused=True)

    assert calls == []
    detail = db.debate_session(eng.comp_id, db.debate_sessions(eng.comp_id)[0]["id"])
    assert detail["status"] == "unavailable"
    snapshot = detail["snapshot"]
    assert snapshot["universe_research"]["scope_status"] == "incomplete"
    assert snapshot["universe_research"]["counts"]["runtime_excluded"] == 1
    assert snapshot["global_analysis"]["market_regime"] == "unknown"
    assert any("全量候选研究覆盖不完整" in event["message"] for event in db.events(eng.comp_id, 50))


def test_cn_preopen_invalid_catalogue_stage_persists_no_models(engine, monkeypatch):
    eng, db = engine
    eng.cfg.competition.market = "cn_futures"
    sidecar = full_universe_sidecar(invalid_catalogue_reason=True)
    calls = []
    monkeypatch.setattr(eng, "_preopen_snapshot", lambda *args, **kwargs: (full_evidence_snapshot(eng), dict(FULL_HEALTH)))
    monkeypatch.setattr(eng, "_preopen_universe_research", lambda *args, **kwargs: sidecar)
    monkeypatch.setattr(eng, "_call_debate_model", lambda *args, **kwargs: calls.append(args[0]))

    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘", None, "manual",
                            allow_paused=True)

    assert sidecar["scope_status"] == "unavailable"
    assert calls == []
    detail = db.debate_session(eng.comp_id, db.debate_sessions(eng.comp_id)[0]["id"])
    assert detail["status"] == "unavailable"
    assert detail["snapshot"]["universe_research"]["scope_status"] == "unavailable"


def test_cn_preopen_refreshes_dynamic_catalogue_before_full_universe_scan(engine, monkeypatch, tmp_path):
    """A stale committed candidate must never become the live debate denominator."""
    eng, _ = engine
    eng.cfg.competition.market = "cn_futures"
    eng.cfg.base_dir = tmp_path
    observed = {}

    def discover(path, *, timeout_sec, workers):
        path = Path(path)
        observed["discovery_path"] = path
        observed["timeout_sec"] = timeout_sec
        observed["workers"] = workers
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return {"snapshot_status": "complete"}

    class Scanner:
        def __init__(self, catalogue_path, _reader, **kwargs):
            observed["scanner_path"] = Path(catalogue_path)

        def run(self):
            return full_universe_scan_manifest()

    monkeypatch.setattr("arena.engine.loop.discover_catalogue", discover)
    monkeypatch.setattr("arena.engine.loop.FullUniverseScanner", Scanner)
    monkeypatch.setattr(eng, "_preopen_universe_reader_instance", lambda: object())

    research = eng._preopen_universe_research(eng.comp_id, eng._generation, eng._decision_epoch, allow_paused=False)

    assert research["scope_status"] == "complete"
    assert observed["discovery_path"] == observed["scanner_path"]
    assert observed["discovery_path"].parent == tmp_path / "data" / "preopen-catalogues"
    assert observed["discovery_path"].name.endswith(".json")
    assert observed["timeout_sec"] == eng.cfg.product_research.discovery_timeout_sec
    assert observed["workers"] == eng.cfg.product_research.discovery_workers


def test_cn_preopen_scan_cancellation_tracks_the_debate_lifecycle(engine, monkeypatch):
    eng, _ = engine
    eng.cfg.competition.market = "cn_futures"
    observed = []

    class Scanner:
        def __init__(self, *args, cancel_event, **kwargs):
            self.cancel_event = cancel_event

        def run(self):
            observed.append(self.cancel_event.is_set())
            eng._decision_epoch += 1
            observed.append(self.cancel_event.is_set())
            return {}

    monkeypatch.setattr("arena.engine.loop.discover_catalogue", lambda *a, **k: {})
    monkeypatch.setattr("arena.engine.loop.FullUniverseScanner", Scanner)
    monkeypatch.setattr(eng, "_preopen_universe_reader_instance", lambda: object())
    research = eng._preopen_universe_research(eng.comp_id, eng._generation, eng._decision_epoch, allow_paused=False)

    assert observed == [False, True]
    assert research["scope_status"] == "unavailable"


def test_automatic_research_does_not_call_models_after_the_market_opened(engine, monkeypatch):
    """`_preopen_candidate()` 在开盘前接单，但引擎恰在开盘点附近启动、或历史请求把快照构建拖久了，
    真正发调用时可能已经开盘：那批付费调用会被归档成一场"盘前"会话，还和正常决策轮抢同一批供应商额度。"""
    eng, db = engine
    calls = _count_calls(eng, monkeypatch)
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘",
                            time.time() - 60, "automatic", allow_paused=True)
    assert calls == [], "已过开盘时间就不该发出任何付费调用"
    session = db.debate_sessions(eng.comp_id)[0]
    assert session["status"] == "unavailable"
    assert any("已过开盘时间" in e["message"] for e in db.events(eng.comp_id, 50))


def test_a_manual_session_is_not_blocked_by_the_opening_time(engine, monkeypatch):
    """手动研究由用户显式发起，不受开盘时刻限制。"""
    eng, _ = engine
    calls = _count_calls(eng, monkeypatch)
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘",
                            time.time() - 60, "manual", allow_paused=True)
    assert len(calls) == 8, f"手动研究应当照常跑完开场与反证：{calls}"


def test_automatic_research_before_the_open_still_runs(engine, monkeypatch):
    eng, _ = engine
    calls = _count_calls(eng, monkeypatch)
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘",
                            time.time() + 600, "automatic", allow_paused=True)
    assert len(calls) == 8


def test_rebuttal_is_skipped_when_the_market_opens_during_the_opening_stage(engine, monkeypatch):
    """开场跑完期间开盘：已经调用过模型，所以记 incomplete 而不是 unavailable，且不再发反证那四次。"""
    eng, db = engine
    calls = _count_calls(eng, monkeypatch)
    expired = {"n": 0}

    def expire_after_opening(trigger, opens_at):
        expired["n"] += 1
        return expired["n"] > 1      # 开场前还没开盘，反证前已经开盘

    monkeypatch.setattr(eng, "_preopen_window_expired", expire_after_opening)
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘",
                            time.time() + 600, "automatic", allow_paused=True)
    assert len(calls) == 4, f"只应发出开场那四次：{calls}"
    assert db.debate_sessions(eng.comp_id)[0]["status"] == "incomplete"


# ---------- 10) 引用的 fact 必须全部来自冻结事实集 ----------

def test_a_hallucinated_fact_id_rejects_the_whole_citation():
    """真 id 与编造的 id 混在一起时，原来是静默丢弃后者、剩下的够数就算通过：
    归档的 payload 看上去完全通过校验，论点却引用了事实集之外的东西。"""
    allowed = {"RB0:quote", "RB0:5m"}
    assert _fact_ids(["RB0:quote", "RB0:5m"], allowed) == ["RB0:quote", "RB0:5m"]
    with pytest.raises(ValueError, match="outside the frozen manifest"):
        _fact_ids(["RB0:quote", "RB0:编造的"], allowed)
    with pytest.raises(ValueError, match="outside the frozen manifest"):
        _fact_ids(["RB0:quote", 42], allowed)
    # 重复的合法 id 不算编造证据，仍然只去重
    assert _fact_ids(["RB0:quote", "RB0:quote"], allowed) == ["RB0:quote"]
    with pytest.raises(ValueError, match="enough valid fact ids"):
        _fact_ids([], allowed)


# ---------- 11) 相位按交易所时区判定 ----------

@pytest.mark.parametrize("display_tz", ["UTC", "America/New_York", "Asia/Shanghai"])
def test_preopen_phase_is_classified_in_the_exchange_timezone(engine, monkeypatch, display_tz):
    """09:00/13:30/21:00 是交易所本地时刻。按 competition.timezone（仅展示用）判定的话，
    上海 09:00 开盘在 UTC 下是 01:00，对不上任何相位、自动研究整条错过；纽约时区下还会错标成夜盘。"""
    eng, _ = engine
    shanghai = ZoneInfo("Asia/Shanghai")
    eng.cfg.competition.market = "cn_futures"
    eng.cfg.preopen_debate.enabled = True
    eng.cfg.preopen_debate.lead_window_sec = 7200
    eng.accelerate = False
    monkeypatch.setattr(eng, "tz", ZoneInfo(display_tz))
    monkeypatch.setattr(eng.source, "tz", shanghai, raising=False)

    opens = datetime.now(shanghai).replace(hour=9, minute=0, second=0, microsecond=0)
    if opens <= datetime.now(shanghai):
        opens += timedelta(days=1)
    monkeypatch.setattr(eng.source, "next_open", lambda now: opens.astimezone(now.tzinfo), raising=False)
    # 把前置窗口放宽到足以覆盖到明天的那次开盘，本用例只考察相位判定
    eng.cfg.preopen_debate.lead_window_sec = int((opens - datetime.now(shanghai)).total_seconds()) + 60

    candidate = eng._preopen_candidate()
    assert candidate is not None, f"展示时区 {display_tz} 下相位判定失败"
    phase, label, opens_at = candidate
    assert phase == "morning_preopen" and label == "早盘开盘前"
    assert abs(opens_at - opens.timestamp()) < 1, "调度仍用绝对时间戳"


# ---------- 12) 终态写入的单一出口：事件与版本都挂在 CAS 结果上 ----------

def test_unavailable_event_is_not_emitted_when_the_session_went_terminal(engine, monkeypatch):
    """重置/停止已经把会话定成终态并记下真正原因时，再发一条"因证据不足未调用模型"，
    归档竞赛里就留下与 aborted_restarted 自相矛盾的记录。"""
    eng, db = engine
    # 证据不足这条路径：给一个缺品种的快照
    def partial(*a, **k):
        snap = full_evidence_snapshot(eng)
        snap.symbols.pop("I0", None)
        return snap, {**FULL_HEALTH, "history_quality": "quote_only"}

    monkeypatch.setattr(eng, "_preopen_snapshot", partial)
    original_reserve = db.reserve_debate_session

    def reserve(*a, **k):
        debate_id = original_reserve(*a, **k)
        db.abort_running_debates(eng.comp_id, "aborted elsewhere")     # 别处抢先定了终态
        return debate_id

    monkeypatch.setattr(db, "reserve_debate_session", reserve)
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘", None, "manual",
                            allow_paused=True)
    messages = [e["message"] for e in db.events(eng.comp_id, 50)]
    assert not any("未调用模型" in m for m in messages), f"终态已被别处写定，不该再发这条：{messages}"
    assert db.debate_sessions(eng.comp_id)[0]["status"] == "aborted_restarted", "别处写下的原因必须保留"


def test_the_unavailable_event_is_emitted_when_the_cas_takes(engine, monkeypatch):
    eng, db = engine

    def partial(*a, **k):
        snap = full_evidence_snapshot(eng)
        snap.symbols.pop("I0", None)
        return snap, {**FULL_HEALTH, "history_quality": "quote_only"}

    monkeypatch.setattr(eng, "_preopen_snapshot", partial)
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘", None, "manual",
                            allow_paused=True)
    assert any("未调用模型" in e["message"] for e in db.events(eng.comp_id, 50))
    assert db.debate_sessions(eng.comp_id)[0]["status"] == "unavailable"


def test_an_aborted_stage_bumps_the_dashboard_version(engine, monkeypatch):
    """写了终态却不推版本时，看板可能一直停在"正在开场"：控制轮询已经消费掉 pause() 那次
    版本自增，缓存的 detail 状态又盖住新返回的最新状态。"""
    eng, db = engine
    _stub_debate(eng, monkeypatch)
    calls = {"n": 0}

    def current_until_stage(*a, **k):
        calls["n"] += 1
        return calls["n"] < 3            # 快照复查与预占复查通过，下一处生命周期复查立刻失效

    monkeypatch.setattr(eng, "_debate_is_current", current_until_stage)
    before = eng._version
    eng._run_preopen_debate(eng.comp_id, eng._generation, eng._decision_epoch, "day", "日盘", None, "manual",
                            allow_paused=True)
    after = eng._version
    assert after > before, "终态写入成功后必须推版本，否则看板不会再拉一次研究详情"


# ---------- 13) 看板投影的相位同样按交易所时区 ----------

@pytest.mark.parametrize("display_tz", ["UTC", "America/New_York", "Asia/Shanghai"])
def test_dashboard_next_phase_is_classified_in_the_exchange_timezone(engine, monkeypatch, display_tz):
    """调度那处修好之后，/api/state 这份独立投影仍在用展示时区换算：
    上海 09:00 在 UTC 下变成 01:00，于是"调度已经排上了、看板却说没有下一场"。"""
    eng, _ = engine
    shanghai = ZoneInfo("Asia/Shanghai")
    eng.cfg.competition.market = "cn_futures"
    eng.cfg.preopen_debate.enabled = True
    eng.accelerate = False
    monkeypatch.setattr(eng, "tz", ZoneInfo(display_tz))
    monkeypatch.setattr(eng.source, "tz", shanghai, raising=False)
    opens = datetime.now(shanghai).replace(hour=9, minute=0, second=0, microsecond=0)
    if opens <= datetime.now(shanghai):
        opens += timedelta(days=1)
    monkeypatch.setattr(eng.source, "next_open", lambda now: opens.astimezone(now.tzinfo), raising=False)

    room = eng.state()["preopen_debate"]
    assert room.get("next") is not None, f"展示时区 {display_tz} 下看板丢了下一场"
    assert room["next"]["phase"] == "morning_preopen"
