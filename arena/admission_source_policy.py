"""Which exact hostname counts as a first-party exchange source, and for which term.

B1a built a place to *record* the public evidence behind a candidate's proposed
multiplier, tick, sessions, margin and fee.  It could prove a citation was well
*formed* -- a parseable URL, a real calendar date, a value of the right shape --
but not that the citation pointed at the exchange that actually lists the
candidate, nor that the kind of document cited is capable of establishing the
term it was cited for.  A margin ratio transcribed from a brokerage's marketing
page, and a multiplier read off a news article, passed B1a exactly as cleanly as
the exchange's own contract specification.

This module is that missing half, and it is deliberately the narrowest thing
that can do the job:

  * **Exact hostnames, listed one by one.**  No suffix rule, no wildcard, no
    substring, no DNS lookup.  ``www.shfe.com.cn.attacker.example`` is a
    *different host* from ``www.shfe.com.cn``, and a suffix check is precisely
    how it would be mistaken for one.  INE's two SHFE hosts are written out as a
    listed exception, because that is what it is -- not evidence for an inferred
    rule that any subdomain of a parent domain is first party.  A *name*, never
    an address: a numeric host has many spellings that reach one machine, so
    matching one of them as a string states nothing about the others.  Every
    numeric IPv4 form -- canonical, short, octal, hex, or a single integer -- and
    every IPv6 literal is refused on both sides, by grammar and not by resolver.
  * **Offline.**  Matching is string comparison against a committed document.
    Nothing here resolves a name, opens a socket or fetches a page: a provenance
    check that consulted the network would be a provenance check whoever
    controls the network could answer.
  * **Term-appropriate kinds.**  A contract specification establishes a
    multiplier; it does not establish this month's margin ratio.  That matrix is
    fixed in code rather than in the policy file, so editing a JSON document can
    never widen what a document kind is allowed to prove.  The file carries only
    the part that legitimately changes -- which hostnames a venue publishes on.
  * **Fail closed.**  An unknown or unmapped exchange, ``http``, an explicit
    port, embedded credentials, a foreign host, a lookalike host, or a term/kind
    pair outside the matrix are all refusals.  There is no "probably fine" path,
    and there is no default host list to fall back on.

The policy document is versioned, fingerprinted and strictly typed for the same
reason the ledger is: the ledger's header binds the policy's declared path,
schema version and fingerprint, so evidence gathered under one host list cannot
be silently re-presented as evidence gathered under another.

Nothing in this module makes anything executable.  Passing every check here means
one thing only: a recorded citation claims a source the policy recognises as
first party for that venue and that term.  It is not verification, not admission
and not permission to trade.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .universe import forbidden_keys_in

DEFAULT_SOURCE_POLICY_FILE = "config/admission_source_policy_cn.json"
POLICY_SCHEMA_VERSION = 1
# Stamped into the document so a policy cannot be mistaken for a catalogue, a
# ledger or a contract table merely because all four are JSON in config/.
POLICY_KIND = "contract_admission_source_policy"
POLICY_FINGERPRINT_FIELD = "policy_fingerprint"
POLICY_FIELDS = ("schema_version", "kind", "note", POLICY_FINGERPRINT_FIELD, "exchange_hosts")

# The five terms B0 refuses to default.  They live here rather than in
# arena/admission.py because this is the layer that decides what a term *means*
# in provenance terms -- which kind of published document can establish it.
# arena/admission.py imports them, so the vocabulary has exactly one definition.
TERM_MULTIPLIER = "proposed_multiplier"
TERM_TICK = "proposed_tick"
TERM_SESSIONS = "proposed_sessions"
TERM_MARGIN = "proposed_margin_ratio"
TERM_FEE = "proposed_fee"
EVIDENCE_TERMS = (TERM_MULTIPLIER, TERM_TICK, TERM_SESSIONS, TERM_MARGIN, TERM_FEE)

# Who published the document.  One value, on purpose: this phase accepts nothing
# but the listing exchange's own site.  A data vendor, an index page, a brokerage
# summary or a regulator's mirror may all be accurate and are all still somebody
# else's transcription, which is the error this layer exists to catch.
SOURCE_AUTHORITY_EXCHANGE_PRIMARY = "exchange_primary"
SOURCE_AUTHORITIES = (SOURCE_AUTHORITY_EXCHANGE_PRIMARY,)

# What the document *is*.  A term may only be cited from a kind that can
# actually establish it.
KIND_CONTRACT = "official_contract"                   # the contract specification itself
KIND_SESSION_NOTICE = "official_session_notice"        # a trading-hours notice
KIND_MARGIN_RULE = "official_margin_rule_or_notice"    # a margin rule or adjustment notice
KIND_FEE_RULE = "official_fee_rule_or_notice"          # a fee schedule or adjustment notice
TERM_SOURCE_KINDS: Dict[str, Tuple[str, ...]] = {
    # A multiplier and a tick are properties of the contract, so only the
    # specification establishes them.
    TERM_MULTIPLIER: (KIND_CONTRACT,),
    TERM_TICK: (KIND_CONTRACT,),
    # Sessions are stated in the specification but are also moved by notice, and
    # the notice is then the newer truth.
    TERM_SESSIONS: (KIND_CONTRACT, KIND_SESSION_NOTICE),
    # Margin and fee are *not* contract properties: they are set by rule and
    # revised by notice, repeatedly, which is why the specification cannot
    # establish them and why they need an effective date (see DATED_TERMS).
    TERM_MARGIN: (KIND_MARGIN_RULE,),
    TERM_FEE: (KIND_FEE_RULE,),
}
SOURCE_KINDS = tuple(sorted({kind for kinds in TERM_SOURCE_KINDS.values() for kind in kinds}))

# Terms whose value is only true for a stated period.  "Published 2026-01-15"
# says when somebody wrote it down, not from when it applies, and a margin ratio
# recorded without the date it takes effect cannot be reviewed at all.
DATED_TERMS = (TERM_MARGIN, TERM_FEE)

# Transport is fixed in code, never in the document: a policy file must not be
# able to weaken it back down to plaintext.
REQUIRED_SOURCE_SCHEME = "https"

# The policy facts a ledger is bound to, mirroring the catalogue binding in
# arena/admission.py.  Content identity (the fingerprint) is not enough on its
# own -- two policy files can be byte-identical and still be two files, only one
# of which the evidence was gathered under -- so the declared path is bound too.
POLICY_BINDING_FIELDS = ("source_policy_path", "source_policy_schema_version",
                         "source_policy_fingerprint")

_EXCHANGE_RE = re.compile(r"^[A-Z]{2,8}$")
# An exact, already-canonical hostname: lowercase, at least two labels, no
# scheme, no port, no path, no wildcard, no trailing dot.  Requiring the
# canonical spelling in the document is what makes comparison against
# ``urlsplit().hostname`` (which lowercases) a plain equality test.
#
# It is deliberately not the last word on what a host may be: a dotted IPv4 quad
# satisfies this grammar too, and is refused separately -- see ``numeric_host``.
_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                      r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
# One label of a numeric address written the way the C resolver has always
# accepted them: decimal (``127``), octal (``0177``, which is just a decimal run
# with a leading zero) or hexadecimal (``0x7f``).  A DNS label is never any of
# these -- a top-level label may not be all digits, and no committed exchange
# host has a label without a letter in it -- so "every label is one of these" is
# a safe reading of "this is an address, not a name".
_NUMERIC_LABEL_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
# Every ASCII C0 control plus DEL.  ``urlsplit`` does not preserve these: it
# strips leading and trailing C0 controls and removes tab, LF and CR from
# anywhere in the string, so the address it parses can differ from the address
# written in the file -- and the file is what a reviewer reads.
_CONTROL_CHARS = frozenset(chr(code) for code in list(range(0x20)) + [0x7F])

DISCLAIMER = (
    "Local provenance policy for admission-ledger evidence: the exact first-party HTTPS "
    "hostnames that may be cited for each exchange. Matching is exact -- no suffix, substring, "
    "wildcard or DNS resolution -- so a lookalike host is simply a different host. INE's SHFE "
    "hosts are a listed exception, not an inferred parent-domain rule. This document carries no "
    "contract term, no candidate value, no citation and no credential; passing it proves a source "
    "is first party, never that a term is verified or that anything may be traded."
)


class SourcePolicyError(ValueError):
    """The source-policy document cannot prove it is a host policy and nothing more."""


# ------------------------------------------------------------- declared path --

def normalized_declared_path(path: Any) -> str:
    """One deterministic spelling for a path a document declares about itself.

    Only spellings the running platform already treats as the same file are
    folded together: ``os.path.normpath`` collapses redundant separators and
    ``.`` / ``..`` segments, and on Windows it also unifies ``/`` with ``\\``.
    The result is written with forward slashes so a committed relative path reads
    identically on either platform.

    The filesystem is never consulted.  ``Path.resolve`` would make the answer
    depend on the current working directory and on which files happen to exist
    on this one machine, and two genuinely different declarations could then
    agree by accident -- the opposite of what a provenance check is for.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normpath(text).replace(os.sep, "/")


