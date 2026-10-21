import json

from arena.config import RiskCfg
from arena.engine.risk import RiskManager
from arena.exchange.contracts import crypto_spec, legacy_fallback_cn_spec, load_cn_contracts
from arena.models import (SPEC_ORIGIN_LOCAL_TABLE, SPEC_ORIGIN_RECOVERY_FALLBACK, SPEC_ORIGIN_UNSPECIFIED,
                          SPEC_ORIGIN_VENUE, ActionRequest, ContractSpec, Position)


def test_auto_stop_and_size_cap(cn_specs, account):
    rm = RiskManager(RiskCfg(max_size_pct=0.2), cn_specs)
    req = ActionRequest(symbol="RB0", action="open_long", size_pct=0.9)
    v = rm.check(account, req, 3400.0, {"RB0": 3400.0}, atr=20.0)
    assert v.ok
    assert v.action.size_pct == 0.2
    assert v.action.stop_loss == 3400.0 - 40.0
    assert v.action.leverage == 1.0


def test_auto_long_stop_never_at_or_below_zero(cn_specs, account):
    rm = RiskManager(RiskCfg(auto_stop_atr_mult=2.0), cn_specs)
    # ATR×倍数 ≥ 现价：原本会生成 ≤ 0 的止损（永不触发）；现在距离最多为现价的一半
    v = rm.check(account, ActionRequest(symbol="RB0", action="open_long", size_pct=0.1), 100.0, {"RB0": 100.0}, atr=80.0)
    assert v.ok and v.action.stop_loss == 50.0
    tick = cn_specs["RB0"].tick
    v2 = rm.check(account, ActionRequest(symbol="RB0", action="open_long", size_pct=0.1), tick, {"RB0": tick}, atr=80.0)
    assert not v2.ok and "止损" in v2.reason


def test_bad_sl_direction_rejected(cn_specs, account):
    rm = RiskManager(RiskCfg(), cn_specs)
    req = ActionRequest(symbol="RB0", action="open_long", size_pct=0.1, stop_loss=3500.0)
    v = rm.check(account, req, 3400.0, {"RB0": 3400.0})
    assert not v.ok


def test_max_positions(cn_specs, account):
    rm = RiskManager(RiskCfg(max_positions=1), cn_specs)
    account.positions["I0"] = Position("t1", "I0", "long", 1, 800, 1000)
    v = rm.check(account, ActionRequest(symbol="RB0", action="open_long", size_pct=0.1), 3400.0, {"RB0": 3400.0, "I0": 800.0})
    assert not v.ok and "上限" in v.reason


def test_close_without_position(cn_specs, account):
    rm = RiskManager(RiskCfg(), cn_specs)
    assert not rm.check(account, ActionRequest(symbol="RB0", action="close"), 3400.0, {}).ok


def test_unknown_symbol(cn_specs, account):
    rm = RiskManager(RiskCfg(), cn_specs)
    assert not rm.check(account, ActionRequest(symbol="XX0", action="open_long", size_pct=0.1), 1.0, {}).ok


def test_aggregate_margin_cap_stops_sizing_when_the_account_is_already_full(cn_specs, account):
    account.positions["I0"] = Position("t1", "I0", "long", 1, 800, 950_000, last_price=800)
    rm = RiskManager(RiskCfg(max_margin_risk_ratio=0.95), cn_specs)
    verdict = rm.check(account, ActionRequest(symbol="RB0", action="open_long", size_pct=0.1), 3400.0, {"RB0": 3400.0, "I0": 800.0})
    assert not verdict.ok and "aggregate" in verdict.reason.lower()


def test_target_margin_band_caps_an_existing_signal_before_the_hard_cap(cn_specs, account):
    """The target upper bound limits an already-proposed order without creating one."""
    account.positions["I0"] = Position("t1", "I0", "long", 1, 800, 750_000, last_price=800)
    cfg = RiskCfg(target_margin_risk_min_ratio=0.80, target_margin_risk_max_ratio=0.90,
                  max_margin_risk_ratio=0.95)
    verdict = RiskManager(cfg, cn_specs).check(
        account, ActionRequest(symbol="RB0", action="open_long", size_pct=0.30), 3400.0,
        {"RB0": 3400.0, "I0": 800.0},
    )
    assert verdict.ok
    assert abs(verdict.action.size_pct - 0.15) < 1e-9


def test_target_margin_band_rejects_new_risk_at_its_upper_bound(cn_specs, account):
    account.positions["I0"] = Position("t1", "I0", "long", 1, 800, 900_000, last_price=800)
    cfg = RiskCfg(target_margin_risk_min_ratio=0.80, target_margin_risk_max_ratio=0.90,
                  max_margin_risk_ratio=0.95)
    verdict = RiskManager(cfg, cn_specs).check(
        account, ActionRequest(symbol="RB0", action="open_long", size_pct=0.10), 3400.0,
        {"RB0": 3400.0, "I0": 800.0},
    )
    assert not verdict.ok and "target ceiling" in verdict.reason.lower()


