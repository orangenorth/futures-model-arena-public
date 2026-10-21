"""Discover candidate CN futures continuous symbols from Sina's public node list.

Pipeline (each step is a separate public read):

  1. GET qihuohangqing.js               -> ARRFUTURESNODES: (exchange group, node id, name)
  2. GET Market_Center.getHQFuturesData -> the node's continuous code, e.g. RB0
  3. GET hq.sinajs.cn/list=nf_RB0       -> quote availability
  4. GET InnerFuturesNewService...      -> 5m / 15m / 1h history availability (--probe-history)
  5. write config/contracts_universe_cn.json

The number of candidates is whatever upstream lists today; it drifts as
varieties are listed or delisted, so nothing here targets a fixed count.  The
catalogue carries identity plus availability only -- multiplier, tick, margin,
fee and sessions cannot be derived from a code list and are structurally
rejected before the file is written.

Usage:
    python scripts/discover_contract_universe.py --out config/contracts_universe_cn.json \\
        --probe-history --timeout 10 --workers 4
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arena.universe import (  # noqa: E402
    AVAILABILITY_FIELDS, CANDIDATE_FIELDS, DISCLAIMER, SCHEMA_VERSION,
    SNAPSHOT_COMPLETE, SNAPSHOT_PARTIAL, SYMBOL_PATTERN, content_hash,
    validate_catalogue,
)

# The writer and the reader share one validator (arena/universe.py): a rule that
# only the writer enforced would be bypassed by any hand-edited file.
__all__ = ["discover", "build_catalogue", "validate_catalogue", "write_catalogue", "main"]

NODES_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/view/js/qihuohangqing.js"
CONTRACTS_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQFuturesData"
QUOTE_URL = "https://hq.sinajs.cn/list=nf_{symbol}"
HISTORY_URL = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService.getFewMinLine"
HEADERS = {
    "Accept": "*/*",
    "Referer": "https://finance.sina.com.cn/futures/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
HISTORY_PERIODS = (("history_5m_ok", "5"), ("history_15m_ok", "15"), ("history_1h_ok", "60"))
SOURCE = "sina/qihuohangqing.js + Market_Center.getHQFuturesData + hq.sinajs.cn + InnerFuturesNewService"
TZ = ZoneInfo("Asia/Shanghai")

# The same asset also lists spot and overseas boards (渤海商品 / 昆明泛亚 / 外盘期货).
# They are not domestic futures varieties, so they are excluded by name rather
# than left to fail a probe, and every exclusion is reported on stderr.
DOMESTIC_EXCHANGE_GROUPS = frozenset({"CZCE", "DCE", "SHFE", "INE", "CFFEX", "GFEX"})

MAX_WORKERS = 8          # hard ceiling: upstream is a public page, not an API quota
DEFAULT_WORKERS = 4
CONTRACTS_PAGE_SIZE = 40
MAX_CONTRACTS_PAGES = 64
_SYMBOL_RE = re.compile(SYMBOL_PATTERN)
_QUOTE_MIN_FIELDS = 17   # matches arena/market/sina_cn.py's quote layout check


class Node(NamedTuple):
    exchange: str
    node: str
    name: str


class Candidate(NamedTuple):
    symbol: str
    name: str
    exchange: str
    source_group: str = ""   # the upstream board the node was listed under
    source_node: str = ""    # the upstream node id the code was resolved from


class ProbeError(Exception):
    """Upstream failure for one candidate, carrying a short category only.

    Raw provider text is deliberately not propagated: the catalogue is a
    committed artefact and must not accumulate endpoint diagnostics.
    """

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


# ---------------------------------------------------------------- parsing ----

def _match_bracket(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the bracket opened at ``start``; -1 when unbalanced."""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


_GROUP_RE = re.compile(r"(\w+)\s*:\s*\[")
_ENTRY_RE = re.compile(r"\[\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([A-Za-z0-9_]+)['\"]")