# -------------------------------------------------------------- fingerprint --

def policy_fingerprint(doc: Mapping[str, Any]) -> str:
    """Fingerprint the whole policy -- every field except the fingerprint itself.

    Hashing only the host mapping would leave the header unprotected, and the
    header is where the document says what it is: its kind and its schema
    version.  ``sort_keys`` makes the digest independent of mapping insertion
    order, so the same policy written in a different order is the same policy.
    """
    body = {key: value for key, value in doc.items() if key != POLICY_FINGERPRINT_FIELD}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- validation --

def validate_policy(doc: Any) -> None:
    """Prove a whole policy document is an exact-host policy and nothing more.

    Raises ``SourcePolicyError`` describing the first violation found.  Both ends
    go through here: the loader calls it before anything is matched against the
    policy, and the ledger binds the validated document's fingerprint.
    """
    if not isinstance(doc, dict):
        raise SourcePolicyError("source policy must be a document mapping")
    extra = set(doc) - set(POLICY_FIELDS)
    if extra:
        raise SourcePolicyError(f"source policy carries unsupported fields: {sorted(extra)}")
    missing = set(POLICY_FIELDS) - set(doc)
    if missing:
        raise SourcePolicyError(f"source policy is missing required fields: {sorted(missing)}")
    if doc.get("kind") != POLICY_KIND:
        raise SourcePolicyError(f"not an admission source policy: kind={doc.get('kind')!r}")
    # By exact type, not by ``==``: ``True == 1`` and ``1.0 == 1`` are both true in
    # Python, so a boolean or a float would otherwise pass for schema 1 and the
    # document would be read under rules it never claimed.  A schema version is the
    # file stating which rules it was written for, and only an integer states that.
    version = doc.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != POLICY_SCHEMA_VERSION:
        raise SourcePolicyError(f"unsupported source policy schema_version: {version!r}")
    # A host policy is not a place to write a contract term.  Swept document-wide
    # for the same reason the ledger is: a policy file carrying ``multiplier`` was
    # hand-edited towards something it must never be.
    banned = forbidden_keys_in(doc)
    if banned:
        raise SourcePolicyError(f"source policy carries execution terms: {sorted(banned)}")
    for field in ("note", POLICY_FINGERPRINT_FIELD):
        if not isinstance(doc.get(field), str):
            raise SourcePolicyError(f"source policy {field} must be a string")
    mapping = doc.get("exchange_hosts")
    # An empty mapping fingerprints perfectly well and would otherwise be served
    # as a valid policy under which no citation can ever be first party -- a
    # silent shutdown of the gate rather than a refusal.
    if not isinstance(mapping, dict) or not mapping:
        raise SourcePolicyError("source policy must carry a non-empty exchange_hosts mapping")
    for exchange, hosts in mapping.items():
        _validate_hosts(str(exchange), hosts)
    if doc.get(POLICY_FINGERPRINT_FIELD) != policy_fingerprint(doc):
        raise SourcePolicyError("policy_fingerprint does not match the source policy body "
                                "(the host mapping or the header was edited after it was stamped)")


