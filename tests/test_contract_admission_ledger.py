"""B1a + B1b-P0: the non-executable admission ledger and its provenance gate.

B0 proved a *local structural* property -- an active symbol may not run unless
the contract table states every execution term explicitly.  This layer sits
before that gate and proves a different thing: that the evidence behind a
candidate's proposed terms can be recorded, reviewed and reported without any
of it becoming executable or claiming exchange verification.

Five families of check live here:
  * schema and identity -- versioned document, deterministic fingerprint, and a
    link to the exact catalogue snapshot the ledger was prepared against *and*
    the exact host policy its citations were judged under;
  * refusals -- mismatch, duplicate/unknown symbols, tampered fingerprint,
    execution terms, malformed URLs/dates/numbers, an unearned `ready_for_review`
    and a review state claimed without its citation;
  * provenance (B1b) -- a citation has to name a first-party host of the venue the
    catalogue says lists the candidate, published by the exchange itself, in a
    kind of document that can actually establish that term.  `http`, a lookalike
    host, another venue's host, a port, credentials, a wrong document kind and a
    margin/fee with no effective date are all refused, offline;
  * projection -- coverage and state reach the dashboard; proposed values, source
    URLs, document kinds and the policy's host list do not;
  * isolation -- ledger codes never reach engine, exchange, risk, market, trader
    or prompt paths, and nothing on those paths imports either module.

Every value in this file is a synthetic fixture on invented codes and invented
hosts.  Nothing here is, or is meant to read as, a real contract term or a real
citation.  The only place the committed production policy is used is a direct
host-matching check -- an attacker's lookalike refused, a listed host accepted --
and neither URL ever enters a ledger.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from arena.admission import (BINDING_FIELDS, CITATION_METADATA_FIELDS, DATED_TERMS,
                             DEFAULT_LEDGER_FILE, EVIDENCE_TERMS,
                             FINGERPRINT_FIELD, LEDGER_BINDING_FIELDS, LEDGER_KIND,
                             LEDGER_SCHEMA_VERSION, MIGRATABLE_SCHEMA_VERSIONS,
                             PROJECTION_FIELDS, PROJECTION_RECORD_FIELDS,
                             REVIEW_CROSS_CHECKED, REVIEW_RECORDED, REVIEW_UNREVIEWED,
                             STATE_BLOCKED, STATE_PENDING, STATE_READY_FOR_REVIEW,
                             V1_EVIDENCE_FIELDS, V1_LEDGER_FIELDS,
                             DISCLAIMER as LEDGER_DISCLAIMER, LedgerError, binding_mismatches,
                             build_ledger, catalogue_binding, ledger_fingerprint,
                             legacy_ledger_problems, load_ledger,
                             load_ledger_projection, normalized_catalogue_path, pending_record,
                             prepared_records, project_ledger, read_ledger, rebind_refusals,
                             schema_migration_refusals, validate_ledger)
from arena.admission_source_policy import (DEFAULT_SOURCE_POLICY_FILE, KIND_CONTRACT,
                                           KIND_FEE_RULE, KIND_MARGIN_RULE, KIND_SESSION_NOTICE,
                                           POLICY_BINDING_FIELDS, POLICY_FIELDS,
                                           POLICY_FINGERPRINT_FIELD, POLICY_KIND,
                                           POLICY_SCHEMA_VERSION,
                                           SOURCE_AUTHORITY_EXCHANGE_PRIMARY, SOURCE_KINDS,
                                           TERM_FEE, TERM_MARGIN, TERM_MULTIPLIER, TERM_SESSIONS,
                                           TERM_SOURCE_KINDS, TERM_TICK, SourcePolicyError,
                                           citation_problem, load_policy, load_policy_or_none,
                                           numeric_host, numeric_host_problem, official_hosts,
                                           policy_binding, policy_binding_mismatches,
                                           policy_fingerprint, read_policy, source_problem,
                                           transport_problem, validate_policy, venue_problem)
from arena.db import Database
from arena.engine.loop import Engine
from arena.exchange.contracts import ContractSpecError, load_cn_contracts
from arena.server.api import create_app
from arena.universe import (DEFAULT_UNIVERSE_FILE, FORBIDDEN_SPEC_KEYS, content_hash,
                            forbidden_keys_in, load_catalogue, sanitize_catalogue)

from tests.test_contract_universe_b0 import DEMO_SYMBOLS, make_cfg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_admission_ledger as builder  # noqa: E402

CONTRACTS = ROOT / "config" / "contracts_cn.json"
CATALOGUE = ROOT / DEFAULT_UNIVERSE_FILE
LEDGER = ROOT / DEFAULT_LEDGER_FILE
POLICY = ROOT / DEFAULT_SOURCE_POLICY_FILE

# Invented codes on an invented venue, citing invented hosts: this file must never
# be mistakable for a record of anybody's real contract terms or real sources.
FIXTURE_SYMBOLS = ("XA0", "XB0")
FIXTURE_EXCHANGE = "XCHG"
FIXTURE_HOSTS = ("rules.example.com", "example.com")
SOURCE_URL = f"https://{FIXTURE_HOSTS[0]}/xchg/xa-contract"
SOURCE_TITLE = "Example venue contract rules (synthetic test fixture)"

# The bindings whose value is a path or a digest, and therefore distinctive enough
# that searching a response body for it means something.  The other two --
# ``catalogue_schema_version`` and ``catalogue_candidate_count`` -- are small
# integers whose digits occur in any coverage count, so they are proved absent by
# key rather than by substring.
OPAQUE_BINDINGS = ("prepared_for", "catalogue_content_hash", "source_policy_path",
                   "source_policy_fingerprint", FINGERPRINT_FIELD)


# ----------------------------------------------------------------- fixtures --

def candidate(symbol: str, exchange: str = FIXTURE_EXCHANGE) -> dict:
    return {"continuous_symbol": symbol, "name": f"{symbol} test contract", "exchange": exchange,
            "source_group": exchange, "source_node": "test_node", "quote_ok": True,
            "history_5m_ok": True, "history_15m_ok": True, "history_1h_ok": True,
            "probe_status": "ok", "probe_error": "", "probed_at": "2026-09-19T10:00:00+08:00"}


def catalogue_doc(symbols=FIXTURE_SYMBOLS, schema_version: int = 1,
                  exchange: str = FIXTURE_EXCHANGE) -> dict:
    candidates = {s: candidate(s, exchange) for s in symbols}
    return {"schema_version": schema_version, "source": "test", "source_url": "https://example.com/list.js",
            "note": "test", "fetched_at": "2026-09-19T10:00:00+08:00", "snapshot_status": "complete",
            "content_hash": content_hash(candidates), "count": len(candidates), "candidates": candidates}


def catalogue_view(symbols=FIXTURE_SYMBOLS, schema_version: int = 1,
                   exchange: str = FIXTURE_EXCHANGE) -> dict:
    """The sanitised catalogue the projection anchors on."""
    return sanitize_catalogue(catalogue_doc(symbols, schema_version, exchange))


def policy_doc(hosts=None, **over) -> dict:
    """A synthetic host policy, stamped exactly the way the committed one is.

    The default lists the invented venue the fixture catalogue uses, so a positive
    citation can be built without naming any real exchange's domain.
    """
    doc = {"schema_version": POLICY_SCHEMA_VERSION, "kind": POLICY_KIND,
           "note": "synthetic source policy for tests; no real host, no real term",
           "exchange_hosts": dict(hosts if hosts is not None else {FIXTURE_EXCHANGE: list(FIXTURE_HOSTS)})}
    doc.update(over)
    doc[POLICY_FINGERPRINT_FIELD] = policy_fingerprint(doc)
    return doc


def evidence(proposed, term: str = TERM_TICK, **over) -> dict:
    """One fully cited, policy-clean evidence slot for ``term``.

    The document kind is the first the matrix allows for that term, and margin /
    fee carry an effective date because for those two a publication date alone is
    not a citation.
    """
    entry = {"value": proposed, "source_url": SOURCE_URL, "source_title": SOURCE_TITLE,
             "published_date": "2026-01-15",
             "effective_date": "2026-01-20" if term in DATED_TERMS else "",
             "review_state": REVIEW_RECORDED, "recorded_by": "reviewer-a",
             "source_authority": SOURCE_AUTHORITY_EXCHANGE_PRIMARY,
             "source_kind": TERM_SOURCE_KINDS[term][0]}
    entry.update(over)
    return entry


def blank_evidence(**over) -> dict:
    """A slot that was created and never filled: value null, every field empty.

    Not a valid record -- ``None`` is the only way to say a term is unresearched --
    so it exists here to be refused, and as the base for the partial drafts below.
    """
    entry = {"value": None, "source_url": "", "source_title": "", "published_date": "",
             "effective_date": "", "review_state": REVIEW_UNREVIEWED, "recorded_by": "",
             "source_authority": "", "source_kind": ""}
    entry.update(over)
    return entry


def full_evidence(**over) -> dict:
    complete = {TERM_MULTIPLIER: evidence(10, TERM_MULTIPLIER),
                TERM_TICK: evidence(1, TERM_TICK),
                TERM_SESSIONS: evidence([["09:00", "10:15"], ["21:00", "26:30"]], TERM_SESSIONS),
                TERM_MARGIN: evidence(0.13, TERM_MARGIN),
                TERM_FEE: evidence({"per_lot": 0, "rate": 0.0001}, TERM_FEE)}
    complete.update(over)
    return complete


def record(code: str = "XA0", **over) -> dict:
    row = pending_record(code)
    row.update(over)
    return row


def reviewable(code: str = "XA0", **over) -> dict:
    fields = {"admission_state": STATE_READY_FOR_REVIEW, "evidence": full_evidence(),
              "prepared_by": "reviewer-a", "prepared_at": "2026-09-19"}
    fields.update(over)
    return record(code, **fields)


def stamped(entries, catalogue=None, catalogue_path=DEFAULT_UNIVERSE_FILE, policy=None,
            policy_path=DEFAULT_SOURCE_POLICY_FILE, **over) -> dict:
    """A self-consistent ledger: overrides applied first, then the fingerprint stamped.

    Stamping last is what makes the negative cases meaningful -- each one has to
    be refused on its own merits rather than incidentally tripping the digest.
    A document that is *edited after* stamping is built with ``tampered``.
    """
    doc = {"schema_version": LEDGER_SCHEMA_VERSION, "kind": LEDGER_KIND, "note": LEDGER_DISCLAIMER,
           "catalogue_fetched_at": "2026-09-19T10:00:00+08:00", "prepared_at": "2026-09-19",
           "count": len(entries), "records": entries,
           **catalogue_binding(catalogue or catalogue_view(), catalogue_path),
           **policy_binding(policy or policy_doc(), policy_path)}
    doc.update(over)
    doc[FINGERPRINT_FIELD] = ledger_fingerprint(doc)
    return doc


def checked(doc, catalogue=None, catalogue_path=DEFAULT_UNIVERSE_FILE, policy=None,
            policy_path=DEFAULT_SOURCE_POLICY_FILE) -> None:
    """Run the full gate: both bindings supplied, so provenance is actually proved."""
    validate_ledger(doc, catalogue=catalogue if catalogue is not None else catalogue_view(),
                    catalogue_path=catalogue_path,
                    policy=policy if policy is not None else policy_doc(),
                    policy_path=policy_path)


def projected(doc, catalogue=None, catalogue_path=DEFAULT_UNIVERSE_FILE, policy=None,
              policy_path=DEFAULT_SOURCE_POLICY_FILE) -> dict:
    """The public view under the full gate, for the same reason as ``checked``."""
    return project_ledger(doc, catalogue if catalogue is not None else catalogue_view(),
                          catalogue_path,
                          policy if policy is not None else policy_doc(), policy_path)


def tampered(doc: dict, **over) -> dict:
    """Edit a stamped ledger without re-stamping it, as a hand edit would."""
    edited = json.loads(json.dumps(doc, ensure_ascii=False))
    edited.update(over)
    return edited


def two_pending() -> dict:
    return {s: record(s) for s in FIXTURE_SYMBOLS}


# ------------------------------------------------- schema, identity, build --

def test_fingerprint_is_deterministic_and_independent_of_insertion_order():
    """Same facts, different mapping order -- one ledger, one digest."""
    forward = stamped({s: record(s) for s in ("XA0", "XB0")})
    backward = stamped({s: record(s) for s in ("XB0", "XA0")})
    assert forward[FINGERPRINT_FIELD] == backward[FINGERPRINT_FIELD]
    shuffled_header = {key: forward[key] for key in reversed(list(forward))}
    assert ledger_fingerprint(shuffled_header) == ledger_fingerprint(forward)
    changed = stamped({"XA0": record("XA0", notes="researched"), "XB0": record("XB0")})
    assert changed[FINGERPRINT_FIELD] != forward[FINGERPRINT_FIELD]


def test_the_fingerprint_covers_the_header_not_only_the_records():
    """A record-only digest would let evidence be re-pointed by editing one line."""
    doc = stamped(two_pending())
    records_only = json.dumps(doc["records"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert doc[FINGERPRINT_FIELD] != "sha256:" + hashlib.sha256(records_only.encode("utf-8")).hexdigest()
    for field in LEDGER_BINDING_FIELDS + ("prepared_at", "catalogue_fetched_at", "note"):
        moved = 99 if isinstance(doc[field], int) else "moved"
        assert ledger_fingerprint(tampered(doc, **{field: moved})) != doc[FINGERPRINT_FIELD], field


def test_a_ledger_carries_its_schema_kind_and_both_bindings():
    doc = build_ledger(catalogue_view(), policy=policy_doc())
    assert doc["schema_version"] == LEDGER_SCHEMA_VERSION and doc["kind"] == LEDGER_KIND
    assert doc[FINGERPRINT_FIELD] == ledger_fingerprint(doc)
    assert {field: doc[field] for field in BINDING_FIELDS} == catalogue_binding(catalogue_view(),
                                                                                DEFAULT_UNIVERSE_FILE)
    assert doc["catalogue_schema_version"] == 1 and doc["catalogue_candidate_count"] == 2
    assert doc["catalogue_content_hash"] == catalogue_doc()["content_hash"]
    assert doc["prepared_for"] == DEFAULT_UNIVERSE_FILE
    # The host policy is bound on the same three terms: which file, which schema,
    # which content.
    assert {field: doc[field] for field in POLICY_BINDING_FIELDS} == policy_binding(
        policy_doc(), DEFAULT_SOURCE_POLICY_FILE)
    assert doc["source_policy_path"] == DEFAULT_SOURCE_POLICY_FILE
    assert doc["source_policy_schema_version"] == POLICY_SCHEMA_VERSION
    assert doc["source_policy_fingerprint"] == policy_doc()[POLICY_FINGERPRINT_FIELD]


def test_a_ledger_cannot_be_built_without_an_available_catalogue():
    with pytest.raises(LedgerError, match="unavailable catalogue"):
        build_ledger({"available": False, "candidates": []}, policy=policy_doc())


def test_a_ledger_cannot_be_built_without_a_source_policy():
    """There is no implicit host list to fall back on, and no empty one either."""
    for absent in ({}, None):
        with pytest.raises(LedgerError, match="without a validated source policy"):
            build_ledger(catalogue_view(), policy=absent)


def test_build_keeps_prepared_records_and_drops_delisted_ones():
    """A record for a code the catalogue no longer lists would make coverage a fiction.

    Whether dropping it is *allowed* is the builder's decision (see the rebind
    policy below); this function only assembles what it is told to assemble.
    """
    previous = {"XA0": reviewable("XA0"), "OLD0": record("OLD0", notes="delisted upstream")}
    doc = build_ledger(catalogue_view(), previous, policy=policy_doc())
    assert sorted(doc["records"]) == ["XA0", "XB0"]
    assert doc["records"]["XA0"] == previous["XA0"], "prepared evidence survives a rebuild verbatim"
    assert doc["records"]["XB0"]["admission_state"] == STATE_PENDING
    assert doc["count"] == 2


def test_a_new_record_starts_pending_with_every_term_unknown():
    """Unknown is preserved as unknown: nothing is defaulted into an empty slot."""
    row = build_ledger(catalogue_view(["XA0"]), policy=policy_doc())["records"]["XA0"]
    assert row["admission_state"] == STATE_PENDING
    assert set(row["evidence"]) == set(EVIDENCE_TERMS)
    assert all(row["evidence"][term] is None for term in EVIDENCE_TERMS)
    assert row["blocked_reason"] == "" and row["prepared_by"] == "" and row["prepared_at"] == ""


def test_an_unknown_value_survives_beside_its_source():
    """A source found before the number is extracted keeps value=None rather than a guess."""
    entry = evidence(None, review_state=REVIEW_UNREVIEWED)
    doc = stamped({"XA0": record("XA0", evidence=full_evidence(proposed_multiplier=entry)),
                   "XB0": record("XB0")})
    validate_ledger(doc)
    assert doc["records"]["XA0"]["evidence"]["proposed_multiplier"]["value"] is None
    assert doc["records"]["XA0"]["evidence"]["proposed_multiplier"]["source_url"] == SOURCE_URL


def test_the_three_states_are_the_whole_vocabulary():
    """No state means verified, admitted or tradable -- that is the point of the layer."""
    assert set((STATE_PENDING, STATE_BLOCKED, STATE_READY_FOR_REVIEW)) == {"pending", "blocked", "ready_for_review"}
    for banned in ("verified", "tradable", "admitted", "approved", "can_trade"):
        assert banned not in (STATE_PENDING + STATE_BLOCKED + STATE_READY_FOR_REVIEW)


# ------------------------------------------------------ committed artefacts --

def test_the_committed_ledger_matches_the_committed_catalogue_and_policy():
    catalogue = load_catalogue(CATALOGUE)
    assert catalogue["available"], "these checks are vacuous without a readable catalogue"
    policy = load_policy(POLICY)
    doc = load_ledger(LEDGER, catalogue=catalogue, catalogue_path=DEFAULT_UNIVERSE_FILE,
                      policy=policy, policy_path=DEFAULT_SOURCE_POLICY_FILE)
    assert doc["count"] == catalogue["count"] == len(doc["records"]) == 83
    assert doc["prepared_for"] == DEFAULT_UNIVERSE_FILE, "the declared path is the committed default"
    assert doc["source_policy_path"] == DEFAULT_SOURCE_POLICY_FILE
    assert binding_mismatches(doc, catalogue, DEFAULT_UNIVERSE_FILE) == []
    assert policy_binding_mismatches(doc, policy, DEFAULT_SOURCE_POLICY_FILE) == []
    assert {field: doc[field] for field in BINDING_FIELDS} == catalogue_binding(catalogue,
                                                                                DEFAULT_UNIVERSE_FILE)
    assert {field: doc[field] for field in POLICY_BINDING_FIELDS} == policy_binding(
        policy, DEFAULT_SOURCE_POLICY_FILE)


def test_the_committed_policy_covers_every_exchange_the_catalogue_lists():
    """A venue with no mapping can never be cited, so an unmapped one is a gap.

    Fail-closed means the gap is safe -- nothing gets through -- but it would
    silently make one exchange's candidates unpreparable, which is a bug rather
    than a policy.
    """
    policy = load_policy(POLICY)
    listed = sorted({c["exchange"] for c in load_catalogue(CATALOGUE)["candidates"]})
    assert listed, "vacuous without a readable catalogue"
    for exchange in listed:
        assert official_hosts(policy, exchange), f"{exchange} has no official host mapping"


def test_the_committed_ledger_claims_nothing_about_any_candidate():
    """The initial ledger is honest about having gathered no evidence at all."""
    doc = read_ledger(LEDGER)
    assert len(doc["records"]) == 83
    states = {row["admission_state"] for row in doc["records"].values()}
    assert states == {STATE_PENDING}
    for symbol, row in doc["records"].items():
        assert all(row["evidence"][term] is None for term in EVIDENCE_TERMS), symbol
    # B1b adds the *fields* a citation must carry, not a citation.  A slot with a
    # provenance claim in it would mean this branch recorded a source.
    assert prepared_records(doc) == []
    assert "source_kind" not in json.dumps(doc, ensure_ascii=False)


def test_the_committed_ledger_carries_no_execution_terms():
    """Structural, not stylistic: a ledger must never be readable as a contract row."""
    doc = read_ledger(LEDGER)
    assert forbidden_keys_in(doc) == set()
    assert FORBIDDEN_SPEC_KEYS.isdisjoint(set(EVIDENCE_TERMS))
    assert json.dumps(doc, ensure_ascii=False).count('"verified"') == 0


def test_the_committed_policy_is_a_host_policy_and_nothing_more():
    """No term, no candidate value, no credential, no citation -- only hostnames."""
    doc = read_policy(POLICY)
    validate_policy(doc)
    assert set(doc) == set(POLICY_FIELDS)
    assert doc["kind"] == POLICY_KIND and doc["schema_version"] == POLICY_SCHEMA_VERSION
    assert forbidden_keys_in(doc) == set()
    blob = json.dumps(doc, ensure_ascii=False)
    for leaked in ("http://", "password", "token", "api_key", "@", "*", "proposed_"):
        assert leaked not in blob, leaked
    for exchange, hosts in doc["exchange_hosts"].items():
        assert exchange == exchange.upper() and hosts == [h.lower() for h in hosts]


def test_the_committed_policy_treats_ines_shfe_hosts_as_a_listed_exception():
    """Listed, not inferred: sharing a parent domain is not evidence of anything."""
    policy = load_policy(POLICY)
    assert "www.shfe.com.cn" in official_hosts(policy, "INE")
    assert "www.ine.cn" in official_hosts(policy, "INE")
    # The exception runs one way only.  SHFE does not inherit INE's own hosts, and
    # no other venue inherits anybody's.
    assert "www.ine.cn" not in official_hosts(policy, "SHFE")
    for exchange in ("CFFEX", "CZCE", "DCE", "GFEX"):
        assert "www.shfe.com.cn" not in official_hosts(policy, exchange)


# -------------------------------------------------------- document refusals --

@pytest.mark.parametrize("override,message", [
    ({"kind": "contracts"}, "not an admission ledger"),
    ({"kind": None}, "not an admission ledger"),
    ({"schema_version": 1}, "schema_version"),
    ({"schema_version": 3}, "schema_version"),
    ({"records": []}, "records mapping"),
    ({"records": None}, "records mapping"),
    ({"count": 99}, "count must equal"),
    ({"active_symbols": ["XA0"]}, "unsupported fields"),
    ({"note": None}, "note must be a string"),
    ({"prepared_for": 7}, "prepared_for must be a string"),
    ({"catalogue_content_hash": None}, "catalogue_content_hash must be a string"),
    ({"catalogue_schema_version": "1"}, "catalogue_schema_version must be a non-negative integer"),
    ({"catalogue_schema_version": -1}, "catalogue_schema_version must be a non-negative integer"),
    ({"catalogue_candidate_count": None}, "catalogue_candidate_count must be a non-negative integer"),
    ({"catalogue_candidate_count": 2.0}, "catalogue_candidate_count must be a non-negative integer"),
    ({"source_policy_path": None}, "source_policy_path must be a string"),
    ({"source_policy_fingerprint": 7}, "source_policy_fingerprint must be a string"),
    ({"source_policy_schema_version": "1"},
     "source_policy_schema_version must be a non-negative integer"),
    ({"source_policy_schema_version": -1},
     "source_policy_schema_version must be a non-negative integer"),
])
def test_a_broken_document_is_refused(override, message):
    with pytest.raises(LedgerError, match=message):
        validate_ledger(stamped(two_pending(), **override))


def test_a_non_mapping_document_is_refused():
    for payload in ([], "ledger", None, 7):
        with pytest.raises(LedgerError, match="document mapping"):
            validate_ledger(payload)


def test_an_edited_record_breaks_the_fingerprint():
    doc = stamped(two_pending())
    doc["records"]["XA0"]["notes"] = "edited after stamping"
    with pytest.raises(LedgerError, match="fingerprint does not match"):
        validate_ledger(doc)


@pytest.mark.parametrize("field,value", [
    ("catalogue_content_hash", "sha256:another-snapshot"),
    ("catalogue_schema_version", 2),
    ("catalogue_candidate_count", 3),
    ("prepared_for", "config/somewhere_else.json"),
    ("source_policy_path", "config/another_policy.json"),
    ("source_policy_schema_version", 2),
    ("source_policy_fingerprint", "sha256:another-policy"),
    ("catalogue_fetched_at", "2027-01-01T00:00:00+08:00"),
    ("prepared_at", "2027-01-01"),
    ("note", "an edited disclaimer"),
    ("ledger_fingerprint", "sha256:0"),
])
def test_a_header_only_tamper_is_refused(field, value):
    """Nothing in the header is outside the digest, so re-pointing evidence fails.

    This is the P1 case: with a record-only fingerprint, every one of these
    edits leaves an otherwise perfectly valid ledger, and gathered evidence
    silently becomes evidence for a different snapshot -- or, for the three policy
    fields, evidence judged first party under a host list nobody approved.
    """
    doc = tampered(stamped(two_pending()), **{field: value})
    with pytest.raises(LedgerError, match="fingerprint does not match"):
        validate_ledger(doc)
    assert projected(doc)["available"] is False


@pytest.mark.parametrize("term", sorted(FORBIDDEN_SPEC_KEYS))
def test_an_execution_term_anywhere_refuses_the_whole_ledger(term):
    """Whitelists catch it in a record; the document-wide sweep catches it anywhere else."""
    doc = stamped({"XA0": dict(record("XA0"), **{term: 10}), "XB0": record("XB0")})
    with pytest.raises(LedgerError):
        validate_ledger(doc)
    nested = stamped(two_pending(), prepared_for={"note": {term: 10}})
    with pytest.raises(LedgerError, match="execution terms"):
        validate_ledger(nested)


# ---------------------------------------------------------- record refusals --

@pytest.mark.parametrize("row,message", [
    (record("XA0", symbol="XB0"), "disagrees with its symbol field"),
    (record("XA0", admission_state="verified"), "unknown admission_state"),
    (record("XA0", admission_state="tradable"), "unknown admission_state"),
    (record("XA0", admission_state=STATE_BLOCKED), "blocked without a blocked_reason"),
    (record("XA0", admission_state=STATE_BLOCKED, blocked_reason="   "), "blocked without a blocked_reason"),
    (record("XA0", blocked_reason="no quote"), "blocked_reason while in state"),
    (record("XA0", prepared_at="19/09/2026"), "prepared_at must be YYYY-MM-DD"),
    (record("XA0", notes=None), "notes must be a string"),
    (record("XA0", evidence={}), "evidence must carry exactly"),
    (record("XA0", evidence=dict(full_evidence(), extra_term=None)), "evidence must carry exactly"),
    ("not a record", "is not an admission record"),
])
def test_a_broken_record_is_refused(row, message):
    with pytest.raises(LedgerError, match=message):
        validate_ledger(stamped({"XA0": row}))


def test_a_record_filed_under_an_invalid_symbol_is_refused():
    with pytest.raises(LedgerError, match="invalid continuous symbol"):
        validate_ledger(stamped({"rb0": record("rb0")}))


def test_a_blocked_record_says_why():
    doc = stamped({"XA0": record("XA0", admission_state=STATE_BLOCKED,
                                 blocked_reason="upstream quote probe failed; no 0-suffix code")})
    validate_ledger(doc)


# -------------------------------------------------------- evidence refusals --

@pytest.mark.parametrize("url", [
    "not a url", "ftp://example.com/a", "javascript:alert(1)", "data:text/html,x",
    "https://", "example.com/rules", " https://example.com/a", "https://exa mple.com/a",
    # Plaintext, credentials and ports are refused on the URL alone -- no policy
    # and no catalogue needed, because none of them is the plain public document
    # address a first-party citation claims to be, whichever venue it names.
    "http://rules.example.com/a", "https://user:pw@rules.example.com/a",
    "https://rules.example.com:8443/a", "https://rules.example.com:443/a",
    "https://rules.example.com:/a",
])
def test_a_malformed_or_unsafe_source_url_is_refused(url):
    row = record("XA0", evidence=full_evidence(proposed_tick=evidence(1, source_url=url)))
    with pytest.raises(LedgerError, match="source_url"):
        validate_ledger(stamped({"XA0": row}))
    # And under the full gate too, so the refusal is not an artefact of validating
    # without the catalogue and policy.
    with pytest.raises(LedgerError, match="source_url"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x08", "\x09", "\x1b", "\x1f", "\x7f"])
@pytest.mark.parametrize("place", ["lead", "trail", "inside"])
def test_a_control_character_anywhere_in_a_source_url_is_refused(control, place):
    """Before, after or inside the address, and before the parser sees any of it."""
    url = {"lead": control + SOURCE_URL,
           "trail": SOURCE_URL + control,
           "inside": SOURCE_URL.replace("rules.", "rules" + control + ".")}[place]
    assert "control character" in transport_problem(url)
    row = record("XA0", evidence=full_evidence(proposed_tick=evidence(1, source_url=url)))
    with pytest.raises(LedgerError, match="source_url"):
        validate_ledger(stamped({"XA0": row}))
    with pytest.raises(LedgerError, match="source_url"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))


def control_free(text: str) -> bool:
    """Whether a message can be printed or logged without carrying a control onward."""
    return all(ord(ch) >= 0x20 and ord(ch) != 0x7F for ch in text)


@pytest.mark.parametrize("url", ["\x00" + SOURCE_URL, SOURCE_URL + "\x00",
                                 "\x1f" + SOURCE_URL, SOURCE_URL + "\x7f",
                                 f"https://rules\t.{FIXTURE_HOSTS[1]}/xchg/xa-contract",
                                 f"https://rules\n.{FIXTURE_HOSTS[1]}/xchg/xa-contract",
                                 f"https://rules\r.{FIXTURE_HOSTS[1]}/xchg/xa-contract"])
def test_the_url_check_runs_before_the_parser_that_would_launder_it(url):
    """``urlsplit`` hands back a *listed* host for every one of these.

    It strips leading and trailing C0 controls and removes tab, LF and CR from
    anywhere in the string.  So an address that is not an official host --
    ``rules<TAB>.example.com`` -- parses into one that is, and a NUL-terminated
    string parses as though the NUL were not written down.  Judging the parsed
    form would approve a host the file does not contain, which is exactly the
    lookalike substitution the exact-host rule exists to refuse; so the refusal
    happens on the raw string, before any parsing.
    """
    assert urlsplit(url).hostname in FIXTURE_HOSTS, "vacuous unless the parser really launders it"
    assert "control character" in transport_problem(url)
    row = record("XA0", evidence=full_evidence(proposed_tick=evidence(1, source_url=url)))
    with pytest.raises(LedgerError, match="source_url"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))
    # The message reports code points, so the raw controls are not echoed onward.
    assert control_free(transport_problem(url))


@pytest.mark.parametrize("field", ["published_date", "effective_date"])
@pytest.mark.parametrize("value", ["2026-13-01", "2026-02-30", "20260919", "2026-9-1",
                                   "yesterday", "2026-09-19T10:00:00+08:00"])
def test_a_malformed_date_is_refused(field, value):
    row = record("XA0", evidence=full_evidence(proposed_tick=evidence(1, **{field: value})))
    with pytest.raises(LedgerError, match=field):
        validate_ledger(stamped({"XA0": row}))


@pytest.mark.parametrize("term,value", [
    ("proposed_multiplier", 0), ("proposed_multiplier", -10), ("proposed_multiplier", "10"),
    ("proposed_multiplier", True), ("proposed_multiplier", float("inf")),
    ("proposed_tick", 0), ("proposed_tick", -1), ("proposed_tick", [1]),
    ("proposed_margin_ratio", 0), ("proposed_margin_ratio", -0.1), ("proposed_margin_ratio", 1.5),
    ("proposed_margin_ratio", "13%"),
    ("proposed_fee", 0), ("proposed_fee", {"per_lot": 0}), ("proposed_fee", {"rate": 0.0001}),
    ("proposed_fee", {"per_lot": 0, "rate": -1}), ("proposed_fee", {"per_lot": 0, "rate": "cheap"}),
    ("proposed_fee", {"per_lot": 0, "rate": 0.0001, "close_today": 1}),
    ("proposed_sessions", []), ("proposed_sessions", "09:00-15:00"),
    ("proposed_sessions", [["09:00"]]), ("proposed_sessions", [["09:00", "08:00"]]),
    ("proposed_sessions", [["09:00", "09:00"]]), ("proposed_sessions", [["9am", "15:00"]]),
    ("proposed_sessions", [["09:00", "72:00"]]), ("proposed_sessions", [[900, 1500]]),
])
def test_a_malformed_proposed_value_is_refused(term, value):
    row = record("XA0", evidence=full_evidence(**{term: evidence(value)}))
    with pytest.raises(LedgerError, match=term):
        validate_ledger(stamped({"XA0": row}))


def test_an_overnight_session_proposal_stays_valid():
    """21:00..26:30 closes at 02:30 the next day, as the contract table writes it."""
    row = record("XA0", evidence=full_evidence(
        proposed_sessions=evidence([["09:00", "10:15"], ["21:00", "26:30"]])))
    validate_ledger(stamped({"XA0": row}))


@pytest.mark.parametrize("state", [REVIEW_RECORDED, REVIEW_CROSS_CHECKED])
@pytest.mark.parametrize("broken,message", [
    ({"value": None}, "no value recorded"),
    ({"source_url": ""}, "without source_url"),
    ({"source_title": "  "}, "without source_title"),
    ({"recorded_by": ""}, "without recorded_by"),
    ({"published_date": "", "effective_date": ""}, "without a published or effective date"),
    ({"source_authority": ""}, "without source_authority"),
    ({"source_kind": "  "}, "without source_kind"),
])
def test_a_stronger_review_state_needs_its_citation(state, broken, message):
    """Claiming a transcription happened without saying from what, when or by whom."""
    entry = evidence(1, review_state=state, **broken)
    row = record("XA0", evidence=full_evidence(proposed_tick=entry))
    with pytest.raises(LedgerError, match=message):
        validate_ledger(stamped({"XA0": row}))


def test_an_effective_date_alone_satisfies_the_citation():
    entry = evidence(1, published_date="", effective_date="2026-01-15")
    validate_ledger(stamped({"XA0": record("XA0", evidence=full_evidence(proposed_tick=entry))}))


@pytest.mark.parametrize("state", [REVIEW_RECORDED, REVIEW_CROSS_CHECKED])
@pytest.mark.parametrize("term", sorted(DATED_TERMS))
def test_a_sourced_margin_or_fee_needs_the_date_it_takes_effect(term, state):
    """Margin and fee are revised by notice; "published" is not "applies from".

    For the other three terms a publication date is enough, because a multiplier
    is a property of the contract rather than a monthly decision.
    """
    entry = evidence(0.13 if term == TERM_MARGIN else {"per_lot": 0, "rate": 0.0001}, term,
                     review_state=state, published_date="2026-01-15", effective_date="")
    row = record("XA0", evidence=full_evidence(**{term: entry}))
    with pytest.raises(LedgerError, match="without an effective_date"):
        validate_ledger(stamped({"XA0": row}))
    with pytest.raises(LedgerError, match="without an effective_date"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))
    # The same slot with the effective date recorded is accepted.
    dated = dict(entry, effective_date="2026-02-01")
    checked(stamped({"XA0": record("XA0", evidence=full_evidence(**{term: dated})),
                     "XB0": record("XB0")}))


@pytest.mark.parametrize("term", sorted(DATED_TERMS))
def test_an_unreviewed_margin_or_fee_citation_still_needs_its_effective_date(term):
    """The dated-term loophole: an undated margin notice is undated before review too.

    This slot is not a sketch.  It names ``exchange_primary``, the right kind of
    document and an exact host the policy lists for the candidate's own venue --
    it *is* a sourced margin or fee notice, with the one thing that makes such a
    notice readable missing.  Deferring the check to ``recorded`` would leave it
    in the file looking complete until the reviewer who trusted it promoted it,
    which is the person least placed to notice the gap.
    """
    entry = evidence(full_evidence()[term]["value"], term, review_state=REVIEW_UNREVIEWED,
                     published_date="2026-01-15", effective_date="", recorded_by="")
    assert all(entry[field].strip() for field in ("source_url", "source_authority", "source_kind"))
    assert source_problem(policy_doc(), FIXTURE_EXCHANGE, term, entry["source_url"],
                          entry["source_authority"], entry["source_kind"]) == "", \
        "the citation must be policy-clean, or this proves only that provenance works"
    row = record("XA0", evidence=full_evidence(**{term: entry}))
    # Structure-only validation refuses it...
    with pytest.raises(LedgerError, match="without an effective_date"):
        validate_ledger(stamped({"XA0": row}))
    # ...and so does the full gate, so the refusal is not an artefact of either.
    with pytest.raises(LedgerError, match="without an effective_date"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))


@pytest.mark.parametrize("term", sorted(DATED_TERMS))
def test_a_dated_unreviewed_margin_or_fee_citation_stays_an_honest_draft(term):
    """Supplying the date makes the draft valid -- and leaves it exactly a draft.

    The repair must not turn the effective date into a promotion: the slot is
    still ``unreviewed``, the record is still ``pending``, and asking for
    ``ready_for_review`` on it is still refused.
    """
    entry = evidence(full_evidence()[term]["value"], term, review_state=REVIEW_UNREVIEWED,
                     published_date="2026-01-15", effective_date="2026-02-01", recorded_by="")
    doc = stamped({"XA0": record("XA0", evidence=full_evidence(**{term: entry})),
                   "XB0": record("XB0")})
    checked(doc)
    assert doc["records"]["XA0"]["admission_state"] == STATE_PENDING
    assert projected(doc)["counts"][STATE_READY_FOR_REVIEW] == 0
    with pytest.raises(LedgerError, match="unreviewed evidence"):
        checked(stamped({"XA0": reviewable("XA0", evidence=full_evidence(**{term: entry})),
                         "XB0": record("XB0")}))


@pytest.mark.parametrize("named", sorted(("source_url", "source_authority", "source_kind")))
@pytest.mark.parametrize("term", sorted(DATED_TERMS))
def test_naming_any_part_of_a_margin_or_fee_source_demands_the_effective_date(term, named):
    """Naming *any* provenance field is naming a source, and one cannot be half-cited.

    Whichever field is filled in first, the slot is refused: either for the two
    that are still blank, or -- once all three are there -- for the date.  There
    is no order of typing that produces an accepted undated citation.
    """
    full = evidence(None, term, review_state=REVIEW_UNREVIEWED, recorded_by="", effective_date="")
    partial = blank_evidence(**{named: full[named]})
    for entry in (partial, dict(full, effective_date="")):
        row = record("XA0", evidence=full_evidence(**{term: entry}))
        with pytest.raises(LedgerError):
            validate_ledger(stamped({"XA0": row}))
    # The wholly blank slot is not the honest unknown state either -- that is
    # ``None``, and it is what an unresearched term already holds.
    with pytest.raises(LedgerError, match="empty evidence record"):
        validate_ledger(stamped({"XA0": record("XA0", evidence=full_evidence(
            **{term: blank_evidence()}))}))
    checked(stamped({"XA0": record("XA0", evidence=full_evidence(**{term: None})),
                     "XB0": record("XB0")}))


@pytest.mark.parametrize("term", sorted(DATED_TERMS))
def test_a_rebuild_never_invents_a_missing_effective_date(term, tmp_path, capsys):
    """A ledger holding an undated citation is left alone, not quietly completed."""
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc())
    undated = evidence(full_evidence()[term]["value"], term, review_state=REVIEW_UNREVIEWED,
                       published_date="2026-01-15", effective_date="", recorded_by="")
    before = put_ledger(out, stamped({"XA0": record("XA0", evidence=full_evidence(**{term: undated})),
                                      "XB0": record("XB0")}, catalogue=catalogue_view(),
                                     catalogue_path=cat_path, policy_path=pol_path))

    assert builder.main(argv) == 1

    assert out.read_bytes() == before, "the file must be left exactly as it was"
    assert "effective_date" in capsys.readouterr().err


@pytest.mark.parametrize("term", [TERM_MULTIPLIER, TERM_TICK, TERM_SESSIONS])
def test_an_undated_term_is_satisfied_by_a_publication_date(term):
    value = {TERM_MULTIPLIER: 10, TERM_TICK: 1, TERM_SESSIONS: [["09:00", "10:15"]]}[term]
    entry = evidence(value, term, published_date="2026-01-15", effective_date="")
    checked(stamped({"XA0": record("XA0", evidence=full_evidence(**{term: entry})),
                     "XB0": record("XB0")}))


# --------------------------------------------------- provenance: the matrix --

def test_a_policy_clean_citation_passes_offline(offline):
    """The positive case, with the network guard armed: nothing is resolved or fetched.

    An exact policy-listed host, the venue the catalogue says lists the candidate,
    ``exchange_primary``, a document kind the term can be established from, and the
    dates each term needs.
    """
    doc = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})
    checked(doc)
    assert projected(doc)["available"] is True
    assert source_problem(policy_doc(), FIXTURE_EXCHANGE, TERM_TICK, SOURCE_URL,
                          SOURCE_AUTHORITY_EXCHANGE_PRIMARY, KIND_CONTRACT) == ""
    # Both listed spellings of the venue's host are equally first party.
    for host in FIXTURE_HOSTS:
        assert venue_problem(policy_doc(), FIXTURE_EXCHANGE, f"https://{host}/spec") == ""


@pytest.mark.parametrize("term,kind", sorted((term, kind) for term, kinds in TERM_SOURCE_KINDS.items()
                                             for kind in kinds))
def test_every_allowed_term_and_kind_pair_is_accepted(term, kind):
    assert citation_problem(term, SOURCE_AUTHORITY_EXCHANGE_PRIMARY, kind) == ""


@pytest.mark.parametrize("term,kind", sorted((term, kind) for term in TERM_SOURCE_KINDS
                                             for kind in SOURCE_KINDS
                                             if kind not in TERM_SOURCE_KINDS[term]))
def test_a_term_cannot_be_established_from_the_wrong_kind_of_document(term, kind):
    """A contract specification does not set this month's margin, and a fee notice
    does not set the multiplier.  The matrix is exhaustive in both directions."""
    assert "cannot establish" in citation_problem(term, SOURCE_AUTHORITY_EXCHANGE_PRIMARY, kind)
    entry = evidence(full_evidence()[term]["value"], term, source_kind=kind)
    row = record("XA0", evidence=full_evidence(**{term: entry}))
    with pytest.raises(LedgerError, match="cannot establish"):
        validate_ledger(stamped({"XA0": row}))
    with pytest.raises(LedgerError, match="cannot establish"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))


@pytest.mark.parametrize("authority", ["", "exchange", "vendor", "broker_summary", "regulator",
                                       "exchange_secondary", "self", "EXCHANGE_PRIMARY", None, 1])
def test_only_the_exchange_itself_may_be_named_as_the_authority(authority):
    """Accurate is not the same as first party: a vendor's transcription is still a
    transcription, which is the error this layer exists to catch."""
    assert "source_authority must be" in citation_problem(TERM_TICK, authority, KIND_CONTRACT)
    if not isinstance(authority, str):
        return                                     # a non-string is a type refusal, tested below
    entry = evidence(1, source_authority=authority)
    row = record("XA0", evidence=full_evidence(proposed_tick=entry))
    expected = "without source_authority" if not authority.strip() else "source_authority must be"
    with pytest.raises(LedgerError, match=expected):
        validate_ledger(stamped({"XA0": row}))


@pytest.mark.parametrize("kind", ["official_blog", "vendor_page", "news_article", "OFFICIAL_CONTRACT",
                                  "official_contract_draft"])
def test_an_unknown_source_kind_is_refused(kind):
    assert "unknown source_kind" in citation_problem(TERM_TICK, SOURCE_AUTHORITY_EXCHANGE_PRIMARY, kind)
    row = record("XA0", evidence=full_evidence(proposed_tick=evidence(1, source_kind=kind)))
    with pytest.raises(LedgerError, match="unknown source_kind"):
        validate_ledger(stamped({"XA0": row}))


@pytest.mark.parametrize("field", ["source_authority", "source_kind"])
def test_a_non_string_provenance_field_is_refused(field):
    row = record("XA0", evidence=full_evidence(proposed_tick=evidence(1, **{field: None})))
    with pytest.raises(LedgerError, match=f"{field} must be a string"):
        validate_ledger(stamped({"XA0": row}))


@pytest.mark.parametrize("missing", ["source_url", "source_authority", "source_kind"])
def test_naming_a_source_at_all_requires_all_three_provenance_fields(missing):
    """Checked at first mention, not at review time: a draft citing a vendor page
    would otherwise sit in the file looking fine until somebody promoted it."""
    entry = evidence(None, review_state=REVIEW_UNREVIEWED, **{missing: ""})
    row = record("XA0", evidence=full_evidence(proposed_tick=entry))
    with pytest.raises(LedgerError, match=f"without {missing}"):
        validate_ledger(stamped({"XA0": row}))


def test_a_slot_that_claims_nothing_at_all_must_be_null():
    """A blank dictionary is not an honest unknown -- it is a slot nobody filled in.

    Coverage is derived from how many slots are non-``None``, so a record with a
    null value and every citation field empty would be counted as a gathered
    source for a term with nothing behind it.  ``None`` says the same thing
    truthfully, and it is what a fresh record already holds, so the blank
    dictionary is refused rather than treated as a second spelling of unknown.
    """
    blank = record("XA0", evidence=full_evidence(proposed_tick=blank_evidence()))
    with pytest.raises(LedgerError, match="empty evidence record"):
        validate_ledger(stamped({"XA0": blank}))
    with pytest.raises(LedgerError, match="empty evidence record"):
        checked(stamped({"XA0": blank, "XB0": record("XB0")}))
    # The same absence written the one supported way is valid, and is what the
    # builder writes for every candidate.
    checked(stamped({"XA0": record("XA0", evidence=full_evidence(proposed_tick=None)),
                     "XB0": record("XB0")}))
    assert pending_record("XA0")["evidence"][TERM_TICK] is None


@pytest.mark.parametrize("term", sorted(EVIDENCE_TERMS))
def test_a_recorded_value_with_no_provenance_at_all_is_refused(term):
    """A number nobody can trace, sitting in exactly the shape a reviewer promotes.

    This is the hole that made ``unreviewed`` a place to park unsourced figures:
    the value rules proved the number was well formed, and nothing asked where it
    came from until somebody claimed a review state.
    """
    numeric = blank_evidence(value=full_evidence()[term]["value"])
    row = record("XA0", evidence=full_evidence(**{term: numeric}))
    with pytest.raises(LedgerError, match="without source_url"):
        validate_ledger(stamped({"XA0": row}))
    with pytest.raises(LedgerError, match="without source_url"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))


@pytest.mark.parametrize("named", sorted(CITATION_METADATA_FIELDS))
def test_citation_metadata_without_the_core_of_a_citation_is_refused(named):
    """Describing a document without saying where it is, is not a citation either.

    ``source_title`` alone names something nobody can open, ``published_date`` and
    ``effective_date`` alone date a document nobody can find, and ``recorded_by``
    alone credits a reading of it.  Each is an assertion that a source exists, so
    each demands the same three fields a recorded value does.
    """
    filled = {"source_title": SOURCE_TITLE, "published_date": "2026-01-15",
              "effective_date": "2026-01-20", "recorded_by": "reviewer-a"}[named]
    row = record("XA0", evidence=full_evidence(proposed_tick=blank_evidence(**{named: filled})))
    with pytest.raises(LedgerError, match="without source_url"):
        validate_ledger(stamped({"XA0": row}))
    with pytest.raises(LedgerError, match="without source_url"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))


@pytest.mark.parametrize("term", sorted(EVIDENCE_TERMS))
def test_a_complete_unreviewed_draft_is_the_one_accepted_partial_slot(term):
    """The honest draft: a source found, the number not transcribed yet.

    Complete core provenance, a policy-clean citation, and -- for the two terms set
    by notice -- the date the figure applies from.  Accepting it is not a
    promotion: the slot stays ``unreviewed`` and the record stays ``pending``.
    Removing any one of those four fields refuses it again.
    """
    draft = evidence(None, term, review_state=REVIEW_UNREVIEWED, recorded_by="",
                     effective_date="2026-01-20" if term in DATED_TERMS else "")
    doc = stamped({"XA0": record("XA0", evidence=full_evidence(**{term: draft})),
                   "XB0": record("XB0")})
    checked(doc)
    assert doc["records"]["XA0"]["admission_state"] == STATE_PENDING
    assert projected(doc)["counts"][STATE_READY_FOR_REVIEW] == 0
    for missing in ("source_url", "source_authority", "source_kind"):
        row = record("XA0", evidence=full_evidence(**{term: dict(draft, **{missing: ""})}))
        with pytest.raises(LedgerError, match=f"without {missing}"):
            checked(stamped({"XA0": row, "XB0": record("XB0")}))
    if term in DATED_TERMS:
        row = record("XA0", evidence=full_evidence(**{term: dict(draft, effective_date="")}))
        with pytest.raises(LedgerError, match="without an effective_date"):
            checked(stamped({"XA0": row, "XB0": record("XB0")}))


# ------------------------------------------------- provenance: the host list --

def test_another_venues_official_host_is_not_this_venues_source():
    """Exchange-primary somewhere is not exchange-primary here."""
    policy = policy_doc({FIXTURE_EXCHANGE: ["rules.example.com"],
                         "YCHG": ["rules.example.org"]})
    assert venue_problem(policy, FIXTURE_EXCHANGE, "https://rules.example.org/spec")
    assert venue_problem(policy, "YCHG", "https://rules.example.com/spec")
    row = record("XA0", evidence=full_evidence(
        proposed_tick=evidence(1, source_url="https://rules.example.org/spec")))
    with pytest.raises(LedgerError, match="not an exact official host"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}, policy=policy), policy=policy)


def test_a_host_outside_the_policy_entirely_is_refused():
    row = record("XA0", evidence=full_evidence(
        proposed_tick=evidence(1, source_url="https://cdn.attacker.example/spec")))
    with pytest.raises(LedgerError, match="not an exact official host"):
        checked(stamped({"XA0": row, "XB0": record("XB0")}))


@pytest.mark.parametrize("lookalike", [
    "https://www.shfe.com.cn.attacker.example/rules",
    "https://www.shfe.com.cn.attacker.example./rules",
    "https://wwwshfe.com.cn/rules",
    "https://attacker.example/?u=www.shfe.com.cn",
    "https://attacker.example/www.shfe.com.cn/rules",
    "https://www.shfe.com.cn./rules",
])
def test_a_lookalike_of_a_listed_host_is_a_different_host(lookalike):
    """Matching is equality against a finite list, so there is no suffix to exploit.

    Run against the committed production policy, because that is where a real
    listed host exists to impersonate.  The URLs are an attacker's, exist only in
    this assertion, and are never written into any ledger.
    """
    policy = load_policy(POLICY)
    assert "www.shfe.com.cn" in official_hosts(policy, "SHFE"), "vacuous without the real host listed"
    assert venue_problem(policy, "SHFE", lookalike), lookalike
    # The genuine host, for contrast: the refusals above are about these hosts
    # specifically, not about the checker refusing everything.
    assert venue_problem(policy, "SHFE", "https://www.shfe.com.cn/rules") == ""


def test_a_policy_with_no_mapping_for_the_candidates_exchange_fails_closed():
    """No mapping is a refusal, never a pass: an unmapped venue has no first party."""
    empty_for_fixture = policy_doc({"YCHG": ["rules.example.org"]})
    assert "lists no official host" in venue_problem(empty_for_fixture, FIXTURE_EXCHANGE, SOURCE_URL)
    assert official_hosts(empty_for_fixture, FIXTURE_EXCHANGE) == ()
    with pytest.raises(LedgerError, match="lists no official host"):
        checked(stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")},
                        policy=empty_for_fixture), policy=empty_for_fixture)


def test_a_candidate_whose_catalogue_record_names_no_exchange_fails_closed():
    """The venue comes from the catalogue, so an unknown venue is an unknown source."""
    nameless = catalogue_view(exchange="")
    assert "names no exchange" in venue_problem(policy_doc(), "", SOURCE_URL)
    with pytest.raises(LedgerError, match="names no exchange"):
        checked(stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")}, catalogue=nameless),
                catalogue=nameless)


def test_the_venue_is_never_taken_from_the_citation_itself():
    """A citation that could nominate its own exchange would be a host proving itself."""
    policy = policy_doc({FIXTURE_EXCHANGE: ["rules.example.com"], "YCHG": ["rules.example.org"]})
    # The same URL is first party for one venue and foreign for the other, decided
    # entirely by what the catalogue says lists the candidate.
    assert venue_problem(policy, FIXTURE_EXCHANGE, "https://rules.example.com/x") == ""
    assert venue_problem(policy, "YCHG", "https://rules.example.com/x") != ""
    elsewhere = catalogue_view(exchange="YCHG")
    doc = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")}, catalogue=elsewhere,
                  policy=policy)
    with pytest.raises(LedgerError, match="not an exact official host"):
        checked(doc, catalogue=elsewhere, policy=policy)


def test_the_host_check_is_case_and_spelling_exact():
    policy = policy_doc({FIXTURE_EXCHANGE: ["rules.example.com"]})
    # urlsplit lowercases the host, so an uppercase URL names the same host.
    assert venue_problem(policy, FIXTURE_EXCHANGE, "https://RULES.EXAMPLE.COM/x") == ""
    # Anything that is not that hostname is not that host.
    for other in ("https://rules.example.com.attacker.example/x", "https://sub.rules.example.com/x",
                  "https://example.com/x", "https://rules-example.com/x"):
        assert venue_problem(policy, FIXTURE_EXCHANGE, other), other


def test_a_bare_policy_host_cannot_be_written_as_a_pattern_or_a_url():
    """The document is forced to hold exact hostnames, so no policy can loosen matching."""
    for bad in ("*.shfe.com.cn", ".shfe.com.cn", "https://www.shfe.com.cn", "www.shfe.com.cn/rules",
                "www.shfe.com.cn:443", "WWW.SHFE.COM.CN", "www.shfe.com.cn.", "localhost",
                "", "-shfe.com.cn", "shfe .com.cn", 7, None):
        with pytest.raises(SourcePolicyError, match="exact lowercase hostname"):
            validate_policy(policy_doc({FIXTURE_EXCHANGE: [bad]}))


@pytest.mark.parametrize("field", ["review_state", "value"])
def test_an_unknown_evidence_field_or_state_is_refused(field):
    entry = evidence(1)
    if field == "review_state":
        entry["review_state"] = "exchange_verified"
        message = "unknown review_state"
    else:
        entry["confidence"] = "high"
        message = "unsupported fields"
    row = record("XA0", evidence=full_evidence(proposed_tick=entry))
    with pytest.raises(LedgerError, match=message):
        validate_ledger(stamped({"XA0": row}))


def test_an_unreviewed_slot_still_has_to_be_well_formed():
    """``unreviewed`` lowers what a slot has to claim, never how well formed it is."""
    draft = evidence(None, review_state=REVIEW_UNREVIEWED, recorded_by="")
    validate_ledger(stamped({"XA0": record("XA0", evidence=full_evidence(proposed_tick=draft))}))
    for broken, message in ((dict(draft, source_url="not a url"), "source_url"),
                            (dict(draft, published_date="2026-13-01"), "published_date"),
                            (dict(draft, source_kind=KIND_FEE_RULE), "cannot establish"),
                            (blank_evidence(source_url="not a url"), "source_url")):
        with pytest.raises(LedgerError, match=message):
            validate_ledger(stamped({"XA0": record("XA0", evidence=full_evidence(
                proposed_tick=broken))}))


# --------------------------------------------------- ready_for_review rules --

def test_ready_for_review_needs_all_five_evidence_categories():
    for term in EVIDENCE_TERMS:
        row = reviewable("XA0", evidence=full_evidence(**{term: None}))
        with pytest.raises(LedgerError, match="ready_for_review without evidence"):
            validate_ledger(stamped({"XA0": row}))


def test_ready_for_review_needs_every_category_actually_reviewed():
    entry = evidence(1, review_state=REVIEW_UNREVIEWED)
    row = reviewable("XA0", evidence=full_evidence(proposed_tick=entry))
    with pytest.raises(LedgerError, match="unreviewed evidence"):
        validate_ledger(stamped({"XA0": row}))


@pytest.mark.parametrize("missing", ["prepared_by", "prepared_at"])
def test_ready_for_review_names_who_prepared_it_and_when(missing):
    row = reviewable("XA0", **{missing: ""})
    with pytest.raises(LedgerError, match="without prepared_by / prepared_at"):
        validate_ledger(stamped({"XA0": row}))


def test_a_fully_cited_record_reaches_ready_for_review_and_no_further():
    doc = stamped({"XA0": reviewable("XA0")})
    validate_ledger(doc)
    assert doc["records"]["XA0"]["admission_state"] == STATE_READY_FOR_REVIEW
    # The strongest state still says nothing about trading: the projection below
    # is the only consumer, and it is display-only.
    assert "XA0" not in load_cn_contracts(CONTRACTS)[0]


def test_a_record_that_passes_the_provenance_gate_is_still_not_executable(tmp_path):
    """The whole point of the layer: first-party evidence is still only evidence.

    A record whose five terms are all cited from policy-approved first-party hosts
    reaches ``ready_for_review`` and nothing else.  B0 still refuses to start a
    competition on it, because the contract table -- the only thing that makes a
    symbol executable -- says nothing about it.
    """
    candidate_code = ledger_only_symbols()[0]
    reviewed = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})
    checked(reviewed)                                   # the full gate, provenance included
    view = projected(reviewed)
    assert view["available"] is True
    rows = {r["symbol"]: r for r in view["records"]}
    assert rows["XA0"]["evidence_recorded"] == len(EVIDENCE_TERMS)
    # No state in the vocabulary, and nothing in the projected row, says otherwise.
    assert rows["XA0"]["admission_state"] == STATE_READY_FOR_REVIEW
    assert set(rows["XA0"]) == set(PROJECTION_RECORD_FIELDS)
    # And B0 is unmoved: a ledger code with no local specification cannot start.
    cfg = make_cfg(tmp_path, ["RB0", candidate_code], db_name="admission-provenance-gate.db")
    db = Database(cfg.db_path)
    with pytest.raises(ContractSpecError) as err:
        Engine(cfg, db, accelerate=True)
    assert err.value.symbols == [candidate_code]
    assert db.list_competitions() == []
    db.close()


# --------------------------------------------------- the catalogue binding --

def test_a_ledger_for_a_different_content_hash_is_refused():
    other = catalogue_view(("XA0", "XC0"))          # same count, different candidates
    doc = stamped(two_pending(), catalogue_content_hash=other["content_hash"])
    with pytest.raises(LedgerError, match="catalogue_content_hash"):
        validate_ledger(doc, catalogue=catalogue_view(), catalogue_path=DEFAULT_UNIVERSE_FILE)


def test_a_ledger_for_a_different_catalogue_schema_is_refused():
    """The content hash covers the candidate block only; a schema move is invisible to it."""
    moved = catalogue_view(schema_version=2)
    assert moved["content_hash"] == catalogue_view()["content_hash"], "the hash alone cannot see this"
    doc = stamped(two_pending())
    with pytest.raises(LedgerError, match="catalogue_schema_version"):
        validate_ledger(doc, catalogue=moved, catalogue_path=DEFAULT_UNIVERSE_FILE)
    assert projected(doc, catalogue=moved)["available"] is False


def test_a_ledger_for_a_different_candidate_count_is_refused():
    grown = catalogue_view(("XA0", "XB0", "XC0"))
    doc = stamped({s: record(s) for s in ("XA0", "XB0", "XC0")}, catalogue=catalogue_view())
    problems = binding_mismatches(doc, grown, DEFAULT_UNIVERSE_FILE)
    assert any("catalogue_candidate_count" in p for p in problems)
    with pytest.raises(LedgerError, match="catalogue_candidate_count"):
        validate_ledger(doc, catalogue=grown, catalogue_path=DEFAULT_UNIVERSE_FILE)


def test_a_ledger_holding_an_unlisted_symbol_is_refused():
    records = dict(two_pending(), ZZ0=record("ZZ0"))
    with pytest.raises(LedgerError, match="catalogue does not list"):
        validate_ledger(stamped(records), catalogue=catalogue_view(), catalogue_path=DEFAULT_UNIVERSE_FILE)


def test_a_ledger_missing_a_listed_candidate_is_refused():
    with pytest.raises(LedgerError, match="no record for listed candidates"):
        validate_ledger(stamped({"XA0": record("XA0")}), catalogue=catalogue_view(), catalogue_path=DEFAULT_UNIVERSE_FILE)


def test_the_catalogue_view_reports_the_schema_version_it_declares():
    """The binding reads its schema off a validated view, never off a raw file."""
    assert catalogue_view()["schema_version"] == 1
    assert catalogue_view(schema_version=2)["schema_version"] == 2
    for bogus in (None, "1", True, 1.0):
        doc = catalogue_doc()
        doc["schema_version"] = bogus
        assert sanitize_catalogue(doc)["schema_version"] == 0, bogus
    assert sanitize_catalogue("not a catalogue")["schema_version"] == 0


def test_the_catalogue_endpoint_is_unchanged_by_the_binding_field(client, tmp_path):
    """The ledger's needs must not quietly widen the candidate catalogue's contract."""
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_trio(tmp_path, catalogue_doc(), stamped(two_pending()))
    body = api.get("/api/universe").json()
    assert "schema_version" not in body
    assert set(body) == {"available", "source", "source_url", "fetched_at", "snapshot_status",
                         "content_hash", "count", "note", "candidates"}


def test_the_binding_cannot_be_proved_against_an_unavailable_catalogue():
    doc = stamped(two_pending())
    assert binding_mismatches(doc, {"available": False}, DEFAULT_UNIVERSE_FILE) == [
        "the catalogue is unavailable, so the ledger binding cannot be proved"]
    with pytest.raises(LedgerError, match="unavailable"):
        validate_ledger(doc, catalogue={"available": False}, catalogue_path=DEFAULT_UNIVERSE_FILE)


def test_a_binding_check_without_a_declared_path_is_refused():
    """A reader that knows which file it opened has to say so; that hole was the bug."""
    with pytest.raises(LedgerError, match="needs the path the catalogue was read from"):
        validate_ledger(stamped(two_pending()), catalogue=catalogue_view())
    with pytest.raises(LedgerError, match="needs the path the policy was read from"):
        validate_ledger(stamped(two_pending()), policy=policy_doc())


# ----------------------------------------------- the source policy binding --

@pytest.mark.parametrize("other", [
    policy_doc({FIXTURE_EXCHANGE: list(FIXTURE_HOSTS) + ["cdn.example.net"]}),
    policy_doc({FIXTURE_EXCHANGE: ["rules.example.com"]}),
    policy_doc({"YCHG": ["rules.example.org"]}),
])
def test_a_ledger_bound_to_a_different_policy_content_is_refused(other):
    """A widened host list is a different policy, and evidence does not follow it."""
    doc = stamped(two_pending())
    assert other[POLICY_FINGERPRINT_FIELD] != policy_doc()[POLICY_FINGERPRINT_FIELD]
    problems = policy_binding_mismatches(doc, other, DEFAULT_SOURCE_POLICY_FILE)
    assert any("source_policy_fingerprint" in p for p in problems)
    with pytest.raises(LedgerError, match="source_policy_fingerprint"):
        checked(doc, policy=other)
    assert projected(doc, policy=other)["available"] is False


def test_a_ledger_declaring_a_different_policy_path_is_refused():
    """Byte-identical policies at two paths are still two files; only one was read."""
    doc = stamped(two_pending())
    other = "config/another_source_policy.json"
    assert policy_binding_mismatches(doc, policy_doc(), other) == [
        f"source_policy_path {doc['source_policy_path']!r} does not match the source policy's "
        f"{normalized_catalogue_path(other)!r}"]
    with pytest.raises(LedgerError, match="source_policy_path"):
        checked(doc, policy_path=other)
    assert projected(doc, policy_path=other)["available"] is False


def test_a_ledger_bound_to_a_different_policy_schema_is_refused():
    """A schema move is reported as itself, not only as a fingerprint that shifted."""
    doc = stamped(two_pending())
    bumped = policy_doc(schema_version=POLICY_SCHEMA_VERSION + 1)
    problems = policy_binding_mismatches(doc, bumped, DEFAULT_SOURCE_POLICY_FILE)
    assert any("source_policy_schema_version" in p for p in problems)
    with pytest.raises(LedgerError, match="source_policy_schema_version"):
        checked(doc, policy=bumped)


def test_a_path_only_policy_move_leaks_nothing_through_the_projection():
    doc = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})
    view = projected(doc, policy_path="config/another_source_policy.json")
    assert view["available"] is False
    assert view["records"] == [] and view["count"] == 0
    blob = json.dumps(view, ensure_ascii=False)
    for leaked in ("XA0", "XB0", SOURCE_URL, SOURCE_TITLE, "reviewer-a", "0.0001",
                   FIXTURE_HOSTS[0], KIND_CONTRACT, SOURCE_AUTHORITY_EXCHANGE_PRIMARY,
                   "another_source_policy"):
        assert leaked not in blob, leaked


# -------------------------------------------- the source policy document ---

def test_the_policy_fingerprint_is_deterministic_and_covers_the_whole_document():
    forward = policy_doc({"AAA": ["a.example.com"], "BBB": ["b.example.com"]})
    backward = policy_doc({"BBB": ["b.example.com"], "AAA": ["a.example.com"]})
    assert forward[POLICY_FINGERPRINT_FIELD] == backward[POLICY_FINGERPRINT_FIELD]
    shuffled = {key: forward[key] for key in reversed(list(forward))}
    assert policy_fingerprint(shuffled) == policy_fingerprint(forward)
    # Every field other than the digest itself is inside the digest.
    for field in set(POLICY_FIELDS) - {POLICY_FINGERPRINT_FIELD}:
        moved = 99 if isinstance(forward[field], int) else "moved"
        edited = dict(forward, **{field: moved})
        assert policy_fingerprint(edited) != forward[POLICY_FINGERPRINT_FIELD], field
        with pytest.raises(SourcePolicyError):
            validate_policy(edited)


def test_an_edited_policy_breaks_its_fingerprint():
    doc = policy_doc()
    doc["exchange_hosts"][FIXTURE_EXCHANGE].append("cdn.attacker.example")
    with pytest.raises(SourcePolicyError, match="policy_fingerprint does not match"):
        validate_policy(doc)


@pytest.mark.parametrize("override,message", [
    ({"kind": "contract_admission_ledger"}, "not an admission source policy"),
    ({"kind": None}, "not an admission source policy"),
    ({"schema_version": 2}, "schema_version"),
    ({"note": None}, "note must be a string"),
    ({"exchange_hosts": {}}, "non-empty exchange_hosts"),
    ({"exchange_hosts": []}, "non-empty exchange_hosts"),
    ({"exchange_hosts": None}, "non-empty exchange_hosts"),
    ({"exchange_hosts": {"SHFE": "www.shfe.com.cn"}}, "at least one official host"),
    ({"exchange_hosts": {"SHFE": []}}, "at least one official host"),
    ({"exchange_hosts": {"shfe": ["www.shfe.com.cn"]}}, "invalid exchange code"),
    ({"exchange_hosts": {"": ["www.shfe.com.cn"]}}, "invalid exchange code"),
    # A policy may not grow a knob that loosens matching, nor a fallback host list
    # that would apply to venues nobody wrote a rule for.
    ({"default_hosts": ["www.shfe.com.cn"]}, "unsupported fields"),
    ({"allow_suffix_match": True}, "unsupported fields"),
    ({"term_source_kinds": {"proposed_fee": ["official_contract"]}}, "unsupported fields"),
])
def test_a_broken_policy_is_refused(override, message):
    with pytest.raises(SourcePolicyError, match=message):
        validate_policy(policy_doc(**override))


def test_an_execution_term_anywhere_refuses_the_whole_policy():
    """A host policy is not a place to write a contract term, at any depth."""
    with pytest.raises(SourcePolicyError, match="unsupported fields"):
        validate_policy(policy_doc(multiplier=10))
    with pytest.raises(SourcePolicyError, match="execution terms"):
        validate_policy(policy_doc(note={"detail": {"margin_ratio": 0.1}}))


@pytest.mark.parametrize("version", [True, False, 1.0, "1", "one", None, [1], {"v": 1}])
def test_a_policy_schema_version_must_be_the_integer_itself(version):
    """``True == 1`` and ``1.0 == 1`` in Python, so equality alone is not a type check.

    A boolean passed for schema 1 and the document was then read under rules it
    never claimed -- the one thing a schema version exists to state.
    """
    doc = policy_doc(schema_version=version)
    assert doc[POLICY_FINGERPRINT_FIELD] == policy_fingerprint(doc), \
        "stamped, so this is refused on the version and not incidentally on the digest"
    with pytest.raises(SourcePolicyError, match="schema_version"):
        validate_policy(doc)


@pytest.mark.parametrize("literal", ["127.0.0.1", "192.168.0.10", "10.0.0.1", "8.8.8.8",
                                     "::1", "[::1]", "2001:db8::1", "[2001:db8::1]",
                                     "::ffff:192.168.0.1"])
def test_a_policy_host_may_not_be_a_numeric_address(literal):
    """An exchange publishes at a name; an address is whatever answers there today.

    A dotted IPv4 quad satisfies the hostname grammar exactly, which is why this
    is a rule of its own rather than something the pattern happens to catch.
    """
    with pytest.raises(SourcePolicyError, match="IP literal"):
        validate_policy(policy_doc({FIXTURE_EXCHANGE: [literal]}))
    # And a citation on such a host is refused as a matter of course: no policy can
    # list it, so it is never an exact official host for any venue.
    assert venue_problem(policy_doc(), FIXTURE_EXCHANGE, f"https://{literal.strip('[]')}/spec")


# The numeric IPv4 spellings that predate the canonical dotted quad and that the
# C resolver -- and therefore a URL stack that normalises before connecting --
# still accepts.  Every one of them names 127.0.0.1.
NUMERIC_IPV4_BYPASSES = ("127.1", "127.0.1", "127.0.0.01", "0177.0.0.1", "0x7f.0.0.1",
                         "0x7f.0x0.0x0.0x1", "0x7f.1", "2130706433")


@pytest.mark.parametrize("spelling", NUMERIC_IPV4_BYPASSES)
def test_a_legacy_numeric_address_spelling_is_never_a_dns_hostname(spelling):
    """``ipaddress`` is necessary and not sufficient: it reads all of these as names.

    Short, octal, hexadecimal and single-integer IPv4 forms all resolve to one
    machine, so matching any one of them as a *string* against a list of names
    states nothing about the others -- and the policy's whole claim is that a
    listed host is the host.  The rule is decided from the text, so it is the same
    answer on every machine and no resolver gets a vote in it.
    """
    with pytest.raises(ValueError):
        ipaddress.ip_address(spelling)          # the stdlib calls it a name ...
    assert numeric_host(spelling)               # ... and this layer does not.

    with pytest.raises(SourcePolicyError, match="IP literal"):
        validate_policy(policy_doc({FIXTURE_EXCHANGE: [spelling]}))
    # Refused on its form, before any host list is consulted: a policy that
    # somehow carried the spelling would still not let the citation through.
    assert "numeric address" in venue_problem(policy_doc(), FIXTURE_EXCHANGE,
                                              f"https://{spelling}/spec")
    assert "numeric address" in venue_problem(policy_doc({FIXTURE_EXCHANGE: [spelling]}),
                                              FIXTURE_EXCHANGE, f"https://{spelling}/spec")


def test_the_numeric_host_rule_decides_canonical_addresses_too():
    """One rule for every address spelling, so no form is refused by accident only."""
    for address in ("127.0.0.1", "8.8.8.8", "::1", "[::1]", "2001:db8::1", "::ffff:192.168.0.1"):
        assert numeric_host(address), address
        assert numeric_host_problem(address)
    for name in ("www.shfe.com.cn", "rules.example.com", "123.example.com", "0x7f.example.com",
                 "1a.2b.example.org"):
        assert not numeric_host(name), name
        assert numeric_host_problem(name) == ""
    for nothing in ("", None, 10):
        assert not numeric_host(nothing)


def test_the_committed_official_hosts_are_still_accepted_names():
    """The hardening must refuse addresses without refusing any listed exchange host."""
    policy = load_policy(POLICY)
    for exchange, hosts in policy["exchange_hosts"].items():
        for host in hosts:
            assert not numeric_host(host), (exchange, host)
            assert venue_problem(policy, exchange, f"https://{host}/rule") == ""


@pytest.mark.parametrize("field", sorted(POLICY_FIELDS))
def test_a_policy_missing_a_required_field_is_refused(field):
    doc = policy_doc()
    doc.pop(field)
    with pytest.raises(SourcePolicyError, match="missing required fields"):
        validate_policy(doc)


def test_a_policy_listing_a_host_twice_is_refused():
    with pytest.raises(SourcePolicyError, match="twice"):
        validate_policy(policy_doc({FIXTURE_EXCHANGE: ["a.example.com", "a.example.com"]}))


def test_a_duplicate_key_in_the_policy_file_is_refused(tmp_path):
    """json.load would silently keep the last of two identical venue entries."""
    path = tmp_path / "policy.json"
    text = json.dumps(policy_doc({"AAA": ["a.example.com"]}), ensure_ascii=False)
    duplicated = text.replace('"AAA": ["a.example.com"]',
                              '"AAA": ["a.example.com"], "AAA": ["cdn.attacker.example"]', 1)
    path.write_text(duplicated, encoding="utf-8")
    with pytest.raises(SourcePolicyError, match="duplicate key"):
        read_policy(path)


def test_an_unreadable_policy_is_refused(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    for path in (tmp_path / "broken.json", tmp_path / "absent.json"):
        with pytest.raises(SourcePolicyError, match="could not be read"):
            read_policy(path)
        with pytest.raises(SourcePolicyError):
            load_policy(path)


# ---------------------------------------------------- the declared path -----

def test_a_ledger_declaring_a_different_catalogue_path_is_refused():
    """Byte-identical catalogues at two paths are still two files; only one was read."""
    doc = stamped(two_pending(), catalogue_path="config/contracts_universe_cn.json")
    other = "config/contracts_universe_cn_copy.json"
    assert binding_mismatches(doc, catalogue_view(), other) == [
        f"prepared_for {doc['prepared_for']!r} does not match the catalogue's "
        f"{normalized_catalogue_path(other)!r}"]
    with pytest.raises(LedgerError, match="prepared_for"):
        validate_ledger(doc, catalogue=catalogue_view(), catalogue_path=other)


def test_a_path_only_mismatch_leaks_nothing_through_the_projection():
    doc = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})
    view = projected(doc, catalogue_path="config/somewhere_else.json")
    assert view["available"] is False
    assert view["records"] == [] and view["count"] == 0
    blob = json.dumps(view, ensure_ascii=False)
    for leaked in ("XA0", "XB0", SOURCE_URL, SOURCE_TITLE, "reviewer-a", "0.0001"):
        assert leaked not in blob


@pytest.mark.parametrize("spelling", ["config/contracts_universe_cn.json",
                                      "config\\contracts_universe_cn.json",
                                      "./config/contracts_universe_cn.json",
                                      "config//contracts_universe_cn.json",
                                      "config/sub/../contracts_universe_cn.json"])
def test_equivalent_spellings_of_one_path_are_one_identity(spelling):
    """Only spellings this platform already treats as the same file are folded together.

    On Windows a backslash is a separator, so both spellings name one file; on
    POSIX it is an ordinary filename character and the two are genuinely
    different, which the normaliser must not paper over.
    """
    same_file = os.path.normpath(spelling) == os.path.normpath(DEFAULT_UNIVERSE_FILE)
    assert (normalized_catalogue_path(spelling) == DEFAULT_UNIVERSE_FILE) is same_file
    doc = stamped(two_pending(), catalogue_path=spelling)
    assert (binding_mismatches(doc, catalogue_view(), DEFAULT_UNIVERSE_FILE) == []) is same_file
    assert projected(doc)["available"] is same_file


def test_the_normaliser_never_consults_the_filesystem(tmp_path):
    """A relative declaration and its absolute twin stay distinct, so nothing agrees by accident."""
    absolute = str(tmp_path / "config" / "contracts_universe_cn.json")
    assert normalized_catalogue_path(absolute) != normalized_catalogue_path(DEFAULT_UNIVERSE_FILE)
    assert normalized_catalogue_path("") == "" and normalized_catalogue_path(None) == ""
    assert normalized_catalogue_path("  config/x.json  ") == "config/x.json"


# ------------------------------------------------------- the rebind policy --

def test_an_untouched_ledger_may_be_rebound():
    """Nothing prepared means nothing to mis-attribute, so a refresh is safe."""
    doc = stamped(two_pending())
    assert prepared_records(doc) == []
    assert rebind_refusals(doc, catalogue_view(("XA0", "XB0", "XC0")), DEFAULT_UNIVERSE_FILE) == []
    assert rebind_refusals(doc, catalogue_view(), "config/elsewhere.json") == []
    # The same holds for the policy binding: an all-pending ledger holds no
    # citation, so there is no judgement to re-attribute to a new host list.
    widened = policy_doc({FIXTURE_EXCHANGE: list(FIXTURE_HOSTS) + ["cdn.example.net"]})
    assert rebind_refusals(doc, catalogue_view(), DEFAULT_UNIVERSE_FILE, widened,
                           DEFAULT_SOURCE_POLICY_FILE) == []
    assert rebind_refusals(doc, catalogue_view(), DEFAULT_UNIVERSE_FILE, policy_doc(),
                           "config/another_source_policy.json") == []


@pytest.mark.parametrize("touched", [
    reviewable("XA0"),
    record("XA0", evidence=full_evidence(proposed_tick=evidence(1))),
    record("XA0", admission_state=STATE_BLOCKED, blocked_reason="no continuous code upstream"),
    record("XA0", notes="checked the venue index, contract page not found yet"),
    record("XA0", prepared_by="reviewer-a"),
    record("XA0", prepared_at="2026-09-19"),
])
def test_a_prepared_ledger_may_not_be_rebound(touched):
    doc = stamped({"XA0": touched, "XB0": record("XB0")})
    assert prepared_records(doc) == ["XA0"]
    refusals = rebind_refusals(doc, catalogue_view(("XA0", "XB0", "XC0")), DEFAULT_UNIVERSE_FILE)
    assert refusals, "a moved binding plus prepared material must never rebind automatically"
    assert any("prepared material" in reason for reason in refusals)
    # A path-only move is a rebind too: same bytes, different file.
    path_only = rebind_refusals(doc, catalogue_view(), "config/contracts_universe_cn_copy.json")
    assert any("prepared_for" in reason for reason in path_only)
    assert any("prepared material" in reason for reason in path_only)
    # The same ledger against its own snapshot at its own path is not a rebind at all.
    assert rebind_refusals(doc, catalogue_view(), DEFAULT_UNIVERSE_FILE) == []
    assert rebind_refusals(doc, catalogue_view(), DEFAULT_UNIVERSE_FILE, policy_doc(),
                           DEFAULT_SOURCE_POLICY_FILE) == []


@pytest.mark.parametrize("policy,policy_path,expected", [
    # A changed host list: the citation may have been judged against hosts this
    # policy no longer lists, or would now accept hosts the old one refused.
    (policy_doc({FIXTURE_EXCHANGE: list(FIXTURE_HOSTS) + ["cdn.example.net"]}),
     DEFAULT_SOURCE_POLICY_FILE, "source_policy_fingerprint"),
    # The identical policy at another declared path: same bytes, different file.
    (policy_doc(), "config/another_source_policy.json", "source_policy_path"),
])
def test_a_prepared_ledger_may_not_be_rebound_to_another_policy(policy, policy_path, expected):
    doc = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})
    refusals = rebind_refusals(doc, catalogue_view(), DEFAULT_UNIVERSE_FILE, policy, policy_path)
    assert any(expected in reason for reason in refusals), refusals
    assert any("prepared material" in reason for reason in refusals)


def test_a_duplicate_symbol_in_the_file_is_refused(tmp_path):
    """json.load would silently keep the last of two identical keys."""
    path = tmp_path / "dup.json"
    doc = json.dumps(stamped(two_pending()), ensure_ascii=False)
    duplicated = doc.replace('"XA0": {', '"XA0": {"symbol": "XA0"}, "XA0": {', 1)
    path.write_text(duplicated, encoding="utf-8")
    with pytest.raises(LedgerError, match="duplicate key"):
        read_ledger(path)


def test_an_unreadable_ledger_is_refused(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LedgerError, match="could not be read"):
        read_ledger(path)
    with pytest.raises(LedgerError, match="could not be read"):
        read_ledger(tmp_path / "absent.json")


# ------------------------------------------------------------- the builder --

def write_catalogue_file(path: Path, catalogue: dict) -> Path:
    path.write_text(json.dumps(catalogue, ensure_ascii=False), encoding="utf-8")
    return path


def write_policy_file(path: Path, policy: dict | None = None) -> Path:
    path.write_text(json.dumps(policy if policy is not None else policy_doc(), ensure_ascii=False),
                    encoding="utf-8")
    return path


def build_paths(tmp_path: Path, catalogue: dict, name: str = "catalogue.json",
                policy: dict | None = None, policy_name: str = "policy.json"):
    """A catalogue file, a policy file, an output path, and the builder's argv."""
    cat_path = write_catalogue_file(tmp_path / name, catalogue)
    pol_path = write_policy_file(tmp_path / policy_name, policy)
    out = tmp_path / "ledger.json"
    argv = ["--catalogue", str(cat_path), "--source-policy", str(pol_path),
            "--out", str(out), "--prepared-at", "2026-09-19"]
    return argv, out, cat_path, pol_path