def parse_nodes(js_text: str) -> List[Node]:
    """Extract (exchange, node, name) triples from the ARRFUTURESNODES literal.

    The asset is a hand-maintained JS file, so entries that do not match the
    documented shape are skipped instead of aborting the whole discovery run.
    Duplicate node ids keep their first occurrence.
    """
    anchor = js_text.find("ARRFUTURESNODES")
    if anchor < 0:
        raise ValueError("ARRFUTURESNODES not found")
    brace = js_text.find("{", anchor)
    end = _match_bracket(js_text, brace, "{", "}") if brace >= 0 else -1
    if end < 0:
        raise ValueError("ARRFUTURESNODES literal is unbalanced")
    body = js_text[brace + 1:end - 1]

    nodes: List[Node] = []
    seen: set = set()
    for group in _GROUP_RE.finditer(body):
        exchange = group.group(1).upper()
        bracket = body.find("[", group.end() - 1)
        stop = _match_bracket(body, bracket, "[", "]") if bracket >= 0 else -1
        if stop < 0:
            continue
        for entry in _ENTRY_RE.finditer(body[bracket + 1:stop - 1]):
            name, node = entry.group(1).strip(), entry.group(2).strip()
            if not node or node in seen:
                continue
            seen.add(node)
            nodes.append(Node(exchange=exchange, node=node, name=name))
    return nodes


def pick_continuous(payload: object, fallback_name: str = "", fallback_exchange: str = "",
                    source_group: str = "", source_node: str = "") -> Optional[Candidate]:
    """Pick the node's continuous contract (``RB0``) out of its contract list.

    The code is read from the row's ``symbol`` identifier field only -- never
    derived from the Chinese name or from the node id, which would amount to
    guessing.  Sina returns the continuous code alongside the dated contracts;
    only the continuous form is a stable identifier, so a node without one
    yields no candidate at all.
    """
    if not isinstance(payload, list):
        return None
    for row in payload:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        if not _SYMBOL_RE.match(symbol):
            continue
        # Upstream names the continuous series "螺纹钢连续"; the node name is the
        # variety itself and reads better in the dashboard.
        upstream_name = re.sub(r"连续$", "", str(row.get("name", "")).strip())
        name = fallback_name or upstream_name or symbol
        exchange = (str(row.get("exchange", "")).strip() or fallback_exchange).upper()
        return Candidate(symbol=symbol, name=name, exchange=exchange,
                         source_group=source_group, source_node=source_node)
    return None


def quote_available(text: str, symbol: str) -> bool:
    """Mirror arena/market/sina_cn.py: a usable quote is a non-empty, wide row."""
    match = re.search(rf'var\s+hq_str_nf_{re.escape(symbol)}="([^"]*)";', text, re.IGNORECASE)
    if not match or not match.group(1):
        return False
    return len(match.group(1).split(",")) > _QUOTE_MIN_FIELDS


def history_available(text: str) -> bool:
    """A usable history response parses into a non-empty array of bars."""
    start, end = text.find("(["), text.rfind("])")
    if start < 0 or end < 0:
        return False
    try:
        rows = json.loads(text[start + 1:end + 1])
    except ValueError:
        return False
    return isinstance(rows, list) and len(rows) > 0


# ------------------------------------------------------------- catalogue ----

def build_catalogue(candidates: List[Dict[str, object]], fetched_at: str, source: str = SOURCE,
                    source_url: str = NODES_URL,
                    snapshot_status: str = SNAPSHOT_COMPLETE) -> Dict[str, object]:
    """Assemble the catalogue document and prove it carries no matching fields."""
    entries: Dict[str, Dict[str, object]] = {}
    for item in sorted(candidates, key=lambda c: (str(c.get("exchange", "")), str(c.get("symbol", "")))):
        symbol = str(item.get("symbol", ""))
        if not _SYMBOL_RE.match(symbol) or symbol in entries:
            continue
        entries[symbol] = {field: item.get(field) for field in CANDIDATE_FIELDS}
    doc = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "source_url": source_url,
        "note": DISCLAIMER,
        "fetched_at": fetched_at,
        # Whether this run saw the whole list end to end; a partial run is never
        # allowed to replace a complete predecessor (see write_catalogue).
        "snapshot_status": snapshot_status,
        "content_hash": content_hash(entries),
        # Whatever upstream listed on this run; never rounded to a target.
        "count": len(entries),
        "candidates": entries,
    }
    validate_catalogue(doc)
    return doc


