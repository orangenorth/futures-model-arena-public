"""Static UI checks for the separate full-universe research panel."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "style.css").read_text(encoding="utf-8")


def test_full_scan_panel_declares_its_research_boundary_and_denominator_fields():
    for anchor in ("research-scan-panel", "research-scan-title", "research-scan-status", "btn-research-scan",
                   "research-scan-summary", "research-scan-tbl"):
        assert f'id="{anchor}"' in HTML
    assert "手动扫描不直接调用模型" in HTML
    assert "赛前决策室会另建一份全量研究快照" in HTML
    for label in ("发现", "目录证明通过", "本轮已分析", "排除", "排除原因"):
        assert label in HTML or label in APP_JS
    assert "失败项会保留排除原因" in HTML


def test_scan_reads_are_low_frequency_and_do_not_enter_the_competition_poll():
    assert APP_JS.count("'/api/research-scan'") == 1
    poll_body = APP_JS[APP_JS.index("async function poll()"):APP_JS.index("  document.addEventListener('input'")]
    assert "/api/research-scan" not in poll_body
    assert "loadResearchScan" not in poll_body
    assert "loadResearchScan();" in APP_JS
    assert "waitForResearchScan" in APP_JS


def test_scan_rows_escape_source_labels_and_the_model_pool_is_not_called_global_coverage():
    scan_render = APP_JS[APP_JS.index("function renderResearchScan()"):APP_JS.index("async function loadResearchScan()")]
    for value in ("item.symbol", "item.name", "item.exchange", "item.stage", "item.reason"):
        assert f"esc({value})" in scan_render
    assert "本面板手动扫描不下单、也不直接调用模型" in APP_JS
    assert "覆盖 ${fmt(analysis.coverage, 0)} 个合约" not in APP_JS
    assert "全局多空扫描" not in HTML
    assert ".research-scan-scroll" in CSS and "max-height" in CSS


def test_scan_timeout_is_not_mislabeled_as_a_catalogue_failure():
    assert "本轮扫描未完成（超时或取消）" in APP_JS
    assert "catalogueStatus === 'complete'" in APP_JS