def put_ledger(out: Path, doc: dict) -> bytes:
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out.read_bytes()


def test_the_builder_refreshes_an_untouched_ledger_onto_a_new_snapshot(tmp_path, capsys):
    """An all-pending ledger has no evidence to mis-attribute, so it follows the catalogue."""
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc(("XA0", "XB0", "XC0")))
    put_ledger(out, stamped(two_pending(), catalogue=catalogue_view(), catalogue_path=cat_path,
                            policy_path=pol_path))

    assert builder.main(argv) == 0

    doc = read_ledger(out)
    catalogue = load_catalogue(cat_path)
    validate_ledger(doc, catalogue=catalogue, catalogue_path=str(cat_path),
                    policy=load_policy(pol_path), policy_path=str(pol_path))
    assert sorted(doc["records"]) == ["XA0", "XB0", "XC0"]
    assert doc["catalogue_candidate_count"] == 3
    assert "1 added" in capsys.readouterr().out


def test_an_untouched_ledger_may_move_to_another_catalogue_file(tmp_path, capsys):
    """Nothing prepared, so the declared path follows the file it was told to read."""
    source_a = write_catalogue_file(tmp_path / "a.json", catalogue_doc())
    argv, out, source_b, pol_path = build_paths(tmp_path, catalogue_doc(), name="b.json")
    put_ledger(out, stamped(two_pending(), catalogue=catalogue_view(), catalogue_path=source_a,
                            policy_path=pol_path))

    assert builder.main(argv) == 0

    doc = read_ledger(out)
    assert doc["prepared_for"] == normalized_catalogue_path(source_b)
    assert doc["prepared_for"] != normalized_catalogue_path(source_a)
    validate_ledger(doc, catalogue=load_catalogue(source_b), catalogue_path=str(source_b),
                    policy=load_policy(pol_path), policy_path=str(pol_path))