def write_catalogue(path: str | Path, doc: Dict[str, object]) -> bool:
    """Write the catalogue atomically; never trade a complete snapshot for a partial one.

    A run that lost part of the upstream list still holds accurate rows, but as
    a *file* it is strictly worse than the last complete snapshot -- replacing
    one with the other would silently shrink the committed catalogue.  So the
    partial result is discarded and the previous file stands.

    The document is re-validated here rather than trusted from the caller.
    ``build_catalogue`` already validates, but this function is a reusable helper
    that accepts an arbitrary mapping, and no helper should be able to persist a
    structurally forbidden catalogue by accident.  Validation is cheap and this
    path runs once per discovery run.
    """
    validate_catalogue(doc)
    out = Path(path)
    if doc.get("snapshot_status") != SNAPSHOT_COMPLETE and _stored_status(out) == SNAPSHOT_COMPLETE:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)   # the reader sees either the old file or the new one
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _stored_status(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return ""
    return str(stored.get("snapshot_status", "")) if isinstance(stored, dict) else ""


# ---------------------------------------------------------------- probing ----

def probe_candidate(candidate: Candidate, fetch: Callable[[str, Optional[dict]], str],
                    probe_history: bool, now: Callable[[], str]) -> Dict[str, object]:
    """Probe one candidate; its failure is recorded, never raised to the batch."""
    record: Dict[str, object] = {"symbol": candidate.symbol,
                                 # Persisted alongside the map key so the code
                                 # travels with the record it was read from.
                                 "continuous_symbol": candidate.symbol,
                                 "name": candidate.name,
                                 "exchange": candidate.exchange,
                                 "source_group": candidate.source_group,
                                 "source_node": candidate.source_node, "probed_at": now(),
                                 "probe_status": "ok", "probe_error": ""}
    for field in AVAILABILITY_FIELDS:
        record[field] = None
    try:
        record["quote_ok"] = quote_available(fetch(QUOTE_URL.format(symbol=candidate.symbol), None), candidate.symbol)
    except ProbeError as e:
        record["probe_status"], record["probe_error"] = "failed", e.category
        return record
    if not probe_history:
        record["probe_status"] = "quote_only"
        return record
    for field, period in HISTORY_PERIODS:
        try:
            record[field] = history_available(fetch(HISTORY_URL, {"symbol": candidate.symbol, "type": period}))
        except ProbeError as e:
            record["probe_status"], record["probe_error"] = "partial", e.category
    return record


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _make_fetch(client, timeout: float) -> Callable[[str, Optional[dict]], str]:
    def fetch(url: str, params: Optional[dict]) -> str:
        try:
            r = client.get(url, params=params, timeout=timeout)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise ProbeError(type(e).__name__[:40]) from None
        return r.content.decode("gb18030", errors="replace")
    return fetch


def discover(fetch: Callable[[str, Optional[dict]], str], probe_history: bool, workers: int,
             now: Callable[[], str] = _now) -> Dict[str, object]:
    """Run the full discovery against an injected fetcher (tests pass a fake one)."""
    fetched_at = now()
    listed = parse_nodes(fetch(NODES_URL, None))
    nodes = [n for n in listed if n.exchange in DOMESTIC_EXCHANGE_GROUPS]
    dropped = sorted({n.exchange for n in listed} - DOMESTIC_EXCHANGE_GROUPS)
    print(f"[universe] {len(listed)} nodes listed upstream, {len(nodes)} on domestic futures boards"
          + (f"; excluded boards: {', '.join(dropped)}" if dropped else ""), file=sys.stderr)

    def resolve(node: Node) -> Tuple[bool, Optional[Candidate]]:
        """Return (reached upstream, candidate).  A node with no continuous code
        is a complete answer; an unreachable node is missing coverage."""
        # A node maps to at most one continuous symbol, but the source does not
        # guarantee that the X0 row is on the first page.  Continue only while
        # a full page could still hide that row.  A repeated page is a defensive
        # stop for upstreams that ignore the ``page`` parameter; it is complete
        # only because no unseen rows can be learned by repeating it.
        seen_pages: set[str] = set()
        for page in range(1, MAX_CONTRACTS_PAGES + 1):
            params = {"page": page, "num": CONTRACTS_PAGE_SIZE, "sort": "position", "asc": 0,
                      "node": node.node, "base": "futures"}
            try:
                payload = json.loads(fetch(CONTRACTS_URL, params))
            except (ProbeError, ValueError):
                return False, None
            if not isinstance(payload, list):
                return False, None
            fingerprint = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if fingerprint in seen_pages:
                return True, None
            seen_pages.add(fingerprint)
            candidate = pick_continuous(payload, fallback_name=node.name, fallback_exchange=node.exchange,
                                        source_group=node.exchange, source_node=node.node)
            if candidate is not None:
                return True, candidate
            if len(payload) < CONTRACTS_PAGE_SIZE:
                return True, None
        # Hitting the hard cap is not silently accepted as a complete node: the
        # remaining pages could contain the only continuous code.
        return False, None

    pool_size = max(1, min(workers, MAX_WORKERS))
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        outcomes = list(pool.map(resolve, nodes))
        unreachable = sum(1 for reached, _ in outcomes if not reached)
        unique: Dict[str, Candidate] = {}
        for _, candidate in outcomes:
            if candidate is not None:
                unique.setdefault(candidate.symbol, candidate)
        print(f"[universe] {len(unique)} continuous codes resolved"
              + (f"; {unreachable} nodes unreachable" if unreachable else ""), file=sys.stderr)
        records = list(pool.map(lambda c: probe_candidate(c, fetch, probe_history, now), unique.values()))

    # Partial the moment any coverage was lost: an unreachable node, a failed or
    # half-finished probe, or an empty result.  Failures leave availability at
    # None -- a network error is never recorded as quote_ok=False.
    complete = (bool(records) and not unreachable
                and all(r["probe_status"] in ("ok", "quote_only") for r in records))
    status = SNAPSHOT_COMPLETE if complete else SNAPSHOT_PARTIAL
    return build_catalogue(records, fetched_at=fetched_at, snapshot_status=status)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Discover candidate CN futures continuous symbols (read-only).")
    parser.add_argument("--out", default="config/contracts_universe_cn.json", help="catalogue output path")
    parser.add_argument("--probe-history", action="store_true", help="also probe 5m/15m/1h availability")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"bounded concurrency, capped at {MAX_WORKERS}")
    args = parser.parse_args(argv)
    if args.workers > MAX_WORKERS:
        print(f"[universe] workers capped at {MAX_WORKERS}", file=sys.stderr)

    import httpx   # imported here so the parsers stay importable without network deps

    with httpx.Client(headers=HEADERS, timeout=args.timeout, follow_redirects=True) as client:
        doc = discover(_make_fetch(client, args.timeout), args.probe_history, args.workers)

    if not write_catalogue(args.out, doc):
        print(f"[universe] run was {doc['snapshot_status']}; kept the previous complete snapshot "
              f"at {args.out} (discarded {doc['count']} candidates)", file=sys.stderr)
        return 1
    print(f"[universe] wrote {doc['count']} candidates ({doc['snapshot_status']}) to {args.out}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
