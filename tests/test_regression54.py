"""Regression 54: 80k competition terms and research-only pre-open debate lifecycle."""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from arena.config import AppConfig, RiskCfg, TraderCfg
from arena.debate import build_market_manifest
from arena.db import Database
from arena.engine.loop import Engine
from arena.models import Position, Quote
from arena.server.api import create_app


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
        # history 要够深：1h 指标（ema60/rsi14/macd_hist）需要几十根小时线，否则这份快照凑不齐完整证据，
        # 研究会按"证据不全"失败关闭，根本不会创建会话
        market_data={"replay": {"seed": 3, "history": 4000, "start_price": {"RB0": 3400, "I0": 800}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"max_margin_risk_ratio": 0.95, "warning_margin_risk_ratio": 0.90, "min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0, "breaker_failures": 2, "breaker_skip_rounds": 2},
        preopen_debate={"enabled": False, "stage_timeout_sec": 5, "max_output_tokens": 128},
        traders=traders,
        storage={"db_path": str(tmp_path / "review54.db")},
        base_dir=ROOT,
    )


def _complete_debate(engine: Engine, *, fail_opening_for: str | None = None):
    engine._tick()
    calls = []

    def fake_call(trader_id, prompt, stage, valid_fact_ids, participant_ids):
        calls.append((trader_id, stage, prompt))
        fact_id = sorted(valid_fact_ids)[0]
        if stage == "opening" and trader_id == fail_opening_for:
            return {"status": "failed", "error": "simulated unavailable participant", "prompt": prompt, "latency_ms": 1}
        if stage == "opening":
            payload = {
                "stance": "bullish" if trader_id in {"deepseek", "qwen"} else "bearish",
                "confidence": 0.6,
                "thesis": f"{trader_id} opening thesis",
                "supporting_fact_ids": [fact_id],
                "counter_fact_ids": [fact_id],
                "counter_argument": "known counter case",
                "invalidation": "a cited fact changes",
                "uncertainties": ["one uncertainty"],
            }
        else:
            target = next(item for item in sorted(participant_ids) if item != trader_id)
            payload = {
                "stance": "neutral",
                "confidence": 0.5,
                "target_trader_id": target,
                "challenge": "a fact-cited challenge",
                "challenge_fact_ids": [fact_id],
                "revised_thesis": "revised research thesis",
                "invalidation": "a cited fact changes",
            }
        return {"status": "complete", "payload": payload, "raw_response": "{redacted mock}", "prompt": prompt,
                "latency_ms": 1, "tokens_in": 10, "tokens_out": 20}

    engine._call_debate_model = fake_call  # type: ignore[method-assign]
    engine._run_preopen_debate(
        engine.comp_id, engine._generation, engine._decision_epoch,
        "manual_research", "手动赛前研究", None, "manual",
    )
    return calls


def _reserve_terminal_debate(db: Database, comp_id: int, session_key: str, status: str) -> int:
    manifest = {"analysis_id": f"fixture:{session_key}", "snapshot_hash": session_key,
                "market_as_of": 1, "global_analysis": {}}
    debate_id = db.reserve_debate_session(comp_id, session_key, "manual_research", "手动赛前研究", None, manifest)
    assert debate_id is not None
    assert db.complete_debate_session(debate_id, status)
    return debate_id


def test_debate_uses_one_account_free_snapshot_and_never_creates_a_trade(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        calls = _complete_debate(engine)
        openings = [prompt for _, stage, prompt in calls if stage == "opening"]
        rebuttals = [prompt for _, stage, prompt in calls if stage == "rebuttal"]
        assert len(openings) == 4 and len(set(openings)) == 1
        assert len(rebuttals) == 4
        assert all(all(trader_id in prompt for trader_id in ("deepseek", "qwen", "kimi", "glm")) for prompt in rebuttals)
        latest = db.latest_debate_session(engine.comp_id)
        assert latest and latest["status"] == "ready_for_codex_review"
        detail = db.debate_session(engine.comp_id, latest["id"], include_raw=False)
        assert detail and len(detail["turns"]) == 8
        assert all(turn["status"] == "complete" for turn in detail["turns"])
        facts_text = str(detail["snapshot"])
        assert "cash" not in facts_text.lower() and "position" not in facts_text.lower()
        assert db.trades(engine.comp_id, limit=10) == []
        assert db.decisions(engine.comp_id, limit=10) == []
        state = engine.state()
        assert state["competition"]["initial_cash"] == 80_000
        assert state["competition"]["risk"]["max_margin_risk_ratio"] == 0.95
        assert state["preopen_debate"]["latest"]["id"] == latest["id"]
    finally:
        engine.stop()
        db.close()


def test_incomplete_opening_never_enters_rebuttal_or_fakes_a_model_view(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        calls = _complete_debate(engine, fail_opening_for="kimi")
        assert [stage for _, stage, _ in calls].count("opening") == 4
        assert not [stage for _, stage, _ in calls if stage == "rebuttal"]
        latest = db.latest_debate_session(engine.comp_id)
        assert latest and latest["status"] == "incomplete"
        detail = db.debate_session(engine.comp_id, latest["id"], include_raw=False)
        failed = next(turn for turn in detail["turns"] if turn["trader_id"] == "kimi")
        assert failed["status"] == "failed" and not failed.get("stance")
    finally:
        engine.stop()
        db.close()


def test_only_manual_research_can_use_fresh_quote_only_fallback(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.competition.market = "cn_futures"
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=False)

    class QuoteOnlySource:
        name = "quote_only_fixture"
        stale_health = False

        def latest(self, symbols):
            prior_session = time.time() - 8 * 60 * 60
            return {symbol: Quote(symbol, 3400 + index, prior_session, change_pct=0.1)
                    for index, symbol in enumerate(symbols)}

        def klines(self, *args, **kwargs):
            raise RuntimeError("historical feed unavailable")

        def health(self):
            last_ok = time.time() - (901 if self.stale_health else 0)
            return {"source": self.name, "ok": True, "last_ok": last_ok, "errors": 0}

        def close(self):
            pass

    try:
        engine.source.close()
        engine.source = QuoteOnlySource()
        automatic, _ = engine._preopen_snapshot(1, allow_quote_only=False)
        manual, health = engine._preopen_snapshot(1, allow_quote_only=True)
        assert automatic is None, "自动赛前研究不得因历史K线失效悄悄降级"
        assert manual is not None and set(manual.symbols) == {"RB0", "I0"}
        assert all(not view.bars_5m and not view.ind_5m for view in manual.symbols.values())
        assert health["history_quality"] == "quote_only"
        manifest = build_market_manifest(manual, health)
        assert manifest["source_health"]["history_quality"] == "quote_only"
        assert manifest["global_analysis"]["confidence"] == "low"
        assert any("分钟与小时K线不可用" in item for item in manifest["global_analysis"]["invalidation_conditions"])
        engine.source.stale_health = True
        stale_manual, _ = engine._preopen_snapshot(1, allow_quote_only=True)
        assert stale_manual is None, "仅报价研究仍必须证明行情源在允许窗口内刷新"
    finally:
        engine.stop()
        db.close()


def test_manual_research_is_available_while_paused_without_opening_a_decision_window(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    captured = {}

    def fake_worker(*args):
        captured["args"] = args

    try:
        engine.running = True
        engine.pause(True)
        engine._run_preopen_debate = fake_worker  # type: ignore[method-assign]
        started, message = engine.start_manual_debate()
        assert started and "保持暂停" in message
        engine._debate_thread.join(timeout=2)
        assert captured["args"][-1] is True, "only a run started while paused may bypass the paused check"
        captured_epoch = captured["args"][2]
        assert engine._debate_is_current(engine.comp_id, engine._generation, captured_epoch, allow_paused=True)
        assert not engine._debate_is_current(engine.comp_id, engine._generation, captured_epoch)
        assert engine._preopen_debate_state()["manual_enabled"] is True
        engine.pause(False)
        assert not engine._debate_is_current(engine.comp_id, engine._generation, captured_epoch, allow_paused=True)
    finally:
        engine.stop()
        db.close()


def test_retry_latest_incomplete_research_is_single_flight_and_keeps_the_competition_paused(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    entered, release = Event(), Event()
    captured = {}
    try:
        engine.running = True
        engine.pause(True)
        retry_id = _reserve_terminal_debate(db, engine.comp_id, "manual:retryable", "incomplete")

        def fake_worker(*args):
            captured["args"] = args
            entered.set()
            release.wait(timeout=2)

        engine._run_preopen_debate = fake_worker  # type: ignore[method-assign]
        started, message = engine.retry_manual_debate(retry_id)
        assert started and "重新创建全量候选研究" in message and "保持暂停" in message
        assert entered.wait(timeout=1)
        args = captured["args"]
        assert args[3] == "manual_research" and args[4] == f"手动赛前研究（重试 #{retry_id}）"
        assert args[6] == "manual" and args[7] is True

        duplicate, duplicate_message = engine.retry_manual_debate(retry_id)
        assert not duplicate and "已有研究辩论" in duplicate_message
        current = db.debate_session(engine.comp_id, retry_id, include_raw=False)
        assert current and current["status"] == "incomplete", "old session must stay immutable during retry"
    finally:
        release.set()
        if engine._debate_thread is not None:
            engine._debate_thread.join(timeout=2)
        engine.stop()
        db.close()


def test_retry_rejects_history_and_nonrecoverable_latest_research(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        engine.running = True
        old_retryable = _reserve_terminal_debate(db, engine.comp_id, "manual:old-incomplete", "incomplete")
        latest_ready = _reserve_terminal_debate(db, engine.comp_id, "manual:latest-ready", "ready_for_codex_review")

        historical, historical_message = engine.retry_manual_debate(old_retryable)
        assert not historical and "当前最新" in historical_message
        current, current_message = engine.retry_manual_debate(latest_ready)
        assert not current and "不支持" in current_message
    finally:
        engine.stop()
        db.close()


def test_retry_api_scopes_the_session_and_returns_the_retry_origin(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    entered, release = Event(), Event()
    try:
        engine.running = True
        retry_id = _reserve_terminal_debate(db, engine.comp_id, "manual:api-retry", "unavailable")
        other_comp = engine.comp_id + 1
        db._exec(
            "INSERT INTO competitions(id, name, market, initial_cash, started_at, round_no, status, config_json) "
            "VALUES (?, 'other', 'replay', 1, 0, 0, 'running', '{}')",
            (other_comp,),
        )
        other_id = _reserve_terminal_debate(db, other_comp, "other:retry", "incomplete")

        def fake_worker(*args):
            entered.set()
            release.wait(timeout=2)

        engine._run_preopen_debate = fake_worker  # type: ignore[method-assign]
        client = TestClient(create_app(engine, ROOT / "web"), base_url="http://127.0.0.1:8765")
        response = client.post(f"/api/debates/{retry_id}/retry")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "message": "已重新创建全量候选研究", "analysis_only": True,
                                   "retry_of": retry_id}
        assert entered.wait(timeout=1)
        assert client.post(f"/api/debates/{retry_id}/retry").status_code == 409
        assert client.post(f"/api/debates/{other_id}/retry").status_code == 404
    finally:
        release.set()
        if engine._debate_thread is not None:
            engine._debate_thread.join(timeout=2)
        engine.stop()
        db.close()


@pytest.mark.parametrize("cancellation", ("pause", "stop"))
def test_debate_stage_rechecks_cancellation_before_submitting_model_requests(tmp_path, cancellation):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    calls = []
    real_set_status = db.set_debate_session_status
    try:
        manifest = {"analysis_id": "a", "snapshot_hash": "b", "market_as_of": 1, "global_analysis": {}}
        debate_id = db.reserve_debate_session(engine.comp_id, "manual:submit-race", "manual", "手动", None, manifest)
        assert debate_id is not None
        generation, epoch = engine._generation, engine._decision_epoch

        def pause_after_stage_status(*args, **kwargs):
            changed = real_set_status(*args, **kwargs)
            if changed:
                if cancellation == "pause":
                    engine.pause(True)
                else:
                    engine.stop()
            return changed

        def fake_call(*args):
            calls.append(args)
            return {"status": "failed", "error": "must not run", "latency_ms": 0}

        db.set_debate_session_status = pause_after_stage_status  # type: ignore[method-assign]
        engine._call_debate_model = fake_call  # type: ignore[method-assign]
        participants = [trader.id for trader in cfg.traders]
        rows, aborted = engine._run_debate_stage(
            engine.comp_id, generation, epoch, debate_id, "opening", participants,
            {trader_id: "fixture prompt" for trader_id in participants}, {"fact-1"},
        )
        assert aborted and rows == []
        assert calls == [], "cancellation between stage setup and submission must not create billable calls"
    finally:
        db.set_debate_session_status = real_set_status  # type: ignore[method-assign]
        engine.stop()
        db.close()


def test_debate_api_scopes_sessions_and_hides_raw_transcripts(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        _complete_debate(engine)
        current = db.latest_debate_session(engine.comp_id)
        client = TestClient(create_app(engine, ROOT / "web"))
        listing = client.get("/api/debates").json()["items"]
        assert listing and "snapshot" not in listing[0] and "referee_packet" not in listing[0]
        detail = client.get(f"/api/debates/{current['id']}").json()
        assert detail["referee_packet"].startswith("# 赛前多空决策室")
        assert all("raw_response" not in turn and "prompt" not in turn for turn in detail["turns"])

        manifest = {"analysis_id": "old", "snapshot_hash": "old", "market_as_of": 1, "global_analysis": {}}
        other_id = engine.comp_id + 100
        db._exec(
            "INSERT INTO competitions(id, name, market, initial_cash, started_at, round_no, status, config_json) "
            "VALUES (?, 'other', 'replay', 1, 0, 0, 'running', '{}')",
            (other_id,),
        )
        old_id = db.reserve_debate_session(other_id, "old:session", "manual", "old", None, manifest)
        assert old_id is not None
        assert client.get(f"/api/debates/{old_id}").status_code == 404
    finally:
        engine.stop()
        db.close()


def test_automatic_session_is_idempotent_and_restart_aborts_each_active_stage(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        manifest = {"analysis_id": "a", "snapshot_hash": "b", "market_as_of": 1, "global_analysis": {}}
        key = "auto:morning_preopen:123"
        session_id = db.reserve_debate_session(engine.comp_id, key, "morning_preopen", "早盘开盘前", 123, manifest)
        assert session_id is not None
        engine._preopen_candidate = lambda: ("morning_preopen", "早盘开盘前", 123.0)  # type: ignore[method-assign]
        assert engine._maybe_start_preopen_debate() is False
        db.set_debate_session_status(session_id, "opening")
        assert db.abort_running_debates(engine.comp_id, "test restart") == 1
        row = db.debate_session(engine.comp_id, session_id, include_raw=False)
        assert row["status"] == "aborted_restarted"
    finally:
        engine.stop()
        db.close()


def test_debate_terminal_cas_preserves_restart_reason_and_hides_error_detail(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        manifest = {"analysis_id": "a", "snapshot_hash": "b", "market_as_of": 1, "global_analysis": {}}
        session_id = db.reserve_debate_session(engine.comp_id, "manual:cas", "manual", "手动", None, manifest)
        assert session_id is not None
        assert db.set_debate_session_status(session_id, "opening", only_active=True)
        db.add_debate_turn(session_id, "opening", "deepseek", 1, "failed", error="https://provider.invalid/?token=never-expose")
        assert db.abort_running_debates(engine.comp_id, "Engine stopped") == 1
        assert not db.complete_debate_session(session_id, "failed", error="late worker result", only_active=True)

        client = TestClient(create_app(engine, ROOT / "web"))
        detail = client.get(f"/api/debates/{session_id}").json()
        assert detail["status"] == "aborted_restarted"
        assert "error" not in detail
        assert all("error" not in turn and "token=never-expose" not in str(turn) for turn in detail["turns"])
    finally:
        engine.stop()
        db.close()


def test_risk_term_change_creates_a_new_competition_boundary(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    first = Engine(cfg, db, accelerate=True)
    second = None
    try:
        first_id = first.comp_id
        manifest = {"analysis_id": "old", "snapshot_hash": "old", "market_as_of": 1, "global_analysis": {}}
        debate_id = db.reserve_debate_session(first_id, "manual:rollover", "manual", "手动", None, manifest)
        assert debate_id is not None
        assert db.set_debate_session_status(debate_id, "opening", only_active=True)
        changed = make_cfg(tmp_path)
        changed.risk.max_margin_risk_ratio = 0.94
        second = Engine(changed, db, accelerate=True)
        assert second.comp_id != first_id
        rows = {row["id"]: row for row in db.list_competitions()}
        assert rows[first_id]["status"] == "ended"
        assert rows[second.comp_id]["status"] == "running"
        assert db.debate_session(first_id, debate_id, include_raw=False)["status"] == "aborted_restarted"
    finally:
        first.stop()
        if second is not None:
            second.stop()
        db.close()


def test_target_risk_band_change_creates_a_new_competition_boundary(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.risk.target_margin_risk_min_ratio = 0.80
    cfg.risk.target_margin_risk_max_ratio = 0.90
    db = Database(cfg.db_path)
    first = Engine(cfg, db, accelerate=True)
    second = None
    try:
        first_id = first.comp_id
        changed = make_cfg(tmp_path)
        changed.risk.target_margin_risk_min_ratio = 0.80
        changed.risk.target_margin_risk_max_ratio = 0.89
        second = Engine(changed, db, accelerate=True)
        assert second.comp_id != first_id
        assert {row["id"]: row for row in db.list_competitions()}[first_id]["status"] == "ended"
    finally:
        first.stop()
        if second is not None:
            second.stop()
        db.close()


def test_state_exposes_and_classifies_the_target_risk_band(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.risk.target_margin_risk_min_ratio = 0.80
    cfg.risk.target_margin_risk_max_ratio = 0.90
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        account = engine.accounts["deepseek"]
        account.positions["I0"] = Position("deepseek", "I0", "long", 1, 800, 64_000, last_price=800)
        state = engine.state()
        trader = next(row for row in state["traders"] if row["id"] == "deepseek")
        assert trader["risk_state"] == "in_target"
        account.positions["I0"].margin = 72_000
        state = engine.state()
        trader = next(row for row in state["traders"] if row["id"] == "deepseek")
        assert trader["risk_state"] == "target_ceiling"
        account.positions["I0"].margin = 72_080
        state = engine.state()
        trader = next(row for row in state["traders"] if row["id"] == "deepseek")
        assert trader["risk_state"] == "above_target"
        risk = state["competition"]["risk"]
        assert risk["target_margin_risk_min_ratio"] == 0.80
        assert risk["target_margin_risk_max_ratio"] == 0.90
    finally:
        engine.stop()
        db.close()


def test_reset_marks_old_research_session_terminal_before_archiving(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        old_comp_id = engine.comp_id
        manifest = {"analysis_id": "old", "snapshot_hash": "old", "market_as_of": 1, "global_analysis": {}}
        debate_id = db.reserve_debate_session(old_comp_id, "manual:reset", "manual", "手动", None, manifest)
        assert debate_id is not None
        assert db.set_debate_session_status(debate_id, "rebuttal", only_active=True)
        engine.reset()
        assert engine.comp_id != old_comp_id
        assert db.debate_session(old_comp_id, debate_id, include_raw=False)["status"] == "aborted_restarted"
    finally:
        engine.stop()
        db.close()


def test_legacy_snapshot_without_admission_cap_rolls_into_capped_competition(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    first = Engine(cfg, db, accelerate=True)
    second = None
    try:
        first_id = first.comp_id
        snapshot = json.loads(db.active_competition()["config_json"])
        snapshot.pop("risk")
        db._exec("UPDATE competitions SET config_json=? WHERE id=?", (json.dumps(snapshot), first_id))

        second = Engine(make_cfg(tmp_path), db, accelerate=True)
        assert second.comp_id != first_id
        assert {row["id"]: row for row in db.list_competitions()}[first_id]["status"] == "ended"
    finally:
        first.stop()
        if second is not None:
            second.stop()
        db.close()


def test_non_admission_risk_tuning_keeps_the_current_competition(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    first = Engine(cfg, db, accelerate=True)
    second = None
    try:
        changed = make_cfg(tmp_path)
        changed.risk.warning_margin_risk_ratio = 0.91
        changed.risk.liquidation_risk_ratio = 0.99
        changed.risk.maintenance_margin_ratio = 0.40
        second = Engine(changed, db, accelerate=True)
        assert second.comp_id == first.comp_id
    finally:
        first.stop()
        if second is not None:
            second.stop()
        db.close()


def test_new_terms_reject_nonfinite_cash_and_invalid_warning_order():
    with pytest.raises(ValueError):
        AppConfig(competition={"initial_cash": float("inf")})
    with pytest.raises(ValueError):
        RiskCfg(max_margin_risk_ratio=0.95, warning_margin_risk_ratio=0.96)