@pytest.mark.parametrize("moved_policy,moved_path,expected", [
    # An all-pending ledger holds no citation, so neither move can mis-attribute one.
    (policy_doc({FIXTURE_EXCHANGE: list(FIXTURE_HOSTS) + ["cdn.example.net"]}), "policy.json",
     "source_policy_fingerprint"),
    (None, "policy_b.json", "source_policy_path"),
])
def test_an_untouched_ledger_may_bind_to_a_changed_or_moved_policy(tmp_path, moved_policy,
                                                                  moved_path, expected):
    """The documented safe migration: nothing prepared, so rebinding costs nothing."""
    old_policy_path = write_policy_file(tmp_path / "policy_old.json")
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc(), policy=moved_policy,
                                                policy_name=moved_path)
    stale = stamped(two_pending(), catalogue=catalogue_view(), catalogue_path=cat_path,
                    policy_path=old_policy_path)
    put_ledger(out, stale)
    # The move really is a move: the same ledger would be refused against the new
    # policy identity before the rebuild.
    assert any(expected in reason for reason in
               policy_binding_mismatches(stale, load_policy(pol_path), str(pol_path)))

    assert builder.main(argv) == 0

    doc = read_ledger(out)
    assert doc["source_policy_path"] == normalized_catalogue_path(pol_path)
    assert doc["source_policy_fingerprint"] == load_policy(pol_path)[POLICY_FINGERPRINT_FIELD]
    assert builder.main(argv + ["--check"]) == 0
    # Still 2 honest pending records: a rebind is a header rewrite, not a promotion.
    assert prepared_records(doc) == [] and doc["count"] == 2


