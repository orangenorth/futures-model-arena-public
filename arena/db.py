"""SQLite 持久化（WAL 模式）。引擎线程写、API 线程读，连接按线程独立。"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .models import Account, Decision, EquityPoint, Position, Trade
from .research_scan import (
    PUBLIC_SCAN_OUTCOMES,
    PUBLIC_SCAN_REASONS,
    PUBLIC_SCAN_STAGE_REASONS,
    PUBLIC_SCAN_STAGES,
    PUBLIC_SCAN_TRENDS,
    canonical_manifest_hash,
)
from .public_errors import (
    decision_failure_category,
    public_decision_failure_message,
    public_execution_result_reason,
    public_runtime_event_message,
)

log = logging.getLogger("arena.db")

LEGACY_COMPETITION_TIMEZONE = "Asia/Shanghai"

# The database retains the internal error code for local diagnostics, but the
# browser may only receive one of these fixed, non-sensitive categories.
PUBLIC_DEBATE_FAILURE_CATEGORY_BY_ERROR = {
    "ResearchRequestTimedOut": "timeout",
    "ResearchConnectionFailed": "connection",
    "ResearchAuthorizationFailed": "authorization",
    "ResearchQuotaExhausted": "quota_or_credits",
    "ResearchRateLimited": "rate_limited",
    "ResearchUpstream4xx": "upstream_4xx",
    "ResearchUpstream5xx": "upstream_5xx",
    "ResearchResponseMalformedJson": "malformed_json",
    "ResearchResponseInvalidFactCitation": "invalid_fact_citation",
    "ResearchResponseInvalidTarget": "invalid_target",
    "ResearchResponseMissingRequiredField": "missing_required_field",
    "ResearchResponseInvalidSchema": "invalid_schema",
    "ResearchResponseInvalid": "invalid_response",
    "ResearchStageTimedOut": "stage_deadline",
    "ResearchWorkerFailed": "worker_failed",
    "ResearchParticipantUnavailable": "participant_unavailable",
    "ResearchRequestFailed": "request_failed",
}

PRODUCT_RESEARCH_ITEM_STATUSES = frozenset({"excluded", "pending", "running", "complete", "partial", "failed"})
PRODUCT_RESEARCH_CALL_STATUSES = frozenset({"pending", "submitted", "complete", "failed", "delivery_unknown", "skipped"})
PRODUCT_RESEARCH_RUN_STATUSES = frozenset({"discovering", "queued", "running", "complete", "complete_with_exceptions", "interrupted", "failed"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  market TEXT NOT NULL,
  initial_cash REAL NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL,
  round_no INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running',
  config_json TEXT
);
CREATE TABLE IF NOT EXISTS traders (
  competition_id INTEGER NOT NULL,
  trader_id TEXT NOT NULL,
  name TEXT NOT NULL,
  color TEXT,
  provider TEXT,
  model TEXT,
  PRIMARY KEY (competition_id, trader_id)
);
CREATE TABLE IF NOT EXISTS accounts (
  competition_id INTEGER NOT NULL,
  trader_id TEXT NOT NULL,
  cash REAL NOT NULL,
  initial_cash REAL NOT NULL,
  realized_pnl REAL NOT NULL DEFAULT 0,
  fees REAL NOT NULL DEFAULT 0,
  peak_equity REAL NOT NULL DEFAULT 0,
  liquidations INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (competition_id, trader_id)
);
CREATE TABLE IF NOT EXISTS positions (
  competition_id INTEGER NOT NULL,
  trader_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty REAL NOT NULL,
  avg_price REAL NOT NULL,
  margin REAL NOT NULL,
  leverage REAL NOT NULL DEFAULT 1,
  stop_loss REAL,
  take_profit REAL,
  opened_at REAL NOT NULL,
  last_price REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (competition_id, trader_id, symbol)
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competition_id INTEGER NOT NULL,
  trader_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  action TEXT NOT NULL,
  qty REAL NOT NULL,
  price REAL NOT NULL,
  fee REAL NOT NULL,
  realized_pnl REAL NOT NULL,
  ts REAL NOT NULL,
  round_no INTEGER NOT NULL,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ct ON trades(competition_id, trader_id, ts);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competition_id INTEGER NOT NULL,
  trader_id TEXT NOT NULL,
  round_no INTEGER NOT NULL,
  ts REAL NOT NULL,
  mode TEXT,
  analysis TEXT,
  actions_json TEXT,
  results_json TEXT,
  raw_response TEXT,
  prompt TEXT,
  latency_ms INTEGER,
  tokens_in INTEGER,
  tokens_out INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ct ON decisions(competition_id, trader_id, round_no);
CREATE TABLE IF NOT EXISTS equity_snapshots (
  competition_id INTEGER NOT NULL,
  trader_id TEXT NOT NULL,
  ts REAL NOT NULL,
  equity REAL NOT NULL,
  cash REAL NOT NULL,
  margin_used REAL NOT NULL,
  unrealized REAL NOT NULL,
  PRIMARY KEY (competition_id, trader_id, ts)
);
CREATE TABLE IF NOT EXISTS daily_summaries (
  competition_id INTEGER NOT NULL,
  trader_id TEXT NOT NULL,
  day TEXT NOT NULL,
  start_equity REAL NOT NULL,
  end_equity REAL NOT NULL,
  pnl REAL NOT NULL,
  trades INTEGER NOT NULL,
  fees REAL NOT NULL,
  PRIMARY KEY (competition_id, trader_id, day)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competition_id INTEGER,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  source TEXT,
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS debate_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competition_id INTEGER NOT NULL,
  session_key TEXT NOT NULL,
  phase TEXT NOT NULL,
  phase_label TEXT NOT NULL,
  scheduled_open_at REAL,
  market_as_of REAL,
  analysis_id TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  global_analysis_json TEXT NOT NULL,
  preliminary_json TEXT,
  referee_status TEXT NOT NULL DEFAULT 'pending_codex_review',
  referee_packet TEXT,
  status TEXT NOT NULL,
  error TEXT,
  started_at REAL NOT NULL,
  completed_at REAL,
  UNIQUE(competition_id, session_key)
);
CREATE INDEX IF NOT EXISTS idx_debate_sessions_comp ON debate_sessions(competition_id, started_at DESC);
CREATE TABLE IF NOT EXISTS debate_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  debate_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  trader_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  status TEXT NOT NULL,
  stance TEXT,
  confidence REAL,
  payload_json TEXT,
  raw_response TEXT,
  prompt TEXT,
  latency_ms INTEGER,
  tokens_in INTEGER,
  tokens_out INTEGER,
  error TEXT,
  created_at REAL NOT NULL,
  UNIQUE(debate_id, stage, trader_id)
);
CREATE INDEX IF NOT EXISTS idx_debate_turns_session ON debate_turns(debate_id, stage, ordinal);
CREATE TABLE IF NOT EXISTS research_scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  catalogue_schema_version INTEGER NOT NULL,
  catalogue_content_hash TEXT NOT NULL,
  catalogue_snapshot_status TEXT NOT NULL,
  catalogue_fetched_at TEXT NOT NULL,
  discovered_count INTEGER NOT NULL,
  eligible_count INTEGER NOT NULL,
  scanned_count INTEGER NOT NULL,
  excluded_count INTEGER NOT NULL,
  coverage_status TEXT NOT NULL,
  coverage_ratio REAL,
  denominator_known INTEGER NOT NULL,
  manifest_hash TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  started_at REAL NOT NULL,
  completed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_scan_runs_started ON research_scan_runs(started_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS research_scan_items (
  run_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  exchange TEXT NOT NULL,
  outcome TEXT NOT NULL,
  stage TEXT NOT NULL,
  reason TEXT NOT NULL,
  latest_price REAL,
  change_pct REAL,
  trend TEXT,
  score REAL,
  PRIMARY KEY (run_id, symbol),
  FOREIGN KEY (run_id) REFERENCES research_scan_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_research_scan_items_run ON research_scan_items(run_id, outcome, symbol);
CREATE TRIGGER IF NOT EXISTS research_scan_runs_no_update
BEFORE UPDATE ON research_scan_runs
BEGIN
  SELECT RAISE(ABORT, 'research scan runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS research_scan_runs_no_delete
BEFORE DELETE ON research_scan_runs
BEGIN
  SELECT RAISE(ABORT, 'research scan runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS research_scan_items_no_update
BEFORE UPDATE ON research_scan_items
BEGIN
  SELECT RAISE(ABORT, 'research scan items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS research_scan_items_no_delete
BEFORE DELETE ON research_scan_items
BEGIN
  SELECT RAISE(ABORT, 'research scan items are immutable');
END;
CREATE TABLE IF NOT EXISTS product_research_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competition_id INTEGER NOT NULL,
  run_key TEXT NOT NULL,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL,
  mapping_rule TEXT NOT NULL,
  catalogue_path TEXT NOT NULL,
  catalogue_hash TEXT,
  catalogue_fetched_at TEXT,
  discovery_status TEXT NOT NULL DEFAULT 'pending',
  scan_run_id INTEGER,
  discovered_count INTEGER NOT NULL DEFAULT 0,
  eligible_count INTEGER NOT NULL DEFAULT 0,
  excluded_count INTEGER NOT NULL DEFAULT 0,
  pending_count INTEGER NOT NULL DEFAULT 0,
  running_count INTEGER NOT NULL DEFAULT 0,
  complete_count INTEGER NOT NULL DEFAULT 0,
  partial_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  candidate_batch_size INTEGER NOT NULL,
  model_concurrency INTEGER NOT NULL,
  max_attempts INTEGER NOT NULL,
  error TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  completed_at REAL,
  UNIQUE(competition_id, run_key)
);
CREATE INDEX IF NOT EXISTS idx_product_research_runs_comp ON product_research_runs(competition_id, created_at DESC);
CREATE TABLE IF NOT EXISTS product_research_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  exchange TEXT NOT NULL,
  mapping_symbol TEXT NOT NULL,
  scan_stage TEXT NOT NULL DEFAULT '',
  exclusion_reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  batch_no INTEGER,
  debate_id INTEGER,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  started_at REAL,
  updated_at REAL NOT NULL,
  completed_at REAL,
  UNIQUE(run_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_product_research_items_run ON product_research_items(run_id, status, symbol);
CREATE TABLE IF NOT EXISTS product_research_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  debate_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  trader_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  dispatch_key TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  completed_at REAL,
  UNIQUE(run_id, symbol, stage, trader_id, attempt),
  UNIQUE(dispatch_key)
);
CREATE INDEX IF NOT EXISTS idx_product_research_calls_item ON product_research_calls(item_id, stage, trader_id, attempt);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
-- CTP 由 tick 本地聚合出的 1 分钟 bar。CTP 不提供历史 K 线，不落库的话每次重启都要重新
-- 攒上 5.5~12 个交易日（看品种时段）才凑得齐 1h 的 ema60；存着就能开机即用。与竞赛无关（换竞赛仍是同一份行情），
-- 所以不带 competition_id。
-- bar_seconds 必须存：改了这个配置之后，旧的一分钟行与新的五分钟行会混在一起，
-- 而 klines("5m") 在 bar_seconds=300 时原样返回，指标就把每一根旧的一分钟当成五分钟用了。
-- bar_seconds 还必须**进主键**：每根 5m 的起点都正好是某根 1m 的起点，主键只有 (symbol, ts)
-- 时，写 5m 会把同一时刻的 1m 行覆盖并改写成 5m。读取虽然按 bar_seconds 过滤，但那条 1m 行
-- 已经不在了——换回一分钟时每五根少一根，重采样出来的 OHLCV 与指标全错，而且不报任何错。
-- 进了主键，两个周期各存一份，换来换去都还在。
-- feed 是"这根 bar 是哪套行情接入累出来的"的指纹（前置地址、钉死的合约、主力选取参数…）。
-- 不进主键的话：换了行情前置或改钉了合约之后，新竞赛的指标仍然用旧前置累出来的 bar 预热，
-- 之后两段历史还会被当成一条继续写下去——排行榜重开了，实验其实没有隔离。
-- 空串是历史行（以及不做区分的行情源）的命名空间。
CREATE TABLE IF NOT EXISTS md_bars (
  symbol TEXT NOT NULL,
  ts REAL NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL DEFAULT 0,
  hold REAL NOT NULL DEFAULT 0,
  bar_seconds INTEGER NOT NULL DEFAULT 60,
  feed TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (symbol, feed, bar_seconds, ts)
);
"""


