from dataclasses import replace

from arena.exchange.contracts import legacy_fallback_cn_spec
from arena.exchange.sim import SimExchange
from arena.models import (SPEC_ORIGIN_LOCAL_TABLE, SPEC_ORIGIN_RECOVERY_FALLBACK, SPEC_ORIGIN_UNSPECIFIED,
                          Account, ContractSpec, Position)


def test_open_close_long_pnl(cn_specs, account):
    ex = SimExchange(cn_specs, slippage_ticks=0)
    spec = cn_specs["RB0"]
    prices = {"RB0": 3400.0}
    r = ex.open(account, spec, "long", 10, 3400.0, 1.0, 3300.0, 3600.0, prices, round_no=1)
    assert r.accepted, r.reason
    pos = account.positions["RB0"]
    assert pos.qty == 10 and pos.avg_price == 3400
    assert pos.margin == 3400 * 10 * 10 * spec.margin_ratio
    fee_open = r.trade.fee
    assert fee_open > 0
    prices["RB0"] = 3450.0
    assert abs(account.unrealized(prices, cn_specs) - 50 * 10 * 10) < 1e-6
    r2 = ex.close(account, spec, 3450.0, round_no=2)
    assert r2.accepted
    assert "RB0" not in account.positions
    assert abs(account.realized_pnl - 5000) < 1e-6
    assert abs(account.cash - (1_000_000 + 5000 - fee_open - r2.trade.fee)) < 1e-6


def test_short_and_stop_loss_trigger(cn_specs, account):
    ex = SimExchange(cn_specs, slippage_ticks=0)
    spec = cn_specs["I0"]
    prices = {"I0": 800.0}
    r = ex.open(account, spec, "short", 5, 800.0, 1.0, 820.0, 760.0, prices, round_no=1)
    assert r.accepted
    trades = ex.mark(account, {"I0": 821.0}, round_no=2)
    assert len(trades) == 1 and trades[0].action == "stop_loss"
    assert "I0" not in account.positions
    assert account.realized_pnl < 0


def test_mark_uses_each_symbols_own_timestamp(cn_specs, account):
    ex = SimExchange(cn_specs, slippage_ticks=0)
    prices = {"RB0": 3400.0, "I0": 800.0}
    assert ex.open(account, cn_specs["RB0"], "long", 1, 3400.0, 1.0, 3300.0, None, prices, round_no=1, ts=100.0).accepted
    assert ex.open(account, cn_specs["I0"], "short", 1, 800.0, 1.0, 820.0, None, prices, round_no=1, ts=100.0).accepted
    # 回放数据流未对齐：RB0 的报价时刻 500，I0 的报价时刻 900；两者同一 tick 触发止损
    trades = ex.mark(account, {"RB0": 3290.0, "I0": 821.0}, 2, ts=900.0, ts_by_symbol={"RB0": 500.0})
    by_sym = {t.symbol: t for t in trades}
    assert by_sym["RB0"].action == "stop_loss" and by_sym["RB0"].ts == 500.0, "触发成交记在该品种自己的报价时刻"
    assert by_sym["I0"].action == "stop_loss" and by_sym["I0"].ts == 900.0, "未提供逐品种时钟的品种退回 ts"


def test_insufficient_margin_rejected(cn_specs):
    acc = Account(trader_id="t", cash=10_000, initial_cash=10_000)
    ex = SimExchange(cn_specs)
    r = ex.open(acc, cn_specs["AU0"], "long", 5, 560.0, 1.0, None, None, {"AU0": 560.0}, round_no=1)
    assert not r.accepted and "不足" in r.reason


def test_opposite_side_rejected(cn_specs, account):
    ex = SimExchange(cn_specs)
    spec = cn_specs["RB0"]
    prices = {"RB0": 3400.0}
    assert ex.open(account, spec, "long", 1, 3400.0, 1.0, None, None, prices, 1).accepted
    r = ex.open(account, spec, "short", 1, 3400.0, 1.0, None, None, prices, 1)
    assert not r.accepted


def test_qty_for_pct(cn_specs):
    ex = SimExchange(cn_specs)
    spec = cn_specs["RB0"]
    q = ex.qty_for_pct(spec, 3400.0, 1_000_000, 0.2, 1.0)
    # 保证金 3400*10*0.13=4420/手，20万预算 → 45 手
    assert q == 45


def test_account_liquidation_on_risk_ratio(cn_specs):
    acc = Account(trader_id="t", cash=50_000, initial_cash=50_000)
    ex = SimExchange(cn_specs, slippage_ticks=0, liquidation_risk_ratio=1.0)
    spec = cn_specs["RB0"]
    r = ex.open(acc, spec, "long", 10, 3400.0, 1.0, None, None, {"RB0": 3400.0}, 1)
    assert r.accepted
    # 保证金 44200，权益跌到 44000 以下即风险度>1
    trades = ex.mark(acc, {"RB0": 3330.0}, 2)
    assert trades and trades[-1].action == "liquidation"
    assert not acc.positions and acc.liquidations == 1