def test_the_builder_refuses_to_rebind_prepared_evidence_to_another_file(tmp_path, capsys):
    """The reproduced breach: identical bytes at another path is still another source."""
    source_a = write_catalogue_file(tmp_path / "a.json", catalogue_doc())
    argv, out, source_b, pol_path = build_paths(tmp_path, catalogue_doc(), name="b.json")
    assert source_a.read_bytes() == source_b.read_bytes(), "the two catalogues must be identical"
    prepared = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")},
                       catalogue=catalogue_view(), catalogue_path=source_a, policy_path=pol_path)
    before = put_ledger(out, prepared)

    assert builder.main(argv) == 3

    assert out.read_bytes() == before, "the file must be left exactly as it was"
    err = capsys.readouterr().err
    assert "refusing to rebind" in err and "prepared_for" in err and "XA0" in err
    # The ledger still declares the file its evidence was actually gathered against.
    doc = read_ledger(out)
    assert doc["prepared_for"] == normalized_catalogue_path(source_a)
    validate_ledger(doc, catalogue=load_catalogue(source_a), catalogue_path=str(source_a),
                    policy=load_policy(pol_path), policy_path=str(pol_path))


@pytest.mark.parametrize("moved_policy,moved_path,expected", [
    # A changed host list: a citation judged under the old one was never judged
    # under this one.
    (policy_doc({FIXTURE_EXCHANGE: list(FIXTURE_HOSTS) + ["cdn.example.net"]}), "policy.json",
     "source_policy_fingerprint"),
    # The identical policy at another declared path: same bytes, different file.
    (None, "policy_b.json", "source_policy_path"),
])
def test_the_builder_refuses_to_rebind_prepared_evidence_to_another_policy(
        tmp_path, capsys, moved_policy, moved_path, expected):
    """Prepared evidence never follows a moved or edited host policy by itself."""
    old_policy_path = write_policy_file(tmp_path / "policy_old.json")
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc(), policy=moved_policy,
                                                policy_name=moved_path)
    before = put_ledger(out, stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")},
                                     catalogue=catalogue_view(), catalogue_path=cat_path,
                                     policy_path=old_policy_path))

    assert builder.main(argv) == 3

    assert out.read_bytes() == before, "the file must be left exactly as it was"
    err = capsys.readouterr().err
    assert "refusing to rebind" in err and expected in err and "XA0" in err
    assert "left unchanged" in err
    # --check agrees, and neither the refusal nor the check leaks the evidence.
    assert builder.main(argv + ["--check"]) == 1
    out_text, err = capsys.readouterr()
    assert expected in err
    for leaked in (SOURCE_URL, SOURCE_TITLE, "reviewer-a", "0.0001", FIXTURE_HOSTS[0],
                   KIND_CONTRACT):
        assert leaked not in err and leaked not in out_text, leaked
    # And the projection under the new policy identity shows nothing at all.
    view = load_ledger_projection(out, load_catalogue(cat_path), str(cat_path),
                                  load_policy(pol_path), str(pol_path))
    assert view["available"] is False and view["records"] == []
    assert out.read_bytes() == before


