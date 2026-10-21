import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arena.engine.loop import CONTRACT_UNIVERSE_KEY, contract_universe_signature  # noqa: E402
from arena.exchange.contracts import load_cn_contracts  # noqa: E402
from arena.models import Account  # noqa: E402


def _competition_snapshot(db, cid: int) -> dict:
    row = next(r for r in db.list_competitions() if r["id"] == cid)
    return json.loads(row["config_json"] or "{}")


def _store_competition_snapshot(db, cid: int, snapshot: dict) -> None:
    db._exec("UPDATE competitions SET config_json=? WHERE id=?",
             (json.dumps(snapshot, ensure_ascii=False), cid))


def strip_competition_universe(db, cid: int) -> None:
    """Remove the contract-universe identity block, as a pre-fingerprint row would lack it."""
    snapshot = _competition_snapshot(db, cid)
    snapshot.pop(CONTRACT_UNIVERSE_KEY, None)
    _store_competition_snapshot(db, cid, snapshot)


def restamp_competition_universe(db, cid: int, symbols, contracts_file=None) -> None:
    """Record the identity the next engine will compute, so the competition resumes.

    Recovery paths such as _reconcile only run for a competition the engine
    resumes.  A test that drops a symbol from the configuration would otherwise
    trigger a correct rollover and never reach them, so it hands the stored
    snapshot the identity the new configuration produces while the accounts still
    carry the older position.
    """
    table, _ = load_cn_contracts(contracts_file or (ROOT / "config" / "contracts_cn.json"))
    snapshot = _competition_snapshot(db, cid)
    snapshot[CONTRACT_UNIVERSE_KEY] = contract_universe_signature(symbols, {s: table[s] for s in symbols})
    _store_competition_snapshot(db, cid, snapshot)


@pytest.fixture
def cn_specs():
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return specs


@pytest.fixture
def account():
    return Account(trader_id="t1", cash=1_000_000, initial_cash=1_000_000)