def test_account_liquidation_closes_unquoted_positions_at_their_last_price(cn_specs):
    """全账户强平必须一次平掉全部持仓：本 tick 没有报价的品种按持久化的最新价（估值用的就是它）平仓，
    不能留着敞口却已计一次强平。"""
    acc = Account(trader_id="t", cash=100_000, initial_cash=100_000)
    ex = SimExchange(cn_specs, slippage_ticks=0, liquidation_risk_ratio=1.0)
    both = {"RB0": 3400.0, "I0": 800.0}
    assert ex.open(acc, cn_specs["RB0"], "long", 5, 3400.0, 1.0, None, None, both, 1).accepted
    assert ex.open(acc, cn_specs["I0"], "long", 2, 800.0, 1.0, None, None, both, 1).accepted
    assert not ex.mark(acc, {"RB0": 3400.0, "I0": 790.0}, 2)          # I0 最新价 790 已记到持仓上
    assert acc.positions["I0"].last_price == 790.0
    trades = ex.mark(acc, {"RB0": 2200.0}, 3)                          # 只来了 RB0 的报价，风险度突破
    assert not acc.positions and acc.liquidations == 1, "全账户强平必须一次平掉全部持仓"
    by_sym = {t.symbol: t for t in trades}
    assert by_sym["RB0"].action == "liquidation" and by_sym["RB0"].price == 2200.0
    assert by_sym["I0"].action == "liquidation" and by_sym["I0"].price == 790.0 and "最后价" in by_sym["I0"].reason
    assert not ex.mark(acc, {"I0": 780.0}, 4) and acc.liquidations == 1   # 之后的部分报价不会再算一次强平


def test_pretrade_margin_cap_allows_exact_95_percent_and_rejects_a_breach(cn_specs):
    """The configured <= 95% admission boundary is inclusive and atomic."""
    spec = replace(cn_specs["RB0"], fee_rate=0.0)
    exact_cash = 3400.0 * spec.multiplier * spec.margin_ratio / 0.95
    acc = Account(trader_id="t", cash=exact_cash, initial_cash=exact_cash)
    ex = SimExchange({spec.symbol: spec}, slippage_ticks=0, max_margin_risk_ratio=0.95)
    exact = ex.open(acc, spec, "long", 1, 3400.0, 1.0, None, None, {"RB0": 3400.0}, 1)
    assert exact.accepted, exact.reason

    capped = Account(trader_id="t", cash=50_000, initial_cash=50_000)
    before = (capped.cash, capped.fees, dict(capped.positions))
    rejected = SimExchange(cn_specs, slippage_ticks=0, max_margin_risk_ratio=0.95).open(
        capped, cn_specs["RB0"], "long", 11, 3400.0, 1.0, None, None, {"RB0": 3400.0}, 1,
    )
    assert not rejected.accepted and "risk" in rejected.reason.lower()
    assert (capped.cash, capped.fees, capped.positions) == before, "rejected fills must not mutate the account"


def test_target_margin_band_allows_exact_upper_bound_and_rejects_postfill_breach(cn_specs):
    """The normal 90% ceiling is inclusive and enforced from actual fill terms."""
    spec = replace(cn_specs["RB0"], fee_rate=0.0)
    exact_cash = 3400.0 * spec.multiplier * spec.margin_ratio / 0.90
    exact = Account(trader_id="t", cash=exact_cash, initial_cash=exact_cash)
    ex = SimExchange({spec.symbol: spec}, slippage_ticks=0, max_margin_risk_ratio=0.95,
                     target_margin_risk_max_ratio=0.90)
    accepted = ex.open(exact, spec, "long", 1, 3400.0, 1.0, None, None, {"RB0": 3400.0}, 1)
    assert accepted.accepted, accepted.reason

    breached = Account(trader_id="t", cash=50_000, initial_cash=50_000)
    before = (breached.cash, breached.fees, dict(breached.positions))
    rejected = SimExchange(cn_specs, slippage_ticks=1, max_margin_risk_ratio=0.95,
                           target_margin_risk_max_ratio=0.885).open(
        breached, cn_specs["RB0"], "long", 10, 3400.0, 1.0, None, None, {"RB0": 3400.0}, 1,
    )
    assert not rejected.accepted and "operating target" in rejected.reason.lower()
    assert (breached.cash, breached.fees, breached.positions) == before


def test_pretrade_margin_cap_applies_to_combined_contract_margin(cn_specs):
    acc = Account(trader_id="t", cash=50_000, initial_cash=50_000)
    prices = {"RB0": 3400.0, "I0": 800.0, "M0": 3100.0}
    ex = SimExchange(cn_specs, slippage_ticks=0, max_margin_risk_ratio=0.95)
    assert ex.open(acc, cn_specs["RB0"], "long", 5, 3400.0, 1.0, None, None, prices, 1).accepted
    assert ex.open(acc, cn_specs["I0"], "long", 2, 800.0, 1.0, None, None, prices, 1).accepted
    assert ex.open(acc, cn_specs["M0"], "long", 1, 3100.0, 1.0, None, None, prices, 1).accepted
    before_margin, before_positions = acc.margin_used(), {k: (v.qty, v.margin) for k, v in acc.positions.items()}
    rejected = ex.open(acc, cn_specs["M0"], "long", 1, 3100.0, 1.0, None, None, prices, 2)
    assert not rejected.accepted and "risk" in rejected.reason.lower()
    assert acc.margin_used() == before_margin
    assert {k: (v.qty, v.margin) for k, v in acc.positions.items()} == before_positions