def test_a_path_only_mismatch_fails_the_checker_without_leaking(tmp_path, capsys):
    source_a = write_catalogue_file(tmp_path / "a.json", catalogue_doc())
    argv, out, _, pol_path = build_paths(tmp_path, catalogue_doc(), name="b.json")
    put_ledger(out, stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")},
                            catalogue=catalogue_view(), catalogue_path=source_a,
                            policy_path=pol_path))

    assert builder.main(argv + ["--check"]) == 1

    out_text, err = capsys.readouterr()
    assert "prepared_for" in err
    for leaked in (SOURCE_URL, SOURCE_TITLE, "reviewer-a", "0.0001"):
        assert leaked not in err and leaked not in out_text


def test_the_builder_refuses_to_rebind_prepared_evidence(tmp_path, capsys):
    """Gathered evidence never follows a moved snapshot by itself either."""
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc(("XA0", "XB0", "XC0")))
    before = put_ledger(out, stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")},
                                     catalogue=catalogue_view(), catalogue_path=cat_path,
                                     policy_path=pol_path))

    assert builder.main(argv) == 3

    assert out.read_bytes() == before, "the file must be left exactly as it was"
    err = capsys.readouterr().err
    assert "refusing to rebind" in err and "XA0" in err
    assert "left unchanged" in err
    validate_ledger(read_ledger(out), catalogue=catalogue_view(), catalogue_path=str(cat_path),
                    policy=load_policy(pol_path), policy_path=str(pol_path))


