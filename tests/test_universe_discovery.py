"""候选目录发现脚本：全部离线，零网络请求。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import discover_contract_universe as disc  # noqa: E402

from arena.universe import (  # noqa: E402
    CANDIDATE_FIELDS, FORBIDDEN_SPEC_KEYS, SNAPSHOT_COMPLETE, SNAPSHOT_PARTIAL,
    content_hash, forbidden_keys_in,
)

# 与上游 qihuohangqing.js 同形的离线样本：含两个内盘板块、一个现货板块、
# 一个重复节点，以及一条不符合三元组形状的畸形条目。
NODES_JS = """
var ARRFUTURESNODES = {
    czce : ['郑州商品交易所', ['PTA', 'pta_qh', '16'],['菜油', 'czy_qh', '18'], ['PTA', 'pta_qh', '16'], [ '坏条目' ]],
    shfe : ['上海期货交易所', ['螺纹钢', 'lwg_qh', '21'], ['黄金', 'hj_qh', '19', 'futures.fu_dig2']],
    bohai : ['渤海商品交易所', ['现货商品', 'bohai_qh', '1']]
};
function unrelated() { return { a: 1 }; }
"""

CONTRACTS_JSON = {
    "pta_qh": [{"symbol": "TA2701", "exchange": "czce", "name": "PTA2701"},
               {"symbol": "TA0", "exchange": "czce", "name": "PTA连续"}],
    "czy_qh": [{"symbol": "OI0", "exchange": "czce", "name": "菜油连续"}],
    "lwg_qh": [{"symbol": "RB0", "exchange": "shfe", "name": "螺纹钢连续"}],
    "hj_qh": [{"symbol": "AU0", "exchange": "shfe", "name": "黄金连续"}],
}

QUOTE_ROW = ",".join(["螺纹钢"] + [str(i) for i in range(1, 18)] + ["20260919"])


def make_fetch(*, failing: set[str] = frozenset(), history_failing: set[str] = frozenset()):
    """离线 fetcher：按 URL 返回样本，并可让指定品种的探测失败。"""
    calls: list[str] = []

    def fetch(url: str, params=None):
        calls.append(url)
        if url == disc.NODES_URL:
            return NODES_JS
        if url == disc.CONTRACTS_URL:
            return json.dumps(CONTRACTS_JSON.get(params["node"], []))
        if url.startswith("https://hq.sinajs.cn/"):
            symbol = url.rsplit("nf_", 1)[1]
            if symbol in failing:
                raise disc.ProbeError("ConnectTimeout")
            return f'var hq_str_nf_{symbol}="{QUOTE_ROW}";'
        if url == disc.HISTORY_URL:
            if params["symbol"] in history_failing:
                raise disc.ProbeError("ReadTimeout")
            return '/*<script>location.href="//sina.com";</script>*/(["2026-09-19 09:00:00",1,2,3,4,5,6])'
        raise AssertionError(f"unexpected url {url}")

    fetch.calls = calls
    return fetch


def run(**kwargs):
    fetch = make_fetch(**kwargs)
    doc = disc.discover(fetch, probe_history=True, workers=2, now=lambda: "2026-09-19T10:00:00+08:00")
    return doc, fetch


# ---------------------------------------------------------------- 解析 ----

def test_parse_nodes_dedupes_and_skips_malformed():
    nodes = disc.parse_nodes(NODES_JS)
    assert [(n.exchange, n.node, n.name) for n in nodes] == [
        ("CZCE", "pta_qh", "PTA"), ("CZCE", "czy_qh", "菜油"),
        ("SHFE", "lwg_qh", "螺纹钢"), ("SHFE", "hj_qh", "黄金"),
        ("BOHAI", "bohai_qh", "现货商品"),
    ]


def test_parse_nodes_rejects_missing_literal():
    with pytest.raises(ValueError):
        disc.parse_nodes("var SOMETHING_ELSE = {};")


def test_pick_continuous_takes_only_the_continuous_code():
    picked = disc.pick_continuous(CONTRACTS_JSON["pta_qh"], fallback_name="PTA", fallback_exchange="CZCE",
                                  source_group="CZCE", source_node="pta_qh")
    # 代码取自源 symbol 标识字段，不是从中文名或 node id 推出来的
    assert picked == disc.Candidate(symbol="TA0", name="PTA", exchange="CZCE",
                                    source_group="CZCE", source_node="pta_qh")
    assert disc.pick_continuous([{"name": "PTA连续", "exchange": "czce"}]) is None
    # 只有到期合约的节点不产出候选，绝不猜一个连续代码
    assert disc.pick_continuous([{"symbol": "TA2701"}]) is None
    assert disc.pick_continuous("not a list") is None


def test_quote_and_history_availability_parsers():
    assert disc.quote_available(f'var hq_str_nf_RB0="{QUOTE_ROW}";', "RB0") is True
    assert disc.quote_available('var hq_str_nf_RB0="";', "RB0") is False
    assert disc.quote_available('var hq_str_nf_RB0="a,b,c";', "RB0") is False   # 字段数不足
    assert disc.history_available('foo(["2026-09-19 09:00:00",1,2,3,4,5,6])') is True
    assert disc.history_available("foo([])") is False
    assert disc.history_available("null") is False


# ------------------------------------------------------------ 目录产出 ----

def test_discover_counts_are_dynamic_and_deduped():
    doc, fetch = run()
    # 数量由上游当次列表决定：样本里 5 个节点，只有 4 个内盘节点能解析出连续代码。
    assert doc["count"] == len(doc["candidates"]) == 4
    assert sorted(doc["candidates"]) == ["AU0", "OI0", "RB0", "TA0"]
    assert doc["fetched_at"] == "2026-09-19T10:00:00+08:00"
    # 现货板块不参与探测
    assert not any("bohai" in str(url) for url in fetch.calls)


def test_discover_follows_a_second_page_when_the_continuous_code_is_not_on_page_one():
    """A full first page without X0 must not silently truncate the product universe."""
    pages = []

    def fetch(url, params=None):
        if url == disc.NODES_URL:
            return 'var ARRFUTURESNODES = { SHFE: [["铜", "cu_qh"]] };'
        if url == disc.CONTRACTS_URL:
            pages.append(params["page"])
            if params["page"] == 1:
                # Exactly one full source page, all dated delivery contracts.
                return json.dumps([{"symbol": f"CU27{index:02d}", "name": "铜", "exchange": "SHFE"}
                                   for index in range(disc.CONTRACTS_PAGE_SIZE)])
            if params["page"] == 2:
                return json.dumps([{"symbol": "CU0", "name": "铜连续", "exchange": "SHFE"}])
            raise AssertionError("continuous mapping must stop once CU0 is found")
        if url.startswith("https://hq.sinajs.cn/"):
            return f'var hq_str_nf_CU0="{QUOTE_ROW}";'
        if url == disc.HISTORY_URL:
            return '(["2026-09-19 09:00:00",1,2,3,4,5,6])'
        raise AssertionError(f"unexpected url {url}")

    doc = disc.discover(fetch, probe_history=True, workers=1, now=lambda: "2026-09-24T10:00:00+08:00")
    assert pages == [1, 2]
    assert list(doc["candidates"]) == ["CU0"]
    assert doc["candidates"]["CU0"]["continuous_symbol"] == "CU0"


def test_discover_symbol_format_and_field_whitelist():
    doc, _ = run()
    for symbol, entry in doc["candidates"].items():
        assert symbol.isupper() and symbol.endswith("0") and symbol[:-1].isalpha()
        assert set(entry) == set(CANDIDATE_FIELDS)
        assert entry["probed_at"] == "2026-09-19T10:00:00+08:00"


def test_catalogue_never_carries_matching_parameters():
    doc, _ = run()
    assert forbidden_keys_in(doc) == set()
    polluted = json.loads(json.dumps(doc))
    polluted["candidates"]["RB0"]["multiplier"] = 10
    # 撮合参数先于哈希被点名：否则只会报"哈希对不上"，既低估了问题，
    # 又会诱导人把哈希重算到污染后的块上。
    with pytest.raises(ValueError, match="matching parameters"):
        disc.validate_catalogue(polluted)
    # 就算真把哈希重算成自洽的，照样拒绝
    polluted["content_hash"] = content_hash(polluted["candidates"])
    with pytest.raises(ValueError, match="matching parameters"):
        disc.validate_catalogue(polluted)
    # 白名单之外的任何键同样被拒绝
    polluted["candidates"]["RB0"].pop("multiplier")
    polluted["candidates"]["RB0"]["verified_by_exchange"] = True
    with pytest.raises(ValueError, match="unsupported fields"):
        disc.validate_catalogue(polluted)
    assert FORBIDDEN_SPEC_KEYS >= {"multiplier", "tick", "margin_ratio", "fee_rate", "sessions"}


def test_every_candidate_persists_its_upstream_continuous_symbol():
    doc, _ = run()
    for symbol, entry in doc["candidates"].items():
        # 代码取自上游行的 symbol 字段，并随记录一起落盘，不是只靠 map 键隐含
        assert entry["continuous_symbol"] == symbol
    # TA0 的来源行里中文名是 "PTA连续"、节点 id 是 "pta_qh"，都没有被当成代码
    assert doc["candidates"]["TA0"]["continuous_symbol"] == "TA0"

    mismatched = json.loads(json.dumps(doc))
    mismatched["candidates"]["RB0"]["continuous_symbol"] = "AU0"
    mismatched["content_hash"] = content_hash(mismatched["candidates"])
    with pytest.raises(ValueError, match="continuous_symbol"):
        disc.validate_catalogue(mismatched)


def test_failed_candidate_is_isolated_not_fatal():
    doc, _ = run(failing={"RB0"}, history_failing={"AU0"})
    assert doc["count"] == 4                      # 一个品种失败不拖垮整批
    failed = doc["candidates"]["RB0"]
    assert failed["probe_status"] == "failed" and failed["probe_error"] == "ConnectTimeout"
    assert failed["quote_ok"] is None and failed["history_5m_ok"] is None
    partial = doc["candidates"]["AU0"]
    assert partial["probe_status"] == "partial" and partial["quote_ok"] is True
    assert partial["history_5m_ok"] is None
    assert doc["candidates"]["TA0"]["probe_status"] == "ok"


def test_quote_only_run_marks_history_as_unprobed():
    fetch = make_fetch()
    doc = disc.discover(fetch, probe_history=False, workers=1, now=lambda: "2026-09-19T10:00:00+08:00")
    entry = doc["candidates"]["RB0"]
    assert entry["probe_status"] == "quote_only" and entry["quote_ok"] is True
    assert entry["history_5m_ok"] is entry["history_15m_ok"] is entry["history_1h_ok"] is None
    assert disc.HISTORY_URL not in fetch.calls


def test_workers_are_capped(monkeypatch):
    seen = {}
    real = disc.ThreadPoolExecutor

    def spy(max_workers=None, **kw):
        seen["max_workers"] = max_workers
        return real(max_workers=max_workers, **kw)

    monkeypatch.setattr(disc, "ThreadPoolExecutor", spy)
    disc.discover(make_fetch(), probe_history=False, workers=64, now=lambda: "t")
    assert seen["max_workers"] == disc.MAX_WORKERS == 8


def test_snapshot_carries_source_hash_and_status():
    doc, _ = run()
    assert doc["source"] and doc["source_url"] == disc.NODES_URL
    assert doc["snapshot_status"] == SNAPSHOT_COMPLETE
    assert doc["content_hash"] == content_hash(doc["candidates"])
    # 每条候选保留原始分组与节点 id，便于回溯
    assert doc["candidates"]["TA0"]["source_group"] == "CZCE"
    assert doc["candidates"]["TA0"]["source_node"] == "pta_qh"
    tampered = json.loads(json.dumps(doc))
    tampered["candidates"].pop("TA0")
    tampered["count"] = len(tampered["candidates"])
    with pytest.raises(ValueError, match="content_hash"):
        disc.validate_catalogue(tampered)


def test_probe_failure_makes_the_snapshot_partial():
    doc, _ = run(failing={"RB0"})
    assert doc["snapshot_status"] == SNAPSHOT_PARTIAL
    # 网络失败不会被写成 quote_ok=False
    assert doc["candidates"]["RB0"]["quote_ok"] is None


def test_partial_run_never_overwrites_a_complete_snapshot(tmp_path):
    out = tmp_path / "universe.json"
    complete, _ = run()
    assert disc.write_catalogue(out, complete) is True
    before = out.read_text(encoding="utf-8")

    partial, _ = run(failing={"RB0"})
    assert disc.write_catalogue(out, partial) is False
    assert out.read_text(encoding="utf-8") == before
    assert list(tmp_path.iterdir()) == [out]        # 没有留下临时文件

    # 但上一份本身就不完整时，允许用新的结果替换
    out.write_text(json.dumps({"snapshot_status": SNAPSHOT_PARTIAL, "candidates": {}}), encoding="utf-8")
    assert disc.write_catalogue(out, partial) is True
    assert json.loads(out.read_text(encoding="utf-8"))["snapshot_status"] == SNAPSHOT_PARTIAL


def test_write_catalogue_refuses_a_structurally_forbidden_document(tmp_path):
    """写入口自己复核文档：任何辅助函数都不该"顺手"把禁键目录落盘。

    `build_catalogue` 已经校验过，但 `write_catalogue` 收的是任意映射，
    调用方给什么就写什么的话，禁键就只剩下一道防线。
    """
    out = tmp_path / "universe.json"
    doc, _ = run()
    polluted = json.loads(json.dumps(doc))
    polluted["candidates"]["RB0"]["multiplier"] = 10
    polluted["content_hash"] = content_hash(polluted["candidates"])   # 自洽哈希也不行

    with pytest.raises(ValueError, match="matching parameters"):
        disc.write_catalogue(out, polluted)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []        # 连临时文件都没留下

    # candidates 不是映射时同样在落盘前被拦住
    broken = dict(doc, candidates=[], count=0, content_hash=content_hash([]))
    with pytest.raises(ValueError, match="candidates mapping"):
        disc.write_catalogue(out, broken)
    assert list(tmp_path.iterdir()) == []


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    out = tmp_path / "nested" / "universe.json"
    doc, _ = run()
    assert disc.write_catalogue(out, doc) is True
    assert [p.name for p in out.parent.iterdir()] == ["universe.json"]
    assert json.loads(out.read_text(encoding="utf-8"))["count"] == doc["count"]


# ------------------------------------------------------ 已提交的目录文件 ----

def test_committed_catalogue_is_valid_and_clean():
    doc = json.loads((ROOT / "config" / "contracts_universe_cn.json").read_text(encoding="utf-8"))
    disc.validate_catalogue(doc)
    assert forbidden_keys_in(doc) == set()
    assert doc["count"] == len(doc["candidates"]) > 0
    assert doc["fetched_at"] and doc["source"] and doc["source_url"]
    assert doc["snapshot_status"] == SNAPSHOT_COMPLETE
    # 目录绝不是为了凑某个固定数字而生成的
    for entry in doc["candidates"].values():
        assert set(entry) == set(CANDIDATE_FIELDS)