class _Txn:
    """with db._transaction() as conn: ... — 成功 COMMIT，异常 ROLLBACK 并重新抛出。"""

    def __init__(self, db: "Database"):
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.db._write_lock.acquire()
        try:
            self.conn = self.db.conn()
            self.conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self.db._write_lock.release()
            raise
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                try:
                    self.conn.execute("COMMIT")
                except Exception:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            else:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
        finally:
            self.db._write_lock.release()
        return False


def competition_timezone(comp: Optional[dict]) -> Optional[str]:
    """竞赛创建时持久化的时区（config_json 中的 competition.timezone）；缺失或无法解析时返回 None。

    引擎用它判断时区是否变化（变化则新建竞赛），离线导出用它格式化历史成交时间。
    """
    if not comp:
        return None
    try:
        tz = json.loads(comp.get("config_json") or "{}").get("competition", {}).get("timezone")
    except (TypeError, ValueError, AttributeError):
        return None
    return str(tz) if tz else None


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate_md_bars(c)

    @staticmethod
    def _run_in_transaction(conn: sqlite3.Connection, statements: List[str]) -> None:
        """一组语句要么全做、要么全不做。

        `executescript` 跑在自动提交的连接上，每条语句各自落定，半路中断会留下改了一半的库。
        SQLite 的 DDL 本身可回滚，所以显式事务能让中断退回原状。
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                conn.execute(statement)
        except BaseException:      # 含 Ctrl-C：中断同样必须退回原状，而不是停在删表之后
            try:
                conn.execute("ROLLBACK")
            except Exception:          # noqa: BLE001  回滚再失败也不能盖住真正的原因
                log.warning("回滚失败", exc_info=True)
            raise
        conn.execute("COMMIT")

    @staticmethod
    def _recover_md_bars_migration(conn: sqlite3.Connection) -> bool:
        """接住更早的非事务版升级中断后留下的 `md_bars_migrated`。

        事务化只挡住了**以后**再出现这种状态，修不了已经出现的那一份。两种中断位置要分开处理：
        - 原表已经删掉（下次启动 `SCHEMA` 会补一张空的新结构表）：`md_bars_migrated` 才是历史，并回去；
        - 原表还在：它是完整的，拷贝只是搬到一半的半成品，丢掉（留着还会让下一次升级直接
          报 "table md_bars_migrated already exists"）。
        """
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='md_bars_migrated'").fetchone():
            return False
        cols = conn.execute("PRAGMA table_info(md_bars)").fetchall()
        # 只有当现存的 md_bars 已经能容下半成品里的列时才并回去；否则那张表马上还要再升一次，
        # 先并再升只是多搬一遍（升级本身会把它一起搬走）。
        upgraded = any(row["name"] == "bar_seconds" and row["pk"] for row in cols)
        if not cols:
            statements = ["ALTER TABLE md_bars_migrated RENAME TO md_bars"]
        elif upgraded:
            statements = [
                "INSERT OR IGNORE INTO md_bars (symbol, ts, open, high, low, close, volume, hold, bar_seconds) "
                "SELECT symbol, ts, open, high, low, close, volume, hold, bar_seconds FROM md_bars_migrated",
                "DROP TABLE md_bars_migrated",
            ]
        else:
            statements = ["DROP TABLE md_bars_migrated"]
        Database._run_in_transaction(conn, statements)
        log.info("接回了上次中断的 md_bars 升级（%s）", "并入历史" if upgraded or not cols else "丢弃半成品")
        return True

    @staticmethod
    def _migrate_md_bars(conn: sqlite3.Connection) -> bool:
        """把旧版 `md_bars` 的主键就地升级成 (symbol, feed, bar_seconds, ts)。

        `CREATE TABLE IF NOT EXISTS` 不会改已存在的表，所以几种旧结构都要在这里接上：
        没有 `bar_seconds` 这一列的最早一版（打开后写 bar 会直接 "no such column"）、
        有这一列但没进主键的那一版（两个基础周期互相覆盖，见 SCHEMA 里的说明），
        以及没有 `feed` 这一列的那一版（换了行情前置或改钉了合约之后，新竞赛会被旧接入
        累出来的 bar 预热，两段历史还会被当成一条继续写下去）。
        SQLite 不能直接改主键，只能新建—搬运—改名。已经是新结构的库直接返回，
        所以每次启动跑一遍也没有代价。

        旧行按空串命名空间搬过来，之后**再也不会被读回**：它们没有来源标记，而任何据配置反推
        "这批 bar 是哪套接入累出来的"的做法都被证伪过（升级与换前置同一次重启；上一场竞赛
        根本没跑过这个品种；`persist_bars` 关着时把判定推迟到了另一套接入之后）。行留着不删，
        是因为删的是使用者的数据；代价是升级后 CTP 要重新积累一次历史，见 docs/ctp.md。
        """
        Database._recover_md_bars_migration(conn)   # 先接住上次中断留下的半成品
        cols = conn.execute("PRAGMA table_info(md_bars)").fetchall()
        if not cols:
            return False
        has_interval = any(row["name"] == "bar_seconds" for row in cols)
        has_feed = any(row["name"] == "feed" for row in cols)
        interval_in_pk = any(row["name"] == "bar_seconds" and row["pk"] for row in cols)
        feed_in_pk = any(row["name"] == "feed" and row["pk"] for row in cols)
        if interval_in_pk and feed_in_pk:
            return False
        # 那一版只有一种基础周期（1 分钟），旧行按 60 秒标注
        interval = "bar_seconds" if has_interval else "60"
        feed = "feed" if has_feed else "''"
        # 建表—搬运—删除—改名必须是**一个事务**。`executescript` 跑在自动提交的连接上，每条
        # 语句各自落定：删完原表、改名之前中断（进程退出或任一语句报错），下次启动时 SCHEMA
        # 会建出一张空的新结构表，这里一看主键已经对了就直接返回，搬到一半的数据留在
        # md_bars_migrated 里再也没人读——CTP 的预热历史悄悄归零，而且没有任何报错。
        # SQLite 的 DDL 本身可回滚，所以显式事务能让中断的升级退回原状。
        statements = [
            """CREATE TABLE md_bars_migrated (
                 symbol TEXT NOT NULL, ts REAL NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
                 low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
                 hold REAL NOT NULL DEFAULT 0, bar_seconds INTEGER NOT NULL DEFAULT 60,
                 feed TEXT NOT NULL DEFAULT '',
                 PRIMARY KEY (symbol, feed, bar_seconds, ts)
               )""",
            f"""INSERT OR IGNORE INTO md_bars_migrated
                  (symbol, ts, open, high, low, close, volume, hold, bar_seconds, feed)
                  SELECT symbol, ts, open, high, low, close, volume, hold, {interval}, {feed} FROM md_bars""",
            "DROP TABLE md_bars",
            "ALTER TABLE md_bars_migrated RENAME TO md_bars",
        ]
        Database._run_in_transaction(conn, statements)
        log.info("md_bars 已升级：主键加入 %s",
                 "bar_seconds 与 feed" if not interval_in_pk else "feed（按行情接入分开存）")
        return True

    # ---- 连接管理 ----
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            c.close()
            self._local.conn = None

    def checkpoint(self):
        with self._write_lock:
            try:
                self.conn().execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass

    def _exec(self, sql: str, params: Iterable[Any] = ()):
        with self._write_lock:
            return self.conn().execute(sql, tuple(params))

    def _q(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        return self.conn().execute(sql, tuple(params)).fetchall()

    # ---- 竞赛 ----
    def active_competition(self) -> Optional[dict]:
        rows = self._q("SELECT * FROM competitions WHERE status='running' ORDER BY id DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def competition(self, comp_id: int) -> Optional[dict]:
        """Return one internal competition row for a tightly scoped recovery path."""
        rows = self._q("SELECT * FROM competitions WHERE id=?", (int(comp_id),))
        return dict(rows[0]) if rows else None

    def _transaction(self):
        """显式事务上下文（连接为 autocommit 模式，多语句写入需手动 BEGIN/COMMIT）。"""
        return _Txn(self)

    def create_competition(self, name: str, market: str, initial_cash: float, config_json: str,
                           kv_prefixed: Optional[Dict[str, str]] = None) -> int:
        """结束进行中的竞赛并新建一场。kv_prefixed：与创建同一事务写入的 kv（键为 f"{前缀}:{新竞赛id}"），
        例如重置时新竞赛沿用的回放位置与暂停标志——要么一起生效，要么一起回滚。"""
        with self._transaction() as c:
            c.execute("UPDATE competitions SET status='ended', ended_at=? WHERE status='running'", (time.time(),))
            cur = c.execute(
                "INSERT INTO competitions(name, market, initial_cash, started_at, round_no, status, config_json) VALUES (?,?,?,?,0,'running',?)",
                (name, market, initial_cash, time.time(), config_json),
            )
            cid = int(cur.lastrowid)
            for prefix, value in (kv_prefixed or {}).items():
                c.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (f"{prefix}:{cid}", value))
            return cid

    def list_competitions(self) -> List[dict]:
        return [dict(r) for r in self._q("SELECT * FROM competitions ORDER BY id DESC")]

    def public_competitions(self) -> List[dict]:
        """Competition rows without config_json — the stored config may hold secrets."""
        return [dict(r) for r in self._q(
            "SELECT id, name, market, initial_cash, started_at, ended_at, round_no, status "
            "FROM competitions ORDER BY id DESC")]

    def set_round(self, comp_id: int, round_no: int):
        self._exec("UPDATE competitions SET round_no=? WHERE id=?", (round_no, comp_id))

    def upsert_trader(self, comp_id: int, trader_id: str, name: str, color: str, provider: str, model: str):
        self._exec(
            "INSERT INTO traders(competition_id, trader_id, name, color, provider, model) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(competition_id, trader_id) DO UPDATE SET name=excluded.name, color=excluded.color, provider=excluded.provider, model=excluded.model",
            (comp_id, trader_id, name, color, provider, model),
        )

    def traders(self, comp_id: int) -> List[dict]:
        return [dict(r) for r in self._q("SELECT * FROM traders WHERE competition_id=? ORDER BY rowid", (comp_id,))]

    # ---- 账户 / 持仓 ----
    def save_account(self, comp_id: int, acc: Account, trades: Iterable[Trade] = (), decision: Optional[Decision] = None):
        """账户、持仓与本次产生的成交/决策在同一个事务内落库：要么全部生效，要么全部回滚。"""
        with self._transaction() as c:
            for t in trades:
                self._insert_trade(c, comp_id, t)
            if decision is not None:
                self._insert_decision(c, comp_id, decision)
            c.execute(
                "INSERT INTO accounts(competition_id, trader_id, cash, initial_cash, realized_pnl, fees, peak_equity, liquidations, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(competition_id, trader_id) DO UPDATE SET cash=excluded.cash, realized_pnl=excluded.realized_pnl, "
                "fees=excluded.fees, peak_equity=excluded.peak_equity, liquidations=excluded.liquidations, updated_at=excluded.updated_at",
                (comp_id, acc.trader_id, acc.cash, acc.initial_cash, acc.realized_pnl, acc.fees, acc.peak_equity, acc.liquidations, time.time()),
            )
            c.execute("DELETE FROM positions WHERE competition_id=? AND trader_id=?", (comp_id, acc.trader_id))
            for p in acc.positions.values():
                c.execute(
                    "INSERT INTO positions(competition_id, trader_id, symbol, side, qty, avg_price, margin, leverage, stop_loss, take_profit, opened_at, last_price) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (comp_id, p.trader_id, p.symbol, p.side, p.qty, p.avg_price, p.margin, p.leverage, p.stop_loss, p.take_profit, p.opened_at, p.last_price),
                )

    def load_accounts(self, comp_id: int) -> Dict[str, Account]:
        out: Dict[str, Account] = {}
        for r in self._q("SELECT * FROM accounts WHERE competition_id=?", (comp_id,)):
            out[r["trader_id"]] = Account(
                trader_id=r["trader_id"], cash=r["cash"], initial_cash=r["initial_cash"], realized_pnl=r["realized_pnl"],
                fees=r["fees"], peak_equity=r["peak_equity"], liquidations=r["liquidations"],
            )
        for r in self._q("SELECT * FROM positions WHERE competition_id=?", (comp_id,)):
            acc = out.get(r["trader_id"])
            if acc is None:
                continue
            acc.positions[r["symbol"]] = Position(
                trader_id=r["trader_id"], symbol=r["symbol"], side=r["side"], qty=r["qty"], avg_price=r["avg_price"],
                margin=r["margin"], leverage=r["leverage"], stop_loss=r["stop_loss"], take_profit=r["take_profit"],
                opened_at=r["opened_at"], last_price=r["last_price"],
            )
        return out

    # ---- 成交 / 决策 / 快照 ----
    @staticmethod
    def _insert_trade(c: sqlite3.Connection, comp_id: int, t: Trade) -> int:
        cur = c.execute(
            "INSERT INTO trades(competition_id, trader_id, symbol, side, action, qty, price, fee, realized_pnl, ts, round_no, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (comp_id, t.trader_id, t.symbol, t.side, t.action, t.qty, t.price, t.fee, t.realized_pnl, t.ts, t.round_no, t.reason),
        )
        t.id = int(cur.lastrowid)
        return t.id

    def add_trade(self, comp_id: int, t: Trade) -> int:
        with self._write_lock:
            return self._insert_trade(self.conn(), comp_id, t)

    def trades(self, comp_id: int, trader_id: Optional[str] = None, limit: int = 200, offset: int = 0, since: float = 0) -> List[dict]:
        sql = "SELECT * FROM trades WHERE competition_id=? AND ts>=?"
        params: List[Any] = [comp_id, since]
        if trader_id:
            sql += " AND trader_id=?"
            params.append(trader_id)
        sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in self._q(sql, params)]

    def trade_count(self, comp_id: int, trader_id: Optional[str] = None) -> int:
        if trader_id:
            return self._q("SELECT COUNT(*) c FROM trades WHERE competition_id=? AND trader_id=?", (comp_id, trader_id))[0]["c"]
        return self._q("SELECT COUNT(*) c FROM trades WHERE competition_id=?", (comp_id,))[0]["c"]

    @staticmethod
    def _insert_decision(c: sqlite3.Connection, comp_id: int, d: Decision) -> int:
        cur = c.execute(
            "INSERT INTO decisions(competition_id, trader_id, round_no, ts, mode, analysis, actions_json, results_json, raw_response, prompt, latency_ms, tokens_in, tokens_out, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (comp_id, d.trader_id, d.round_no, d.ts, d.mode, d.analysis, json.dumps([a.__dict__ for a in d.actions], ensure_ascii=False),
             json.dumps(d.results, ensure_ascii=False), d.raw_response, d.prompt, d.latency_ms, d.tokens_in, d.tokens_out, d.error),
        )
        return int(cur.lastrowid)

    def add_decision(self, comp_id: int, d: Decision) -> int:
        with self._write_lock:
            return self._insert_decision(self.conn(), comp_id, d)

    def decisions(self, comp_id: int, trader_id: Optional[str] = None, limit: int = 50, offset: int = 0, with_prompt: bool = False) -> List[dict]:
        cols = "id, competition_id, trader_id, round_no, ts, mode, analysis, actions_json, results_json, raw_response, latency_ms, tokens_in, tokens_out, error" + (", prompt" if with_prompt else "")
        sql = f"SELECT {cols} FROM decisions WHERE competition_id=?"
        params: List[Any] = [comp_id]
        if trader_id:
            sql += " AND trader_id=?"
            params.append(trader_id)
        sql += " ORDER BY round_no DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        out = []
        for r in self._q(sql, params):
            d = dict(r)
            d["actions"] = json.loads(d.pop("actions_json") or "[]")
            d["results"] = json.loads(d.pop("results_json") or "[]")
            out.append(d)
        return out

    @staticmethod
    def _public_decision_row(item: dict) -> dict:
        """Strip runtime and provider error bodies from the browser-facing decision view."""
        result = dict(item)
        error = str(result.pop("error", "") or "")
        if error:
            result["failure_category"] = decision_failure_category(error, mode=str(result.get("mode") or ""))
        raw_results = result.get("results")
        if isinstance(raw_results, list):
            projected_results = []
            for raw_item in raw_results:
                if not isinstance(raw_item, dict):
                    projected_results.append(raw_item)
                    continue
                projected_item = dict(raw_item)
                reason = projected_item.get("reason")
                if isinstance(reason, str):
                    projected_item["reason"] = public_execution_result_reason(reason)
                projected_results.append(projected_item)
            result["results"] = projected_results
        return result

    def public_decisions(self, comp_id: int, trader_id: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[dict]:
        """Return decisions with fixed failure categories instead of provider text."""
        return [self._public_decision_row(item) for item in self.decisions(comp_id, trader_id, limit, offset)]

    def decision(self, decision_id: int) -> Optional[dict]:
        rows = self._q("SELECT * FROM decisions WHERE id=?", (decision_id,))
        return self._decision_row(rows)

    def decision_for_competition(self, comp_id: int, decision_id: int) -> Optional[dict]:
        """Return a raw decision only when it belongs to the active competition."""
        rows = self._q("SELECT * FROM decisions WHERE competition_id=? AND id=?", (comp_id, decision_id))
        return self._decision_row(rows)

    def public_decision_for_competition(self, comp_id: int, decision_id: int) -> Optional[dict]:
        """Return an active-competition decision without the provider error body."""
        item = self.decision_for_competition(comp_id, decision_id)
        return self._public_decision_row(item) if item else None

    @staticmethod
    def _decision_row(rows: List[sqlite3.Row]) -> Optional[dict]:
        if not rows:
            return None
        d = dict(rows[0])
        d["actions"] = json.loads(d.pop("actions_json") or "[]")
        d["results"] = json.loads(d.pop("results_json") or "[]")
        return d

    def add_equity(self, comp_id: int, pts: Iterable[EquityPoint]):
        # executemany 包在显式事务里：连接是 autocommit 模式，否则逐行提交，
        # 中途失败会留下半截快照。_transaction 内部已持有 _write_lock（RLock）。
        with self._transaction() as c:
            c.executemany(
                "INSERT OR REPLACE INTO equity_snapshots(competition_id, trader_id, ts, equity, cash, margin_used, unrealized) VALUES (?,?,?,?,?,?,?)",
                [(comp_id, p.trader_id, p.ts, p.equity, p.cash, p.margin_used, p.unrealized) for p in pts],
            )

    def equity_series(self, comp_id: int, since: float = 0, max_points: int = 2000) -> Dict[str, List[list]]:
        rows = self._q(
            "SELECT trader_id, ts, equity FROM equity_snapshots WHERE competition_id=? AND ts>=? ORDER BY ts",
            (comp_id, since),
        )
        out: Dict[str, List[list]] = {}
        for r in rows:
            out.setdefault(r["trader_id"], []).append([r["ts"], r["equity"]])
        # 降采样，保证前端点数可控
        for k, series in out.items():
            if len(series) > max_points:
                step = len(series) / max_points
                sampled = [series[int(i * step)] for i in range(max_points)]
                if sampled[-1] is not series[-1]:
                    sampled.append(series[-1])
                out[k] = sampled
        return out

    def equity_latest_ts(self, comp_id: int) -> Optional[float]:
        """最新权益快照的时间戳（回放竞赛的时间轴与墙钟无关，看板的时间范围以此为基准）。"""
        rows = self._q("SELECT MAX(ts) AS mts FROM equity_snapshots WHERE competition_id=?", (comp_id,))
        return float(rows[0]["mts"]) if rows and rows[0]["mts"] is not None else None

    def equity_buckets(self, comp_id: int, trader_id: str, bucket_sec: int) -> List[list]:
        """按时间桶取每桶最后一个权益点：[[bucket_index, equity], ...]（用于 Sharpe 等低频指标）。"""
        rows = self._q(
            "SELECT CAST(ts/? AS INTEGER) AS b, MAX(ts) AS mts, equity FROM equity_snapshots "
            "WHERE competition_id=? AND trader_id=? GROUP BY b ORDER BY b",
            (bucket_sec, comp_id, trader_id),
        )
        return [[r["b"], r["equity"]] for r in rows]

    def equity_peak_and_mdd(self, comp_id: int, trader_id: str) -> tuple:
        """一次性扫描历史快照，返回 (peak, max_drawdown_pct)。仅启动时调用。"""
        peak, mdd = 0.0, 0.0
        for r in self.conn().execute(
                "SELECT equity FROM equity_snapshots WHERE competition_id=? AND trader_id=? ORDER BY ts", (comp_id, trader_id)):
            e = r["equity"]
            peak = max(peak, e)
            if peak > 0:
                mdd = max(mdd, (peak - e) / peak * 100)
        return peak, mdd

    def equity_values(self, comp_id: int, trader_id: str, since: float = 0) -> List[float]:
        return [r["equity"] for r in self._q(
            "SELECT equity FROM equity_snapshots WHERE competition_id=? AND trader_id=? AND ts>=? ORDER BY ts", (comp_id, trader_id, since))]

    def upsert_daily(self, comp_id: int, trader_id: str, day: str, start_equity: float, end_equity: float, trades: int, fees: float):
        self._exec(
            "INSERT INTO daily_summaries(competition_id, trader_id, day, start_equity, end_equity, pnl, trades, fees) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(competition_id, trader_id, day) DO UPDATE SET end_equity=excluded.end_equity, pnl=excluded.pnl, trades=excluded.trades, fees=excluded.fees",
            (comp_id, trader_id, day, start_equity, end_equity, end_equity - start_equity, trades, fees),
        )

    def daily(self, comp_id: int, limit_days: int = 60) -> List[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM daily_summaries WHERE competition_id=? ORDER BY day DESC, trader_id LIMIT ?", (comp_id, limit_days * 8))]

    def trades_between(self, comp_id: int, trader_id: str, t0: float, t1: float) -> List[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM trades WHERE competition_id=? AND trader_id=? AND ts>=? AND ts<? ORDER BY ts", (comp_id, trader_id, t0, t1))]

    def all_trades(self, comp_id: int, trader_id: str) -> List[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM trades WHERE competition_id=? AND trader_id=? ORDER BY ts", (comp_id, trader_id))]

    # ---- Research debate (append-only, never part of executable decisions) ----
    def reserve_debate_session(self, comp_id: int, session_key: str, phase: str, phase_label: str,
                               scheduled_open_at: Optional[float], manifest: Dict[str, Any]) -> Optional[int]:
        """Reserve one immutable research session, or return None if it already exists.

        Reservation happens before paid model calls.  A duplicate scheduler tick may
        therefore only observe the existing row; it cannot submit a second debate.

        The insert is an ``INSERT…SELECT`` against ``competitions.status='running'``.
        Reset archives the captured contest before a slow full-universe scan returns;
        a late insert must not attach research to that ended row.
        """
        with self._transaction() as c:
            try:
                cur = c.execute(
                    "INSERT INTO debate_sessions(competition_id, session_key, phase, phase_label, scheduled_open_at, market_as_of, "
                    "analysis_id, snapshot_hash, snapshot_json, global_analysis_json, status, started_at) "
                    "SELECT id,?,?,?,?,?,?,?,?,?,?,? FROM competitions WHERE id=? AND status='running'",
                    (session_key, phase, phase_label, scheduled_open_at, manifest.get("market_as_of"),
                     manifest["analysis_id"], manifest["snapshot_hash"], json.dumps(manifest, ensure_ascii=False),
                     json.dumps(manifest.get("global_analysis") or {}, ensure_ascii=False), "running", time.time(),
                     comp_id),
                )
            except sqlite3.IntegrityError:
                return None
            if not cur.rowcount:
                return None
            return int(cur.lastrowid)

    def add_debate_turn(self, debate_id: int, stage: str, trader_id: str, ordinal: int, status: str,
                        payload: Optional[Dict[str, Any]] = None, raw_response: str = "", prompt: str = "",
                        latency_ms: Optional[int] = None, tokens_in: Optional[int] = None,
                        tokens_out: Optional[int] = None, error: str = "", *, only_active: bool = False) -> bool:
        """写入一条回合；返回是否真的写进去了。

        ``only_active``：会话已被别处（暂停/重置/停止触发的 ``abort_running_debates``）定成终态时不再追加。
        阶段跑完到落库之间存在一段窗口，仅靠调用方复查生命周期挡不住全部——归档记录不该在"已中止"之后继续长出模型输出。
        """
        payload = payload or {}
        columns = ("INSERT INTO debate_turns(debate_id, stage, trader_id, ordinal, status, stance, confidence, payload_json, "
                   "raw_response, prompt, latency_ms, tokens_in, tokens_out, error, created_at) ")
        upsert = ("ON CONFLICT(debate_id, stage, trader_id) DO UPDATE SET ordinal=excluded.ordinal, status=excluded.status, "
                  "stance=excluded.stance, confidence=excluded.confidence, payload_json=excluded.payload_json, "
                  "raw_response=excluded.raw_response, prompt=excluded.prompt, latency_ms=excluded.latency_ms, "
                  "tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out, error=excluded.error")
        params: list[object] = [debate_id, stage, trader_id, ordinal, status, payload.get("stance"), payload.get("confidence"),
                                json.dumps(payload, ensure_ascii=False), raw_response, prompt, latency_ms, tokens_in,
                                tokens_out, error[:2000], time.time()]
        if only_active:
            # INSERT…SELECT…WHERE：会话状态与写入在同一条语句里判定，复查与写入之间没有可乘之机
            sql = (columns + "SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS "
                   "(SELECT 1 FROM debate_sessions WHERE id=? AND status IN ('running','opening','rebuttal')) " + upsert)
            params.append(debate_id)
        else:
            sql = columns + "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) " + upsert
        cur = self._exec(sql, tuple(params))
        return bool(cur.rowcount)

    def complete_debate_session(self, debate_id: int, status: str, preliminary: Optional[Dict[str, Any]] = None,
                                referee_packet: str = "", error: str = "", *, only_active: bool = False) -> bool:
        """Set a terminal research status without reviving a session stopped elsewhere.

        ``only_active`` is used by background workers.  If a shutdown/restart
        already made the append-only session terminal, a late HTTP worker must
        not overwrite that durable reason with a generic completion status.
        """
        where = "WHERE id=?"
        params: list[object] = [status, json.dumps(preliminary or {}, ensure_ascii=False), referee_packet,
                                error[:2000], time.time(), debate_id]
        if only_active:
            where += " AND status IN ('running','opening','rebuttal')"
        cur = self._exec(
            f"UPDATE debate_sessions SET status=?, preliminary_json=?, referee_packet=?, error=?, completed_at=? {where}",
            params,
        )
        return bool(cur.rowcount)

    def reopen_product_research_debate_session(self, debate_id: int) -> bool:
        """Reopen only a deterministic-failure product session for its explicit retry.

        Product research persists model slots independently from the presentation
        session.  A partial result is terminal for display, but an operator may
        explicitly retry a failed slot.  This narrow transition never applies to
        delivery-unknown work and never revives a normal decision-room session.
        """
        cur = self._exec(
            "UPDATE debate_sessions SET status='running', error='', completed_at=NULL "
            "WHERE id=? AND phase='product_research' AND status IN ('incomplete','failed')",
            (debate_id,),
        )
        return bool(cur.rowcount)

    def set_debate_session_status(self, debate_id: int, status: str, error: str = "", *, only_active: bool = False) -> bool:
        where = "WHERE id=?"
        params: list[object] = [status, error[:2000], debate_id]
        if only_active:
            where += " AND status IN ('running','opening','rebuttal')"
        cur = self._exec(f"UPDATE debate_sessions SET status=?, error=? {where}", params)
        return bool(cur.rowcount)

    def abort_running_debates(self, comp_id: int, reason: str) -> int:
        cur = self._exec(
            "UPDATE debate_sessions SET status='aborted_restarted', error=?, completed_at=? "
            "WHERE competition_id=? AND phase<>'product_research' AND status IN ('running','opening','rebuttal')",
            (reason[:2000], time.time(), comp_id),
        )
        return int(cur.rowcount or 0)

    @staticmethod
    def _debate_session_row(row: sqlite3.Row, *, include_snapshot: bool, include_packet: bool = False,
                            include_error: bool = False) -> dict:
        item = dict(row)
        for source, target in (("global_analysis_json", "global_analysis"), ("preliminary_json", "preliminary")):
            try:
                item[target] = json.loads(item.pop(source) or "{}")
            except (TypeError, ValueError):
                item[target] = {}
        raw_snapshot = item.pop("snapshot_json", None)
        if include_snapshot:
            try:
                item["snapshot"] = json.loads(raw_snapshot or "{}")
            except (TypeError, ValueError):
                item["snapshot"] = {}
        if not include_packet:
            item.pop("referee_packet", None)
        if not include_error:
            item.pop("error", None)
        return item

    @staticmethod
    def _debate_turn_row(row: sqlite3.Row, *, include_raw: bool) -> dict:
        item = dict(row)
        error_code = item.get("error")
        try:
            payload = json.loads(item.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        # A failed turn has no model-authored structured payload. Do not let a
        # stored or future payload field override the server-side error allowlist.
        payload.pop("failure_category", None)
        if include_raw or item.get("status") == "complete":
            item.update(payload)
        if not include_raw:
            item.pop("raw_response", None)
            item.pop("prompt", None)
            item.pop("error", None)
            if item.get("status") != "complete":
                category = PUBLIC_DEBATE_FAILURE_CATEGORY_BY_ERROR.get(error_code)
                if category:
                    item["failure_category"] = category
        return item

    def debate_sessions(self, comp_id: int, limit: int = 20, offset: int = 0) -> List[dict]:
        rows = self._q(
            "SELECT * FROM debate_sessions WHERE competition_id=? ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (comp_id, limit, offset),
        )
        return [self._debate_session_row(row, include_snapshot=False) for row in rows]

    def debate_session(self, comp_id: int, debate_id: int, *, include_raw: bool = True) -> Optional[dict]:
        rows = self._q("SELECT * FROM debate_sessions WHERE competition_id=? AND id=?", (comp_id, debate_id))
        if not rows:
            return None
        item = self._debate_session_row(rows[0], include_snapshot=True, include_packet=True, include_error=include_raw)
        turns = self._q("SELECT * FROM debate_turns WHERE debate_id=? ORDER BY stage, ordinal, id", (debate_id,))
        item["turns"] = [self._debate_turn_row(row, include_raw=include_raw) for row in turns]
        return item

    def latest_debate_session(self, comp_id: int) -> Optional[dict]:
        items = self.debate_sessions(comp_id, limit=1)
        return items[0] if items else None

    def debate_session_by_key(self, comp_id: int, session_key: str) -> Optional[dict]:
        """Return a compact idempotency record without loading transcript data."""
        rows = self._q(
            "SELECT * FROM debate_sessions WHERE competition_id=? AND session_key=?", (comp_id, session_key),
        )
        return self._debate_session_row(rows[0], include_snapshot=False) if rows else None

    # ---- 独立全量研究扫描（不绑定竞赛、账户或模型会话） ----
    @staticmethod
    def _research_number(value: Any) -> Optional[float]:
        """Persist only finite numeric projections; source payloads never reach this boundary."""
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _research_int(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid research scan {field}")
        return int(value)

    @staticmethod
    def _research_text(value: Any, field: str, limit: int, *, allow_empty: bool = False) -> str:
        """Accept only short, presentation-safe labels at the persistence boundary."""
        if not isinstance(value, str):
            raise ValueError(f"invalid research scan {field}")
        text = value.strip()
        if (not allow_empty and not text) or len(text) > limit or any(ord(char) < 32 for char in text):
            raise ValueError(f"invalid research scan {field}")
        lowered = text.lower()
        forbidden = ("://", "www.", "secret", "authorization", "bearer ", "cookie", "set-cookie", "x-api-key")
        if any(marker in lowered for marker in forbidden):
            raise ValueError(f"unsafe research scan {field}")
        return text

    @classmethod
    def _normalise_research_item(cls, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValueError("research scan item must be a mapping")
        symbol = cls._research_text(raw.get("symbol"), "symbol", 16)
        if re.fullmatch(r"[A-Z]{1,3}0", symbol) is None:
            raise ValueError("invalid research scan symbol")
        name = cls._research_text(raw.get("name"), "name", 120)
        exchange = cls._research_text(raw.get("exchange"), "exchange", 32)
        outcome = cls._research_text(raw.get("outcome"), "outcome", 16)
        stage = cls._research_text(raw.get("stage"), "stage", 16)
        reason = cls._research_text(raw.get("reason"), "reason", 64, allow_empty=True)
        if outcome not in PUBLIC_SCAN_OUTCOMES or stage not in PUBLIC_SCAN_STAGES:
            raise ValueError("invalid research scan item classification")
        if outcome == "scanned":
            if stage != "analysis" or reason:
                raise ValueError("successful research scan item must be an analysis row without a reason")
            trend = cls._research_text(raw.get("trend"), "trend", 16)
            if trend not in PUBLIC_SCAN_TRENDS:
                raise ValueError("invalid research scan trend")
            latest_price = cls._research_number(raw.get("latest_price"))
            change_pct = cls._research_number(raw.get("change_pct"))
            score = cls._research_number(raw.get("score"))
            if latest_price is None or latest_price <= 0 or change_pct is None or score is None:
                raise ValueError("invalid research scan analysis numbers")
            return {"symbol": symbol, "name": name, "exchange": exchange, "outcome": outcome, "stage": stage,
                    "reason": "", "latest_price": latest_price, "change_pct": change_pct, "trend": trend,
                    "score": score}
        if reason not in PUBLIC_SCAN_REASONS:
            raise ValueError("invalid research scan exclusion reason")
        if reason not in PUBLIC_SCAN_STAGE_REASONS.get(stage, frozenset()):
            raise ValueError("research scan exclusion reason has an invalid stage")
        if any(raw.get(field) is not None for field in ("latest_price", "change_pct", "trend", "score")):
            raise ValueError("excluded research scan item must not carry market values")
        return {"symbol": symbol, "name": name, "exchange": exchange, "outcome": outcome, "stage": stage,
                "reason": reason, "latest_price": None, "change_pct": None, "trend": None, "score": None}

    @classmethod
    def _normalise_research_market(cls, raw: Any, scanned_count: int) -> Optional[Dict[str, Any]]:
        """Validate optional, hashed source-time disclosure for newer scan manifests."""
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ValueError("invalid research scan market disclosure")
        if scanned_count <= 0:
            raise ValueError("empty research scan cannot claim a market disclosure")
        timestamps: Dict[str, float] = {}
        for field in ("quote_as_of_min", "quote_as_of_max", "history_as_of_min", "history_as_of_max"):
            value = cls._research_number(raw.get(field))
            if value is None:
                raise ValueError("invalid research scan market timestamp")
            timestamps[field] = value
        if (timestamps["quote_as_of_min"] > timestamps["quote_as_of_max"]
                or timestamps["history_as_of_min"] > timestamps["history_as_of_max"]):
            raise ValueError("invalid research scan market timestamp range")
        mode = cls._research_text(raw.get("freshness_mode"), "market freshness mode", 32)
        if mode not in {"live_session", "prior_session", "mixed"}:
            raise ValueError("invalid research scan market freshness mode")
        prior_count = cls._research_int(raw.get("prior_session_count"), "prior session count")
        live_count = cls._research_int(raw.get("live_session_count"), "live session count")
        if prior_count + live_count != scanned_count:
            raise ValueError("research scan market counts do not match scanned rows")
        if ((mode == "prior_session" and (prior_count != scanned_count or live_count != 0))
                or (mode == "live_session" and (live_count != scanned_count or prior_count != 0))
                or (mode == "mixed" and (prior_count <= 0 or live_count <= 0))):
            raise ValueError("research scan market freshness mode does not match counts")
        return {**timestamps, "freshness_mode": mode, "prior_session_count": prior_count,
                "live_session_count": live_count}

    @classmethod
    def _normalise_research_manifest(cls, manifest: Mapping[str, Any]) -> Dict[str, Any]:
        """Build the exact public projection that is hashed, persisted, and later re-proved."""
        if not isinstance(manifest, Mapping):
            raise ValueError("research scan manifest must be a mapping")
        status = cls._research_text(manifest.get("status"), "status", 16)
        if status not in {"complete", "partial", "incomplete", "unavailable"}:
            raise ValueError("invalid research scan status")
        catalogue_raw = manifest.get("catalogue")
        counts_raw = manifest.get("counts")
        coverage_raw = manifest.get("coverage")
        items_raw = manifest.get("items")
        if not isinstance(catalogue_raw, Mapping) or not isinstance(counts_raw, Mapping) or not isinstance(coverage_raw, Mapping):
            raise ValueError("invalid research scan manifest sections")
        if not isinstance(items_raw, list):
            raise ValueError("invalid research scan items")
        catalogue = {
            "schema_version": cls._research_int(catalogue_raw.get("schema_version"), "catalogue schema version"),
            "content_hash": cls._research_text(catalogue_raw.get("content_hash"), "catalogue hash", 80, allow_empty=True),
            "snapshot_status": cls._research_text(catalogue_raw.get("snapshot_status"), "catalogue status", 32),
            "fetched_at": cls._research_text(catalogue_raw.get("fetched_at"), "catalogue timestamp", 80, allow_empty=True),
        }
        counts = {
            "discovered": cls._research_int(counts_raw.get("discovered"), "discovered count"),
            "eligible": cls._research_int(counts_raw.get("eligible"), "eligible count"),
            "scanned": cls._research_int(counts_raw.get("scanned"), "scanned count"),
            "excluded": cls._research_int(counts_raw.get("excluded"), "excluded count"),
        }
        if not (counts["scanned"] <= counts["eligible"] <= counts["discovered"]
                and counts["scanned"] + counts["excluded"] == counts["discovered"]):
            raise ValueError("invalid research scan counts")
        items = sorted((cls._normalise_research_item(item) for item in items_raw), key=lambda item: item["symbol"])
        if len(items) != counts["discovered"] or len({item["symbol"] for item in items}) != len(items):
            raise ValueError("research scan items do not match the discovered denominator")
        if sum(item["outcome"] == "scanned" for item in items) != counts["scanned"]:
            raise ValueError("research scan scanned count does not match items")
        # ``eligible`` is intentionally not a transient quote-success count.
        # It means this candidate cleared the immutable catalogue proof gate and
        # was permitted to enter the public-market read phase.  Derive it from
        # the persisted terminal rows so a hash-consistent caller cannot label a
        # proof-rejected candidate as catalogue-eligible.
        derived_eligible = sum(
            not (item["outcome"] == "excluded" and item["stage"] == "catalogue")
            for item in items
        )
        if counts["eligible"] != derived_eligible:
            raise ValueError("research scan eligible count does not match catalogue proof rows")
        reasons = {item["reason"] for item in items if item["outcome"] == "excluded"}
        has_incomplete_reason = ("catalogue_incomplete" in reasons or "scan_cancelled" in reasons
                                 or any(reason.endswith("_timeout") for reason in reasons))
        denominator_known = coverage_raw.get("denominator_known")
        if not isinstance(denominator_known, bool):
            raise ValueError("invalid research scan denominator flag")
        coverage_status = cls._research_text(coverage_raw.get("status"), "coverage status", 32)
        ratio = cls._research_number(coverage_raw.get("ratio"))
        if coverage_raw.get("ratio") is not None and ratio is None:
            raise ValueError("invalid research scan coverage ratio")
        if status == "unavailable":
            if denominator_known or any(counts.values()) or items or coverage_status != "unavailable" or ratio is not None:
                raise ValueError("unavailable research scan must not claim a denominator")
        else:
            if catalogue["snapshot_status"] != "complete":
                if (status != "incomplete" or counts["eligible"] != 0 or counts["scanned"] != 0
                        or any(item["outcome"] != "excluded" or item["stage"] != "catalogue"
                               or item["reason"] != "catalogue_incomplete" for item in items)):
                    raise ValueError("untrusted catalogue must be a catalogue-only incomplete scan")
            if not denominator_known or coverage_status != ("complete" if status == "complete" else "incomplete"):
                raise ValueError("invalid research scan coverage state")
            if status == "complete":
                if counts["scanned"] != counts["discovered"] or counts["excluded"] != 0 or counts["eligible"] != counts["discovered"]:
                    raise ValueError("complete research scan has unresolved candidates")
            if status == "partial" and has_incomplete_reason:
                raise ValueError("partial research scan cannot hide an incomplete reason")
            if status == "incomplete" and catalogue["snapshot_status"] == "complete" and not has_incomplete_reason:
                raise ValueError("incomplete research scan needs a bounded timeout or cancellation reason")
            expected_ratio = (counts["scanned"] / counts["discovered"]) if status in {"complete", "partial"} and counts["discovered"] else None
            if expected_ratio is None:
                if ratio is not None:
                    raise ValueError("research scan coverage ratio must be null")
            elif ratio is None or not math.isclose(ratio, expected_ratio, abs_tol=0.0000005):
                raise ValueError("research scan coverage ratio does not match counts")
        market = cls._normalise_research_market(manifest.get("market"), counts["scanned"])
        started_at = cls._research_number(manifest.get("started_at"))
        completed_at = cls._research_number(manifest.get("completed_at"))
        if started_at is None or completed_at is None or completed_at < started_at:
            raise ValueError("research scan timestamps must be finite and ordered")
        canonical = {
            "schema_version": cls._research_int(manifest.get("schema_version"), "schema version"),
            "rules_version": cls._research_text(manifest.get("rules_version"), "rules version", 80),
            "status": status,
            "catalogue": catalogue,
            "counts": counts,
            "coverage": {"status": coverage_status, "ratio": ratio, "denominator_known": denominator_known},
            "started_at": started_at,
            "completed_at": completed_at,
            "items": items,
        }
        if market is not None:
            canonical["market"] = market
        return canonical

    def append_research_scan(self, manifest: Mapping[str, Any]) -> int:
        """Append one completed, immutable research manifest and its one-row-per-symbol projection.

        The scan worker supplies only a whitelisted summary.  This method refuses
        malformed rows rather than silently dropping them, so the run-level
        counts can never claim a complete denominator while individual symbols
        disappeared during persistence.
        """
        canonical = self._normalise_research_manifest(manifest)
        calculated_hash = canonical_manifest_hash(canonical)
        supplied_hash = self._research_text(manifest.get("manifest_hash"), "manifest hash", 80)
        if supplied_hash != calculated_hash:
            raise ValueError("research scan manifest hash does not match canonical facts")
        canonical["manifest_hash"] = calculated_hash
        status = canonical["status"]
        catalogue = canonical["catalogue"]
        counts = canonical["counts"]
        coverage = canonical["coverage"]
        with self._transaction() as c:
            cur = c.execute(
                "INSERT INTO research_scan_runs(status, catalogue_schema_version, catalogue_content_hash, "
                "catalogue_snapshot_status, catalogue_fetched_at, discovered_count, eligible_count, scanned_count, "
                "excluded_count, coverage_status, coverage_ratio, denominator_known, manifest_hash, manifest_json, "
                "started_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (status, catalogue["schema_version"], catalogue["content_hash"], catalogue["snapshot_status"],
                 catalogue["fetched_at"], counts["discovered"], counts["eligible"], counts["scanned"], counts["excluded"],
                 coverage["status"], coverage["ratio"], 1 if coverage["denominator_known"] else 0, calculated_hash,
                 json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
                 canonical["started_at"], canonical["completed_at"]),
            )
            run_id = int(cur.lastrowid)
            for raw in canonical["items"]:
                c.execute(
                    "INSERT INTO research_scan_items(run_id, symbol, name, exchange, outcome, stage, reason, "
                    "latest_price, change_pct, trend, score) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, raw["symbol"], raw["name"], raw["exchange"], raw["outcome"], raw["stage"], raw["reason"],
                     raw["latest_price"], raw["change_pct"], raw["trend"], raw["score"]),
                )
        return run_id

    def latest_research_scan(self) -> Optional[dict]:
        """Return the safe latest-run projection; never expose raw upstream failures or source payloads."""
        rows = self._q(
            "SELECT id, status, catalogue_schema_version, catalogue_content_hash, catalogue_snapshot_status, "
            "catalogue_fetched_at, discovered_count, eligible_count, scanned_count, excluded_count, coverage_status, "
            "coverage_ratio, denominator_known, manifest_hash, manifest_json, started_at, completed_at "
            "FROM research_scan_runs ORDER BY started_at DESC, id DESC LIMIT 1"
        )
        if not rows:
            return None
        row = dict(rows[0])
        item_rows = self._q(
            "SELECT symbol, name, exchange, outcome, stage, reason, latest_price, change_pct, trend, score "
            "FROM research_scan_items WHERE run_id=? ORDER BY symbol",
            (row["id"],),
        )
        try:
            stored = json.loads(row["manifest_json"])
            if not isinstance(stored, Mapping):
                return None
            projection = {
                "schema_version": stored.get("schema_version"), "rules_version": stored.get("rules_version"),
                "status": row["status"],
                "catalogue": {
                    "schema_version": row["catalogue_schema_version"], "content_hash": row["catalogue_content_hash"],
                    "snapshot_status": row["catalogue_snapshot_status"], "fetched_at": row["catalogue_fetched_at"],
                },
                "counts": {
                    "discovered": row["discovered_count"], "eligible": row["eligible_count"],
                    "scanned": row["scanned_count"], "excluded": row["excluded_count"],
                },
                "coverage": {
                    "status": row["coverage_status"], "ratio": row["coverage_ratio"],
                    "denominator_known": bool(row["denominator_known"]),
                },
                "started_at": row["started_at"], "completed_at": row["completed_at"],
                "items": [dict(item) for item in item_rows],
            }
            if "market" in stored:
                projection["market"] = stored.get("market")
            canonical = self._normalise_research_manifest(projection)
            if canonical_manifest_hash(canonical) != row["manifest_hash"]:
                return None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        result = {
            "id": row["id"], "schema_version": canonical["schema_version"],
            "rules_version": canonical["rules_version"], "status": row["status"],
            "catalogue": {
                "schema_version": row["catalogue_schema_version"], "content_hash": row["catalogue_content_hash"],
                "snapshot_status": row["catalogue_snapshot_status"], "fetched_at": row["catalogue_fetched_at"],
            },
            "counts": {
                "discovered": row["discovered_count"], "eligible": row["eligible_count"],
                "scanned": row["scanned_count"], "excluded": row["excluded_count"],
            },
            "coverage": {
                "status": row["coverage_status"], "ratio": row["coverage_ratio"],
                "denominator_known": bool(row["denominator_known"]),
            },
            "manifest_hash": row["manifest_hash"], "started_at": row["started_at"], "completed_at": row["completed_at"],
            "items": [dict(item) for item in item_rows],
        }
        if "market" in canonical:
            result["market"] = canonical["market"]
        return result

    # ---- Full-universe product research debates (never executable) ----
    @staticmethod
    def _product_research_run_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in (
            "discovered_count", "eligible_count", "excluded_count", "pending_count", "running_count",
            "complete_count", "partial_count", "failed_count", "candidate_batch_size", "model_concurrency",
            "max_attempts",
        ):
            item[key] = int(item.get(key) or 0)
        return item

    def create_product_research_run(self, comp_id: int, run_key: str, trigger: str, mapping_rule: str,
                                    catalogue_path: str, candidate_batch_size: int, model_concurrency: int,
                                    max_attempts: int) -> int:
        """Reserve a full-universe run before any public-market or model work starts."""
        now = time.time()
        with self._transaction() as c:
            cur = c.execute(
                "INSERT INTO product_research_runs(competition_id, run_key, trigger, status, mapping_rule, catalogue_path, "
                "candidate_batch_size, model_concurrency, max_attempts, created_at, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (comp_id, run_key, trigger[:32], "discovering", mapping_rule[:240], catalogue_path[:500],
                 int(candidate_batch_size), int(model_concurrency), int(max_attempts), now, now),
            )
            return int(cur.lastrowid)

    def product_research_run(self, comp_id: int, run_id: int) -> Optional[dict]:
        rows = self._q("SELECT * FROM product_research_runs WHERE competition_id=? AND id=?", (comp_id, run_id))
        return self._product_research_run_row(rows[0]) if rows else None

    def product_research_run_by_id(self, run_id: int) -> Optional[dict]:
        """Resolve a durable research run by its globally unique primary key.

        Product-research work is analysis-only and may need to finish after a
        routing-only configuration change starts a new trading competition.  The
        caller must still use the returned competition_id to scope its debates.
        """
        rows = self._q("SELECT * FROM product_research_runs WHERE id=?", (int(run_id),))
        return self._product_research_run_row(rows[0]) if rows else None

    def latest_product_research_run(self, comp_id: int) -> Optional[dict]:
        rows = self._q(
            "SELECT * FROM product_research_runs WHERE competition_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (comp_id,),
        )
        return self._product_research_run_row(rows[0]) if rows else None

    def latest_resumable_product_research_run(self) -> Optional[dict]:
        """Find the newest analysis queue that still has explicit work left.

        This is intentionally not used by the normal current-competition view:
        callers use it only to offer recovery of a clearly labelled historical
        analysis run, never to mix it into the trading leaderboard.
        """
        rows = self._q(
            "SELECT * FROM product_research_runs "
            "WHERE pending_count>0 OR running_count>0 "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        )
        return self._product_research_run_row(rows[0]) if rows else None

    def product_research_runs(self, comp_id: int, limit: int = 20, offset: int = 0) -> List[dict]:
        rows = self._q(
            "SELECT * FROM product_research_runs WHERE competition_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (comp_id, max(1, min(int(limit), 100)), max(0, int(offset))),
        )
        return [self._product_research_run_row(row) for row in rows]

    def product_research_item_for_debate(self, run_id: int, debate_id: int) -> Optional[dict]:
        """Verify a product-research debate is owned by exactly one requested run."""
        rows = self._q(
            "SELECT * FROM product_research_items WHERE run_id=? AND debate_id=?",
            (int(run_id), int(debate_id)),
        )
        return dict(rows[0]) if rows else None

    def product_research_participant_ids(self, run_id: int) -> List[str]:
        rows = self._q(
            "SELECT DISTINCT trader_id FROM product_research_calls WHERE run_id=? ORDER BY trader_id",
            (int(run_id),),
        )
        return [str(row["trader_id"]) for row in rows]

    def update_product_research_run(self, run_id: int, *, status: Optional[str] = None,
                                    catalogue_path: Optional[str] = None, catalogue_hash: Optional[str] = None,
                                    catalogue_fetched_at: Optional[str] = None, discovery_status: Optional[str] = None,
                                    scan_run_id: Optional[int] = None, max_attempts: Optional[int] = None,
                                    error: Optional[str] = None,
                                    completed: bool = False) -> bool:
        """Update only the run control plane; item/call evidence remains append-only."""
        fields: List[str] = []
        values: List[object] = []
        if status is not None:
            if status not in PRODUCT_RESEARCH_RUN_STATUSES:
                raise ValueError("invalid product research run status")
            fields.append("status=?")
            values.append(status)
        if catalogue_path is not None:
            fields.append("catalogue_path=?")
            values.append(catalogue_path[:500])
        if catalogue_hash is not None:
            fields.append("catalogue_hash=?")
            values.append(catalogue_hash[:128])
        if catalogue_fetched_at is not None:
            fields.append("catalogue_fetched_at=?")
            values.append(catalogue_fetched_at[:100])
        if discovery_status is not None:
            fields.append("discovery_status=?")
            values.append(discovery_status[:40])
        if scan_run_id is not None:
            fields.append("scan_run_id=?")
            values.append(int(scan_run_id))
        if max_attempts is not None:
            if int(max_attempts) < 1:
                raise ValueError("invalid product research max attempts")
            fields.append("max_attempts=?")
            values.append(int(max_attempts))
        if error is not None:
            fields.append("error=?")
            values.append(error[:200])
        if completed:
            fields.append("completed_at=?")
            values.append(time.time())
        if not fields:
            return False
        values.append(run_id)
        cur = self._exec(f"UPDATE product_research_runs SET {', '.join(fields)} WHERE id=?", values)
        return bool(cur.rowcount)

    def create_product_research_items(self, run_id: int, items: Iterable[Mapping[str, Any]]) -> None:
        """Create every discovered item and all eight planned model slots atomically.

        Planned calls receive their stable dispatch identity here but become
        ``submitted`` only immediately before the provider is invoked.  Thus a
        queued item is observable without claiming that a billable request exists.
        """
        now = time.time()
        with self._transaction() as c:
            for raw in items:
                symbol = str(raw.get("symbol") or "").upper()
                status = str(raw.get("status") or "")
                if not symbol or status not in PRODUCT_RESEARCH_ITEM_STATUSES:
                    raise ValueError("invalid product research item")
                cur = c.execute(
                    "INSERT INTO product_research_items(run_id, symbol, name, exchange, mapping_symbol, scan_stage, "
                    "exclusion_reason, status, batch_no, debate_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, symbol, str(raw.get("name") or symbol)[:120], str(raw.get("exchange") or "")[:32],
                     str(raw.get("mapping_symbol") or symbol)[:16], str(raw.get("scan_stage") or "")[:32],
                     str(raw.get("exclusion_reason") or "")[:80], status, raw.get("batch_no"), raw.get("debate_id"), now),
                )
                item_id = int(cur.lastrowid)
                for planned in raw.get("calls") or []:
                    c.execute(
                        "INSERT INTO product_research_calls(run_id, item_id, symbol, debate_id, stage, trader_id, attempt, "
                        "dispatch_key, status, error, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, item_id, symbol, int(planned["debate_id"]), str(planned["stage"]),
                         str(planned["trader_id"]), int(planned.get("attempt", 1)), str(planned["dispatch_key"]),
                         "pending", "", now),
                    )
        self.refresh_product_research_counts(run_id)

    def product_research_items(self, run_id: int, *, limit: int = 50, offset: int = 0,
                               status: Optional[str] = None) -> List[dict]:
        where = "WHERE run_id=?"
        params: List[object] = [run_id]
        if status:
            if status not in PRODUCT_RESEARCH_ITEM_STATUSES:
                return []
            where += " AND status=?"
            params.append(status)
        params.extend((max(1, min(int(limit), 200)), max(0, int(offset))))
        rows = self._q(
            f"SELECT * FROM product_research_items {where} ORDER BY symbol LIMIT ? OFFSET ?", params,
        )
        if not rows:
            return []
        item_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in item_ids)
        call_rows = self._q(
            f"SELECT item_id, stage, trader_id, attempt, dispatch_key, status, error, started_at, completed_at, debate_id "
            f"FROM product_research_calls WHERE item_id IN ({placeholders}) ORDER BY stage, trader_id, attempt",
            item_ids,
        )
        grouped: Dict[int, Dict[str, Dict[str, List[dict]]]] = {}
        for call in call_rows:
            entry = dict(call)
            grouped.setdefault(int(call["item_id"]), {}).setdefault(str(call["stage"]), {}).setdefault(
                str(call["trader_id"]), []).append(entry)
        result: List[dict] = []
        for row in rows:
            item = dict(row)
            item["models"] = grouped.get(int(row["id"]), {})
            result.append(item)
        return result

    def product_research_item(self, run_id: int, symbol: str) -> Optional[dict]:
        item_rows = self._q("SELECT * FROM product_research_items WHERE run_id=? AND symbol=?", (run_id, symbol))
        if not item_rows:
            return None
        row = item_rows[0]
        calls = self._q(
            "SELECT item_id, stage, trader_id, attempt, dispatch_key, status, error, started_at, completed_at, debate_id "
            "FROM product_research_calls WHERE item_id=? ORDER BY stage, trader_id, attempt",
            (row["id"],),
        )
        models: Dict[str, Dict[str, List[dict]]] = {}
        for call in calls:
            models.setdefault(str(call["stage"]), {}).setdefault(str(call["trader_id"]), []).append(dict(call))
        item = dict(row)
        item["models"] = models
        return item

    def set_product_research_item(self, run_id: int, symbol: str, status: str, *, debate_id: Optional[int] = None,
                                  attempt_count: Optional[int] = None, completed: bool = False) -> bool:
        if status not in PRODUCT_RESEARCH_ITEM_STATUSES:
            raise ValueError("invalid product research item status")
        fields = ["status=?", "updated_at=?"]
        values: List[object] = [status, time.time()]
        if debate_id is not None:
            fields.append("debate_id=?")
            values.append(int(debate_id))
        if attempt_count is not None:
            fields.append("attempt_count=?")
            values.append(int(attempt_count))
        if completed:
            fields.append("completed_at=?")
            values.append(time.time())
        values.extend((run_id, symbol))
        cur = self._exec(
            f"UPDATE product_research_items SET {', '.join(fields)} WHERE run_id=? AND symbol=?", values,
        )
        if cur.rowcount:
            self.refresh_product_research_counts(run_id)
        return bool(cur.rowcount)

    def mark_product_research_item_started(self, run_id: int, symbol: str) -> bool:
        now = time.time()
        cur = self._exec(
            "UPDATE product_research_items SET status='running', started_at=COALESCE(started_at, ?), updated_at=? "
            "WHERE run_id=? AND symbol=? AND status IN ('pending','partial','failed')",
            (now, now, run_id, symbol),
        )
        if cur.rowcount:
            self.refresh_product_research_counts(run_id)
        return bool(cur.rowcount)

    def product_research_call_attempts(self, run_id: int, symbol: str, stage: str, trader_id: str) -> List[dict]:
        return [dict(row) for row in self._q(
            "SELECT * FROM product_research_calls WHERE run_id=? AND symbol=? AND stage=? AND trader_id=? ORDER BY attempt",
            (run_id, symbol, stage, trader_id),
        )]

    def add_product_research_call_attempt(self, run_id: int, item_id: int, symbol: str, debate_id: int,
                                          stage: str, trader_id: str, attempt: int, dispatch_key: str) -> bool:
        try:
            self._exec(
                "INSERT INTO product_research_calls(run_id, item_id, symbol, debate_id, stage, trader_id, attempt, "
                "dispatch_key, status, error, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, item_id, symbol, debate_id, stage, trader_id, attempt, dispatch_key, "pending", "", time.time()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def submit_product_research_call(self, dispatch_key: str) -> bool:
        cur = self._exec(
            "UPDATE product_research_calls SET status='submitted', started_at=?, completed_at=NULL "
            "WHERE dispatch_key=? AND status='pending'",
            (time.time(), dispatch_key),
        )
        return bool(cur.rowcount)

    def persist_product_research_result(self, dispatch_key: str, *, status: str, payload: Optional[Dict[str, Any]],
                                        raw_response: str, prompt: str, latency_ms: Optional[int],
                                        tokens_in: Optional[int], tokens_out: Optional[int], error: str) -> bool:
        """Atomically persist one returned model result and its dispatch receipt.

        A late worker cannot revive a call previously marked ``delivery_unknown``
        by a deadline or shutdown.  This prevents a late result from masking the
        fact that the caller cannot prove whether the provider billed the request.
        """
        if status not in {"complete", "failed"}:
            raise ValueError("invalid terminal product research call status")
        with self._transaction() as c:
            rows = c.execute(
                "SELECT debate_id, stage, trader_id FROM product_research_calls WHERE dispatch_key=? AND status='submitted'",
                (dispatch_key,),
            ).fetchall()
            if not rows:
                return False
            call = rows[0]
            active = c.execute(
                "SELECT 1 FROM debate_sessions WHERE id=? AND status IN ('running','opening','rebuttal')",
                (call["debate_id"],),
            ).fetchone()
            if not active:
                return False
            body = payload or {}
            c.execute(
                "INSERT INTO debate_turns(debate_id, stage, trader_id, ordinal, status, stance, confidence, payload_json, "
                "raw_response, prompt, latency_ms, tokens_in, tokens_out, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(debate_id, stage, trader_id) DO UPDATE SET status=excluded.status, stance=excluded.stance, "
                "confidence=excluded.confidence, payload_json=excluded.payload_json, raw_response=excluded.raw_response, "
                "prompt=excluded.prompt, latency_ms=excluded.latency_ms, tokens_in=excluded.tokens_in, "
                "tokens_out=excluded.tokens_out, error=excluded.error",
                (call["debate_id"], call["stage"], call["trader_id"], 1, status, body.get("stance"),
                 body.get("confidence"), json.dumps(body, ensure_ascii=False), raw_response, prompt, latency_ms,
                 tokens_in, tokens_out, error[:200], time.time()),
            )
            c.execute(
                "UPDATE product_research_calls SET status=?, error=?, completed_at=? WHERE dispatch_key=? AND status='submitted'",
                (status, error[:200], time.time(), dispatch_key),
            )
        return True

    def mark_product_research_call_unknown(self, dispatch_key: str, error: str = "delivery_unknown") -> bool:
        cur = self._exec(
            "UPDATE product_research_calls SET status='delivery_unknown', error=?, completed_at=? "
            "WHERE dispatch_key=? AND status='submitted'",
            (error[:200], time.time(), dispatch_key),
        )
        return bool(cur.rowcount)

    def skip_pending_product_research_calls(self, run_id: int, symbol: str, stage: str, error: str) -> int:
        cur = self._exec(
            "UPDATE product_research_calls SET status='skipped', error=?, completed_at=? "
            "WHERE run_id=? AND symbol=? AND stage=? AND status='pending'",
            (error[:200], time.time(), run_id, symbol, stage),
        )
        return int(cur.rowcount or 0)

    def requeue_opening_blocked_product_research_calls(self, run_id: int, symbol: str) -> int:
        """Make never-submitted rebuttal slots available after a repaired opening.

        Only the explicit ``opening_incomplete`` marker is reversible.  Other
        skipped states remain immutable so a resume cannot accidentally replay
        any model work whose dispatch history is unclear.
        """
        cur = self._exec(
            "UPDATE product_research_calls SET status='pending', error='', completed_at=NULL "
            "WHERE run_id=? AND symbol=? AND stage='rebuttal' AND status='skipped' AND error='opening_incomplete'",
            (run_id, symbol),
        )
        return int(cur.rowcount or 0)

    def has_definitive_product_research_failures(self, run_id: int) -> bool:
        rows = self._q(
            """
            SELECT 1
            FROM product_research_calls AS call
            JOIN product_research_items AS item ON item.id = call.item_id
            WHERE call.run_id=?
              AND call.status='failed'
              AND item.status IN ('partial', 'failed')
              AND call.attempt=(
                  SELECT MAX(newer.attempt)
                  FROM product_research_calls AS newer
                  WHERE newer.run_id=call.run_id
                    AND newer.symbol=call.symbol
                    AND newer.stage=call.stage
                    AND newer.trader_id=call.trader_id
              )
            LIMIT 1
            """,
            (run_id,),
        )
        return bool(rows)

    def recover_product_research_submissions(self, run_id: int) -> int:
        """Reconcile durable turns, then quarantine every unresolved send safely."""
        with self._transaction() as c:
            c.execute(
                "UPDATE product_research_calls SET status='complete', completed_at=COALESCE(completed_at, ?) "
                "WHERE run_id=? AND status='submitted' AND EXISTS ("
                "SELECT 1 FROM debate_turns t WHERE t.debate_id=product_research_calls.debate_id "
                "AND t.stage=product_research_calls.stage AND t.trader_id=product_research_calls.trader_id "
                "AND t.status='complete')",
                (time.time(), run_id),
            )
            cur = c.execute(
                "UPDATE product_research_calls SET status='delivery_unknown', error='delivery_unknown', completed_at=? "
                "WHERE run_id=? AND status='submitted'",
                (time.time(), run_id),
            )
        return int(cur.rowcount or 0)

    def refresh_product_research_counts(self, run_id: int) -> Optional[dict]:
        rows = self._q(
            "SELECT status, COUNT(*) AS n FROM product_research_items WHERE run_id=? GROUP BY status", (run_id,),
        )
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        discovered = sum(counts.values())
        eligible = sum(counts.get(status, 0) for status in ("pending", "running", "complete", "partial", "failed"))
        self._exec(
            "UPDATE product_research_runs SET discovered_count=?, eligible_count=?, excluded_count=?, pending_count=?, "
            "running_count=?, complete_count=?, partial_count=?, failed_count=? WHERE id=?",
            (discovered, eligible, counts.get("excluded", 0), counts.get("pending", 0), counts.get("running", 0),
             counts.get("complete", 0), counts.get("partial", 0), counts.get("failed", 0), run_id),
        )
        rows = self._q("SELECT * FROM product_research_runs WHERE id=?", (run_id,))
        return self._product_research_run_row(rows[0]) if rows else None

    # ---- 事件 / KV ----
    def add_event(self, comp_id: Optional[int], level: str, source: str, message: str):
        self._exec("INSERT INTO events(competition_id, ts, level, source, message) VALUES (?,?,?,?,?)",
                   (comp_id, time.time(), level, source, message[:2000]))

    def events(self, comp_id: Optional[int], limit: int = 100) -> List[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM events WHERE competition_id IS ? OR competition_id=? ORDER BY id DESC LIMIT ?", (comp_id, comp_id, limit))]

    @staticmethod
    def _public_event_row(item: dict) -> dict:
        """Redact legacy exception details at the public API boundary."""
        result = dict(item)
        message = str(result.get("message") or "")
        runtime_projection = public_runtime_event_message(message)
        if runtime_projection is not None:
            result["message"] = runtime_projection
        elif message.startswith("连续失败触发熔断，跳过 ") and "：" in message:
            headline = message.split("：", 1)[0]
            category = decision_failure_category(message)
            result["message"] = f"{headline}：{public_decision_failure_message(category)}"
        return result

    def public_events(self, comp_id: Optional[int], limit: int = 100) -> List[dict]:
        """Return event rows without historical provider error details."""
        return [self._public_event_row(item) for item in self.events(comp_id, limit)]

    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str):
        self._exec("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------- 本地聚合的行情 bar（CTP） ----------
    def save_md_bars(self, symbol: str, bars: Iterable, keep: int = 5000, bar_seconds: int = 60,
                     feed: str = "") -> int:
        """写入/更新该品种在某套行情接入下的基础周期 bar，并把这一份历史裁到最近 keep 根。

        用 upsert 而不是"只插新的"：最后一根 bar 在它那个周期内会被反复更新，
        只插新的会把它永久冻结在第一个 tick 的价格上。
        `bar_seconds` 一并进主键：两个周期的 bar 会撞在同一个时间戳上（每根 5m 的起点都是
        某根 1m 的起点），不分开存就会互相覆盖。`feed` 同理，但分的是"哪套行情接入"：换了
        行情前置或改钉了合约之后，同一个品种、同一个周期、同一个时间戳上是**另一条**价格
        序列，混在一起等于把两场实验的 bar 缝成一条。换回旧接入/旧周期时那一份历史仍然完整；
        代价是停用的组合会各自留下最多 `keep` 根，只在写入该组合时才裁剪。
        """
        rows = [(symbol, float(b.ts), float(b.open), float(b.high), float(b.low), float(b.close),
                 float(getattr(b, "volume", 0.0) or 0.0), float(getattr(b, "hold", 0.0) or 0.0),
                 int(bar_seconds), str(feed))
                for b in bars]
        if not rows:
            return 0
        with self._transaction() as conn:
            conn.executemany(
                "INSERT INTO md_bars(symbol, ts, open, high, low, close, volume, hold, bar_seconds, feed) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol, feed, bar_seconds, ts) DO UPDATE SET open=excluded.open, high=excluded.high, "
                "low=excluded.low, close=excluded.close, volume=excluded.volume, hold=excluded.hold", rows)
            # 裁剪同样按周期与接入分开算：按品种一起裁的话，写几根 5m 就能把 1m 的历史削掉大半
            conn.execute(
                "DELETE FROM md_bars WHERE symbol=? AND feed=? AND bar_seconds=? AND ts < "
                "(SELECT MIN(ts) FROM (SELECT ts FROM md_bars WHERE symbol=? AND feed=? AND bar_seconds=? "
                "ORDER BY ts DESC LIMIT ?))",
                (symbol, str(feed), int(bar_seconds), symbol, str(feed), int(bar_seconds), int(keep)))
        return len(rows)

    def copy_md_bars_feed(self, symbol: str, src_feed: str, dst_feed: str, bar_seconds: int) -> int:
        """把该品种某套接入下的基础周期 bar 复制到另一个命名空间，已存在的时间戳不覆盖；返回复制的行数。

        只用于签名算法换版本之后认领同一套接入的历史。旧行保留不删：认领是复制，不是搬家。
        """
        if not src_feed and src_feed == dst_feed:
            return 0
        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO md_bars(symbol, ts, open, high, low, close, volume, hold, bar_seconds, feed) "
                "SELECT symbol, ts, open, high, low, close, volume, hold, bar_seconds, ? FROM md_bars "
                "WHERE symbol=? AND feed=? AND bar_seconds=?",
                (str(dst_feed), symbol, str(src_feed), int(bar_seconds)))
            return int(cur.rowcount or 0)

    def md_bars(self, symbol: str, limit: int = 3000, bar_seconds: Optional[int] = None,
                feed: Optional[str] = None) -> List[dict]:
        """该品种最近的基础周期 bar，旧→新。

        给了 `bar_seconds` 就只返回同周期的行：配置改过之后，旧周期的行拿来当新周期用，
        指标会把每一根旧 bar 按新周期的长度解读，而且不会报任何错。
        `feed` 同理，分的是"哪套行情接入"：换了前置或改钉了合约之后，旧接入累出来的 bar
        拿来给新接入预热，第一批指标算的是另一条价格序列，同样不报任何错。
        两个都不给时返回该品种的全部行，只供诊断与历史用例使用。
        """
        clauses, params = ["symbol=?"], [symbol]
        if bar_seconds is not None:
            clauses.append("bar_seconds=?")
            params.append(int(bar_seconds))
        if feed is not None:
            clauses.append("feed=?")
            params.append(str(feed))
        params.append(int(limit))
        rows = self._q("SELECT ts, open, high, low, close, volume, hold, bar_seconds, feed FROM md_bars "
                       f"WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT ?", params)
        return [dict(r) for r in reversed(rows)]