def test_pretrade_margin_cap_uses_actual_fill_slippage_and_fee(cn_specs):
    """A quote-only 88.4% estimate must not bypass an 88.5% real-fill cap."""
    acc = Account(trader_id="t", cash=50_000, initial_cash=50_000)
    ex = SimExchange(cn_specs, slippage_ticks=1, max_margin_risk_ratio=0.885)
    rejected = ex.open(acc, cn_specs["RB0"], "long", 10, 3400.0, 1.0, None, None, {"RB0": 3400.0}, 1)
    assert not rejected.accepted and "risk" in rejected.reason.lower()
    assert not acc.positions and acc.fees == 0 and acc.cash == 50_000


def test_recovery_fallback_spec_is_refused_at_the_matching_boundary(cn_specs):
    """Matching blocks it too: pricing a fill off invented terms fabricates it."""
    gone = legacy_fallback_cn_spec("ZZ0", reason="regression")
    assert gone.origin == SPEC_ORIGIN_RECOVERY_FALLBACK and not gone.admits_new_positions
    acc = Account(trader_id="t", cash=1_000_000, initial_cash=1_000_000)
    ex = SimExchange({**cn_specs, "ZZ0": gone}, slippage_ticks=0)
    rejected = ex.open(acc, gone, "long", 1, 1000.0, 1.0, None, None, {"ZZ0": 1000.0}, round_no=1)
    assert not rejected.accepted and "不在合约表" in rejected.reason
    assert not acc.positions and acc.cash == 1_000_000 and acc.fees == 0


def test_a_directly_constructed_spec_cannot_open_through_matching(cn_specs):
    """SimExchange.open() is reachable without the engine, so it enforces the gate itself."""
    bare = ContractSpec(symbol="XX0", name="XX0")
    assert bare.origin == SPEC_ORIGIN_UNSPECIFIED and not bare.admits_new_positions
    acc = Account(trader_id="t", cash=1_000_000, initial_cash=1_000_000)
    ex = SimExchange({**cn_specs, "XX0": bare}, slippage_ticks=0)
    rejected = ex.open(acc, bare, "long", 1, 1000.0, 1.0, None, None, {"XX0": 1000.0}, round_no=1)
    assert not rejected.accepted and "不接受开仓" in rejected.reason
    assert not acc.positions and acc.cash == 1_000_000 and acc.fees == 0, "refused before sizing, fees or mutation"


def test_a_complete_table_spec_still_opens_through_matching(cn_specs):
    """The positive case: a fully explicit local row is admitted and fills normally."""
    spec = cn_specs["RB0"]
    assert spec.origin == SPEC_ORIGIN_LOCAL_TABLE and spec.admits_new_positions
    acc = Account(trader_id="t", cash=1_000_000, initial_cash=1_000_000)
    ex = SimExchange(cn_specs, slippage_ticks=0)
    accepted = ex.open(acc, spec, "long", 1, 3400.0, 1.0, None, None, {"RB0": 3400.0}, round_no=1)
    assert accepted.accepted and acc.positions["RB0"].qty == 1


def test_an_unspecified_spec_can_still_be_marked_and_liquidated(cn_specs):
    """Recovery must keep working: mark/liquidation never goes through open()."""
    bare = ContractSpec(symbol="XX0", name="XX0", multiplier=10, tick=1, margin_ratio=0.1)
    acc = Account(trader_id="t", cash=100.0, initial_cash=1_000.0)
    acc.positions["XX0"] = Position(trader_id="t", symbol="XX0", side="long", qty=1, avg_price=1000.0,
                                    margin=1000.0, last_price=1000.0)
    ex = SimExchange({"XX0": bare}, slippage_ticks=0, min_hold_sec=0)
    trades = ex.mark(acc, {"XX0": 800.0}, round_no=1)
    assert trades and trades[0].action == "liquidation" and "XX0" not in acc.positions


def test_recovery_fallback_spec_can_still_be_closed(cn_specs):
    """A legacy position must remain closable, or it is stuck in the account forever."""
    gone = legacy_fallback_cn_spec("ZZ0", reason="regression")
    acc = Account(trader_id="t", cash=1_000_000, initial_cash=1_000_000)
    acc.positions["ZZ0"] = Position(trader_id="t", symbol="ZZ0", side="long", qty=1, avg_price=1000.0,
                                    margin=1000.0 * gone.multiplier * gone.margin_ratio, last_price=1100.0)
    ex = SimExchange({**cn_specs, "ZZ0": gone}, slippage_ticks=0, min_hold_sec=0)
    closed = ex.close(acc, gone, 1100.0, round_no=1, reason="symbol dropped from the configuration", force=True)
    assert closed.accepted and "ZZ0" not in acc.positions
    assert abs(closed.trade.realized_pnl - 100.0 * gone.multiplier) < 1e-6
