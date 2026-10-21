"""竞赛引擎：盯市 tick、决策轮、风控、撮合、落库、恢复、熔断、看门狗。"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime
from pathlib import Path
from typing import AbstractSet, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo

from ..config import AppConfig, TraderCfg
from ..debate import (RESEARCH_SYSTEM_PROMPT, build_market_manifest, has_usable_indicators, opening_prompt, parse_opening,
                      parse_rebuttal, phase_for_open_at, preliminary_summary, rebuttal_prompt, referee_packet)
from ..db import Database, LEGACY_COMPETITION_TIMEZONE, competition_timezone
from ..exchange.contracts import (CLOSED_SESSION_QUOTE_GRACE_SEC, ContractSpecError, contract_terms_digest, crypto_spec,
                                  latest_session_close, legacy_fallback_cn_spec, load_active_cn_contracts,
                                  load_calendar_valid_until, load_cn_contracts)
from ..exchange.sim import SimExchange
from ..market import indicators
from ..market.crypto import CryptoSource
from ..market.ctp import (EXCHANGE_TZ, CtpMarketSource, canonical_contract_id, normalize_instrument_overrides,
                          retention_floor)
from ..market.replay import ReplaySource
from ..market.sina_cn import SinaFuturesSource
from ..models import Account, Bar, ContractSpec, Decision, EquityPoint, Quote, Trade
from ..preopen_universe import build_preopen_universe_research
from ..product_research import ProductResearchService, discover_catalogue
from ..public_errors import (
    PUBLIC_ACTION_EXECUTION_FAILURE,
    PUBLIC_RESET_PERSISTENCE_FAILURE,
    PUBLIC_RUNTIME_EVENT_MESSAGES,
    decision_failure_category,
    public_decision_failure_message,
    public_source_health,
)
from ..research_scan import FullUniverseScanner, SinaResearchReader
from ..traders.base import AccountView, MarketSnapshot, SymbolView, Trader
from ..traders.jev import JevTrader, effective_model as jev_effective_model
from ..traders.llm import LlmTrader, MockTrader
from ..traders.prompt import rules_text
from ..traders.rule import RuleTrader
from . import metrics
from .risk import RiskManager

log = logging.getLogger("arena.engine")

# Decision modes produced by a model rather than by a fallback rule. The dashboard,
# the trader table and the roster-change check all label these seats by their model
# name, so any new model-backed mode must be listed here.
_LIVE_MODES = frozenset({"llm", "jev"})


def _is_live_mode(mode: str) -> bool:
    return mode in _LIVE_MODES


def _seat_model(trader: TraderCfg) -> str:
    """该席位**实际请求**的模型：Jev 没配 model 时发的是 DEFAULT_MODEL，签名与落库都按它记。"""
    return jev_effective_model(trader) if trader.provider == "jev" else trader.model


# 决策本该来自模型的席位类型。它们在阵容比较里**始终**参与：Key 的环境变量没了、Jev 的
# base_url 没了，席位会被静默换成演示策略，不比它就等于让演示策略的动作记进模型的账户。
MODEL_BACKED_PROVIDERS = frozenset({"openai_compatible", "jev"})

# Key under which a competition snapshot records "active universe + contract terms".
# It sits outside the AppConfig fields on purpose: pydantic ignores unknown keys
# when reading a snapshot back, so _same_roster / _archived_context are unaffected.
# 境内期货的两个行情源都适用盘前研究：都有交易所日历（next_open）、报价与 K 线。
# 写成集合而不是逐个 == 比较，是为了让下一个境内源加进来时只改这一处。
DOMESTIC_MARKETS = frozenset({"cn_futures", "ctp"})

# 行情未就绪时推迟盘前研究的下限：距开盘还剩这么多秒时，即使行情仍未就绪也要尝试一次，
# 让"始终没等到行情"留下一条永久记录，而不是整个开盘时刻悄无声息地跳过。
STALE_PREOPEN_RETRY_FLOOR_SEC = 60.0
# 上一节收盘之后还可能到一笔时间戳正落在收盘那一刻（或稍后几秒）的最后成交：核验上一节收盘时
# 容许它比收盘晚这么多秒。比收盘晚得多的报价说明那一节之后又成交过，不是上一节的收盘。
PRIOR_CLOSE_LATE_TICK_SEC = 60.0

# 行情就绪度的三态。"unknown" 是真正的第三态，不能并进另外两个：当成就绪会拿证据不足的
# 快照去调模型，当成故障会把不提供 health()（或不报这些字段）的行情源整条挡在研究之外。
QUOTES_FRESH = "fresh"
QUOTES_UNREADY = "unready"
QUOTES_UNKNOWN = "unknown"

CONTRACT_UNIVERSE_KEY = "contract_universe"
# 竞赛创建时**实际生效**的行情接入签名。`market_data` 配置段本身不足以复现它：钉合约的规范
# 形式与候选合约的命名都取决于合约表里该品种的交易所，而合约表在配置之外、可以原地改。
MARKET_FEED_KEY = "market_feed"
# A retry is a recovery action for the current terminal research failure, not a
# generic way to duplicate a successful or historical paid session.
RETRYABLE_DEBATE_STATUSES = frozenset({"incomplete", "unavailable"})

# 决定"价格序列长什么样"的行情接入与聚合设置，按市场分组。改了它们，K 线、指标以及模型
# 看到的证据全都不同（CTP 落库的 md_bars 还会拿旧设置的 bar 去给新设置预热），两场成绩
# 不能挂在同一张排行榜上。只影响"连不连得上"的设置（超时、流文件目录）不在其中：调它们
# 不该把竞赛掀掉重来。
MARKET_FEED_FIELDS: Dict[str, tuple] = {
    # md_front/broker_id 决定这些行情来自哪台服务器——仿真前置（7x24 重放）与生产前置
    # 给出的是完全不同的价格序列，这是最需要防住的混淆；代价是换备用前置也会另起一场。
    "ctp": ("md_front", "broker_id", "instruments", "auto_months", "bar_seconds",
            "max_bars", "discovery_grace_sec", "dominant_stale_sec", "persist_bars"),
    "crypto": ("provider",),
    # 新浪源只有 contracts_file 与超时：前者的内容已经进了合约条款摘要，后者不改价格。
    "cn_futures": (),
    # replay 有意不在此列。合成/CSV 回放换了 seed 或数据源时，引擎已经有一条专门设计的
    # 通道（确定性快进对不上就"近似衔接"，时间轴不倒退、价格连续）继续同一场竞赛；
    # 把它改成掀掉重来会删掉那条行为，而且那不是这条缺陷说的实盘行情混淆。
}

# 进竞赛身份、但不进落库 bar 的命名空间：改它们不改变"一根已经落库的 bar 表示什么"。
FEED_NAMESPACE_EXCLUDED = frozenset({"max_bars", "persist_bars"})


def preopen_quote_readiness(health: object, max_quote_age_sec: float, *, now: Optional[float] = None) -> str:
    """行情源对"刚刚成功刷新过"这件事的自我报告，三态。

    `unready` 只在源**正面报告**了问题时给出：自报 `ok` 为假，或 `last_ok` 确实超窗。
    两者是同一件事的两种说法——一个刚断线的 CTP 源 `ok` 已经是假，缓存的 `last_ok`
    却还在窗口内；只看时间戳会以为它就绪，于是预约掉这个开盘时刻、再以"证据不足"钉死，
    之后重连也没有第二次机会。
    读不出来（没有 health()、没有这些键、值无法解析或不是有限数）一律是 `unknown`：
    预约前不据此拦截，调用模型前也不据此放行。
    """
    if not isinstance(health, dict):
        return QUOTES_UNKNOWN
    if "ok" in health and not health.get("ok"):
        return QUOTES_UNREADY
    raw = health.get("last_ok")
    if raw is not None:
        # bool 是 int 的子类，float(True) 会悄悄变成 1970 年的时间戳
        last_ok = None if isinstance(raw, bool) else _finite_float(raw)
        if last_ok is None:
            # 源声称记录了刷新时刻却给不出可用的值：既证明不了新鲜，也证明不了陈旧
            return QUOTES_UNKNOWN
        if (time.time() if now is None else now) - last_ok > max_quote_age_sec:
            return QUOTES_UNREADY
    return QUOTES_FRESH if health.get("ok") else QUOTES_UNKNOWN


def _finite_float(raw: object) -> Optional[float]:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _market_feed_row(md: object, market: str, symbols: Iterable[str],
                     specs: Optional[Mapping[str, ContractSpec]], *,
                     canonical_pins: bool, with_exchanges: bool) -> Dict[str, object]:
    """签名的一种"算法版本"。当前版本见 `market_feed_signature`；旧版本只用来找回升级前落库的 bar。"""
    section = getattr(md, market, None)
    fields = MARKET_FEED_FIELDS.get(market) or ()
    if section is None or not fields:
        return {}
    active = list(symbols)
    exchanges = {s: (getattr((specs or {}).get(s), "exchange", "") or "") for s in active}
    row: Dict[str, object] = {}
    for name in fields:
        value = getattr(section, name, None)
        if isinstance(value, dict):
            # 按源实际匹配的形式归一化再筛，不能拿配置里的原始键去比：源把 `instruments`
            # 的键大写之后才用，写成 `rb0` 同样钉死了 RB0，而原始键筛不中它——于是改动
            # 前后两份签名都是空映射，竞赛照旧沿用，价格序列却已经换了一条。
            value = normalize_instrument_overrides(value) if name == "instruments" else dict(value)
            value = {s: value[s] for s in active if s in value}
            if name == "instruments" and canonical_pins:
                value = {s: canonical_contract_id(v, exchanges.get(s, "")) for s, v in value.items()}
        row[name] = value
    if market == "ctp":
        row["max_bars"] = max(2, int(section.max_bars), retention_floor(int(section.bar_seconds)))
        if with_exchanges:
            row["exchanges"] = exchanges
    return row


def market_feed_signature(md: object, market: str, symbols: Iterable[str],
                          specs: Optional[Mapping[str, ContractSpec]] = None) -> Dict[str, object]:
    """本场竞赛**实际生效**的行情接入与聚合设置。

    按品种索引的映射只取当前在跑的那些键（"生效"，而不是"配置里写了"），CTP 的 max_bars
    换算成源真正会用的值——源会按 bar_seconds 反比把配小的值抬上去，两个不同的配置值抬到
    同一处时仍是同一场实验。钉死的合约同理：源订阅的是按交易所规范化之后的代码，`RB2601`
    与 `rb2601` 是同一条行情，签名也必须按规范形式算——否则把配置改写成规范写法就会掀掉
    竞赛、换掉落库 bar 的命名空间，白白丢掉几个交易日的指标历史。

    `specs` 给出每个品种在合约表里的交易所。CTP 源靠它枚举候选合约、决定年份位数和大小写，
    所以合约表里把某个品种的交易所改正之后，同一份 `market_data` 配置对应的已经是另一条行情：
    交易所进签名（`exchanges`），也进每个品种落库 bar 的命名空间。`contract_terms_digest()`
    仍把交易所当展示字段，因为新浪源不靠它取数。给不出 `specs` 时（只有测试会这样）钉合约
    只去空白、不改大小写，也不带交易所。
    """
    known = specs is not None
    return _market_feed_row(md, market, symbols, specs, canonical_pins=known, with_exchanges=known)


def legacy_market_feed_namespaces(md: object, market: str, symbol: str,
                                  specs: Optional[Mapping[str, ContractSpec]] = None) -> List[str]:
    """该品种在**以前的签名算法**下会落到的命名空间，用于升级后找回已经落库的 bar。

    两个旧版本：钉合约已规范化、不带交易所（`bb81876`）；钉合约按原始拼写、不带交易所
    （`bb81876` 之前）。它们描述的和现在是同一套接入——前置、钉的合约、聚合周期都相同，
    只是哈希的写法变了——所以把那一份历史认领过来不会把另一条价格序列喂进指标。

    **按由新到旧排列**：一个装机可能跨过两个旧版本各攒下一段历史（老的一段在原始拼写的
    命名空间里，新的一段在规范化的命名空间里），认领时要全部并进来，同一时间戳以新的一段为准。
    """
    current = market_feed_namespace(md, market, symbol, specs)
    out: List[str] = []
    for canonical_pins in (True, False):
        row = {k: v for k, v in _market_feed_row(md, market, [symbol], specs, canonical_pins=canonical_pins,
                                                 with_exchanges=False).items()
               if k not in FEED_NAMESPACE_EXCLUDED}
        if not row:
            continue
        digest = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        if digest != current and digest not in out:
            out.append(digest)
    return out


def market_feed_namespace(md: object, market: str, symbol: str,
                          specs: Optional[Mapping[str, ContractSpec]] = None) -> str:
    """**这个品种**的落库 bar 属于哪套行情接入：同一个命名空间里的 bar 才是同一条价格序列。

    竞赛身份换了一张排行榜，但 `md_bars` 只按 (品种, 基础周期, 时间戳) 存：换了行情前置或
    改钉了合约之后，新竞赛的第一根指标仍然是用旧前置/旧合约累出来的 bar 预热的，之后两段
    历史还会被当成一条继续写下去——排行榜重开了，实验其实没有隔离。

    按品种算而不是按整份配置算：往 `symbols` 里加一个品种，不改变 RB0 的 bar 是怎么来的，
    但会让整份配置的 `instruments` 子集变掉——拿它当指纹的话，加一个合约就会把**所有**品种
    已经攒下的历史一次性弃掉，而且不报错。`max_bars` 与 `persist_bars` 同样不进来：前者只决定
    留多深，后者只决定写不写，都不改变"一根已经落库的 bar 表示什么"。
    """
    row = {k: v for k, v in market_feed_signature(md, market, [symbol], specs).items()
           if k not in FEED_NAMESPACE_EXCLUDED}
    if not row:
        return ""
    blob = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def contract_universe_signature(symbols: Iterable[str], specs: Dict[str, ContractSpec]) -> Dict[str, object]:
    """Identity of the contract universe a competition runs under.

    Order is preserved rather than sorted: the configured order is what the
    market source, the decision snapshot and the model prompt iterate in, so a
    reordering is a different experiment even though the set is unchanged.
    """
    return {"symbols": [str(s) for s in symbols], "terms": contract_terms_digest(specs)}


class ResetBlocked(RuntimeError):
    """重置被拒绝：旧竞赛仍有触发成交未落库，补做成功后才能归档。"""


class RolloverBlocked(RuntimeError):
    """启动时按新配置切换竞赛被拒绝：旧竞赛仍有触发成交未落库，补做成功后才能归档。"""


class _SpecMap(dict):
    """当前配置的合约规格表。持仓里可能出现配置之外的品种（已从配置移除、但触发成交待落库而推迟对齐的持仓）：
    按键取值时退回引擎的 _spec_for（合约表全集 / 默认规格），估值、看板与账户视图不会因 KeyError 整体失败；
    `in` 判断仍只认配置内的品种，配置对齐照旧识别"已移除"。"""

    def __init__(self, engine: "Engine", base: Dict[str, ContractSpec]):
        super().__init__(base)
        self._engine = engine

    def __missing__(self, sym: str) -> ContractSpec:
        return self._engine._spec_for(sym)


class Engine:
    def __init__(self, cfg: AppConfig, db: Database, accelerate: bool = False):
        self.cfg = cfg
        self.db = db
        self.accelerate = accelerate
        self.tz = ZoneInfo(cfg.competition.timezone)
        self.currency = "USDT" if cfg.competition.market == "crypto" else "CNY"
        self.symbols = list(cfg.symbols)
        self._stop = threading.Event()   # 停止信号：主循环、决策等待、行情请求与 LLM 重试循环都会检查
        self.persist_lock_timeout = 20.0
        self.stale_quote_sec = 300.0     # 行情超过此秒数未更新则不发起决策
        self.max_future_quote_skew_sec = 60.0
        self.max_future_bar_skew_sec = 60.0
        self._stale_logged = False
        self.calendar_valid_until: Optional[date] = None
        self._calendar_warned_day: Optional[date] = None
        self._all_specs: Dict[str, ContractSpec] = {}   # 合约表全集，用于处理配置中已移除但仍有持仓的品种
        self.source, specs, self.holidays = self._build_source()
        # 行情源在**提交竞赛切换之前**启动：`_load_or_create_competition()` 会归档旧竞赛、
        # 新建一条（换市场/换起始资金/换阵容时），那是落库的、不可回退的改动。放在它之后启动
        # 的话，凭证错或前置不可达会让旧实验先被归档、再失败退出，正常启动就再也接不回去了。
        # 代价是启动之后的每一步失败都得负责关掉它——下面这个 try 就是干这个的：构造抛出去时
        # 这个 Engine 永远不会交到调用方手里，谁也不会再调 source.close()。
        try:
            self.specs: Dict[str, ContractSpec] = _SpecMap(self, specs)
            # The pre-open full-universe reader is deliberately separate from the
            # execution source.  It is lazy because replay/crypto never use it and
            # it is not closed during stop: a bounded scan can still have a public
            # HTTP worker unwinding after its cancellation signal.
            self._preopen_universe_reader: Optional[SinaResearchReader] = None
            self._preopen_universe_reader_lock = threading.Lock()
            if hasattr(self.source, "stop_event"):
                self.source.stop_event = self._stop
            self._generation = 0   # 每次 reset 递增；用于丢弃重置前仍在飞行中的决策
            self._decision_epoch = 0  # Changes when strategy decisions must be invalidated, including pause.
            r = cfg.risk
            self.exchange = SimExchange(self.specs, slippage_ticks=r.slippage_ticks, min_hold_sec=0 if accelerate else r.min_hold_sec,
                                        liquidation_risk_ratio=r.liquidation_risk_ratio, maintenance_margin_ratio=r.maintenance_margin_ratio,
                                        max_margin_risk_ratio=r.max_margin_risk_ratio,
                                        target_margin_risk_max_ratio=r.target_margin_risk_max_ratio)
            self.risk = RiskManager(r, self.specs)
            self.traders: Dict[str, Trader] = {t.id: self._build_trader(t) for t in cfg.traders}
            self.trader_cfgs = {t.id: t for t in cfg.traders}
            self.initial_cash = float(cfg.competition.initial_cash)   # 以进行中竞赛的起始资金为准（加载后可能与配置不同）
            self.rules = ""

            self.lock = threading.RLock()
            # Coordinates only the narrow, billable research submission boundary.
            # It must never be held while waiting for the broader engine lock.
            self._research_submit_lock = threading.Lock()
            self.accounts: Dict[str, Account] = {}
            self.prices: Dict[str, float] = {}
            self.quotes: Dict[str, Quote] = {}
            self._replay_bars: Dict[str, List[Bar]] = {}
            self._replay_retry: Optional[tuple] = None   # 未全部结算的回放 bar（quotes, bars）：下个 tick 重放而不是前进
            # 重启恢复的预写日志记录的回放位置（关闭时保留的那根 bar 之后）：日志全部结算后才写入 kv，期间回放停住不前进、
            # 也不重放那根 bar——重放会按现在的风控参数/合约表重新评估当时已评估过且无成交的 bar
            self._journal_replay_pos: Optional[dict] = None
            self.shutdown_errors: List[str] = []   # 停止时没能持久化的内容（预写日志与数据库都写不进去），供外壳/日志汇报
            # 所有账户都已结算完当前回放 bar（或刚重置），但回放位置写不进数据库：不取下一根 bar、不开新一轮，每个 tick 与
            # 关闭时重试写入，直到成功。否则持久化的位置落后于账户状态，重启后已按后续行情记录的成交/持仓会跑到行情前面
            self._cursor_pending = False
            # 触发了止损/止盈/强平但落库失败的账户：保留当时的价格与时钟（_pending_marks），下个 tick 先按原价重试，
            # 结算前不执行该账户的任何决策（否则触发条件可能被改掉、触发永久丢失）。实时源与回放源都适用。
            self._unsettled: set = set()
            self._pending_marks: Dict[str, tuple] = {}   # tid -> (prices, clock, clocks)
            # tid -> 触发时生效的撮合参数（滑点、强平阈值）与所涉品种的合约规格快照：重试（含重启后、切换竞赛前）必须按当时的
            # 条款补做，而不是按重启后可能已改变的风控参数或已被编辑的合约表——否则记录的成交/盈亏与当时触发的不一致，
            # 甚至按新条款不再触发、日志被当作"已不成立"清掉
            self._pending_terms: Dict[str, dict] = {}
            # 品种 → 上次落库写到的最后一根行情 bar 的 ts（那一根当时可能还没收口，所以下次从它开始重写）。
            # 只写变过的那几根，否则暖起来之后每次快照都要重写整条序列（见 _persist_md_bars）。
            self._md_persisted: Dict[str, float] = {}
            self._journal_version = 0.0                   # 预写日志版本号（时间戳，单调递增）
            # 触发已结算落库、但磁盘上仍残留其旧的预写日志副本且墓碑也写不进去的账户：在日志清理成功之前保持封锁
            # （不执行其决策），否则崩溃重启会把旧触发价套到它之后新开的持仓上
            self._journal_stale: set = set()
            self._deferred_reconcile: set = set()   # 启动时因触发成交待落库而推迟配置对齐的账户：结算成功后再对齐
            # 无成交盯市更新（最新价/权益峰值）落库失败的账户 -> 失败那次算出的权益峰值：重试时把它带上，即使期间在飞的决策
            # 已把持仓平掉（重放再也算不出那个更高的权益），高水位也不会丢；成功写入后清除。只在首次失败记一次日志
            self._mark_save_failed: Dict[str, float] = {}
            # 关闭前仍写不进去的盯市更新（各持仓最新价 + 权益峰值），记在预写日志里（kind=mark）：重启后直接写入，
            # 不经过撮合器——按现在的风控参数/合约表重新盯市可能凭空产生当时并不存在的强平。写入成功或被更新的盯市取代后清除
            self._pending_mark_updates: Dict[str, dict] = {}
            self._account_versions: Dict[str, int] = {}
            self.comp: dict = {}
            self.round_no = 0
            self.paused = False
            self.running = False
            self._thread: Optional[threading.Thread] = None
            self._watchdog: Optional[threading.Thread] = None
            self._round_thread: Optional[threading.Thread] = None   # 决策轮独立线程：慢模型不能阻塞盯市
            self._inflight: Dict[str, int] = {}  # tid -> round_no：在途的模型决策。慢交易员只占用自己的槽位、
            # 本轮跳过；其他交易员照常开新一轮（流水线），不再因上一轮未结束推迟整轮
            self._debate_thread: Optional[threading.Thread] = None  # Account-free research debate; never submits orders.
            self._last_tick = 0.0
            self._last_decision = 0.0
            self._last_snapshot = 0.0
            self._last_round_ms = 0
            self._breaker: Dict[str, dict] = {tid: {"fails": 0, "skip": 0, "status": "ok", "last_error": ""} for tid in self.traders}
            self._metrics: Dict[str, dict] = {}
            self._stats: Dict[str, dict] = {}       # trader -> 运行中的峰值/回撤/小时桶/成交统计缓存
            self._day_start: Dict[str, dict] = {}   # trader -> {day, equity, trades, fees}
            self._events_cache: List[dict] = []
            self.status_text = "初始化"
            # 数据版本号：决策结算、盯市成交、事件写入、暂停/重置后递增；看板据此刷新决策与事件列表
            # （轮次号在决策轮开始时就变化，不能代表『结果已落库』）。以启动时间为起点，重启后不会与旧值重合。
            self._version = int(time.time())
            self._version_lock = threading.Lock()
            self.pool = self._new_pool()
            self._load_or_create_competition()
            self.product_research = ProductResearchService(
                self.db,
                comp_id=lambda: self.comp_id,
                cfg=self.cfg.product_research,
                catalogue_dir=self.cfg.resolve("data/product-research/catalogues"),
                calendar_path=self.cfg.resolve(self.cfg.market_data.cn_futures.contracts_file),
                participants=self._product_research_participants,
                call_model=self._call_product_research_model,
                market_timeout_sec=float(self.cfg.market_data.cn_futures.timeout_sec),
                can_start=lambda: (self.running and not self._stop.is_set()
                                   and self.cfg.competition.market in DOMESTIC_MARKETS),
                submit_guard=lambda: self._research_submit_lock,
                on_change=self._bump_version,
            )
        except BaseException:   # 含 Ctrl-C：构造没走完就不能留下一个活着的行情源
            close = getattr(self.source, 'close', None)
            if callable(close):
                try:
                    close()
                except Exception as e:  # noqa: BLE001
                    log.warning('构造失败后关闭行情源失败（已忽略）：%s', e)
            raise

    def _new_pool(self) -> ThreadPoolExecutor:
        # 两倍于交易员数：暂停后恢复时，上一轮仍未返回的模型调用不会挤占新一轮的线程
        return ThreadPoolExecutor(max_workers=max(8, 2 * len(self.traders)), thread_name_prefix="arena-decide")

    def _decision_round_timeout_sec(self, trader_ids: Iterable[str]) -> float:
        """Budget a round for the slowest submitted worker, not the global default.

        Workers are submitted in parallel, so their request budgets must be
        maximized rather than summed.  Retain the existing scheduler/persistence
        reserve after accounting for every configured outer retry and backoff.
        """
        global_budget = (
            self.cfg.llm.timeout_sec * (self.cfg.llm.max_retries + 1)
            + LlmTrader.retry_backoff_seconds(self.cfg.llm.max_retries)
        )
        budgets = [global_budget]
        for tid in trader_ids:
            # 按能力判断而不是按类型：下一个席位只要实现了这个方法就自动算进预算，
            # 不必再记得回来改这里——Jev 席位当初就是这么漏掉的。
            budget = getattr(self.traders.get(tid), "decision_timeout_budget_sec", None)
            if callable(budget):
                budgets.append(budget())
        return max(budgets) + 15.0

    def _build_rules(self) -> str:
        c = self.cfg.competition
        return rules_text(self.cfg.risk, c.market, self.currency, self.initial_cash, c.decision_interval_sec)

    # ---------- 构建 ----------
    def _build_source(self):
        m = self.cfg.competition.market
        md = self.cfg.market_data
        if m == "crypto":
            src = CryptoSource(self.symbols, provider=md.crypto.provider, timeout=md.crypto.timeout_sec, max_leverage=max(1.0, self.cfg.risk.max_leverage))
            return src, src.specs(), []
        contracts_file = self.cfg.resolve(md.cn_futures.contracts_file)
        # Fail closed before the source, the exchange, the risk manager, any model
        # call or any write: an active row that omits a term used to be filled in
        # with a generic default, which traded one product under another's
        # multiplier, tick, margin, fee and sessions.  The CLI runs the same check
        # earlier still; this one covers direct construction.
        specs, holidays = load_active_cn_contracts(self.symbols, contracts_file)
        self.calendar_valid_until = load_calendar_valid_until(contracts_file)
        self._all_specs = dict(specs)
        specs = {s: specs[s] for s in self.symbols}
        if m == "ctp":
            # CTP 是订阅推送：登录后一次性订阅全部候选合约，之后不再有轮询请求，
            # 品种数不会像 HTTP 行情那样把请求量线性放大
            c = md.ctp
            src = CtpMarketSource(self.symbols, specs, holidays, md_front=c.md_front, broker_id=c.broker_id,
                                  user_id=c.resolved_user_id(), password=c.resolved_password(),
                                  instruments=c.instruments, auto_months=c.auto_months,
                                  flow_dir=str(self.cfg.resolve(c.flow_dir)),
                                  connect_timeout_sec=c.connect_timeout_sec, bar_seconds=c.bar_seconds,
                                  max_bars=c.max_bars, tz=EXCHANGE_TZ,
                                  discovery_grace_sec=c.discovery_grace_sec,
                                  dominant_stale_sec=c.dominant_stale_sec)
            # 就地启动：必须早于 `_load_or_create_competition()` 提交竞赛切换（见 __init__）
            src.start()
            return src, specs, holidays
        if m == "replay":
            src = ReplaySource(self.symbols, specs, seed=md.replay.seed, bar_seconds=md.replay.bar_seconds,
                               start_price=md.replay.start_price, csv_dir=(str(self.cfg.resolve(md.replay.csv_dir)) if md.replay.csv_dir else None),
                               history=md.replay.history, tz=self.cfg.competition.timezone)
            return src, specs, holidays
        src = SinaFuturesSource(self.symbols, specs, holidays, timeout=md.cn_futures.timeout_sec)
        return src, specs, holidays

    def _build_trader(self, t) -> Trader:
        if t.provider == "mock":
            return MockTrader(t.id, t.name, t.mock_response)
        if t.provider == "openai_compatible" and t.is_live_llm:
            return LlmTrader(t, self.cfg.llm, stop_event=self._stop)
        if t.provider == "openai_compatible":
            log.warning("交易员 %s 未配置 API Key，降级为演示策略 %s", t.id, t.fallback_rule)
        if t.is_jev:
            return JevTrader(t, self.cfg.llm, stop_event=self._stop)
        if t.provider == "jev":
            log.warning("交易员 %s 未配置 Jev 端点（base_url），降级为演示策略 %s", t.id, t.fallback_rule)
        return RuleTrader(t.id, t.name, t.fallback_rule)

    def _config_snapshot_json(self) -> str:
        """Serialize reproducibility settings without any credential-bearing fields.

        每场新竞赛都会把这份快照写进 `competitions.config_json`，所以任何漏掉的凭证字段都会
        在数据库里留下副本——即使之后把 config.yaml 改回环境变量，旧竞赛里的那份也还在。
        CTP 的密码字段（以及被误填进 `password_env` 的明文）与交易员 Key 同样必须排除。
        `user_id` 是资金账号，本身也不该跟着竞赛存档到处走。
        """
        payload = json.loads(self.cfg.model_dump_json(
            exclude={
                "traders": {"__all__": {"api_key", "api_key_env"}},
                "market_data": {"ctp": {"password", "password_env", "user_id", "user_id_env"}},
            }
        ))
        # The contract terms live in a file outside the configuration, so recording
        # the path alone cannot reproduce a run: an in-place edit leaves the path
        # identical.  Store the active universe and a digest of the terms so
        # competition identity can depend on both.
        payload[CONTRACT_UNIVERSE_KEY] = self._contract_universe_signature()
        # 行情接入签名也记下**生效**的那份：它依赖合约表里的交易所，而合约表不在配置里。
        payload[MARKET_FEED_KEY] = self._market_feed_signature(self.cfg.market_data)
        return json.dumps(payload, ensure_ascii=False)

    def _contract_universe_signature(self) -> Dict[str, object]:
        """Identity of the universe and terms actually in force for this competition."""
        return contract_universe_signature(self.symbols, self.specs)

    @staticmethod
    def _recorded_market_feed(comp: dict) -> Optional[dict]:
        """The effective market-feed signature a competition stored at creation, or None."""
        try:
            snapshot = json.loads(comp.get("config_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(snapshot, dict):
            return None
        recorded = snapshot.get(MARKET_FEED_KEY)
        return recorded if isinstance(recorded, dict) else None

    @staticmethod
    def _recorded_contract_universe(comp: dict) -> Optional[dict]:
        """The identity block a competition stored, or None if it never recorded one."""
        try:
            snapshot = json.loads(comp.get("config_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(snapshot, dict):
            return None
        recorded = snapshot.get(CONTRACT_UNIVERSE_KEY)
        return recorded if isinstance(recorded, dict) else None

    def _same_contract_universe(self, comp: dict) -> bool:
        """Whether this competition demonstrably runs the same universe and terms.

        A different symbol list -- including a different order, which the source,
        the decision snapshot and the model prompt all iterate in -- or different
        terms (multiplier, tick, margin, fees, sessions, lot step) change what the
        recorded results mean, so they must not share a leaderboard.

        A snapshot that never recorded an identity block cannot demonstrate
        anything, so it is treated as different rather than assumed equal:
        continuing it would put results whose universe and terms are unknown on
        the same leaderboard as results produced under a known one.  The rollover
        is controlled -- pending marks are replayed and the old competition is
        archived intact before the new one starts.
        """
        recorded = self._recorded_contract_universe(comp)
        return recorded is not None and recorded == self._contract_universe_signature()

    @staticmethod
    def _configured_roster_signature(cfg: AppConfig) -> Dict[str, tuple]:
        rows = {}

        def effective_retries(trader) -> int:
            """记**生效**的重试次数，不是配置里那个可能为 None 的字段。

            席位没配时回落到全局 `llm.max_retries`：把全局从 0 改成 3，这名选手实际会多发
            三次付费请求、同一轮可能拿到不同决策，而签名里记的还是那个没变过的 None。
            """
            return trader.max_retries if trader.max_retries is not None else cfg.llm.max_retries

        for trader in cfg.traders:
            if trader.provider == "openai_compatible":
                # max_retries 同样改变这名选手的行为（重试次数不同，同一轮可能拿到不同决策），
                # 漏掉它会让改动后的选手在同一张排行榜下继续跑，与其他模型参数的处理不一致
                rows[trader.id] = (trader.provider, trader.base_url.rstrip("/"), trader.model, trader.api_style,
                                   trader.fallback_rule, trader.max_tokens, trader.max_completion_tokens, trader.reasoning_effort,
                                   trader.temperature, effective_retries(trader))
            elif trader.provider == "mock":
                rows[trader.id] = (trader.provider, trader.mock_response)
            elif trader.provider == "jev":
                # max_retries 与文本席位同理：重试次数不同，同一轮可能拿到不同决策，
                # 也会发出不同数量的付费请求——改了它就该另起一张排行榜。
                rows[trader.id] = (trader.provider, trader.base_url.rstrip("/"), jev_effective_model(trader),
                                   trader.jev_min_confidence, trader.fallback_rule, effective_retries(trader))
            else:
                rows[trader.id] = (trader.provider, trader.fallback_rule)
        return rows

    @staticmethod
    def _product_research_resume_signature(cfg: AppConfig, participant_ids: set[str]) -> Dict[str, tuple]:
        """Semantic model settings for a durable research recovery.

        Credentials and a provider base URL are deliberately absent: replacing
        an expired key or repairing a compatible routing endpoint must not
        orphan an already-paid research queue.  A model, protocol, sampling, or
        retry-policy change still changes the analysis and is rejected.
        """
        rows: Dict[str, tuple] = {}
        for trader in cfg.traders:
            if trader.id not in participant_ids:
                continue
            rows[trader.id] = (
                trader.provider,
                trader.model,
                trader.api_style,
                trader.max_tokens,
                trader.max_completion_tokens,
                trader.reasoning_effort,
                trader.temperature,
                trader.max_retries,
            )
        return rows

    def _product_research_resume_allowed(self, run_id: int) -> tuple[bool, str]:
        """Permit only routing/credential repairs to resume an old analysis run."""
        run = self.db.product_research_run_by_id(int(run_id))
        if not run:
            return False, "未找到全品种研究运行"
        participant_ids = set(self.db.product_research_participant_ids(int(run_id)))
        current_ids = {cfg.id for cfg in self.cfg.traders}
        if not participant_ids or participant_ids != current_ids:
            return False, "研究运行的四模型名单已变更，不能把不同模型混入原运行"
        competition = self.db.competition(int(run["competition_id"]))
        if not competition:
            return False, "研究运行的归属竞赛记录缺失，不能安全续跑"
        try:
            recorded_cfg = AppConfig.model_validate(json.loads(competition.get("config_json") or "{}"))
        except Exception:  # noqa: BLE001 - never fall back when model identity is unknown
            return False, "研究运行缺少可核验的模型配置快照，不能安全续跑"
        if (self._product_research_resume_signature(recorded_cfg, participant_ids)
                != self._product_research_resume_signature(self.cfg, participant_ids)):
            return False, "模型或推理参数已变更；请新建研究运行，不能混入原运行"
        return True, ""

    def _same_roster(self, comp: dict) -> bool:
        """Keep legacy and rule-only test rosters resumable, but never mix changed or degraded model contestants.

        每个**配置为模型席位**的交易员都参与比较，不只是此刻真的连上模型的那些：重启时 Key 的
        环境变量丢了、Jev 的 `base_url` 被删了，`_build_trader()` 会把席位换成演示策略，而这个
        席位如果因此被过滤掉，竞赛照旧续跑，演示策略的动作就混进了模型的排行榜账户。运行态
        比较（`traders` 表里记的 provider/model）正是为了发现这种降级。
        """
        try:
            snapshot = json.loads(comp.get("config_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("traders"), list):
            return True
        try:
            recorded_cfg = AppConfig.model_validate(snapshot)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot read roster from competition #%s configuration snapshot: %s", comp.get("id"), exc)
            return True
        recorded_signature = self._configured_roster_signature(recorded_cfg)
        current_signature = self._configured_roster_signature(self.cfg)
        # 记录里或当前配置里**任一方**是模型席位的 ID 都要比：保留 ID 把 openai_compatible/jev 改成
        # rule 或 mock 时，只看当前 provider 会把这个席位漏掉，演示策略就接着用前身模型的账户和历史。
        # 已从配置里移除的 ID 不在此列（移除席位仍可续跑，见 test_regression59）。
        current_ids = {trader.id for trader in self.cfg.traders}
        model_seat_ids = {
            trader.id for trader in list(self.cfg.traders) + list(recorded_cfg.traders)
            if trader.provider in MODEL_BACKED_PROVIDERS and trader.id in current_ids
        }
        if any(recorded_signature.get(tid) != current_signature[tid] for tid in model_seat_ids):
            return False

        recorded_runtime = self.db.traders(comp["id"])
        if not recorded_runtime:
            return True
        current_runtime = {}
        for cfg in self.cfg.traders:
            if cfg.id not in model_seat_ids:
                continue
            trader = self.traders[cfg.id]
            model = _seat_model(cfg) if _is_live_mode(trader.mode) else f"rule:{cfg.fallback_rule}"
            current_runtime[cfg.id] = (trader.mode, model)
        old_runtime = {row["trader_id"]: (row["provider"], row["model"]) for row in recorded_runtime}
        return all(old_runtime.get(tid) == signature for tid, signature in current_runtime.items())

    def _market_feed_signature(self, md: object) -> Dict[str, object]:
        """This competition's effective, non-secret feed and aggregation settings."""
        return market_feed_signature(md, self.cfg.competition.market, self.symbols, self._all_specs)

    def _same_market_feed(self, comp: dict) -> bool:
        """行情接入与聚合设置没变，这场竞赛才能接着跑。

        把 RB0 从一个合约换钉到另一个、或者改 bar_seconds，价格序列与指标就都变了，而
        `_load_or_create_competition()` 看到的市场、合约宇宙、选手名单却一模一样，于是旧排行榜
        继续累加——两场性质不同的实验的成绩被混在一起。CTP 尤其明显：落库的 md_bars 还会拿旧
        设置下累出来的 bar 去预热新设置。凭证字段不参与比较（快照里本来就没存）。
        """
        try:
            snapshot = json.loads(comp.get("config_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(snapshot, dict):
            return False
        recorded_feed = snapshot.get(MARKET_FEED_KEY)
        recorded_md = snapshot.get("market_data")
        if isinstance(recorded_feed, dict):
            # 创建时记下的生效签名（含各品种的交易所）也要相等：合约表里改正了某个品种的交易所，
            # 从存档的 `market_data` 配置重新算是看不出来的——两边都会用改正后的表。这一条是
            # 附加条件，不取代下面按存档配置重算的比较。
            current = json.loads(json.dumps(self._market_feed_signature(self.cfg.market_data), ensure_ascii=False))
            if recorded_feed != current:
                return False
        elif self.cfg.competition.market == "ctp" and recorded_md is not None:
            # 升级前创建的 CTP 竞赛：存了配置段却没存生效签名。此时若合约表里的交易所恰好在这次
            # 升级里被改正，下面按存档配置重算两边都会用改正后的表，看不出区别，两条行情的成绩
            # 就混进一张排行榜。拿不出证据就不续跑，与合约宇宙身份缺失时的处理一致。
            return False
        if recorded_md is None:
            # 早于这项记录的快照（以及测试里直接写的空 config_json）根本没有行情段：
            # 拿不出证据就不据此掀掉竞赛，与 _same_roster 对旧快照的处理一致。
            return True
        if not isinstance(recorded_md, dict):
            return False
        try:
            # 与风控那条同理：模型校验会给旧快照补上后加字段的默认值，也就是旧行为。
            recorded = AppConfig.model_validate({"market_data": recorded_md}).market_data
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot read market feed settings from competition #%s configuration snapshot: %s",
                        comp.get("id"), exc)
            return False
        return self._market_feed_signature(recorded) == self._market_feed_signature(self.cfg.market_data)

    def _same_margin_admission_cap(self, comp: dict) -> bool:
        """Keep the configured risk-increasing-order policy stable.

        The target band and ``max_margin_risk_ratio`` determine which opening
        and adding orders may be accepted, so changing either starts a new
        competition.  Warning, maintenance, and forced-liquidation values keep
        their existing restart and recovery semantics.  In particular,
        historical tests and the write-ahead journal permit an operator to
        tighten a liquidation setting on restart while completing an in-flight
        trigger under its journaled terms.
        """
        try:
            snapshot = json.loads(comp.get("config_json") or "{}")
            recorded_risk = (snapshot or {}).get("risk")
            if recorded_risk is None:
                # Snapshots predating RiskCfg are equivalent to the legacy
                # no-admission-cap behavior, rather than an unconstrained
                # version of the newly configured contest rule.
                return self.cfg.risk.max_margin_risk_ratio is None
            if not isinstance(recorded_risk, dict):
                return False
            # Model validation supplies defaults for legacy snapshots that predate
            # a newly introduced field, preserving their documented old semantics.
            recorded = AppConfig.model_validate({"risk": recorded_risk}).risk
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot read risk terms from competition #%s configuration snapshot: %s", comp.get("id"), exc)
            return False
        return (
            recorded.target_margin_risk_min_ratio == self.cfg.risk.target_margin_risk_min_ratio
            and recorded.target_margin_risk_max_ratio == self.cfg.risk.target_margin_risk_max_ratio
            and recorded.max_margin_risk_ratio == self.cfg.risk.max_margin_risk_ratio
        )

    def _load_or_create_competition(self):
        comp = self.db.active_competition()
        c = self.cfg.competition
        same_market = bool(comp and comp["market"] == c.market)
        same_initial_cash = bool(comp and abs(float(comp["initial_cash"]) - float(c.initial_cash)) <= 1e-6)
        # A legacy snapshot without an explicit timezone was created under the historical default.
        # Treating it as any timezone would relabel existing trades and daily summaries on restart.
        recorded_tz = competition_timezone(comp) if comp else None
        old_tz = recorded_tz or LEGACY_COMPETITION_TIMEZONE
        same_tz = bool(comp and old_tz == c.timezone)
        same_roster = bool(comp and self._same_roster(comp))
        same_margin_admission_cap = bool(comp and self._same_margin_admission_cap(comp))
        same_contract_universe = bool(comp and self._same_contract_universe(comp))
        same_market_feed = bool(comp and self._same_market_feed(comp))
        if (comp and same_market and same_initial_cash and same_tz and same_roster
                and same_margin_admission_cap and same_contract_universe and same_market_feed):
            self.comp = comp
            self.round_no = comp["round_no"]
            self.initial_cash = float(comp.get("initial_cash") or c.initial_cash)
            self.accounts = self.db.load_accounts(comp["id"])
            log.info("恢复竞赛 #%s 第 %d 轮，%d 个账户", comp["id"], self.round_no, len(self.accounts))
            self._restore_pending_marks(comp["id"])
            # 日志带回放位置（关闭时保留的 bar 之后）时按它恢复，而不是 kv 里落后一根的位置：那根 bar 已在所有账户上评估过
            self._restore_replay_position(comp["id"], override=self._journal_replay_pos)
            self._apply_pending_mark_updates()   # 关闭前没写进去的盯市更新：先直接写入（不经过撮合器）
            # 再按原触发价补做恢复的触发成交，然后做配置对齐：对齐会删掉已移除交易员的账户、按最后价平掉已移除品种的持仓，
            # 之后重试就找不到持仓，原触发价的成交会被替换或永久丢失
            for tid in list(self._pending_marks):
                self._mark_and_settle(tid, *self._pending_marks[tid], retry=True)
            self._release_journal_replay_pos()   # 日志全部结算：回放位置越过关闭时保留的 bar
            if not self._pending_marks and not self._pending_mark_updates and self._journal_replay_pos is None:
                self._write_journal()   # 残留的旧副本/墓碑（含比 kv 旧的位置）：清掉
        else:
            if comp:
                # 归档前先补做旧竞赛尚未落库的触发成交（补不了则拒绝切换）：启动只恢复进行中竞赛的预写日志
                self._settle_archived_journal(comp)
                # ``reason`` is dashboard copy and stays Chinese; ``log_message`` is
                # the internal log line.  Branches that predate this split log their
                # event text unchanged.
                log_message = ""
                if not same_market:
                    reason = f"市场从 {comp['market']} 变更为 {c.market}，结束竞赛 #{comp['id']} 并新建竞赛"
                elif not same_initial_cash:
                    reason = (f"起始资金从 {float(comp['initial_cash']):,.0f} 变更为 {c.initial_cash:,.0f}，"
                              f"结束竞赛 #{comp['id']} 并新建竞赛，避免不同资金口径混入同一排名")
                elif not same_roster:
                    reason = (f"新增或替换了真实模型参赛者/模型配置，结束竞赛 #{comp['id']} 并新建竞赛，"
                              "避免不同模型在同一排行榜中途接力")
                elif not same_margin_admission_cap:
                    reason = (f"保证金开仓/目标风险区间已变更，结束竞赛 #{comp['id']} 并新建竞赛，"
                              "避免不同风险准入规则混入同一排名")
                elif not same_tz:
                    # Reported before the universe reasons: a snapshot old enough to
                    # omit the timezone also omits the identity block, and the
                    # timezone is the more specific thing to tell the operator.
                    reason = (f"竞赛时区从 {old_tz} 变更为 {c.timezone}，结束竞赛 #{comp['id']} 并新建竞赛，"
                              f"避免历史成交与日汇总被按新时区重新划分")
                elif self._recorded_contract_universe(comp) is None:
                    log_message = (f"Competition #{comp['id']} recorded no contract-universe identity, so it cannot "
                                   "be shown to match the current configuration; ending it and starting a new one "
                                   "(its trades and standings are kept intact)")
                    reason = (f"竞赛 #{comp['id']} 未记录参赛品种与合约条款身份，无法证明与当前配置一致，"
                              "结束该竞赛并新建竞赛（其成交与排名完整保留）")
                elif (same_contract_universe and not same_market_feed and c.market == "ctp"
                      and self._recorded_market_feed(comp) is None):
                    log_message = (f"Competition #{comp['id']} recorded no effective market-feed signature (each "
                                   "symbol's exchange), so it cannot be shown to run the same CTP feed as the current "
                                   "contract table; ending it and starting a new one (its trades and standings are kept intact)")
                    reason = (f"竞赛 #{comp['id']} 未记录生效的行情接入签名（含各品种的交易所），无法证明与当前"
                              "合约表接的是同一条 CTP 行情，结束该竞赛并新建竞赛（其成交与排名完整保留）")
                elif same_contract_universe and not same_market_feed:
                    # 行情接入自己有一条分支：落到下面那条兜底文案的话，改了前置或钉死的合约的
                    # 使用者看到的是"参赛品种或合约条款已变更"，会去翻一个根本没动过的地方。
                    log_message = (f"Effective market feed settings changed; ending competition #{comp['id']} and "
                                   "starting a new one so results produced from different price series do not "
                                   "share a leaderboard")
                    reason = (f"行情接入设置（前置、钉死的合约、聚合周期等）已变更，结束竞赛 #{comp['id']} "
                              "并新建竞赛，避免不同价格序列下的成绩混入同一排名")
                else:
                    log_message = (f"Active symbols (including their order) or their contract terms changed; ending "
                                   f"competition #{comp['id']} and starting a new one so results produced under "
                                   "different universes or terms do not share a leaderboard")
                    reason = (f"参赛品种（含顺序）或其合约条款已变更，结束竞赛 #{comp['id']} 并新建竞赛，"
                              "避免不同品种范围/不同合约条款下的成绩混入同一排名")
                log.warning(log_message or reason)
                self.db.add_event(comp["id"], "warn", "engine", reason)
            cid = self.db.create_competition(c.name, c.market, c.initial_cash, self._config_snapshot_json())
            if comp:
                # 竞赛切换会让旧 id 失活，它的在飞研究必须定成终态，否则重启后留下孤儿 "running" 记录。
                # 但这一步只能在新竞赛写入成功之后做：create_competition 失败时切换根本没有发生、
                # 旧竞赛仍然进行中，而 aborted_restarted 是终态，提前打上去会让那次付费研究再也写不回结果
                # （与 reset() 里同一条约束）。
                aborted = self.db.abort_running_debates(
                    comp["id"], "Competition configuration changed before research debate completed"
                )
                if aborted:
                    self.db.add_event(comp["id"], "warn", "debate", f"配置切换前已中止 {aborted} 场未完成的赛前研究辩论")
            self.comp = self.db.active_competition() or {"id": cid}
            self.round_no = 0
            self.initial_cash = float(c.initial_cash)
            self.accounts = {}
            self.db.add_event(cid, "info", "engine", f"新建竞赛：{c.name}（{c.market}），起始资金 {c.initial_cash:,.0f}")
        cid = self.comp["id"]
        aborted = self.db.abort_running_debates(cid, "Engine restarted before the research debate could finish")
        if aborted:
            self.db.add_event(cid, "warn", "debate", f"已中止 {aborted} 场因重启未完成的赛前研究辩论")
        self.rules = self._build_rules()
        # 暂停状态必须在任何后台线程启动前恢复，否则重启后可能在恢复前就执行一轮决策
        self.paused = self.db.kv_get(f"paused:{cid}", "0") == "1"
        self._reconcile(cid)
        for t in self.cfg.traders:
            if t.id not in self.accounts:
                self.accounts[t.id] = Account(trader_id=t.id, cash=self.initial_cash, initial_cash=self.initial_cash, peak_equity=self.initial_cash)
                self.db.save_account(cid, self.accounts[t.id])
            tr = self.traders[t.id]
            self._account_versions.setdefault(t.id, 0)
            self.db.upsert_trader(cid, t.id, t.name, t.color, tr.mode, _seat_model(t) if _is_live_mode(tr.mode) else f"rule:{t.fallback_rule}")
        raw = self.db.kv_get(f"day_start:{cid}")
        if raw:
            try:
                self._day_start = json.loads(raw)
            except Exception:  # noqa: BLE001
                self._day_start = {}
        # 恢复后先用持仓最后价初始化 prices，避免权益显示为 0
        for acc in self.accounts.values():
            for p in acc.positions.values():
                if p.last_price:
                    self.prices.setdefault(p.symbol, p.last_price)

    def _spec_for(self, sym: str) -> ContractSpec:
        if sym in self.specs:
            return self.specs[sym]
        if sym in self._all_specs:
            return self._all_specs[sym]
        if self.cfg.competition.market == "crypto":
            return crypto_spec(sym, max_leverage=max(1.0, self.cfg.risk.max_leverage))
        # Only a code the configuration has dropped while the database still holds
        # a position reaches this (via _SpecMap.__missing__); active symbols were
        # proven to have explicit terms in _build_source.
        return legacy_fallback_cn_spec(sym, reason=f"an account still holds a legacy {sym} position to close out")

    def _reconcile(self, cid: int):
        """配置与数据库状态对齐：移除的交易员不再参赛；移除品种上的持仓按最后价平掉。

        触发成交仍待落库的账户推迟到结算之后再对齐（_tick 重试成功时补做）：现在对齐会删掉账户或按最后价平掉持仓，
        原触发价的那笔成交就被替换或丢弃了。
        """
        for tid in list(self.accounts):
            if tid in self._pending_marks or tid in self._pending_mark_updates:
                # 关闭前未写入的盯市更新同样：先对齐会按陈旧的库内最后价平掉已移除品种、或删掉账户，随后重试找不到
                # 持仓/账户而清掉日志，记录的价就永久丢了
                self._deferred_reconcile.add(tid)
                log.warning("交易员 %s 有触发成交或盯市更新待落库，配置对齐推迟到写入之后", tid)
                continue
            self._reconcile_account(cid, tid)

    def _reconcile_account(self, cid: int, tid: str):
        acc = self.accounts.get(tid)
        if acc is None:
            return
        if tid not in {t.id for t in self.cfg.traders}:
            del self.accounts[tid]
            self.db.add_event(cid, "warn", "engine", f"配置中已移除交易员 {tid}，其账户保留在数据库但不再参赛")
            return
        acc = copy.deepcopy(acc)
        changed = False
        trades: List[Trade] = []
        for sym in list(acc.positions):
            if sym in self.specs:
                continue
            pos = acc.positions[sym]
            spec = self._spec_for(sym)
            price = pos.last_price or pos.avg_price
            r = self.exchange.close(acc, spec, price, self.round_no, reason="品种已从配置移除，按最后价平仓", force=True)
            if r.accepted and r.trade:
                trades.append(r.trade)
                self.db.add_event(cid, "warn", tid, f"{sym} 已从配置移除，按最后价 {price:g} 平仓，盈亏 {r.trade.realized_pnl:+,.0f}")
            else:
                acc.positions.pop(sym, None)
                self.db.add_event(cid, "error", tid, f"{sym} 已从配置移除且无法平仓（{r.reason}），持仓已丢弃")
            changed = True
        if changed:
            self.db.save_account(cid, acc, trades=trades)
            self.accounts[tid] = acc

    def _archived_context(self, comp: dict, accounts: Dict[str, Account]):
        """归档竞赛的撮合器与合约规格：按该竞赛创建时持久化的配置（市场、合约表、风控参数）构建，
        不能用新配置——市场可能已经不同。持久化配置解析失败时退回当前风控参数，规格按旧市场取默认值。

        只是兜底：预写日志里记录了触发时生效的条款（撮合参数 + 合约规格快照）时，重试按日志条款重建撮合器
        （_pending_exchange），不受合约表事后编辑的影响；这里的撮合器只服务于没有条款的旧格式日志。"""
        market = comp.get("market") or self.cfg.competition.market
        try:
            old_cfg = AppConfig.model_validate_json(comp.get("config_json") or "{}")
            old_cfg.base_dir = self.cfg.base_dir
        except Exception as e:  # noqa: BLE001
            log.warning("竞赛 #%s 持久化的配置无法解析（%s），按当前风控参数补做", comp.get("id"), e)
            old_cfg = self.cfg
        needed = set(old_cfg.symbols)
        for acc in accounts.values():
            needed.update(acc.positions)
        if market == "crypto":
            specs = {s: crypto_spec(s, max_leverage=max(1.0, old_cfg.risk.max_leverage)) for s in needed}
        else:
            try:
                table, _ = load_cn_contracts(old_cfg.resolve(old_cfg.market_data.cn_futures.contracts_file))
            except Exception as e:  # noqa: BLE001
                log.warning("读取旧竞赛合约表失败（%s），使用默认规格", e)
                table = dict(self._all_specs)
            # Prefer the terms persisted in the archived competition's own table;
            # degrade only when the row truly cannot be found, and only to replay
            # a fill that has already happened.
            specs = {s: table.get(s) or legacy_fallback_cn_spec(
                s, reason=f"archived competition #{comp.get('id')} has no {s} row; replaying a triggered fill only")
                for s in needed}
        r = old_cfg.risk
        exchange = SimExchange(specs, slippage_ticks=r.slippage_ticks, min_hold_sec=0,
                               liquidation_risk_ratio=r.liquidation_risk_ratio, maintenance_margin_ratio=r.maintenance_margin_ratio,
                               max_margin_risk_ratio=r.max_margin_risk_ratio,
                               target_margin_risk_max_ratio=r.target_margin_risk_max_ratio)
        return exchange, specs

    def _settle_archived_journal(self, comp: dict):
        """按新配置切换竞赛之前，先把旧竞赛尚未落库的触发成交（止损/止盈/强平）按其持久化配置补做。

        启动只恢复进行中竞赛的预写日志：旧竞赛一旦归档，它的日志再也没人读，这笔成交就永久丢失。
        补不了（数据库暂不可写）则拒绝切换（RolloverBlocked），旧竞赛保持进行中。
        """
        old_cid = int(comp["id"])
        accounts = self.db.load_accounts(old_cid)
        saved = (self.comp, self.exchange, self.specs, self.accounts, self.round_no)
        self.comp, self.accounts, self.round_no = comp, accounts, int(comp.get("round_no") or 0)
        self.exchange, self.specs = self._archived_context(comp, accounts)
        try:
            self._restore_pending_marks(old_cid)
            if not self._pending_marks and not self._pending_mark_updates:
                return
            self._apply_pending_mark_updates()
            for tid in list(self._pending_marks):
                self._mark_and_settle(tid, *self._pending_marks[tid], retry=True)
            if self._pending_marks or self._pending_mark_updates:
                who = "、".join(sorted(set(self._pending_marks) | set(self._pending_mark_updates)))
                raise RolloverBlocked(f"竞赛 #{old_cid} 中账户 {who} 的触发成交尚未落库（数据库暂不可写？），"
                                      f"不能按新配置切换竞赛：请修复后重启，或先恢复原配置")
            self._write_journal()   # 清理旧竞赛的日志文件；残留副本归档后无人再读，不阻止切换
            self._journal_stale.clear()
            self._unsettled.clear()
            self._mark_save_failed.clear()
            log.info("旧竞赛 #%s 待落库的触发成交已补做，继续按新配置切换竞赛", old_cid)
        finally:
            self.comp, self.exchange, self.specs, self.accounts, self.round_no = saved
            self._journal_replay_pos = None   # 旧竞赛的回放位置不属于新竞赛（新竞赛从头回放）

    def _restore_replay_position(self, cid: int, override: Optional[dict] = None):
        """回放源：恢复到上次运行回放到的位置。否则重启后会把同一段行情再放一遍，
        数据库里已恢复的成交/持仓会出现在后续行情之前，时间顺序与实验结果都被破坏。
        override：预写日志记录的位置（关闭时保留的 bar 之后），优先于 kv 里落后一根的位置。"""
        if not isinstance(self.source, ReplaySource):
            return
        raw = self.db.kv_get(f"replay_pos:{cid}")
        try:
            state = json.loads(raw) if raw else None
            if override is not None:
                if state is None or self._cursor_ahead(override, state):
                    state = override
                else:
                    # 日志里的位置不比 kv 新（残留的旧副本/墓碑）：以 kv 为准，也不为它停住回放
                    self._journal_replay_pos = None
            if state is None:
                return
            current = self.source.position()
            if (state.get("steps") == current.get("steps") and state.get("base_ts") == current.get("base_ts")
                    and state.get("symbols") == current.get("symbols")):
                return   # 已经在该位置（重置时与新竞赛一起写入的就是当前位置）：resume 会再快进一次，不能调用
            exact = self.source.resume(state)
            syms = state.get("symbols") if isinstance(state.get("symbols"), dict) else state
            where = {s: datetime.fromtimestamp(v["ts"], self.tz).strftime("%m-%d %H:%M") for s, v in syms.items()}
            log.info("回放位置已%s恢复：%s", "精确" if exact else "近似", where)
            if not exact:
                self._event_best_effort("warn", "engine", "回放数据或配置与上次运行不一致，已按上次时刻/价格近似衔接")
        except Exception as e:  # noqa: BLE001
            log.warning("恢复回放位置失败，从头回放: %s", e)

    def _persist_replay_position(self) -> bool:
        """记录回放位置。返回是否写入成功。

        只在本 tick 所有账户都已成功处理这根 bar 之后调用：若某账户的触发成交落库失败或进程中途退出，
        持久化的位置仍停在上一根，重启后会重放这根 bar、再次触发。
        写入失败则进入 _cursor_pending：不取下一根 bar、不开新一轮，每个 tick 重试，直到写入成功——
        否则回放继续前进而持久化的位置落后，重启后已按后续行情落库的成交/持仓会跑到行情前面。
        """
        try:
            self.db.kv_set(f"replay_pos:{self.comp_id}", json.dumps(self.source.position()))
        except Exception as e:  # noqa: BLE001
            log.warning("持久化回放位置失败，回放暂停前进、每个 tick 重试: %s", e)
            if not self._cursor_pending:
                self._event_best_effort("warn", "engine", PUBLIC_RUNTIME_EVENT_MESSAGES["replay_position"])
            self._cursor_pending = True
            return False
        if self._cursor_pending:
            log.info("回放位置写入成功，继续前进")
        self._cursor_pending = False
        return True

    def _replay_held(self) -> bool:
        """回放是否停住：这根 bar 尚有账户未结算、回放位置待写入，或重启恢复的预写日志尚未全部结算。"""
        return self._replay_retry is not None or self._cursor_pending or self._journal_replay_pos is not None

    def _held_status(self) -> str:
        if self._cursor_pending:
            return "回放位置尚未持久化，等待写入成功后再决策"
        if self._journal_replay_pos is not None:
            return "重启恢复的预写日志尚未全部写入，等待写入成功后再决策"
        return "回放 bar 尚有账户未结算，等待重放后再决策"

    @staticmethod
    def _cursor_ahead(a: dict, b: dict) -> bool:
        """回放位置 a 是否比 b 更靠后（同一起点、更多步）。"""
        return (isinstance(a.get("steps"), int) and isinstance(b.get("steps"), int)
                and a.get("base_ts") == b.get("base_ts") and a["steps"] > b["steps"])

    def _release_journal_replay_pos(self):
        """重启恢复的预写日志全部结算：关闭时保留的那根 bar 在所有账户上都已持久化，回放位置（已停在它之后）写入 kv，
        不再重放它——重放会按现在的风控参数/合约表重新评估当时已评估过且无成交的 bar，凭空产生成交。
        先写 kv 再清日志：写入失败时位置仍留在日志里（只剩位置的空日志不删），进入 _cursor_pending，下个 tick 只重试写入。"""
        if self._journal_replay_pos is None or self._pending_marks or self._pending_mark_updates:
            return
        if isinstance(self.source, ReplaySource) and not self._persist_replay_position():
            return
        self._journal_replay_pos = None
        self._write_journal()   # 位置已进 kv：清掉只剩位置的空日志（清不掉也无妨，恢复时它不比 kv 新）

    # ---------- 盯市结算 ----------
    def _journal_path(self, cid: Optional[int] = None) -> Path:
        """触发成交预写日志的主位置（数据库旁边）。"""
        return self._journal_paths(cid)[0]

    def _journal_paths(self, cid: Optional[int] = None) -> List[Path]:
        """触发成交的预写日志：与 SQLite 无关的独立文件。数据库锁死/写满/不可用时 kv 同样写不进去，
        只有先写在这里的原触发价，才能保证进程退出后重启仍按原价补做。

        主位置在数据库旁边；数据目录写满或只读时（此时 SQLite 多半也写不进去）退到系统临时目录下的备用位置。
        """
        name = f"pending_marks_{self.comp_id if cid is None else cid}.json"
        db_path = Path(self.cfg.db_path)
        primary = db_path.with_name(name)
        # 备用位置按数据库路径命名空间隔离：同一主机上不同数据库的实例可以并存（run.py 的实例锁按数据库粒度），
        # 不能互相覆盖/删除对方的待重试触发
        db_key = hashlib.sha1(str(db_path.resolve()).encode("utf-8")).hexdigest()[:12]
        fallback = Path(tempfile.gettempdir()) / "futures-arena" / db_key / name
        return [primary, fallback]

    @staticmethod
    def _write_json_atomic(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)   # 原子替换：不会留下半写的文件

    def _write_journal(self) -> bool:
        """把待重试触发写入预写日志。返回是否已有一份持久化成功（主位置或备用位置）。

        每次写入带递增的版本号：主位置写成功后又变得不可写、后续状态只写进了备用位置时，
        两处会并存新旧两份，恢复时以版本最新的一份为准，而不是固定偏向主位置。
        """
        paths = self._journal_paths()
        if not self._pending_marks and not self._pending_mark_updates and self._journal_replay_pos is None:
            # 条目全部结算、也没有待写入 kv 的回放位置：删掉日志（还没写进 kv 的位置得留在日志里，重启从它恢复）
            leftover = False
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except Exception as e:  # noqa: BLE001
                    leftover = True
                    log.warning("清除触发预写日志 %s 失败: %s", path, e)
            if not leftover:
                return True
            # 有旧副本删不掉（目录只读等）：必须写一份版本更新的空墓碑，否则崩溃重启会把旧副本里的
            # 历史触发价套到之后新开的持仓上，凭空产生止损/强平。墓碑写到任一可写位置即可，恢复时以最新版本为准。
            log.warning("旧的触发预写日志无法删除，改写空墓碑标记已结算")
        self._journal_version = max(time.time(), self._journal_version + 1e-3)
        # 触发条目也带上记住的高水位：之前失败的无成交盯市算出的权益峰值，重试落库时一起写入，不随进程消失
        marks = {tid: {"prices": p, "clock": c, "clocks": cs, "terms": self._pending_terms.get(tid),
                       "peak": self._mark_save_failed.get(tid)}
                 for tid, (p, c, cs) in self._pending_marks.items()}
        for tid, upd in self._pending_mark_updates.items():
            if tid in marks:
                # 同一账户既有触发待重试又有关闭前未写入的盯市更新（重启后新报价触发、落库又失败）：更新随触发条目一起保留，
                # 恢复时先直接写入更新、再按原价重试触发，两者都不丢
                marks[tid]["mark_update"] = {"prices": upd["prices"], "peak": upd["peak"]}
            else:
                # 只记盯市更新的条目带 kind=mark，恢复时直接写入而不重新盯市
                marks[tid] = {"kind": "mark", "prices": upd["prices"], "peak": upd["peak"]}
        payload: dict = {"version": self._journal_version, "marks": marks}
        if self._journal_replay_pos is not None:
            payload["replay_pos"] = self._journal_replay_pos   # 关闭时保留的 bar 之后的回放位置：日志全部结算后越过它
        text = json.dumps(payload)
        ok = False
        for i, path in enumerate(paths):
            try:
                self._write_json_atomic(path, text)
                ok = True
                # 写成功后尽力清掉其他位置的旧副本（主位置不可写时多半也删不掉，靠版本号兜底）
                for other in paths[:i] + paths[i + 1:]:
                    try:
                        other.unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        pass
                break
            except Exception as e:  # noqa: BLE001
                log.warning("写入触发预写日志失败（%s）: %s", path, e)
        return ok

    @staticmethod
    def _parse_journal(text: str) -> tuple:
        """返回 (version, marks, replay_pos)。兼容没有版本号的旧格式（视为版本 0，无回放位置）。"""
        data = json.loads(text)
        if isinstance(data, dict) and "marks" in data:
            pos = data.get("replay_pos")
            return float(data.get("version") or 0.0), dict(data["marks"]), pos if isinstance(pos, dict) else None
        return 0.0, dict(data), None

    def _restore_pending_marks(self, cid: int):
        """读取主位置与备用位置，以版本最新的一份为准（版本相同则合并）。"""
        newest_version, newest, chosen, replay_pos = -1.0, {}, None, None
        for path in self._journal_paths(cid):
            if not path.exists():
                continue
            try:
                version, marks, pos = self._parse_journal(path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("读取触发预写日志 %s 失败: %s", path, e)
                continue
            if version > newest_version:
                newest_version, newest, chosen, replay_pos = version, marks, path, pos
            elif version == newest_version:
                newest = {**marks, **newest}
                replay_pos = replay_pos or pos
        if chosen is None:
            return
        self._journal_version = max(self._journal_version, newest_version)
        for tid, v in newest.items():
            if tid not in self.accounts:
                continue
            upd = v if v.get("kind") == "mark" else v.get("mark_update")
            if isinstance(upd, dict):
                # 关闭前没写进去的盯市更新：记住最新价与高水位，直接写入（_apply_mark_update），不经过撮合器
                peak = float(upd.get("peak") or 0.0)
                self._pending_mark_updates[tid] = {"prices": dict(upd.get("prices") or {}), "peak": peak}
                if peak > 0:
                    self._mark_save_failed[tid] = peak
            if v.get("kind") == "mark":
                continue
            self._pending_marks[tid] = (dict(v["prices"]), v.get("clock"), v.get("clocks"))
            if isinstance(v.get("terms"), dict):
                self._pending_terms[tid] = v["terms"]
            if v.get("peak"):   # 触发前一次失败的无成交盯市记住的高水位：重试落库时带上
                self._mark_save_failed[tid] = max(float(v["peak"]), self._mark_save_failed.get(tid, 0.0))
            self._unsettled.add(tid)
        if replay_pos is not None and isinstance(self.source, ReplaySource):
            # 关闭时保留的那根回放 bar 之后的位置：回放从这里恢复、日志全部结算后才写入 kv，期间不前进也不重放那根 bar。
            # 只剩位置的空日志/墓碑也算：条目已全部写入、位置却还没写进 kv 就退出了。比 kv 旧的位置在 _restore_replay_position 作废
            self._journal_replay_pos = replay_pos
        if self._pending_marks or self._pending_mark_updates:
            log.warning("从 %s 恢复到 %d 个账户有落库失败待重试的触发成交、%d 个账户有未写入的盯市更新，将按记录补做",
                        chosen, len(self._pending_marks), len(self._pending_mark_updates))

    def _settle_journal(self, tid: str):
        """账户 tid 的预写日志条目（触发成交或关闭前未写入的盯市更新）已落库：更新预写日志。
        旧副本删不掉且墓碑也写不进去时，该账户保持封锁直到日志清理成功——否则崩溃重启会把旧副本里的历史价套到之后新开的持仓上。"""
        self._release_journal_replay_pos()   # 这是最后一条时先把回放位置写进 kv，再清日志（清了就没人记得它）
        if self._write_journal():
            self._unsettled -= self._journal_stale
            self._journal_stale.clear()
            return
        self._journal_stale.add(tid)
        self._unsettled.add(tid)
        log.error("交易员 %s 的预写日志条目已落库，但旧副本无法删除且墓碑写入失败：在日志清理成功前不执行其决策", tid)
        self._event_best_effort("error", tid, "预写日志条目已落库，但旧副本无法清理/写墓碑（磁盘只读或写满？）；清理成功前本账户不执行决策")

    def _retry_stale_journal(self):
        """每个 tick 重试清理残留的预写日志副本（写墓碑）；成功后解除相关账户的封锁。"""
        if self._journal_stale and self._write_journal():
            self._unsettled -= self._journal_stale
            self._journal_stale.clear()
            log.info("预写日志清理成功，解除封锁")

    def _mark_and_settle(self, tid: str, prices: Dict[str, float], clock: Optional[float], clocks: Optional[Dict[str, float]],
                         retry: bool = False) -> bool:
        """在账户副本上盯市并落库；成功（含无成交）返回 True 并发布副本，落库失败返回 False 且内存状态不变。

        有触发成交时先把原价/时钟写入预写日志，再尝试落库：落库失败则该账户进入待重试（其决策被拒），
        成功后清除日志。retry=True 表示这是对之前失败触发的重试（使用记录下来的原价与时钟）。
        """
        acc = self.accounts.get(tid)
        if acc is None:
            self._pending_marks.pop(tid, None)
            self._pending_terms.pop(tid, None)
            self._unsettled.discard(tid)
            return True
        cand = copy.deepcopy(acc)
        upd = self._pending_mark_updates.get(tid)
        if upd:
            # A pending mark is an already-evaluated state, not a fresh price to evaluate under today's terms.
            # Seed only symbols absent from this tick before mark() so account equity and any account-level
            # liquidation use the same journaled price. A current quote always takes precedence.
            for sym, px in (upd.get("prices") or {}).items():
                if sym in cand.positions and sym not in prices:
                    cand.positions[sym].last_price = px
        failed_peak = self._mark_save_failed.get(tid)
        if failed_peak is not None:
            cand.peak_equity = max(cand.peak_equity, failed_peak)   # 上次没写进去的高水位：这次连同结果一起落库
        # 重试按日志里记录的当时条款（撮合参数 + 合约规格）补做；首次触发用当前撮合器
        ex = self._pending_exchange(tid, cand) if retry else self._exchange_for(cand)
        trades = ex.mark(cand, prices, self.round_no, ts=clock, ts_by_symbol=clocks)
        if not trades:
            # 无成交也会改持久化字段（持仓最新价、权益峰值）：同样先落库再发布。否则进程突然退出后数据库里是旧值，
            # 重启时若该品种已从配置移除，对齐会按陈旧的最新价平仓、盈亏错误。落库失败不是触发待重试（没有触发）：
            # 内存保持旧值（估值本就用最新行情，不受影响），下个 tick 再试；retry 的条目与预写日志此时也不清，
            # 要等写入真正提交
            if self._durable_fields(cand) != self._durable_fields(acc):
                if upd and not self._journal_merged_update(tid, upd, {s: px for s, px in prices.items() if s in cand.positions},
                                                           cand.peak_equity):
                    # 关闭前记在日志里的更新还没写进去：这次按新报价的写入同样先把并入后的值写进日志，日志写不进去就不写数据库
                    self._mark_save_failed[tid] = max(self._mark_save_failed.get(tid, 0.0), cand.peak_equity)
                    return False
                try:
                    self.db.save_account(self.comp_id, cand)
                except Exception as e:  # noqa: BLE001
                    if tid not in self._mark_save_failed:
                        log.warning("交易员 %s 的盯市更新（无成交）落库失败，内存保持旧值，下个 tick 再试: %s", tid, e)
                    self._mark_save_failed[tid] = cand.peak_equity
                    # 回放源：这根 bar 对该账户尚未落库，回放位置不能越过它（否则重启后位置在 bar 之后而最新价还是更早的），
                    # _tick 会保留这根 bar 重放，直到写入成功
                    return False
            # 写入已提交（或无需写入）：现在才解除待重试条目、预写日志与失败标记
            if retry:   # 记录的触发已不再成立（如持仓已不存在）：解除待重试
                self._pending_marks.pop(tid, None)
                self._pending_terms.pop(tid, None)
                self._unsettled.discard(tid)
                self._settle_journal(tid)
            self._mark_write_succeeded(tid)
            self.accounts[tid] = cand
            return True
        self._pending_marks[tid] = (dict(prices), clock if clock is not None else time.time(), dict(clocks) if clocks else None)
        if tid not in self._pending_terms:   # 首次触发（或旧格式日志没有条款）：记下此刻生效的条款
            self._pending_terms[tid] = self._terms_of(ex, list(acc.positions))
        journaled = self._write_journal()   # 先于落库；主位置与备用位置都失败时才为 False
        if not journaled:
            # 无法预写日志（磁盘写满/只读）：仍尝试落库——落库成功本身就是最持久的结果；
            # 若落库也失败，触发只存在于内存，账户保持待重试，每个 tick 都会重新尝试写日志与落库
            log.error("交易员 %s 的触发成交无法写入预写日志（主/备用位置均失败），仍尝试落库", tid)
        try:
            self.db.save_account(self.comp_id, cand, trades=trades)
        except Exception as e:  # noqa: BLE001
            log.exception("交易员 %s 盯市成交落库失败，已回滚内存状态（将按原价重试）: %s", tid, e)
            message = (
                PUBLIC_RUNTIME_EVENT_MESSAGES["mark_persistence_without_journal"]
                if not journaled
                else PUBLIC_RUNTIME_EVENT_MESSAGES["mark_persistence"]
            )
            self._event_best_effort("error", tid, message)
            self._unsettled.add(tid)
            return False
        self._pending_marks.pop(tid, None)
        self._pending_terms.pop(tid, None)
        self._unsettled.discard(tid)
        self._mark_write_succeeded(tid)   # 成交连同最新价/权益峰值（含带上的高水位）一起落库了
        self._settle_journal(tid)
        # 事务已提交：立即发布副本，事件日志只作尽力而为，失败不能让内存落后于数据库
        self.accounts[tid] = cand
        self._account_versions[tid] = self._account_versions.get(tid, 0) + 1
        self._bump_version()
        for t in trades:
            self._event_best_effort("info", tid, f"{t.symbol} {t.action} {t.qty:g}@{t.price:g} 盈亏 {t.realized_pnl:+,.0f}（{t.reason}）"
                                    + ("（落库失败后重试成功）" if retry else ""))
        return True

    def _mark_write_succeeded(self, tid: str):
        """该账户的一次盯市写入已提交：记住的高水位已进库，关闭前未写入的更新也已进库或被更新的盯市取代。"""
        had_update = self._pending_mark_updates.pop(tid, None) is not None
        if tid in self._mark_save_failed:
            self._mark_save_failed.pop(tid, None)
            log.info("交易员 %s 的盯市更新落库恢复", tid)
        if had_update:
            self._settle_journal(tid)   # 清日志；旧副本删不掉且墓碑写不进去时与触发一样封锁该账户，直到清理成功

    def _journal_merged_update(self, tid: str, upd: dict, prices: Dict[str, float], peak: float) -> bool:
        """把并入了新报价/更高高水位的盯市更新先写进预写日志；日志与磁盘一致后才允许写数据库。返回日志是否就绪。

        否则数据库提交后、日志清理前退出，重启会把日志里的旧价盖回新状态（品种随后被移除时还会按旧价平仓）。
        日志写不进去就本次不写数据库，内存条目保持与磁盘一致，下次再试。直接写入与按新报价的盯市写入都走这里。
        """
        old_prices = dict(upd.get("prices") or {})
        old_peak = float(upd.get("peak") or 0.0)
        merged = {**old_prices, **prices}
        peak = max(old_peak, peak)
        if merged == old_prices and peak == old_peak:
            return True
        self._pending_mark_updates[tid] = {"prices": merged, "peak": peak}
        if self._write_journal():
            return True
        self._pending_mark_updates[tid] = upd
        log.warning("交易员 %s 并入新报价的盯市更新写不进预写日志，本次不写数据库，稍后再试", tid)
        return False

    def _apply_pending_mark_updates(self):
        for tid in list(self._pending_mark_updates):
            self._apply_mark_update(tid)

    def _apply_mark_update(self, tid: str) -> bool:
        """写入关闭前没写进去、记在预写日志里的盯市更新（各持仓最新价 + 权益峰值）。

        直接写入，不经过撮合器：它只是当时已算过且没有成交的盯市结果，按现在的风控参数或合约表重新盯市可能凭空
        产生当时并不存在的强平。写入失败则日志与条目保留，下个 tick 再试；期间高水位由 _mark_save_failed 带给其他写入。
        """
        upd = self._pending_mark_updates.get(tid)
        if upd is None:
            return True
        acc = self.accounts.get(tid)
        if acc is None:
            self._pending_mark_updates.pop(tid, None)
            self._write_journal()
            return True
        # 重启后真正收到的报价比日志里的新，且已在各自的 tick 评估过（有成交的在触发条目里，这里只有无成交的）：
        # 一起写入，否则写入成功后清掉的失败标记会把这些报价的盯市（含更高的权益峰值）永久丢掉
        fresh = {s: q.price for s, q in self.quotes.items() if s in acc.positions}
        if not self._journal_merged_update(tid, upd, fresh, self._mark_save_failed.get(tid, 0.0)):
            return False
        upd = self._pending_mark_updates[tid]
        cand = copy.deepcopy(acc)
        for sym, px in (upd.get("prices") or {}).items():
            if sym in cand.positions:
                cand.positions[sym].last_price = px
        cand.peak_equity = max(cand.peak_equity, float(upd.get("peak") or 0.0))
        try:
            self.db.save_account(self.comp_id, cand)
        except Exception as e:  # noqa: BLE001
            log.warning("交易员 %s 关闭前未写入的盯市更新仍写不进数据库，日志保留，稍后再试: %s", tid, e)
            self._mark_save_failed[tid] = max(self._mark_save_failed.get(tid, 0.0), cand.peak_equity)
            return False
        self.accounts[tid] = cand
        self._mark_write_succeeded(tid)
        return True

    @staticmethod
    def _durable_fields(acc: Account) -> tuple:
        """无成交盯市也会变化、且需要持久化的字段：各持仓最新价与权益峰值。"""
        return (acc.peak_equity, tuple(sorted((s, p.last_price) for s, p in acc.positions.items())))

    @staticmethod
    def _terms_of(ex: SimExchange, symbols: List[str]) -> dict:
        """触发时生效的条款：撮合参数与所涉品种的合约规格（写入预写日志，重试时据此重建撮合器）。"""
        return {"risk": {"slippage_ticks": ex.slippage_ticks, "min_hold_sec": ex.min_hold_sec,
                         "liquidation_risk_ratio": ex.liq_ratio, "maintenance_margin_ratio": ex.maint_ratio,
                         "max_margin_risk_ratio": ex.max_margin_risk_ratio,
                         "target_margin_risk_max_ratio": ex.target_margin_risk_max_ratio},
                "specs": {s: asdict(ex.specs[s]) for s in symbols if s in ex.specs}}

    def _pending_exchange(self, tid: str, acc: Account) -> SimExchange:
        """重试待落库触发用的撮合器：按日志记录的当时条款重建；旧格式日志没有条款时退回当前撮合器。"""
        terms = self._pending_terms.get(tid)
        if not terms:
            return self._exchange_for(acc)
        cur = self.exchange
        try:
            specs = {s: ContractSpec(**d) for s, d in (terms.get("specs") or {}).items()}
            r = terms.get("risk") or {}
            ex = SimExchange(specs, slippage_ticks=int(r.get("slippage_ticks", cur.slippage_ticks)),
                             min_hold_sec=int(r.get("min_hold_sec", cur.min_hold_sec)),
                             liquidation_risk_ratio=float(r.get("liquidation_risk_ratio", cur.liq_ratio)),
                             maintenance_margin_ratio=float(r.get("maintenance_margin_ratio", cur.maint_ratio)),
                             max_margin_risk_ratio=r.get("max_margin_risk_ratio", cur.max_margin_risk_ratio),
                             target_margin_risk_max_ratio=r.get("target_margin_risk_max_ratio", cur.target_margin_risk_max_ratio))
        except Exception as e:  # noqa: BLE001
            log.warning("交易员 %s 的触发条款无法解析（%s），按当前条款重试", tid, e)
            return self._exchange_for(acc)
        for s in acc.positions:          # 条款未覆盖的品种（不应发生）补当前规格，避免盯市 KeyError
            if s not in ex.specs:
                ex.specs[s] = self._spec_for(s)
        return ex

    def _exchange_for(self, acc: Account) -> SimExchange:
        """盯市用的撮合器。持仓里有当前合约规格之外的品种（已从配置移除、但触发成交尚未结算，对齐被推迟）时，
        用补全了规格的副本，不改动主撮合器。"""
        missing = [s for s in acc.positions if s not in self.exchange.specs]
        if not missing:
            return self.exchange
        specs = dict(self.exchange.specs)
        for s in missing:
            specs[s] = self._spec_for(s)
        ex = self.exchange
        return SimExchange(specs, slippage_ticks=ex.slippage_ticks, min_hold_sec=ex.min_hold_sec,
                           liquidation_risk_ratio=ex.liq_ratio, maintenance_margin_ratio=ex.maint_ratio,
                           max_margin_risk_ratio=ex.max_margin_risk_ratio,
                           target_margin_risk_max_ratio=ex.target_margin_risk_max_ratio)

    @property
    def comp_id(self) -> int:
        return int(self.comp["id"])

    # ---------- 生命周期 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self._stop.clear()
        # 先恢复暂停标志，再启动线程（避免加速模式下线程在恢复前就执行决策）
        self.paused = self.db.kv_get(f"paused:{self.comp_id}", "0") == "1"
        self._seed_md_bars()   # 预热必须在第一个 tick 之前：否则第一轮仍然是空历史
        self._thread = threading.Thread(target=self._run, name="arena-engine", daemon=True)
        self._thread.start()
        self._watchdog = threading.Thread(target=self._watch, name="arena-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self):
        # Keep stop prompt even if unrelated state persistence holds self.lock.
        # The research submit gate establishes a precise ordering with the small
        # submit batch without ever waiting for that broader engine lock.
        with self._research_submit_lock:
            self._stop.set()
            self.running = False
        # Product-level research keeps its own durable call ledger.  It marks a
        # live run interrupted and quarantines any submitted-but-unconfirmed
        # provider work on resume; do not let the legacy global-session cleanup
        # erase that recovery boundary.
        self.product_research.stop()
        comp_id = self.comp_id
        # Persist the terminal state before waiting for bounded research workers.
        # A running HTTP request cannot be forcibly killed safely, but it must
        # never leave an automatic session reserved across a restart.
        try:
            if self.db.abort_running_debates(comp_id, "Engine stopped before research debate completed"):
                self._bump_version()
        except Exception as exc:  # noqa: BLE001
            log.warning("Unable to mark active research debates stopped: %s", exc)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)
        if self._round_thread and self._round_thread.is_alive():
            self._round_thread.join(timeout=15)   # 决策线程在停止信号后的下一个检查点放弃本轮
        if self._debate_thread and self._debate_thread.is_alive():
            self._debate_thread.join(timeout=15)
        # 取消尚未开始的决策任务；进行中的 LLM 请求在超时或重试检查点自行退出，其结果不再落库
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self._pending_marks or self._journal_stale or self._pending_mark_updates or self._journal_replay_pos is not None:
            # 关闭前再试一次预写日志（待重试触发或残留副本的墓碑）：之前磁盘写满/只读导致主备位置都失败时，这里是最后的机会
            self._write_journal()
        if self._cursor_pending:
            self._persist_replay_position()   # 回放位置同样再试一次：成功则重启后不必重放这根 bar
        self._release_journal_replay_pos()    # 恢复的日志已全部结算但位置还没写进 kv：再试一次，成功后清掉只剩位置的日志
        close = getattr(self.source, "close", None)
        if callable(close):
            try:
                close()   # 行情源的后台抓取线程池同样不再等待
            except Exception as e:  # noqa: BLE001
                log.warning("关闭行情源失败（已忽略）: %s", e)
        # 落库放在关闭**之后**：CTP 的回调跑在 SDK 自己的线程上，先落库的话，回调完全可能在
        # 落库返回之后、close() 生效之前再追加一条 tick——那条就又丢了。close() 只释放 API，
        # 内存里的聚合器仍在，所以关完再落一次是完整的。
        # 最后一次定期快照之后的 tick 只存在内存里，而 CTP 不提供历史 K 线、重启补不回来；
        # 跑得比一个快照间隔还短的一次运行，不冲这一下就等于什么都没积累到。
        self._persist_md_bars()
        # 账户状态在每次变动时都已落库，最终持久化只是兜底：拿不到锁不能让关闭挂死
        if self.lock.acquire(timeout=self.persist_lock_timeout):
            try:
                if not self._persist_all():
                    self.status_text = "已停止：有盯市更新未能持久化（见日志）"
            except Exception as e:  # noqa: BLE001
                # 状态已在每次变动时落库，最终持久化失败只记录，不能让关闭流程崩溃
                log.exception("停止时最终持久化失败（状态已在每次变动时落库）: %s", e)
                self._event_best_effort("error", "engine", PUBLIC_RUNTIME_EVENT_MESSAGES["shutdown_persistence"])
            finally:
                self.lock.release()
        else:
            log.error("停止时 %.0fs 内未能获取引擎锁，跳过最终持久化（状态已在每次变动时落库）", self.persist_lock_timeout)
            self._event_best_effort("error", "engine", "停止时未能获取引擎锁，跳过最终持久化")
        try:
            self.db.checkpoint()
        except Exception as e:  # noqa: BLE001
            log.exception("停止时 SQLite checkpoint 失败（已忽略）: %s", e)

    def pause(self, flag: bool):
        with self.lock:
            # Persist first so a failed control request never claims a durable state change.
            self.db.kv_set(f"paused:{self.comp_id}", "1" if flag else "0")
            if self.paused != flag:
                self._decision_epoch += 1
            self.paused = flag
            try:
                self.db.add_event(self.comp_id, "info", "user", "竞赛已暂停" if flag else "竞赛已继续")
            except Exception:  # noqa: BLE001
                log.exception("暂停状态已持久化，但无法写入审计事件")
            self._bump_version()

    def reset(self):
        """新建一场竞赛（旧竞赛保留为 ended）。

        旧竞赛仍有触发成交（止损/止盈/强平）落库失败待重试时，先按记录的原价补做；仍失败则拒绝重置（ResetBlocked）：
        归档后启动只恢复进行中竞赛的预写日志，直接丢弃会让这笔成交从旧竞赛的结果里永久消失。
        """
        with self.lock:
            self._retry_stale_journal()
            for tid in list(self._pending_marks):
                self._mark_and_settle(tid, *self._pending_marks[tid], retry=True)
            if self._pending_marks:
                who = "、".join(sorted(self._pending_marks))
                raise ResetBlocked(f"账户 {who} 的触发成交尚未落库（数据库暂不可写？），补做成功后才能重置")
            # 无成交盯市更新落库失败的账户：按当前行情（回放：保留重放的那根 bar）再写一次；关闭前记在日志里的更新直接写入；
            # 仍失败则拒绝重置——否则旧竞赛归档时留下陈旧的最新价/权益峰值，而新竞赛从已越过这根 bar 的位置开始
            self._apply_pending_mark_updates()
            for tid in list(self._mark_save_failed):
                if tid in self._pending_mark_updates:   # 日志里的更新只直接写入（刚试过），不经撮合器再盯市
                    continue
                self._mark_and_settle(tid, dict(self.prices), self._exec_clock(), self._exec_clocks())
            if self._mark_save_failed or self._pending_mark_updates:
                who = "、".join(sorted(set(self._mark_save_failed) | set(self._pending_mark_updates)))
                raise ResetBlocked(f"账户 {who} 的盯市更新尚未落库（数据库暂不可写？），写入成功后才能重置")
            self._release_journal_replay_pos()   # 恢复的日志已全部结算：位置越过关闭时保留的 bar，新竞赛从它之后开始
            if self._journal_stale:
                # 触发已结算，只是旧竞赛的预写日志副本还删不掉：归档后没有人再读它，不阻止重置
                log.warning("旧竞赛残留的预写日志副本未清理（%s），归档后不再使用", "、".join(sorted(self._journal_stale)))
            old_comp_id = self.comp_id
            self.db.add_event(self.comp_id, "info", "user", "用户重置竞赛")
            c = self.cfg.competition
            seed = {"paused": "0"}
            if isinstance(self.source, ReplaySource):
                # 新竞赛沿用当前回放位置，且与竞赛创建同一事务写入：写不进去就整体回滚、旧竞赛保持进行中。
                # 否则进程在下个 tick 之前退出，新竞赛没有回放位置，重启后会从最初的历史重新放起
                seed["replay_pos"] = json.dumps(self.source.position())
            try:
                self.db.create_competition(c.name, c.market, c.initial_cash, self._config_snapshot_json(), kv_prefixed=seed)
            except Exception as e:  # noqa: BLE001
                log.exception("Reset could not create the next competition: %s", e)
                raise ResetBlocked(PUBLIC_RESET_PERSISTENCE_FAILURE) from e
            # 中止在飞的研究会话必须等新竞赛写入成功之后：上面抛 ResetBlocked 时旧竞赛继续进行，
            # 而 aborted_restarted 是终态，提前打上去会让仍然有效的 worker 再也写不回结果——那次研究的钱白花了
            aborted = self.db.abort_running_debates(old_comp_id, "Competition reset before research debate completed")
            if aborted:
                self.db.add_event(old_comp_id, "warn", "debate", f"重置前已中止 {aborted} 场未完成的赛前研究辩论")
            self.accounts = {}
            self._account_versions = {}
            self._inflight = {}
            self._replay_bars = {}
            self._replay_retry = None   # 新竞赛账户全新，没有待结算的触发
            self._journal_replay_pos = None
            self._unsettled = set()
            self._pending_marks = {}
            self._pending_terms = {}
            self._journal_stale = set()
            self._deferred_reconcile = set()
            self._mark_save_failed = {}
            self._pending_mark_updates = {}
            self._day_start = {}
            self._metrics = {}
            self._stats = {}
            self._breaker = {tid: {"fails": 0, "skip": 0, "status": "ok", "last_error": ""} for tid in self.traders}
            self._generation += 1
            self._decision_epoch += 1
            # 旧竞赛仍在飞行中的模型调用会占用线程池直到超时：换一个新池，新竞赛的决策不必排在它们后面
            old_pool, self.pool = self.pool, self._new_pool()
            old_pool.shutdown(wait=False, cancel_futures=True)
            self._load_or_create_competition()   # 回放位置已随新竞赛写入：恢复时发现就在该位置，不会再快进一次
            self._last_decision = 0.0
            self.paused = False
            self._cursor_pending = False
            self._bump_version()

    def force_round(self):
        self._last_decision = 0.0

    # ---------- Pre-open research debate (never part of order execution) ----------
    def _preopen_candidate(self):
        """Return an exchange-calendar-derived supported pre-open phase, if any."""
        cfg = self.cfg.preopen_debate
        if not cfg.enabled or self.accelerate or self.cfg.competition.market not in DOMESTIC_MARKETS:
            return None
        try:
            now = datetime.now(self.tz)
            target, phase = self._next_research_open(now)
        except Exception as exc:  # noqa: BLE001
            log.warning("Unable to resolve next open for pre-open debate: %s", exc)
            return None
        if target is None:
            return None
        lead = (target - now).total_seconds()
        if not 0 <= lead <= cfg.lead_window_sec:
            return None
        return phase[0], phase[1], target.timestamp()

    def _next_research_open(self, now: datetime):
        """下一个**受支持的研究相位**对应的开市时刻与相位；没有则 (None, None)。

        不能只看行情源的 `next_open()`（全体品种里最早的那个）：股指午盘 13:00 开，商品 13:30 开，
        最早值在 13:00 前是 13:00、之后是股指的下一分钟，13:30 只在 13:29 那一分钟露面，午盘研究
        实际跑不了。逐个品种的开市时刻里挑最早一个落在相位上的。
        09:00 / 13:30 / 21:00 是**交易所本地**时刻，必须按交易所时区判定相位。
        competition.timezone 只是展示时区：用它换算后，上海 09:00 开盘在 UTC 下是 01:00，
        对不上任何相位、自动研究整条错过；在纽约时区下还会被错标成夜盘。调度仍用绝对时间戳。
        """
        next_opens = getattr(self.source, "next_opens", None)
        candidates = next_opens(now) if callable(next_opens) else [self.source.next_open(now)]
        targets = sorted(c.replace(tzinfo=self.tz) if c.tzinfo is None else c for c in candidates if c is not None)
        for target in targets:
            phase = phase_for_open_at(target.astimezone(self._exchange_tz))
            if phase is not None:
                return target, phase
        return None, None

    def _preopen_quotes_known_unready(self) -> bool:
        """行情源**确实报告**了自己还不能用：自报 `ok` 为假，或刷新时刻已经超窗。

        判定与真正调用模型前那道 `fresh_ok` 走同一个函数，免得两处各自演化：那里放行不了的
        情况，这里就不该先把开盘时刻预约掉。读不出来不算——那种源本来就不是这条缺陷说的
        情况（CTP 的 `last_ok` 是最后一条推送 tick 的到达时刻），把未知一律当故障会把别的
        行情源的自动研究一起挡掉。
        """
        try:
            health = self.source.health() if hasattr(self.source, "health") else None
        except Exception:  # noqa: BLE001
            return False
        return preopen_quote_readiness(health, self.cfg.preopen_debate.max_quote_age_sec) == QUOTES_UNREADY

    def _preopen_session_symbols(self, opens_at: Optional[float]) -> List[str]:
        """这次开盘时刻**真正开市**的品种。

        夜盘只有一部分品种上市。拿"全部配置品种"去要求新鲜报价的话，股指与只有日盘的品种
        在 21:00 挂着的还是 15:00 的收盘价，永远超窗，夜盘研究一次都跑不了——那是把一个
        真缺陷换成另一个。判不出来的品种不纳入要求：未知不该成为拦路的理由。
        手动研究（`opens_at is None`）没有"这次要开的盘"可以据以缩小范围，要求全部配置品种。
        """
        if opens_at is None:
            # 手动研究没有"这次要开的盘"，快照覆盖的是全部配置品种、花的也是四次模型调用，
            # 所以每个品种都要新鲜。按"此刻开市"缩小范围的话，夜盘里一个只有日盘的品种挂着
            # 下午的收盘价照样会被送进模型；休市时这个集合更是空的，等于这道闸门不存在。
            return list(self.symbols)
        else:
            # 往开盘后挪一点再问：开盘那一刻本身处在区间边界上
            at = datetime.fromtimestamp(opens_at + 60.0, self.tz)
        symbols: List[str] = []
        for symbol in self.symbols:
            try:
                if self.source.is_open(at, symbol):
                    symbols.append(symbol)
            except Exception as e:  # noqa: BLE001
                log.warning("Unable to determine opening session for %s: %s", symbol, e)
        return symbols

    def _preopen_unready_symbols(self, quotes: Dict[str, Quote], opens_at: Optional[float],
                                 *, now: Optional[float] = None, allow_prior_close: bool = False) -> List[str]:
        """这次开盘要交易、而报价不能证明自己就绪的品种：根本没有报价、超过 `max_quote_age_sec`
        没更新、时间戳读不出来、或者时间戳跑到了未来。

        没有报价也算：集合竞价里第一个报价的品种就把源级健康刷新了，其余品种还一条 tick 都没来，
        这时预约会被永久占掉，随后工作线程按覆盖度把会话记成 `unavailable`，后到的 tick 再也触发
        不了重试。所以在最后一分钟的审计底线之前，缺报价和陈旧一样都要先等。

        行情源级的 `last_ok` 证明不了这件事：CTP 是订阅推送，`last_ok` 是**任意**一个已订阅
        合约最后一条 tick 的到达时刻，集合竞价里一个品种报了价就能把它刷新，而其余品种缓存的
        已发布报价可以任意陈旧。于是自动研究照样预约、照样花四次模型调用，喂进去的是新鲜价与
        陈旧价掺在一起的一份快照。新浪源不同（一次批量请求覆盖全部品种，`last_ok` 确实是全集
        的证明），但闸门按最弱的那个源来写才是对的。

        无效或未来的时间戳同样在这里判掉：自动会话的准入只看覆盖度、源健康和这份名单，
        不会再过 `_manual_quote_only_reason` 那道校验。原来这两种情况被直接跳过，落库的历史
        bar 又能把指标补齐，于是一条畸形或未来时间的 CTP 报价照样预约开盘、照样花四次模型
        调用，而正常的实盘路径（`_live_quote_admission_reason`）会拒掉同一条报价。

        `allow_prior_close` 只给自动研究真正调用模型前那道闸门用：超窗的报价若此刻品种不在交易、
        且正好收在它最近一节已结束时段的收盘上（见 `_verified_prior_close`），就算就绪。13:30 午盘
        没有集合竞价，13:30 前商品最新的报价**只能**是 11:30 的收盘价，不放行的话午盘研究永远跑不了。
        预约前的等待不用它：9:00 / 21:00 有集合竞价，先等竞价 tick，只在最后一分钟才退到收盘价。
        """
        if not self._requires_live_quote_freshness():
            return []
        limit = float(self.cfg.preopen_debate.max_quote_age_sec)
        current = time.time() if now is None else now
        stale: List[str] = []
        for symbol in self._preopen_session_symbols(opens_at):
            quote = quotes.get(symbol)
            if quote is None:
                stale.append(symbol)
                continue
            try:
                raw_ts = quote.ts
                quote_ts = None if isinstance(raw_ts, bool) else float(raw_ts)
            except (AttributeError, TypeError, ValueError, OverflowError):
                quote_ts = None
            if (quote_ts is None or not math.isfinite(quote_ts)
                    or quote_ts > current + self.max_future_quote_skew_sec):
                stale.append(symbol)
            elif current - quote_ts > limit and not (
                    allow_prior_close and opens_at is not None
                    and self._verified_prior_close(symbol, quote_ts, current)):
                stale.append(symbol)
        return stale

    def _symbol_in_session(self, symbol: Optional[str], current: float) -> bool:
        """此刻是否在交易（`symbol=None` 问的是任一配置品种）。判不出来按"在交易"算：
        这个结论只用来放宽新鲜度要求，未知不能成为放宽的理由。"""
        try:
            return bool(self.source.is_open(datetime.fromtimestamp(current, self.tz), symbol))
        except Exception as e:  # noqa: BLE001
            log.warning("Unable to determine whether %s is in session: %s", symbol or "any symbol", e)
            return True

    def _verified_prior_close(self, symbol: str, quote_ts: float, current: float) -> bool:
        """这条超窗的报价是不是该品种**最近一节已结束时段的收盘**。

        与行情源 `closed_session_quotes()` 同一条界限：落在收盘前 CLOSED_SESSION_QUOTE_GRACE_SEC 之内
        （清淡品种收盘前几分钟可能没有更新），最多比收盘晚 PRIOR_CLOSE_LATE_TICK_SEC。此刻正在交易的
        品种不适用——它的旧报价就是陈旧，不是收盘价；停在 10:00 的是盘中价，停在上一个交易日的说明中间
        漏了一节，都不是。
        """
        spec = self.specs.get(symbol)
        if spec is None or not spec.sessions or self._symbol_in_session(symbol, current):
            return False
        try:
            close_ts = latest_session_close(spec, current, self._exchange_tz, self.holidays)
        except Exception as e:  # noqa: BLE001
            log.warning("Unable to resolve the last session close for %s: %s", symbol, e)
            return False
        return (close_ts is not None
                and close_ts - CLOSED_SESSION_QUOTE_GRACE_SEC <= quote_ts <= close_ts + PRIOR_CLOSE_LATE_TICK_SEC)

    def _preopen_source_ready(self, health: object, opens_at: Optional[float], *, now: Optional[float] = None) -> bool:
        """自动研究调用模型前的源级闸门。

        CTP 是推送式的：午休里没有任何品种在交易，`last_ok`（最后一条 tick 的到达时刻）必然停在
        11:30，超过 `max_quote_age_sec`，午盘研究在源级就被挡死。所以只在**此刻没有任何配置品种
        在交易**时不看 `last_ok` 的年龄——没有交易就不该有推送。源自报的 `ok`（已连接、已登录）
        照旧必须为真，逐品种的报价另有 `_preopen_unready_symbols` 核验收盘。手动研究不放宽。
        """
        limit = self.cfg.preopen_debate.max_quote_age_sec
        if preopen_quote_readiness(health, limit, now=now) == QUOTES_FRESH:
            return True
        if opens_at is None or not isinstance(health, dict) or not health.get("ok"):
            return False
        current = time.time() if now is None else now
        if self._symbol_in_session(None, current):
            return False
        return preopen_quote_readiness(health, math.inf, now=now) == QUOTES_FRESH

    def _maybe_start_preopen_debate(self):
        if self.paused or self._stop.is_set():
            return False
        candidate = self._preopen_candidate()
        if candidate is None:
            return False
        phase, label, opens_at = candidate
        # 一次尝试（成功/不可用/不完整）都是这个开盘时刻的**永久预约**，之后不再重试。
        # 所以行情还没就绪时先别占坑，两种"没就绪"都算：CTP 的 last_ok 是最后一条推送 tick
        # 的到达时刻，收盘之后必然已经超过 max_quote_age_sec；而一个盘前刚断线的源 ok 已经
        # 是假、缓存的 last_ok 却还在窗口内。任何一种在 30 分钟前置窗口一开始就尝试，都等于
        # 把"不可用"钉死，集合竞价的 tick 真到了、或者重连成功了也不会再跑。窗口只剩最后
        # 一分钟时仍然尝试一次，好让"始终没等到行情"留下审计记录，而不是悄无声息地跳过。
        lead = opens_at - time.time()
        if lead > STALE_PREOPEN_RETRY_FLOOR_SEC:
            if self._preopen_quotes_known_unready():
                return False
            # 源级就绪不代表每个要开盘的品种都有新鲜价（CTP 的 last_ok 只是"某个合约刚报过价"）。
            # 这一条与真正调模型前那道闸门判的是同一件事，不先在这儿等，预约同样会被白白烧掉。
            with self.lock:
                quotes = dict(self.quotes)
            if self._preopen_unready_symbols(quotes, opens_at):
                return False
        session_key = f"auto:{phase}:{int(opens_at)}"
        try:
            if self.db.debate_session_by_key(self.comp_id, session_key) is not None:
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("Unable to check pre-open debate reservation: %s", exc)
            return False
        return self._start_debate(phase, label, opens_at, trigger="automatic")

    def start_manual_debate(self) -> tuple[bool, str]:
        """Start an explicit, research-only run from the local dashboard."""
        if self._stop.is_set() or not self.running:
            return False, "引擎未运行，未发起研究辩论"
        paused = self.paused
        started = self._start_debate("manual_research", "手动赛前研究", None, trigger="manual")
        if not started:
            return False, "已有研究辩论正在进行"
        return True, "已开始统一快照研究辩论（竞赛保持暂停）" if paused else "已开始统一快照研究辩论"

    def _product_research_participants(self) -> List[dict]:
        """Expose only display metadata; no account, position, or credential reaches research."""
        return [
            {"id": cfg.id, "name": cfg.name, "color": cfg.color}
            for cfg in self.cfg.traders
            if cfg.id in self.traders
        ]

    def _is_research_participant(self, trader_id: str) -> bool:
        """能参加研究辩论的实时模型：文本席位，或走类型化适配的 Jev 席位。规则交易员不算。"""
        return isinstance(self.traders.get(trader_id), (LlmTrader, JevTrader))

    def _product_research_readiness(self) -> dict:
        """Require exactly four live LLMs before any full-universe paid work.

        Normal competition mode may intentionally fall back to a rule trader.
        Research debate must never do that: a three-model result plus one rule
        response is not a four-model debate, and charging the three live models
        would make the incomplete result needlessly expensive.
        """
        configured = [cfg.id for cfg in self.cfg.traders]
        unavailable = [cfg.id for cfg in self.cfg.traders if not self._is_research_participant(cfg.id)]
        return {
            "ready": len(configured) == 4 and not unavailable,
            "configured_count": len(configured),
            "unavailable": unavailable,
        }

    def _call_product_research_model(self, trader_id: str, prompt: str, stage: str,
                                     valid_fact_ids: set[str], participant_ids: set[str]) -> Dict[str, object]:
        return self._call_debate_model(
            trader_id, prompt, stage, valid_fact_ids, participant_ids,
            timeout_sec=float(self.cfg.product_research.stage_timeout_sec),
            max_output_tokens=int(self.cfg.product_research.max_output_tokens),
        )

    def start_product_research(self, *, sample_limit: Optional[int] = None) -> tuple[bool, str, Optional[int]]:
        """Start the full-universe product queue; it is separate from trading and risk limits."""
        readiness = self._product_research_readiness()
        if not readiness["ready"]:
            unavailable = ",".join(readiness["unavailable"]) or f"configured:{readiness['configured_count']}"
            return False, f"全品种论证要求恰好四个真实模型，当前未就绪：{unavailable}", None
        resumable = self.db.latest_resumable_product_research_run()
        if resumable:
            allowed, _ = self._product_research_resume_allowed(int(resumable["id"]))
            if allowed:
                return (False, "已有可安全续跑的全品种研究运行；请续跑以避免重复调用", int(resumable["id"]))
        return self.product_research.start("manual", sample_limit=sample_limit)

    def resume_product_research(self, run_id: int) -> tuple[bool, str]:
        allowed, message = self._product_research_resume_allowed(run_id)
        if not allowed:
            return False, message
        readiness = self._product_research_readiness()
        if not readiness["ready"]:
            unavailable = ",".join(readiness["unavailable"]) or f"configured:{readiness['configured_count']}"
            return False, f"全品种论证要求恰好四个真实模型，当前未就绪：{unavailable}"
        return self.product_research.resume(run_id)

    def retry_failed_product_research(self, run_id: int) -> tuple[bool, str]:
        allowed, message = self._product_research_resume_allowed(run_id)
        if not allowed:
            return False, message
        readiness = self._product_research_readiness()
        if not readiness["ready"]:
            unavailable = ",".join(readiness["unavailable"]) or f"configured:{readiness['configured_count']}"
            return False, f"全品种论证要求恰好四个真实模型，当前未就绪：{unavailable}"
        return self.product_research.retry_failed(run_id)

    def retry_manual_debate(self, debate_id: int) -> tuple[bool, str]:
        """Create one fresh, research-only retry for the latest recoverable session.

        The failed session remains immutable.  This method never resumes a
        partial stage or copies its snapshot: the normal manual worker creates
        a new snapshot and runs the full-universe sidecar again.  Keeping the
        status/id check beside the single-flight launch gate prevents a stale
        history card or a double-click from creating an extra paid study.
        """
        if isinstance(debate_id, bool) or not isinstance(debate_id, int) or debate_id <= 0:
            return False, "研究记录无效，未发起重试"
        with self.lock:
            if self._stop.is_set() or not self.running:
                return False, "引擎未运行，未发起研究重试"
            latest = self.db.latest_debate_session(self.comp_id)
            if not latest or int(latest.get("id") or 0) != debate_id:
                return False, "只能重新生成当前最新的一次研究"
            if str(latest.get("status") or "") not in RETRYABLE_DEBATE_STATUSES:
                return False, "当前研究状态不支持重新生成"
            paused = self.paused
            label = f"手动赛前研究（重试 #{debate_id}）"
            # self.lock is an RLock: validation and _debate_thread assignment
            # remain one atomic boundary even when two local browser requests
            # arrive together.
            started = self._start_debate("manual_research", label, None, trigger="manual")
        if not started:
            return False, "已有研究辩论正在进行"
        message = "已重新创建全量候选研究（竞赛保持暂停）" if paused else "已重新创建全量候选研究"
        return True, message

    def _start_debate(self, phase: str, label: str, opens_at: Optional[float], *, trigger: str) -> bool:
        with self.lock:
            allow_paused = trigger == "manual" and self.paused
            if (self.paused and not allow_paused) or self._stop.is_set():
                return False
            if self._debate_thread is not None and self._debate_thread.is_alive():
                return False
            comp_id, generation, decision_epoch = self.comp_id, self._generation, self._decision_epoch
            self._debate_thread = threading.Thread(
                target=self._run_preopen_debate,
                args=(comp_id, generation, decision_epoch, phase, label, opens_at, trigger, allow_paused),
                name="arena-preopen-debate",
                daemon=True,
            )
            self._debate_thread.start()
        # 事件写进 worker 捕获的竞赛，不是发事件这一刻的 self.comp_id：锁已经放开，
        # 重置可能恰好插在这中间，那样新竞赛的事件流里会冒出一条它从未启动过的研究。
        # worker 的终态事件早已按这个口径走，启动事件是同一不变量上最后一处漏网。
        self._event_best_effort("info", "debate", f"{label}研究辩论已启动（{trigger}）", comp_id=comp_id)
        return True

    def _debate_is_current(self, comp_id: int, generation: int, decision_epoch: int, *, allow_paused: bool = False) -> bool:
        with self.lock:
            return (not self._stop.is_set() and (allow_paused or not self.paused) and self.comp_id == comp_id
                    and self._generation == generation and self._decision_epoch == decision_epoch)

    def _incomplete_evidence(self, snap) -> Optional[str]:
        """完整证据 = 每个参赛品种都有可用的 5m / 15m / 1h 指标。缺什么就说明白，全齐则返回 None。

        `_build_snapshot` 只要求任意一个品种拿到 5m K 线就返回快照，所以"非 None"远不等于"证据完整"。
        """
        missing: List[str] = []
        gaps: List[str] = []
        for symbol in self.symbols:
            view = snap.symbols.get(symbol)
            if view is None:
                missing.append(symbol)
                continue
            lacking = [period for period, ind in (("5m", view.ind_5m), ("15m", view.ind_15m), ("1h", view.ind_1h))
                       if not has_usable_indicators(ind, period)]
            if lacking:
                gaps.append(f"{symbol} 缺 {'/'.join(lacking)}")
        parts = []
        if missing:
            parts.append("无行情：" + "、".join(missing))
        if gaps:
            parts.append("指标不全：" + "、".join(gaps))
        return "；".join(parts) if parts else None

    def _preopen_universe_reader_instance(self) -> SinaResearchReader:
        """Return the process-lifetime public reader used only by pre-open scans."""
        with self._preopen_universe_reader_lock:
            if self._preopen_universe_reader is None:
                self._preopen_universe_reader = SinaResearchReader(
                    timeout_sec=float(self.cfg.market_data.cn_futures.timeout_sec),
                    tz="Asia/Shanghai",
                )
            return self._preopen_universe_reader

    def _preopen_universe_research(self, comp_id: int, generation: int, decision_epoch: int,
                                   *, allow_paused: bool) -> Optional[Dict[str, object]]:
        """Scan the full candidate catalogue without changing the execution pool.

        The scanner has its own reader and never sees ``self.symbols``, contract
        terms, accounts, orders, risk, or the simulation exchange.  A failure is
        converted into an auditable unavailable projection; it never falls back
        to the eight-symbol execution manifest while claiming full coverage.
        """
        # CTP is only the execution feed; the scanner reads the public catalogue
        # through its own reader, so a domestic CTP competition gets the same scan.
        if self.cfg.competition.market not in DOMESTIC_MARKETS:
            return None

        class _DebateCancellation:
            """Expose the debate lifecycle as the scanner's narrow cancellation API."""

            def is_set(_self) -> bool:
                return not self._debate_is_current(
                    comp_id, generation, decision_epoch, allow_paused=allow_paused,
                )

        catalogue_dir = self.cfg.resolve("data") / "preopen-catalogues"
        catalogue_path = catalogue_dir / f"{uuid.uuid4().hex}.json"
        try:
            # The committed catalogue is an audit/reference artifact.  It must
            # not become a live research denominator: the upstream source can
            # remove or add a continuous symbol between commits.  Freeze a
            # fresh source-supported catalogue for every pre-open debate, just
            # as the per-product research lane already does.
            discover_catalogue(
                catalogue_path,
                timeout_sec=self.cfg.product_research.discovery_timeout_sec,
                workers=self.cfg.product_research.discovery_workers,
            )
            raw_manifest = FullUniverseScanner(
                catalogue_path,
                self._preopen_universe_reader_instance(),
                calendar_path=self.cfg.resolve(self.cfg.market_data.cn_futures.contracts_file),
                cancel_event=_DebateCancellation(),
            ).run()
        except Exception as exc:  # noqa: BLE001 - external details must not reach a public research session
            log.warning("Pre-open full-universe scan failed: %s", type(exc).__name__)
            raw_manifest = FullUniverseScanner.failed_manifest(catalogue_path)
        return build_preopen_universe_research(raw_manifest)

    def _closed_session_research_quotes(self, quotes: Dict[str, Quote], opens_at: Optional[float],
                                        *, now: Optional[float] = None) -> Dict[str, Quote]:
        """自动研究：这次开盘**不交易**的品种，报价一律换成行情源核验过的上一节收盘价。

        这些品种的新鲜度闸门是不查的（21:00 时股指挂的本来就是 15:00 的收盘价），所以它们的
        报价必须由另一道校验担保——"收在最近一节的收盘上"。两种情况都归它管：
        - 午后重启后只有预热历史、没有实时 tick：`latest()` 按设计不返回，缺席会让混合日夜盘的
          品种表永远出不了 21:00 的研究，由行情源用库里最后那根 bar 补上；
        - 进程一直开着、但行情源在收盘前就断了：`latest()` 缓存里挂着一条 14:00 的报价，它不能
          因为"在缓存里"就免检，否则一个盘中价会被当成收盘价送进四次付费的模型调用。
        行情源给不出核验过的收盘的品种，从这份快照里去掉，让研究按"无行情"失败关闭。
        换上的报价只进这份研究快照，不写 `self.quotes`、不进盯市与风控；这次要开盘的品种照旧
        必须有本进程收到的实时报价（新鲜，或在调模型前的闸门里核验为收在上一节收盘上），不拿预热
        的历史充数；手动研究（`opens_at is None`）要求全部品种新鲜，两者都不从这里拿。
        """
        reader = getattr(self.source, "closed_session_quotes", None)
        if opens_at is None or reader is None:
            return quotes
        opening = set(self._preopen_session_symbols(opens_at))
        closed = [symbol for symbol in self.symbols if symbol not in opening]
        if not closed:
            return quotes
        try:
            validated = reader(closed, now=now) if now is not None else reader(closed)
        except Exception as exc:  # noqa: BLE001
            # 核验不了就当没有：缓存里那条未经核验的报价不能顶上
            log.warning("Closed-session research quotes unavailable: %s", exc)
            validated = {}
        merged = {symbol: quote for symbol, quote in quotes.items() if symbol not in closed}
        merged.update({symbol: quote for symbol, quote in validated.items() if symbol in closed})
        rejected = [symbol for symbol in closed if symbol in quotes and symbol not in validated]
        if rejected:
            log.warning("Cached quotes for %s stop short of their last session close; research treats them as missing",
                        ", ".join(rejected))
        return merged

    def _preopen_snapshot(self, round_no: int, *, allow_quote_only: bool = False,
                          opens_at: Optional[float] = None, now: Optional[float] = None):
        """Fetch a fresh account-free snapshot even though the market is still closed.

        Scheduled research requires the same multi-timeframe evidence as an
        execution decision. A user-triggered research-only check may explicitly
        fall back to fresh quotes when the history endpoint is down; the manifest
        labels that limitation and it never reaches the execution path.

        Research only reads the market, so it must never advance the replay
        clock: ``ReplaySource.latest()`` advances one bar, and a bar produced
        here is never marked to market (no stop-loss, take-profit or
        liquidation is evaluated against it) nor written to ``replay_pos``.
        The next tick advances again and skips it outright, so merely pressing
        the research button would corrupt the competition. Take the
        non-advancing ``snapshot()`` path instead, and carry its raw bars into
        the snapshot so quote and indicators come from one single revision —
        a concurrent tick must not be able to advance between the two reads.
        """
        replay_bars: Optional[Dict[str, List[Bar]]] = None
        try:
            if isinstance(self.source, ReplaySource):
                quotes, replay_bars = self.source.snapshot(list(self.symbols))
            else:
                quotes = self.source.latest(list(self.symbols))
                quotes = self._closed_session_research_quotes(quotes, opens_at, now=now)
        except Exception as exc:  # noqa: BLE001
            log.warning("Pre-open quote fetch failed: %s", exc)
            return None, {"source": getattr(self.source, "name", "market"), "ok": False, "detail": str(exc)[:200]}
        snap = self._build_snapshot(quotes=quotes, round_no=round_no, replay_bars=replay_bars, symbols=self.symbols)
        try:
            health = self.source.health()
        except Exception:  # noqa: BLE001
            health = {"source": getattr(self.source, "name", "market"), "ok": False}
        if snap is not None:
            gap = self._incomplete_evidence(snap)
            if gap is None:
                return snap, {**health, "history_quality": "full"}
            # 调度研究要求与执行决策同样的多周期证据：缺就不能叫 full，也不能悄悄按 full 去调模型。
            # 只有用户显式触发的手动研究才允许降级到下面那条 quote_only 通道，其余情况失败关闭。
            log.warning("Pre-open snapshot lacks full evidence: %s", gap)
            snap, health = None, {**health, "history_quality": "incomplete", "detail": gap}
        if (allow_quote_only and self._requires_live_quote_freshness()
                and self._manual_quote_only_reason(quotes, health) is None):
            views = {
                symbol: SymbolView(symbol=symbol, spec=self.specs[symbol], quote=quotes[symbol],
                                   ind_5m={}, ind_15m={}, ind_1h={}, bars_5m=[])
                for symbol in self.symbols
                if symbol in self.specs and symbol in quotes
            }
            if len(views) == len(self.symbols):
                snap = MarketSnapshot(ts=time.time(), round_no=round_no, market=self.cfg.competition.market,
                                      currency=self.currency, symbols=views)
                health = {
                    **health,
                    "history_quality": "quote_only",
                    "detail": "分钟与小时K线暂不可用；本次手动研究仅使用新鲜即时行情。",
                }
        return snap, health

    def _call_debate_model(self, trader_id: str, prompt: str, stage: str, valid_fact_ids: set[str],
                           participant_ids: set[str], *, timeout_sec: Optional[float] = None,
                           max_output_tokens: Optional[int] = None) -> Dict[str, object]:
        trader = self.traders.get(trader_id)
        started = time.time()
        if not isinstance(trader, (LlmTrader, JevTrader)):
            return {"status": "failed", "error": "ResearchParticipantUnavailable",
                    "prompt": prompt, "latency_ms": 0}
        try:
            # The stage deadline is a real billable-work boundary, not merely a
            # UI timeout.  Leave a small margin for result persistence and use no
            # retry loop in LlmTrader.research().
            request_timeout = min(
                self.cfg.llm.timeout_sec,
                max(1.0, (float(timeout_sec) if timeout_sec is not None
                          else float(self.cfg.preopen_debate.stage_timeout_sec)) - 3.0),
            )
            output_tokens = int(max_output_tokens) if max_output_tokens is not None else self.cfg.preopen_debate.max_output_tokens
            if isinstance(trader, JevTrader):
                # Jev 不产出文字：立场、依据与反证对象按类型化问题判断，文本由代码组装成同一份 JSON 契约，
                # 下面照样经 parse_opening / parse_rebuttal 校验。
                try:
                    result = trader.research_turn(stage, prompt, valid_fact_ids, participant_ids,
                                                  timeout_sec=request_timeout)
                except ValueError as exc:
                    # 已经送达、但响应体或类型化答案不合契约：与文本席位的结构校验失败同一类，
                    # 不是"投递未知"。
                    return {"status": "failed", "error": self._classify_debate_response_invalid(exc),
                            "prompt": prompt, "latency_ms": int((time.time() - started) * 1000)}
            else:
                result = trader.research(RESEARCH_SYSTEM_PROMPT, prompt, output_tokens, timeout_sec=request_timeout)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": self._classify_debate_request_failure(exc), "prompt": prompt,
                    "latency_ms": int((time.time() - started) * 1000)}
        try:
            if stage == "opening":
                payload = parse_opening(result.text, valid_fact_ids)
            else:
                payload = parse_rebuttal(result.text, valid_fact_ids, participant_ids, trader_id)
            return {"status": "complete", "payload": payload, "raw_response": result.text, "prompt": prompt,
                    "latency_ms": int((time.time() - started) * 1000), "tokens_in": result.tokens_in,
                    "tokens_out": result.tokens_out}
        except ValueError as exc:
            return {"status": "failed", "error": self._classify_debate_response_invalid(exc), "prompt": prompt,
                    "latency_ms": int((time.time() - started) * 1000)}
        except Exception:  # noqa: BLE001
            return {"status": "failed", "error": "ResearchRequestFailed", "prompt": prompt,
                    "latency_ms": int((time.time() - started) * 1000)}

    @staticmethod
    def _classify_debate_request_failure(exc: Exception) -> str:
        """Return a fixed code without retaining provider messages or metadata."""
        if isinstance(exc, TimeoutError):
            return "ResearchRequestTimedOut"
        try:
            from openai import APIConnectionError, APITimeoutError
        except ImportError:  # The demo path keeps the SDK import lazy.
            APIConnectionError = APITimeoutError = ()  # type: ignore[assignment]
        if APITimeoutError and isinstance(exc, APITimeoutError):
            # APITimeoutError is also an APIConnectionError, so it must precede
            # the broader connection test.
            return "ResearchRequestTimedOut"
        if isinstance(exc, (ConnectionError, OSError)) or (APIConnectionError and isinstance(exc, APIConnectionError)):
            return "ResearchConnectionFailed"
        # Jev 席位走 httpx：超时与连接失败要和 openai 的同类归到同一码（全品种研究据此判"投递未知"、
        # 不盲目重发付费请求），状态码放在 `response.status_code` 上。
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx 是运行依赖
            httpx = None  # type: ignore[assignment]
        if httpx is not None:
            if isinstance(exc, httpx.TimeoutException):
                return "ResearchRequestTimedOut"
            if isinstance(exc, httpx.TransportError):
                return "ResearchConnectionFailed"
        if LlmTrader._is_credit_exhausted_error(exc):
            return "ResearchQuotaExhausted"
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if type(status_code) is int:
            if status_code in {401, 403}:
                return "ResearchAuthorizationFailed"
            if status_code == 429:
                return "ResearchRateLimited"
            if 400 <= status_code < 500:
                return "ResearchUpstream4xx"
            if 500 <= status_code < 600:
                return "ResearchUpstream5xx"
        return "ResearchRequestFailed"

    @staticmethod
    def _classify_debate_response_invalid(exc: ValueError) -> str:
        """Classify only locally generated validation reasons; never persist model text.

        The browser receives a fixed safe category for each result.  The
        narrower internal codes make the next retry auditable without retaining
        a provider response, parser exception text, prompts, credentials, or
        hidden facts.
        """
        if isinstance(exc, json.JSONDecodeError):
            return "ResearchResponseMalformedJson"
        detail = str(exc)
        if ("fact id" in detail or "fact ids" in detail or "valid fact ids" in detail):
            return "ResearchResponseInvalidFactCitation"
        if "target_trader_id" in detail:
            return "ResearchResponseInvalidTarget"
        if "required" in detail:
            return "ResearchResponseMissingRequiredField"
        if "stance" in detail or "confidence" in detail or "JSON object" in detail:
            return "ResearchResponseInvalidSchema"
        return "ResearchResponseInvalid"

    def _run_debate_stage(self, comp_id: int, generation: int, decision_epoch: int, debate_id: int,
                          stage: str, participants: List[str], prompts: Dict[str, str], valid_fact_ids: set[str],
                          *, allow_paused: bool = False) -> tuple[List[dict], bool]:
        """Run one concurrent stage; all completed/failed turns are persisted by configured order."""
        if not self.db.set_debate_session_status(debate_id, stage, only_active=True):
            return [], True
        self._bump_version()
        pool = ThreadPoolExecutor(max_workers=max(1, len(participants)), thread_name_prefix=f"arena-debate-{stage}")
        participant_ids = set(participants)
        completed: Dict[str, Dict[str, object]] = {}
        deadline = time.time() + self.cfg.preopen_debate.stage_timeout_sec
        aborted = False
        try:
            # Keep cancellation validation and billable submission atomic.
            # pause()/reset() are serialized by self.lock; stop() is serialized
            # by the narrow submit gate so it can remain prompt if self.lock is
            # otherwise occupied by persistence work.
            with self.lock:
                with self._research_submit_lock:
                    if not self._debate_is_current(comp_id, generation, decision_epoch, allow_paused=allow_paused):
                        return [], True
                    futures = {
                        pool.submit(self._call_debate_model, trader_id, prompts[trader_id], stage,
                                    valid_fact_ids, participant_ids): trader_id
                        for trader_id in participants
                    }
            pending = dict(futures)
            while pending:
                if not self._debate_is_current(comp_id, generation, decision_epoch, allow_paused=allow_paused):
                    aborted = True
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                done, _ = wait(pending, timeout=min(1.0, remaining), return_when=FIRST_COMPLETED)
                for future in done:
                    trader_id = pending.pop(future)
                    try:
                        completed[trader_id] = future.result()
                    except Exception:  # noqa: BLE001
                        completed[trader_id] = {"status": "failed", "error": "ResearchWorkerFailed",
                                                "prompt": prompts[trader_id], "latency_ms": 0}
            for future, trader_id in list(pending.items()):
                future.cancel()
                completed.setdefault(trader_id, {"status": "failed", "error": "ResearchStageTimedOut",
                                                  "prompt": prompts[trader_id], "latency_ms": self.cfg.preopen_debate.stage_timeout_sec * 1000})
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if aborted:
            return [], True
        # 收完最后一个 future 到这里之间仍可能发生暂停/重置/停止：那时 abort_running_debates() 已把会话定成终态，
        # 再写回合等于让"已中止"的归档记录继续长出模型输出。先复查一次生命周期，
        # 落库本身再按"会话是否仍然活跃"做 CAS，兜住复查与写入之间剩下的那点窗口。
        if not self._debate_is_current(comp_id, generation, decision_epoch, allow_paused=allow_paused):
            return [], True
        rows: List[dict] = []
        for ordinal, trader_id in enumerate(participants, start=1):
            result = completed.get(trader_id) or {"status": "failed", "error": "ResearchWorkerFailed", "prompt": prompts[trader_id], "latency_ms": 0}
            status = "complete" if result.get("status") == "complete" else "failed"
            payload = result.get("payload") if status == "complete" and isinstance(result.get("payload"), dict) else {}
            row = {**payload, "stage": stage, "trader_id": trader_id, "ordinal": ordinal, "status": status,
                   "error": str(result.get("error") or ""), "raw_response": str(result.get("raw_response") or ""),
                   "prompt": str(result.get("prompt") or ""), "latency_ms": result.get("latency_ms"),
                   "tokens_in": result.get("tokens_in"), "tokens_out": result.get("tokens_out")}
            if not self.db.add_debate_turn(debate_id, stage, trader_id, ordinal, str(row["status"]), payload,
                                           row["raw_response"], row["prompt"], row["latency_ms"], row["tokens_in"],
                                           row["tokens_out"], row["error"], only_active=True):
                log.info("Research session %s went terminal mid-stage; stopped appending turns", debate_id)
                return [], True
            rows.append(row)
        self._bump_version()
        return rows, False

    @property
    def _exchange_tz(self) -> ZoneInfo:
        """交易所时区。相位表 09:00 / 13:30 / 21:00 是交易所本地时刻，
        `competition.timezone` 只管展示——调度判定与看板投影都必须用这一个口径，不能各取各的。"""
        return getattr(self.source, "tz", None) or ZoneInfo(LEGACY_COMPETITION_TIMEZONE)

    def _preopen_window_expired(self, trigger: str, opens_at: Optional[float]) -> bool:
        """自动盘前研究是否已经错过了它所针对的开盘时刻。

        `_preopen_candidate()` 是在开盘前接下这单的，但引擎恰在开盘点附近启动、或行情历史请求把快照构建拖久了，
        真正发出付费调用时可能已经开盘：那批调用会被归档成一场"盘前"会话（名不副实），
        还要和正常决策轮抢同一批供应商额度。手动研究由用户显式发起，不受此限。
        """
        return trigger == "automatic" and opens_at is not None and time.time() >= float(opens_at)

    def _run_preopen_debate(self, comp_id: int, generation: int, decision_epoch: int, phase: str, label: str,
                             opens_at: Optional[float], trigger: str, allow_paused: bool = False):
        """Persist a two-stage four-model research debate, or an auditable incomplete record."""
        debate_id: Optional[int] = None
        turns: List[dict] = []

        def finish(status: str, preliminary: Optional[Dict[str, object]] = None, packet_text: str = "",
                   error: str = "", event: Optional[tuple] = None) -> bool:
            """写终态，并把"发事件"和"推版本"都挂在终态 CAS 的结果上。

            CAS 没命中意味着重置/停止已经先一步把这场会话定成终态并记下了真正的原因；此时再发一条
            "因证据不足未调用模型"之类的事件，归档竞赛里就留下一条与会话状态自相矛盾的记录。
            反过来，命中却不推版本，看板可能一直停在"正在开场"——两件事因此共用同一个返回值。
            所有终态写入都走这里，避免以后新增分支时又漏掉其中一件。
            """
            took = self.db.complete_debate_session(debate_id, status, preliminary, packet_text, error,
                                                   only_active=True)
            if took:
                if event is not None:
                    self._event_best_effort(event[0], "debate", event[1], comp_id=comp_id)
                self._bump_version()
            return took

        try:
            snap, health = self._preopen_snapshot(self.round_no, allow_quote_only=(trigger == "manual"),
                                                  opens_at=opens_at)
            if snap is None:
                self._event_best_effort("warn", "debate", f"{label}未生成：无法取得完整行情快照", comp_id=comp_id)
                return
            if not self._debate_is_current(comp_id, generation, decision_epoch, allow_paused=allow_paused):
                return
            universe_research = self._preopen_universe_research(
                comp_id, generation, decision_epoch, allow_paused=allow_paused,
            )
            if universe_research is not None and not self._debate_is_current(
                    comp_id, generation, decision_epoch, allow_paused=allow_paused):
                return
            manifest = build_market_manifest(snap, health, universe_research=universe_research)
            session_key = (f"auto:{phase}:{int(opens_at or 0)}" if trigger == "automatic"
                           else f"manual:{int(time.time() * 1000)}")
            # 快照和全量扫描都在引擎锁外。pause/reset 抢 self.lock，stop 抢提交门闩。
            # 复查与预占必须同时拿住这两把锁：扫描期间的生命周期切换不能给已归档或已停止的竞赛挂上会话。
            with self.lock:
                with self._research_submit_lock:
                    if not self._debate_is_current(comp_id, generation, decision_epoch, allow_paused=allow_paused):
                        return
                    debate_id = self.db.reserve_debate_session(comp_id, session_key, phase, label, opens_at, manifest)
            if debate_id is None:
                return
            session = {"phase": phase, "phase_label": label, "status": "running", "analysis_id": manifest["analysis_id"],
                       "snapshot_hash": manifest["snapshot_hash"], "market_as_of": manifest.get("market_as_of"),
                       "snapshot": manifest, "global_analysis": manifest.get("global_analysis") or {}}
            if universe_research is not None and universe_research.get("scope_status") != "complete":
                # Full-universe coverage is a hard research admission gate.  Do
                # not call four models on the eight execution symbols and label
                # their result a global candidate-universe conclusion.
                session["status"] = "unavailable"
                packet = referee_packet(session, turns, {t.id: t.name for t in self.cfg.traders})
                finish("unavailable", {"status": "unavailable", "label": "全量候选研究未完整，未调用模型", "analysis_only": True},
                       packet, "FullUniverseResearchIncomplete",
                       event=("warn", f"{label}未调用模型：全量候选研究覆盖不完整"))
                return
            missing = sorted(set(self.symbols) - set(snap.symbols))
            fresh_ok = self._preopen_source_ready(health, opens_at)
            stale = sorted(self._preopen_unready_symbols({s: v.quote for s, v in snap.symbols.items()}, opens_at,
                                                         allow_prior_close=True))
            if missing or not fresh_ok or stale:
                if missing:
                    why = f"行情覆盖不足：缺少 {','.join(missing)}"
                elif not fresh_ok:
                    why = "行情来源未在允许窗口内成功刷新"
                else:
                    shown = "，".join(stale[:5]) + (f" 等 {len(stale)} 个品种" if len(stale) > 5 else "")
                    why = f"本次开盘要交易的品种报价未在允许窗口内更新或时间戳无效：{shown}"
                session["status"] = "unavailable"
                packet = referee_packet(session, turns, {t.id: t.name for t in self.cfg.traders})
                finish("unavailable", {"status": "unavailable", "label": "证据不足，未调用模型", "analysis_only": True},
                       packet, why, event=("warn", f"{label}未调用模型：{why}"))
                return
            if not self._debate_is_current(comp_id, generation, decision_epoch, allow_paused=allow_paused):
                finish("aborted", error="Competition paused, reset, or stopped before model calls")
                return
            participants = [trader.id for trader in self.cfg.traders]
            unavailable = [trader_id for trader_id in participants if not self._is_research_participant(trader_id)]
            if len(participants) != 4 or unavailable:
                why = ("Research debate requires exactly four live LLM participants; unavailable="
                       + ",".join(unavailable or [f"configured:{len(participants)}"]))
                session["status"] = "unavailable"
                packet = referee_packet(session, turns, {t.id: t.name for t in self.cfg.traders})
                finish("unavailable",
                       {"status": "unavailable", "label": "四个真实模型未全部就绪，未调用模型", "analysis_only": True},
                       packet, why, event=("warn", f"{label}未调用模型：四个真实模型未全部就绪"))
                return
            if self._preopen_window_expired(trigger, opens_at):
                why = "已过开盘时间：盘前研究不在盘中发起，未调用模型"
                session["status"] = "unavailable"
                packet = referee_packet(session, turns, {t.id: t.name for t in self.cfg.traders})
                finish("unavailable", {"status": "unavailable", "label": "已过开盘时间，未调用模型", "analysis_only": True},
                       packet, why, event=("warn", f"{label}未调用模型：已过开盘时间"))
                return
            opening = opening_prompt(manifest, phase, label, opens_at)
            prompts = {trader_id: opening for trader_id in participants}
            valid_fact_ids = {fact["id"] for fact in manifest.get("facts", []) if isinstance(fact, dict) and fact.get("id")}
            opening_rows, aborted = self._run_debate_stage(
                comp_id, generation, decision_epoch, debate_id, "opening", participants, prompts, valid_fact_ids,
                allow_paused=allow_paused,
            )
            turns.extend(opening_rows)
            if aborted or not self._debate_is_current(comp_id, generation, decision_epoch, allow_paused=allow_paused):
                finish("aborted", error="Competition paused, reset, or stopped during opening stage")
                return
            if len(opening_rows) != len(participants) or any(row["status"] != "complete" for row in opening_rows):
                session["status"] = "incomplete"
                packet = referee_packet(session, turns, {t.id: t.name for t in self.cfg.traders})
                finish("incomplete", {"status": "incomplete", "label": "四模型开场未完整，未进入反证/仲裁", "analysis_only": True},
                       packet, "Opening stage did not receive four valid, fact-cited responses",
                       event=("warn", f"{label}开场不完整，未进入反证"))
                return
            frozen_openings = [{key: row.get(key) for key in ("trader_id", "stance", "confidence", "thesis", "supporting_fact_ids", "counter_argument", "invalidation")}
                               for row in opening_rows]
            if self._preopen_window_expired(trigger, opens_at):
                # 开场跑完期间开盘了：已经调用过模型，所以是 incomplete 而不是 unavailable
                session["status"] = "incomplete"
                packet = referee_packet(session, turns, {t.id: t.name for t in self.cfg.traders})
                finish("incomplete", {"status": "incomplete", "label": "开场后已过开盘时间，未进入反证", "analysis_only": True},
                       packet, "Market opened before the rebuttal stage could start",
                       event=("warn", f"{label}已过开盘时间，未进入反证"))
                return
            rebuttal_prompts = {trader_id: rebuttal_prompt(manifest, phase, label, frozen_openings, trader_id) for trader_id in participants}
            rebuttal_rows, aborted = self._run_debate_stage(
                comp_id, generation, decision_epoch, debate_id, "rebuttal", participants, rebuttal_prompts, valid_fact_ids,
                allow_paused=allow_paused,
            )
            turns.extend(rebuttal_rows)
            if aborted or not self._debate_is_current(comp_id, generation, decision_epoch, allow_paused=allow_paused):
                finish("aborted", error="Competition paused, reset, or stopped during rebuttal stage")
                return
            if len(rebuttal_rows) != len(participants) or any(row["status"] != "complete" for row in rebuttal_rows):
                session["status"] = "incomplete"
                packet = referee_packet(session, turns, {t.id: t.name for t in self.cfg.traders})
                finish("incomplete", {"status": "incomplete", "label": "四模型反证未完整，等待人工复核", "analysis_only": True},
                       packet, "Rebuttal stage did not receive four valid, fact-cited responses",
                       event=("warn", f"{label}反证不完整，未生成裁判汇总"))
                return
            preliminary = preliminary_summary(opening_rows, rebuttal_rows)
            session["status"] = "ready_for_codex_review"
            packet = referee_packet(session, turns, {t.id: t.name for t in self.cfg.traders})
            if not finish("ready_for_codex_review", preliminary, packet,
                          event=("info", f"{label}四模型辩论完成，已生成 Codex 仲裁包")):
                # 终态 CAS 没命中：关闭/重启/重置已经给这场会话定了终态（并记下了真正的原因），
                # 这里不能再对外宣布"完成"，否则事件流与会话状态互相矛盾
                log.info("Research session %s was already terminal; completion event suppressed", debate_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Pre-open debate failed: %s", exc)
            failure = ("error", f"{label}研究辩论失败：{type(exc).__name__}")
            if debate_id is not None:
                finish("failed", error="ResearchSessionFailed", event=failure)
            else:
                # 还没登记会话，没有可矛盾的终态，异常本身仍要报出去
                self._event_best_effort(failure[0], "debate", failure[1], comp_id=comp_id)

    # ---------- 主循环 ----------
    def _run(self):
        log.info("引擎启动（accelerate=%s）", self.accelerate)
        tick_int = 1.0 if self.accelerate else self.cfg.competition.tick_interval_sec
        dec_int = 3.0 if self.accelerate else self.cfg.competition.decision_interval_sec
        snap_int = 2.0 if self.accelerate else self.cfg.competition.snapshot_interval_sec
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
                if not self.accelerate:
                    self._maybe_start_preopen_debate()
                now = time.time()
                if now - self._last_snapshot >= snap_int:
                    self._snapshot()
                    self._persist_md_bars()
                    self._last_snapshot = now
                if not self.accelerate:
                    self._check_calendar_expiry()
                if self._replay_held() and not self.paused:
                    # 回放 bar 上有账户的触发成交尚未落库：重放结算之前不开始新一轮（新决策会改变持仓/止损，
                    # 使重放不再触发、原本的止损永久丢失）。回放位置写不进数据库、重启恢复的日志尚未全部结算时同样
                    # 不开新一轮、不前进。_last_decision 不更新，结算/写入成功后立即开始一轮。
                    self.status_text = self._held_status()
                elif not self.paused and now - self._last_decision >= dec_int:
                    decision_symbols = self._active_symbols()
                    if decision_symbols:
                        tradable, stale = self._tradable_now(decision_symbols)
                        if stale:
                            # 行情长时间未更新：可能是日历之外的假期、数据源故障或时段刚切换。不决策，下个 tick 再看。
                            self.status_text = stale
                            if not self._stale_logged:
                                self._event_best_effort("warn", "engine", stale)
                                self._stale_logged = True
                        else:
                            self._stale_logged = False
                            # 决策轮在独立线程中进行，主循环继续每个 tick 盯市（止损/止盈/强平不受慢模型影响）
                            if self._start_round_thread(tradable):
                                self._last_decision = now
                    else:
                        self._stale_logged = False
                        nxt = self.source.next_open(datetime.now(self.tz))
                        self.status_text = "休市" + (f"，下次开盘 {nxt.strftime('%m-%d %H:%M')}" if nxt else "")
                        self._last_decision = now
                elif self.paused:
                    self.status_text = "已暂停"
            except Exception as e:  # noqa: BLE001
                log.exception("主循环异常: %s", e)
                # 异常可能正是数据库故障：审计写入必须尽力而为，再次抛出会终止主循环
                self._event_best_effort("error", "engine", PUBLIC_RUNTIME_EVENT_MESSAGES["main_loop"])
            elapsed = time.time() - t0
            self._stop.wait(max(0.2, tick_int - elapsed))
        log.info("引擎停止")

    def _requires_live_quote_freshness(self) -> bool:
        """Replay timestamps belong to its simulated clock, so wall-clock freshness applies only to live sources."""
        return not isinstance(self.source, ReplaySource)

    def _live_quote_admission_reason(self, quote: Quote, now: Optional[float] = None) -> Optional[str]:
        """Return a reason when a live quote cannot safely update prices or trigger risk controls."""
        current = time.time() if now is None else now
        limit = max(self.stale_quote_sec, 3.0 * self.cfg.competition.tick_interval_sec)
        try:
            raw_price, raw_ts = quote.price, quote.ts
        except (AttributeError, TypeError, ValueError, OverflowError):
            return "报价无效"
        if isinstance(raw_price, bool) or isinstance(raw_ts, bool):
            return "报价无效"
        try:
            price, quote_ts = float(raw_price), float(raw_ts)
        except (TypeError, ValueError, OverflowError):
            return "报价无效"
        if not math.isfinite(price) or price <= 0:
            return "报价无效"
        if not math.isfinite(quote_ts) or quote_ts > current + self.max_future_quote_skew_sec:
            return "时间异常"
        if current - quote_ts > limit:
            return f"已 {(current - quote_ts) / 60:.0f} 分钟未更新"
        return None

    def _live_bar_admission_reason(self, bar: Bar, now: Optional[float] = None) -> Optional[str]:
        """Return a reason when a live OHLC bar is unsafe for model-visible indicators."""
        current = time.time() if now is None else now
        try:
            raw_values = (bar.ts, bar.open, bar.high, bar.low, bar.close)
        except AttributeError:
            return "K线无效"
        if any(isinstance(value, bool) for value in raw_values):
            return "K线无效"
        try:
            ts, open_, high, low, close = (float(value) for value in raw_values)
        except (TypeError, ValueError, OverflowError):
            return "K线无效"
        if not all(math.isfinite(value) for value in (ts, open_, high, low, close)) or min(open_, high, low, close) <= 0:
            return "K线无效"
        if ts > current + self.max_future_bar_skew_sec:
            return "K线时间异常"
        return None

    def _manual_quote_only_reason(self, quotes: Dict[str, Quote], health: object) -> Optional[str]:
        """Validate a manual research fallback without treating a prior-session quote as live execution data."""
        if not isinstance(quotes, dict):
            return "行情报价格式异常"
        if not isinstance(health, dict) or not bool(health.get("ok")):
            return "行情源未报告健康"
        now = time.time()
        try:
            raw_last_ok = health.get("last_ok")
            if isinstance(raw_last_ok, bool):
                raise ValueError("boolean last_ok")
            last_ok = float(raw_last_ok)
        except (TypeError, ValueError, OverflowError):
            return "行情源未提供有效刷新时间"
        if not math.isfinite(last_ok) or last_ok > now + self.max_future_quote_skew_sec:
            return "行情源刷新时间异常"
        if now - last_ok > self.cfg.preopen_debate.max_quote_age_sec:
            return "行情源未在允许窗口内成功刷新"
        for symbol in self.symbols:
            quote = quotes.get(symbol)
            if quote is None:
                return f"{symbol} 无报价"
            try:
                raw_price, raw_ts = quote.price, quote.ts
                if isinstance(raw_price, bool) or isinstance(raw_ts, bool):
                    raise ValueError("boolean quote field")
                price, quote_ts = float(raw_price), float(raw_ts)
            except (AttributeError, TypeError, ValueError, OverflowError):
                return f"{symbol} 报价无效"
            if not math.isfinite(price) or price <= 0:
                return f"{symbol} 报价无效"
            if not math.isfinite(quote_ts) or quote_ts > now + self.max_future_quote_skew_sec:
                return f"{symbol} 报价时间异常"
        # Pre-open quotes may legitimately be from the prior trading session.
        # Their own timestamp is therefore deliberately not subject to the
        # intraday decision freshness window; source last_ok is the freshness proof.
        return None

    def _quote_issues(self, quotes: Optional[Dict[str, Quote]] = None,
                      symbols: Optional[List[str]] = None) -> Dict[str, str]:
        """每个不能定价的决策品种 → 原因；空字典表示全都能定价。

        "能不能定价"只在这里判一次：下面的用户文案与决策轮的取舍都由它派生。
        两处各判一次迟早漂移，而这两处的判定必须永远一致。
        """
        if not self._requires_live_quote_freshness():
            return {}
        if quotes is None:
            # Tick writes quotes under the same lock. Copying prevents a partial read while a source recovers one symbol at a time.
            with self.lock:
                quotes = dict(self.quotes)
        else:
            quotes = dict(quotes)
        decision_symbols = list(symbols) if symbols is not None else self._active_symbols()
        now = time.time()
        issues: Dict[str, str] = {}
        for symbol in decision_symbols:
            quote = quotes.get(symbol)
            if quote is None:
                issues[symbol] = "无报价"
                continue
            rejection = self._live_quote_admission_reason(quote, now)
            if rejection:
                issues[symbol] = rejection
        return issues

    def _stale_quotes_reason(self, quotes: Optional[Dict[str, Quote]] = None,
                             symbols: Optional[List[str]] = None) -> Optional[str]:
        """Return a user-facing reason when any decision symbol quote is missing, invalid, or stale."""
        issues = self._quote_issues(quotes, symbols)
        if not issues:
            return None
        limit = max(self.stale_quote_sec, 3.0 * self.cfg.competition.tick_interval_sec)
        items = [f"{symbol} {reason}" for symbol, reason in issues.items()]
        summary = "；".join(items[:3])
        if len(items) > 3:
            summary += f"；另有 {len(items) - 3} 个品种"
        return f"行情缺失、无效或超过 {limit / 60:.0f} 分钟未更新（{summary}），暂不决策"

    def _held_symbols(self) -> set:
        """任一账户仍有持仓的品种。"""
        with self.lock:
            return {p.symbol for acc in self.accounts.values() for p in acc.positions.values()}

    def _tradable_now(self, decision_symbols: List[str],
                      quotes: Optional[Dict[str, Quote]] = None) -> tuple:
        """本轮真正能决策的品种，以及必须整轮停下的理由（二者其一为空）。

        全品种配置里混着长期几乎无成交的合约（线材、纤维板、胶合板、部分郑商所小品种）：交易所
        时段表说它们在开市，行情源却可能一直给不出新的分钟 bar。原来只要有一个这样的品种，
        `_stale_quotes_reason` 就让整轮不决策——默认配置会永远停在"行情缺失…暂不决策"，
        一次模型都不调。不能定价的品种本来就不能交易，移出本轮即可，不该连累其余品种。

        只有两种情况仍然整轮停下：
        1. 某个账户在不能定价的品种上有持仓——账户权益、平仓与改止损的决策都会建立在陈旧价格上；
        2. 一个能定价的品种都不剩，本来就没什么可决策的。
        """
        issues = self._quote_issues(quotes, decision_symbols)
        if not issues:
            return list(decision_symbols), None
        held = sorted(self._held_symbols() & set(issues))
        if held:
            detail = "；".join(f"{symbol} {issues[symbol]}" for symbol in held[:3])
            if len(held) > 3:
                detail += f"；另有 {len(held) - 3} 个品种"
            return [], f"持仓品种行情缺失或已过期（{detail}），暂不决策"
        tradable = [s for s in decision_symbols if s not in issues]
        if not tradable:
            return [], self._stale_quotes_reason(quotes, decision_symbols)
        return tradable, None

    def _check_calendar_expiry(self):
        """内置交易日历过期后每天提醒一次：过期后节假日判断只剩周末规则，需更新 holidays。"""
        if self.calendar_valid_until is None:
            return
        today = datetime.now(self.tz).date()
        if today <= self.calendar_valid_until or self._calendar_warned_day == today:
            return
        self._calendar_warned_day = today
        msg = (f"内置交易日历有效期至 {self.calendar_valid_until.isoformat()}，已过期：此后只按周末休市，"
               f"法定节假日请更新 config/contracts_cn.json 的 holidays（行情未更新时引擎仍会自动跳过决策）")
        log.warning(msg)
        self._event_best_effort("warn", "engine", msg)

    def _start_round_thread(self, decision_symbols: Optional[List[str]] = None) -> bool:
        """启动一轮决策线程。不再因上一轮未结束而推迟整轮：仍在途的交易员本轮跳过，
        其他交易员照常开新一轮（流水线）；慢模型只拖住自己，不绑架全场决策频率。"""
        symbols = list(decision_symbols) if decision_symbols is not None else None
        self._round_thread = threading.Thread(target=self._round_guarded, args=(symbols,), name="arena-round", daemon=True)
        self._round_thread.start()
        return True

    def _release_inflight(self, tid: str, rn: int):
        """释放本轮占用的在途槽位。若该槽位已被更新的轮次占用则不动——
        reset 后旧轮次不得释放新轮次的槽位，否则会出现同一交易员两个并发决策。"""
        with self.lock:
            if self._inflight.get(tid) == rn:
                del self._inflight[tid]

    def _release_inflight_round(self, rn: int):
        """释放某一轮占用的全部在途槽位（只释放仍属于该轮的标记）。"""
        with self.lock:
            for tid, rno in list(self._inflight.items()):
                if rno == rn:
                    del self._inflight[tid]

    def _round_guarded(self, decision_symbols: Optional[List[str]] = None):
        try:
            self._round(decision_symbols)
        except Exception as e:  # noqa: BLE001
            log.exception("决策轮异常: %s", e)
            self._event_best_effort("error", "engine", PUBLIC_RUNTIME_EVENT_MESSAGES["round"])
        finally:
            # 无论正常结束、提前放弃还是异常，都不能把"第 N 轮…"留在状态栏
            if self.status_text.startswith("第 "):
                self.status_text = "已停止" if self._stop.is_set() else ("已暂停" if self.paused else "运行中")

    def _watch(self):
        while not self._stop.is_set():
            self._stop.wait(30)
            if self._stop.is_set():
                break
            try:
                self._watch_once()
            except Exception as e:  # noqa: BLE001
                # 看门狗自身不能因任何异常退出，否则引擎线程死亡后再无人重启
                log.exception("看门狗巡检异常: %s", e)

    def _watch_once(self) -> bool:
        """引擎线程死亡时重启，返回是否重启。先拉起线程再写审计事件：数据库故障时事件写入失败也不影响重启。"""
        if self._thread is None or self._thread.is_alive():
            return False
        log.error("引擎线程已死亡，重启")
        self._thread = threading.Thread(target=self._run, name="arena-engine", daemon=True)
        self._thread.start()
        self._event_best_effort("error", "watchdog", "引擎线程死亡，自动重启")
        return True

    def _active_symbols(self, now: Optional[datetime] = None) -> List[str]:
        """Return configured symbols that are tradable at one clock instant."""
        current = now or datetime.now(self.tz)
        active: List[str] = []
        for symbol in self.symbols:
            try:
                if self.source.is_open(current, symbol):
                    active.append(symbol)
            except Exception as e:  # noqa: BLE001
                log.warning("Unable to determine market session for %s: %s", symbol, e)
        return active

    def _market_open(self) -> bool:
        return bool(self._active_symbols())

    def _exec_clock(self, sym: Optional[str] = None) -> Optional[float]:
        """成交/盯市/持仓时长使用的时钟。

        回放源的行情时间戳是历史或压缩时间：成交必须记在该时刻，否则历史数据集的成交会被记成今天、
        持仓时长按真实流逝秒数计算。实时源返回 None，由撮合按墙钟取时间。
        """
        if self._requires_live_quote_freshness():
            return None
        if sym is not None and sym in self.quotes:
            return self.quotes[sym].ts
        return max((q.ts for q in self.quotes.values()), default=None)

    def _exec_clocks(self) -> Optional[Dict[str, float]]:
        """各品种自身的执行时钟（回放）；实时源返回 None。"""
        if self._requires_live_quote_freshness():
            return None
        return {s: q.ts for s, q in self.quotes.items()}

    def _now(self) -> float:
        """引擎的『当前时间』：回放按行情时间轴（权益快照、日汇总、持仓时长与成交同源），实时源为墙钟。"""
        clock = self._exec_clock()
        return clock if clock is not None else time.time()

    def _bump_version(self):
        with self._version_lock:
            self._version += 1

    def _event_best_effort(self, level: str, source: str, message: str, comp_id: Optional[int] = None):
        """comp_id：后台 worker 传入它启动时捕获的竞赛 id。

        重置之后 self.comp_id 已经是新竞赛，旧竞赛的在飞 worker 再用它写事件，
        会让新竞赛的事件流里冒出一场它从未跑过的研究。
        """
        try:
            self.db.add_event(self.comp_id if comp_id is None else comp_id, level, source, message)
        except Exception as e:  # noqa: BLE001
            log.warning("写入事件失败（已忽略）: %s | %s", e, message[:120])
        self._bump_version()

    # ---------- tick：盯市 ----------
    def _tick(self):
        replay_bars: Optional[Dict[str, List[Bar]]] = None
        try:
            if isinstance(self.source, ReplaySource):
                if self._replay_retry is not None:
                    # 上一根 bar 有账户的触发成交未能落库：重放同一根 bar（不前进），直到所有账户都结算成功；
                    # 否则回放位置会越过这根 bar，只在它上面短暂触发的止损就永远丢了
                    quotes, replay_bars = self._replay_retry
                elif self._cursor_pending or self._journal_replay_pos is not None:
                    # 上一根 bar 已全部结算但回放位置写不进数据库，或重启恢复的预写日志尚未全部结算（回放已停在关闭时保留的
                    # bar 之后）：不取新 bar（不前进），本 tick 只重试写入与待重试的结算
                    quotes, replay_bars = {}, None
                else:
                    # Keep the quote and raw bars from one replay-clock revision.
                    quotes, replay_bars = self.source.latest_with_snapshot(self.symbols)
            else:
                quotes = self.source.latest(self.symbols)
        except Exception as e:  # noqa: BLE001
            log.warning("获取行情失败: %s", e)
            quotes = {}
        if self._requires_live_quote_freshness():
            received_at = time.time()
            market_now = datetime.fromtimestamp(received_at, self.tz)
            usable_quotes: Dict[str, Quote] = {}
            for symbol, quote in quotes.items():
                try:
                    is_open = self.source.is_open(market_now, symbol)
                except Exception as e:  # noqa: BLE001
                    log.warning("Unable to determine market session for %s: %s", symbol, e)
                    continue
                if not is_open:
                    log.debug("Skipping closed-market live quote for %s", symbol)
                    continue
                rejection = self._live_quote_admission_reason(quote, received_at)
                if rejection:
                    log.warning("Dropping unusable live quote for %s: %s", symbol, rejection)
                    continue
                usable_quotes[symbol] = quote
            quotes = usable_quotes
        mark_prices = {symbol: quote.price for symbol, quote in quotes.items()}
        with self.lock:
            # 0) 残留的旧预写日志副本：重试清理/写墓碑，成功后解除封锁；关闭前没写进去的盯市更新：直接写入
            self._retry_stale_journal()
            self._apply_pending_mark_updates()
            # 1) 先重试之前落库失败的触发：只用记录下来的原价与时钟，不依赖新行情。
            #    收盘后、行情源故障期间数据库恢复了，也要能立刻结算，而不是等到下一根可用报价（可能隔夜/隔周末）。
            for tid in list(self._pending_marks):
                self._mark_and_settle(tid, *self._pending_marks[tid], retry=True)
            # 启动时推迟的配置对齐：触发结算完（不再待重试）就补做；对齐本身落库失败则下个 tick 再试
            for tid in list(self._deferred_reconcile):
                if tid in self._pending_marks or tid in self._pending_mark_updates:
                    continue
                try:
                    self._reconcile_account(self.comp_id, tid)
                    self._deferred_reconcile.discard(tid)
                except Exception as e:  # noqa: BLE001
                    log.warning("交易员 %s 推迟的配置对齐落库失败，下个 tick 再试: %s", tid, e)
            if self._cursor_pending and replay_bars is None:
                self._persist_replay_position()   # 上次没写进去的回放位置：先补写，成功后下个 tick 才取下一根 bar
            self._release_journal_replay_pos()    # 恢复的日志全部结算：回放位置越过关闭时保留的 bar，下个 tick 取下一根
            if not quotes:
                # 没有可用行情（收盘、行情源故障）也要把之前失败的无成交盯市更新按缓存的最后报价补写：这类更新没有预写日志，
                # 若进程在下一个可用报价之前退出，数据库里就永远是陈旧的最新价/权益峰值
                for tid in list(self._mark_save_failed):
                    # 触发待重试的账户只按记录的原价重试（上面已做）；关闭前记在日志里的更新只直接写入（上面已试）：
                    # 都不用缓存价再经撮合器盯市——那会按现在的条款重新评估已评估过的价格
                    if tid in self._pending_marks or tid in self._pending_mark_updates:
                        continue
                    self._mark_and_settle(tid, dict(self.prices), self._exec_clock(), self._exec_clocks())
                return
            for s, q in quotes.items():
                self.prices[s] = q.price
                self.quotes[s] = q
            if replay_bars is not None:
                self._replay_bars = replay_bars
            self._last_tick = time.time()
            clock = self._exec_clock()
            # 回放数据流可能未对齐（不同时段、缺 bar）：每个品种的触发成交记在它自己的报价时刻
            clocks = self._exec_clocks()
            # 2) 再用新行情盯市；重试仍失败的账户本 tick 不用新价格盯市，等下个 tick 再试
            for tid in list(self.accounts):
                if tid in self._pending_marks:
                    continue
                # 盯市更新之前落库失败的账户用完整的缓存价：本 tick 只来了部分品种的报价时，缺席品种那笔没写进去的
                # 最新价仍要一起补写，否则按部分价格盯市看不出差异、标记被清掉，该持仓的最新价就永远陈旧。
                # 关闭前记在日志里的更新还没写进去的账户只按新报价评估，缺席品种沿用日志记录的价（_mark_and_settle 只补字段）
                if tid in self._pending_mark_updates or tid not in self._mark_save_failed:
                    px = mark_prices
                else:
                    px = dict(self.prices)
                self._mark_and_settle(tid, px, clock, clocks)
            if replay_bars is not None:
                if not self._unsettled and not self._mark_save_failed and not self._pending_mark_updates:
                    self._replay_retry = None
                    self._persist_replay_position()   # 失败 → _cursor_pending：下个 tick 不前进，只重试写入
                else:
                    # 有账户的触发成交或无成交盯市更新尚未落库：保留这根 bar，下个 tick 重放；持久化的位置停在上一根
                    self._replay_retry = (quotes, replay_bars)
            round_active = self._round_thread is not None and self._round_thread.is_alive()
            if not self.paused and not round_active:   # 决策轮进行中时保留其状态文案；否则恢复常态
                self.status_text = "运行中" if self._market_open() else self.status_text

    # ---------- snapshot ----------
    def _persist_md_bars(self):
        """把 CTP 本地聚合的 1m bar 落库（尽力而为，失败只记日志）。

        CTP 不提供历史 K 线，全靠本地从 tick 累，而 1h 的 ema60 要 60 根 1h——各品种每个交易日
        只产出 5～11 根，也就是 5.5～12 个交易日。不落库的话每次重启都要从头攒，日常重开软件
        就等于长时间拿着残缺的慢指标在跑。

        **只写变过的那几根**：预热历史暖起来之后，整条序列里每次真正变的通常只有当前那一根。
        全量重写的话，默认 71 个品种 × 3600 根保留 ≈ 每分钟 25 万次 upsert，而这段是同步跑在
        引擎循环里的，盯市与止损/强平检查全被它拖住。记住每个品种上次写到哪一根，下次从那根
        （含，它当时可能还没收口）往后写。
        """
        source = self.source
        if not isinstance(source, CtpMarketSource) or not self.cfg.market_data.ctp.persist_bars:
            return
        for symbol in list(self.symbols):
            try:
                bars = source.bars_1m(symbol)
                if not bars:
                    continue
                # 用源上**生效**的保留根数，不是配置里写的那个：源会按 bar_seconds 把它抬到
                # 1h 指标够用为止（见 ctp.retention_floor）。`keep` 也要一起给——默认的
                # 5000 会把 30s 周期下该留的 7200 根裁掉，重启后 1h 照样凑不齐。
                keep = source.max_bars
                since = self._md_persisted.get(symbol)
                pending = bars[-keep:] if since is None else [b for b in bars if b.ts >= since]
                if not pending:
                    continue
                self.db.save_md_bars(symbol, pending, keep=keep,
                                     bar_seconds=self.cfg.market_data.ctp.bar_seconds,
                                     feed=self._md_feed(symbol))
                # 写成功才前进：失败时保持原位，下次把这一段重新写一遍
                self._md_persisted[symbol] = pending[-1].ts
            except Exception as e:  # noqa: BLE001
                log.warning("行情 bar 落库失败（%s，已忽略）：%s", symbol, e)

    def _md_feed(self, symbol: str) -> str:
        """该品种当前这套行情接入的指纹，落库 bar 的命名空间。"""
        return market_feed_namespace(self.cfg.market_data, self.cfg.competition.market, symbol, self._all_specs)

    def _seed_md_bars(self):
        """启动时用库里存的 bar 预热 CTP 行情源，避免开机后前几轮指标不全。

        只读**当前这套接入**的那一份：换了行情前置或改钉了合约之后，旧接入累出来的 bar
        是另一条价格序列，拿它预热等于让新竞赛的第一批指标算在旧实验的数据上，之后两段
        历史还会被当成一条继续写下去——排行榜换了，实验其实没有隔离。

        升级前那批没有来源标记的行（空命名空间）因此永远读不到。曾经试过据配置反推它们属于
        哪套接入，三种反推依据全被证伪：升级与换前置可能在同一次重启里发生；上一场竞赛可能
        根本没跑过这个品种；`persist_bars` 关着时判定会被推迟到另一套接入之后才做。反推错了
        不会报错，只是把另一条价格序列喂进指标——所以宁可让 CTP 重新积累一次。

        **有来源标记、只是签名算法换过版本**的行不在此列：`legacy_market_feed_namespaces` 给出
        的命名空间描述的就是当前这套接入（前置、钉的合约、周期都一样，只是哈希的写法变了）。
        当前命名空间还是空的、而旧写法下已有历史时，把那一份认领过来（复制，不删旧行），否则
        每次改签名算法都会让所有装机的指标冷启动几个交易日。

        两个旧写法都不带交易所，所以合约表里把某个品种的交易所改正前后，它们算出的命名空间相同。
        这不会把另一条价格序列认领进来：CTP 只按**合约代码**订阅，交易所从不发给前置，它只决定
        代码怎么拼（郑商所一位年份、中金所大写、其余小写）。拼法相同的交易所之间改来改去，订的
        是同一批合约；拼法变了，新代码在前置那里不存在（一个合约只有一种代码），一笔 tick 都不会
        来，认领的历史配不上任何实时报价，也就进不了任何快照。库里能有 bar，本身就证明当时订的
        是前置认得的那种拼法——也就是这个品种真正的合约。
        """
        source = self.source
        if not isinstance(source, CtpMarketSource) or not self.cfg.market_data.ctp.persist_bars:
            return
        seeded = 0
        bar_seconds = self.cfg.market_data.ctp.bar_seconds
        for symbol in list(self.symbols):
            feed = self._md_feed(symbol)
            try:
                rows = self.db.md_bars(symbol, source.max_bars, bar_seconds=bar_seconds, feed=feed)
                if not rows:
                    # 旧写法的命名空间由新到旧依次并入：`INSERT OR IGNORE` 让先并入（较新）的那段
                    # 在重叠的时间戳上胜出，较旧的一段只补上更早的历史。不能并到第一个非空就停：
                    # 跨过两个旧版本的装机两段都有，只认老的那段会让指标从一个缺口之前接着算。
                    copied = 0
                    for legacy in legacy_market_feed_namespaces(self.cfg.market_data, self.cfg.competition.market,
                                                                symbol, self._all_specs):
                        copied += self.db.copy_md_bars_feed(symbol, legacy, feed, bar_seconds)
                    if copied:
                        log.info("已把 %s 在旧签名写法下落库的 %d 根 bar 认领到当前命名空间", symbol, copied)
                        rows = self.db.md_bars(symbol, source.max_bars, bar_seconds=bar_seconds, feed=feed)
                if not rows:
                    continue
                source.seed_bars(symbol, [Bar(ts=r["ts"], open=r["open"], high=r["high"], low=r["low"],
                                              close=r["close"], volume=r["volume"], hold=r["hold"])
                                          for r in rows])
                # 预热来的这一段本来就是从库里读出来的，落库的起点直接接在它后面：
                # 否则重启后第一次落库又要把整条序列原样重写一遍（最后一根仍会被重写，
                # 因为它当时可能还没收口，预热时还会与新 tick 合并）
                self._md_persisted[symbol] = rows[-1]["ts"]
                seeded += 1
            except Exception as e:  # noqa: BLE001
                log.warning("行情 bar 预热失败（%s，已忽略）：%s", symbol, e)
        if seeded:
            log.info("已用持久化的 1m bar 预热 %d 个品种的 CTP 行情", seeded)

    def _snapshot(self):
        with self.lock:
            now = self._now()   # 回放：权益快照与日汇总也按回放时间轴，与成交时间戳一致
            pts = []
            for tid, acc in self.accounts.items():
                eq = acc.equity(self.prices, self.specs)
                pts.append(EquityPoint(tid, now, eq, acc.cash, acc.margin_used(), acc.unrealized(self.prices, self.specs)))
                acc.peak_equity = max(acc.peak_equity, eq)
            self.db.add_equity(self.comp_id, pts)
            self._update_daily(now)
            self._recompute_metrics()

    def _update_daily(self, now: float):
        """按日汇总。新的一天以『上一日最后一次快照』的权益/成交数/手续费为基线，而不是本次快照的值：
        两次快照之间（含跨日的那个 tick、停机期间）发生的盈亏与成交计入新的一天，不会两头都不算。"""
        day = datetime.fromtimestamp(now, self.tz).strftime("%Y-%m-%d")
        for tid, acc in self.accounts.items():
            eq = acc.equity(self.prices, self.specs)
            n_now = self.db.trade_count(self.comp_id, tid)
            ds = self._day_start.get(tid)
            if not ds or ds.get("day") != day:
                base = ds or {}
                self._day_start[tid] = {"day": day, "equity": base.get("last_equity", eq), "trades": base.get("last_trades", n_now),
                                        "fees": base.get("last_fees", acc.fees)}
                ds = self._day_start[tid]
            ds["last_equity"], ds["last_trades"], ds["last_fees"] = eq, n_now, acc.fees
            self.db.upsert_daily(self.comp_id, tid, day, ds["equity"], eq, n_now - ds["trades"], acc.fees - ds["fees"])
        # 每次快照都持久化：重启（可能已跨日）后新一天的基线才是停机前最后一次快照的值
        self.db.kv_set(f"day_start:{self.comp_id}", json.dumps(self._day_start))

    # Sharpe 用小时级（加速模式 10 秒级）收益率年化，避免分钟级快照放大数值
    def _bucket_sec(self) -> int:
        return 10 if self.accelerate else 3600

    def _ensure_stats(self, tid: str) -> dict:
        st = self._stats.get(tid)
        if st is None:
            peak, mdd = self.db.equity_peak_and_mdd(self.comp_id, tid)
            st = {"peak": peak, "mdd": mdd, "buckets": self.db.equity_buckets(self.comp_id, tid, self._bucket_sec()),
                  "trade_count": -1, "trade_stats": {}, "avg_hold": None}
            self._stats[tid] = st
        return st

    def _recompute_metrics(self):
        now = self._now()   # 桶序号与数据库中快照的时间戳同一时钟
        bsec = self._bucket_sec()
        per_year = 365 * 24  # 每桶视为一小时（加速模式下 10 秒桶等价于压缩后的一小时）
        for tid, acc in self.accounts.items():
            st = self._ensure_stats(tid)
            eq = acc.equity(self.prices, self.specs)
            # 运行中的峰值/回撤
            st["peak"] = max(st["peak"], eq)
            if st["peak"] > 0:
                st["mdd"] = max(st["mdd"], (st["peak"] - eq) / st["peak"] * 100)
            # 小时桶序列（内存维护，只追加/更新最后一桶）
            b = int(now // bsec)
            buckets = st["buckets"]
            if buckets and buckets[-1][0] == b:
                buckets[-1][1] = eq
            else:
                buckets.append([b, eq])
                if len(buckets) > 20000:
                    del buckets[: len(buckets) - 20000]
            # 成交统计只在成交数变化时重算
            n_tr = self.db.trade_count(self.comp_id, tid)
            if n_tr != st["trade_count"]:
                trades = self.db.all_trades(self.comp_id, tid)
                st["trade_stats"] = metrics.trade_stats(trades)
                st["avg_hold"] = _avg_hold_minutes(trades)
                st["trade_count"] = n_tr
            ts_ = st["trade_stats"] or metrics.trade_stats([])
            series = [x[1] for x in buckets]
            self._metrics[tid] = {
                "equity": eq, "return_pct": metrics.total_return(acc.initial_cash, eq),
                "max_drawdown": st["mdd"],
                "sharpe": metrics.sharpe(series, per_year) if len(series) > 5 else None,
                "win_rate": ts_["win_rate"], "closed_trades": ts_["closed_trades"], "total_trades": ts_["total_trades"],
                "profit_factor": ts_["profit_factor"], "fees": acc.fees, "realized_pnl": acc.realized_pnl,
                "avg_hold_min": st["avg_hold"], "liquidations": acc.liquidations,
                "today_pnl": eq - self._day_start.get(tid, {}).get("equity", eq),
            }

    # ---------- 决策轮 ----------
    def _build_snapshot(self, quotes: Optional[Dict[str, Quote]] = None, round_no: Optional[int] = None,
                        replay_bars: Optional[Dict[str, List[Bar]]] = None,
                        symbols: Optional[List[str]] = None) -> Optional[MarketSnapshot]:
        """构建共享行情快照。不持有引擎锁；收到停止信号时立即放弃并返回 None。"""
        quotes = dict(self.quotes) if quotes is None else quotes
        round_no = self.round_no if round_no is None else round_no
        decision_symbols = list(self.symbols) if symbols is None else list(symbols)
        if isinstance(self.source, ReplaySource) and replay_bars is None:
            quotes, replay_bars = self.source.snapshot(decision_symbols)
        views: Dict[str, SymbolView] = {}
        # 实盘 K 线串行拉取是每轮几十秒的主要耗时：线程池并行（回放走本地聚合，无需并行）。
        # _build_snapshot 本身不在引擎锁内，这里只做纯 I/O。
        klines_map: Dict[str, Optional[tuple]] = {}
        live_needed = [s for s in decision_symbols if replay_bars is None and quotes.get(s) is not None]
        if live_needed:
            def _fetch(sym: str):
                try:
                    return sym, (self.source.klines(sym, "5m", 120),
                                 self.source.klines(sym, "15m", 100),
                                 self.source.klines(sym, "1h", 100))
                except Exception as e:  # noqa: BLE001
                    log.warning("K线 %s 获取失败: %s", sym, e)
                    return sym, None
            with ThreadPoolExecutor(max_workers=min(8, len(live_needed)),
                                    thread_name_prefix="arena-klines") as ex:
                for sym, res in ex.map(_fetch, live_needed):
                    klines_map[sym] = res
                    if self._stop.is_set():
                        return None
        for s in decision_symbols:
            if self._stop.is_set():
                return None
            q = quotes.get(s)
            if q is None:
                continue
            try:
                if replay_bars is not None:
                    raw = replay_bars.get(s, [])
                    b5 = ReplaySource.bars_for_period(raw, "5m", 120)
                    b15 = ReplaySource.bars_for_period(raw, "15m", 100)
                    b1h = ReplaySource.bars_for_period(raw, "1h", 100)
                else:
                    got = klines_map.get(s)
                    if got is None:
                        continue  # 拉取失败已在 _fetch 内记日志
                    b5, b15, b1h = got
            except Exception as e:  # noqa: BLE001
                log.warning("K线 %s 获取失败: %s", s, e)
                continue
            if self._requires_live_quote_freshness():
                received_at = time.time()
                # 与回放侧统一口径：指标只用已收盘的 bar。bar.ts 为开盘时间，
                # 收盘时刻 = ts + 周期秒数；收盘时刻晚于现在的 forming bar 剔除。
                # 口径变更：实盘指标与旧版本不可比（旧版含 forming bar）。
                period_sec = {"5m": 300, "15m": 900, "1h": 3600}

                def safe_bars(period: str, bars: List[Bar]) -> List[Bar]:
                    sec = period_sec.get(period, 0)
                    accepted = [bar for bar in bars
                                if self._live_bar_admission_reason(bar, received_at) is None
                                and bar.ts + sec <= received_at]
                    rejected = len(bars) - len(accepted)
                    if rejected:
                        log.warning("Dropped %d unsafe/forming live %s bars for %s", rejected, period, s)
                    return accepted

                b5 = safe_bars("5m", b5)
                b15 = safe_bars("15m", b15)
                b1h = safe_bars("1h", b1h)
            if not b5:
                continue
            views[s] = SymbolView(symbol=s, spec=self.specs[s], quote=q, ind_5m=indicators.summarize(b5),
                                  ind_15m=indicators.summarize(b15) if b15 else {}, ind_1h=indicators.summarize(b1h) if b1h else {}, bars_5m=b5)
        if not views:
            return None
        return MarketSnapshot(ts=time.time(), round_no=round_no, market=self.cfg.competition.market, currency=self.currency, symbols=views)

    def _account_view(self, tid: str, snap: MarketSnapshot) -> AccountView:
        acc = self.accounts[tid]
        prices = self.prices
        now = self._now()   # 持仓时长与成交时钟同源（回放时按行情时间）
        positions = []
        for p in acc.positions.values():
            px = prices.get(p.symbol, p.last_price)
            positions.append({"symbol": p.symbol, "side": p.side, "qty": p.qty, "avg_price": p.avg_price, "last": px,
                              "unrealized": p.unrealized(px, self.specs[p.symbol].multiplier), "margin": p.margin,
                              "stop_loss": p.stop_loss, "take_profit": p.take_profit, "hold_min": (now - p.opened_at) / 60, "leverage": p.leverage})
        recent = self.db.trades(self.comp_id, tid, limit=8)
        last = self.db.decisions(self.comp_id, tid, limit=1)
        summary = ""
        if last:
            d = last[0]
            acts = ", ".join(f"{a.get('symbol')}:{a.get('action')}" for a in d.get("actions", [])) or "hold"
            summary = f"第{d['round_no']}轮 {d.get('analysis','')[:80]} → {acts}"
        return AccountView(trader_id=tid, equity=acc.equity(prices, self.specs), cash=acc.cash, available=acc.available(prices, self.specs),
                           margin_used=acc.margin_used(), realized_pnl=acc.realized_pnl, fees=acc.fees, initial_cash=acc.initial_cash,
                           positions=positions, recent_trades=recent, last_decision_summary=summary, round_no=self.round_no)

    def _round(self, decision_symbols: Optional[List[str]] = None):
        t0 = time.time()
        if decision_symbols is None:
            decision_symbols = self._active_symbols()
        else:
            decision_symbols = list(decision_symbols)
        if not decision_symbols:
            self.status_text = "休市"
            return
        with self.lock:
            if self.paused:
                self.status_text = "已暂停"
                return
            if self._replay_held():
                # 回放 bar 被保留（有账户的结算/盯市更新没写进数据库）、回放位置写入失败或恢复的日志未结算：不发起模型调用
                self.status_text = self._held_status()
                return
            gen = self._generation
            decision_epoch = self._decision_epoch
            self.round_no += 1
            rn = self.round_no
            self.db.set_round(self.comp_id, rn)
            quotes = dict(self.quotes)
            replay_bars = ({s: list(bars) for s, bars in self._replay_bars.items()}
                           if isinstance(self.source, ReplaySource) else None)
        # 行情快照涉及多次网络请求（含重试），必须在引擎锁之外进行：否则停止、看板读取都会被 I/O 阻塞
        self.status_text = f"第 {rn} 轮获取行情"
        snap = self._build_snapshot(quotes, rn, replay_bars, decision_symbols)
        if snap is None:
            if not self._stop.is_set():
                self._event_best_effort("warn", "engine", f"第 {rn} 轮无行情快照，跳过")
            return
        with self.lock:
            # 拉取行情期间可能发生了重置或暂停：此时不再发起任何模型调用，也不动熔断计数
            if gen != self._generation or self.paused or decision_epoch != self._decision_epoch:
                self.status_text = "已暂停" if self.paused else self.status_text
                return
            if self._replay_held():
                # 拉取行情期间这根回放 bar 被保留或回放位置写入失败：模型调用是付费的，返回的动作也必然被拒，整轮放弃；
                # 写入成功后主循环立即开新一轮
                self.status_text = self._held_status()
                self._event_best_effort("info", "engine", f"第 {rn} 轮：回放 bar 尚未全部落库，跳过本轮模型调用")
                return
            # 拉取 K 线可能耗时数十秒，期间部分品种可能已收盘：收盘品种不再进入本轮（模型调用是付费的，
            # 执行时也必然被拒），全部收盘则整轮放弃
            active_now = set(self._active_symbols())
            closed = [s for s in list(snap.symbols) if s not in active_now]
            for s in closed:
                del snap.symbols[s]
                log.info("第 %d 轮：快照构建期间 %s 已收盘，移出本轮", rn, s)
            decision_symbols = [s for s in decision_symbols if s in active_now]
            if not snap.symbols or not decision_symbols:
                self.status_text = "休市"
                self._event_best_effort("info", "engine", f"第 {rn} 轮：快照构建期间所有品种已收盘，跳过")
                return
            if self._requires_live_quote_freshness():
                snapshot_quotes = {symbol: view.quote for symbol, view in snap.symbols.items()}
                # 盯市报价与快照报价两处都要能定价才算数；不能定价的品种移出本轮，而不是让整轮停下
                tradable, stale = self._tradable_now(decision_symbols, quotes)
                if not stale:
                    tradable, stale = self._tradable_now(tradable, snapshot_quotes)
                if stale:
                    self.status_text = stale
                    self._event_best_effort("warn", "engine", f"第 {rn} 轮行情快照已过期，跳过：{stale}")
                    return
                unpriceable = [s for s in decision_symbols if s not in set(tradable)]
                for s in unpriceable:
                    snap.symbols.pop(s, None)
                    log.info("第 %d 轮：%s 无可用报价，移出本轮", rn, s)
                decision_symbols = tradable
                if not snap.symbols or not decision_symbols:
                    self.status_text = "行情缺失，暂不决策"
                    self._event_best_effort("warn", "engine", f"第 {rn} 轮：所有品种都无可用报价，跳过")
                    return
            views = {tid: self._account_view(tid, snap) for tid in self.traders}
            account_versions = {tid: self._account_versions.get(tid, 0) for tid in self.traders}
            # 提交模型任务与上面的暂停/代次检查在同一个锁块内完成：pause() 也要拿这把锁，
            # 因此不可能插在检查与提交之间。提交本身只是入队，锁内耗时可忽略。
            self.status_text = f"第 {rn} 轮决策中"
            futures = {}
            submitted = False
            try:
                for tid, trader in self.traders.items():
                    br = self._breaker[tid]
                    if br["skip"] > 0:
                        br["skip"] -= 1
                        d = Decision(trader_id=tid, round_no=rn, ts=time.time(), mode="skipped", error=f"熔断中，剩余跳过 {br['skip']} 轮")
                        futures[tid] = (None, account_versions[tid])
                        self._finish_safely(tid, d, snap, gen, decision_epoch, account_versions[tid])
                        continue
                    if self._stop.is_set():
                        return
                    if tid in self._inflight:
                        # 该交易员上一轮决策仍在途：跳过本轮，不阻塞其他交易员
                        log.info("第 %d 轮：%s 第 %d 轮决策仍在途，跳过本轮", rn, tid, self._inflight[tid])
                        continue
                    try:
                        futures[tid] = (self.pool.submit(self._safe_decide, trader, snap, views[tid]), account_versions[tid])
                        self._inflight[tid] = rn
                    except RuntimeError:   # 线程池已在 stop() 中关闭
                        return
                submitted = True
            finally:
                # 提交阶段提前返回（停止/线程池关闭）时释放本轮已占用的槽位；
                # 正常提交后槽位保持占用，直到各自决策完成/取消
                if not submitted:
                    self._release_inflight_round(rn)
        timeout = self._decision_round_timeout_sec(
            tid for tid, (future, _) in futures.items() if future is not None
        )
        deadline = time.time() + timeout
        pending = {fut: (tid, account_version) for tid, (fut, account_version) in futures.items() if fut is not None}
        try:
            while pending:
                if self._stop.is_set():
                    for fut, (tid, _) in pending.items():
                        fut.cancel()
                        self._release_inflight(tid, rn)
                        log.info("收到停止信号，放弃 %s 第 %d 轮尚未完成的决策", tid, rn)
                    break
                if gen != self._generation or decision_epoch != self._decision_epoch:
                    for fut, (tid, _) in pending.items():
                        fut.cancel()
                        self._release_inflight(tid, rn)
                        log.info("竞赛已重置或暂停，放弃 %s 第 %d 轮尚未完成的决策", tid, rn)
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    done = {fut for fut in pending if fut.done()}
                    if not done:
                        for fut, (tid, account_version) in list(pending.items()):
                            fut.cancel()
                            d = Decision(trader_id=tid, round_no=rn, ts=time.time(), mode=self.traders[tid].mode, error="决策超时")
                            self._finish_safely(tid, d, snap, gen, decision_epoch, account_version)
                            self._release_inflight(tid, rn)
                            pending.pop(fut, None)
                        break
                else:
                    done, _ = wait(pending, timeout=min(1.0, remaining), return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                for fut in done:
                    tid, account_version = pending.pop(fut)
                    try:
                        d = fut.result()
                    except Exception as e:  # noqa: BLE001
                        d = Decision(trader_id=tid, round_no=rn, ts=time.time(), mode=self.traders[tid].mode, error=f"决策异常: {e}")
                    self._finish_safely(tid, d, snap, gen, decision_epoch, account_version)
                    self._release_inflight(tid, rn)
        finally:
            # 兜底：本轮标记过的在途槽位全部释放（正常路径已在每个决策完成/取消时释放；
            # 只释放仍属于本轮的槽位，不会误伤更新轮次的标记）
            self._release_inflight_round(rn)
        self._last_round_ms = int((time.time() - t0) * 1000)
        self.status_text = "已停止" if self._stop.is_set() else ("已暂停" if self.paused else "运行中")

    def _finish_safely(self, tid: str, d: Decision, snap: MarketSnapshot, gen: int,
                       decision_epoch: Optional[int] = None, account_version: Optional[int] = None):
        """单个交易员的执行/落库异常不能中断整轮，其他交易员照常处理。"""
        try:
            self._finish_decision(tid, d, snap, gen, decision_epoch, account_version)
        except Exception as e:  # noqa: BLE001
            log.exception("交易员 %s 第 %d 轮结算异常: %s", tid, d.round_no, e)
            # 数据库不可用时这里的审计写入也会失败，必须尽力而为，不能再次抛出中断整轮
            self._event_best_effort("error", tid, f"第 {d.round_no} 轮{PUBLIC_RUNTIME_EVENT_MESSAGES['settlement']}")

    def _safe_decide(self, trader: Trader, snap: MarketSnapshot, view: AccountView) -> Decision:
        try:
            return trader.decide(snap, view, self.rules)
        except Exception as e:  # noqa: BLE001
            return Decision(trader_id=trader.id, round_no=snap.round_no, ts=time.time(), mode=trader.mode, error=f"{type(e).__name__}: {e}")

    def _finish_decision(self, tid: str, d: Decision, snap: MarketSnapshot, gen: int,
                         decision_epoch: Optional[int] = None, account_version: Optional[int] = None):
        with self.lock:
            if decision_epoch is None:
                decision_epoch = self._decision_epoch
            if gen != self._generation or tid not in self.accounts:
                # 决策发起后竞赛已被重置（或交易员已移除）：丢弃，避免把旧竞赛的动作写进新竞赛
                log.info("丢弃第 %d 轮 %s 的过期决策（竞赛已重置）", d.round_no, tid)
                self._event_best_effort("info", tid, f"竞赛已重置，丢弃重置前第 {d.round_no} 轮的决策")
                return
            if self.paused or decision_epoch != self._decision_epoch:
                log.info("丢弃第 %d 轮 %s 的过期决策（竞赛已暂停）", d.round_no, tid)
                self._event_best_effort("info", tid, f"竞赛已暂停，丢弃暂停前第 {d.round_no} 轮的决策")
                return
            if account_version is not None and account_version != self._account_versions.get(tid, 0):
                log.info("丢弃第 %d 轮 %s 的过期决策（账户风险状态已变化）", d.round_no, tid)
                self._event_best_effort("info", tid, f"账户风险状态已变化，丢弃第 {d.round_no} 轮的过期决策")
                return
            if (tid in self._unsettled or tid in self._pending_mark_updates) and d.actions:
                # 该账户上一次触发的止损/止盈/强平尚未落库：在重试结算之前不能让模型加仓或改止损，
                # 否则重试时不再触发（回放还会让游标前进），原本的触发就永久丢失。决策保留为零成交的审计记录。
                # 关闭前记在日志里的盯市更新还没写进去时同理（实时源没有回放那样的整体停住）：数据库恰在决策落库时恢复、
                # 决策改了持仓之后，写入的日志价会盖掉更新后的状态
                if tid in self._unsettled:
                    reason = ("该账户上一次触发的止损/止盈/强平的结算记录尚未持久化完成（等待按原价重试落库，或预写日志尚未清理），"
                              "本轮动作不执行")
                else:
                    reason = "该账户关闭前未写入的盯市更新尚未持久化（等待写入数据库），本轮动作不执行"
                d.results = [{"symbol": req.symbol, "action": req.action, "accepted": False, "reason": reason} for req in d.actions]
                self._save_decision(tid, self.accounts[tid], d)
                self._event_best_effort("warn", tid, f"第 {d.round_no} 轮：{reason}")
                self._bump_version()
                return
            if self._replay_held() and d.actions:
                # 回放位置尚未持久化，或这根回放 bar 还有账户的结算/盯市更新没写进数据库（bar 被保留、位置停在上一根）：
                # 这一轮是在此之前发起的。现在执行动作会把成交记在持久化位置之后的 bar 上，崩溃重启重放该 bar 时
                # 又会对已经改变的账户再做一次决策。保留为零成交的审计记录；写入成功后主循环开新一轮。
                reason = ("回放位置尚未持久化（等待写入成功），本轮动作不执行" if self._cursor_pending
                          else "回放 bar 尚未全部落库（有账户的结算或盯市更新待写入），本轮动作不执行")
                d.results = [{"symbol": req.symbol, "action": req.action, "accepted": False, "reason": reason} for req in d.actions]
                self._save_decision(tid, self.accounts[tid], d)
                self._event_best_effort("warn", tid, f"第 {d.round_no} 轮：{reason}")
                self._bump_version()
                return
            if self._requires_live_quote_freshness():
                # 执行时的闸门必须用**本轮实际决策的那组品种**，不是全部开市品种。
                # 决策轮已经把取不到行情的品种移出本轮了；这里再按全集判定，等于让一个
                # 根本没参与本轮的品种（例如长期无成交的停滞合约）把 RB0 上完全有效的决策
                # 全部拒掉——那正是"按品种取舍"想解决的问题，只在出口处又漏了回去。
                active_symbols = set(self._active_symbols())
                round_symbols = [symbol for symbol in snap.symbols if symbol in active_symbols]
                current_stale = self._stale_quotes_reason(symbols=round_symbols)
                snapshot_stale = self._stale_quotes_reason(
                    {symbol: view.quote for symbol, view in snap.symbols.items()}, round_symbols
                )
                stale = current_stale or snapshot_stale
                if stale:
                    reason = f"行情不新鲜，拒绝执行：{stale}"
                    log.info("Rejecting round %d decision from %s because quotes are not fresh", d.round_no, tid)
                    d.error = f"{d.error}; {reason}" if d.error else reason
                    d.results = [
                        {"symbol": req.symbol, "action": req.action, "accepted": False, "reason": reason}
                        for req in d.actions
                    ]
                    # Keep the model output as a zero-trade audit record; a stale decision must never reach matching.
                    self._save_decision(tid, self.accounts[tid], d)
                    self._event_best_effort("warn", tid, f"第 {d.round_no} 轮{reason}")
                    return
            br = self._breaker[tid]
            if d.mode != "skipped":
                if d.error and not d.actions:
                    br["fails"] += 1
                    br["last_error"] = d.error
                    if br["fails"] >= self.cfg.llm.breaker_failures:
                        br["skip"] = self.cfg.llm.breaker_skip_rounds
                        br["fails"] = 0
                        br["status"] = "breaker"
                        category = decision_failure_category(d.error, mode=d.mode)
                        message = public_decision_failure_message(category)
                        self._event_best_effort("warn", tid, f"连续失败触发熔断，跳过 {br['skip']} 轮：{message}")
                else:
                    br["fails"] = 0
                    br["status"] = "ok"
                    br["last_error"] = ""
            # 在账户副本上执行；事务提交成功后才发布副本，失败时内存状态不变（与数据库一致）
            acc = copy.deepcopy(self.accounts[tid])
            results = []
            trades: List[Trade] = []
            # 本轮的开市品种集合只算一次，不再每个动作重复调 _active_symbols()
            active_symbols = set(self._active_symbols())
            for req in d.actions:
                try:
                    res = self._execute(acc, req, snap, d.round_no, trades, active_symbols)
                except Exception as e:  # noqa: BLE001
                    # 单个动作执行异常（如非法数值）只记为拒绝，不影响其余动作与其他交易员
                    log.warning("交易员 %s 动作 %s %s 执行异常: %s", tid, req.symbol, req.action, e)
                    res = {"symbol": req.symbol, "action": req.action, "accepted": False, "reason": PUBLIC_ACTION_EXECUTION_FAILURE}
                results.append(res)
            d.results = results
            # 决策、成交、账户与持仓同一事务落库
            self._save_decision(tid, acc, d, trades)
            self.accounts[tid] = acc
            if trades:
                self._account_versions[tid] = self._account_versions.get(tid, 0) + 1
            self._bump_version()   # 结果已落库：看板此时才应刷新决策列表

    def _execute(self, acc: Account, req, snap: MarketSnapshot, round_no: int, trades: List[Trade],
                 active_symbols: Optional[AbstractSet[str]] = None) -> dict:
        sym = req.symbol
        view = snap.symbols.get(sym)
        if req.action == "hold":
            return {"symbol": sym, "action": "hold", "accepted": True, "reason": ""}
        if active_symbols is None:
            active_symbols = set(self._active_symbols())
        if sym not in active_symbols:
            return {"symbol": sym, "action": req.action, "accepted": False, "reason": "当前休市"}
        if view is None:
            return {"symbol": sym, "action": req.action, "accepted": False, "reason": "品种不在本轮行情快照"}
        # 公平性：执行价 pin 到本轮快照。决策轮在途时 tick 会继续推进 self.prices，
        # 若用实时价，同一决策在不同时刻执行会拿到不同成交价（回放结果不可复现、
        # 同轮先/后 finish 的交易员价格不一致）。口径变更：回放结果与旧版本不可比。
        price = view.quote.price
        if price is None:
            return {"symbol": sym, "action": req.action, "accepted": False, "reason": "无最新价"}
        atr = view.ind_5m.get("atr14") if view else None
        verdict = self.risk.check(acc, req, price, self.prices, atr=atr)
        if not verdict.ok:
            return {"symbol": sym, "action": req.action, "accepted": False, "reason": verdict.reason}
        req = verdict.action
        spec = self.specs[sym]
        if req.action == "hold":
            return {"symbol": sym, "action": "hold", "accepted": True, "reason": ""}
        # 回放：成交记在本轮快照的品种时间戳上，与执行价同源；
        # 实时源返回 None，由撮合按墙钟取时间
        clock = None if self._requires_live_quote_freshness() else view.quote.ts
        if req.action == "close":
            r = self.exchange.close(acc, spec, price, round_no, reason=req.reason, ts=clock)
        elif req.action == "adjust_sl_tp":
            r = self.exchange.adjust_sl_tp(acc, sym, req.stop_loss, req.take_profit)
        else:
            side = "long" if req.action == "open_long" else "short"
            equity = acc.equity(self.prices, self.specs)
            qty = self.exchange.qty_for_pct(spec, price, equity, req.size_pct, req.leverage)
            r = self.exchange.open(acc, spec, side, qty, price, req.leverage, req.stop_loss, req.take_profit, self.prices, round_no,
                                   reason=req.reason, ts=clock)
        if r.trade:
            trades.append(r.trade)
        out = {"symbol": sym, "action": req.action, "accepted": r.accepted, "reason": r.reason,
               "size_pct": req.size_pct, "stop_loss": req.stop_loss, "take_profit": req.take_profit}
        if r.trade:
            out.update({"qty": r.trade.qty, "price": r.trade.price, "fee": r.trade.fee, "realized_pnl": r.trade.realized_pnl})
        return out

    def _persist_all(self) -> bool:
        """停止时的最终持久化。返回 False 表示有盯市更新既写不进数据库、也写不进预写日志（主/备用位置都失败）：
        它只存在于内存，进程退出后丢失——记入 shutdown_errors、事件与 ERROR 日志（含数值），不能当作已持久化。"""
        # 关闭前记在日志里、还没写进去的盯市更新先直接写入；之前落库失败的盯市更新（最新价 + 高水位）按缓存价补写：
        # 关闭后内存里的记忆就没了。触发待重试的账户不在这里用缓存价再盯市——那会用此刻的价格覆盖日志里记录的原触发价；
        # 它记住的高水位随触发条目一起进日志，重试落库时带上
        self._apply_pending_mark_updates()
        for tid in list(self._mark_save_failed):
            if tid in self._pending_marks or tid in self._pending_mark_updates:   # 日志里的更新只直接写入，不经撮合器再盯市
                continue
            self._mark_and_settle(tid, dict(self.prices), self._exec_clock(), self._exec_clocks())
        ok = True
        if self._mark_save_failed:
            # 关闭前仍写不进去：把最新价与记住的高水位记入预写日志（kind=mark），重启后直接写入、不重新盯市，
            # 记录不随进程消失。这些账户跳过下面的兜底保存——那会写成"旧最新价 + 新峰值"的半截状态，
            # 且一旦成功就再没有人补写最新价。已有条目（重启后仍没写进去的）只按重启后真正收到的报价与高水位刷新，
            # 不用按持仓最后价初始化的缓存（比日志里的价旧）
            fresh = {s: q.price for s, q in self.quotes.items()}
            for tid, peak in list(self._mark_save_failed.items()):
                if tid in self._pending_marks:
                    continue
                old = self._pending_mark_updates.get(tid)
                self._pending_mark_updates[tid] = {"prices": {**old["prices"], **fresh} if old else dict(self.prices),
                                                   "peak": max(peak, float((old or {}).get("peak") or 0.0))}
            if isinstance(self.source, ReplaySource) and (self._replay_retry is not None or self._journal_replay_pos is not None):
                # 关闭时保留的这根 bar 之后的位置：所有账户对它的评估都已完成（只差落库），重启后日志全部写入即越过它、不重放
                self._journal_replay_pos = self.source.position()
            who = "、".join(sorted(self._mark_save_failed))
            if self._write_journal():
                log.warning("交易员 %s 的盯市更新在关闭前仍写不进数据库，已记入预写日志，重启后写入", who)
            else:
                ok = False
                lost = {tid: self._pending_mark_updates[tid] for tid in self._mark_save_failed if tid in self._pending_mark_updates}
                msg = (f"交易员 {who} 的盯市更新在关闭前写不进数据库，预写日志主/备用位置也都写不进去：该更新未持久化，"
                       f"进程退出后丢失，请检查磁盘空间/权限后核对最新价与权益峰值")
                log.error("%s: %s", msg, json.dumps(lost, ensure_ascii=False))
                self._event_best_effort("error", "engine", msg)
                self.shutdown_errors.append(msg)
        for tid, acc in self.accounts.items():
            if tid in self._mark_save_failed:
                continue
            self.db.save_account(self.comp_id, acc)
        self.db.set_round(self.comp_id, self.round_no)
        return ok

    def _carry_failed_peak(self, tid: str, acc: Account):
        """把落库失败那次盯市算出的权益峰值并入即将写入的账户：任何一次成功写入都不能丢掉高水位。"""
        peak = self._mark_save_failed.get(tid)
        if peak is not None and peak > acc.peak_equity:
            acc.peak_equity = peak

    def _save_decision(self, tid: str, acc: Account, d: Decision, trades: Iterable[Trade] = ()):
        self._carry_failed_peak(tid, acc)
        self.db.save_account(self.comp_id, acc, trades=trades, decision=d)

    # ---------- 对外状态 ----------
    def _preopen_debate_state(self) -> dict:
        """Return a small, non-secret dashboard projection of the research room."""
        cfg = self.cfg.preopen_debate
        automatic_supported = (self.cfg.competition.market in DOMESTIC_MARKETS and not self.accelerate)
        next_phase = None
        if automatic_supported:
            try:
                now = datetime.now(self.tz)
                target, phase = self._next_research_open(now)
                if target is not None:
                    # 展示用竞赛时区的 target（下面 isoformat 进看板），相位判定已在交易所时区做过：
                    # 否则展示时区是 UTC 时，上海 09:00 开盘变成 01:00，调度已经排上了、看板却说没有下一场
                    target = target.astimezone(self.tz)
                    if phase is not None:
                        lead = (target - now).total_seconds()
                        next_phase = {
                            "phase": phase[0], "label": phase[1], "opens_at": target.isoformat(),
                            "lead_window_sec": cfg.lead_window_sec, "within_window": 0 <= lead <= cfg.lead_window_sec,
                        }
            except Exception as exc:  # noqa: BLE001
                log.warning("Unable to build pre-open debate state: %s", exc)
        latest = self.db.latest_debate_session(self.comp_id)
        latest_view = None
        if latest:
            latest_view = {key: latest.get(key) for key in (
                "id", "phase", "phase_label", "scheduled_open_at", "market_as_of", "analysis_id", "snapshot_hash",
                "status", "started_at", "completed_at", "global_analysis", "preliminary",
            )}
        if automatic_supported:
            mode = "automatic" if cfg.enabled else "automatic_disabled"
        else:
            mode = "manual_only"
        return {
            "analysis_only": True,
            "enabled": cfg.enabled,
            "automatic_supported": automatic_supported,
            # Manual research is deliberately available while the competition is
            # paused: it is account-free and lets an operator verify providers
            # without opening an executable decision window.
            "manual_enabled": self.running and not self._stop.is_set(),
            "mode": mode,
            "running": bool(self._debate_thread and self._debate_thread.is_alive()),
            "next": next_phase,
            "latest": latest_view,
        }

    def state(self) -> dict:
        with self.lock:
            now = time.time()
            hold_now = self._now()   # 回放时持仓时长按行情时间计算，与成交时间戳同源
            dec_int = 3.0 if self.accelerate else self.cfg.competition.decision_interval_sec
            traders = []
            for tid, acc in self.accounts.items():
                if tid not in self.traders:
                    # 已从配置移除、只因触发成交待落库而暂留内存的账户：不再参赛，不进排行榜（结算后会被对齐移除）
                    continue
                tcfg = self.trader_cfgs.get(tid)
                m = self._metrics.get(tid) or {"equity": acc.equity(self.prices, self.specs), "return_pct": metrics.total_return(acc.initial_cash, acc.equity(self.prices, self.specs))}
                positions = []
                for p in acc.positions.values():
                    px = self.prices.get(p.symbol, p.last_price)
                    positions.append({**p.to_dict(), "last": px, "unrealized": p.unrealized(px, self.specs[p.symbol].multiplier),
                                      "notional": p.notional(px, self.specs[p.symbol].multiplier), "hold_min": (hold_now - p.opened_at) / 60,
                                      "name": self.specs[p.symbol].name})
                current_equity = acc.equity(self.prices, self.specs)
                margin_used = acc.margin_used()
                risk_ratio = (margin_used / current_equity) if current_equity > 0 else None
                risk_cap = self.cfg.risk.max_margin_risk_ratio
                target_min = self.cfg.risk.target_margin_risk_min_ratio
                target_max = self.cfg.risk.target_margin_risk_max_ratio
                warning = self.cfg.risk.warning_margin_risk_ratio
                if risk_ratio is None:
                    risk_state = "unknown"
                elif risk_cap is not None and risk_ratio >= risk_cap - 1e-9:
                    risk_state = "cap"
                elif target_max is not None and risk_ratio > target_max + 1e-9:
                    risk_state = "above_target"
                elif target_max is not None and risk_ratio >= target_max - 1e-9:
                    risk_state = "target_ceiling"
                elif target_min is not None and risk_ratio >= target_min - 1e-9:
                    risk_state = "in_target"
                elif target_min is not None:
                    risk_state = "below_target"
                elif risk_ratio >= warning:
                    risk_state = "warning"
                else:
                    risk_state = "normal"
                raw_breaker = self._breaker.get(tid, {})
                raw_error = str(raw_breaker.get("last_error") or "")
                breaker = {
                    "fails": int(raw_breaker.get("fails") or 0),
                    "skip": int(raw_breaker.get("skip") or 0),
                    "status": str(raw_breaker.get("status") or "ok"),
                }
                if raw_error:
                    breaker["failure_category"] = decision_failure_category(raw_error)
                traders.append({
                    "id": tid, "name": tcfg.name if tcfg else tid, "color": tcfg.color if tcfg else "#888",
                    "mode": self.traders[tid].mode, "model": (_seat_model(tcfg) if tcfg and _is_live_mode(self.traders[tid].mode) else f"演示策略:{tcfg.fallback_rule if tcfg else ''}"),
                    "cash": acc.cash, "available": acc.available(self.prices, self.specs), "margin_used": margin_used,
                    "unrealized": acc.unrealized(self.prices, self.specs), "initial_cash": acc.initial_cash,
                    "risk_ratio": risk_ratio, "risk_state": risk_state,
                    "positions": positions, "breaker": breaker, **m,
                })
            traders.sort(key=lambda t: t.get("equity", 0), reverse=True)
            for i, t in enumerate(traders):
                t["rank"] = i + 1
            market_open = self._market_open()
            nxt = None
            if not market_open:
                n = self.source.next_open(datetime.now(self.tz))
                nxt = n.isoformat() if n else None
            return {
                "data_version": self._version,
                "competition": {"id": self.comp_id, "name": self.cfg.competition.name, "market": self.cfg.competition.market,
                                "currency": self.currency, "started_at": self.comp.get("started_at"), "round_no": self.round_no,
                                "initial_cash": self.initial_cash, "timezone": self.cfg.competition.timezone,
                                "accelerate": self.accelerate,
                                "risk": {"warning_margin_risk_ratio": self.cfg.risk.warning_margin_risk_ratio,
                                         "target_margin_risk_min_ratio": self.cfg.risk.target_margin_risk_min_ratio,
                                         "target_margin_risk_max_ratio": self.cfg.risk.target_margin_risk_max_ratio,
                                         "max_margin_risk_ratio": self.cfg.risk.max_margin_risk_ratio,
                                         "liquidation_risk_ratio": self.cfg.risk.liquidation_risk_ratio}},
                "status": {"text": self.status_text, "paused": self.paused, "running": self.running, "market_open": market_open,
                           "next_open": nxt, "next_decision_in": max(0, int(dec_int - (now - self._last_decision))),
                           "last_tick": self._last_tick, "last_round_ms": self._last_round_ms, "server_time": now,
                           "source": public_source_health(self.source.health())},
                "quotes": [{"symbol": s, "name": self.specs[s].name, "price": q.price, "change_pct": q.change_pct, "ts": q.ts,
                            "funding_rate": q.funding_rate, "open_interest": q.open_interest} for s, q in self.quotes.items()],
                "traders": traders,
                "preopen_debate": self._preopen_debate_state(),
                "product_research": {
                    **self.product_research.public_status(),
                    "readiness": self._product_research_readiness(),
                },
            }


def _avg_hold_minutes(trades: List[dict]) -> Optional[float]:
    """按品种配对开/平仓时间估算平均持仓分钟数。"""
    opens: Dict[str, float] = {}
    durations: List[float] = []
    for t in trades:
        key = t["symbol"]
        if t["action"].startswith("open_"):
            opens.setdefault(key, t["ts"])
        elif key in opens:
            durations.append((t["ts"] - opens.pop(key)) / 60)
    return sum(durations) / len(durations) if durations else None
