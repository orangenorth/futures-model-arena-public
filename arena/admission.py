"""Non-executable admission ledger: where a candidate's term evidence is prepared.

B0 (``arena/exchange/contracts.py``) proved a *local structural* property -- an
active symbol may not run unless ``config/contracts_cn.json`` states every
execution term explicitly.  It cannot prove those terms are the exchange's.
This module is the layer that comes *before* that gate: a place to record,
review and report the public evidence behind a candidate's multiplier, tick,
sessions, margin assumption and fee assumption, without any of it becoming
executable and without any of it claiming exchange verification.

Four properties keep that safe:

  * **The ledger cannot be read as a contract row.**  Every term key is prefixed
    ``proposed_``, and the whole document is swept with
    ``universe.forbidden_keys_in``; a file carrying ``multiplier`` / ``tick`` /
    ``margin_ratio`` / ``fee_*`` / ``sessions`` / ``verified`` is refused whole.
    So a ledger spliced into the contract table, or mistaken for one, produces a
    validation error rather than a spec.
  * **No state in this vocabulary means admitted.**  The strongest value is
    ``ready_for_review``: evidence is assembled and a human has not looked at it
    yet.  There is no ``verified``, ``admitted`` or ``tradable``.
  * **Unknown stays unknown, and says so exactly one way.**  A term with no
    evidence is ``None``; a gathered source with no extracted number keeps
    ``value: null`` beside a complete citation.  There is no third state: a
    dictionary with a null value and every citation field blank records nothing
    and is refused, because coverage counts slots and would read it as a gathered
    source.  Nothing is defaulted -- inventing a plausible multiplier is exactly
    the failure B0 exists to stop.
  * **A citation has to name a first-party source.**  Being well formed is not
    enough: every slot that asserts anything -- a value, a title, a date --
    declares a ``source_url``, a ``source_authority`` and a ``source_kind``, and
    ``arena/admission_source_policy.py`` checks them against
    the catalogue candidate's own exchange and a committed list of that
    exchange's exact hostnames.  A margin ratio read off a vendor page, a
    multiplier read off a news article, an ``http`` link, a lookalike host or a
    fee cited from a contract specification are all refused -- offline, by string
    comparison, with no network request anywhere in the path.
  * **Evidence cannot be re-pointed.**  The fingerprint covers the whole
    document, header included, so the declared catalogue path, schema, content
    hash and candidate count the evidence was gathered against -- and the
    declared path, schema and fingerprint of the source policy it was gathered
    under -- are sealed together with the evidence.  Editing either binding
    invalidates the ledger, and the builder refuses to rebind a ledger that holds
    prepared material at all: moving evidence from one snapshot, one file or one
    host policy to another is a human decision, never a rebuild.
  * **It is inert.**  Nothing in the engine, exchange, risk, market, trader or
    prompt paths imports this module; the only consumer is the read-only
    dashboard projection, which reports coverage and state and never a value.
  * **The bindings are proved, not published.**  Every check above runs on every
    read, but its inputs stay in the file: the projection emits coverage and
    state only, so no declared path, schema version, content hash, candidate
    count, policy digest, ledger digest, block reason or preparation date
    reaches a browser.  An internal integrity anchor rendered on a page is an
    invitation to reason about which anchor would make a record acceptable.

Validation is the single gate for both directions: the builder script calls it
before writing, the projection calls it before exposing.  A document that fails
is reported unavailable in full -- never partially projected, since dropping the
offending record and serving the rest would hide the edit.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import namedtuple
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

# The term and provenance vocabulary is defined once, next to the rules that give
# it meaning, and re-exported here: a caller assembling or reading a record works
# in terms of ledger records, so ``arena.admission`` stays the single import.
from .admission_source_policy import (  # noqa: F401  (re-exported vocabulary)
    DATED_TERMS, DEFAULT_SOURCE_POLICY_FILE, EVIDENCE_TERMS, POLICY_BINDING_FIELDS,
    SOURCE_AUTHORITIES, SOURCE_AUTHORITY_EXCHANGE_PRIMARY, SOURCE_KINDS, TERM_FEE, TERM_MARGIN,
    TERM_MULTIPLIER, TERM_SESSIONS, TERM_SOURCE_KINDS, TERM_TICK, SourcePolicyError,
    citation_problem, normalized_declared_path, policy_binding, policy_binding_mismatches,
    transport_problem, venue_problem,
)
from .universe import DEFAULT_UNIVERSE_FILE, SYMBOL_PATTERN, forbidden_keys_in

DEFAULT_LEDGER_FILE = "config/contracts_admission_ledger_cn.json"
# v2 added the source-policy binding and the two provenance fields every citation
# now has to carry.  v1 ledgers are accepted for *migration only*, and only when
# they hold nothing prepared -- see ``schema_migration_refusals``.
LEDGER_SCHEMA_VERSION = 2
MIGRATABLE_SCHEMA_VERSIONS = (1,)
# Stamped into the document so a file cannot be mistaken for a catalogue (or the
# reverse) merely because both are symbol-keyed JSON.
LEDGER_KIND = "contract_admission_ledger"

# The five terms B0 refuses to default are
# defined once, in arena/admission_source_policy.py: that is the layer which says
# what kind of published document can establish each one, so the vocabulary and
# its provenance rules cannot drift apart.  Re-exported here because a ledger
# record is the thing readers reach for them through.
#
# The ``proposed_`` prefix is load-bearing: it keeps every key in this file
# outside universe.FORBIDDEN_SPEC_KEYS, so the ledger can never be read as a
# specification by the loader, the matching layer or a careless copy-paste.

# How far a candidate has been *prepared*, never whether it may be traded:
#   pending           -- nothing gathered yet (the honest initial state)
#   blocked           -- preparation cannot proceed; the reason is written down
#   ready_for_review  -- all five terms carry cited evidence, awaiting a human
STATE_PENDING = "pending"
STATE_BLOCKED = "blocked"
STATE_READY_FOR_REVIEW = "ready_for_review"
ADMISSION_STATES = (STATE_PENDING, STATE_BLOCKED, STATE_READY_FOR_REVIEW)

# How far one piece of evidence has been handled:
#   unreviewed    -- a slot exists; the value may still be unknown
#   recorded      -- someone transcribed a value from a cited public document
#   cross_checked -- a second reader confirmed the transcription against it
# Neither of the last two says the exchange confirmed anything to us.
REVIEW_UNREVIEWED = "unreviewed"
REVIEW_RECORDED = "recorded"
REVIEW_CROSS_CHECKED = "cross_checked"
REVIEW_STATES = (REVIEW_UNREVIEWED, REVIEW_RECORDED, REVIEW_CROSS_CHECKED)
# Claiming either of these asserts a citation exists, so the metadata must be
# complete -- that is the "stronger review state" rule below.
SOURCED_REVIEW_STATES = (REVIEW_RECORDED, REVIEW_CROSS_CHECKED)

FINGERPRINT_FIELD = "ledger_fingerprint"
# The catalogue facts a ledger is bound to.  Content hash alone is not enough:
# it covers the candidate block, so a catalogue whose *schema* moved under an
# unchanged candidate set would still match, and a reader comparing counts would
# have nothing to compare against.  Neither is content identity enough on its
# own -- two catalogue files can be byte-identical and still be two files, only
# one of which the evidence was gathered against -- so the declared path
# (``prepared_for``) is bound as well.  All four are re-checked on every read.
BINDING_FIELDS = ("prepared_for", "catalogue_schema_version", "catalogue_content_hash",
                  "catalogue_candidate_count")
# The host policy the evidence was judged against is bound on exactly the same
# terms, and for the same reason: a citation that is first party under one host
# list is not automatically first party under another, so swapping the policy
# under gathered evidence has to be as visible as swapping the catalogue.
LEDGER_BINDING_FIELDS = BINDING_FIELDS + POLICY_BINDING_FIELDS
LEDGER_FIELDS = (("schema_version", "kind", "note", "prepared_at", "catalogue_fetched_at",
                  "count", FINGERPRINT_FIELD, "records") + LEDGER_BINDING_FIELDS)
RECORD_FIELDS = ("symbol", "admission_state", "blocked_reason", "prepared_by",
                 "prepared_at", "notes", "evidence")
# ``source_authority`` and ``source_kind`` are the whole of B1b's addition to a
# record: who published the document, and what the document is.  Nothing wider --
# no confidence score, no reviewer verdict, no "verified" flag.
EVIDENCE_FIELDS = ("value", "source_url", "source_title", "published_date",
                   "effective_date", "review_state", "recorded_by",
                   "source_authority", "source_kind")
# The three fields that identify *where a number came from*: the address, the
# publisher and the type of document.  A slot that asserts anything has to carry
# all three -- two of them describe a document nobody can find.
PROVENANCE_FIELDS = ("source_url", "source_authority", "source_kind")
# Everything else a slot can say about that document.  Filling any one of them is
# claiming a document exists, which is why they demand the core above just as a
# recorded value does: "published 2026-01-15" with no address is an assertion
# about a page nobody can open.
CITATION_METADATA_FIELDS = ("source_title", "published_date", "effective_date", "recorded_by")
CITATION_FIELDS = PROVENANCE_FIELDS + CITATION_METADATA_FIELDS
FEE_FIELDS = ("per_lot", "rate")

# What a B1a (schema_version 1) document was: this ledger without the
# source-policy binding, and this evidence record without the two provenance
# fields.  Written out rather than derived by subtraction, so the older shape
# stays a stated fact after the current one moves again -- and so a v1 document
# can be judged by v1's rules.  Judging it by today's would report every legacy
# citation as malformed, when what it actually is is material a migration must
# not invent fields for.
V1_LEDGER_FIELDS = (("schema_version", "kind", "note", "prepared_at", "catalogue_fetched_at",
                     "count", FINGERPRINT_FIELD, "records") + BINDING_FIELDS)
V1_EVIDENCE_FIELDS = ("value", "source_url", "source_title", "published_date", "effective_date",
                      "review_state", "recorded_by")

# One evidence slot's rules, per schema.  ``fields`` is the exact key set;
# ``core`` is what a slot that claims anything has to carry; ``dated`` says
# whether the margin/fee effective-date rule applies.  B1a had neither the
# provenance pair nor the dated-term rule, so a legacy slot is held to the URL
# alone and its authority and kind are neither demanded nor invented.
EvidenceShape = namedtuple("EvidenceShape", "fields core dated")
CURRENT_EVIDENCE = EvidenceShape(EVIDENCE_FIELDS, PROVENANCE_FIELDS, True)
LEGACY_EVIDENCE = EvidenceShape(V1_EVIDENCE_FIELDS, ("source_url",), False)

# Everything the public projection is allowed to say, listed once so the shape is
# a stated contract rather than whatever the two return statements happen to
# build.  It is deliberately a strict subset of the ledger: coverage and state,
# with no binding, digest, citation, value, reason or date in it.  Both the
# available and the unavailable view carry exactly these keys, so a reader cannot
# tell from the key set alone what went wrong -- and a refusal cannot leak by
# being shaped differently from a success.
PROJECTION_FIELDS = ("available", "note", "evidence_terms", "count", "counts", "records")
PROJECTION_RECORD_FIELDS = ("symbol", "admission_state", "evidence_recorded",
                            "evidence_required", "terms")

_SYMBOL_RE = re.compile(SYMBOL_PATTERN)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Overnight windows are written with 24+ hours (21:00..26:30 closes at 02:30 the
# next day), matching how the contract table writes them.
_SESSION_TIME_RE = re.compile(r"(\d{1,2}):([0-5]\d)")
MAX_SESSION_MINUTE = 48 * 60

DISCLAIMER = (
    "准入台账是候选品种的条款证据准备记录，不是交易所核验结果，也不是可下单授权。"
    "状态只说明证据准备到哪一步：pending 未开始、blocked 已阻断、ready_for_review 待人工复核；"
    "未取得证据的条款一律保持未知，不填默认值。"
    "证据来源只接受所属交易所一手站点的精确域名，且须与条款类型相符；通过来源校验仍不等于已核验或可交易。"
    "台账不参与撮合、风控、行情与模型决策。"
)


class LedgerError(ValueError):
    """The ledger document cannot prove it is an evidence record set and nothing more."""


def ledger_fingerprint(doc: Mapping[str, Any]) -> str:
    """Fingerprint the whole ledger -- every field except the fingerprint itself.

    Hashing only the records would leave the header unprotected, and the header
    is where the evidence gets its meaning: the catalogue schema, hash and count
    it was prepared against, the path it claims to describe, and the source
    policy its citations were judged first party under.  With a record-only
    digest, someone could point an untouched set of gathered evidence at a
    different catalogue snapshot -- or at a policy that lists a host the original
    one refused -- by editing one header line, and every integrity check would
    still pass.  So both bindings are hashed with the evidence, and re-pointing a
    ledger is indistinguishable from forging one.

    ``sort_keys`` makes the digest independent of mapping insertion order, so a
    rebuild that emits the same facts in a different order is the same ledger.
    """
    body = {key: value for key, value in doc.items() if key != FINGERPRINT_FIELD}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalized_catalogue_path(path: Any) -> str:
    """One deterministic spelling for the catalogue path a ledger declares.

    The shared normaliser lives in ``arena/admission_source_policy.py`` so the
    catalogue path and the source-policy path are folded by exactly the same
    rules; see ``normalized_declared_path`` for why the filesystem is never
    consulted.
    """
    return normalized_declared_path(path)


def catalogue_binding(catalogue: Mapping[str, Any], catalogue_path: Any) -> Dict[str, Any]:
    """The immutable facts a ledger is bound to: the snapshot, and where it came from.

    The declared path is part of the evidence's identity, not a caption.  Two
    catalogue files can be byte-identical today and diverge tomorrow, and a
    reviewer who reads "prepared against config/contracts_universe_cn.json"
    needs that to be the file the evidence was actually gathered against -- so
    the path is bound and hashed with everything else.
    """
    return {"prepared_for": normalized_catalogue_path(catalogue_path),
            "catalogue_schema_version": catalogue.get("schema_version"),
            "catalogue_content_hash": catalogue.get("content_hash"),
            "catalogue_candidate_count": catalogue.get("count")}


def empty_evidence() -> Dict[str, None]:
    """A record with nothing gathered: every term present, every term unknown."""
    return {term: None for term in EVIDENCE_TERMS}


def pending_record(symbol: str) -> Dict[str, Any]:
    """The initial, honest state of a candidate: listed, prepared for nothing yet."""
    return {"symbol": symbol, "admission_state": STATE_PENDING, "blocked_reason": "",
            "prepared_by": "", "prepared_at": "", "notes": "", "evidence": empty_evidence()}


def build_ledger(catalogue: Mapping[str, Any], records: Optional[Mapping[str, Any]] = None, *,
                 prepared_at: str = "", catalogue_path: Any = DEFAULT_UNIVERSE_FILE,
                 policy: Mapping[str, Any],
                 policy_path: Any = DEFAULT_SOURCE_POLICY_FILE) -> Dict[str, Any]:
    """Assemble a ledger for exactly the catalogue's candidates, keeping written records.

    A ledger cannot be built without a validated catalogue view *and* a validated
    source policy: both bindings are part of the document's identity, not labels
    added afterwards.  ``policy`` has no default for that reason -- there is no
    sensible implicit host list, and a ledger built without one would accept
    citations nobody had a rule for.

    Symbols the catalogue no longer lists are dropped rather than carried along --
    the ledger's whole value is that it matches one snapshot exactly, and a record
    for a delisted code would make the coverage report a fiction.  Whether it is
    *permissible* to drop them is the caller's decision, not this function's:
    see ``rebind_refusals``.
    """
    if not catalogue.get("available"):
        raise LedgerError("cannot prepare a ledger against an unavailable catalogue")
    if not policy:
        raise LedgerError("cannot prepare a ledger without a validated source policy")
    existing = dict(records or {})
    symbols = [c["symbol"] for c in catalogue.get("candidates", [])]
    entries = {sym: dict(existing.get(sym) or pending_record(sym)) for sym in sorted(set(symbols))}
    doc = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "note": DISCLAIMER,
        # The catalogue this ledger was prepared against, by path *and* by
        # content: the path alone would still match after the file was rewritten,
        # and the content alone cannot tell two identical files apart.
        **catalogue_binding(catalogue, catalogue_path),
        # The host policy its citations were judged first party under, bound the
        # same way and for the same reason.
        **policy_binding(policy, policy_path),
        "catalogue_fetched_at": _text(catalogue.get("fetched_at"), 40),
        "prepared_at": prepared_at,
        "count": len(entries),
        # Placed before the records so a human opening the file sees the header
        # first; the digest excludes this key, so the placeholder never counts.
        FINGERPRINT_FIELD: "",
        "records": entries,
    }
    doc[FINGERPRINT_FIELD] = ledger_fingerprint(doc)
    validate_ledger(doc, catalogue=catalogue, catalogue_path=catalogue_path,
                    policy=policy, policy_path=policy_path)
    return doc


# ------------------------------------------------------------- validation ----

def validate_ledger(doc: Any, *, catalogue: Optional[Mapping[str, Any]] = None,
                    catalogue_path: Any = None, policy: Optional[Mapping[str, Any]] = None,
                    policy_path: Any = None) -> None:
    """Prove a whole ledger document is an evidence record set and nothing more.

    Both ends go through this one function: the builder calls it before writing,
    the projection calls it before exposing.  Pass a validated catalogue view
    *and the path it was read from* to additionally prove the ledger belongs to
    that snapshot -- declared path, schema, content hash, candidate count and
    symbol set all have to agree.  Pass a validated source policy and *its*
    declared path to prove the citations were judged under the policy now on
    disk.  A ledger that has drifted on any of them is refused rather than
    reported as partial coverage.

    Neither path is optional alongside its document: a caller that knows which
    file it opened but does not say so is exactly how a ledger came to be
    re-pointed at a different declared source while every other check still
    passed.

    What each argument buys:

      * nothing passed -- structure and vocabulary only.  Shape, types, the
        fingerprint, the value rules, and the authority/kind vocabulary including
        the term/kind matrix, which is fixed in code and therefore always
        checkable.  This is a pre-flight check, not grounds to accept a ledger.
      * ``catalogue`` + ``policy`` -- the full gate.  Every citation's host is
        matched against the exact hosts the policy lists for *that candidate's*
        exchange.  Every acceptance path in the application passes both: the
        builder before writing, ``--check``, and the dashboard projection.

    Raises ``LedgerError`` describing the first violation found.
    """
    if not isinstance(doc, dict):
        raise LedgerError("ledger must be a document mapping")
    extra = set(doc) - set(LEDGER_FIELDS)
    if extra:
        raise LedgerError(f"ledger carries unsupported fields: {sorted(extra)}")
    if doc.get("kind") != LEDGER_KIND:
        raise LedgerError(f"not an admission ledger: kind={doc.get('kind')!r}")
    # By exact type, for the reason the source policy's version is: ``True == 1``
    # and ``2.0 == 2`` in Python, so a document could be read under a schema it
    # never declared.
    if not _exact_schema(doc.get("schema_version"), LEDGER_SCHEMA_VERSION):
        raise LedgerError(f"unsupported ledger schema_version: {doc.get('schema_version')!r}")
    # Swept document-wide and reported first, so an execution term cannot hide at
    # document level or nested inside a stray value, and so the message says what
    # the file actually is.  Nothing in this schema uses those names, so a hit
    # always means the file was hand-edited towards a contract row -- a failure
    # no amount of re-stamping or re-typing can fix.
    banned = forbidden_keys_in(doc)
    if banned:
        raise LedgerError(f"ledger carries execution terms: {sorted(banned)}")
    # The header is hashed with the records, so it has to be well typed before
    # the digest means anything -- and the binding comparisons below compare
    # these values field by field, never as whatever str() would make of them.
    for field in ("note", "prepared_for", "prepared_at", "catalogue_fetched_at",
                  "catalogue_content_hash", "source_policy_path", "source_policy_fingerprint",
                  FINGERPRINT_FIELD):
        if not isinstance(doc.get(field), str):
            raise LedgerError(f"ledger {field} must be a string")
    for field in ("catalogue_schema_version", "catalogue_candidate_count",
                  "source_policy_schema_version"):
        value = doc.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LedgerError(f"ledger {field} must be a non-negative integer (got {value!r})")
    records = doc.get("records")
    # A non-mapping records value is rejected rather than treated as empty:
    # ``records: []`` fingerprints perfectly well and would otherwise be served
    # as a complete ledger of nothing.
    if not isinstance(records, dict):
        raise LedgerError("ledger must carry a records mapping")
    if doc.get("count") != len(records):
        raise LedgerError("ledger count must equal the number of records")
    # Before the records, because it is what says the document has not been
    # rewritten since it was stamped -- and after it, the binding comparisons
    # decide *which* policy and *which* snapshot the records are judged against.
    if doc.get(FINGERPRINT_FIELD) != ledger_fingerprint(doc):
        raise LedgerError("ledger_fingerprint does not match the ledger body "
                          "(records, the catalogue binding or the source policy binding "
                          "were edited after it was stamped)")
    if policy is not None:
        if policy_path is None:
            raise LedgerError("a source policy check needs the path the policy was read from")
        problems = policy_binding_mismatches(doc, policy, policy_path)
        if problems:
            raise LedgerError("; ".join(problems))
    if catalogue is not None:
        if catalogue_path is None:
            raise LedgerError("a catalogue binding check needs the path the catalogue was read from")
        problems = binding_mismatches(doc, catalogue, catalogue_path)
        if problems:
            raise LedgerError("; ".join(problems))
    # Last, because a record can only be judged once it is settled which
    # catalogue names its exchange and which policy lists that venue's hosts.
    venues = _candidate_exchanges(catalogue)
    for symbol, record in records.items():
        _validate_record(str(symbol), record, policy=policy,
                         exchange=venues.get(str(symbol)) if venues is not None else None)


def _candidate_exchanges(catalogue: Optional[Mapping[str, Any]]) -> Optional[Dict[str, str]]:
    """Symbol -> the exchange the catalogue says lists it; ``None`` with no catalogue.

    The venue always comes from the candidate's own catalogue record, never from
    the citation: a citation that nominated its own exchange would be a host
    proving itself first party.
    """
    if catalogue is None:
        return None
    return {str(c.get("symbol")): str(c.get("exchange") or "")
            for c in catalogue.get("candidates", []) if isinstance(c, dict)}


def binding_mismatches(doc: Mapping[str, Any], catalogue: Mapping[str, Any],
                       catalogue_path: Any) -> List[str]:
    """Every way this ledger fails to describe the catalogue at this path.

    Returned rather than raised so the builder can decide what to do about a
    mismatch (``rebind_refusals``) using the same comparison the reader applies.
    """
    if not catalogue.get("available"):
        return ["the catalogue is unavailable, so the ledger binding cannot be proved"]
    problems: List[str] = []
    expected = catalogue_binding(catalogue, catalogue_path)
    for field in BINDING_FIELDS:
        # Both sides of the path go through one normaliser, so a ledger written
        # with a different separator spelling is not reported as drift while two
        # genuinely different files still are.
        stored = normalized_catalogue_path(doc.get(field)) if field == "prepared_for" else doc.get(field)
        if stored != expected[field]:
            problems.append(f"{field} {doc.get(field)!r} does not match the catalogue's {expected[field]!r}")
    records = doc.get("records")
    held = set(str(s) for s in records) if isinstance(records, dict) else set()
    listed = set(str(c["symbol"]) for c in catalogue.get("candidates", []))
    unknown = held - listed
    if unknown:
        problems.append(f"ledger holds records for symbols the catalogue does not list: {sorted(unknown)}")
    missing = listed - held
    if missing:
        problems.append(f"ledger has no record for listed candidates: {sorted(missing)}")
    return problems


def prepared_records(doc: Mapping[str, Any]) -> List[str]:
    """Symbols whose record holds something a human put there.

    Anything beyond a freshly created ``pending`` record counts: a gathered
    source, a block with its reason, a note, a preparer.  The builder uses this
    to decide whether a rebind would destroy work -- an all-pending, wholly
    unknown ledger can be rebuilt freely because there is nothing in it to lose.
    """
    records = doc.get("records")
    if not isinstance(records, dict):
        return []
    touched = []
    for symbol, record in sorted(records.items()):
        if not isinstance(record, dict):
            touched.append(str(symbol))
            continue
        evidence = record.get("evidence")
        gathered = any(evidence.get(term) is not None for term in EVIDENCE_TERMS) \
            if isinstance(evidence, dict) else True
        written = any(str(record.get(field, "")).strip()
                      for field in ("blocked_reason", "prepared_by", "prepared_at", "notes"))
        if gathered or written or record.get("admission_state") != STATE_PENDING:
            touched.append(str(symbol))
    return touched


def rebind_refusals(doc: Mapping[str, Any], catalogue: Mapping[str, Any], catalogue_path: Any,
                    policy: Optional[Mapping[str, Any]] = None,
                    policy_path: Any = None) -> List[str]:
    """Why this ledger must not be automatically rebound to the sources at these paths.

    Rebinding is rewriting the header that says which snapshot, which file, and
    under which host policy the evidence was gathered.  Doing that silently would
    let a multiplier transcribed from one contract version be re-presented as
    evidence for another, or a citation accepted under a widened host list be
    presented as one the original policy had approved -- precisely the
    substitutions this layer exists to make impossible.

    A path-only move is no less a rebind, for either document: two files that are
    byte-identical today are still two files, and only one of them was read.  So
    the builder refuses whenever any part of either binding moved *and* the ledger
    holds anything a human prepared; resolving that is a human's call, not a
    rebuild's.  A ledger with nothing prepared in it has no evidence to
    mis-attribute and rebinds freely.
    """
    mismatches = binding_mismatches(doc, catalogue, catalogue_path)
    if policy is not None:
        mismatches = mismatches + policy_binding_mismatches(doc, policy, policy_path)
    if not mismatches:
        return []
    touched = prepared_records(doc)
    if not touched:
        return []
    return mismatches + [f"{len(touched)} record(s) hold prepared material: {', '.join(touched[:10])}"
                         + (" ..." if len(touched) > 10 else "")]


def _exact_schema(declared: Any, expected: int) -> bool:
    """A schema version is an integer saying which rules the file was written for.

    ``True == 1`` and ``1.0 == 1`` are both true in Python, so a plain equality
    test would read a boolean or a float as a version number and judge the
    document under rules it never declared.
    """
    return isinstance(declared, int) and not isinstance(declared, bool) and declared == expected


def legacy_ledger_problems(doc: Any) -> List[str]:
    """Every way this document fails to be a structurally valid B1a (v1) ledger.

    A migration rewrites the header and stamps a *fresh* fingerprint over whatever
    it found.  So a document whose only B1a credential is the number 1 in its
    schema field must not be migrated: the rebuild would compute a digest over the
    tampering and hand back a currently valid file, which turns the migration into
    the thing that launders the edit the old digest existed to catch.  A truncated
    record set, a records array, a count that does not match, an unknown field or a
    broken legacy fingerprint are all the same problem wearing different clothes.

    So a v1 document proves itself under v1's own rules first -- kind, exact field
    set, field types, the records mapping, the count, the B1a evidence shape, the
    execution-term ban, the required catalogue binding, and its own fingerprint --
    and only then is it asked whether it holds prepared material.

    Returned as a list rather than raised because the caller reports every reason
    and leaves the file alone; see ``schema_migration_refusals``.
    """
    if not isinstance(doc, dict):
        return ["legacy ledger must be a document mapping"]
    extra = set(doc) - set(V1_LEDGER_FIELDS)
    if extra:
        return [f"legacy ledger carries unsupported fields: {sorted(extra)}"]
    missing = set(V1_LEDGER_FIELDS) - set(doc)
    if missing:
        return [f"legacy ledger is missing required fields: {sorted(missing)}"]
    if doc.get("kind") != LEDGER_KIND:
        return [f"not an admission ledger: kind={doc.get('kind')!r}"]
    # Reported before the type checks, so the message says what the file actually
    # is rather than which of its fields is the wrong shape.
    banned = forbidden_keys_in(doc)
    if banned:
        return [f"ledger carries execution terms: {sorted(banned)}"]
    for field in ("note", "prepared_for", "prepared_at", "catalogue_fetched_at",
                  "catalogue_content_hash", FINGERPRINT_FIELD):
        if not isinstance(doc.get(field), str):
            return [f"legacy ledger {field} must be a string"]
    for field in ("catalogue_schema_version", "catalogue_candidate_count"):
        value = doc.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return [f"legacy ledger {field} must be a non-negative integer (got {value!r})"]
    records = doc.get("records")
    if not isinstance(records, dict):
        return ["legacy ledger must carry a records mapping"]
    if doc.get("count") != len(records):
        return ["legacy ledger count must equal the number of records"]
    if doc.get(FINGERPRINT_FIELD) != ledger_fingerprint(doc):
        return ["legacy ledger_fingerprint does not match the ledger body; a migration stamps a "
                "new fingerprint and would certify the edit instead of refusing it"]
    problems: List[str] = []
    for symbol, record in sorted(records.items(), key=lambda item: str(item[0])):
        try:
            _validate_record(str(symbol), record, shape=LEGACY_EVIDENCE)
        except LedgerError as exc:
            problems.append(str(exc))
    return problems


def schema_migration_refusals(doc: Mapping[str, Any]) -> List[str]:
    """Why this older ledger may not simply be rebuilt under the current schema.

    B1b added two fields every citation has to carry.  A v1 ledger that holds
    gathered evidence therefore holds citations whose authority and document kind
    *nobody recorded*, and the only ways a rebuild could produce them are to
    invent them or to drop the evidence.  Both are worse than stopping, so it
    stops: the file is left alone and a human decides whether each citation
    really was the exchange's own contract, margin rule or fee notice.

    A valid v1 ledger with nothing prepared in it carries no citation at all, so
    there is nothing to invent and the migration is a pure header rewrite.  What
    it may not be is a *claim* of version 1 -- see ``legacy_ledger_problems``.
    Returning ``[]`` for an already-current ledger keeps the caller free of
    version branching.
    """
    declared = doc.get("schema_version") if isinstance(doc, dict) else None
    if _exact_schema(declared, LEDGER_SCHEMA_VERSION):
        return []
    if not any(_exact_schema(declared, version) for version in MIGRATABLE_SCHEMA_VERSIONS):
        return [f"ledger schema_version {declared!r} cannot be migrated to "
                f"schema_version {LEDGER_SCHEMA_VERSION}"]
    problems = legacy_ledger_problems(doc)
    if problems:
        return problems
    touched = prepared_records(doc)
    if not touched:
        return []
    return [f"schema_version {declared} ledger holds prepared material: "
            f"{', '.join(touched[:10])}" + (" ..." if len(touched) > 10 else ""),
            f"schema_version {LEDGER_SCHEMA_VERSION} requires a source_authority and a "
            f"source_kind for every citation, and a rebuild must not invent either"]


def _validate_record(symbol: str, record: Any, *, policy: Optional[Mapping[str, Any]] = None,
                     exchange: Optional[str] = None,
                     shape: EvidenceShape = CURRENT_EVIDENCE) -> None:
    """Prove one record prepares a listed candidate and claims nothing more.

    ``shape`` is the schema its evidence slots are judged under; the record fields
    themselves are the same in v1 and v2.
    """
    if not isinstance(record, dict):
        raise LedgerError(f"{symbol} is not an admission record")
    extra = set(record) - set(RECORD_FIELDS)
    if extra:
        raise LedgerError(f"{symbol} carries unsupported fields: {sorted(extra)}")
    if not _SYMBOL_RE.match(symbol):
        raise LedgerError(f"invalid continuous symbol: {symbol!r}")
    # The stored code is the record's own identity, not an echo of the map key:
    # two keys pointing at one symbol is a duplicate, not a second candidate.
    if record.get("symbol") != symbol:
        raise LedgerError(f"{symbol} disagrees with its symbol field: {record.get('symbol')!r}")
    state = record.get("admission_state")
    if state not in ADMISSION_STATES:
        raise LedgerError(f"{symbol} has an unknown admission_state: {state!r}")
    for field in ("blocked_reason", "prepared_by", "prepared_at", "notes"):
        if not isinstance(record.get(field), str):
            raise LedgerError(f"{symbol} {field} must be a string")
    if record["prepared_at"] and not _valid_date(record["prepared_at"]):
        raise LedgerError(f"{symbol} prepared_at must be YYYY-MM-DD (got {record['prepared_at']!r})")
    # A block that does not say why cannot be reviewed or lifted.
    if state == STATE_BLOCKED and not record["blocked_reason"].strip():
        raise LedgerError(f"{symbol} is blocked without a blocked_reason")
    if state != STATE_BLOCKED and record["blocked_reason"].strip():
        raise LedgerError(f"{symbol} carries a blocked_reason while in state {state!r}")

    evidence = record.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE_TERMS):
        raise LedgerError(f"{symbol} evidence must carry exactly {list(EVIDENCE_TERMS)}")
    for term in EVIDENCE_TERMS:
        _validate_evidence(symbol, term, evidence[term], policy=policy, exchange=exchange,
                           shape=shape)

    if state == STATE_READY_FOR_REVIEW:
        _validate_ready_for_review(symbol, record, evidence)


def _validate_ready_for_review(symbol: str, record: Mapping[str, Any],
                               evidence: Mapping[str, Any]) -> None:
    """The strongest state has to be earned by all five terms, not by three of them."""
    missing = [term for term in EVIDENCE_TERMS if evidence[term] is None]
    if missing:
        raise LedgerError(f"{symbol} is ready_for_review without evidence for: {sorted(missing)}")
    unsourced = [term for term in EVIDENCE_TERMS
                 if evidence[term].get("review_state") not in SOURCED_REVIEW_STATES]
    if unsourced:
        raise LedgerError(f"{symbol} is ready_for_review with unreviewed evidence for: {sorted(unsourced)}")
    if not record["prepared_by"].strip() or not record["prepared_at"].strip():
        raise LedgerError(f"{symbol} is ready_for_review without prepared_by / prepared_at")


def _validate_evidence(symbol: str, term: str, entry: Any, *,
                       policy: Optional[Mapping[str, Any]] = None,
                       exchange: Optional[str] = None,
                       shape: EvidenceShape = CURRENT_EVIDENCE) -> None:
    """Prove one term-evidence slot is either honestly absent or fully, first-party cited.

    ``None`` is *the* empty slot, and the only one.  An absent source is the truth
    for a candidate nobody has researched, and this layer's whole claim is that it
    says so.  A dictionary with a null value and every citation field blank is not
    that truth: it is a slot that has been created rather than filled, and
    coverage -- which counts non-``None`` slots -- would read it as a gathered
    source.  So it is refused and ``None`` is required instead.

    Beyond that there are exactly two states, and no half-way one:

      * the slot claims nothing, and is ``None``;
      * the slot claims something -- a recorded value, or *any* field describing
        the document it came from -- and then it carries the whole core of a
        citation: the address, the publisher and the document kind, all three
        surviving the policy (``exchange_primary``, an exact official host of the
        candidate's own venue, and a kind that can establish this term).  For
        margin and fee it also says from when the figure applies.

    A value with no provenance is the worst of these: a number nobody can trace,
    sitting in the file in exactly the shape a reviewer would promote.  A
    ``published_date`` or a ``source_title`` with no address is the same failure
    read from the other end -- a document that cannot be found being described.
    Both are refused at first mention rather than at review time, because the
    person who would otherwise discover the gap is the reviewer who already
    trusted it.

    ``recorded`` / ``cross_checked`` then add their own requirements on top: those
    states assert a human transcribed the value from the document, so they need
    the value, the title, the reader and a date as well.
    """
    if entry is None:
        return
    where = f"{symbol}.{term}"
    if not isinstance(entry, dict):
        raise LedgerError(f"{where} must be an evidence record or null")
    extra = set(entry) - set(shape.fields)
    if extra:
        raise LedgerError(f"{where} carries unsupported fields: {sorted(extra)}")
    review_state = entry.get("review_state")
    if review_state not in REVIEW_STATES:
        raise LedgerError(f"{where} has an unknown review_state: {review_state!r}")
    for field in [name for name in shape.fields if name in CITATION_FIELDS]:
        if not isinstance(entry.get(field), str):
            raise LedgerError(f"{where} {field} must be a string")
    # Format is checked whenever a value is present, regardless of review state:
    # a malformed URL or date is wrong even in an unreviewed draft.  A source URL
    # is held to the first-party transport rules here rather than to the looser
    # "any public http(s) address", so plaintext, credentials and ports are
    # refused at the first character somebody types.
    if entry["source_url"]:
        problem = transport_problem(entry["source_url"])
        if problem:
            raise LedgerError(f"{where} source_url: {problem}")
    for field in ("published_date", "effective_date"):
        if entry[field] and not _valid_date(entry[field]):
            raise LedgerError(f"{where} {field} must be YYYY-MM-DD (got {entry[field]!r})")
    problem = _value_problem(term, entry.get("value"))
    if problem:
        raise LedgerError(f"{where} {problem}")
    claimed = _claims(entry, shape)
    if not claimed:
        raise LedgerError(f"{where} is an empty evidence record: it has no value and names no "
                          f"source, so it records nothing. An unresearched term is null, not a "
                          f"slot with nothing in it -- coverage counts slots, not claims")
    _validate_provenance(where, term, entry, claimed, shape, policy=policy, exchange=exchange)
    _validate_time_anchor(where, term, entry, claimed, shape)

    if review_state in SOURCED_REVIEW_STATES:
        if entry.get("value") is None:
            raise LedgerError(f"{where} claims review_state={review_state!r} with no value recorded")
        for field in [name for name in ("source_url", "source_title", "recorded_by",
                                        "source_authority", "source_kind")
                      if name in shape.fields]:
            if not entry[field].strip():
                raise LedgerError(f"{where} claims review_state={review_state!r} without {field}")
        if not entry["published_date"].strip() and not entry["effective_date"].strip():
            raise LedgerError(f"{where} claims review_state={review_state!r} without a published or effective date")
        # The margin/fee effective date is *not* checked here.  A sourced state
        # always names a source, so ``_validate_time_anchor`` has already run on
        # it; repeating the rule at review time is what let an unreviewed draft
        # keep an undated margin notice in the first place.


def _claims(entry: Mapping[str, Any], shape: EvidenceShape = CURRENT_EVIDENCE) -> List[str]:
    """Everything this slot asserts: a recorded value, and any field describing a source.

    One definition, because every rule below turns on it.  A recorded number and a
    named document are the same kind of assertion -- both say somebody looked
    something up -- so both demand the same complete core, and neither can be
    filled in without the other.  An empty list means the slot asserts nothing at
    all, which is a slot that should have been ``None``.
    """
    stated = [field for field in CITATION_FIELDS
              if field in shape.fields and entry[field].strip()]
    return (["value"] if entry.get("value") is not None else []) + stated


def _validate_time_anchor(where: str, term: str, entry: Mapping[str, Any],
                          claimed: Sequence[str],
                          shape: EvidenceShape = CURRENT_EVIDENCE) -> None:
    """A cited margin or fee has to say from when it applies, at any review state.

    Margin and fee are not contract properties: they are set by rule and revised
    by notice, repeatedly, and an exchange notice routinely prints the superseded
    figure beside the new one.  "Published 2026-01-15" says when that document
    was written, not which of the numbers on it is in force -- so a ratio whose
    effective date nobody recorded cannot be told apart from the one it replaced.

    The trigger is asserting anything at all -- a ratio, a title, a publication
    date -- not reaching a review state.  A draft that already names the exchange,
    the document kind and an exact official host *is* a sourced margin or fee
    notice; whether a reviewer has upgraded the slot yet changes nothing about what
    is missing from it, and waiting for the upgrade means the gap is discovered by
    the person least able to notice it.  Only ``None`` claims nothing, and ``None``
    never reaches here.

    The date's *format* is checked earlier, on any slot that carries one; this is
    the rule that says it has to be there at all.  It is a v2 rule: B1a carried the
    field but never required it, so a legacy document is not judged by it and a
    migration cannot supply one (``LEGACY_EVIDENCE``).
    """
    if not shape.dated or term not in DATED_TERMS or not claimed:
        return
    if entry["effective_date"].strip():
        return
    raise LedgerError(f"{where} records evidence ({', '.join(claimed)}) without an effective_date; "
                      f"a {term} is set by notice and revised repeatedly, so a publication date "
                      f"does not say from when this one applies")


def _validate_provenance(where: str, term: str, entry: Mapping[str, Any],
                         claimed: Sequence[str], shape: EvidenceShape = CURRENT_EVIDENCE, *,
                         policy: Optional[Mapping[str, Any]] = None,
                         exchange: Optional[str] = None) -> None:
    """Prove a slot that asserts anything traces it to a first-party source.

    Called only for a slot that claims something -- ``None`` returned earlier, and
    a slot claiming nothing was already refused -- so the core is required
    unconditionally here.  A recorded number and a described document both need
    the same three fields: where it is from, who published it, and what it is.

    Three layers, deliberately separated by what each one needs:

      * completeness of the core is a property of the entry alone;
      * the vocabulary -- authority, kind, and whether that kind can establish
        this term -- is fixed in code and so is checked unconditionally;
      * transport was already checked above, on the URL alone;
      * the *venue* needs both the candidate's catalogue exchange and the policy's
        host list, so it runs when a caller supplied them.  Every path that
        accepts a ledger does; see ``validate_ledger``.

    A B1a slot (``LEGACY_EVIDENCE``) stops after the core it actually had: it
    recorded no authority and no kind, so there is no vocabulary to check and
    nothing a reader could supply on its behalf.
    """
    for field in shape.core:
        if not entry[field].strip():
            raise LedgerError(f"{where} records evidence ({', '.join(claimed)}) without {field}; "
                              f"a recorded term has to say where it is from, who published it "
                              f"and what it is")
    if not {"source_authority", "source_kind"} <= set(shape.fields):
        return
    problem = citation_problem(term, entry["source_authority"], entry["source_kind"])
    if not problem and policy is not None and exchange is not None:
        problem = venue_problem(policy, exchange, entry["source_url"])
    if problem:
        raise LedgerError(f"{where} {problem}")


def _value_problem(term: str, value: Any) -> str:
    """Describe why a proposed value is unusable, or "" when it is fine.

    ``None`` is always fine: unknown is a legitimate, preserved state.  A value
    that *is* present has to be well formed, because a typo here is the same
    class of error B0 refuses in the contract table -- a wrong multiplier does
    not raise, it just quietly rescales a leaderboard.
    """
    if value is None:
        return ""
    if term in (TERM_MULTIPLIER, TERM_TICK):
        return "" if _positive(value) else f"value must be a finite positive number (got {value!r})"
    if term == TERM_MARGIN:
        number = _finite(value)
        if number is None or not 0 < number <= 1:
            return f"value must be a ratio in (0, 1] (got {value!r})"
        return ""
    if term == TERM_FEE:
        return _fee_problem(value)
    return _sessions_problem(value)


def _fee_problem(value: Any) -> str:
    """A fee proposal states both components; zero is fine, unstated is not.

    Mirrors the contract table's rule (``fee_per_lot`` and ``fee_rate``): a fee
    that is absent from the evidence reads as "free", which no contract is.
    """
    if not isinstance(value, dict) or set(value) != set(FEE_FIELDS):
        return f"value must be an object with exactly {list(FEE_FIELDS)} (got {value!r})"
    for field in FEE_FIELDS:
        number = _finite(value[field])
        if number is None or number < 0:
            return f"value.{field} must be a finite non-negative number (got {value[field]!r})"
    return ""


def _sessions_problem(value: Any) -> str:
    """Session windows are checked for shape only; whether they are *the* windows is the reviewer's job."""
    if not isinstance(value, list) or not value:
        return f"value must be a non-empty list of [start, end] pairs (got {value!r})"
    for index, window in enumerate(value):
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            return f"value[{index}] must be a [start, end] pair (got {window!r})"
        bounds = [_session_minute(edge) for edge in window]
        if None in bounds:
            return f"value[{index}] must be HH:MM within 00:00..48:00 (got {list(window)!r})"
        if bounds[0] >= bounds[1]:
            return f"value[{index}] must end after it starts (got {list(window)!r})"
    return ""


def _session_minute(value: Any) -> Optional[int]:
    if not isinstance(value, str):
        return None
    match = _SESSION_TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    minute = int(match.group(1)) * 60 + int(match.group(2))
    return minute if 0 <= minute <= MAX_SESSION_MINUTE else None


def _finite(value: Any) -> Optional[float]:
    """Return the value as a float only when it is a real, finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive(value: Any) -> bool:
    number = _finite(value)
    return number is not None and number > 0


def _valid_date(value: Any) -> bool:
    """A calendar date, not merely ten characters shaped like one."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------- reading -----

def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    """json.load would keep the last of two identical keys; here that is an error.

    A file with ``"RB0"`` twice has two different intentions for one candidate,
    and silently keeping the second is how a reviewed record gets replaced by an
    unreviewed one without anybody noticing.
    """
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise LedgerError(f"duplicate key in ledger document: {key!r}")
        seen[key] = value
    return seen


def read_ledger(path: str | Path) -> Dict[str, Any]:
    """Read a ledger document, refusing duplicate keys; raises ``LedgerError``."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=_reject_duplicate_keys)
    except LedgerError:
        raise
    except (OSError, ValueError) as exc:
        raise LedgerError(f"ledger could not be read: {exc}") from exc


def load_ledger(path: str | Path, *, catalogue: Optional[Mapping[str, Any]] = None,
                catalogue_path: Any = None, policy: Optional[Mapping[str, Any]] = None,
                policy_path: Any = None) -> Dict[str, Any]:
    """Read and fully validate a ledger; raises ``LedgerError`` on any violation."""
    doc = read_ledger(path)
    validate_ledger(doc, catalogue=catalogue, catalogue_path=catalogue_path,
                    policy=policy, policy_path=policy_path)
    return doc


# ------------------------------------------------------------ projection -----

def _text(value: Any, limit: int = 120) -> str:
    return "" if value is None else str(value)[:limit]


def project_ledger(doc: Any, catalogue: Mapping[str, Any], catalogue_path: Any,
                   policy: Optional[Mapping[str, Any]] = None,
                   policy_path: Any = None) -> Dict[str, Any]:
    """Build the read-only public view of a ledger: coverage and state, never values.

    Proposed numbers are deliberately withheld.  A dashboard column showing
    "multiplier 10" for an unreviewed candidate is indistinguishable from a
    verified term at a glance, and the whole point of this layer is that nothing
    in it has been verified.  So are the citations and everything the policy knows
    about them -- source URL, title, host, document kind, authority, reviewer --
    because a first-party hostname rendered beside a code reads as approval, and
    passing the provenance gate is not approval.  What the reader gets is how far
    preparation has gone, per candidate and per term, which is exactly what a
    reviewer needs to decide where to work next.

    Two anchors, both re-proved on every read on top of the fingerprint that
    covers them and the records together: the catalogue's complete binding
    (declared path, schema, content hash, candidate count, symbol set) and the
    source policy's (declared path, schema, fingerprint).  A missing or invalid
    policy is a refusal, not a reason to skip the provenance checks -- there is no
    reading of "we could not load the host list" under which the citations in the
    file become more trustworthy.  A ledger that is stale, tampered, duplicated,
    incomplete, or bound to another snapshot, another file or another policy
    becomes wholly unavailable.  It is never partially projected and never
    downgraded to "pending": either the file proves what it claims, or the reader
    has nothing to show.

    Those anchors are *proved here and kept here*.  Neither binding, neither
    digest, the block reason and the preparation date are all absent from the
    result: they are the internal apparatus that decides whether this file may be
    believed, not findings about a candidate.  Publishing them would put the
    exact values a forged ledger has to reproduce on a page anyone with the
    dashboard open can read, and would put free-form reviewer prose -- the one
    field in the document nothing constrains -- on that page with it.  The public
    shape is therefore fixed at ``PROJECTION_FIELDS`` / ``PROJECTION_RECORD_FIELDS``
    and is identical whether the ledger was accepted or refused.
    """
    if not catalogue.get("available") or not policy:
        return _empty_projection()
    try:
        validate_ledger(doc, catalogue=catalogue, catalogue_path=catalogue_path,
                        policy=policy, policy_path=policy_path)
    except LedgerError:
        return _empty_projection()
    records = doc["records"]
    counts = {state: 0 for state in ADMISSION_STATES}
    rows: List[Dict[str, Any]] = []
    for symbol in sorted(records):
        record = records[symbol]
        evidence = record["evidence"]
        counts[record["admission_state"]] += 1
        terms = {term: (evidence[term]["review_state"] if evidence[term] else REVIEW_UNREVIEWED)
                 for term in EVIDENCE_TERMS}
        rows.append({
            "symbol": symbol,
            "admission_state": record["admission_state"],
            # Coverage is derived from what is actually present, so a stale or
            # optimistic summary field could never overstate it.
            "evidence_recorded": sum(1 for term in EVIDENCE_TERMS if evidence[term] is not None),
            "evidence_required": len(EVIDENCE_TERMS),
            "terms": terms,
        })
    # ``blocked_reason`` is absent for the same reason the citations are: it is
    # unconstrained reviewer prose, and "已阻断" already tells the reader every
    # actionable thing the dashboard needs.  Whoever has to lift the block reads
    # the file.
    return {"available": True, "note": DISCLAIMER, "evidence_terms": list(EVIDENCE_TERMS),
            "count": len(rows), "counts": counts, "records": rows}


def _empty_projection() -> Dict[str, Any]:
    """The refusal view -- the same keys as a success, carrying nothing.

    Matching the successful shape exactly is deliberate: a refusal that dropped
    or added keys would tell a reader which check failed, and the honest answer
    to every failure here is the same one.
    """
    return {"available": False, "note": DISCLAIMER, "evidence_terms": list(EVIDENCE_TERMS),
            "count": 0, "counts": {state: 0 for state in ADMISSION_STATES}, "records": []}


def load_ledger_projection(path: str | Path, catalogue: Mapping[str, Any], catalogue_path: Any,
                           policy: Optional[Mapping[str, Any]] = None,
                           policy_path: Any = None) -> Dict[str, Any]:
    """Project the ledger at ``path``, degrading to an empty view on any problem.

    ``catalogue_path`` and ``policy_path`` are the paths those documents were read
    from, as the reader declares them -- the values a ledger prepared by this
    application would have recorded, not resolved absolute forms, so a committed
    relative declaration keeps matching wherever the repository is checked out.
    """
    try:
        doc = read_ledger(path)
    except LedgerError:
        return _empty_projection()
    return project_ledger(doc, catalogue, catalogue_path, policy, policy_path)


def ledger_symbols(doc: Mapping[str, Any]) -> Set[str]:
    """The codes a ledger holds records for -- planning identifiers, never a trading pool."""
    records = doc.get("records")
    return set(str(s) for s in records) if isinstance(records, dict) else set()
