"""/api/universe：只读候选目录，两个本地派生状态，且不泄露配置或撮合字段。"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine
from arena.server.api import create_app
from arena.universe import DEFAULT_UNIVERSE_FILE, content_hash, forbidden_keys_in

ROOT = Path(__file__).resolve().parents[1]

# 接口对外暴露的候选字段白名单：身份 + 上游出处 + 可用性 + 三个本地派生字段。
CANDIDATE_KEYS = {"symbol", "name", "exchange", "source_group", "source_node",
                  "quote_ok", "history_5m_ok", "history_15m_ok", "history_1h_ok",
                  "probe_status", "probe_error", "probed_at",
                  "configured_spec", "active_in_competition", "verification_status"}

CANDIDATES = {
    "RB0": {"continuous_symbol": "RB0", "name": "螺纹钢", "exchange": "SHFE",
            "source_group": "SHFE", "source_node": "lwg_qh", "quote_ok": True,
            "history_5m_ok": True, "history_15m_ok": True, "history_1h_ok": True,
            "probe_status": "ok", "probe_error": "", "probed_at": "2026-09-19T10:00:00+08:00"},
    "AU0": {"continuous_symbol": "AU0", "name": "黄金", "exchange": "SHFE",
            "source_group": "SHFE", "source_node": "hj_qh", "quote_ok": True,
            "history_5m_ok": None, "history_15m_ok": None, "history_1h_ok": None,
            "probe_status": "quote_only", "probe_error": "", "probed_at": "2026-09-19T10:00:00+08:00"},
    "WH0": {"continuous_symbol": "WH0", "name": "强麦", "exchange": "CZCE",
            "source_group": "CZCE", "source_node": "qm_qh", "quote_ok": False,
            "history_5m_ok": None, "history_15m_ok": None, "history_1h_ok": None,
            "probe_status": "failed", "probe_error": "ConnectTimeout",
            "probed_at": "2026-09-19T10:00:00+08:00"},
}


def catalogue(candidates: dict) -> dict:
    """与脚本产出同形的目录文件；content_hash 始终覆盖当次的候选块。"""
    return {"schema_version": 1, "source": "sina/qihuohangqing.js",
            "source_url": "https://vip.stock.finance.sina.com.cn/qihuohangqing.js",
            "note": "只读发现结果", "fetched_at": "2026-09-19T10:00:00+08:00",
            "snapshot_status": "complete", "content_hash": content_hash(candidates),
            "count": len(candidates), "candidates": candidates}


CATALOGUE = catalogue(CANDIDATES)


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(
        competition={"name": "universe-api", "market": "replay", "initial_cash": 50_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 1, "start_price": {"RB0": 3400}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="model", name="Model", provider="mock")],
        storage={"db_path": str(tmp_path / "universe.db")},
        base_dir=ROOT,
    )
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        yield TestClient(create_app(engine, ROOT / "web")), engine
    finally:
        engine.stop()
        db.close()


@pytest.fixture
def offline(monkeypatch):
    """拦掉一切对外连接：目录是本地静态产物，读取与失败降级都不该碰网络。

    回环放行——TestClient 的事件循环自带 socketpair。
    """
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("candidate catalogue must never touch the network")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def write_catalogue(base: Path, doc) -> None:
    (base / "config").mkdir(parents=True, exist_ok=True)
    (base / DEFAULT_UNIVERSE_FILE).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_missing_catalogue_returns_safe_empty_payload(client, tmp_path):
    api, engine = client
    engine.cfg.base_dir = tmp_path          # 指向一个没有目录文件的根
    body = api.get("/api/universe").json()
    # 空载荷同样交代出处与快照完整性字段，看板不必猜"没数据"是哪一种。
    assert body == {"available": False, "source": "", "source_url": "", "fetched_at": "",
                    "snapshot_status": "unknown", "content_hash": "", "count": 0,
                    "note": body["note"], "candidates": []}
    assert "候选目录" in body["note"]


def test_catalogue_loads_with_local_derived_flags(client, tmp_path):
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_catalogue(tmp_path, CATALOGUE)
    (tmp_path / "config" / "contracts_cn.json").write_text(
        json.dumps({"contracts": {"RB0": {"name": "螺纹钢"}, "AU0": {"name": "黄金"}}}), encoding="utf-8")

    body = api.get("/api/universe").json()

    assert body["available"] is True and body["count"] == 3
    assert body["source"] == "sina/qihuohangqing.js"
    assert body["fetched_at"] == "2026-09-19T10:00:00+08:00"
    assert body["snapshot_status"] == "complete"
    assert body["content_hash"] == CATALOGUE["content_hash"]
    rows = {c["symbol"]: c for c in body["candidates"]}
    # 展示用代码取自文件里持久化的 continuous_symbol，不是另猜一次
    assert sorted(rows) == ["AU0", "RB0", "WH0"]
    # 本地已有规格 + 当前竞赛在交易
    assert rows["RB0"]["configured_spec"] is True and rows["RB0"]["active_in_competition"] is True
    # 本地已有规格，但本期未启用
    assert rows["AU0"]["configured_spec"] is True and rows["AU0"]["active_in_competition"] is False
    # 既无本地规格也未启用；失败状态如实保留
    assert rows["WH0"]["configured_spec"] is False and rows["WH0"]["active_in_competition"] is False
    # 本期没有任何候选被核验过：有本地旧规格的写 legacy_unverified，其余 unknown
    assert rows["RB0"]["verification_status"] == "legacy_unverified"
    assert rows["WH0"]["verification_status"] == "unknown"
    assert rows["RB0"]["source_group"] == "SHFE" and rows["RB0"]["source_node"] == "lwg_qh"
    assert rows["WH0"]["quote_ok"] is False and rows["WH0"]["probe_status"] == "failed"
    assert rows["AU0"]["history_5m_ok"] is None
    assert engine.symbols == ["RB0"]        # 候选没有进入引擎的交易品种
    # 干净文件下的对外字段白名单：没有任何价格/撮合参数
    assert set(rows["RB0"]) == CANDIDATE_KEYS
    assert forbidden_keys_in(body) == set()


def assert_unavailable(response) -> None:
    """安全空载荷的统一断言：不抛异常、不残留 complete、不露出任何候选。"""
    body = response.json()
    assert response.status_code == 200
    assert body["available"] is False
    assert body["candidates"] == [] and body["count"] == 0
    assert body["snapshot_status"] == "unknown" and body["content_hash"] == ""
    assert body["source"] == "" and body["source_url"] == "" and body["fetched_at"] == ""
    assert forbidden_keys_in(body) == set()


def test_malformed_catalogue_degrades_instead_of_leaking(client, tmp_path):
    api, engine = client
    engine.cfg.base_dir = tmp_path
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / DEFAULT_UNIVERSE_FILE).write_text("{not json", encoding="utf-8")
    assert_unavailable(api.get("/api/universe"))


def test_tampered_catalogue_fails_closed(client, tmp_path, offline):
    """哈希是读取侧的完整性边界：对不上就整份作废，且不抛异常、不联网。"""
    api, engine = client
    engine.cfg.base_dir = tmp_path
    tampered = json.loads(json.dumps(CATALOGUE))
    tampered["candidates"]["RB0"]["name"] = "被改过的名字"   # 哈希仍是原来那份的
    write_catalogue(tmp_path, tampered)

    response = api.get("/api/universe")

    assert_unavailable(response)
    assert "被改过的名字" not in response.text


def test_selfhashed_matching_parameters_fail_closed(client, tmp_path, offline):
    """自洽哈希不是通行证：撮合参数让整份文件作废，而不是被静默剥掉后照常上架。

    这是本地用户最省力的攻击路径——加一个 multiplier、把 content_hash 重算成
    自洽的。只做逐字段投影会丢掉那个键却仍然 available=True，等于把改过的
    目录端上看板。
    """
    api, engine = client
    engine.cfg.base_dir = tmp_path
    candidates = json.loads(json.dumps(CANDIDATES))
    candidates["RB0"].update({"multiplier": 10, "tick": 1, "tick_size": 1,
                              "margin_ratio": 0.13, "fee_rate": 0.0001,
                              "sessions": [["09:00", "10:15"]], "verified": True})
    doc = catalogue(candidates)                  # content_hash 覆盖污染后的候选块
    assert doc["content_hash"] == content_hash(doc["candidates"])
    write_catalogue(tmp_path, doc)

    response = api.get("/api/universe")

    assert_unavailable(response)
    # 连没被污染的 AU0 / WH0 也不露出来：不做局部曝光
    for leaked in ("AU0", "WH0", "multiplier", "sessions", "margin_ratio"):
        assert leaked not in response.text


def test_selfhashed_unknown_field_fails_closed(client, tmp_path, offline):
    """白名单之外的键同样让整份作废——顺带保证夹带的秘密不会被回显。"""
    api, engine = client
    engine.cfg.base_dir = tmp_path
    candidates = json.loads(json.dumps(CANDIDATES))
    candidates["RB0"]["api_key"] = "SECRET"
    write_catalogue(tmp_path, catalogue(candidates))

    response = api.get("/api/universe")

    assert_unavailable(response)
    assert "SECRET" not in response.text and "api_key" not in response.text


def test_selfhashed_non_mapping_candidates_fail_closed(client, tmp_path, offline):
    """`candidates: []` 也能自洽成哈希，但它不是映射，不能当"完整的空目录"上架。"""
    api, engine = client
    engine.cfg.base_dir = tmp_path
    doc = catalogue([])                          # 列表而非映射，哈希照样对得上
    assert doc["content_hash"] == content_hash([]) and doc["snapshot_status"] == "complete"
    write_catalogue(tmp_path, doc)

    response = api.get("/api/universe")

    assert_unavailable(response)                 # 尤其是 snapshot_status 不能残留 complete


def test_unidentifiable_candidate_fails_the_whole_document(client, tmp_path, offline):
    """代码只能来自持久化的 continuous_symbol；缺失或自相矛盾即整份作废。"""
    api, engine = client
    engine.cfg.base_dir = tmp_path
    candidates = json.loads(json.dumps(CANDIDATES))
    candidates["RB0"].pop("continuous_symbol")
    write_catalogue(tmp_path, catalogue(candidates))
    assert_unavailable(api.get("/api/universe"))

    candidates = json.loads(json.dumps(CANDIDATES))
    candidates["AU0"]["continuous_symbol"] = "WH0"          # 与自身键位不符
    write_catalogue(tmp_path, catalogue(candidates))
    assert_unavailable(api.get("/api/universe"))


def test_state_endpoint_keeps_candidates_out_of_the_quote_path(client, tmp_path):
    """候选目录是独立的低频接口：高频 /api/state 不带候选，行情也不为候选拉取。"""
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_catalogue(tmp_path, CATALOGUE)
    state = api.get("/api/state").json()
    assert "universe" not in state and "candidates" not in state
    assert {q["symbol"] for q in state["quotes"]} <= set(engine.symbols)
    assert "WH0" not in api.get("/api/state").text


def test_committed_catalogue_is_served(client):
    """仓库里已提交的目录能被真实加载，数量取自文件而不是硬编码常量。"""
    api, _ = client
    body = api.get("/api/universe").json()
    stored = json.loads((ROOT / DEFAULT_UNIVERSE_FILE).read_text(encoding="utf-8"))
    assert body["available"] is True
    assert body["count"] == len(stored["candidates"]) == stored["count"]
    assert any(c["configured_spec"] for c in body["candidates"])
