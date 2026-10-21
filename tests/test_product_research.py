"""Per-product full-universe debate coverage, all with fake market/model work."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arena.config import AppConfig, ProductResearchCfg, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.product_research import ProductResearchService, _opening_prompt, _rebuttal_prompt
from arena.research_scan import canonical_manifest_hash
from arena.server.api import create_app


ROOT = Path(__file__).resolve().parents[1]
PARTICIPANTS = [
    {"id": "deepseek", "name": "DeepSeek", "color": "#3b82f6"},
    {"id": "qwen", "name": "Qwen", "color": "#8b5cf6"},
    {"id": "kimi", "name": "Kimi", "color": "#10b981"},
    {"id": "glm", "name": "GLM", "color": "#f59e0b"},
]


def _symbols(count: int) -> list[str]:
    return [f"{chr(ord('A') + index)}0" for index in range(count)]


def _manifest(symbols: list[str], excluded: list[str] | None = None) -> dict:
    excluded = excluded or []
    items = []
    for index, symbol in enumerate(symbols):
        items.append({"symbol": symbol, "name": f"{symbol} name", "exchange": "SHFE", "outcome": "scanned",
                      "stage": "analysis", "reason": "", "latest_price": float(100 + index), "change_pct": 0.1,
                      "trend": "bullish" if index % 2 == 0 else "bearish", "score": 0.5 + index / 100})
    for symbol in excluded:
        items.append({"symbol": symbol, "name": f"{symbol} name", "exchange": "SHFE", "outcome": "excluded",
                      "stage": "quote", "reason": "quote_missing", "latest_price": None, "change_pct": None,
                      "trend": None, "score": None})
    discovered = len(items)
    scanned = len(symbols)
    manifest = {
        "schema_version": 1,
        "rules_version": "research-scan-v5",
        "status": "complete" if not excluded else "partial",
        "catalogue": {"schema_version": 1, "content_hash": "sha256:" + "a" * 64,
                      "snapshot_status": "complete", "fetched_at": "2026-09-24T10:00:00+08:00"},
        "counts": {"discovered": discovered, "eligible": discovered, "scanned": scanned, "excluded": len(excluded)},
        "coverage": {"status": "complete" if not excluded else "incomplete",
                     "ratio": round(scanned / discovered, 6) if discovered else None, "denominator_known": True},
        "started_at": 1_700_000_000.0,
        "completed_at": 1_700_000_001.0,
        "items": items,
    }
    manifest["manifest_hash"] = canonical_manifest_hash(manifest)
    return manifest


def _catalogue() -> dict:
    return {"snapshot_status": "complete", "content_hash": "sha256:" + "b" * 64,
            "fetched_at": "2026-09-24T10:00:00+08:00"}


def _fake_call(calls: list[tuple[str, str, str]], *, fail=None, block=None):
    def call(trader_id, prompt, stage, fact_ids, participant_ids):
        calls.append((trader_id, stage, prompt))
        if block is not None:
            block(trader_id, stage, prompt)
        failure = fail(trader_id, stage, prompt) if fail else None
        if failure:
            return {"status": "failed", "error": failure, "prompt": prompt, "latency_ms": 1}
        fact_id = sorted(fact_ids)[0]
        if stage == "opening":
            payload = {"stance": "bullish", "confidence": 0.6, "thesis": f"{trader_id} opening",
                       "supporting_fact_ids": [fact_id], "counter_fact_ids": [fact_id],
                       "counter_argument": "counter", "invalidation": "fact changes", "uncertainties": ["uncertain"]}
        else:
            target = next(candidate for candidate in sorted(participant_ids) if candidate != trader_id)
            payload = {"stance": "neutral", "confidence": 0.5, "target_trader_id": target,
                       "challenge": "challenge", "challenge_fact_ids": [fact_id],
                       "revised_thesis": "revised", "invalidation": "fact changes"}
        return {"status": "complete", "payload": payload, "raw_response": "{fixture}", "prompt": prompt,
                "latency_ms": 1, "tokens_in": 11, "tokens_out": 22}
    return call


def _service(tmp_path, manifest, calls, *, cfg=None, fail=None, block=None):
    db = Database(tmp_path / "product-research.db")
    comp_id = db.create_competition("product research", "cn_futures", 80_000, "{}")
    service = ProductResearchService(
        db, comp_id=lambda: comp_id, cfg=cfg or ProductResearchCfg(candidate_batch_size=8, model_concurrency=4, max_attempts=1),
        catalogue_dir=tmp_path / "catalogues", calendar_path=tmp_path / "calendar.json", participants=lambda: PARTICIPANTS,
        call_model=_fake_call(calls, fail=fail, block=block), can_start=lambda: True,
        discovery_factory=lambda _path: _catalogue(), scan_factory=lambda _path, _cancel: manifest,
    )
    return db, comp_id, service


def _join(service):
    assert service._thread is not None
    service._thread.join(timeout=10)
    assert not service._thread.is_alive(), "fixture product-research worker did not finish"


def test_product_prompt_contract_lists_only_current_facts_and_marks_structural_retry():
    facts = [{"id": "AA0:quote"}, {"id": "AA0:trend"}, {"id": "AA0:score"}]

    first = _opening_prompt("AA0", "AA0 name", "SHFE", facts)
    retry = _opening_prompt("AA0", "AA0 name", "SHFE", facts, attempt=2)

    contract = '允许的 fact id 仅为：["AA0:quote","AA0:trend","AA0:score"]'
    assert contract in first and contract in retry
    assert "ZZ0:quote" not in first and "ZZ0:quote" not in retry
    assert "第 2 次受限重试" not in first
    assert "第 2 次受限重试" in retry


def test_product_rebuttal_contract_excludes_self_and_requires_another_participant():
    facts = [{"id": "AA0:quote"}, {"id": "AA0:trend"}, {"id": "AA0:score"}]
    openings = [{"trader_id": trader_id} for trader_id in ("deepseek", "qwen", "kimi", "glm")]

    prompt = _rebuttal_prompt("AA0", "AA0 name", "SHFE", facts, openings, "qwen", attempt=2)
    target_prefix = "target_trader_id 只能是以下其他模型编号之一："
    target_contract = prompt.split(target_prefix, 1)[1].split("。", 1)[0]
    assert target_contract == '["deepseek","kimi","glm"]'
    assert "第 2 次受限重试" in prompt
    with pytest.raises(ValueError, match="another participant"):
        _rebuttal_prompt("AA0", "AA0 name", "SHFE", facts, [{"trader_id": "qwen"}], "qwen")


def test_more_than_eight_products_each_get_an_independent_two_stage_four_model_debate(tmp_path):
    symbols = _symbols(10)
    calls: list[tuple[str, str, str]] = []
    db, comp_id, service = _service(tmp_path, _manifest(symbols, excluded=["K0", "L0"]), calls)
    try:
        started, _, run_id = service.start()
        assert started and run_id
        _join(service)
        run = db.product_research_run(comp_id, run_id)
        assert run and run["status"] == "complete"
        assert {key: run[key] for key in ("discovered_count", "eligible_count", "excluded_count", "pending_count", "running_count", "complete_count", "partial_count", "failed_count")} == {
            "discovered_count": 12, "eligible_count": 10, "excluded_count": 2, "pending_count": 0,
            "running_count": 0, "complete_count": 10, "partial_count": 0, "failed_count": 0,
        }
        assert len(calls) == 10 * 4 * 2
        items = db.product_research_items(run_id, limit=100)
        assert {item["symbol"] for item in items} == set(symbols) | {"K0", "L0"}
        assert {item["symbol"] for item in items if item["status"] == "complete"} == set(symbols)
        assert {item["symbol"] for item in items if item["status"] == "excluded"} == {"K0", "L0"}
        ninth = next(item for item in items if item["symbol"] == symbols[8])
        tenth = next(item for item in items if item["symbol"] == symbols[9])
        assert ninth["batch_no"] == tenth["batch_no"] == 2
        for item in (ninth, tenth):
            detail = db.debate_session(comp_id, item["debate_id"], include_raw=False)
            assert detail and detail["status"] == "ready_for_codex_review"
            assert len(detail["turns"]) == 8 and {turn["status"] for turn in detail["turns"]} == {"complete"}
            own = item["symbol"]
            prompts = [turn["prompt"] for turn in db.debate_session(comp_id, item["debate_id"], include_raw=True)["turns"]]
            assert all(own in prompt for prompt in prompts)
            assert all(symbol not in "\n".join(prompts) for symbol in symbols if symbol != own)
    finally:
        db.close()


def test_explicit_small_model_sample_keeps_the_full_queue_and_reaches_the_tenth_product(tmp_path):
    symbols = _symbols(12)
    calls: list[tuple[str, str, str]] = []
    db, comp_id, service = _service(tmp_path, _manifest(symbols), calls)
    try:
        started, _, run_id = service.start(sample_limit=10)
        assert started and run_id
        _join(service)
        run = db.product_research_run(comp_id, run_id)
        assert run and run["status"] == "interrupted" and run["error"] == "sample_limit_reached"
        assert (run["eligible_count"], run["complete_count"], run["pending_count"]) == (12, 10, 2)
        rows = {item["symbol"]: item for item in db.product_research_items(run_id, limit=100)}
        assert rows["I0"]["status"] == rows["J0"]["status"] == "complete"
        assert rows["K0"]["status"] == rows["L0"]["status"] == "pending"
        assert len(calls) == 10 * 8
        resumed, _ = service.resume(run_id)
        assert resumed
        _join(service)
        final = db.product_research_run(comp_id, run_id)
        assert final and final["status"] == "complete" and final["pending_count"] == 0
        rows = {item["symbol"]: item for item in db.product_research_items(run_id, limit=100)}
        assert rows["K0"]["status"] == rows["L0"]["status"] == "complete"
        assert len(calls) == len(symbols) * 8
    finally:
        db.close()


def test_resume_uses_the_run_competition_after_a_new_competition_is_created(tmp_path):
    """A routing-only rollover must not orphan the durable analysis queue."""
    calls: list[tuple[str, str, str]] = []
    db, old_competition_id, first_service = _service(tmp_path, _manifest(["A0", "B0"]), calls)
    try:
        started, _, run_id = first_service.start(sample_limit=1)
        assert started and run_id
        _join(first_service)
        before_resume = list(calls)
        old_run = db.product_research_run(old_competition_id, run_id)
        assert old_run and old_run["pending_count"] == 1 and old_run["complete_count"] == 1

        new_competition_id = db.create_competition("new trading scope", "cn_futures", 80_000, "{}")
        assert new_competition_id != old_competition_id
        resumed_service = ProductResearchService(
            db, comp_id=lambda: new_competition_id,
            cfg=ProductResearchCfg(candidate_batch_size=8, model_concurrency=4, max_attempts=1),
            catalogue_dir=tmp_path / "catalogues", calendar_path=tmp_path / "calendar.json",
            participants=lambda: PARTICIPANTS, call_model=_fake_call(calls), can_start=lambda: True,
            discovery_factory=lambda _path: _catalogue(), scan_factory=lambda _path, _cancel: _manifest(["A0", "B0"]),
        )
        resumed, _ = resumed_service.resume(run_id)
        assert resumed
        _join(resumed_service)

        final = db.product_research_run(old_competition_id, run_id)
        assert final and final["status"] == "complete" and final["pending_count"] == 0
        assert len(calls) == len(before_resume) + 8
        assert not any("A0" in prompt for _, _, prompt in calls[len(before_resume):])
        for item in db.product_research_items(run_id, limit=20):
            assert db.debate_session(old_competition_id, item["debate_id"], include_raw=False)
            assert db.debate_session(new_competition_id, item["debate_id"], include_raw=False) is None
    finally:
        db.close()


def test_one_model_timeout_is_quarantined_to_one_product_and_later_products_continue(tmp_path):
    symbols = _symbols(10)
    calls: list[tuple[str, str, str]] = []

    def fail(trader_id, stage, prompt):
        return "ResearchRequestTimedOut" if "B0" in prompt and trader_id == "qwen" and stage == "opening" else None

    db, comp_id, service = _service(tmp_path, _manifest(symbols), calls, fail=fail)
    try:
        assert service.start()[0]
        _join(service)
        run = db.latest_product_research_run(comp_id)
        assert run and run["status"] == "complete_with_exceptions"
        rows = {item["symbol"]: item for item in db.product_research_items(run["id"], limit=100)}
        assert rows["B0"]["status"] == "partial"
        assert rows["J0"]["status"] == "complete", "the tenth product must continue after B0 fails"
        qwen_opening = rows["B0"]["models"]["opening"]["qwen"][-1]
        assert qwen_opening["status"] == "delivery_unknown"
        assert all(call["status"] == "skipped" for calls_by_model in rows["B0"]["models"]["rebuttal"].values() for call in calls_by_model)
        assert len(calls) == 9 * 8 + 4
    finally:
        db.close()


def test_definitive_response_failure_retries_only_its_model_slot(tmp_path):
    symbols = ["A0", "B0"]
    calls: list[tuple[str, str, str]] = []

    def fail(trader_id, stage, prompt):
        matching = [call for call in calls if "A0" in call[2] and call[0] == "qwen" and call[1] == "opening"]
        if "A0" in prompt and trader_id == "qwen" and stage == "opening" and len(matching) == 1:
            return "ResearchResponseMalformedJson"
        return None

    cfg = ProductResearchCfg(candidate_batch_size=8, model_concurrency=4, max_attempts=2, retry_backoff_sec=0)
    db, comp_id, service = _service(tmp_path, _manifest(symbols), calls, cfg=cfg, fail=fail)
    try:
        assert service.start()[0]
        _join(service)
        run = db.latest_product_research_run(comp_id)
        assert run and run["status"] == "complete"
        rows = {item["symbol"]: item for item in db.product_research_items(run["id"], limit=100)}
        retries = rows["A0"]["models"]["opening"]["qwen"]
        assert [(call["attempt"], call["status"]) for call in retries] == [(1, "failed"), (2, "complete")]
        assert all(len(entries) == 1 for trader, entries in rows["A0"]["models"]["opening"].items() if trader != "qwen")
        assert rows["B0"]["status"] == "complete"
        assert len(calls) == len(symbols) * 8 + 1
    finally:
        db.close()


def test_one_product_all_model_failure_does_not_stop_later_batches(tmp_path):
    symbols = _symbols(10)
    calls: list[tuple[str, str, str]] = []

    def fail(_trader_id, stage, prompt):
        return "ResearchResponseInvalid" if "A0" in prompt and stage == "opening" else None

    db, comp_id, service = _service(tmp_path, _manifest(symbols), calls, fail=fail)
    try:
        assert service.start()[0]
        _join(service)
        run = db.latest_product_research_run(comp_id)
        assert run and run["status"] == "complete_with_exceptions"
        rows = {item["symbol"]: item for item in db.product_research_items(run["id"], limit=100)}
        assert rows["A0"]["status"] == "failed"
        assert rows["J0"]["status"] == "complete", "the tenth product must run after A0 fails entirely"
        assert all(call["status"] == "failed" for calls_by_model in rows["A0"]["models"]["opening"].values()
                   for call in calls_by_model)
        assert all(call["status"] == "skipped" for calls_by_model in rows["A0"]["models"]["rebuttal"].values()
                   for call in calls_by_model)
        assert len(calls) == 4 + (len(symbols) - 1) * 8
    finally:
        db.close()


def test_manual_failed_item_rerun_reuses_completed_calls_and_releases_never_sent_rebuttals(tmp_path):
    calls: list[tuple[str, str, str]] = []
    first_pass = {"fail": True}

    def fail(trader_id, stage, prompt):
        if first_pass["fail"] and "A0" in prompt and trader_id == "qwen" and stage == "opening":
            return "ResearchResponseInvalid"
        return None

    cfg = ProductResearchCfg(candidate_batch_size=8, model_concurrency=4, max_attempts=1,
                             manual_retry_limit=2, retry_backoff_sec=0)
    db, comp_id, service = _service(tmp_path, _manifest(["A0", "B0"]), calls, cfg=cfg, fail=fail)
    try:
        started, _, run_id = service.start(sample_limit=1)
        assert started and run_id
        _join(service)
        first = db.product_research_run(comp_id, run_id)
        assert first and first["status"] == "interrupted" and first["error"] == "sample_limit_reached"
        rows = {item["symbol"]: item for item in db.product_research_items(run_id, limit=100)}
        assert rows["A0"]["status"] == "partial" and rows["B0"]["status"] == "pending"
        before_retry = len(calls)
        first_pass["fail"] = False
        retried, _ = service.retry_failed(run_id)
        assert retried
        _join(service)
        retry_calls = calls[before_retry:]
        assert len(retry_calls) == 5
        assert all("A0" in prompt for _, _, prompt in retry_calls)
        assert sum(1 for trader, stage, _ in retry_calls if trader == "qwen" and stage == "opening") == 1
        run = db.product_research_run(comp_id, run_id)
        assert run and run["status"] == "interrupted" and run["error"] == "pending_not_requested"
        assert run["max_attempts"] == 2
        rows = {item["symbol"]: item for item in db.product_research_items(run_id, limit=100)}
        assert rows["A0"]["status"] == "complete" and rows["B0"]["status"] == "pending"
        assert [(call["attempt"], call["status"]) for call in rows["A0"]["models"]["opening"]["qwen"]] == [
            (1, "failed"), (2, "complete"),
        ]
        recovered_call_count = len(calls)
        recovered_max_attempts = int(run["max_attempts"])
        recovered_status = str(run["status"])
        recovered_error = str(run["error"])
        assert not service.retry_failed(run_id)[0]
        final_run = db.product_research_run(comp_id, run_id)
        assert final_run and int(final_run["max_attempts"]) == recovered_max_attempts
        assert str(final_run["status"]) == recovered_status
        assert str(final_run["error"]) == recovered_error
        assert len(calls) == recovered_call_count
    finally:
        db.close()


def test_interrupted_submitted_call_is_not_reissued_on_resume_and_completed_calls_are_reused(tmp_path):
    symbols = ["A0", "B0"]
    calls: list[tuple[str, str, str]] = []
    entered = threading.Event()
    release = threading.Event()

    def block(trader_id, stage, prompt):
        if "A0" in prompt and trader_id == "qwen" and stage == "opening":
            entered.set()
            assert release.wait(timeout=5)

    db, comp_id, service = _service(tmp_path, _manifest(symbols), calls, block=block)
    try:
        started, _, run_id = service.start()
        assert started and run_id
        assert entered.wait(timeout=5)
        service.stop()
        _join(service)
        before_resume = len(calls)
        # Let the late worker return only after its dispatch has been marked
        # delivery_unknown.  It must not overwrite the quarantine.
        release.set()
        resumed, _ = service.resume(run_id)
        assert resumed
        _join(service)
        after_resume = calls[before_resume:]
        assert not any("A0" in prompt for _, _, prompt in after_resume), "A0 must not be automatically reissued"
        run = db.product_research_run(comp_id, run_id)
        assert run and run["status"] == "complete_with_exceptions"
        rows = {item["symbol"]: item for item in db.product_research_items(run_id, limit=100)}
        assert rows["A0"]["status"] == "partial"
        assert rows["B0"]["status"] == "complete"
        a_calls = [call for stages in rows["A0"]["models"].values() for attempts in stages.values() for call in attempts]
        assert any(call["status"] == "delivery_unknown" for call in a_calls)
    finally:
        release.set()
        db.close()


def test_product_research_api_pages_the_ninth_and_tenth_products_and_matches_storage(tmp_path):
    traders = [
        TraderCfg(id=item["id"], name=item["name"], provider="openai_compatible", base_url="https://example.invalid/v1",
                  model=f"{item['id']}-fixture", api_key="fixture")
        for item in PARTICIPANTS
    ]
    cfg = AppConfig(
        competition={"name": "api fixture", "market": "cn_futures", "initial_cash": 80_000,
                     "decision_interval_sec": 60, "tick_interval_sec": 60, "snapshot_interval_sec": 60,
                     "timezone": "Asia/Shanghai"},
        symbols=["RB0"], market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json", "timeout_sec": 1}},
        traders=traders, storage={"db_path": str(tmp_path / "api.db")}, base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    calls: list[tuple[str, str, str]] = []
    engine.running = True
    engine.product_research._discovery_factory = lambda _path: _catalogue()
    engine.product_research._scan_factory = lambda _path, _cancel: _manifest(_symbols(10), excluded=["K0"])
    engine.product_research._call_model = _fake_call(calls)
    app = create_app(engine, ROOT / "web")
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.post("/api/product-research/run")
            assert response.status_code == 200
            run_id = response.json()["run_id"]
            _join(engine.product_research)
            first = client.get("/api/product-research?limit=8&offset=0").json()
            second = client.get("/api/product-research?limit=8&offset=8").json()
            assert first["run"]["id"] == second["run"]["id"] == run_id
            assert first["total"] == second["total"] == 11
            assert {item["symbol"] for item in first["items"]}.isdisjoint({item["symbol"] for item in second["items"]})
            assert {"I0", "J0"} <= {item["symbol"] for item in second["items"]}
            stored = db.product_research_items(run_id, limit=100)
            assert {item["symbol"] for item in first["items"] + second["items"]} == {item["symbol"] for item in stored}
            assert len(calls) == 10 * 8
            detail = client.get(f"/api/debates/{next(item['debate_id'] for item in stored if item['symbol'] == 'J0')}")
            assert detail.status_code == 200 and len(detail.json()["turns"]) == 8
    finally:
        engine.stop()
        db.close()


def test_historical_product_research_is_resumable_and_result_scoped_after_route_change(tmp_path):
    def config_for(qwen_base_url: str):
        traders = [
            TraderCfg(id=item["id"], name=item["name"], provider="openai_compatible",
                      base_url=qwen_base_url if item["id"] == "qwen" else "https://example.invalid/v1",
                      model=f"{item['id']}-fixture", api_key="fixture")
            for item in PARTICIPANTS
        ]
        return AppConfig(
            competition={"name": "route rollover fixture", "market": "cn_futures", "initial_cash": 80_000,
                         "decision_interval_sec": 60, "tick_interval_sec": 60, "snapshot_interval_sec": 60,
                         "timezone": "Asia/Shanghai"},
            symbols=["RB0"], market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json", "timeout_sec": 1}},
            traders=traders, storage={"db_path": str(tmp_path / "route-rollover.db")}, base_dir=ROOT,
        )

    db = Database(config_for("https://route-one.invalid/v1").db_path)
    old_engine = Engine(config_for("https://route-one.invalid/v1"), db, accelerate=True)
    old_engine.running = True
    old_engine.product_research._discovery_factory = lambda _path: _catalogue()
    old_engine.product_research._scan_factory = lambda _path, _cancel: _manifest(["A0", "B0"])
    calls: list[tuple[str, str, str]] = []
    old_engine.product_research._call_model = _fake_call(calls)
    old_competition_id = old_engine.comp_id
    try:
        old_app = create_app(old_engine, ROOT / "web")
        with TestClient(old_app, base_url="http://127.0.0.1") as client:
            started = client.post("/api/product-research/run?sample_limit=1")
            assert started.status_code == 200
            run_id = started.json()["run_id"]
            _join(old_engine.product_research)
        old_item = next(item for item in db.product_research_items(run_id, limit=10) if item["symbol"] == "A0")
        old_engine.stop()

        new_engine = Engine(config_for("https://route-two.invalid/v1"), db, accelerate=True)
        new_engine.running = True
        new_engine.product_research._call_model = _fake_call(calls)
        try:
            assert new_engine.comp_id != old_competition_id, "normal trading competition rollover remains intact"
            new_app = create_app(new_engine, ROOT / "web")
            with TestClient(new_app, base_url="http://127.0.0.1") as client:
                assert client.get("/api/product-research").json()["run"] is None
                explicit = client.get(f"/api/product-research?run_id={run_id}")
                assert explicit.status_code == 200 and explicit.json()["run"]["id"] == run_id
                resumable = client.get("/api/product-research/resumable")
                assert resumable.status_code == 200 and resumable.json()["historical_recovery"] is True
                assert client.get(f"/api/debates/{old_item['debate_id']}").status_code == 404
                assert client.get(f"/api/product-research/{run_id}/debates/{old_item['debate_id']}").status_code == 200
                assert client.get(f"/api/product-research/{run_id}/debates/999999").status_code == 404
                assert client.post(f"/api/product-research/{run_id}/resume").status_code == 200
            _join(new_engine.product_research)
            final = db.product_research_run(old_competition_id, run_id)
            assert final and final["status"] == "complete" and final["pending_count"] == 0
            app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
            assert "/api/product-research/${encodeURIComponent(runId)}/debates/" in app_js
            assert "product_research_run" in app_js and "rememberProductResearchRun" in app_js
        finally:
            new_engine.stop()
    finally:
        db.close()


def test_incompatible_historical_run_does_not_block_a_new_current_research_run(tmp_path):
    def config_for(qwen_model: str):
        traders = [
            TraderCfg(id=item["id"], name=item["name"], provider="openai_compatible",
                      base_url="https://example.invalid/v1", model=qwen_model if item["id"] == "qwen" else f"{item['id']}-fixture",
                      api_key="fixture")
            for item in PARTICIPANTS
        ]
        return AppConfig(
            competition={"name": "incompatible route fixture", "market": "cn_futures", "initial_cash": 80_000,
                         "decision_interval_sec": 60, "tick_interval_sec": 60, "snapshot_interval_sec": 60,
                         "timezone": "Asia/Shanghai"},
            symbols=["RB0"], market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json", "timeout_sec": 1}},
            traders=traders, storage={"db_path": str(tmp_path / "incompatible-route.db")}, base_dir=ROOT,
        )

    db = Database(config_for("qwen-fixture").db_path)
    old_engine = Engine(config_for("qwen-fixture"), db, accelerate=True)
    old_engine.running = True
    old_engine.product_research._discovery_factory = lambda _path: _catalogue()
    old_engine.product_research._scan_factory = lambda _path, _cancel: _manifest(["A0", "B0"])
    old_engine.product_research._call_model = _fake_call([])
    old_competition_id = old_engine.comp_id
    try:
        started, _, old_run_id = old_engine.start_product_research(sample_limit=1)
        assert started and old_run_id
        _join(old_engine.product_research)
        assert db.product_research_run(old_competition_id, old_run_id)["pending_count"] == 1
        old_engine.stop()

        new_engine = Engine(config_for("qwen-replacement-fixture"), db, accelerate=True)
        new_engine.running = True
        new_engine.product_research._discovery_factory = lambda _path: _catalogue()
        new_engine.product_research._scan_factory = lambda _path, _cancel: _manifest(["A0"])
        new_engine.product_research._call_model = _fake_call([])
        try:
            assert new_engine.comp_id != old_competition_id
            new_app = create_app(new_engine, ROOT / "web")
            with TestClient(new_app, base_url="http://127.0.0.1") as client:
                historical = client.get(f"/api/product-research?run_id={old_run_id}").json()
                assert historical["historical_recovery"] is True and historical["resume_allowed"] is False
                assert client.post(f"/api/product-research/{old_run_id}/resume").status_code == 409
                started = client.post("/api/product-research/run?sample_limit=1")
                assert started.status_code == 200
                new_run_id = started.json()["run_id"]
            assert new_run_id and new_run_id != old_run_id
            _join(new_engine.product_research)
            assert db.product_research_run(old_competition_id, old_run_id)["pending_count"] == 1
            assert db.product_research_run(new_engine.comp_id, new_run_id)
            app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
            assert "payload.resume_allowed === false" in app_js and "historicalRecovery" in app_js
        finally:
            new_engine.stop()
    finally:
        db.close()


def test_engine_refuses_a_paid_product_run_when_one_of_four_models_is_not_live(tmp_path):
    traders = [
        TraderCfg(id=item["id"], name=item["name"], provider="openai_compatible",
                  base_url="https://example.invalid/v1", model=f"{item['id']}-fixture",
                  api_key="fixture" if item["id"] != "glm" else "")
        for item in PARTICIPANTS
    ]
    cfg = AppConfig(
        competition={"name": "readiness fixture", "market": "cn_futures", "initial_cash": 80_000,
                     "decision_interval_sec": 60, "tick_interval_sec": 60, "snapshot_interval_sec": 60,
                     "timezone": "Asia/Shanghai"},
        symbols=["RB0"], market_data={"cn_futures": {"contracts_file": "config/contracts_cn.json", "timeout_sec": 1}},
        traders=traders, storage={"db_path": str(tmp_path / "readiness.db")}, base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    engine.running = True
    app = create_app(engine, ROOT / "web")
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            state = client.get("/api/state").json()
            assert state["product_research"]["readiness"] == {
                "ready": False, "configured_count": 4, "unavailable": ["glm"],
            }
            response = client.post("/api/product-research/run?sample_limit=10")
            assert response.status_code == 409
            assert "glm" in response.json()["detail"]
            assert "productFailureSummary" in client.get("/static/app.js").text
        assert db.product_research_runs(engine.comp_id) == []
    finally:
        engine.stop()
        db.close()