def numeric_host(host: Any) -> bool:
    """Whether this text is a numeric address spelling rather than a DNS name.

    An exchange publishes its documents at a hostname, and a citation's host is
    compared against the policy as a name.  An address is not a name: it
    identifies whatever answers at that number today, which is not something a
    committed policy file can vouch for.  A dotted IPv4 quad happens to satisfy
    the hostname grammar above, so this is a rule of its own rather than
    something the pattern catches by accident.

    ``ipaddress.ip_address`` is necessary and *not sufficient*.  It parses only
    the canonical spellings, and the numeric form of an IPv4 address predates
    that canon: ``127.1``, ``127.0.1``, ``0177.0.0.1``, ``0x7f.0.0.1`` and the
    single integer ``2130706433`` all name the same host to the C resolver, and a
    URL stack that normalises before connecting will happily resolve them --
    while ``ip_address`` calls every one of them invalid, i.e. a name.  Matching
    such a spelling as a *string* against a host list is then a comparison
    against something that will never be typed the same way twice.

    So the grammar is decided here, from the text alone -- no DNS, no socket, no
    URL-library normalisation, none of which a provenance check may depend on --
    and it is read conservatively: every dot-separated label being a decimal,
    octal or hexadecimal run is enough to call the whole thing an address, even
    when the arrangement is not a *valid* one (``1.2.3.4.5``, ``999.999``).  A
    hostname that would be refused for looking like an address is a hostname
    nobody can publish at anyway, whereas a numeric form that slipped through
    would be a host the policy cannot pin down.  No committed exchange host has a
    label without a letter in it.
    """
    if not isinstance(host, str) or not host:
        return False
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        pass
    else:
        # Canonical IPv4, and IPv6 in either spelling: refused here rather than
        # only by the hostname grammar, so every form says the same thing.
        return True
    return all(_NUMERIC_LABEL_RE.match(label) for label in bare.split("."))