def test_the_builder_refuses_a_rebind_when_only_the_catalogue_schema_moved(tmp_path):
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc(schema_version=2))
    before = put_ledger(out, stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")},
                                     catalogue=catalogue_view(), catalogue_path=cat_path,
                                     policy_path=pol_path))
    assert builder.main(argv) == 3
    assert out.read_bytes() == before


def test_the_builder_restamps_a_prepared_ledger_on_its_own_snapshot(tmp_path):
    """Same bindings and same paths, so this is a refresh rather than a rebind."""
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc())
    prepared = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")},
                       catalogue=catalogue_view(), catalogue_path=cat_path, policy_path=pol_path)
    put_ledger(out, prepared)
    assert builder.main(argv) == 0
    doc = read_ledger(out)
    assert doc["records"]["XA0"] == prepared["records"]["XA0"]
    validate_ledger(doc, catalogue=catalogue_view(), catalogue_path=str(cat_path),
                    policy=load_policy(pol_path), policy_path=str(pol_path))


def test_the_builder_leaves_a_structurally_broken_ledger_alone(tmp_path, capsys):
    argv, out, _, _ = build_paths(tmp_path, catalogue_doc())
    out.write_text("{not json", encoding="utf-8")
    assert builder.main(argv) == 1
    assert out.read_text(encoding="utf-8") == "{not json"
    assert "could not be read" in capsys.readouterr().err


def test_the_builder_checks_the_full_binding_without_writing(tmp_path):
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc())
    before = put_ledger(out, stamped(two_pending(), catalogue=catalogue_view(),
                                     catalogue_path=cat_path, policy_path=pol_path))
    assert builder.main(argv + ["--check"]) == 0
    assert out.read_bytes() == before

    drifted, out, moved, pol_path = build_paths(tmp_path, catalogue_doc(schema_version=2),
                                                name="moved.json")
    put_ledger(out, stamped(two_pending(), catalogue=catalogue_view(), catalogue_path=moved,
                            policy_path=pol_path))
    assert builder.main(drifted + ["--check"]) == 1


def test_the_builder_will_not_prepare_against_an_unusable_catalogue(tmp_path, capsys):
    broken = catalogue_doc()
    broken["candidates"]["XA0"]["multiplier"] = 10        # self-hashed contract row
    broken["content_hash"] = content_hash(broken["candidates"])
    argv, out, _, _ = build_paths(tmp_path, broken)
    assert builder.main(argv) == 2
    assert not out.exists()
    assert "unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("broken", [
    "{not json",
    json.dumps({"schema_version": 1, "kind": "contract_admission_source_policy"}),
    json.dumps(dict(policy_doc(), exchange_hosts={})),
    json.dumps(dict(policy_doc(), policy_fingerprint="sha256:0")),
])
def test_the_builder_will_not_prepare_without_a_usable_policy(tmp_path, capsys, broken):
    """No fallback host list: an unusable policy stops the build rather than widening it."""
    argv, out, _, pol_path = build_paths(tmp_path, catalogue_doc())
    pol_path.write_text(broken, encoding="utf-8")
    assert builder.main(argv) == 2
    assert not out.exists()
    assert "source policy" in capsys.readouterr().err

    # A missing policy file is the same refusal, and leaves an existing ledger alone.
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc(), name="c.json",
                                               policy_name="p2.json")
    before = put_ledger(out, stamped(two_pending(), catalogue=catalogue_view(),
                                     catalogue_path=cat_path, policy_path=pol_path))
    pol_path.unlink()
    assert builder.main(argv) == 2
    assert out.read_bytes() == before


# ------------------------------------------- the v1 -> v2 migration path ----

def v1_ledger(entries, catalogue=None, catalogue_path=DEFAULT_UNIVERSE_FILE, **over) -> dict:
    """A B1a ledger: no policy binding, no provenance fields, stamped as v1.

    Built by hand rather than by ``stamped`` because the point is a document the
    current schema does *not* accept.  Overrides are applied before stamping, as
    in ``stamped``, so a malformed legacy document is refused on its own merits
    rather than incidentally on the digest; edit it afterwards with ``tampered``
    to test the digest itself.
    """
    doc = {"schema_version": MIGRATABLE_SCHEMA_VERSIONS[0], "kind": LEDGER_KIND,
           "note": "B1a disclaimer", "catalogue_fetched_at": "2026-09-19T10:00:00+08:00",
           "prepared_at": "2026-09-19", "count": len(entries), "records": entries,
           **catalogue_binding(catalogue or catalogue_view(), catalogue_path)}
    doc.update(over)
    doc[FINGERPRINT_FIELD] = ledger_fingerprint(doc)
    return doc


def v1_evidence(value, **over) -> dict:
    """A B1a citation: complete by v1's rules, and silent about its provenance."""
    entry = {"value": value, "source_url": SOURCE_URL, "source_title": SOURCE_TITLE,
             "published_date": "2026-01-15", "effective_date": "",
             "review_state": REVIEW_RECORDED, "recorded_by": "reviewer-a"}
    entry.update(over)
    assert set(entry) == set(V1_EVIDENCE_FIELDS), "a B1a citation, by B1a's field set"
    return entry


def v1_blank() -> dict:
    """A B1a evidence mapping with every term still unknown."""
    return {term: None for term in EVIDENCE_TERMS}


def v1_blank_evidence() -> dict:
    """A legacy slot that was created and never filled -- refused in v1 too."""
    return v1_evidence(None, source_url="", source_title="", published_date="", recorded_by="",
                       review_state=REVIEW_UNREVIEWED)


def test_an_all_pending_v1_ledger_migrates_to_the_current_schema(tmp_path):
    """No citation in it, so the migration is a pure header rewrite."""
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc())
    legacy = v1_ledger(two_pending(), catalogue_path=cat_path)
    # It earns the migration by proving it was a valid B1a ledger first.
    assert legacy_ledger_problems(legacy) == []
    assert schema_migration_refusals(legacy) == []
    put_ledger(out, legacy)

    assert builder.main(argv) == 0

    doc = read_ledger(out)
    assert doc["schema_version"] == LEDGER_SCHEMA_VERSION
    assert doc["source_policy_path"] == normalized_catalogue_path(pol_path)
    assert sorted(doc["records"]) == list(FIXTURE_SYMBOLS)
    assert prepared_records(doc) == [], "a migration does not promote anything"
    assert builder.main(argv + ["--check"]) == 0


def test_a_v1_ledger_holding_prepared_material_fails_closed(tmp_path, capsys):
    """A rebuild would have to invent an authority and a kind for every citation."""
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc())
    legacy = v1_ledger({"XA0": record("XA0", evidence=dict(
        {term: None for term in EVIDENCE_TERMS}, proposed_tick=v1_evidence(1))),
        "XB0": record("XB0")}, catalogue_path=cat_path)
    refusals = schema_migration_refusals(legacy)
    assert any("must not invent" in reason for reason in refusals)
    before = put_ledger(out, legacy)

    assert builder.main(argv) == 3

    assert out.read_bytes() == before, "the file must be left exactly as it was"
    err = capsys.readouterr().err
    assert "predate the provenance fields" in err and "XA0" in err and "left unchanged" in err
    # --check refuses it too, and the projection shows nothing.
    assert builder.main(argv + ["--check"]) == 1
    assert load_ledger_projection(out, load_catalogue(cat_path), str(cat_path),
                                  load_policy(pol_path), str(pol_path))["available"] is False
    assert out.read_bytes() == before


@pytest.mark.parametrize("declared", [0, 3, 99, "1", None, True, 1.0, 2.0])
def test_an_unrecognised_schema_version_is_not_migratable(declared):
    """By exact type: ``True == 1`` and ``1.0 == 1``, and neither states a version."""
    legacy = dict(v1_ledger(two_pending()), schema_version=declared)
    assert any("cannot be migrated" in reason for reason in schema_migration_refusals(legacy))


def test_a_v1_document_that_is_not_a_ledger_is_not_migratable():
    for override, message in (({"kind": "contracts"}, "not an admission ledger"),
                              ({"note": {"multiplier": 10}}, "execution terms")):
        legacy = dict(v1_ledger(two_pending()), **override)
        assert any(message in reason for reason in schema_migration_refusals(legacy))


def test_a_current_schema_ledger_is_never_reported_as_needing_migration():
    assert schema_migration_refusals(stamped(two_pending())) == []
    assert schema_migration_refusals(stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})) == []


@pytest.mark.parametrize("override,expected", [
    ({"kind": "contracts"}, "not an admission ledger"),
    ({"kind": None}, "not an admission ledger"),
    ({"note": None}, "note must be a string"),
    ({"note": {"multiplier": 10}}, "execution terms"),
    ({"prepared_for": 7}, "prepared_for must be a string"),
    ({"catalogue_content_hash": None}, "catalogue_content_hash must be a string"),
    ({"catalogue_fetched_at": 20260919}, "catalogue_fetched_at must be a string"),
    ({"catalogue_schema_version": "1"}, "must be a non-negative integer"),
    ({"catalogue_schema_version": True}, "must be a non-negative integer"),
    ({"catalogue_candidate_count": 2.0}, "must be a non-negative integer"),
    ({"catalogue_candidate_count": -1}, "must be a non-negative integer"),
    ({"records": []}, "records mapping"),
    ({"records": None}, "records mapping"),
    ({"records": "XA0"}, "records mapping"),
    ({"count": 99}, "count must equal"),
    ({"count": "2"}, "count must equal"),
    # A v1 document may not carry v2 fields, nor fields no schema ever had: either
    # way it is not the B1a ledger it claims to be.
    ({"source_policy_path": DEFAULT_SOURCE_POLICY_FILE}, "unsupported fields"),
    ({"active_symbols": ["XA0"]}, "unsupported fields"),
])
def test_only_a_structurally_valid_v1_document_may_be_migrated(override, expected):
    """A file that merely says version 1 is not thereby a B1a ledger.

    Stamped with the flaw in it, so each case is refused for what it is rather
    than incidentally for a broken digest.  None of them may reach the
    prepared-material branch: "nothing to invent" is only true of a document whose
    records have been read and understood.
    """
    legacy = v1_ledger(two_pending(), **override)
    refusals = schema_migration_refusals(legacy)
    assert any(expected in reason for reason in refusals), refusals
    assert not any("must not invent" in reason for reason in refusals), refusals


