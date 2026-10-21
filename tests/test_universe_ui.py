"""看板「品种宇宙」只读区的静态结构断言。

边界：本仓库无浏览器/无头运行环境，因此这里断言的是 HTML/JS 的可验证结构
（筛选框、滚动容器、接口调用位置），而不是渲染后的 DOM。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "style.css").read_text(encoding="utf-8")


def test_panel_has_source_updated_count_filter_and_table():
    assert 'id="universe-panel"' in HTML
    for anchor in ("universe-source", "universe-fetched", "universe-count", "universe-filter", "universe-tbl"):
        assert f'id="{anchor}"' in HTML
    assert '<input id="universe-filter" type="search"' in HTML
    for header in ("代码", "中文名", "交易所", "本地规格", "本期启用"):
        assert f"<th>{header}</th>" in HTML


def test_long_list_scrolls_inside_its_own_container():
    assert '<div class="scroll universe-scroll">' in HTML
    assert re.search(r"\.universe-scroll\s*\{[^}]*max-height", CSS)


def test_disclaimer_states_the_catalogue_is_not_a_trading_pool():
    note = HTML[HTML.index('id="universe-note"'):HTML.index("universe-toolbar")]
    assert "不是</b>默认交易池" in note
    assert "不代表交易所核验或可下单授权" in note
    for excluded in ("合约乘数", "保证金", "手续费", "交易时段"):
        assert excluded in note


def test_panel_never_claims_a_candidate_is_authorised_to_trade():
    """Phase A 只有 configured_spec / active_in_competition / verification_status 三种说法。

    否定式免责声明可以出现，肯定式授权措辞不行：看板任何一处写出「可交易 / 已核验 /
    tradable」，都会把一份只读发现结果读成下单许可。
    """
    for text in (HTML, APP_JS):
        for claim in ("可交易", "已核验", "tradable"):
            assert claim not in text
    assert "未核验" in APP_JS and "未对任何候选做交易所条款核验" in HTML


def test_universe_is_loaded_once_and_stays_out_of_the_state_poll():
    assert APP_JS.count("'/api/universe'") == 1
    poll_body = APP_JS[APP_JS.index("async function poll()"):APP_JS.index("  initChart();")]
    assert "/api/universe" not in poll_body
    assert "loadUniverse" not in poll_body
    # 启动时取一次；没有为候选目录挂任何定时刷新
    assert "loadUniverse();" in APP_JS
    for interval in re.findall(r"setInterval\(([^,]+),", APP_JS):
        assert "universe" not in interval.lower()


def test_filter_rerenders_from_cache_without_refetching():
    handler = APP_JS[APP_JS.index("ev.target.id !== 'universe-filter'"):]
    handler = handler[:handler.index("initChart();")]
    assert "renderUniverse()" in handler
    assert "api(" not in handler


def test_candidates_never_enter_the_quote_rendering_path():
    render_quotes = APP_JS[APP_JS.index("function renderQuotes"):APP_JS.index("// ---------- 品种宇宙")]
    assert "universe" not in render_quotes
    assert "d.quotes.map" in render_quotes
    render_universe = APP_JS[APP_JS.index("function renderUniverse"):APP_JS.index("async function loadUniverse")]
    assert "state.data" not in render_universe   # 只读目录自成数据源，不掺进竞赛状态