def numeric_host_problem(host: Any) -> str:
    """Why this host is an address rather than a DNS name, or "" when it is a name."""
    if numeric_host(host):
        return (f"a first-party source is published at a DNS hostname, never at a numeric "
                f"address (got {host!r})")
    return ""


def _validate_hosts(exchange: str, hosts: Any) -> None:
    """Prove one venue's entry is a finite list of exact, canonical hostnames."""
    if not _EXCHANGE_RE.match(exchange):
        raise SourcePolicyError(f"invalid exchange code in source policy: {exchange!r}")
    if not isinstance(hosts, list) or not hosts:
        raise SourcePolicyError(f"{exchange} must list at least one official host")
    seen = set()
    for host in hosts:
        if numeric_host(host):
            raise SourcePolicyError(f"{exchange} official host must be a DNS hostname, not an "
                                    f"IP literal (got {host!r})")
        if not isinstance(host, str) or not _HOST_RE.match(host):
            # This is where a wildcard, a URL, a bare suffix or an uppercase
            # spelling is refused: every one of them would turn an exact match
            # into something looser than the policy claims to be.
            raise SourcePolicyError(f"{exchange} official host must be an exact lowercase "
                                    f"hostname (got {host!r})")
        if host in seen:
            raise SourcePolicyError(f"{exchange} lists {host!r} twice")
        seen.add(host)


# ------------------------------------------------------------------ matching --

def official_hosts(policy: Mapping[str, Any], exchange: Any) -> Tuple[str, ...]:
    """The exact hosts this policy recognises for a venue -- empty when unmapped.

    Empty is a refusal, never a pass: see ``venue_problem``.  Lookup is by exact
    venue code, so a catalogue naming ``shfe`` rather than ``SHFE`` is unmapped
    rather than quietly folded onto a mapping it may not belong to.
    """
    mapping = policy.get("exchange_hosts") if isinstance(policy, Mapping) else None
    hosts = mapping.get(str(exchange)) if isinstance(mapping, dict) else None
    return tuple(hosts) if isinstance(hosts, list) else ()


