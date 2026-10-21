"""Static UI acceptance checks for full-universe pre-open research states."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "style.css").read_text(encoding="utf-8")


def test_full_scope_distinguishes_complete_incomplete_and_unavailable_states():
    assert "const universeStatus = universe?.scope_status || analysis.scope_status || 'unavailable';" in APP_JS
    assert "if (universeStatus === 'unavailable')" in APP_JS
    assert "if (!complete)" in APP_JS
    assert "全量候选研究不可用" in APP_JS
    assert "全量 eligible 覆盖未完成" in APP_JS
    assert "本轮未调用模型，不形成全量多空结论" in APP_JS
    assert "已扫描候选技术宽度（不构成全量结论）" in APP_JS
    assert ".metric-chip.failed" in CSS


def test_complete_scope_discloses_the_bounded_model_packet_and_execution_boundary():
    for phrase in (
        "全量 eligible 技术宽度",
        "全部已扫描候选都参与确定性宽度计算",
        "模型只收到固定大小的汇总与代表性事实",
        "不代表模型逐条阅读未列明合约",
        "仅执行池内",
    ):
        assert phrase in APP_JS


def test_full_scan_distinguishes_catalogue_proof_gaps_from_missing_eligible_coverage():
    for phrase in (
        "hasFullEligibleCoverage",
        "可研究范围覆盖完成（目录保留缺口）",
        "可研究范围已覆盖：",
        "目录公开报价证明缺口",
        "目录覆盖 ${ratio}",
        "可研究覆盖 ${eligibleRatio}",
    ):
        assert phrase in APP_JS


def test_full_scope_modal_reads_only_the_frozen_session_snapshot():
    assert "state.debate?.snapshot?.universe_research" in APP_JS
    assert "查看逐合约状态与排除原因" in APP_JS
    assert "openModal('全量候选逐项状态'" in APP_JS
    for field in ("scope_status", "scan_manifest_hash", "counts", "coverage", "technical_breadth", "items"):
        assert field in APP_JS


def test_latest_incomplete_research_exposes_a_fresh_full_universe_retry_only():
    assert 'id="btn-debate-retry"' in INDEX
    assert "const retryEligible = Boolean(detail) && Number(detail?.id) === Number(latest?.id)" in APP_JS
    assert "['incomplete', 'unavailable'].includes(detail?.status || '')" in APP_JS
    assert "trigger.hidden = retryEligible;" in APP_JS
    assert "retryTrigger.hidden = !retryEligible;" in APP_JS
    assert "重新获取全量行情并重跑" in APP_JS
    assert "覆盖不完整时不会调用模型，也不会退回 8 个合约" in APP_JS
    assert "preopen-debate/v2:" in APP_JS
    assert "`/api/debates/${encodeURIComponent(retryId)}/retry`" in APP_JS


def _missing_turn_state(detail, stage):
    match = re.search(r"function missingTurnState\(detail, stage\) \{.*?\n  \}", APP_JS, re.S)
    assert match, "web/app.js must project sessions with no persisted turn into an explicit terminal state"
    script = (
        match.group(0)
        + "\nconsole.log(JSON.stringify(missingTurnState("
        + json.dumps(detail, ensure_ascii=False)
        + ", "
        + json.dumps(stage)
        + ")));"
    )
    output = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(output.stdout)


def test_missing_turn_cards_distinguish_terminal_unavailable_from_running_and_not_requested():
    unavailable = _missing_turn_state(
        {"status": "unavailable", "preliminary": {"label": "全量候选研究未完整，未调用模型"}},
        "opening",
    )
    assert unavailable == {
        "badge": "未调用",
        "line": "全量候选研究未完整，未调用模型",
        "note": "本轮没有向模型服务发送请求。",
    }
    incomplete_rebuttal = _missing_turn_state({"status": "incomplete"}, "rebuttal")
    assert incomplete_rebuttal["badge"] == "未进入"
    assert "未进入定向反证" in incomplete_rebuttal["line"]
    running = _missing_turn_state({"status": "opening"}, "opening")
    assert running["badge"] == "进行中"
    assert "正在准备独立陈述" in running["line"]
    idle = _missing_turn_state(None, "opening")
    assert idle["badge"] == "尚未请求"


def test_dashboard_only_discloses_fixed_response_validation_categories():
    for category in ("malformed_json", "invalid_fact_citation", "invalid_target", "missing_required_field", "invalid_schema"):
        assert f"{category}:" in APP_JS
    assert "row.error" not in APP_JS


def test_full_scan_discloses_the_actual_market_as_of_window_and_prior_session_snapshot_mode():
    for phrase in ("行情有效时间", "上一已收盘会话快照（非盘中实时）", "quote_as_of_min", "history_as_of_min"):
        assert phrase in APP_JS