@pytest.mark.parametrize("field", sorted(V1_LEDGER_FIELDS))
def test_a_v1_ledger_missing_any_required_field_is_not_migratable(field):
    """An incomplete legacy header is not completed by the rebuild either."""
    legacy = v1_ledger(two_pending())
    legacy.pop(field)
    expected = "cannot be migrated" if field == "schema_version" else "missing required fields"
    assert any(expected in reason for reason in schema_migration_refusals(legacy))


@pytest.mark.parametrize("field,value", [
    ("prepared_at", "2027-01-01"),
    ("catalogue_fetched_at", "2027-01-01T00:00:00+08:00"),
    ("catalogue_content_hash", "sha256:another-snapshot"),
    ("catalogue_schema_version", 2),
    ("catalogue_candidate_count", 3),
    ("prepared_for", "config/somewhere_else.json"),
    ("note", "an edited disclaimer"),
    (FINGERPRINT_FIELD, "sha256:0"),
])
def test_a_tampered_v1_ledger_is_not_migratable(field, value):
    """The migration stamps a fresh digest, so it must not be handed a broken one.

    This was the hole: a v1 document only had to *say* version 1, and the rebuild
    hashed whatever it found -- so an edit the legacy fingerprint would have caught
    came out of the migration as a currently valid, correctly stamped v2 ledger.
    """
    legacy = tampered(v1_ledger(two_pending()), **{field: value})
    refusals = schema_migration_refusals(legacy)
    assert any("fingerprint does not match" in reason for reason in refusals), refusals
    assert not any("must not invent" in reason for reason in refusals), refusals


def test_an_edited_v1_record_is_not_migratable():
    legacy = v1_ledger(two_pending())
    legacy["records"]["XA0"]["notes"] = "smuggled in after stamping"
    assert any("fingerprint does not match" in reason
               for reason in schema_migration_refusals(legacy))


@pytest.mark.parametrize("row,expected", [
    ("not a record", "not an admission record"),
    (record("XB0", symbol="XA0"), "disagrees with its symbol field"),
    (record("XB0", admission_state="verified"), "unknown admission_state"),
    (record("XB0", admission_state=STATE_BLOCKED), "blocked without a blocked_reason"),
    (record("XB0", notes=None), "notes must be a string"),
    (record("XB0", evidence={}), "evidence must carry exactly"),
    (record("XB0", evidence=dict(v1_blank(), proposed_tick=dict(v1_evidence(1),
                                                               source_url="not a url"))),
     "source_url"),
    (record("XB0", evidence=dict(v1_blank(), proposed_tick=dict(v1_evidence(1),
                                                               published_date="2026-13-01"))),
     "published_date"),
    (record("XB0", evidence=dict(v1_blank(), proposed_tick=dict(v1_evidence("1")))),
     "finite positive number"),
    (record("XB0", evidence=dict(v1_blank(), proposed_tick=v1_blank_evidence())),
     "empty evidence record"),
    # A v1 record may not carry the v2 provenance fields: a document holding them
    # is not the schema it declares, and its citations were never judged as v2's.
    (record("XB0", evidence=dict(v1_blank(), proposed_tick=dict(
        v1_evidence(1), source_authority=SOURCE_AUTHORITY_EXCHANGE_PRIMARY))),
     "unsupported fields"),
])
def test_a_v1_record_outside_the_b1a_shape_is_not_migratable(row, expected):
    """The legacy records are read under B1a's rules, not waved through.

    Under B1a's rules, not today's: a legacy citation is held to its URL and never
    asked for an authority or a kind, because demanding fields the schema did not
    have would report every real B1a citation as malformed instead of as what it
    is -- material a migration must not invent fields for.
    """
    legacy = v1_ledger({"XA0": record("XA0"), "XB0": row})
    refusals = schema_migration_refusals(legacy)
    assert any(expected in reason for reason in refusals), refusals
    assert not any("must not invent" in reason for reason in refusals), refusals


def test_a_valid_b1a_citation_is_read_as_prepared_material_not_as_malformed():
    """The one case that must reach the "do not invent provenance" refusal.

    A complete B1a citation -- value, URL, title, publication date, reviewer -- is
    a structurally valid legacy record.  What stops the migration is that nobody
    recorded who published the document or what it is, which a rebuild may not
    decide on their behalf.
    """
    legacy = v1_ledger({"XA0": record("XA0", evidence=dict(v1_blank(),
                                                           proposed_tick=v1_evidence(1))),
                        "XB0": record("XB0")})
    assert legacy_ledger_problems(legacy) == [], "a valid B1a ledger, by B1a's rules"
    refusals = schema_migration_refusals(legacy)
    assert any("must not invent" in reason for reason in refusals), refusals
    assert any("holds prepared material" in reason for reason in refusals), refusals


def test_a_non_mapping_document_is_never_migratable():
    """Nothing that is not a mapping can declare a schema, so it is refused twice over."""
    for payload in ([], "ledger", None, 7, [v1_ledger(two_pending())]):
        assert any("cannot be migrated" in reason for reason in schema_migration_refusals(payload))
        assert legacy_ledger_problems(payload) == ["legacy ledger must be a document mapping"]


@pytest.mark.parametrize("wreck,expected", [
    (lambda doc: tampered(doc, prepared_at="2027-01-01"), "fingerprint does not match"),
    (lambda doc: tampered(doc, **{FINGERPRINT_FIELD: "sha256:0"}), "fingerprint does not match"),
    (lambda doc: dict(doc, kind="contracts"), "not an admission ledger"),
    (lambda doc: dict(doc, records=[]), "records mapping"),
    (lambda doc: {k: v for k, v in doc.items() if k != "catalogue_content_hash"},
     "missing required fields"),
    (lambda doc: dict(doc, count=99), "count must equal"),
])
def test_the_builder_leaves_a_broken_v1_ledger_byte_for_byte(tmp_path, capsys, wreck, expected):
    """A claim of version 1 is not a licence to rewrite the file it is written in."""
    argv, out, cat_path, pol_path = build_paths(tmp_path, catalogue_doc())
    before = put_ledger(out, wreck(v1_ledger(two_pending(), catalogue_path=cat_path)))

    assert builder.main(argv) == 3

    assert out.read_bytes() == before, "the file must be left exactly as it was"
    err = capsys.readouterr().err
    assert expected in err and "left unchanged" in err
    # And nothing downstream treats it as readable either.
    assert builder.main(argv + ["--check"]) == 1
    assert load_ledger_projection(out, load_catalogue(cat_path), str(cat_path),
                                  load_policy(pol_path), str(pol_path))["available"] is False
    assert out.read_bytes() == before


def test_a_duplicate_key_in_a_v1_ledger_is_refused_without_a_rewrite(tmp_path, capsys):
    """Two intentions for one candidate, and json.load would keep the second."""
    argv, out, cat_path, _ = build_paths(tmp_path, catalogue_doc())
    text = json.dumps(v1_ledger(two_pending(), catalogue_path=cat_path), ensure_ascii=False)
    out.write_text(text.replace('"XA0": {', '"XA0": {"symbol": "XA0"}, "XA0": {', 1),
                   encoding="utf-8")
    before = out.read_bytes()

    assert builder.main(argv) == 1

    assert out.read_bytes() == before, "the file must be left exactly as it was"
    assert "duplicate key" in capsys.readouterr().err


def test_a_non_mapping_ledger_file_is_refused_without_a_rewrite(tmp_path, capsys):
    argv, out, cat_path, _ = build_paths(tmp_path, catalogue_doc())
    out.write_text(json.dumps([v1_ledger(two_pending(), catalogue_path=cat_path)],
                              ensure_ascii=False), encoding="utf-8")
    before = out.read_bytes()

    assert builder.main(argv) == 1

    assert out.read_bytes() == before, "the file must be left exactly as it was"
    assert "document mapping" in capsys.readouterr().err


# ------------------------------------------------------------- projection ---

def test_the_projection_reports_coverage_and_state_but_never_a_value():
    records = {"XA0": reviewable("XA0"), "XB0": record("XB0")}
    view = projected(stamped(records))

    assert view["available"] is True and view["count"] == 2
    assert view["counts"] == {STATE_PENDING: 1, STATE_BLOCKED: 0, STATE_READY_FOR_REVIEW: 1}
    rows = {r["symbol"]: r for r in view["records"]}
    assert rows["XA0"]["evidence_recorded"] == 5 and rows["XA0"]["evidence_required"] == 5
    assert rows["XB0"]["evidence_recorded"] == 0
    assert rows["XA0"]["terms"] == {term: REVIEW_RECORDED for term in EVIDENCE_TERMS}
    assert rows["XB0"]["terms"] == {term: REVIEW_UNREVIEWED for term in EVIDENCE_TERMS}
    # No proposed value, no citation, no execution key reaches the projection.
    blob = json.dumps(view, ensure_ascii=False)
    for leaked in ("0.0001", "0.13", "09:00", "26:30", SOURCE_URL, SOURCE_TITLE, "reviewer-a"):
        assert leaked not in blob
    assert forbidden_keys_in(view) == set()


def test_the_projection_never_discloses_the_provenance_it_proved():
    """Passing the gate is not approval, and an official hostname next to a code reads
    as approval.  The policy's path, hosts, kinds and authority all stay in the files."""
    view = projected(stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")}))
    assert view["available"] is True
    blob = json.dumps(view, ensure_ascii=False)
    for leaked in (SOURCE_AUTHORITY_EXCHANGE_PRIMARY, KIND_CONTRACT, KIND_MARGIN_RULE,
                   KIND_FEE_RULE, KIND_SESSION_NOTICE, DEFAULT_SOURCE_POLICY_FILE,
                   "source_policy_path", "exchange_hosts", FIXTURE_HOSTS[0], FIXTURE_HOSTS[1],
                   "source_kind", "source_authority", "source_url", "verified", "tradable"):
        assert leaked not in blob, leaked
    # Not even an opaque digest of the policy: a version plus a fingerprint is
    # still the pair a forged ledger has to reproduce, published to anyone with
    # the dashboard open.
    assert "source_policy_schema_version" not in view
    assert policy_doc()[POLICY_FINGERPRINT_FIELD] not in blob


def test_the_public_projection_is_exactly_coverage_and_state():
    """The shape is a stated contract, asserted key for key rather than by sampling.

    Sampling for known-bad substrings cannot catch the field somebody adds next
    week, which is how the bindings reached the browser in the first place.
    """
    for view in (projected(stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})),
                 projected("not a ledger")):
        assert set(view) == set(PROJECTION_FIELDS)
        assert set(view) == {"available", "note", "evidence_terms", "count", "counts", "records"}
        for row in view["records"]:
            assert set(row) == set(PROJECTION_RECORD_FIELDS)
            assert set(row) == {"symbol", "admission_state", "evidence_recorded",
                                "evidence_required", "terms"}
            assert set(row["terms"]) == set(EVIDENCE_TERMS)
    # Available and unavailable are the same shape, so the key set never says
    # which check refused the file.
    assert set(projected(stamped(two_pending()))) == set(projected(stamped({"XA0": record("XA0")})))


def test_the_projection_never_reports_the_bindings_it_proved():
    """Every binding is re-proved on each read and none of it is published.

    Each of these is an internal integrity anchor: the declared paths, the
    catalogue's schema/hash/count, the policy's schema and digest, and the
    ledger's own digest.  They decide whether the file may be believed; serving
    them tells a reader exactly what a substituted ledger has to say.
    """
    doc = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})
    # All seven are in the file, so the assertions below are not vacuous.
    for field in LEDGER_BINDING_FIELDS + (FINGERPRINT_FIELD,):
        assert doc[field] not in ("", None), field
    for view in (projected(doc), projected(doc, catalogue_path="config/elsewhere.json")):
        blob = json.dumps(view, ensure_ascii=False)
        for field in LEDGER_BINDING_FIELDS + (FINGERPRINT_FIELD,):
            assert field not in view, field
            assert field not in blob, field
        # The two schema/count bindings are small integers, so a substring test on
        # their values would only rediscover the digits in the coverage counts:
        # key absence above is what proves those two are gone.  The rest are
        # distinctive enough to search for by value.
        for field in OPAQUE_BINDINGS:
            assert str(doc[field]) not in blob, field
        for value in (DEFAULT_UNIVERSE_FILE, DEFAULT_SOURCE_POLICY_FILE,
                      catalogue_doc()["content_hash"], policy_doc()[POLICY_FINGERPRINT_FIELD],
                      doc[FINGERPRINT_FIELD]):
            assert value not in blob, value


def test_the_projection_never_reports_a_block_reason_or_a_preparation_date():
    """Two free-form fields nothing in the schema constrains stay in the file."""
    blocked = record("XA0", admission_state=STATE_BLOCKED,
                     blocked_reason="reviewer note: <script>alert(1)</script> 联系张三 2026",
                     prepared_by="reviewer-a", prepared_at="2026-09-19",
                     notes="internal: ticket ARENA-1234")
    doc = stamped({"XA0": blocked, "XB0": record("XB0")}, prepared_at="2026-09-19")
    view = projected(doc)
    assert view["available"] is True and view["counts"][STATE_BLOCKED] == 1
    blob = json.dumps(view, ensure_ascii=False)
    for leaked in ("blocked_reason", "prepared_at", "prepared_by", "notes", "reviewer note",
                   "<script>", "张三", "reviewer-a", "ARENA-1234", "2026-09-19"):
        assert leaked not in blob, leaked
    # The row still says the candidate is blocked -- that is the whole finding.
    assert view["records"][0]["admission_state"] == STATE_BLOCKED


def test_the_projection_fails_closed_without_a_provable_catalogue_link():
    records = two_pending()
    unavailable = {"available": False, "candidates": [], "content_hash": ""}
    assert projected(stamped(records), catalogue=unavailable)["available"] is False
    # Same catalogue, drifted ledger: refused rather than shown as partial coverage.
    assert projected(stamped({"XA0": record("XA0")}))["available"] is False
    assert projected(stamped(records, catalogue_content_hash="sha256:other"))["available"] is False
    assert projected(stamped(records), catalogue=catalogue_view(schema_version=2))["available"] is False
    assert projected(stamped(records), catalogue=catalogue_view(("XA0", "XB0", "XC0")))["available"] is False
    assert projected(stamped(records), catalogue_path="config/elsewhere.json")["available"] is False


def test_the_projection_fails_closed_without_a_source_policy():
    """A host list that could not be loaded makes nothing in the file more trustworthy."""
    doc = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})
    for absent in (None, {}):
        view = project_ledger(doc, catalogue_view(), DEFAULT_UNIVERSE_FILE, absent,
                              DEFAULT_SOURCE_POLICY_FILE)
        assert view["available"] is False and view["records"] == []
        assert "XA0" not in json.dumps(view, ensure_ascii=False)
    # And a policy path the ledger does not declare is refused the same way.
    assert projected(doc, policy_path=None)["available"] is False


def test_the_projection_of_a_tampered_ledger_exposes_nothing():
    doc = stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")})
    doc["records"]["XA0"]["notes"] = "smuggled in after stamping"
    view = projected(doc)
    assert view == projected("not a ledger")
    assert view["available"] is False and view["records"] == [] and view["counts"] == {
        STATE_PENDING: 0, STATE_BLOCKED: 0, STATE_READY_FOR_REVIEW: 0}
    assert "smuggled" not in json.dumps(view, ensure_ascii=False)


def test_a_missing_ledger_file_degrades_to_an_empty_projection(tmp_path):
    view = load_ledger_projection(tmp_path / "absent.json", catalogue_view(),
                                  DEFAULT_UNIVERSE_FILE, policy_doc(), DEFAULT_SOURCE_POLICY_FILE)
    assert view["available"] is False and view["note"] == LEDGER_DISCLAIMER


def test_the_committed_ledger_projects_beside_the_committed_catalogue_and_policy():
    view = load_ledger_projection(LEDGER, load_catalogue(CATALOGUE), DEFAULT_UNIVERSE_FILE,
                                  load_policy(POLICY), DEFAULT_SOURCE_POLICY_FILE)
    assert view["available"] is True
    assert view["counts"][STATE_PENDING] == view["count"] == 83
    assert view["counts"][STATE_READY_FOR_REVIEW] == 0
    assert set(view) == set(PROJECTION_FIELDS)
    # The committed bindings were proved to get here and none of them is served.
    blob = json.dumps(view, ensure_ascii=False)
    for field in LEDGER_BINDING_FIELDS + (FINGERPRINT_FIELD,):
        assert field not in blob, field
    assert load_policy(POLICY)[POLICY_FINGERPRINT_FIELD] not in blob


# -------------------------------------------------------------- HTTP view ---

@pytest.fixture
def client(tmp_path):
    cfg = make_cfg(tmp_path, ["RB0"], db_name="admission-api.db")
    db = Database(cfg.db_path)
    engine = Engine(cfg, db, accelerate=True)
    try:
        yield TestClient(create_app(engine, ROOT / "web")), engine
    finally:
        engine.stop()
        db.close()


