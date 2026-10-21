from arena.engine.metrics import max_drawdown, sharpe, total_return, trade_stats
from arena.market.indicators import ema, rsi, macd, atr, summarize
from arena.market.replay import ReplaySource, aggregate
from arena.models import Bar


def test_ema_rsi_macd_basic():
    vals = [float(i) for i in range(1, 60)]
    assert abs(ema(vals, 5) - 57.0) < 1.0
    assert rsi(vals, 14) == 100.0
    m = macd(vals)
    assert m and m["dif"] > 0


def test_atr_and_summary(cn_specs):
    src = ReplaySource(["RB0"], cn_specs, seed=1, start_price={"RB0": 3400})
    bars = src.klines("RB0", "5m", 100)
    assert len(bars) > 30
    a = atr(bars, 14)
    assert a and a > 0
    s = summarize(bars)
    assert s["last"] == bars[-1].close and s["ema20"] is not None


def test_aggregate():
    bars = [Bar(ts=i * 60, open=i, high=i + 1, low=i - 1, close=i + 0.5, volume=1) for i in range(10)]
    agg = aggregate(bars, 5)
    assert len(agg) == 2
    assert agg[0].open == 0 and agg[0].close == 4.5 and agg[0].high == 5 and agg[0].volume == 5


def test_metrics():
    assert abs(total_return(100, 110) - 10) < 1e-9
    assert abs(max_drawdown([100, 120, 90, 130]) - 25) < 1e-9
    assert sharpe([100, 101, 102, 103, 104], 252) is not None
    st = trade_stats([
        {"action": "open_long", "realized_pnl": 0, "fee": 1},
        {"action": "close_long", "realized_pnl": 100, "fee": 1},
        {"action": "stop_loss", "realized_pnl": -50, "fee": 1},
    ])
    assert st["closed_trades"] == 2 and st["win_rate"] == 50 and st["profit_factor"] == 2
