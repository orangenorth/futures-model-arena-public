"""Persistent, research-only, per-product four-model debate orchestration.

The existing pre-open room intentionally compressed a market-wide scan into a
small representative packet.  This module is a different lane: one discovered
continuous contract receives its own immutable facts, debate session, eight
planned model slots, and terminal status.  It never touches the execution
symbol list, accounts, orders, positions, or risk manager.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import ProductResearchCfg
from .db import Database
from .research_scan import FullUniverseScanner, SinaResearchReader


log = logging.getLogger("arena.product_research")

MAPPING_RULE = "one upstream-listed continuous X0 contract per product; no dated delivery-month expansion"
PHASE = "product_research"
SCHEMA_VERSION = "product-research/v1"
UNCERTAIN_DELIVERY_ERRORS = frozenset({
    "ResearchRequestTimedOut", "ResearchConnectionFailed", "ResearchRequestFailed", "ResearchStageTimedOut",
})
def _stable_key(*parts: object) -> str:
    body = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _safe_text(value: object, limit: int = 220) -> str:
    return str(value or "").replace("\x00", " ").strip()[:limit]


def discover_catalogue(path: Path, *, timeout_sec: float, workers: int) -> Mapping[str, Any]:
    """Fetch and persist one fresh, source-supported continuous-contract catalogue."""
    from scripts import discover_contract_universe as discovery  # local project module, not a shell subprocess
    import httpx

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=discovery.HEADERS, timeout=float(timeout_sec), follow_redirects=True) as client:
        document = discovery.discover(
            discovery._make_fetch(client, float(timeout_sec)),
            True,
            int(workers),
        )
    discovery.write_catalogue(path, dict(document))
    return document


def _facts_for_scan_item(item: Mapping[str, Any]) -> List[dict]:
    """Build a compact, product-specific immutable fact set from a fresh scan row."""
    symbol = str(item["symbol"])
    return [
        {"id": f"{symbol}:quote", "kind": "quote", "symbol": symbol,
         "price": item.get("latest_price"), "change_pct": item.get("change_pct")},
        {"id": f"{symbol}:trend", "kind": "technical_trend", "symbol": symbol,
         "trend": item.get("trend")},
        {"id": f"{symbol}:score", "kind": "technical_score", "symbol": symbol,
         "score": item.get("score")},
    ]


def _fact_id_contract(facts: Sequence[Mapping[str, Any]]) -> tuple[list[str], str]:
    """Provide exact valid citation IDs without weakening result validation."""
    fact_ids = [str(fact["id"]) for fact in facts if fact.get("id")]
    if not fact_ids:
        raise ValueError("product research facts require at least one id")
    return fact_ids, json.dumps(fact_ids, ensure_ascii=False, separators=(",", ":"))


def _retry_contract_note(attempt: int) -> str:
    if int(attempt) <= 1:
        return ""
    return (
        "这是第 " + str(int(attempt)) + " 次受限重试。上一次 JSON 未通过结构校验；"
        "请逐字复制下方允许值，不要翻译、缩写、拼接或自造 fact id / 模型编号。\n"
    )


def _opening_prompt(symbol: str, name: str, exchange: str, facts: Sequence[Mapping[str, Any]], *,
                    attempt: int = 1) -> str:
    fact_ids, fact_id_contract = _fact_id_contract(facts)
    example = fact_ids[0]
    return (
        "你参加的是模拟竞赛的单品种赛前研究辩论，不是下单环节。只可使用下方不可变事实；"
        "不得给出交易指令、仓位、目标价或未提供的新闻。目标品种："
        f"{symbol}（{name}，{exchange}）。只返回 JSON："
        '{"stance":"bullish|bearish|neutral","confidence":0.0,"thesis":"结论",'
        f'"supporting_fact_ids":["{example}"],"counter_fact_ids":["{example}"],'
        '"counter_argument":"最强反方依据","invalidation":"证伪条件","uncertainties":["不确定性"]}\n\n'
        + _retry_contract_note(attempt)
        + "允许的 fact id 仅为：" + fact_id_contract + "。supporting_fact_ids 与 counter_fact_ids 必须是非空数组，"
        "数组中每个值都必须从该列表逐字复制。\n"
        "事实集：\n"
        + json.dumps(list(facts), ensure_ascii=False, separators=(",", ":"))
    )


def _rebuttal_prompt(symbol: str, name: str, exchange: str, facts: Sequence[Mapping[str, Any]],
                     openings: Iterable[Mapping[str, Any]], self_id: str, *, attempt: int = 1) -> str:
    fact_ids, fact_id_contract = _fact_id_contract(facts)
    example = fact_ids[0]
    peers = [
        {key: row.get(key) for key in (
            "trader_id", "stance", "confidence", "thesis", "supporting_fact_ids", "counter_fact_ids",
            "counter_argument", "invalidation",
        )}
        for row in openings
    ]
    valid_targets = [str(row["trader_id"]) for row in peers if row.get("trader_id") != self_id]
    if not valid_targets:
        raise ValueError("product research rebuttal requires another participant")
    target = valid_targets[0]
    target_contract = json.dumps(valid_targets, ensure_ascii=False, separators=(",", ":"))
    return (
        "你参加的是模拟竞赛的单品种赛前研究反证，不是下单环节。只可使用下方不可变事实与第一轮观点；"
        "不得给出交易指令、仓位、目标价或未提供的新闻。目标品种："
        f"{symbol}（{name}，{exchange}）。只返回 JSON："
        '{"stance":"bullish|bearish|neutral","confidence":0.0,'
        f'"target_trader_id":"{target}","challenge":"可核验挑战",'
        f'"challenge_fact_ids":["{example}"],"revised_thesis":"修订结论","invalidation":"证伪条件"}}\n\n'
        + _retry_contract_note(attempt)
        + "target_trader_id 只能是以下其他模型编号之一：" + target_contract + "。"
        "challenge_fact_ids 必须是非空数组，且每个值只能从允许的 fact id 列表逐字复制："
        + fact_id_contract + "。\n"
        "第一轮观点：\n" + json.dumps(peers, ensure_ascii=False, separators=(",", ":"))
        + "\n\n事实集：\n"
        + json.dumps(list(facts), ensure_ascii=False, separators=(",", ":"))
    )


class ProductResearchService:
    """One active full-universe run with durable per-product resumability."""

    def __init__(
        self,
        db: Database,
        *,
        comp_id: Callable[[], int],
        cfg: ProductResearchCfg,
        catalogue_dir: str | Path,
        calendar_path: str | Path,
        participants: Callable[[], Sequence[Mapping[str, Any]]],
        call_model: Callable[[str, str, str, set[str], set[str]], Dict[str, object]],
        market_timeout_sec: float = 10.0,
        can_start: Optional[Callable[[], bool]] = None,
        submit_guard: Optional[Callable[[], ContextManager[Any]]] = None,
        discovery_factory: Optional[Callable[[Path], Mapping[str, Any]]] = None,
        scan_factory: Optional[Callable[[Path, threading.Event], Mapping[str, Any]]] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self._db = db
        self._comp_id = comp_id
        self._cfg = cfg
        self._catalogue_dir = Path(catalogue_dir)
        self._calendar_path = Path(calendar_path)
        self._participants = participants
        self._call_model = call_model
        self._market_timeout_sec = float(market_timeout_sec)
        self._can_start = can_start or (lambda: True)
        self._submit_guard = submit_guard or nullcontext
        self._discovery_factory = discovery_factory or self._discover_catalogue
        self._scan_factory = scan_factory or self._scan_catalogue
        self._on_change = on_change or (lambda: None)
        self._lock = threading.RLock()
        self._running_run_id: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel_event: Optional[threading.Event] = None

    def public_status(self) -> Dict[str, Any]:
        with self._lock:
            running = self._running_run_id
        return {"running": running is not None, "run_id": running, "latest": self._db.latest_product_research_run(self._comp_id())}

    def start(self, trigger: str = "manual", *, sample_limit: Optional[int] = None) -> Tuple[bool, str, Optional[int]]:
        if not self._cfg.enabled:
            return False, "全品种赛前论证已在配置中关闭", None
        if not self._can_start():
            return False, "引擎未运行，未发起全品种赛前论证", None
        if sample_limit is not None and int(sample_limit) < 1:
            return False, "小样品种数必须大于零", None
        with self._lock:
            if self._running_run_id is not None:
                return False, "已有全品种赛前论证正在进行", self._running_run_id
            run_key = uuid.uuid4().hex
            catalogue_path = self._catalogue_dir / f"{run_key}.json"
            stored_trigger = (f"{trigger}_sample_{int(sample_limit)}" if sample_limit is not None else trigger)
            run_id = self._db.create_product_research_run(
                self._comp_id(), run_key, stored_trigger, MAPPING_RULE, str(catalogue_path),
                self._cfg.candidate_batch_size, self._cfg.model_concurrency, self._cfg.max_attempts,
            )
            self._start_worker_locked(run_id, discover=True, sample_limit=sample_limit, only_failed=False)
        self._on_change()
        if sample_limit is not None:
            return True, "已创建全量发现、小样模型验收运行；未处理品种将保留为待处理，需续跑才能完成全量验收", run_id
        return True, "已创建全品种赛前论证运行；先刷新品种目录和行情，再逐品种进入四模型两轮辩论", run_id

    def resume(self, run_id: int) -> Tuple[bool, str]:
        if not self._can_start():
            return False, "引擎未运行，不能续跑"
        run = self._db.product_research_run_by_id(int(run_id))
        if not run:
            return False, "未找到全品种研究运行"
        if run["status"] in {"complete", "complete_with_exceptions"}:
            return False, "该运行已经结束；请查看结果或仅补跑明确失败项"
        with self._lock:
            if self._running_run_id is not None:
                return False, "已有全品种赛前论证正在进行"
            self._start_worker_locked(int(run_id), discover=False, sample_limit=None, only_failed=False)
        self._on_change()
        return True, "已续跑；已完成模型调用不会重发，未确认投递不会自动补发"

    def retry_failed(self, run_id: int) -> Tuple[bool, str]:
        """Resume only deterministic failures; delivery-unknown calls stay quarantined."""
        run = self._db.product_research_run_by_id(int(run_id))
        if not run:
            return False, "未找到全品种研究运行"
        if not self._can_start():
            return False, "引擎未运行，不能补跑"
        if not self._db.has_definitive_product_research_failures(int(run_id)):
            return False, "没有可安全补跑的确定性失败项；投递状态不确定的调用不会自动重发"
        retry_ceiling = int(self._cfg.max_attempts) + int(self._cfg.manual_retry_limit)
        current_attempt_cap = int(run["max_attempts"])
        if current_attempt_cap >= retry_ceiling:
            return False, "确定性失败项的人工补跑上限已用完；投递状态不确定的调用仍不会自动重发"
        with self._lock:
            if self._running_run_id is not None:
                return False, "已有全品种赛前论证正在进行"
            # Persist the extra bound before the worker starts.  A restart sees
            # the same cap and can never turn repeated button clicks into an
            # unbounded provider retry loop.
            self._db.update_product_research_run(
                int(run_id), max_attempts=current_attempt_cap + 1, status="queued", error="",
            )
            self._start_worker_locked(int(run_id), discover=False, sample_limit=None, only_failed=True)
        self._on_change()
        return True, "已安排一轮确定性失败项补跑；已完成调用不会重发，投递状态不确定的调用不会自动重发"

    def stop(self) -> None:
        with self._lock:
            run_id = self._running_run_id
            if self._cancel_event is not None:
                self._cancel_event.set()
        if run_id is not None:
            self._db.update_product_research_run(run_id, status="interrupted", error="stopped")
            self._on_change()

    def _start_worker_locked(self, run_id: int, *, discover: bool, sample_limit: Optional[int],
                             only_failed: bool) -> None:
        self._running_run_id = run_id
        self._cancel_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(run_id, discover, sample_limit, only_failed, self._cancel_event),
            name=f"product-research-{run_id}", daemon=True,
        )
        self._thread.start()

    def _run(self, run_id: int, discover: bool, sample_limit: Optional[int], only_failed: bool,
             cancel_event: threading.Event) -> None:
        try:
            if discover:
                if not self._discover_and_materialise(run_id, cancel_event):
                    return
            else:
                self._db.recover_product_research_submissions(run_id)
                self._db.update_product_research_run(run_id, status="running", error="")
            self._process_run(run_id, cancel_event, sample_limit=sample_limit, only_failed=only_failed)
        except Exception:  # noqa: BLE001 - public state stores only a fixed category
            log.exception("product research run %s failed", run_id)
            self._db.update_product_research_run(run_id, status="failed", error="runner_failed", completed=True)
        finally:
            with self._lock:
                if self._running_run_id == run_id:
                    self._running_run_id = None
                    self._cancel_event = None
            self._on_change()

    def _discover_catalogue(self, path: Path) -> Mapping[str, Any]:
        """Fetch a fresh paginated catalogue without overwriting the configured snapshot."""
        return discover_catalogue(
            path,
            timeout_sec=self._cfg.discovery_timeout_sec,
            workers=self._cfg.discovery_workers,
        )

    def _scan_catalogue(self, path: Path, cancel_event: threading.Event) -> Mapping[str, Any]:
        reader = SinaResearchReader(timeout_sec=self._market_timeout_sec)
        scanner = FullUniverseScanner(
            path, reader, calendar_path=self._calendar_path, cancel_event=cancel_event,
        )
        return scanner.run()

    def _discover_and_materialise(self, run_id: int, cancel_event: threading.Event) -> bool:
        run = self._db.product_research_run_by_id(run_id)
        if not run:
            return False
        path = Path(str(run["catalogue_path"]))
        try:
            catalogue = self._discovery_factory(path)
        except Exception:  # noqa: BLE001 - no fallback to a stale static universe
            self._db.update_product_research_run(run_id, status="failed", discovery_status="failed",
                                                 error="discovery_failed", completed=True)
            return False
        if cancel_event.is_set():
            self._db.update_product_research_run(run_id, status="interrupted", error="stopped")
            return False
        self._db.update_product_research_run(
            run_id, discovery_status=str(catalogue.get("snapshot_status") or "unknown"),
            catalogue_path=str(path), catalogue_hash=str(catalogue.get("content_hash") or ""),
            catalogue_fetched_at=str(catalogue.get("fetched_at") or ""),
        )
        try:
            manifest = self._scan_factory(path, cancel_event)
            scan_run_id = self._db.append_research_scan(manifest)
        except Exception:  # noqa: BLE001 - the scan cannot silently become an eligible universe
            self._db.update_product_research_run(run_id, status="failed", error="scan_failed", completed=True)
            return False
        if cancel_event.is_set():
            self._db.update_product_research_run(run_id, status="interrupted", scan_run_id=scan_run_id, error="stopped")
            return False
        self._db.update_product_research_run(run_id, scan_run_id=scan_run_id, status="queued", error="")
        self._materialise_scan(run_id, run, manifest)
        self._on_change()
        return True

    def _materialise_scan(self, run_id: int, run: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
        participants = self._participant_rows()
        run_key = str(run["run_key"])
        run_competition_id = int(run["competition_id"])
        rows: List[dict] = []
        eligible_index = 0
        for scanned in sorted(manifest.get("items") or [], key=lambda value: str(value.get("symbol") or "")):
            symbol = str(scanned.get("symbol") or "").upper()
            if not symbol:
                continue
            common = {
                "symbol": symbol,
                "name": _safe_text(scanned.get("name") or symbol, 120),
                "exchange": _safe_text(scanned.get("exchange"), 32),
                "mapping_symbol": symbol,
                "scan_stage": _safe_text(scanned.get("stage"), 32),
            }
            if scanned.get("outcome") != "scanned":
                rows.append({**common, "status": "excluded", "exclusion_reason": _safe_text(scanned.get("reason"), 80)})
                continue
            eligible_index += 1
            batch_no = (eligible_index - 1) // self._cfg.candidate_batch_size + 1
            facts = _facts_for_scan_item(scanned)
            product_manifest = self._product_manifest(run_id, symbol, common, facts, manifest, participants)
            session_key = f"product-research:{run_key}:{symbol}"
            debate_id = self._db.reserve_debate_session(
                run_competition_id, session_key, PHASE, f"全品种赛前论证 · {symbol}", None, product_manifest,
            )
            if debate_id is None:
                existing = self._db.debate_session_by_key(run_competition_id, session_key)
                if not existing:
                    raise RuntimeError("product debate reservation failed")
                debate_id = int(existing["id"])
            calls = [
                {"debate_id": debate_id, "stage": stage, "trader_id": participant["id"], "attempt": 1,
                 "dispatch_key": _stable_key(SCHEMA_VERSION, run_key, symbol, stage, participant["id"], 1)}
                for stage in ("opening", "rebuttal") for participant in participants
            ]
            rows.append({**common, "status": "pending", "batch_no": batch_no, "debate_id": debate_id, "calls": calls})
        self._db.create_product_research_items(run_id, rows)
        self._db.refresh_product_research_counts(run_id)

    @staticmethod
    def _product_manifest(run_id: int, symbol: str, item: Mapping[str, Any], facts: Sequence[Mapping[str, Any]],
                          scan: Mapping[str, Any], participants: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
        market_as_of = scan.get("completed_at")
        frozen = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "mapping_rule": MAPPING_RULE,
            "product": {"symbol": symbol, "name": item["name"], "exchange": item["exchange"]},
            "facts": list(facts), "participants": list(participants),
            "scan": {"catalogue": scan.get("catalogue"), "market": scan.get("market"), "completed_at": market_as_of},
        }
        snapshot_hash = hashlib.sha256(
            json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            **frozen,
            "analysis_id": f"{SCHEMA_VERSION}:{run_id}:{symbol}",
            "snapshot_hash": snapshot_hash,
            "market_as_of": market_as_of,
            "global_analysis": {"scope": "single_product_four_model_debate", "symbol": symbol},
        }

    def _process_run(self, run_id: int, cancel_event: threading.Event, *, sample_limit: Optional[int] = None,
                     only_failed: bool = False) -> None:
        self._db.update_product_research_run(run_id, status="running", error="")
        offset = 0
        items: List[dict] = []
        while True:
            page = self._db.product_research_items(run_id, limit=200, offset=offset)
            if not page:
                break
            items.extend(page)
            offset += len(page)
        processed = 0
        for item in items:
            if cancel_event.is_set():
                self._db.update_product_research_run(run_id, status="interrupted", error="stopped")
                return
            if item.get("status") == "excluded":
                continue
            if only_failed and item.get("status") not in {"partial", "failed"}:
                continue
            if sample_limit is not None and processed >= sample_limit:
                continue
            processed += 1
            self._process_item(run_id, str(item["symbol"]), cancel_event)
        current = self._db.refresh_product_research_counts(run_id)
        if not current or cancel_event.is_set():
            if current:
                self._db.update_product_research_run(run_id, status="interrupted", error="stopped")
            return
        if only_failed and (current["pending_count"] or current["running_count"]):
            # A retry endpoint must never silently turn into a full-run endpoint.
            # Pending work remains visible for the explicit Resume action.
            self._db.update_product_research_run(run_id, status="interrupted", error="pending_not_requested")
        elif sample_limit is not None and (current["pending_count"] or current["running_count"]):
            # A small real-model proof must never masquerade as a truncated
            # full-universe run.  Every untested item remains visible and can
            # be resumed later from the identical, already-materialised run.
            self._db.update_product_research_run(run_id, status="interrupted", error="sample_limit_reached")
        elif current["pending_count"] or current["running_count"]:
            self._db.update_product_research_run(run_id, status="interrupted", error="unfinished")
        elif current["partial_count"] or current["failed_count"]:
            self._db.update_product_research_run(run_id, status="complete_with_exceptions", completed=True)
        else:
            self._db.update_product_research_run(run_id, status="complete", completed=True)
        self._on_change()

    def _process_item(self, run_id: int, symbol: str, cancel_event: threading.Event) -> None:
        item = self._db.product_research_item(run_id, symbol)
        if not item or item.get("status") in {"excluded", "complete"}:
            return
        self._db.mark_product_research_item_started(run_id, symbol)
        item = self._db.product_research_item(run_id, symbol)
        if not item:
            return
        if self._has_unknown(item):
            self._settle_item(run_id, symbol)
            return
        # A manual retry of a deterministic failure is allowed to write its
        # replacement turn only after the product presentation session returns
        # to its active research state.  Unknown delivery is handled above and
        # cannot take this transition.
        self._db.reopen_product_research_debate_session(int(item["debate_id"]))
        opening = self._latest_stage(item, "opening")
        if not self._all_complete(opening):
            self._run_stage(run_id, item, "opening", cancel_event)
            item = self._db.product_research_item(run_id, symbol)
            if not item or not self._all_complete(self._latest_stage(item, "opening")):
                if item:
                    self._db.skip_pending_product_research_calls(run_id, symbol, "rebuttal", "opening_incomplete")
                self._settle_item(run_id, symbol)
                return
        # A previous deterministic opening failure may have made rebuttal
        # slots ``opening_incomplete`` before an explicit retry repaired the
        # opening.  Those slots were never submitted, so restoring only this
        # marker is safe and lets the product reach a real two-stage debate.
        self._db.requeue_opening_blocked_product_research_calls(run_id, symbol)
        if cancel_event.is_set():
            return
        item = self._db.product_research_item(run_id, symbol)
        if not item:
            return
        if not self._all_complete(self._latest_stage(item, "rebuttal")):
            self._run_stage(run_id, item, "rebuttal", cancel_event)
        self._settle_item(run_id, symbol)

    def _run_stage(self, run_id: int, item: Mapping[str, Any], stage: str, cancel_event: threading.Event) -> None:
        participants = self._participant_rows()
        if not participants or cancel_event.is_set():
            return
        # A failed response with a definite receipt is eligible for a bounded
        # retry.  Each loop consumes exactly one attempt per such model slot;
        # all other completed slots remain untouched.  Delivery-unknown work is
        # deliberately absent from this path, because it may already be billed.
        while not cancel_event.is_set():
            refreshed = self._db.product_research_item(run_id, str(item["symbol"]))
            run = self._db.product_research_run_by_id(run_id)
            if not refreshed or not run:
                return
            run_competition_id = int(run["competition_id"])
            max_attempts = max(1, int(run["max_attempts"]))
            slots = self._latest_stage(refreshed, stage)
            planned: List[dict] = []
            for participant in participants:
                trader_id = participant["id"]
                call = slots.get(trader_id)
                if call is None:
                    continue
                if call["status"] == "failed" and int(call["attempt"]) < max_attempts:
                    attempt = int(call["attempt"]) + 1
                    dispatch_key = _stable_key(
                        SCHEMA_VERSION, run["run_key"], refreshed["symbol"], stage, trader_id, attempt,
                    )
                    if not self._db.add_product_research_call_attempt(
                        run_id, int(refreshed["id"]), str(refreshed["symbol"]), int(refreshed["debate_id"]),
                        stage, trader_id, attempt, dispatch_key,
                    ):
                        continue
                    call = {
                        "stage": stage, "trader_id": trader_id, "attempt": attempt,
                        "dispatch_key": dispatch_key, "status": "pending", "debate_id": refreshed["debate_id"],
                    }
                if call["status"] == "pending":
                    planned.append(call)
            if not planned:
                return
            facts = self._facts_for_item(refreshed, run_competition_id)
            fact_ids = {str(fact["id"]) for fact in facts}
            participant_ids = {str(participant["id"]) for participant in participants}
            openings = self._opening_rows(run_competition_id, int(refreshed["debate_id"])) if stage == "rebuttal" else []
            prompts = {
                str(call["dispatch_key"]): (
                    _opening_prompt(str(refreshed["symbol"]), str(refreshed["name"]), str(refreshed["exchange"]), facts,
                                    attempt=int(call["attempt"]))
                    if stage == "opening" else
                    _rebuttal_prompt(str(refreshed["symbol"]), str(refreshed["name"]), str(refreshed["exchange"]), facts,
                                     openings, str(call["trader_id"]), attempt=int(call["attempt"]))
                )
                for call in planned
            }
            active: Dict[Future, dict] = {}
            deadline = time.monotonic() + self._cfg.stage_timeout_sec
            executor = ThreadPoolExecutor(max_workers=min(self._cfg.model_concurrency, len(planned)),
                                          thread_name_prefix=f"product-{stage}")
            try:
                for call in planned:
                    if cancel_event.is_set():
                        break
                    dispatch_key = str(call["dispatch_key"])
                    with self._submit_guard():
                        if cancel_event.is_set() or not self._db.submit_product_research_call(dispatch_key):
                            continue
                    active[executor.submit(
                        self._call_and_persist, call, prompts[dispatch_key], fact_ids, participant_ids,
                    )] = call
                while active and not cancel_event.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    done, _ = wait(active, timeout=min(0.25, remaining), return_when=FIRST_COMPLETED)
                    for future in done:
                        active.pop(future)
                        try:
                            future.result()
                        except Exception:  # noqa: BLE001 - worker records a safe terminal state itself
                            log.exception("product research worker failed")
                for future, call in list(active.items()):
                    future.cancel()
                    self._db.mark_product_research_call_unknown(str(call["dispatch_key"]), "stage_deadline")
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            self._on_change()
            if cancel_event.is_set():
                return
            latest_item = self._db.product_research_item(run_id, str(item["symbol"]))
            latest_stage = self._latest_stage(latest_item or {}, stage)
            if self._all_complete(latest_stage):
                return
            retryable = any(
                call.get("status") == "failed" and int(call.get("attempt") or 0) < max_attempts
                for call in latest_stage.values()
            )
            if not retryable:
                return
            # Event.wait makes the backoff interruptible during shutdown.
            if self._cfg.retry_backoff_sec and cancel_event.wait(self._cfg.retry_backoff_sec):
                return

    def _call_and_persist(self, call: Mapping[str, Any], prompt: str, fact_ids: set[str],
                          participant_ids: set[str]) -> None:
        dispatch_key = str(call["dispatch_key"])
        trader_id = str(call["trader_id"])
        stage = str(call["stage"])
        started = time.time()
        try:
            result = self._call_model(trader_id, prompt, stage, fact_ids, participant_ids)
        except Exception:  # noqa: BLE001 - preserve no provider details
            result = {"status": "failed", "error": "ResearchRequestFailed", "prompt": prompt,
                      "latency_ms": int((time.time() - started) * 1000)}
        raw_status = str(result.get("status") or "failed")
        error = _safe_text(result.get("error"), 200)
        if raw_status != "complete" and error in UNCERTAIN_DELIVERY_ERRORS:
            self._db.mark_product_research_call_unknown(dispatch_key, error or "delivery_unknown")
            return
        status = "complete" if raw_status == "complete" else "failed"
        payload = result.get("payload") if status == "complete" and isinstance(result.get("payload"), dict) else {}
        self._db.persist_product_research_result(
            dispatch_key, status=status, payload=payload, raw_response=_safe_text(result.get("raw_response"), 12000),
            prompt=_safe_text(result.get("prompt") or prompt, 16000), latency_ms=result.get("latency_ms"),
            tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"), error=error,
        )

    def _settle_item(self, run_id: int, symbol: str) -> None:
        item = self._db.product_research_item(run_id, symbol)
        if not item:
            return
        calls = [call for stage in item.get("models", {}).values() for entries in stage.values() for call in entries]
        latest = self._latest_calls(item)
        current = list(latest.values())
        complete = len(current) == len(self._participant_rows()) * 2 and all(call["status"] == "complete" for call in current)
        completed_calls = sum(1 for call in current if call["status"] == "complete")
        unknown = any(call["status"] == "delivery_unknown" for call in current)
        if complete:
            status = "complete"
            session_status = "ready_for_codex_review"
        elif completed_calls:
            status = "partial"
            session_status = "incomplete"
        elif unknown:
            status = "failed"
            session_status = "incomplete"
        else:
            status = "failed"
            session_status = "failed"
        self._db.complete_debate_session(int(item["debate_id"]), session_status,
                                         preliminary={"product_symbol": symbol, "complete_model_calls": completed_calls},
                                         error="" if complete else "product_research_incomplete")
        self._db.set_product_research_item(run_id, symbol, status,
                                           attempt_count=max((int(call["attempt"]) for call in calls), default=0),
                                           completed=True)
        self._on_change()

    @staticmethod
    def _latest_calls(item: Mapping[str, Any]) -> Dict[Tuple[str, str], dict]:
        latest: Dict[Tuple[str, str], dict] = {}
        for stage, traders in (item.get("models") or {}).items():
            for trader_id, attempts in (traders or {}).items():
                if attempts:
                    latest[(str(stage), str(trader_id))] = max(attempts, key=lambda value: int(value.get("attempt") or 0))
        return latest

    @classmethod
    def _latest_stage(cls, item: Mapping[str, Any], stage: str) -> Dict[str, dict]:
        return {trader: call for (call_stage, trader), call in cls._latest_calls(item).items() if call_stage == stage}

    def _all_complete(self, stage: Mapping[str, Mapping[str, Any]]) -> bool:
        ids = {str(participant["id"]) for participant in self._participant_rows()}
        return bool(ids) and set(stage) == ids and all(call.get("status") == "complete" for call in stage.values())

    @classmethod
    def _has_unknown(cls, item: Mapping[str, Any]) -> bool:
        return any(call.get("status") == "delivery_unknown" for call in cls._latest_calls(item).values())

    def _facts_for_item(self, item: Mapping[str, Any], competition_id: int) -> List[dict]:
        """Read the product's frozen facts, never reconstructed model output."""
        detail = self._db.debate_session(int(competition_id), int(item["debate_id"]), include_raw=False) or {}
        snapshot = detail.get("snapshot") if isinstance(detail, Mapping) else None
        facts = snapshot.get("facts") if isinstance(snapshot, Mapping) else None
        if not isinstance(facts, list) or not facts:
            raise RuntimeError("product research facts unavailable")
        return [dict(fact) for fact in facts if isinstance(fact, Mapping) and fact.get("id")]

    def _opening_rows(self, competition_id: int, debate_id: int) -> List[dict]:
        detail = self._db.debate_session(int(competition_id), debate_id, include_raw=True) or {}
        return [dict(turn) for turn in detail.get("turns") or [] if turn.get("stage") == "opening" and turn.get("status") == "complete"]

    def _participant_rows(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        seen: set[str] = set()
        for raw in self._participants():
            trader_id = _safe_text(raw.get("id"), 80)
            if not trader_id or trader_id in seen:
                continue
            seen.add(trader_id)
            rows.append({"id": trader_id, "name": _safe_text(raw.get("name") or trader_id, 120),
                         "color": _safe_text(raw.get("color") or "#888", 32)})
        return rows
