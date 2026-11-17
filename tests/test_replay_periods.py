"""Replay indicators must describe the configured base-bar duration."""
from threading import Event
from types import SimpleNamespace

import pytest

from arena.config import ReplayCfg
from arena.engine.loop import Engine
from arena.market import indicators
from arena.market.replay import ReplaySource, aggregate
from arena.models import ContractSpec


def source(seconds=60):
    return ReplaySource(
        ["X"], {"X": ContractSpec("X", "synthetic", tick=1)},
        bar_seconds=seconds, history=2401,
    )


@pytest.mark.parametrize("seconds", [10, 30, 60, 75, 150, 300])
@pytest.mark.parametrize("period,duration", [("5m", 300), ("15m", 900), ("1h", 3600)])
def test_replay_period_matches_elapsed_time(seconds, period, duration):
    replay = source(seconds)
    bars = replay.klines("X", period)
    assert len(bars) >= 2
    assert bars[-1].ts - bars[-2].ts == pytest.approx(duration)
    assert bars[-1].ts + duration <= replay.bars["X"][-1].ts


def test_default_sixty_second_aggregation_is_unchanged():
    replay = source()
    raw = replay.bars["X"]
    assert replay.klines("X", "1m") == raw[-120:]
    for period, count in [("5m", 5), ("15m", 15), ("1h", 60), ("1d", 240)]:
        assert replay.klines("X", period) == aggregate(raw[:-1], count)[-120:]


@pytest.mark.parametrize("seconds", [7, 40, 120, 301])
def test_config_rejects_base_periods_that_cannot_form_five_minutes(seconds):
    with pytest.raises(ValueError, match="divide 300"):
        ReplayCfg(bar_seconds=seconds)


def test_source_rejects_incompatible_period_before_generating_bars():
    with pytest.raises(ValueError, match="divide 300"):
        source(120)


def test_one_minute_request_cannot_split_a_larger_base_bar():
    with pytest.raises(ValueError, match="cannot aggregate"):
        source(75).klines("X", "1m")


@pytest.mark.parametrize("seconds", [30, 75, 300])
def test_engine_snapshot_uses_the_same_base_period_as_source(seconds):
    replay = source(seconds)
    engine = SimpleNamespace(
        source=replay, quotes={}, round_no=1, symbols=["X"],
        _stop=Event(), specs=replay.specs(), currency="CNY",
        cfg=SimpleNamespace(competition=SimpleNamespace(market="replay")),
        _requires_live_quote_freshness=lambda: False,
    )
    snap = Engine._build_snapshot(engine)
    view = snap.symbols["X"]
    assert view.bars_5m == replay.klines("X", "5m", 120)
    assert view.bars_5m[-1].ts - view.bars_5m[-2].ts == pytest.approx(300)
    assert view.ind_15m == indicators.summarize(replay.klines("X", "15m", 100))
    assert view.ind_1h == indicators.summarize(replay.klines("X", "1h", 100))


def csv_source(tmp_path, timestamps, seconds):
    lines = ["datetime,open,high,low,close,volume"]
    lines += [f"{ts},100,101,99,100,1" for ts in timestamps]
    (tmp_path / "X.csv").write_text("\n".join(lines), encoding="utf-8")
    return ReplaySource(["X"], {"X": ContractSpec("X", "synthetic", tick=1)},
                        csv_dir=str(tmp_path), bar_seconds=seconds, history=len(timestamps))


def test_thirty_second_csv_and_synthetic_tail_keep_the_same_period(tmp_path):
    replay = csv_source(tmp_path, [1_700_000_000 + i * 30 for i in range(121)], 30)
    bars = replay.klines("X", "5m")
    assert bars[-1].ts - bars[-2].ts == 300
    replay.advance(2)
    assert replay.bars["X"][-1].ts - replay.bars["X"][-2].ts == 30
    assert replay.klines("X", "5m")[-1].ts - replay.klines("X", "5m")[-2].ts == 300


@pytest.mark.parametrize("offsets", [[0, 60, 120], [0], [0, 30, 75], [0, 30, 30]])
def test_csv_with_unproven_or_incompatible_nondefault_period_is_rejected(tmp_path, offsets):
    with pytest.raises(ValueError, match="CSV base period"):
        csv_source(tmp_path, [1_700_000_000 + offset for offset in offsets], 30)


def test_nondefault_csv_allows_missing_rows_and_session_gaps(tmp_path):
    replay = csv_source(tmp_path, [1_700_000_000 + offset for offset in [0, 30, 90, 3600]], 30)
    assert len(replay.bars["X"]) == 4


def test_default_minute_csv_keeps_legacy_sparse_history_behavior(tmp_path):
    replay = csv_source(tmp_path, [1_700_000_000, 1_700_003_600], 60)
    assert len(replay.bars["X"]) == 2
