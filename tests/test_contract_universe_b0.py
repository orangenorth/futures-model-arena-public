"""B0 safety floor.

Three invariants live here:
  * an active symbol without an explicit, valid local specification stops startup
    before any source, exchange, risk manager, model call or database work;
  * competition identity depends on the active universe (including its order) and
    on a digest of the terms in force, so neither can change under one leaderboard;
  * the Phase A candidate catalogue never reaches the execution path.

The companion degraded-spec boundary (a code the table no longer lists may be
closed but never opened) lives in tests/test_risk.py and tests/test_sim.py.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from arena.config import AppConfig, TraderCfg, load_config
from arena.db import Database
from arena.engine.loop import CONTRACT_UNIVERSE_KEY, Engine, contract_universe_signature
from arena.exchange.contracts import (ContractSpecError, contract_terms_digest, load_active_cn_contracts,
                                      load_cn_contracts, preflight_contract_specs, validate_active_cn_contracts)
from arena.exchange.sim import SimExchange
from arena.engine.risk import RiskManager
from arena.config import RiskCfg
from arena.models import (OPEN_ADMITTED_SPEC_ORIGINS, SPEC_ORIGIN_LOCAL_TABLE, SPEC_ORIGIN_UNSPECIFIED,
                          Account, ActionRequest, ContractSpec)
from arena.universe import load_catalogue

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "config" / "contracts_cn.json"
CATALOGUE = ROOT / "config" / "contracts_universe_cn.json"
DEMO_SYMBOLS = ["RB0", "I0", "AU0", "CU0", "M0", "TA0"]   # the pool in config/demo.yaml
VALID_ROW = {"name": "Test", "exchange": "SHFE", "multiplier": 10, "tick": 1, "margin_ratio": 0.13,
             "fee_per_lot": 0, "fee_rate": 0.0001,
             "sessions": [["09:00", "10:15"], ["21:00", "23:00"]]}


def make_cfg(tmp_path, symbols, contracts_file="config/contracts_cn.json", db_name="b0.db"):
    return AppConfig(
        competition={"name": "b0", "market": "replay", "initial_cash": 80_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=list(symbols),
        market_data={"replay": {"seed": 5, "start_price": {s: 1000.0 for s in symbols}},
                     "cn_futures": {"contracts_file": contracts_file}},
        risk={"min_hold_sec": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response="")],
        storage={"db_path": str(tmp_path / db_name)},
        base_dir=ROOT,
    )


def contracts_copy(tmp_path) -> Path:
    """A copy of the shipped table, for "same path, edited content" scenarios."""
    dest = tmp_path / "contracts_cn.json"
    dest.write_text(CONTRACTS.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def write_table(tmp_path, contracts, name="table.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"holidays": [], "contracts": contracts}, ensure_ascii=False), encoding="utf-8")
    return path


def edit_term(path: Path, symbol: str, **terms) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["contracts"][symbol].update(terms)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def drop_term(path: Path, symbol: str, field: str) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["contracts"][symbol].pop(field, None)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def candidate_only_symbols():
    """Codes the catalogue lists that have no local specification."""
    catalogue = load_catalogue(CATALOGUE)
    assert catalogue["available"], "the Phase A catalogue must be readable or these isolation checks are vacuous"
    table, _ = load_cn_contracts(CONTRACTS)
    return [c["symbol"] for c in catalogue["candidates"] if c["symbol"] not in table]


# ---------- P1-1: no active symbol may run on an invented specification ----------
def test_unlisted_symbols_abort_before_any_market_or_model_activity(tmp_path, monkeypatch):
    missing = ["ZZ0", "AA0"]
    cfg = make_cfg(tmp_path, ["RB0"] + missing)
    db = Database(cfg.db_path)

    def never(*a, **kw):
        raise AssertionError("no market source may be constructed for an unusable configuration")

    monkeypatch.setattr("arena.engine.loop.ReplaySource", never)
    monkeypatch.setattr("arena.engine.loop.SinaFuturesSource", never)
    monkeypatch.setattr("arena.engine.loop.CryptoSource", never)
    monkeypatch.setattr(db, "create_competition", never)

    with pytest.raises(ContractSpecError) as err:
        Engine(cfg, db, accelerate=True)

    assert err.value.symbols == sorted(missing), "every offending symbol must be reported at once"
    assert all(err.value.problems[sym] == ("not listed in the contract table",) for sym in missing)
    assert "RB0" not in err.value.problems, "a symbol with valid terms is not an offender"
    assert str(cfg.resolve("config/contracts_cn.json")) in str(err.value), "the message must name the table"
    assert db.active_competition() is None and db.list_competitions() == [], "nothing may be written before the refusal"
    db.close()


@pytest.mark.parametrize("field", ["multiplier", "tick", "margin_ratio", "sessions"])
def test_an_omitted_execution_term_is_not_defaulted(tmp_path, field):
    """The loader used to invent a value for every omitted term; now it refuses."""
    contracts = contracts_copy(tmp_path)
    drop_term(contracts, "RB0", field)
    db = Database(make_cfg(tmp_path, ["RB0"], str(contracts)).db_path)
    with pytest.raises(ContractSpecError) as err:
        Engine(make_cfg(tmp_path, ["RB0"], str(contracts)), db, accelerate=True)
    assert err.value.symbols == ["RB0"]
    assert any(field in issue for issue in err.value.problems["RB0"])
    assert db.list_competitions() == []
    db.close()


def test_a_row_without_any_fee_term_is_refused(tmp_path):
    """Zero fees are fine, but they have to be written down rather than assumed."""
    row = {k: v for k, v in VALID_ROW.items() if k not in ("fee_per_lot", "fee_rate")}
    path = write_table(tmp_path, {"RB0": row})
    with pytest.raises(ContractSpecError) as err:
        validate_active_cn_contracts(["RB0"], path)
    assert any("fee" in issue for issue in err.value.problems["RB0"])
    validate_active_cn_contracts(["RB0"], write_table(tmp_path, {"RB0": {**row, "fee_rate": 0}}, "explicit.json"))


@pytest.mark.parametrize("sessions", [
    [],                                     # empty would read as always open
    "09:00-15:00",                          # not a list of windows
    [["09:00"]],                            # not a pair
    [["09:00", "08:00"]],                   # ends before it starts
    [["09:00", "09:00"]],                   # zero length
    [["9am", "15:00"]],                     # unparseable
    [["09:00", "72:00"]],                   # beyond two days
    [[900, 1500]],                          # not HH:MM strings
])
def test_structurally_invalid_sessions_are_refused(tmp_path, sessions):
    path = write_table(tmp_path, {"RB0": {**VALID_ROW, "sessions": sessions}})
    with pytest.raises(ContractSpecError) as err:
        validate_active_cn_contracts(["RB0"], path)
    assert any("sessions" in issue for issue in err.value.problems["RB0"])


@pytest.mark.parametrize("field,value", [
    ("multiplier", 0), ("multiplier", -10), ("multiplier", "10"), ("multiplier", None),
    ("tick", 0), ("tick", -1), ("tick", True),
    ("margin_ratio", 0), ("margin_ratio", -0.1),
    ("fee_per_lot", -1), ("fee_rate", -0.0001), ("fee_rate", "cheap"),
    ("lot_step", 5),
])
def test_invalid_execution_values_are_refused(tmp_path, field, value):
    path = write_table(tmp_path, {"RB0": {**VALID_ROW, field: value}})
    with pytest.raises(ContractSpecError) as err:
        validate_active_cn_contracts(["RB0"], path)
    assert any(field in issue for issue in err.value.problems["RB0"])


def test_overnight_sessions_past_midnight_stay_valid(tmp_path):
    """21:00..26:30 closes at 02:30 the next day and must not be mistaken for invalid."""
    path = write_table(tmp_path, {"AU0": {**VALID_ROW, "sessions": [["09:00", "10:15"], ["21:00", "26:30"]]}})
    validate_active_cn_contracts(["AU0"], path)


def test_every_invalid_symbol_and_field_is_reported_once(tmp_path):
    path = write_table(tmp_path, {
        "RB0": VALID_ROW,
        "I0": {**VALID_ROW, "multiplier": -1, "sessions": []},
        "AU0": {k: v for k, v in VALID_ROW.items() if k != "tick"},
    })
    with pytest.raises(ContractSpecError) as err:
        validate_active_cn_contracts(["RB0", "I0", "AU0", "NOPE0"], path)
    assert err.value.symbols == ["AU0", "I0", "NOPE0"], "deterministic, sorted, and complete"
    assert any("multiplier" in i for i in err.value.problems["I0"])
    assert any("sessions" in i for i in err.value.problems["I0"])
    assert any("tick" in i for i in err.value.problems["AU0"])
    assert err.value.problems["NOPE0"] == ("not listed in the contract table",)


def test_an_unreadable_table_fails_closed(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractSpecError) as err:
        validate_active_cn_contracts(["RB0"], path)
    assert err.value.symbols == ["RB0"]


def test_shipped_table_validates_for_every_listed_symbol():
    table, _ = load_cn_contracts(CONTRACTS)
    validate_active_cn_contracts(sorted(table), CONTRACTS)


def test_preflight_runs_before_any_database_is_created(tmp_path):
    """The CLI gate must refuse before the lock or the schema exist."""
    contracts = contracts_copy(tmp_path)
    drop_term(contracts, "RB0", "multiplier")
    db_path = tmp_path / "never-created.db"
    with pytest.raises(ContractSpecError):
        preflight_contract_specs("replay", ["RB0"], contracts)
    assert not db_path.exists()
    # A healthy configuration passes the same gate, and crypto has no local table.
    preflight_contract_specs("replay", ["RB0"], CONTRACTS)
    preflight_contract_specs("crypto", ["BTC-USDT-SWAP"], tmp_path / "absent.json")


def test_run_py_preflights_before_the_instance_lock(tmp_path, monkeypatch, capsys):
    """run.py must not take the lock or open the database for a bad configuration."""
    import run as run_module

    contracts = contracts_copy(tmp_path)
    drop_term(contracts, "RB0", "sessions")
    cfg = make_cfg(tmp_path, ["RB0"], str(contracts), db_name="cli.db")

    def never(*a, **kw):
        raise AssertionError("must not be reached before the contract preflight")

    monkeypatch.setattr(run_module, "app_base_dir", lambda: ROOT)
    monkeypatch.setattr("os.chdir", lambda *_: None)
    monkeypatch.setattr("arena.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr("arena.desktop.acquire_instance_lock", never)
    monkeypatch.setattr("arena.db.Database", never)
    monkeypatch.setattr(run_module, "setup_logging", never)

    assert run_module.main(["--headless"]) == 4
    assert not (tmp_path / "cli.db").exists()
    assert "RB0" in capsys.readouterr().err


def test_an_invalid_active_row_can_never_open_through_risk_or_matching(tmp_path):
    """Startup refuses, so no spec exists for the symbol on either boundary."""
    contracts = contracts_copy(tmp_path)
    edit_term(contracts, "I0", multiplier=0)
    db = Database(make_cfg(tmp_path, ["RB0", "I0"], str(contracts)).db_path)
    with pytest.raises(ContractSpecError):
        Engine(make_cfg(tmp_path, ["RB0", "I0"], str(contracts)), db, accelerate=True)
    db.close()

    # The valid subset still starts, and the rejected symbol is absent everywhere.
    db = Database(make_cfg(tmp_path, ["RB0"], str(contracts), db_name="valid.db").db_path)
    eng = Engine(make_cfg(tmp_path, ["RB0"], str(contracts), db_name="valid.db"), db, accelerate=True)
    try:
        assert "I0" not in eng.exchange.specs and "I0" not in eng.risk.specs
        verdict = eng.risk.check(Account(trader_id="t", cash=100_000, initial_cash=100_000),
                                 ActionRequest(symbol="I0", action="open_long", size_pct=0.1), 800.0, {"I0": 800.0})
        assert not verdict.ok, "risk must not size an order for a symbol with no validated spec"
        ex = SimExchange(eng.specs, slippage_ticks=0)
        assert "I0" not in ex.specs
    finally:
        eng.stop()
        db.close()


def test_open_admission_defaults_to_refused():
    """The default must be outside the admitted set; nobody has to opt in to fail closed."""
    assert SPEC_ORIGIN_UNSPECIFIED not in OPEN_ADMITTED_SPEC_ORIGINS
    assert ContractSpec(symbol="X", name="X").origin == SPEC_ORIGIN_UNSPECIFIED
    assert not ContractSpec(symbol="X", name="X").admits_new_positions


def test_a_spec_restored_from_a_pre_origin_journal_is_not_admitted(tmp_path):
    """A journal written before the field existed must not be readmitted for opens."""
    from dataclasses import asdict

    table, _ = load_cn_contracts(CONTRACTS)
    stored = asdict(table["RB0"])
    stored.pop("origin")                       # as an older journal entry would look
    restored = ContractSpec(**stored)
    assert not restored.admits_new_positions, "recovery may replay a fill, but must not open a new one"
    acc = Account(trader_id="t", cash=1_000_000, initial_cash=1_000_000)
    assert not SimExchange({"RB0": restored}, slippage_ticks=0).open(
        acc, restored, "long", 1, 3400.0, 1.0, None, None, {"RB0": 3400.0}, round_no=1).accepted


def test_an_engine_built_spec_is_admitted_end_to_end(tmp_path):
    """Positive path: a validated active row opens through both risk and matching."""
    cfg = make_cfg(tmp_path, ["RB0"])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        spec = eng.specs["RB0"]
        assert spec.origin == SPEC_ORIGIN_LOCAL_TABLE and spec.admits_new_positions
        acc = Account(trader_id="t", cash=1_000_000, initial_cash=1_000_000)
        verdict = eng.risk.check(acc, ActionRequest(symbol="RB0", action="open_long", size_pct=0.1),
                                 3400.0, {"RB0": 3400.0}, atr=20.0)
        assert verdict.ok
        assert eng.exchange.open(acc, spec, "long", 1, 3400.0, 1.0, None, None,
                                 {"RB0": 3400.0}, round_no=1).accepted
    finally:
        eng.stop()
        db.close()


def test_load_active_cn_contracts_returns_the_full_table_after_validating(tmp_path):
    specs, holidays = load_active_cn_contracts(["RB0"], CONTRACTS)
    table, table_holidays = load_cn_contracts(CONTRACTS)
    assert set(specs) == set(table) and holidays == table_holidays, "non-active rows stay available for recovery"
    assert specs["RB0"] == table["RB0"]


# ---------- P1-2 / P1-3: competition identity ----------
def test_snapshot_records_the_active_universe_and_terms(tmp_path):
    cfg = make_cfg(tmp_path, ["RB0", "I0"])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        recorded = json.loads(db.active_competition()["config_json"])[CONTRACT_UNIVERSE_KEY]
        assert recorded["symbols"] == ["RB0", "I0"], "the configured order is recorded, not a sorted copy"
        assert recorded["terms"] == contract_terms_digest(eng.specs)
    finally:
        eng.stop()
        db.close()


def test_reordering_the_symbols_starts_a_new_competition(tmp_path):
    """Order is model-visible: the source, the snapshot and the prompt all iterate it."""
    db = Database(make_cfg(tmp_path, ["RB0", "I0"]).db_path)
    first = Engine(make_cfg(tmp_path, ["RB0", "I0"]), db, accelerate=True)
    first_id = first.comp_id
    first.stop()
    second = Engine(make_cfg(tmp_path, ["I0", "RB0"]), db, accelerate=True)
    try:
        assert second.comp_id != first_id
        assert {row["id"]: row for row in db.list_competitions()}[first_id]["status"] == "ended"
    finally:
        second.stop()
        db.close()


def test_configured_order_is_what_the_model_actually_sees(tmp_path):
    """Justifies order-sensitive identity rather than canonicalising every path."""
    from arena.traders.prompt import build_prompt

    cfg = make_cfg(tmp_path, ["I0", "RB0"])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        eng._tick()
        snap = eng._build_snapshot()
        assert list(eng.source.symbols) == ["I0", "RB0"]
        assert list(snap.symbols) == ["I0", "RB0"], "the decision snapshot keeps the configured order"
        prompt = build_prompt(snap, eng._account_view("ok", snap), eng.rules)
        assert prompt.index("I0") < prompt.index("RB0"), "so the model text differs when the order differs"
    finally:
        eng.stop()
        db.close()


@pytest.mark.parametrize("changed", [["RB0"], ["RB0", "I0", "AU0"], ["RB0", "AU0"]])
def test_changing_the_active_universe_starts_a_new_competition(tmp_path, changed):
    db = Database(make_cfg(tmp_path, ["RB0", "I0"]).db_path)
    first = Engine(make_cfg(tmp_path, ["RB0", "I0"]), db, accelerate=True)
    first_id = first.comp_id
    first.stop()
    second = Engine(make_cfg(tmp_path, changed), db, accelerate=True)
    try:
        assert second.comp_id != first_id, "a different universe must not extend the old leaderboard"
        assert {row["id"]: row for row in db.list_competitions()}[first_id]["status"] == "ended"
    finally:
        second.stop()
        db.close()


@pytest.mark.parametrize("term", [{"multiplier": 100}, {"tick": 5}, {"margin_ratio": 0.2},
                                  {"fee_per_lot": 7}, {"fee_rate": 0.001},
                                  {"sessions": [["09:00", "11:30"]]}])
def test_editing_an_active_contract_term_starts_a_new_competition(tmp_path, term):
    contracts = contracts_copy(tmp_path)
    db = Database(make_cfg(tmp_path, ["RB0"], str(contracts)).db_path)
    first = Engine(make_cfg(tmp_path, ["RB0"], str(contracts)), db, accelerate=True)
    first_id = first.comp_id
    first.stop()
    edit_term(contracts, "RB0", **term)   # same path, different content
    second = Engine(make_cfg(tmp_path, ["RB0"], str(contracts)), db, accelerate=True)
    try:
        assert second.comp_id != first_id, f"{term} changes matching semantics"
        assert {row["id"]: row for row in db.list_competitions()}[first_id]["status"] == "ended"
    finally:
        second.stop()
        db.close()


def test_editing_an_inactive_contract_keeps_the_current_competition(tmp_path):
    """The digest covers the symbols in force; a bystander row must not void a leaderboard."""
    contracts = contracts_copy(tmp_path)
    db = Database(make_cfg(tmp_path, ["RB0"], str(contracts)).db_path)
    first = Engine(make_cfg(tmp_path, ["RB0"], str(contracts)), db, accelerate=True)
    first_id = first.comp_id
    first.stop()
    edit_term(contracts, "AU0", multiplier=999)
    second = Engine(make_cfg(tmp_path, ["RB0"], str(contracts)), db, accelerate=True)
    try:
        assert second.comp_id == first_id
    finally:
        second.stop()
        db.close()


def test_renaming_a_contract_keeps_the_current_competition(tmp_path):
    """name and exchange are display-only and stay out of the digest."""
    contracts = contracts_copy(tmp_path)
    db = Database(make_cfg(tmp_path, ["RB0"], str(contracts)).db_path)
    first = Engine(make_cfg(tmp_path, ["RB0"], str(contracts)), db, accelerate=True)
    first_id = first.comp_id
    first.stop()
    edit_term(contracts, "RB0", name="Rebar (front month)")
    second = Engine(make_cfg(tmp_path, ["RB0"], str(contracts)), db, accelerate=True)
    try:
        assert second.comp_id == first_id
    finally:
        second.stop()
        db.close()


def test_terms_digest_is_stable_and_order_independent():
    table, _ = load_cn_contracts(CONTRACTS)
    a = {s: table[s] for s in ("RB0", "I0", "AU0")}
    b = {s: table[s] for s in ("AU0", "RB0", "I0")}
    assert contract_terms_digest(a) == contract_terms_digest(b) == contract_terms_digest(a)
    assert contract_terms_digest(a) != contract_terms_digest({s: table[s] for s in ("RB0", "I0")})
    # The universe signature keeps order even though the digest does not.
    assert contract_universe_signature(["RB0", "I0"], a) != contract_universe_signature(["I0", "RB0"], a)


def strip_identity(db, cid):
    snapshot = json.loads(next(r for r in db.list_competitions() if r["id"] == cid)["config_json"])
    snapshot.pop(CONTRACT_UNIVERSE_KEY)
    db._exec("UPDATE competitions SET config_json=? WHERE id=?", (json.dumps(snapshot), cid))


@pytest.mark.parametrize("second_symbols", [["RB0", "I0"], ["RB0"]])
def test_a_snapshot_without_an_identity_block_never_silently_resumes(tmp_path, second_symbols):
    """Unchanged or changed, a fingerprint-less competition cannot prove equivalence."""
    cfg = make_cfg(tmp_path, ["RB0", "I0"])
    db = Database(cfg.db_path)
    first = Engine(cfg, db, accelerate=True)
    first_id = first.comp_id
    first.stop()
    strip_identity(db, first_id)

    second = Engine(make_cfg(tmp_path, second_symbols), db, accelerate=True)
    try:
        assert second.comp_id != first_id, "an unprovable identity is a rollover, not an assumed match"
        rows = {row["id"]: row for row in db.list_competitions()}
        assert rows[first_id]["status"] == "ended", "the old competition is preserved, not discarded"
        assert CONTRACT_UNIVERSE_KEY in json.loads(db.active_competition()["config_json"])
        assert any("未记录" in e["message"] for e in db.events(first_id, 50)), "the reason is recorded on the old row"
    finally:
        second.stop()
        db.close()


def test_a_legacy_rollover_replays_pending_marks_before_archiving(tmp_path):
    """Journal recovery must complete before the old competition is archived."""
    from tests.test_regression32 import leave_pending_stop, make_cfg as r32_cfg, stop_trades

    cid, qty, journal = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    strip_identity(db, cid)
    eng = Engine(r32_cfg(tmp_path), db, accelerate=True)
    try:
        assert eng.comp_id != cid, "a fingerprint-less competition rolls over"
        settled = stop_trades(db, cid)
        assert [t["action"] for t in settled] == ["stop_loss"] and settled[0]["qty"] == qty, \
            "the journaled trigger is replayed at its original price before archiving"
        assert not journal.exists() and db.load_accounts(cid)["ok"].positions == {}
        assert {row["id"]: row for row in db.list_competitions()}[cid]["status"] == "ended"
    finally:
        eng.stop()
        db.close()


def test_a_legacy_rollover_is_refused_while_a_trigger_cannot_be_settled(tmp_path):
    """An unwritable database blocks the rollover instead of losing the fill."""
    from arena.engine.loop import RolloverBlocked
    from tests.test_regression32 import leave_pending_stop, make_cfg as r32_cfg

    cid, qty, journal = leave_pending_stop(tmp_path)
    db = Database(str(tmp_path / "e.db"))
    strip_identity(db, cid)

    def broken(*a, **kw):
        raise sqlite3.OperationalError("database is locked (simulated)")

    db.save_account = broken   # type: ignore[method-assign]
    with pytest.raises(RolloverBlocked):
        Engine(r32_cfg(tmp_path), db, accelerate=True)
    assert {row["id"]: row for row in db.list_competitions()}[cid]["status"] == "running"
    assert journal.exists(), "the pending trigger is still on disk for the next attempt"
    db.close()


# ---------- the Phase A catalogue stays out of the execution path ----------
def test_catalogue_is_larger_than_the_tradable_table():
    assert candidate_only_symbols(), "the catalogue must list codes with no local terms, or these checks are vacuous"


def test_candidate_only_symbols_never_reach_engine_exchange_or_risk_specs(tmp_path):
    cfg = make_cfg(tmp_path, DEMO_SYMBOLS)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        only = set(candidate_only_symbols())
        for specs in (eng.specs, eng.exchange.specs, eng.risk.specs, eng._all_specs):
            assert not (only & set(specs)), "a catalogue code must not appear in any execution-facing spec table"
        assert set(eng.source.symbols) == set(DEMO_SYMBOLS), "the source subscribes to active symbols only"
        eng._tick()
        assert not (only & set(eng._build_snapshot().symbols)), "a catalogue code must not reach the prompt"
    finally:
        eng.stop()
        db.close()


def test_candidate_only_symbols_are_never_fetched(tmp_path):
    cfg = make_cfg(tmp_path, DEMO_SYMBOLS)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        asked = []
        real_latest, real_klines = eng.source.latest, eng.source.klines
        real_snapshot = eng.source.latest_with_snapshot   # the replay source ticks through this one
        eng.source.latest = lambda syms, **kw: (asked.extend(syms), real_latest(syms, **kw))[1]
        eng.source.latest_with_snapshot = lambda syms, **kw: (asked.extend(syms), real_snapshot(syms, **kw))[1]
        eng.source.klines = lambda sym, *a, **kw: (asked.append(sym), real_klines(sym, *a, **kw))[1]
        eng._tick()
        eng._build_snapshot()
        assert asked, "this test must observe real fetches or it proves nothing"
        assert set(asked) <= set(DEMO_SYMBOLS), f"catalogue-only codes were fetched: {sorted(set(asked) - set(DEMO_SYMBOLS))}"
    finally:
        eng.stop()
        db.close()


def test_configuring_a_candidate_only_symbol_still_fails_closed(tmp_path):
    """Being in the catalogue is not permission to trade: local terms are still required."""
    candidate = candidate_only_symbols()[0]
    cfg = make_cfg(tmp_path, ["RB0", candidate])
    db = Database(cfg.db_path)
    with pytest.raises(ContractSpecError) as err:
        Engine(cfg, db, accelerate=True)
    assert err.value.symbols == [candidate]
    assert db.list_competitions() == []
    db.close()


def test_catalogue_carries_no_matching_parameters():
    """The digest can only come from the contract table; the catalogue holds no terms."""
    for entry in load_catalogue(CATALOGUE)["candidates"]:
        assert not ({"multiplier", "tick", "margin_ratio", "fee_rate", "fee_per_lot", "sessions"} & set(entry))


# ---------- normal startup is untouched ----------
def test_repository_demo_config_still_starts_and_runs(tmp_path):
    """The shipped demo configuration must start and run (storage redirected off data/ and logs/)."""
    cfg = load_config(ROOT / "config" / "demo.yaml", base_dir=ROOT)
    cfg.storage.db_path = str(tmp_path / "demo.db")
    cfg.storage.log_dir = str(tmp_path / "logs")
    assert cfg.symbols == DEMO_SYMBOLS
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        assert set(eng.specs) == set(DEMO_SYMBOLS)
        eng._tick()
        eng._round()
        eng._snapshot()
        assert eng.comp_id and set(eng.accounts) == {t.id for t in cfg.traders}
        assert all(acc.equity(eng.prices, eng.specs) > 0 for acc in eng.accounts.values())
    finally:
        eng.stop()
        db.close()


def test_demo_symbols_keep_the_normal_startup_path(tmp_path):
    cfg = make_cfg(tmp_path, DEMO_SYMBOLS)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        table, _ = load_cn_contracts(CONTRACTS)
        assert set(eng.specs) == set(DEMO_SYMBOLS)
        for sym in DEMO_SYMBOLS:
            assert eng.specs[sym] == table[sym], "an active symbol uses the table's terms field for field"
            assert eng.specs[sym].origin == SPEC_ORIGIN_LOCAL_TABLE and eng.specs[sym].admits_new_positions
        assert db.active_competition()["id"] == eng.comp_id
        eng._tick()
        assert set(eng.prices) == set(DEMO_SYMBOLS)
    finally:
        eng.stop()
        db.close()


def test_recovery_fallback_is_marked_and_kept_out_of_the_active_table(tmp_path):
    """The fallback exists only for a leftover position, and must declare its origin."""
    cfg = make_cfg(tmp_path, DEMO_SYMBOLS)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        assert all(spec.admits_new_positions for spec in eng.specs.values())
        leftover = eng._spec_for("ZZ0")
        assert leftover.is_recovery_fallback and not leftover.admits_new_positions
        assert "ZZ0" not in eng.exchange.specs and "ZZ0" not in eng.risk.specs and "ZZ0" not in eng._all_specs
        assert eng.risk.specs.get("ZZ0") is None, "risk uses get(); it must not conjure a spec via _SpecMap"
        assert not RiskManager(RiskCfg(), {"ZZ0": leftover}).check(
            Account(trader_id="t", cash=100_000, initial_cash=100_000),
            ActionRequest(symbol="ZZ0", action="open_long", size_pct=0.1), 1000.0, {"ZZ0": 1000.0}).ok
    finally:
        eng.stop()
        db.close()
