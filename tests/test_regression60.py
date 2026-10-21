"""Regression 60: 赛前研究读取回放行情时不得推进回放时钟。

`_preopen_snapshot` 用 `source.latest()` 取报价。对 `ReplaySource` 来说 `latest()` 等于
`latest_with_snapshot()`——**先 advance 一根 bar** 再返回。研究是只读的：那根 bar 不经
`_mark_and_settle` 盯市（止损/止盈/强平都不会按它判定），也不写 `replay_pos`，下个 tick
再 advance 一根，于是这根 bar 被整个跳过。点一下"研究"按钮就能让竞赛结果失真。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine

ROOT = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path):
    traders = [
        TraderCfg(id=tid, name=tid, provider="openai_compatible", base_url="https://example.invalid/v1",
                  model=f"{tid}-test", api_key="test-key")
        for tid in ("deepseek", "qwen")
    ]
    return AppConfig(
        competition={"name": "r60", "market": "replay", "initial_cash": 80_000, "decision_interval_sec": 1,
                     "tick_interval_sec": 1, "snapshot_interval_sec": 1, "timezone": "Asia/Shanghai"},
        symbols=["RB0", "I0"],
        market_data={"replay": {"seed": 11, "history": 4000, "start_price": {"RB0": 3400, "I0": 800}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0},
        llm={"timeout_sec": 5, "max_retries": 0},
        preopen_debate={"enabled": False, "stage_timeout_sec": 5},
        traders=traders,
        storage={"db_path": str(tmp_path / "r60.db")},
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


def test_preopen_research_does_not_advance_the_replay_clock(engine):
    """研究只读行情：回放步数与每个品种的最后一根 bar 都必须原封不动。

    还原修复后本用例失败：`latest()` 会 advance 一根 bar。
    """
    eng, _ = engine
    before = eng.source.position()
    before_bars = {s: len(eng.source.bars[s]) for s in eng.symbols}

    snap, health = eng._preopen_snapshot(eng.round_no)

    after = eng.source.position()
    assert after["steps"] == before["steps"], "研究推进了回放时钟"
    assert after["symbols"] == before["symbols"], "研究让回放多走了一根 bar"
    assert {s: len(eng.source.bars[s]) for s in eng.symbols} == before_bars
    assert snap is not None and health["ok"]


def test_preopen_research_reads_the_replay_source_exactly_once(engine, monkeypatch):
    """报价与 K 线必须来自**同一次**读取，否则 tick 线程能在两次读取之间 advance，
    快照里的最新价就对不上同一根 bar 的指标。

    还原修复后本用例失败：`_preopen_snapshot` 读一次（`latest`），`_build_snapshot` 因为
    没拿到 raw bars 又读一次（`snapshot`），中间正是那个窗口。
    """
    eng, _ = engine
    reads = []
    for name in ("snapshot", "latest_with_snapshot"):
        original = getattr(eng.source, name)

        def counted(symbols, *, _name=name, _original=original):
            reads.append(_name)
            return _original(symbols)

        monkeypatch.setattr(eng.source, name, counted)

    snap, _ = eng._preopen_snapshot(eng.round_no)
    assert snap is not None
    assert reads == ["snapshot"], f"研究读了 {len(reads)} 次回放行情：{reads}"
    for symbol, view in snap.symbols.items():
        assert view.quote.price == pytest.approx(eng.source.bars[symbol][-1].close)
        assert view.quote.ts == pytest.approx(eng.source.bars[symbol][-1].ts)


def test_research_between_ticks_leaves_no_bar_unsettled(engine):
    """端到端：一个 tick → 研究 → 下一个 tick，两个 tick 之间不能有任何一根 bar 没被结算。

    回放时钟只应由 tick 推进，所以两个 tick 之间的步数差必须正好是 1。
    """
    eng, _ = engine
    eng._tick()
    steps_after_first = eng.source.position()["steps"]
    eng._preopen_snapshot(eng.round_no)
    eng._tick()
    assert eng.source.position()["steps"] == steps_after_first + 1