def test_legacy_stricter_liquidation_threshold_can_omit_the_new_admission_cap():
    cfg = RiskCfg(liquidation_risk_ratio=0.0001)
    assert cfg.max_margin_risk_ratio is None


def test_recovery_fallback_spec_can_be_closed_but_never_opened(cn_specs, account):
    """A code the table no longer lists may be wound down, never opened."""
    gone = legacy_fallback_cn_spec("ZZ0", reason="regression")
    assert gone.origin == SPEC_ORIGIN_RECOVERY_FALLBACK and not gone.admits_new_positions
    specs = {**cn_specs, "ZZ0": gone}
    rm = RiskManager(RiskCfg(), specs)
    prices = {"ZZ0": 1000.0}

    opened = rm.check(account, ActionRequest(symbol="ZZ0", action="open_long", size_pct=0.1), 1000.0, prices, atr=10.0)
    assert not opened.ok and "不在合约表" in opened.reason
    shorted = rm.check(account, ActionRequest(symbol="ZZ0", action="open_short", size_pct=0.1), 1000.0, prices, atr=10.0)
    assert not shorted.ok and "不在合约表" in shorted.reason

    account.positions["ZZ0"] = Position(trader_id="t1", symbol="ZZ0", side="long", qty=1, avg_price=1000.0, margin=100.0)
    assert rm.check(account, ActionRequest(symbol="ZZ0", action="close"), 1000.0, prices).ok, \
        "closing out a legacy position must stay allowed"
    adjusted = rm.check(account, ActionRequest(symbol="ZZ0", action="adjust_sl_tp", stop_loss=900.0), 1000.0, prices)
    assert adjusted.ok, "tightening a stop reduces risk and must not be blocked by the fallback"
    assert rm.check(account, ActionRequest(symbol="ZZ0", action="open_long", size_pct=0.1), 1000.0, prices, atr=10.0).ok is False


def test_complete_table_specs_are_admitted_to_open(cn_specs, account):
    """A fully explicit local row opens normally -- which is not a claim it is exchange-verified."""
    assert all(spec.origin == SPEC_ORIGIN_LOCAL_TABLE and spec.admits_new_positions for spec in cn_specs.values())
    assert RiskManager(RiskCfg(), cn_specs).check(
        account, ActionRequest(symbol="RB0", action="open_long", size_pct=0.1), 3400.0, {"RB0": 3400.0}, atr=20.0).ok


def test_a_directly_constructed_spec_cannot_open_through_risk(account):
    """ContractSpec's dataclass defaults are placeholders, not terms, so they must not size an order."""
    bare = ContractSpec(symbol="XX0", name="XX0")
    assert bare.origin == SPEC_ORIGIN_UNSPECIFIED and not bare.admits_new_positions
    rm = RiskManager(RiskCfg(), {"XX0": bare})
    prices = {"XX0": 1000.0}
    for action in ("open_long", "open_short"):
        verdict = rm.check(account, ActionRequest(symbol="XX0", action=action, size_pct=0.1), 1000.0, prices, atr=10.0)
        assert not verdict.ok and "不接受开仓" in verdict.reason
    # Winding a legacy position down stays possible even under an unspecified spec.
    account.positions["XX0"] = Position(trader_id="t1", symbol="XX0", side="long", qty=1, avg_price=1000.0, margin=100.0)
    assert rm.check(account, ActionRequest(symbol="XX0", action="close"), 1000.0, prices).ok


def test_a_spec_from_an_incomplete_table_row_cannot_open(tmp_path, account):
    """The lenient loader marks an invalid row unspecified instead of defaulting its terms."""
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"contracts": {"XX0": {"name": "XX0", "multiplier": 10}}}), encoding="utf-8")
    specs, _ = load_cn_contracts(path)
    assert specs["XX0"].origin == SPEC_ORIGIN_UNSPECIFIED
    verdict = RiskManager(RiskCfg(), specs).check(
        account, ActionRequest(symbol="XX0", action="open_long", size_pct=0.1), 1000.0, {"XX0": 1000.0}, atr=10.0)
    assert not verdict.ok


def test_venue_specs_are_admitted_to_open():
    """Crypto terms come from the venue's own rules and are intentionally complete."""
    spec = crypto_spec("BTC-USDT-SWAP")
    assert spec.origin == SPEC_ORIGIN_VENUE and spec.admits_new_positions


def test_risk_lookup_stays_fail_closed_for_unknown_symbols(cn_specs, account):
    """specs.get() does not trigger _SpecMap.__missing__; this boundary must never become specs[...]."""
    verdict = RiskManager(RiskCfg(), cn_specs).check(
        account, ActionRequest(symbol="NOPE0", action="open_long", size_pct=0.1), 100.0, {"NOPE0": 100.0})
    assert not verdict.ok and "未知品种" in verdict.reason