def citation_problem(term: Any, source_authority: Any, source_kind: Any) -> str:
    """Why this authority/kind pair cannot establish this term, or "" when it can.

    Policy-independent: it is about the *vocabulary*, which no document can
    widen.  Checked even when no policy or catalogue is available, so a term
    cited from the wrong sort of document is refused at every entry point.
    """
    if source_authority not in SOURCE_AUTHORITIES:
        return (f"source_authority must be {SOURCE_AUTHORITY_EXCHANGE_PRIMARY!r} "
                f"(got {source_authority!r})")
    allowed = TERM_SOURCE_KINDS.get(str(term))
    if allowed is None:
        return f"no source kind is defined for {term!r}"
    if source_kind not in SOURCE_KINDS:
        return f"unknown source_kind: {source_kind!r}"
    if source_kind not in allowed:
        return (f"source_kind {source_kind!r} cannot establish {term}; "
                f"allowed: {list(allowed)}")
    return ""


def transport_problem(source_url: Any) -> str:
    """Why this URL cannot be a first-party citation on its own terms, or "".

    Policy-independent and host-independent: plaintext, embedded credentials and
    an explicit port each mean the URL is not the plain public document address
    it claims to be, whichever venue it names.  Every explicit port is refused,
    including the scheme's default: a published exchange document has no reason
    to carry one, and refusing all of them removes the need to trust a
    scheme-to-default-port table.

    The control-character check runs *before* the parser, because the parser does
    not preserve them: ``urlsplit`` strips leading and trailing C0 controls and
    removes tab, LF and CR from anywhere in the string, so ``"https://a.example\\x00"``
    and ``"https://a.exa\\tmple"`` parse into hosts nobody wrote down.  Judging the
    parsed form would then approve a host the file does not contain, and the file
    is what a reviewer reads.  A published document address has no control
    character in it, before it, after it or inside it.
    """
    if not isinstance(source_url, str) or not source_url.strip():
        return f"source_url must be a plain URL (got {source_url!r})"
    found = sorted({ord(ch) for ch in source_url if ch in _CONTROL_CHARS})
    if found:
        # The code points, not the string: echoing the raw controls back into an
        # error message only moves the problem into whatever reads the message.
        return ("source_url must not contain control characters (found "
                + ", ".join(f"U+{code:04X}" for code in found) + ")")
    if source_url != source_url.strip():
        return f"source_url must be a plain URL (got {source_url!r})"
    if any(ch.isspace() for ch in source_url):
        return f"source_url must not contain whitespace (got {source_url!r})"
    try:
        parts = urlsplit(source_url)
    except ValueError:
        return f"source_url is not a parseable URL (got {source_url!r})"
    if parts.scheme != REQUIRED_SOURCE_SCHEME:
        return (f"a first-party source must be served over {REQUIRED_SOURCE_SCHEME} "
                f"(got {source_url!r})")
    if "@" in parts.netloc:
        return "a first-party source URL must not carry credentials"
    try:
        port = parts.port
    except ValueError:
        return f"source_url names an unusable port (got {source_url!r})"
    if port is not None or parts.netloc.endswith(":"):
        return f"a first-party source URL must not name a port (got {source_url!r})"
    if not parts.hostname:
        return f"source_url names no host (got {source_url!r})"
    return ""


def venue_problem(policy: Mapping[str, Any], exchange: Any, source_url: Any) -> str:
    """Why this URL is not an exact official host of this venue, or "" when it is.

    The venue comes from the catalogue candidate's own ``exchange`` field, never
    from the URL: letting the citation nominate its own venue would let a host
    prove itself.  Comparison is equality against a finite list -- a host that
    merely *ends with* a listed host (``www.shfe.com.cn.attacker.example``) is a
    different host and is refused here.

    The host is proved to be a *name* before it is compared at all.  A numeric
    address has many textual spellings that resolve to one machine, so string
    equality against a name list is the wrong question to ask about it: the
    answer would be "no" for the spelling in front of us and could be "yes" for
    another one, which is not a property a committed policy can state.  Whether
    any policy happens to list it is therefore never reached -- see
    ``numeric_host``.
    """
    problem = transport_problem(source_url)
    if problem:
        return problem
    host = urlsplit(source_url).hostname or ""
    problem = numeric_host_problem(host)
    if problem:
        return problem
    venue = str(exchange or "").strip()
    if not venue:
        return ("the candidate's catalogue record names no exchange, so no source host "
                "can be shown to be first party")
    hosts = official_hosts(policy, venue)
    if not hosts:
        return f"the source policy lists no official host for exchange {venue!r}"
    if host not in hosts:
        return (f"{host!r} is not an exact official host for {venue} "
                f"(allowed: {list(hosts)})")
    return ""