@pytest.fixture
def offline(monkeypatch):
    """The ledger is a local artefact: reading it must never reach the network."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("the admission ledger must never touch the network")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def write_trio(base: Path, catalogue: dict, ledger: dict, policy: dict | None = None) -> None:
    """The three local artefacts the endpoint reads, at the paths it declares."""
    (base / "config").mkdir(parents=True, exist_ok=True)
    (base / DEFAULT_UNIVERSE_FILE).write_text(json.dumps(catalogue, ensure_ascii=False), encoding="utf-8")
    (base / DEFAULT_LEDGER_FILE).write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    (base / DEFAULT_SOURCE_POLICY_FILE).write_text(
        json.dumps(policy if policy is not None else policy_doc(), ensure_ascii=False), encoding="utf-8")


def test_the_endpoint_serves_coverage_without_terms_or_permissions(client, tmp_path, offline):
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_trio(tmp_path, catalogue_doc(), stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")}))

    response = api.get("/api/universe/admission")

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["available"] is True and body["count"] == 2
    assert body["counts"][STATE_READY_FOR_REVIEW] == 1
    assert set(body) == set(PROJECTION_FIELDS)
    for row in body["records"]:
        assert set(row) == set(PROJECTION_RECORD_FIELDS)
    for leaked in ("0.0001", "0.13", "09:00", SOURCE_URL, "reviewer-a", '"multiplier"', "verified",
                   "tradable", SOURCE_AUTHORITY_EXCHANGE_PRIMARY, KIND_CONTRACT, KIND_MARGIN_RULE,
                   FIXTURE_HOSTS[0], "exchange_hosts", DEFAULT_SOURCE_POLICY_FILE):
        assert leaked not in response.text, leaked
    assert forbidden_keys_in(body) == set()


def test_the_endpoint_publishes_no_binding_block_reason_or_preparation_date(client, tmp_path,
                                                                           offline):
    """The wire has the same no-leak rule as the projection, proved over HTTP.

    The ledger written here is the worst case for it: one blocked record whose
    reason is free-form reviewer prose, one fully cited record, and a header
    carrying all seven bindings the read had to prove.
    """
    api, engine = client
    engine.cfg.base_dir = tmp_path
    ledger = stamped({"XA0": reviewable("XA0"),
                      "XB0": record("XB0", admission_state=STATE_BLOCKED,
                                    blocked_reason="内部备注 ARENA-1234 <script>alert(1)</script>",
                                    notes="ask reviewer-b")})
    write_trio(tmp_path, catalogue_doc(), ledger)

    available = api.get("/api/universe/admission")
    # And the refusal path, which must not differ in shape or say more.
    (tmp_path / DEFAULT_SOURCE_POLICY_FILE).unlink()
    unavailable = api.get("/api/universe/admission")

    assert available.json()["available"] is True
    assert unavailable.json()["available"] is False
    assert set(available.json()) == set(unavailable.json()) == set(PROJECTION_FIELDS)
    for response in (available, unavailable):
        for field in LEDGER_BINDING_FIELDS + (FINGERPRINT_FIELD,):
            assert field not in response.text, field
        for field in OPAQUE_BINDINGS:                 # the two integers: key absence above
            assert str(ledger[field]) not in response.text, field
        for leaked in ("blocked_reason", "prepared_at", "prepared_by", "notes", "ARENA-1234",
                       "<script>", "内部备注", "reviewer-b", "2026-09-19",
                       catalogue_doc()["content_hash"], policy_doc()[POLICY_FINGERPRINT_FIELD]):
            assert leaked not in response.text, leaked


def test_the_endpoint_fails_closed_on_a_drifted_or_broken_ledger(client, tmp_path, offline):
    api, engine = client
    engine.cfg.base_dir = tmp_path
    # A ledger that no longer matches the catalogue it names.
    write_trio(tmp_path, catalogue_doc(), stamped({"XA0": record("XA0")}))
    assert api.get("/api/universe/admission").json()["available"] is False
    # An unparseable ledger.
    (tmp_path / DEFAULT_LEDGER_FILE).write_text("{not json", encoding="utf-8")
    assert api.get("/api/universe/admission").json()["available"] is False
    # A valid ledger beside a catalogue that fails its own validation.
    broken = catalogue_doc()
    broken["candidates"]["XA0"]["multiplier"] = 10
    broken["content_hash"] = content_hash(broken["candidates"])
    write_trio(tmp_path, broken, stamped(two_pending()))
    response = api.get("/api/universe/admission")
    assert response.json()["available"] is False
    assert "XA0" not in response.text and '"multiplier"' not in response.text


@pytest.mark.parametrize("break_policy", [
    lambda path: path.write_text("{not json", encoding="utf-8"),
    lambda path: path.write_text(json.dumps(dict(policy_doc(), exchange_hosts={})), encoding="utf-8"),
    lambda path: path.write_text(json.dumps(policy_doc({FIXTURE_EXCHANGE: ["cdn.attacker.example"]})),
                                 encoding="utf-8"),
    lambda path: path.unlink(),
])
def test_the_endpoint_fails_closed_on_a_broken_or_moved_policy(client, tmp_path, offline,
                                                              break_policy):
    """A policy that is missing, invalid or no longer the one bound is a refusal."""
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_trio(tmp_path, catalogue_doc(), stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")}))
    assert api.get("/api/universe/admission").json()["available"] is True

    break_policy(tmp_path / DEFAULT_SOURCE_POLICY_FILE)

    response = api.get("/api/universe/admission")
    assert response.json()["available"] is False
    for leaked in ("XA0", "XB0", SOURCE_URL, "reviewer-a", "0.0001", "attacker"):
        assert leaked not in response.text, leaked


def test_the_endpoint_reproves_the_policy_on_disk_not_one_it_remembered(client, tmp_path, offline):
    """A same-length policy with its modification time restored is still another policy.

    Path, size and modification time are metadata; the binding the endpoint claims
    to re-prove is about *content*.  Keyed on the three, a cache answers from a
    policy that is no longer on disk -- and the read then reports a ledger as bound
    to a host list this process never opened, which is precisely the substitution
    the policy binding exists to catch.  The file is small and the endpoint is low
    frequency, so it is read and validated on every call.
    """
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_trio(tmp_path, catalogue_doc(), stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")}))
    path = tmp_path / DEFAULT_SOURCE_POLICY_FILE
    assert api.get("/api/universe/admission").json()["available"] is True
    before = path.stat()

    # Another *valid* policy of exactly the same serialized length: one host label
    # swapped for one the same width, so only the content differs.
    substitute = policy_doc({FIXTURE_EXCHANGE: ["rules.example.org", FIXTURE_HOSTS[1]]})
    text = json.dumps(substitute, ensure_ascii=False)
    assert len(text.encode("utf-8")) == before.st_size, "the substitution must preserve the size"
    assert substitute[POLICY_FINGERPRINT_FIELD] != policy_doc()[POLICY_FINGERPRINT_FIELD]
    assert load_policy_or_none(path)[POLICY_FINGERPRINT_FIELD] != substitute[POLICY_FINGERPRINT_FIELD]
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns), \
        "the replacement must be indistinguishable by file metadata"
    # The document really did change, and the new one is valid on its own terms.
    assert load_policy_or_none(path)[POLICY_FINGERPRINT_FIELD] == substitute[POLICY_FINGERPRINT_FIELD]

    response = api.get("/api/universe/admission")

    assert response.json()["available"] is False, \
        "the ledger's policy binding no longer describes the policy on disk"
    assert set(response.json()) == set(PROJECTION_FIELDS)
    for leaked in ("XA0", "XB0", SOURCE_URL, "reviewer-a", "0.0001", "rules.example.org"):
        assert leaked not in response.text, leaked


def substitute_catalogue() -> dict:
    """A different, equally valid catalogue of exactly the fixture's serialized length.

    One letter of each candidate's descriptive name is swapped, so the candidate
    set, the schema and the counts are untouched and only the *content* -- and
    therefore ``content_hash``, which the ledger is bound to -- differs.  The hash
    is a fixed-width digest and the names keep their width, so the file is the
    same number of bytes as the one it replaces.
    """
    doc = catalogue_doc()
    for entry in doc["candidates"].values():
        entry["name"] = entry["name"].replace("contract", "contracX")
    doc["content_hash"] = content_hash(doc["candidates"])
    return doc


def test_the_endpoint_reproves_the_catalogue_on_disk_not_one_it_remembered(client, tmp_path,
                                                                          offline):
    """A same-length catalogue with its modification time restored is still another snapshot.

    The discovery endpoint may answer a catalogue out of a cache keyed on path,
    modification time and size: a stale listing there costs a stale listing.  The
    admission endpoint may not.  Its whole claim is that the ledger in front of it
    describes the snapshot that is *on disk*, and path, size and modification time
    are metadata -- all three survive a replacement whose content differs, which is
    exactly the substitution the catalogue binding exists to catch.  So the file is
    read and validated on every admission request, and the ledger bound to the
    previous content fails closed.
    """
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_trio(tmp_path, catalogue_doc(), stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")}))
    path = tmp_path / DEFAULT_UNIVERSE_FILE
    assert api.get("/api/universe/admission").json()["available"] is True
    before = path.stat()

    substitute = substitute_catalogue()
    text = json.dumps(substitute, ensure_ascii=False)
    assert len(text.encode("utf-8")) == before.st_size, "the substitution must preserve the size"
    assert substitute["content_hash"] != catalogue_doc()["content_hash"]
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns), \
        "the replacement must be indistinguishable by file metadata"
    # The document really did change, and the new one is valid on its own terms --
    # so the refusal below is about the binding, not about an unreadable file.
    fresh = load_catalogue(path)
    assert fresh["available"] is True
    assert fresh["content_hash"] == substitute["content_hash"]

    response = api.get("/api/universe/admission")

    assert response.json()["available"] is False, \
        "the ledger's catalogue binding no longer describes the catalogue on disk"
    assert set(response.json()) == set(PROJECTION_FIELDS)
    for row in response.json()["records"]:
        assert set(row) == set(PROJECTION_RECORD_FIELDS)
    assert response.json()["records"] == [] and response.json()["count"] == 0
    for leaked in ("XA0", "XB0", "contracX", FIXTURE_EXCHANGE, SOURCE_URL, FIXTURE_HOSTS[0],
                   "reviewer-a", "0.0001", "0.13", DEFAULT_SOURCE_POLICY_FILE,
                   catalogue_doc()["content_hash"], substitute["content_hash"],
                   policy_doc()[POLICY_FINGERPRINT_FIELD]):
        assert leaked not in response.text, leaked
    for field in LEDGER_BINDING_FIELDS + (FINGERPRINT_FIELD,):
        assert field not in response.text, field


def test_the_admission_proof_does_not_lean_on_the_discovery_catalogue_cache(client, tmp_path,
                                                                           offline):
    """The metadata-keyed cache may still be warm and wrong; the proof reads past it.

    Primed through ``/api/universe`` first, so the cache genuinely holds the
    superseded snapshot at the moment the admission request arrives.  That the
    discovery endpoint keeps serving it is the cache working as intended; that the
    admission endpoint does not is the point.
    """
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_trio(tmp_path, catalogue_doc(), stamped({"XA0": reviewable("XA0"), "XB0": record("XB0")}))
    path = tmp_path / DEFAULT_UNIVERSE_FILE
    assert api.get("/api/universe").json()["count"] == 2      # the cache is now warm
    assert api.get("/api/universe/admission").json()["available"] is True
    before = path.stat()

    path.write_text(json.dumps(substitute_catalogue(), ensure_ascii=False), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    # The cached view is untouched by the substitution -- metadata is all it looks
    # at -- so an admission route reading through it would have proved the binding
    # against a snapshot this process never opened.
    cached = api.get("/api/universe").json()
    assert [c["name"] for c in cached["candidates"]] == [f"{s} test contract" for s in FIXTURE_SYMBOLS]
    assert cached["content_hash"] == catalogue_doc()["content_hash"]

    assert api.get("/api/universe/admission").json()["available"] is False


def test_the_ledger_stays_out_of_the_state_poll(client, tmp_path):
    api, engine = client
    engine.cfg.base_dir = tmp_path
    write_trio(tmp_path, catalogue_doc(), stamped(two_pending()))
    state = api.get("/api/state").json()
    assert "admission" not in state and "ledger" not in state
    assert "policy" not in state and "source_policy" not in json.dumps(state, ensure_ascii=False)
    assert "XA0" not in api.get("/api/state").text


def test_the_dashboard_speaks_of_preparation_not_permission():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "<th>准入台账</th>" in html and "<th>证据覆盖</th>" in html
    labels = app_js[app_js.index("const admissionLabel"):app_js.index("function renderUniverse")]
    for claim in ("可交易", "已核验", "核验通过", "可下单", "tradable", "verified"):
        assert claim not in labels
    for wording in ("待人工复核", "已阻断", "未开始", "无台账"):
        assert wording in labels


# --------------------------------------------------------------- isolation --

def ledger_only_symbols():
    """Codes the ledger prepares that have no local specification."""
    table, _ = load_cn_contracts(CONTRACTS)
    symbols = [s for s in read_ledger(LEDGER)["records"] if s not in table]
    assert symbols, "the ledger must hold codes with no local terms, or these checks are vacuous"
    return symbols


def test_ledger_codes_never_reach_engine_exchange_risk_or_prompt(tmp_path):
    cfg = make_cfg(tmp_path, DEMO_SYMBOLS, db_name="admission-iso.db")
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        only = set(ledger_only_symbols())
        for specs in (eng.specs, eng.exchange.specs, eng.risk.specs, eng._all_specs):
            assert not (only & set(specs)), "a ledger code must not appear in any execution-facing spec table"
        assert set(eng.source.symbols) == set(DEMO_SYMBOLS)
        eng._tick()
        assert not (only & set(eng._build_snapshot().symbols)), "a ledger code must not reach the prompt"
    finally:
        eng.stop()
        db.close()


def test_ledger_codes_are_never_fetched(tmp_path):
    cfg = make_cfg(tmp_path, DEMO_SYMBOLS, db_name="admission-fetch.db")
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        asked = []
        real_latest, real_klines = eng.source.latest, eng.source.klines
        real_snapshot = eng.source.latest_with_snapshot
        eng.source.latest = lambda syms, **kw: (asked.extend(syms), real_latest(syms, **kw))[1]
        eng.source.latest_with_snapshot = lambda syms, **kw: (asked.extend(syms), real_snapshot(syms, **kw))[1]
        eng.source.klines = lambda sym, *a, **kw: (asked.append(sym), real_klines(sym, *a, **kw))[1]
        eng._tick()
        eng._build_snapshot()
        assert asked, "this test must observe real fetches or it proves nothing"
        assert set(asked) <= set(DEMO_SYMBOLS)
    finally:
        eng.stop()
        db.close()


def test_a_prepared_candidate_still_fails_b0s_admission_gate(tmp_path):
    """Being in the ledger -- at any state -- is not a local specification."""
    candidate_code = ledger_only_symbols()[0]
    cfg = make_cfg(tmp_path, ["RB0", candidate_code], db_name="admission-gate.db")
    db = Database(cfg.db_path)
    with pytest.raises(ContractSpecError) as err:
        Engine(cfg, db, accelerate=True)
    assert err.value.symbols == [candidate_code]
    assert db.list_competitions() == []
    db.close()


@pytest.mark.parametrize("package", ["engine", "exchange", "market", "traders"])
def test_no_execution_path_imports_the_ledger(package):
    """Inertness proved at the import graph, not only at runtime."""
    for module in (ROOT / "arena" / package).glob("*.py"):
        text = module.read_text(encoding="utf-8")
        for reach in ("admission import", "import admission", "arena.admission",
                      "from ..admission", "from .admission", "admission_source_policy",
                      "source_policy import"):
            assert reach not in text, f"{module.name} must not reach the admission ledger"


@pytest.mark.parametrize("module", ["admission.py", "admission_source_policy.py"])
def test_the_ledger_modules_do_not_import_the_execution_path(module):
    text = (ROOT / "arena" / module).read_text(encoding="utf-8")
    for forbidden in ("from .engine", "from .exchange", "from .market", "from .traders",
                      "import arena.engine", "import arena.exchange"):
        assert forbidden not in text


def test_the_policy_module_reaches_no_network_api_at_all():
    """A provenance check that could resolve a name could be answered by the network."""
    text = (ROOT / "arena" / "admission_source_policy.py").read_text(encoding="utf-8")
    for forbidden in ("import socket", "import http", "httpx", "requests", "urlopen",
                      "urlretrieve", "getaddrinfo", "gethostbyname", "resolve(", "subprocess"):
        assert forbidden not in text, forbidden
