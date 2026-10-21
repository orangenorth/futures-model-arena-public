"""FastAPI 只读查询 + 控制接口，并托管 web/ 静态看板。"""
from __future__ import annotations

import csv
import io
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..admission import DEFAULT_LEDGER_FILE, load_ledger_projection
from ..admission_source_policy import DEFAULT_SOURCE_POLICY_FILE, load_policy_or_none
from ..engine.loop import Engine, ResetBlocked
from ..research_scan import ResearchScanService
from ..universe import (DEFAULT_UNIVERSE_FILE, DISCLAIMER, CatalogueCache, load_catalogue,
                        load_configured_symbols, verification_status)


class ControlReq(BaseModel):
    action: str  # pause | resume | reset | force_round


CROSS_SITE_REFUSED = "跨站请求被拒绝：请在本机看板页面上操作"
FOREIGN_HOST_REFUSED = "请求的 Host 不是本机看板地址：请用 http://127.0.0.1:<端口>/ 打开看板"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


def host_name(raw: str) -> str:
    """从 Host 头或配置里取出主机名：去端口、IPv6 去方括号、转小写。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return (urlsplit(f"//{raw}").hostname or "").lower()
    except ValueError:
        return ""


def allowed_hosts(server_host: str, extra: Sequence[str] = ()) -> frozenset[str]:
    """允许出现在写请求 Host 头里的主机名。

    回环名字始终允许（看板就开在 127.0.0.1 上，用户也可能手敲 localhost）；
    绑定到具体网卡时把该地址一并放行；绑定到 0.0.0.0 / :: 时无法从配置推出合法主机名，
    需要用 `server.allowed_hosts` 显式列出——否则任何域名都能满足下面的同源判定。
    """
    hosts = set(LOOPBACK_HOSTS)
    bound = host_name(server_host)
    if bound and bound not in WILDCARD_HOSTS:
        hosts.add(bound)
    hosts.update(h for h in (host_name(e) for e in extra) if h)
    return frozenset(hosts)


def is_foreign_host(request: Request, allowed: frozenset[str]) -> bool:
    """Host 头指向的是不是本机看板地址。

    只看 `Sec-Fetch-Site` / `Origin` 挡不住 DNS 重绑定：攻击者把自己域名的 A 记录改到 127.0.0.1，
    受害者浏览器再访问 `http://evil.example:<端口>/` 时请求就落到本机看板上，
    而此时 `Origin` 与 `Host` 都是 evil.example、`Sec-Fetch-Site` 是 same-origin，前面那套判定全部通过。
    合法的看板地址只会是回环名字（或配置里绑定/列出的主机名），所以按 Host 卡住即可。

    没有 Host 头的客户端（HTTP/1.0 脚本）放行：浏览器必发 Host，构不成上面这条攻击路径。
    """
    name = host_name(request.headers.get("host"))
    return bool(name) and name not in allowed


def is_cross_site(request: Request) -> bool:
    """该请求是否是浏览器发起的跨站请求。

    看板与 API 由同一进程托管，所以看板发出的写请求永远是同源的；浏览器对写请求必带
    `Sec-Fetch-Site`（现代浏览器）或 `Origin`，本机其他端口上的恶意页面即使猜到端口也过不来。
    没有这两个头的客户端（pywebview 壳、curl、脚本）不是浏览器发起的跨站请求，照常放行。

    只靠"对端是回环地址"不够：本机浏览器打开的任意网页，其请求的对端同样是回环地址。
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site:
        # same-origin：看板页面自己；none：用户直接输入地址/书签打开。
        # 其余（cross-site，以及同主机不同端口的 same-site）都不是看板发出的。
        return site not in ("same-origin", "none")
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return False
    host = (request.headers.get("host") or "").strip().lower()
    # Origin: null（sandbox iframe、file://）的 netloc 为空，与 Host 不相等，同样算跨站
    return not host or urlsplit(origin).netloc.lower() != host


REMOTE_CONTROL_REFUSED = "remote control is disabled; use the local dashboard or enable allow_remote_control"


def is_remote_peer(request: Request, server_host: str, allow_remote_control: bool) -> bool:
    """非本机对端在未开放远程控制时是否应被拒绝。

    绑定在回环上时不可能收到非本机连接，直接放行；绑定到具体网卡或通配地址时，
    只有显式打开 `allow_remote_control` 才接受非回环对端发来的写请求。
    """
    if allow_remote_control:
        return False
    if (server_host or "").strip().lower() in LOOPBACK_HOSTS:
        return False
    peer = (request.client.host if request.client else "").lower()
    return peer not in LOOPBACK_HOSTS


