"""Regression 63: prompt 里的示例 fact id 必须来自清单本身；研究启动事件写进捕获的竞赛。

两条都是前几轮修复交互出来的：
- quote_only 清单不再登记全 null 的指标 fact（review 59 第 2 条），`_fact_ids()` 又改成
  "引用未登记的 id 就整条拒绝"（review 59 后续）。而 prompt 的示例仍写死 `RB0:5m`——
  模型照着写，一次付费开场直接作废。配置里没有 RB0 时连 quote 示例都是假的。
- 终态事件已经按 worker 捕获的 comp_id 写了，启动事件还在用发事件那一刻的 self.comp_id。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.debate import example_fact_ids, opening_prompt, rebuttal_prompt
from arena.engine.loop import Engine

ROOT = Path(__file__).resolve().parents[1]


def manifest(fact_ids, quality="full"):
    return {"analysis_id": "v1:abc", "snapshot_hash": "h",
            "facts": [{"id": fid} for fid in fact_ids],
            "source_health": {"history_quality": quality}}


# ---------- 示例 fact id ----------

def test_quote_only_examples_never_cite_a_missing_indicator_fact():
    """quote_only 清单里没有任何指标 fact，示例就不能出现 `:5m`。

    还原成写死 `RB0:quote` / `RB0:5m` 后本用例失败。
    """
    m = manifest(["AU0:quote", "CU0:quote"], quality="quote_only")
    prompt = opening_prompt(m, "morning_preopen", "早盘", None)
    allowed = {f["id"] for f in m["facts"]}
    for field in ('"supporting_fact_ids":[', '"counter_fact_ids":['):
        start = prompt.index(field) + len(field)
        cited = prompt[start:prompt.index("]", start)].strip('"')
        assert cited in allowed, f"{field} 引用了清单里没有的 {cited}"


def test_examples_follow_a_configuration_without_rb0():
    """配置里没有 RB0 时，示例也必须换成实际存在的品种。"""
    m = manifest(["AU0:quote", "AU0:5m", "AU0:15m"])
    prompt = opening_prompt(m, "morning_preopen", "早盘", None)
    assert "RB0" not in prompt.split("事实集")[0], "示例仍写死了 RB0"
    allowed = {f["id"] for f in m["facts"]}
    start = prompt.index('"counter_fact_ids":[') + len('"counter_fact_ids":[')
    assert prompt[start:prompt.index("]", start)].strip('"') in allowed


def test_the_rebuttal_example_is_also_derived():
    m = manifest(["AU0:quote"], quality="quote_only")
    prompt = rebuttal_prompt(m, "morning_preopen", "早盘", [], "deepseek")
    start = prompt.index('"challenge_fact_ids":[') + len('"challenge_fact_ids":[')
    assert prompt[start:prompt.index("]", start)].strip('"') == "AU0:quote"


def test_a_single_available_fact_is_used_for_both_lists():
    assert example_fact_ids(manifest(["AU0:quote"], "quote_only")) == ("AU0:quote", "AU0:quote")


def test_indicator_facts_are_preferred_for_the_counter_example():
    """有指标 fact 时两个示例应当不同，模型才看得出两个列表的区别。"""
    support, counter = example_fact_ids(manifest(["AU0:quote", "AU0:5m"]))
    assert support == "AU0:quote" and counter == "AU0:5m"


def test_an_empty_manifest_does_not_crash_the_prompt():
    assert example_fact_ids({"facts": []}) == ("", "")
    opening_prompt(manifest([]), "morning_preopen", "早盘", None)   # 不抛异常即可


# ---------- 启动事件的竞赛归属 ----------

def make_cfg(tmp_path):
    return AppConfig(
        competition={"name": "r63", "market": "replay", "initial_cash": 80_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 9, "history": 4000, "start_price": {"RB0": 3400}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        llm={"timeout_sec": 5, "max_retries": 0},
        preopen_debate={"enabled": False, "stage_timeout_sec": 5},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response="{}")],
        storage={"db_path": str(tmp_path / "r63.db")},
        base_dir=ROOT,
    )


def test_the_start_event_is_written_to_the_captured_competition(tmp_path, monkeypatch):
    """锁放开之后、发事件之前发生重置时，启动事件仍属于 worker 实际启动的那场竞赛。

    还原成不传 comp_id 后本用例失败：事件落到重置后的新竞赛上。
    """
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        launched = eng.comp_id
        written = []
        original_add_event = db.add_event

        def record(comp_id, level, source, message):
            written.append((source, comp_id))
            return original_add_event(comp_id, level, source, message)

        original_start = threading.Thread.start

        def start_then_reset(self):
            # 在 worker 启动之后、启动事件发出之前插入重置：这正是发现描述的那个窗口。
            # 不靠线程调度制造这个时序，用例才不会时灵时不灵。
            original_start(self)
            eng.comp["id"] = launched + 100

        monkeypatch.setattr(db, "add_event", record)
        monkeypatch.setattr(eng, "_run_preopen_debate", lambda *a, **k: None)
        monkeypatch.setattr(threading.Thread, "start", start_then_reset)
        assert eng._start_debate("manual_research", "手动赛前研究", None, trigger="manual") is True
        assert eng.comp_id == launched + 100, "前提不成立：重置未发生"
        debate_events = [c for src, c in written if src == "debate"]
        assert debate_events, "启动事件未发出"
        assert debate_events[0] == launched, (
            f"启动事件写进了 {debate_events[0]}，应当是 worker 捕获的 {launched}")
    finally:
        eng.comp["id"] = launched
        eng.stop()
        db.close()
