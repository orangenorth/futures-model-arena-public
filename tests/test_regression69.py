"""Regression 69: 全品种研究在 CTP 竞赛下同样可用。

CTP 只是执行行情源；逐品种研究与盘前全量扫描都用自己的公开目录读取器和同一张国内合约日历，
所以 `competition.market: ctp` 不能被当成"引擎未运行"而拒绝启动，看板按钮也不能被灰掉。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _cfg(tmp_path):
    traders = [
        TraderCfg(id=tid, name=tid, provider="openai_compatible", base_url="https://example.invalid/v1",
                  model=f"{tid}-test", api_key="test-key")
        for tid in ("deepseek", "qwen", "kimi", "glm")
    ]
    return AppConfig(
        competition={"name": "r69", "market": "replay", "initial_cash": 80_000, "decision_interval_sec": 1,
                     "tick_interval_sec": 1, "snapshot_interval_sec": 1, "timezone": "Asia/Shanghai"},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400, "I0": 800}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        preopen_debate={"enabled": False, "stage_timeout_sec": 5},
        traders=traders,
        storage={"db_path": str(tmp_path / "r69.db")},
        base_dir=ROOT,
    )


@pytest.fixture
def engine(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    yield eng
    eng.running = False
    eng.stop()
    db.close()


@pytest.mark.parametrize("market, allowed", [("cn_futures", True), ("ctp", True), ("crypto", False), ("replay", False)])
def test_product_research_start_guard_accepts_every_domestic_market(engine, market, allowed):
    engine.running = True
    engine.cfg.competition.market = market
    assert engine.product_research._can_start() is allowed


def test_a_ctp_competition_runs_the_full_universe_preopen_scan(engine, monkeypatch):
    scanned = []

    class Scanner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            scanned.append(True)
            return {}

        @staticmethod
        def failed_manifest(path):
            return {}

    monkeypatch.setattr("arena.engine.loop.discover_catalogue", lambda *a, **k: {})
    monkeypatch.setattr("arena.engine.loop.FullUniverseScanner", Scanner)
    monkeypatch.setattr(engine, "_preopen_universe_reader_instance", lambda: object())

    engine.cfg.competition.market = "ctp"
    research = engine._preopen_universe_research(engine.comp_id, engine._generation, engine._decision_epoch,
                                                 allow_paused=True)
    assert scanned == [True], "CTP 竞赛也必须跑全量候选扫描，不能静默退回只有执行池的研究"
    assert research is not None

    scanned.clear()
    engine.cfg.competition.market = "crypto"
    assert engine._preopen_universe_research(engine.comp_id, engine._generation, engine._decision_epoch,
                                             allow_paused=True) is None
    assert scanned == []


def _is_domestic_market(market):
    match = re.search(r"function isDomesticMarket\(market\) \{.*?\n  \}", APP_JS, re.S)
    assert match, "web/app.js must decide the domestic-market gate in one helper"
    script = match.group(0) + f"\nconsole.log(JSON.stringify(isDomesticMarket({json.dumps(market)})));"
    output = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(output.stdout)


def test_dashboard_full_universe_button_is_enabled_for_ctp():
    assert _is_domestic_market("cn_futures") is True
    assert _is_domestic_market("ctp") is True
    assert _is_domestic_market("crypto") is False
    assert _is_domestic_market("replay") is False
    assert "isDomesticMarket(state.data?.competition?.market) && ready && !active" in APP_JS
    assert "competition?.market === 'cn_futures'" not in APP_JS