def create_app(engine: Engine, web_dir: Path, control_token: Optional[str] = None) -> FastAPI:
    """control_token：/api/control 的可选鉴权。

    未配置时保持现状（默认只绑 127.0.0.1，风险可控）；配置后（参数传入或
    ARENA_CONTROL_TOKEN 环境变量）调用方须在 X-Control-Token 请求头中携带
    正确 token，否则 403。只读查询接口不受影响。
    """
    if control_token is None:
        control_token = os.environ.get("ARENA_CONTROL_TOKEN")
    app = FastAPI(title="四模型期货竞赛", docs_url=None, redoc_url=None)
    db = engine.db
    universe_cache = CatalogueCache()
    research_scan = ResearchScanService(
        db,
        engine.cfg.resolve(DEFAULT_UNIVERSE_FILE),
        timeout_sec=float(engine.cfg.market_data.cn_futures.timeout_sec),
        calendar_path=engine.cfg.resolve(engine.cfg.market_data.cn_futures.contracts_file),
    )
    # Tests and the desktop shell can inspect this only to replace the narrow
    # public-market reader; the endpoint still owns the single-flight guard.
    app.state.research_scan = research_scan
    app.state.product_research = engine.product_research
    scfg = engine.cfg.server
    hosts = allowed_hosts(scfg.host, scfg.allowed_hosts)

    @app.on_event("shutdown")
    def _stop_research_scan() -> None:
        # Stop before the caller closes the shared database.  The service does
        # not close its reader here because a timed-out public request may still
        # be unwinding on a worker thread.
        research_scan.stop()

    @app.middleware("http")
    async def _refuse_cross_site_writes(request: Request, call_next):
        """在中间件里按请求方法统一拦截，而不是让每个写接口各自记得调用检查。

        单个路由里的一次函数调用只保护它自己：以后新增的写接口（例如盘前研究的触发接口）
        会默认失去保护，而这类接口往往正是会花钱或改竞赛状态的那种。
        中间件在路由之外运行，异常处理器不会接管，所以这里直接构造响应，
        保持与 HTTPException 相同的 {"detail": ...} 形状。

        两道判定缺一不可：来源头挡住普通跨站页面，Host 挡住 DNS 重绑定——后者能让攻击者页面
        拿到一个"同源"的请求上下文，此时来源头全部合法。
        """
        if request.method.upper() in WRITE_METHODS:
            # 三道判定各挡一类攻击，缺一不可：对端地址挡住未开放远程控制时的局域网调用，
            # 来源头挡住本机其他页面的跨站写请求，Host 挡住 DNS 重绑定——后者能让攻击者页面
            # 拿到一个"同源"的请求上下文，此时来源头全部合法。
            if is_remote_peer(request, scfg.host, scfg.allow_remote_control):
                return JSONResponse(status_code=403, content={"detail": REMOTE_CONTROL_REFUSED})
            if is_foreign_host(request, hosts):
                return JSONResponse(status_code=403, content={"detail": FOREIGN_HOST_REFUSED})
            if is_cross_site(request):
                return JSONResponse(status_code=403, content={"detail": CROSS_SITE_REFUSED})
        return await call_next(request)

    @app.get("/api/state")
    def state():
        return engine.state()

    @app.get("/api/equity")
    def equity(since: float = 0, max_points: int = 1500, range_sec: float = 0):
        # range_sec 相对最新快照而不是浏览器时间：回放竞赛的时间轴与墙钟无关
        if range_sec > 0:
            latest = db.equity_latest_ts(engine.comp_id)
            if latest is not None:
                since = max(since, latest - range_sec)
        return {"series": db.equity_series(engine.comp_id, since=since, max_points=max_points),
                "initial_cash": engine.initial_cash}

    @app.get("/api/trades")
    def trades(trader: Optional[str] = None, limit: int = Query(100, le=1000), offset: int = 0):
        return {"items": db.trades(engine.comp_id, trader, limit=limit, offset=offset),
                "total": db.trade_count(engine.comp_id, trader),
                "names": {s: engine.specs[s].name for s in engine.symbols}}

    @app.get("/api/decisions")
    def decisions(trader: Optional[str] = None, limit: int = Query(30, le=500), offset: int = 0):
        return {"items": db.public_decisions(engine.comp_id, trader, limit=limit, offset=offset)}

    @app.get("/api/decisions/{decision_id}")
    def decision(decision_id: int):
        d = db.public_decision_for_competition(engine.comp_id, decision_id)
        if not d:
            raise HTTPException(404, "not found")
        return d

    @app.get("/api/debates")
    def debates(limit: int = Query(12, ge=1, le=50), offset: int = Query(0, ge=0)):
        """List current-competition research sessions without raw prompts/responses."""
        return JSONResponse(
            content={"items": db.debate_sessions(engine.comp_id, limit=limit, offset=offset)},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/debates/{debate_id}")
    def debate(debate_id: int):
        # The current competition scope prevents a guessed old ID from exposing
        # another competition's model transcript.
        item = db.debate_session(engine.comp_id, debate_id, include_raw=False)
        if not item:
            raise HTTPException(404, "not found")
        return JSONResponse(content=item, headers={"Cache-Control": "no-store"})

    @app.post("/api/debates/run")
    def run_debate(request: Request):
        """Explicit UI action only; it launches research, never an order or decision round."""
        started, message = engine.start_manual_debate()
        if not started:
            raise HTTPException(409, message)
        return {"ok": True, "message": message, "analysis_only": True}

    @app.post("/api/debates/{debate_id}/retry")
    def retry_debate(debate_id: int, request: Request):
        """Retry only the latest recoverable research session with a fresh snapshot."""
        # Scope the id before asking the engine to create billable work.  This
        # prevents an old competition id from becoming an existence oracle and
        # gives the UI a normal 404 for a stale history page.
        if not db.debate_session(engine.comp_id, debate_id, include_raw=False):
            raise HTTPException(404, "not found")
        started, message = engine.retry_manual_debate(debate_id)
        if not started:
            raise HTTPException(409, message)
        return {"ok": True, "message": message, "analysis_only": True, "retry_of": debate_id}

    @app.get("/api/product-research/runs")
    def product_research_runs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return JSONResponse(
            content={"items": db.product_research_runs(engine.comp_id, limit=limit, offset=offset)},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/product-research")
    def product_research_status(run_id: Optional[int] = None, limit: int = Query(50, ge=1, le=200),
                                offset: int = Query(0, ge=0), status: Optional[str] = None):
        run = (db.product_research_run_by_id(run_id) if run_id is not None
               else db.latest_product_research_run(engine.comp_id))
        if run_id is not None and not run:
            raise HTTPException(404, "not found")
        if not run:
            return JSONResponse(content={"run": None, "items": [], "total": 0, "participants": engine._product_research_participants()},
                                headers={"Cache-Control": "no-store"})
        historical_recovery = int(run["competition_id"]) != int(engine.comp_id)
        resume_allowed, resume_reason = (engine._product_research_resume_allowed(int(run["id"]))
                                         if historical_recovery else (None, ""))
        return JSONResponse(
            content={
                "run": run,
                "items": db.product_research_items(int(run["id"]), limit=limit, offset=offset, status=status),
                "total": int(run["discovered_count"]),
                "participants": engine._product_research_participants(),
                "historical_recovery": historical_recovery,
                "resume_allowed": resume_allowed,
                "resume_reason": resume_reason,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/product-research/resumable")
    def resumable_product_research(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                                   status: Optional[str] = None):
        """Offer a clearly labelled historical analysis queue without changing the trading view."""
        run = db.latest_resumable_product_research_run()
        if not run:
            return JSONResponse(content={"run": None, "items": [], "total": 0, "participants": engine._product_research_participants()},
                                headers={"Cache-Control": "no-store"})
        historical_recovery = int(run["competition_id"]) != int(engine.comp_id)
        resume_allowed, resume_reason = (engine._product_research_resume_allowed(int(run["id"]))
                                         if historical_recovery else (None, ""))
        return JSONResponse(
            content={
                "run": run,
                "items": db.product_research_items(int(run["id"]), limit=limit, offset=offset, status=status),
                "total": int(run["discovered_count"]),
                "participants": engine._product_research_participants(),
                "historical_recovery": historical_recovery,
                "resume_allowed": resume_allowed,
                "resume_reason": resume_reason,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/product-research/{run_id}/debates/{debate_id}")
    def product_research_debate(run_id: int, debate_id: int):
        """Read one historic product result only through its owning research run."""
        run = db.product_research_run_by_id(run_id)
        if not run or not db.product_research_item_for_debate(run_id, debate_id):
            raise HTTPException(404, "not found")
        detail = db.debate_session(int(run["competition_id"]), debate_id, include_raw=False)
        if not detail or detail.get("phase") != "product_research":
            raise HTTPException(404, "not found")
        return JSONResponse(content=detail, headers={"Cache-Control": "no-store"})

    @app.post("/api/product-research/run")
    def run_product_research(sample_limit: Optional[int] = Query(None, ge=1, le=200)):
        """Create a persistent queue; a sample run leaves remaining products pending."""
        started, message, run_id = engine.start_product_research(sample_limit=sample_limit)
        if not started:
            raise HTTPException(409, message)
        return {"ok": True, "message": message, "run_id": run_id, "sample_limit": sample_limit,
                "analysis_only": True}

    @app.post("/api/product-research/{run_id}/resume")
    def resume_product_research(run_id: int):
        started, message = engine.resume_product_research(run_id)
        if not started:
            raise HTTPException(409, message)
        return {"ok": True, "message": message, "run_id": run_id, "analysis_only": True}

    @app.post("/api/product-research/{run_id}/retry-failed")
    def retry_failed_product_research(run_id: int):
        started, message = engine.retry_failed_product_research(run_id)
        if not started:
            raise HTTPException(409, message)
        return {"ok": True, "message": message, "run_id": run_id, "analysis_only": True}

    @app.get("/api/daily")
    def daily(days: int = 60):
        return {"items": db.daily(engine.comp_id, days)}

    @app.get("/api/events")
    def events(limit: int = 100):
        return {"items": db.public_events(engine.comp_id, limit)}

    @app.get("/api/competitions")
    def competitions():
        return {"items": db.public_competitions()}

    @app.get("/api/universe")
    def universe():
        """Read-only candidate catalogue; never a trading pool or an order authorisation.

        The stored file proves identity and upstream availability only.  The two
        booleans added here are derived locally — ``configured_spec`` says the
        repository already carries an approximate specification, and
        ``active_in_competition`` says the running engine trades it.  Neither
        means the exchange verified anything, and ``verification_status`` says so
        outright: every candidate is ``legacy_unverified`` or ``unknown``, never
        verified and never tradable.  Discovery output is static, so this
        endpoint stays off the dashboard's high-frequency state poll.
        """
        catalogue = universe_cache.get(engine.cfg.resolve(DEFAULT_UNIVERSE_FILE))
        configured = load_configured_symbols(engine.cfg.resolve(engine.cfg.market_data.cn_futures.contracts_file))
        active = set(engine.symbols)
        items = [dict(c, configured_spec=c["symbol"] in configured,
                      active_in_competition=c["symbol"] in active,
                      verification_status=verification_status(c["symbol"], configured))
                 for c in catalogue["candidates"]]
        return JSONResponse(
            content={"available": catalogue["available"], "source": catalogue["source"],
                     "source_url": catalogue["source_url"], "fetched_at": catalogue["fetched_at"],
                     "snapshot_status": catalogue["snapshot_status"],
                     "content_hash": catalogue["content_hash"], "count": len(items),
                     "note": DISCLAIMER, "candidates": items},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/universe/admission")
    def universe_admission():
        """Read-only admission ledger coverage; never a term, never an authorisation.

        The ledger is planning material: which candidates have had their
        multiplier / tick / sessions / margin / fee evidence gathered, and how
        far each piece has been reviewed.  The body carries six keys and nothing
        else -- availability, the disclaimer, the term vocabulary, a count, the
        per-state counts and one row per candidate -- because a number or an
        official-looking hostname rendered next to a code reads as a verified term
        at a glance and nothing here has been verified.  Proposed values, source
        URLs, document kinds, the policy's host list, the block reason and the
        preparation date all stay in the files.

        Two anchors are re-proved on every read, under one fingerprint that covers
        them and the evidence together: the ledger's catalogue binding (declared
        path, schema, content hash, candidate count, symbol set) and its
        source-policy binding (declared path, schema, fingerprint).  Anything
        stale, tampered, or bound to another snapshot, another file or another
        policy is reported unavailable rather than shown beside a catalogue it may
        not describe -- and a policy that cannot be loaded is a refusal too, since
        no reading of "the host list is missing" makes the citations in the file
        more trustworthy.  None of those anchors is in the response: they are what
        decides whether the file may be believed, and serving them would publish
        the exact values a forged ledger has to reproduce.  The refusal body has
        the same six keys as a success, so the shape never says which check failed.
        Kept off the dashboard's high-frequency state poll for the same reason as
        the catalogue: these are static local artefacts.

        Both documents are read and validated from disk on every call rather than
        remembered.  "Re-proved on every read" has to mean the documents that are
        there now: a cache keyed on path, modification time and size would hand
        back a file that had since been replaced by another of the same length,
        and the binding would then be proved against content this process never
        read.  That is why ``universe_cache`` -- which identifies a catalogue by
        exactly that metadata, and is fine for the discovery endpoint, where a
        stale read costs a stale listing -- is deliberately not used here: the
        ledger's catalogue binding is a claim about the snapshot's *content*, and
        proving it means reading it.
        """
        catalogue = load_catalogue(engine.cfg.resolve(DEFAULT_UNIVERSE_FILE))
        policy = load_policy_or_none(engine.cfg.resolve(DEFAULT_SOURCE_POLICY_FILE))
        # Declared paths, not resolved ones: a ledger records the catalogue and the
        # policy it was prepared against the way this application names them, so the
        # committed relative declarations keep matching wherever the repository is
        # checked out.
        projection = load_ledger_projection(engine.cfg.resolve(DEFAULT_LEDGER_FILE), catalogue,
                                            DEFAULT_UNIVERSE_FILE, policy,
                                            DEFAULT_SOURCE_POLICY_FILE)
        return JSONResponse(content=projection, headers={"Cache-Control": "no-store"})

    @app.get("/api/research-scan")
    def research_scan_status():
        """Latest independent research scan; reading never starts a market request."""
        return JSONResponse(content=research_scan.public_status(), headers={"Cache-Control": "no-store"})

    @app.post("/api/research-scan/run")
    def run_research_scan():
        """Explicit dashboard action; it only reads public market data and never starts a decision round."""
        started, message = research_scan.start()
        if not started:
            raise HTTPException(409, message)
        return {"ok": True, "message": message, "analysis_only": True}

    @app.post("/api/control")
    def control(req: ControlReq, request: Request):
        if control_token:
            token = request.headers.get("X-Control-Token", "")
            if not secrets.compare_digest(token, control_token):
                raise HTTPException(403, "control token 无效或缺失")
        if req.action == "pause":
            engine.pause(True)
        elif req.action == "resume":
            engine.pause(False)
        elif req.action == "reset":
            try:
                engine.reset()
            except ResetBlocked as e:
                raise HTTPException(409, str(e))
        elif req.action == "force_round":
            engine.force_round()
        else:
            raise HTTPException(400, "unknown action")
        return {"ok": True, "paused": engine.paused}

    def fmt_ts(ts: float) -> str:
        # 导出时间按竞赛时区格式化，而不是主机本地时区：无头服务器常为 UTC，会把成交挪到错误的交易日
        return datetime.fromtimestamp(ts, engine.tz).strftime("%Y-%m-%d %H:%M:%S")

    @app.get("/api/export/trades.csv")
    def export_trades():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "trader", "symbol", "side", "action", "qty", "price", "fee", "realized_pnl", "time", "round", "reason"])
        for t in db.trades(engine.comp_id, limit=100000):
            w.writerow([t["id"], t["trader_id"], t["symbol"], t["side"], t["action"], t["qty"], t["price"], t["fee"], t["realized_pnl"],
                        fmt_ts(t["ts"]), t["round_no"], t.get("reason", "")])
        buf.seek(0)
        return StreamingResponse(iter([("﻿" + buf.getvalue()).encode("utf-8")]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=trades.csv"})

    @app.get("/api/export/equity.csv")
    def export_equity():
        series = db.equity_series(engine.comp_id, max_points=1_000_000)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["trader", "time", "equity"])
        for tid, pts in series.items():
            for ts, eq in pts:
                w.writerow([tid, fmt_ts(ts), eq])
        buf.seek(0)
        return StreamingResponse(iter([("﻿" + buf.getvalue()).encode("utf-8")]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=equity.csv"})

    @app.get("/api/health")
    def health():
        return {"ok": True, "running": engine.running, "ts": time.time()}

    @app.get("/")
    def index():
        return FileResponse(web_dir / "index.html")

    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.exception_handler(Exception)
    async def _err(request, exc):  # noqa: ANN001
        # Never reflect provider or filesystem exception text to the browser.
        # Such diagnostics can contain endpoint details that do not belong in a
        # public dashboard response.
        return JSONResponse(status_code=500, content={"error": "internal server error"})

    return app
