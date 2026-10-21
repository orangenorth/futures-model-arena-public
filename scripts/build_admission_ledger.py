"""Build or refresh the non-executable contract admission ledger (offline).

The ledger is the evidence-preparation layer that sits *before* B0's structural
admission guard: one record per catalogue candidate, holding the public sources
behind its proposed multiplier, tick, sessions, margin and fee.  This script
never invents a term -- it only creates empty ``pending`` records for candidates
that do not have one yet, drops records for candidates the catalogue no longer
lists, and re-stamps the fingerprint and the catalogue binding.

**It will not rebind prepared evidence.**  If the ledger on disk was prepared
against a different catalogue path, schema, content hash, candidate count or
symbol set -- or under a different source policy, by content or by declared path
-- and any record holds gathered evidence, a block, a note or a preparer, the
rebuild is refused and the file is left exactly as it was (exit code 3).  That
includes a path-only move of either document: two files can be byte-identical
today and diverge tomorrow, and only one of them was read.  Re-pointing evidence
at a snapshot it was not gathered against, or at a host policy it was not judged
under, is a human decision with a human's reasons, not a side effect of running a
script.  An all-pending, wholly unknown ledger has nothing to mis-attribute and is
refreshed normally.

**It will not invent provenance either.**  A schema_version 1 (B1a) ledger has no
``source_authority`` / ``source_kind`` on its citations.  An all-pending v1 ledger
carries no citation and migrates as a pure header rewrite; a v1 ledger holding
prepared material is refused (exit code 3) rather than rebuilt with fields nobody
recorded.

It is entirely offline: it reads the committed catalogue, the committed source
policy and the existing ledger, and writes one file.  It does not fetch sources,
resolve a hostname, touch the contract table, or read any configuration beyond the
three paths given.

Usage:
    python scripts/build_admission_ledger.py
    python scripts/build_admission_ledger.py --catalogue config/contracts_universe_cn.json \\
        --source-policy config/admission_source_policy_cn.json \\
        --out config/contracts_admission_ledger_cn.json --check
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arena.admission import (  # noqa: E402
    DEFAULT_LEDGER_FILE, MIGRATABLE_SCHEMA_VERSIONS, LedgerError, build_ledger, rebind_refusals,
    read_ledger, schema_migration_refusals, validate_ledger,
)
from arena.admission_source_policy import (  # noqa: E402
    DEFAULT_SOURCE_POLICY_FILE, SourcePolicyError, load_policy,
)
from arena.universe import DEFAULT_UNIVERSE_FILE, load_catalogue  # noqa: E402


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _explain(refusals: List[str], remedy: str) -> None:
    """Report a refusal on stderr: every reason, then what a human should do next.

    Reasons name fields, symbols and paths -- never a proposed value, a source URL
    or a reviewer, so a refused rebuild does not become the channel that leaks what
    the projection is careful not to show.
    """
    for reason in refusals:
        print(f"  - {reason}", file=sys.stderr)
    print(f"  {remedy}", file=sys.stderr)


def existing_ledger(path: Path) -> Optional[Dict[str, Any]]:
    """The ledger already on disk, or None when there is not one yet.

    A ledger that no longer validates is *not* silently rebuilt from: doing so
    would erase whatever was wrong along with whatever was right.  The caller
    reports the failure and leaves the file alone.

    A document declaring an older, migratable schema is returned unvalidated:
    validating it against the *current* schema would report a missing source-policy
    binding, which is true but useless -- the interesting question is whether it
    holds evidence a migration would have to invent provenance for, and
    ``schema_migration_refusals`` is what answers that.
    """
    if not path.exists():
        return None
    doc = read_ledger(path)        # raises LedgerError, which main() reports
    if isinstance(doc, dict) and doc.get("schema_version") in MIGRATABLE_SCHEMA_VERSIONS:
        return doc
    validate_ledger(doc)           # structure only; both bindings are compared separately
    return doc


def write_ledger(path: Path, doc: Dict[str, Any], catalogue: Dict[str, Any], catalogue_path: str,
                 policy: Dict[str, Any], policy_path: str) -> None:
    """Write atomically, re-proving the full gate rather than trusting the caller."""
    validate_ledger(doc, catalogue=catalogue, catalogue_path=catalogue_path,
                    policy=policy, policy_path=policy_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)      # the reader sees either the old file or the new one
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the non-executable contract admission ledger.")
    parser.add_argument("--catalogue", default=DEFAULT_UNIVERSE_FILE)
    parser.add_argument("--source-policy", default=DEFAULT_SOURCE_POLICY_FILE,
                        help="the exact-host provenance policy citations are judged against")
    parser.add_argument("--out", default=DEFAULT_LEDGER_FILE)
    parser.add_argument("--prepared-at", default="", help="YYYY-MM-DD; defaults to today (Asia/Shanghai)")
    parser.add_argument("--check", action="store_true",
                        help="validate the existing ledger against the catalogue and the source "
                             "policy; write nothing")
    args = parser.parse_args(argv)

    catalogue = load_catalogue(args.catalogue)
    if not catalogue["available"]:
        print(f"catalogue {args.catalogue} is unavailable or failed validation; nothing to prepare", file=sys.stderr)
        return 2
    # No fallback policy, not even an empty one: a ledger built without a host list
    # would accept citations nobody had a rule for, which is worse than not
    # building one at all.
    try:
        policy = load_policy(args.source_policy)
    except SourcePolicyError as exc:
        print(f"source policy {args.source_policy} is unusable; nothing to prepare: {exc}",
              file=sys.stderr)
        return 2
    symbols = [c["symbol"] for c in catalogue["candidates"]]
    out = Path(args.out)

    try:
        if args.check:
            # Read-only by construction: nothing below this line writes.
            doc = read_ledger(out)
            validate_ledger(doc, catalogue=catalogue, catalogue_path=args.catalogue,
                            policy=policy, policy_path=args.source_policy)
            print(f"{out}: {doc['count']} record(s) valid against {args.catalogue} "
                  f"under {args.source_policy}")
            return 0
        existing = existing_ledger(out)
        if existing is not None:
            # A rebuild under the current schema would have to produce a
            # source_authority and a source_kind for every citation an older
            # ledger recorded without them.  Inventing those is exactly the
            # failure this layer exists to prevent, so a v1 ledger holding
            # prepared material stops here instead.
            refusals = schema_migration_refusals(existing)
            if refusals:
                print(f"{out}: refusing to migrate a ledger whose citations predate the "
                      f"provenance fields", file=sys.stderr)
                _explain(refusals, "re-check each affected citation by hand and record the "
                                   "publishing authority and document kind, or archive this "
                                   "ledger and start a new one. The file was left unchanged.")
                return 3
            # Rebinding rewrites the header that says which snapshot, which file,
            # and under which host policy the evidence was gathered.  Doing that
            # automatically would let material transcribed from one catalogue
            # version be re-presented as evidence for another, or a citation
            # accepted under a widened host list be presented as one the original
            # policy approved, so it stops here and a human resolves it.  A ledger
            # with nothing prepared in it has no evidence to mis-attribute and is
            # refreshed normally.
            refusals = rebind_refusals(existing, catalogue, args.catalogue, policy,
                                       args.source_policy)
            if refusals:
                print(f"{out}: refusing to rebind prepared evidence to a different catalogue "
                      f"or source policy", file=sys.stderr)
                _explain(refusals, "resolve the mismatch by hand: re-check the affected evidence "
                                   "against the new snapshot and policy, or archive this ledger "
                                   "and start a new one. The file was left unchanged.")
                return 3
        previous = (existing or {}).get("records") or {}
        doc = build_ledger(catalogue, previous, prepared_at=args.prepared_at or _now(),
                           catalogue_path=args.catalogue, policy=policy,
                           policy_path=args.source_policy)
        write_ledger(out, doc, catalogue, args.catalogue, policy, args.source_policy)
    except LedgerError as exc:
        print(f"{out}: {exc}", file=sys.stderr)
        return 1

    added = sorted(set(symbols) - set(previous))
    dropped = sorted(set(previous) - set(symbols))
    print(f"{out}: {doc['count']} record(s) bound to {args.catalogue} under {args.source_policy}; "
          f"{len(added)} added, {len(dropped)} dropped")
    if added:
        print(f"  added:   {', '.join(added)}")
    if dropped:
        print(f"  dropped: {', '.join(dropped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