def source_problem(policy: Mapping[str, Any], exchange: Any, term: Any, source_url: Any,
                   source_authority: Any, source_kind: Any) -> str:
    """Every provenance check for one citation, in one call; "" when it passes.

    Offline by construction: the only inputs are these strings and the committed
    policy document.
    """
    return (citation_problem(term, source_authority, source_kind)
            or venue_problem(policy, exchange, source_url))


# ----------------------------------------------------------------- binding ---

def policy_binding(policy: Mapping[str, Any], policy_path: Any) -> Dict[str, Any]:
    """The policy identity a ledger is bound to: which document, and where from."""
    return {"source_policy_path": normalized_declared_path(policy_path),
            "source_policy_schema_version": policy.get("schema_version"),
            "source_policy_fingerprint": policy.get(POLICY_FINGERPRINT_FIELD)}


def policy_binding_mismatches(header: Mapping[str, Any], policy: Mapping[str, Any],
                              policy_path: Any) -> List[str]:
    """Every way a ledger header fails to describe the policy at this path.

    Returned rather than raised so the builder can decide what to do about a
    mismatch using the same comparison the reader applies.
    """
    problems: List[str] = []
    expected = policy_binding(policy, policy_path)
    for field in POLICY_BINDING_FIELDS:
        # Both sides of the path go through one normaliser, so a ledger written
        # with a different separator spelling is not reported as drift while two
        # genuinely different files still are.
        stored = (normalized_declared_path(header.get(field))
                  if field == "source_policy_path" else header.get(field))
        if stored != expected[field]:
            problems.append(f"{field} {header.get(field)!r} does not match the source policy's "
                            f"{expected[field]!r}")
    return problems


# ----------------------------------------------------------------- reading ---

def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    """json.load would keep the last of two identical keys; here that is an error.

    A file listing ``"SHFE"`` twice has two different intentions for one venue,
    and silently keeping the second is how a narrowed host list gets replaced by
    a wider one without anybody noticing.
    """
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise SourcePolicyError(f"duplicate key in source policy document: {key!r}")
        seen[key] = value
    return seen


def read_policy(path: str | Path) -> Dict[str, Any]:
    """Read a policy document, refusing duplicate keys; raises ``SourcePolicyError``."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=_reject_duplicate_keys)
    except SourcePolicyError:
        raise
    except (OSError, ValueError) as exc:
        raise SourcePolicyError(f"source policy could not be read: {exc}") from exc


def load_policy(path: str | Path) -> Dict[str, Any]:
    """Read and fully validate a policy; raises ``SourcePolicyError`` on any violation.

    There is no empty-policy fallback on purpose.  An unreadable catalogue can
    degrade to "nothing known"; an unreadable *policy* cannot degrade to
    anything, because every fallback is either a host list nobody wrote or a gate
    that passes everything.  Callers that must not raise -- the dashboard
    projection -- treat the absence of a policy as a refusal instead.
    """
    doc = read_policy(path)
    validate_policy(doc)
    return doc


def load_policy_or_none(path: str | Path) -> Optional[Dict[str, Any]]:
    """The validated policy *now on disk*, or ``None`` when there is not a usable one.

    For readers that must not raise -- the dashboard projection -- and that must
    fail closed on ``None`` rather than carry on without a host list.

    Deliberately not cached.  The document is a few hundred bytes and the
    admission endpoint is low frequency, so a cache saves nothing measurable, and
    what it would have to remember is *identity*.  Keyed on path, modification
    time and size, a replacement of the same length whose mtime was restored
    returns the policy that is no longer there: the endpoint would then re-prove
    the ledger's policy binding against a document it did not read, and report a
    ledger as bound to a host list that is not the one on disk.  That is exactly
    the substitution the binding exists to catch, so the file is read and
    validated on every call -- the guarantee the projection makes is that it
    proved the policy, and proving it means reading it.
    """
    try:
        return load_policy(path)
    except SourcePolicyError:
        return None
