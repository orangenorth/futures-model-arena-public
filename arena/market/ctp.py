"""CTP 行情源：对接境内期货公司的程序化接入（上期技术综合交易平台）。

与新浪/OKX 那两个源的根本区别：**CTP 是订阅推送，不是 HTTP 轮询**。登录后一次性订阅全部
合约，之后由行情前置把 tick 推过来，品种数再多也不会产生成百上千次请求。

CTP 只推 tick（最新价、买卖一档、累计成交量、持仓量），**不提供历史 K 线**，所以 1 分钟 bar
由本地从 tick 聚合，5m/15m/1h 再由 1m 重采样。代价是冷启动要攒很久：每个周期**各自**都要
60 根才算得出 ema60。数的是对齐的桶而不是交易小时（横跨开收盘的桶也算一根），按本仓库合约表
逐品种数下来，1h 每个交易日只产出 5～11 根，凑满 60 根要 5.5～12 个交易日（黄金最快、股指与
无夜盘品种最慢）。换来的是完全不依赖任何第三方行情网站，而且 bar 落库后这份代价只付一次。

CTP 订阅的是**具体合约**（rb2601），没有"主力连续"这种东西。本源按品种生成未来若干个月的
候选合约全部订阅，再按持仓量选出主力——持仓量最大的那个就是主力，这也是行业通行口径。

凭证由期货公司开通程序化后单独下发（BrokerID、行情前置地址、InvestorID、密码），与手机/
桌面终端的登录账号是同一个投资者编码，但**必须走程序化接入才能连行情前置**。
配置里只写环境变量名，绝不写明文密码。
"""
from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..exchange.contracts import (CLOSED_SESSION_QUOTE_GRACE_SEC, NIGHT_START_MIN, latest_session_close,
                                  next_open as _next_open, session_open)
from ..models import Bar, ContractSpec, Quote
from .base import SourceError
from .indicators import resample
# 直接借用新浪源里那个常量而不是再写一遍 60：两处各写一份，改了其中一处就会悄悄漂移。
from .sina_cn import INDICATOR_MIN_BARS

log = logging.getLogger("arena.market.ctp")

# CTP 用 DBL_MAX 表示"该字段无值"（例如尚未成交时的最新价），不是一个真实价格
CTP_NULL = 1.7976931348623157e308
MAX_SANE_PRICE = 1e9

# 郑商所合约代码用一位年份（SR601），其余交易所用两位（rb2601）；
# 大商所/上期所/上期能源/广期所小写，郑商所/中金所大写。
SINGLE_DIGIT_YEAR_EXCHANGES = frozenset({"CZCE"})
UPPERCASE_EXCHANGES = frozenset({"CZCE", "CFFEX"})

PERIOD_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
# 引擎的快照每次都先要 5m（`_build_snapshot`），所以基础周期**必须能整除 300 秒**：
# 比 5m 还长就根本给不出 5m，快照构建会对每个品种抛错、被外层吞掉，最终每一轮都是空快照、
# 一个交易员都不会被调用——而且全程只在日志里留下几行 K 线获取失败。
# 能整除 300 的值自然也能整除 900 与 3600，一条规则覆盖引擎要的全部周期。
BASE_PERIOD_CEILING_SEC = 300

# 引擎的快照还要 1h（`_build_snapshot` 里 `klines(s, "1h", 100)`），而 `summarize` 里最长的
# 指标是 ema60：1h 序列不足 60 根，ema60 与 macd/boll 就一直是 None。
MAX_PERIOD_SEC = max(PERIOD_SECONDS.values())

# 基础周期的下限。保留根数与 bar_seconds 成反比（见 retention_floor），再短下去就失控：
# 1 秒的 bar 每个品种要留 216000 根，七十个品种就是上千万个 bar、好几 GB。而 CTP 的行情
# 本身是 500ms 一档的快照，1~5 秒的 bar 并不比 10 秒的多出任何信息。
MIN_BAR_SECONDS = 10

# 交易所时区。CTP 推的 ActionDay/UpdateTime 与合约表里的交易时段都是**交易所本地时间**，
# 与看板用哪个时区显示无关：拿 `competition.timezone` 来解释它们，UTC 下一条 10:00 的 tick
# 就成了八小时之后的未来行情，整批 bar 被新鲜度闸门丢掉，决策轮一轮都跑不起来。
EXCHANGE_TZ = "Asia/Shanghai"

# 选主力时，一个合约"安静了多久"算失效。计的不是墙上时间，而是**观察到的交易时长**：
# 两次 tick 之间的间隔最多按 IDLE_GAP_CAP_SEC 计入，所以午休两小时只折算 5 秒。
# 按墙上时间算的话，每次休市后的第一条 tick 都会让当前主力显得"安静了两小时"，
# 于是谁先回调谁就成了主力——正是观察期要挡的那种假换月，只不过换到了每个交易时段开头。
DOMINANT_STALE_SEC = 300.0
IDLE_GAP_CAP_SEC = 5.0
# 订阅请求失败（异常或非零返回码，例如每秒请求数超限）之后隔这么久再试。合约先记作待订，
# 订上了才从待订里去掉：连接一直没断的话不会再有下一次登录来补，只能靠之后的 tick 重试。
SUBSCRIBE_RETRY_SEC = 30.0
# 一个合约最多发这么多次订阅请求；前置一直不确认（或一直拒绝）就放弃，多半是没挂牌的候选。
SUBSCRIBE_MAX_ATTEMPTS = 3
# 交易所时刻比本机时钟超前这么多秒以上的 tick 整条丢弃（进任何状态之前）。与引擎对报价的
# 未来偏差上限一致：本机时钟慢到这个程度时引擎本来就一条报价都不收，行情源跟着拒绝不改变
# 任何交易行为；而一条日期写错、指向未来的 tick 一旦被记成该合约的"最新时刻"，之后每一条
# 正常的 tick 都会被当成迟到丢掉，这个合约就冻住了，直到那个未来时刻真的到来或者重启。
MAX_TICK_FUTURE_SKEW_SEC = 60.0
# ReqUserLogin 同步被拒（返回非 0）时的重发次数与退避基数：1s、2s 后各重发一次，第三次仍被拒即报登录失败
LOGIN_REQUEST_ATTEMPTS = 3
LOGIN_RETRY_BASE_SEC = 1.0
# 订阅应答里这些错误码是永久性的拒绝（16：找不到合约），不必再试；其余错误按可重试处理。
PERMANENT_SUBSCRIBE_ERRORS = frozenset({16})


def normalize_instrument_overrides(instruments: Optional[Dict[str, str]]) -> Dict[str, str]:
    """把"品种 → 指定合约"的键统一成行情源实际匹配的形式（品种代码大写）。

    写成 `instruments: {rb0: rb2601}` 也是真的把 RB0 钉死了，所以任何据此判断"钉了哪个
    合约"的地方（竞赛身份签名、落库 bar 的命名空间）都必须先过这里。各自用原始键去比，
    `rb0` 会被当成"没配"：把它改成 rb2605 时两边签名都是空的，竞赛照旧沿用，而价格序列
    已经换了一条。合约号本身不动——上期所是 `rb2601`、郑商所是 `AP601`，大小写有含义。
    """
    return {str(k).upper(): str(v).strip() for k, v in (instruments or {}).items()}


def canonical_contract_id(contract: str, exchange: str = "") -> str:
    """把钉死的合约代码写成交易所实际使用的形式：去掉首尾空白，字母部分按交易所大小写。

    校验通过的是去空白的副本，订阅与 `_owner` 查找却用原始字符串的话，`"rb2601 "` 会被
    交易所拒掉、回调里的 `rb2601` 也对不上归属——启动时明明通过了检查，这个品种却永远没数据。
    """
    code = (contract or "").strip()
    match = _CONTRACT_ID_RE.fullmatch(code)
    if match is None or not exchange:
        return code
    letters, digits = match.group(1), match.group(2)
    letters = letters.upper() if exchange in UPPERCASE_EXCHANGES else letters.lower()
    return f"{letters}{digits}"


def retention_floor(bar_seconds: int) -> int:
    """保住 1h 的 ema60 所需的最少基础 bar 根数。

    基础 bar 全靠本地从 tick 累，被淘汰掉就再也补不回来（CTP 没有历史 K 线接口），所以内存里
    的保留根数必须随 `bar_seconds` **反比**缩放：固定 3000 根在 60s 下只覆盖 50 小时行情，
    最坏情况（行情连续、每个 1h 桶都装满）只分得出 50 个 1h 桶；到 30s 就只剩 25 小时，
    `klines("1h")` 怎么跑都给不出 60 根，1h 的慢指标对模型**永久缺失**——不报错、不告警，
    只是 prompt 里少了几行，看上去和"冷启动还没跑满"一模一样。
    按休市间隔算实际桶数会多一些，但下限要按最坏情况取，否则连续夜盘就正好卡在边上。
    """
    return -(-INDICATOR_MIN_BARS * MAX_PERIOD_SEC // max(1, int(bar_seconds)))


def variety_code(symbol: str) -> str:
    """把竞赛用的主力连续代码还原成品种代码：RB0 → RB。"""
    base = (symbol or "").strip().upper()
    return base[:-1] if base.endswith("0") and len(base) > 1 else base


def contract_id(symbol: str, exchange: str, year: int, month: int) -> str:
    """按交易所的命名规则拼出具体合约代码。

    郑商所只写一位年份，所以 2026 年 1 月是 SR601 而不是 SR2601——写错的代码订阅会被拒，
    而且一年后会和十年前的合约撞号，因此这里按交易所分开处理而不是统一格式。
    """
    code = variety_code(symbol)
    code = code.upper() if exchange in UPPERCASE_EXCHANGES else code.lower()
    if exchange in SINGLE_DIGIT_YEAR_EXCHANGES:
        return f"{code}{year % 10}{month:02d}"
    return f"{code}{year % 100:02d}{month:02d}"


_CONTRACT_ID_RE = re.compile(r"([A-Za-z]{1,2})(\d{3,4})")


def pinned_instrument_problem(symbol: str, contract: str, exchange: str = "") -> Optional[str]:
    """`instruments` 里给品种钉死的合约不成立时，返回原因；成立返回 None。

    `instruments: {RB0: au2612}` 原来会照单全收：黄金的每一笔 tick 都以 RB0 的名义发布、落库，
    而估值、保证金、手续费、最小变动价位仍按螺纹钢的规格算，整场实验悄悄报废，连一条错误都没有。
    所以钉的合约必须真的属于这个品种；年份位数也按交易所的命名规则核一遍（郑商所一位年份），
    写错的代码订阅会被拒，这个品种就永远没有数据了。
    """
    code = (contract or "").strip()
    match = _CONTRACT_ID_RE.fullmatch(code)
    if match is None:
        return "不是合约代码（应形如 rb2601 或 AP601）"
    letters, digits = match.group(1).upper(), match.group(2)
    variety = variety_code(symbol)
    if letters != variety:
        return f"属于品种 {letters}，不是 {variety}"
    if exchange:
        expected = 3 if exchange in SINGLE_DIGIT_YEAR_EXCHANGES else 4
        if len(digits) != expected:
            return f"{exchange} 的合约代码年份应为 {expected - 2} 位（如 {contract_id(symbol, exchange, 2026, 1)}）"
    return None


def exchange_today() -> date:
    """交易所所在时区的今天。候选合约按它枚举，不按主机的本地日期。

    主机时区落后于上海时，上海已经进入新的一个月而主机还在上个月：窗口从已经交割完的那个月
    起算，最远的那个月就少订一个；`auto_months: 1` 更是只剩当月加下一个挂牌月，而当月那个
    早就不报价了。
    """
    return datetime.now(ZoneInfo(EXCHANGE_TZ)).date()


_ALL_MONTHS = frozenset(range(1, 13))
_ODD_MONTHS = frozenset({1, 3, 5, 7, 9, 11})
_EVEN_MONTHS = frozenset({2, 4, 6, 8, 10, 12})
_QUARTER_MONTHS = frozenset({3, 6, 9, 12})

# 挂牌交割月不是连续 12 个月的品种（按品种代码）。**只用来把候选窗口往后延，绝不用来收窄**：
# 这张表写错的代价是多订几个不存在的合约（订阅应答里被拒，零成本）；反过来若拿它删候选，
# 写错一处那个品种就永远没有行情。没列出来的品种按连续 12 个月处理。
LISTED_DELIVERY_MONTHS: Dict[str, frozenset] = {
    # 中金所国债：只有季月
    "T": _QUARTER_MONTHS, "TF": _QUARTER_MONTHS, "TS": _QUARTER_MONTHS, "TL": _QUARTER_MONTHS,
    # 郑商所
    "CF": _ODD_MONTHS, "SR": _ODD_MONTHS, "OI": _ODD_MONTHS,
    "RM": frozenset({1, 3, 5, 7, 8, 9, 11}),
    "AP": frozenset({1, 3, 4, 5, 10, 11, 12}), "CJ": frozenset({1, 3, 5, 7, 9, 12}),
    "PK": frozenset({1, 3, 4, 10, 11, 12}),
    "WH": _ODD_MONTHS, "PM": _ODD_MONTHS, "RI": _ODD_MONTHS, "LR": _ODD_MONTHS, "JR": _ODD_MONTHS,
    "RS": frozenset({7, 8, 9, 11}),
    # 大商所
    "A": _ODD_MONTHS, "C": _ODD_MONTHS, "CS": _ODD_MONTHS, "LH": _ODD_MONTHS, "LG": _ODD_MONTHS,
    "M": frozenset({1, 3, 5, 7, 8, 9, 11, 12}), "Y": frozenset({1, 3, 5, 7, 8, 9, 11, 12}),
    # 上期所 / 能源中心
    "RU": frozenset({1, 3, 4, 5, 6, 7, 8, 9, 10, 11}),
    "EC": _EVEN_MONTHS,
}


def listed_delivery_months(symbol: str) -> frozenset:
    """该品种会挂牌的交割月份集合；不在表里的品种按每个月都挂牌。"""
    return LISTED_DELIVERY_MONTHS.get(variety_code(symbol), _ALL_MONTHS)


def candidate_contracts(symbol: str, exchange: str, months: int = 12,
                        today: Optional[date] = None) -> List[str]:
    """该品种未来至少 `months` 个月的候选合约代码（含当月）。

    md 接口无法查询合约列表（那是交易接口的活），所以只能按命名规则枚举再全部订阅；
    订阅是推送式的，多订几个合约几乎没有成本，没挂牌的会在订阅应答里被拒、不影响其余合约。

    **窗口至少延到当月之后下一个挂牌的交割月。** 当月合约在当月中途就到期（股指第三个周五、
    国债第二个周五、商品多在中旬），到期之后到月底这段时间它一笔 tick 都不再有；`auto_months: 1`
    只订当月，那个品种就要一直等到日历翻月才重新有行情。交割月稀疏的品种（国债只有季月、
    苹果没有 6～9 月）用 2、3 个月的预算也会碰到同样的空档，所以按 `listed_delivery_months`
    往后延，而不是让使用者自己去算每个品种的挂牌规则。
    """
    start = today or exchange_today()
    listed = listed_delivery_months(symbol)
    span = max(1, months)
    for ahead in range(1, 13):
        if (start.month - 1 + ahead) % 12 + 1 in listed:
            span = max(span, ahead + 1)
            break
    out: List[str] = []
    for i in range(span):
        month_index = start.month - 1 + i
        year = start.year + month_index // 12
        out.append(contract_id(symbol, exchange, year, month_index % 12 + 1))
    return out


def _finite(value: Any) -> Optional[float]:
    """拒绝 None、布尔、NaN/Inf 与 CTP 的 DBL_MAX 空值哨兵。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or abs(number) >= MAX_SANE_PRICE or number == CTP_NULL:
        return None
    return number


class TickAggregator:
    """把 tick 累成固定长度的 bar。

    成交量在 CTP 里是**当日累计**，不是本 tick 的增量：直接当成 bar 成交量会把一整天的量
    记进每一根 bar。这里存上一次的累计值做差；累计值变小说明换了交易日（夜盘开盘会清零），
    此时重新起算而不是记一个负数。
    """

    def __init__(self, bar_seconds: int = 60, max_bars: int = 3000):
        if bar_seconds < 1:
            raise ValueError(f"bar_seconds 必须 ≥ 1（当前 {bar_seconds}）")
        self.bar_seconds = int(bar_seconds)
        self.max_bars = max(2, int(max_bars))
        self.bars: List[Bar] = []
        self._open_bar: Optional[Bar] = None
        self._last_volume: Optional[float] = None
        self._volume_source: Optional[str] = None   # 累计成交量属于哪个合约
        # 当前这根未收口的 bar 由哪个合约供数。None = 未知（预热进来的那半根，或调用方没传
        # source），此时不做换月判定，直接认下来——预热那半根本来就该被后续 tick 并进去。
        self._bar_source: Optional[str] = None

    def _bucket(self, ts: float) -> float:
        return ts - (ts % self.bar_seconds)

    def seed(self, bars: List[Bar]) -> None:
        """用持久化的 bar 预热。**合并而不是替换**：已经收到的实时 bar 一根都不能丢。

        重启正好落在某根 bar 中途时，库里那根是"半根"。把它当已收口的塞进 `bars`，
        重启后同一周期的 tick 会另起一根：去重丢掉库里那半根，则重启前的开/高/低/量全没了；
        不去重则出现两根同 ts。所以库里最后一根留作**未收口**的 bar，后续 tick 直接并进去。

        另一个方向同样要防：行情源在引擎调用本方法**之前**就可能推来 tick（登录订阅发生在
        引擎构造期间，预热在 `Engine.start()` 里）。那时直接覆盖会把已经收到的实时 bar 抹掉，
        当前报价还会退回库里那根陈旧的尾巴。所以只补实时序列**开始之前**的部分，
        同一根则两边合并；顺序谁先谁后都得到同一个结果。
        """
        ordered = sorted(bars, key=lambda b: b.ts)[-self.max_bars:]
        if not ordered:
            return
        live = self.bars + ([self._open_bar] if self._open_bar is not None else [])
        if not live:
            self.bars = ordered[:-1]
            last = ordered[-1]
            self._open_bar = Bar(ts=last.ts, open=last.open, high=last.high, low=last.low,
                                 close=last.close, volume=last.volume, hold=last.hold)
            self._bar_source = None      # 同上：预热那半根的来源未知
        else:
            first_live = live[0].ts
            older = [b for b in ordered if b.ts < first_live]
            # **同一根桶上不再把库里那半截并进实时那根。** 落库的 bar 没有记来源合约，
            # 而重启后第一笔 tick 完全可能来自新选出的主力：两段价位差几百个跳价，并起来就是
            # 一段没发生过的行情，还会被落库、喂进 ATR。上一轮只清了 _bar_source，但合并本身
            # 已经把假行情做出来了——来源连续性证明不了的时候，正确做法是丢掉库里那半截。
            # 代价只有重启那一瞬间的一根未收口的 bar；更早的历史照常接在前面（各自成根，不合并）。
            merged = older + live
            self.bars = merged[:-1][-self.max_bars:]
            self._open_bar = merged[-1]
            return      # 有实时状态时，累计成交量的基准是本进程自己攒的，必须留着（见下）
        # 只有冷启动（预热之前一条 tick 都没收到）才清空累计成交量的基准：库里那些 bar 来自
        # 上一次运行，与本次的累计计数器不可比。已经收到过 tick 时那份基准是本进程自己的，
        # 清掉的话下一条 tick 会被当成"首个 tick"重新起算，它那一段增量就凭空消失
        # （100 → 110 → 预热 → 120，本该 +20 却只记了 +10），vol_mean20 也跟着失真。
        self._last_volume = None
        self._volume_source = None

    def add(self, ts: float, price: float, cumulative_volume: Optional[float],
            open_interest: Optional[float], source: Optional[str] = None) -> bool:
        """累进一笔 tick。返回它有没有被接受——**调用方必须看这个返回值**。

        丢弃是静默的，而调用方往往紧接着就把这笔 tick 的时间戳当成"最新报价时刻"发布出去。
        那个时间戳比已经发布的旧，报价就凭空显得陈旧，引擎会把这个品种移出本轮决策
        （持仓品种更是整轮停下），而价格其实一点没变。
        """
        bucket = self._bucket(ts)
        current = self._open_bar
        if current is not None and bucket < current.ts:
            # 迟到的 tick：既不回写已经收口的 bar，**也不能动累计成交量的基准**。
            # 它的累计值比当前低，把基准拉下去之后，下一个正常 tick 的增量会把中间那段重复计入
            # （基准 200 → 迟到 150 → 下一个 210 就变成 +60 而不是 +10），vol_mean20 随之失真。
            return False
        step = 0.0
        if cumulative_volume is not None:
            if (self._last_volume is None or cumulative_volume < self._last_volume
                    or source != self._volume_source):
                # 首个 tick、换日清零，或者主力换到了另一个合约：两个合约的累计成交量毫无关系，
                # 直接相减会凭空造出一个巨大的增量。这三种情况都从这里重新起算。
                step = 0.0
            else:
                step = cumulative_volume - self._last_volume
            self._last_volume = cumulative_volume
            self._volume_source = source
        hold = open_interest if open_interest is not None else 0.0

        if current is None or bucket > current.ts:
            if current is not None:
                self._append(current)
            self._open_bar = Bar(ts=bucket, open=price, high=price, low=price, close=price,
                                 volume=step, hold=hold)
            self._bar_source = source
            return True
        if source is not None and source != self._bar_source:
            # 换月正好发生在这根 bar 中途，**或者**这根 bar 是预热进来的、来源根本不可证
            # （冷启动时 _bar_source 是 None）。两个合约的价位可以差出几百个跳价，并进同一根
            # 会造出一段从没发生过的高低点——它会被落库、喂进 ATR、再喂给模型。已经收口的 bar
            # 永不改写，所以这里只能把这根未收口的重开：本桶从这个来源开始记，原来那半截作废。
            # "来源未知"不等于"来源相同"：这正是上一轮只修了有实时状态那条分支留下的洞。
            self._open_bar = Bar(ts=bucket, open=price, high=price, low=price, close=price,
                                 volume=step, hold=hold)
            self._bar_source = source
            return True
        current.high = max(current.high, price)
        current.low = min(current.low, price)
        current.close = price
        current.volume += step
        current.hold = hold
        return True

    def _append(self, bar: Bar) -> None:
        self.bars.append(bar)
        if len(self.bars) > self.max_bars:
            del self.bars[: len(self.bars) - self.max_bars]

    def snapshot(self) -> List[Bar]:
        """已收口的 bar 加上当前未收口的那根。

        不把未收口的 bar 排除在外：决策轮正好落在一分钟中间时，排除它等于丢掉最新一分钟的
        全部价格信息，指标会比行情慢整整一根。
        """
        out = list(self.bars)
        if self._open_bar is not None:
            out.append(Bar(ts=self._open_bar.ts, open=self._open_bar.open, high=self._open_bar.high,
                           low=self._open_bar.low, close=self._open_bar.close,
                           volume=self._open_bar.volume, hold=self._open_bar.hold))
        return out


class CtpMarketSource:
    """CTP 行情源。

    `sdk` 可注入：真实运行时是 `openctp_ctp.mdapi`，测试里换成假的即可在没有原生库、
    没有网络的环境下跑完整条链路。构造时不连接，`start()` 才登录订阅。
    """

    name = "ctp"

    def __init__(self, symbols: List[str], specs: Dict[str, ContractSpec], holidays: List[str],
                 md_front: str, broker_id: str, user_id: str, password: str,
                 instruments: Optional[Dict[str, str]] = None, auto_months: int = 12,
                 flow_dir: str = "data/ctp_flow", connect_timeout_sec: float = 20.0,
                 bar_seconds: int = 60, max_bars: int = 3600, tz: str = EXCHANGE_TZ,
                 discovery_grace_sec: float = 3.0, dominant_stale_sec: float = DOMINANT_STALE_SEC,
                 sdk: Any = None, clock: Callable[[], float] = time.monotonic):
        self.symbols = list(symbols)
        self._specs = specs
        self.holidays = holidays
        self.tz = ZoneInfo(tz)
        self.md_front = (md_front or "").strip()
        self.broker_id = (broker_id or "").strip()
        self.user_id = (user_id or "").strip()
        self._password = password or ""
        self.flow_dir = flow_dir
        self.connect_timeout_sec = float(connect_timeout_sec)
        self.bar_seconds = int(bar_seconds)
        if self.bar_seconds < MIN_BAR_SECONDS:
            raise ValueError(
                f"bar_seconds={self.bar_seconds} 太短（下限 {MIN_BAR_SECONDS} 秒）："
                f"1h 的 ema60 要 {retention_floor(self.bar_seconds)} 根基础 bar 才凑得齐，"
                f"几十个品种就是几 GB 内存；而 CTP 的行情是 500ms 一档的快照，再细也没有新信息。"
            )
        if BASE_PERIOD_CEILING_SEC % self.bar_seconds:
            # 一条规则同时挡住两种坏配置：
            # 1) 比 5m 还长（900/1800/3600）——快照永远建不出来，整机不决策；
            # 2) 不整除 300（90/200）——一根基础 bar 横跨 5m 边界，resample() 按起始时间戳分桶，
            #    边界之后的成交被算进前一个桶，指标悄悄变形且不报错。
            # 两种都是启动时就能判定的，没必要等到出数据（或者不出数据）才发现。
            allowed = [n for n in (10, 15, 20, 30, 60, 100, 150, 300) if BASE_PERIOD_CEILING_SEC % n == 0]
            raise ValueError(
                f"bar_seconds={self.bar_seconds} 无法整除 {BASE_PERIOD_CEILING_SEC} 秒（引擎的快照每次都要 5m）："
                f"比 5m 长就给不出 5m、快照会一直是空的；不整除则一根基础 bar 会横跨 5m 边界、"
                f"重采样出来的 OHLC 是错的。请改用能整除 300 的值，例如 {allowed}"
            )
        # 保留根数按 bar_seconds 反比缩放（见 retention_floor），配小了直接抬上去：
        # 少留了不会报错，只是 1h 的慢指标永远算不出来，没有理由把这个算术留给使用者。
        floor = retention_floor(self.bar_seconds)
        self.max_bars = max(2, int(max_bars))
        if self.max_bars < floor:
            log.warning("max_bars=%d 在 bar_seconds=%d 下只覆盖 %.1f 小时，凑不满 1h 的 %d 根（ema60）；已抬到 %d",
                        self.max_bars, self.bar_seconds, self.max_bars * self.bar_seconds / 3600.0,
                        INDICATOR_MIN_BARS, floor)
            self.max_bars = floor
        self.auto_months = int(auto_months)
        # 选主力的观察期：从该品种的第一条 tick 起等这么久再开始发布 bar
        self.discovery_grace_sec = max(0.0, float(discovery_grace_sec))
        # 合约安静这么久（按观察到的交易时长算）就不再参与选主力
        self.dominant_stale_sec = max(0.0, float(dominant_stale_sec))
        self._clock = clock        # 单调时钟，测试可注入
        self._sdk = sdk
        self._lock = threading.RLock()
        self._api = None
        self._spi = None
        self._connected = threading.Event()
        self._logged_in = threading.Event()      # 只在登录成功时置位
        self._login_done = threading.Event()     # 收到任何一个登录应答就置位（成功或失败）
        self._closing = False
        self._errors = 0
        self._last_err = ""
        self._last_tick_ts: Optional[float] = None
        self._login_error = ""
        self.stop_event: Optional[threading.Event] = None

        # 品种 → 候选合约；合约 → 品种。显式配置优先，没配的按命名规则枚举。
        self._candidates: Dict[str, List[str]] = {}
        self._owner: Dict[str, str] = {}
        explicit = normalize_instrument_overrides(instruments)
        pinned_by: Dict[str, str] = {}      # 合约（大写）→ 钉了它的品种：同一个合约不能供两个品种
        today = exchange_today()
        # 候选表是按这个月枚举的；进程跨月之后 refresh_candidates() 把新进入窗口的合约补订上
        self._candidates_month: Tuple[int, int] = (today.year, today.month)
        for symbol in self.symbols:
            spec = self._specs.get(symbol)
            exchange = getattr(spec, "exchange", "") or ""
            pinned = explicit.get(symbol.upper())
            if pinned:
                problem = pinned_instrument_problem(symbol, pinned, exchange)
                if problem:
                    raise ValueError(f"instruments 里给 {symbol} 钉的合约 {pinned!r} 无效：{problem}")
                pinned = canonical_contract_id(pinned, exchange)
                other = pinned_by.get(pinned.upper())
                if other:
                    raise ValueError(f"instruments 里 {other} 和 {symbol} 钉的是同一个合约 {pinned!r}，后者会永远没有数据")
                pinned_by[pinned.upper()] = symbol
            ids = [pinned] if pinned else candidate_contracts(symbol, exchange, self.auto_months, today)
            self._candidates[symbol] = ids
            for cid in ids:
                self._owner[cid] = symbol

        # 连续行情按**品种**累积，而且是**发生时就落定**的：当时哪个合约是主力，那一刻的 bar
        # 就来自它。换月只改变之后由谁供数，已经发布出去的 bar 永不改写。
        # 反过来（按合约各存一份、用时取当前主力那份）会让一次换月把整个进程生命期的价格与
        # 成交量追溯性替换掉，落库时还会 upsert 覆盖历史。
        self._agg: Dict[str, TickAggregator] = {s: TickAggregator(self.bar_seconds, self.max_bars)
                                                for s in self.symbols}
        self._ticked: set = set()                 # 收到过 tick 的合约：只有它们参与选主力
        self._last_tick_at: Dict[str, float] = {}  # 品种 → 最后一条 tick 的**真实**时刻
        # 合约 → 最后一条**被接受**的 tick 的交易所时刻。CTP 的回调不保证有序，迟到的那笔
        # 带着旧的持仓量与昨结算，照单全收会让已经退场的合约重新显得持仓量最大（凭空换月）。
        self._contract_tick_ts: Dict[str, float] = {}
        self._discovery_until: Dict[str, float] = {}   # 品种 → 选主力的观察期截止（单调时钟）
        # 品种 → 观察到的交易时长（只在 tick 之间累加，单次间隔封顶 IDLE_GAP_CAP_SEC）；
        # 合约 → 它最后一条 tick 时该品种的交易时长。两者之差就是"这个合约安静了多久"。
        self._activity: Dict[str, float] = {}
        self._symbol_tick_at: Dict[str, float] = {}
        self._contract_activity: Dict[str, float] = {}
        self._open_interest: Dict[str, float] = {}
        self._prev_settlement: Dict[str, float] = {}
        self._dominant: Dict[str, str] = {sym: ids[0] for sym, ids in self._candidates.items() if ids}
        # 品种 → **当前真正在供数**的合约。与 `_dominant` 分开：选主力是按持仓量做的判断，
        # 换月那一刻立即生效；而连续序列里的价、时刻、昨结算、持仓量必须始终来自同一个合约。
        # 新主力的第一笔可能落在已收口的 bar 里被累加器丢掉——这时数据面还不能切，否则报价会
        # 拿旧合约的收盘价去配新合约的昨结算与持仓量，拼出一条自相矛盾的报价。
        self._published: Dict[str, str] = {}
        self._pinned = {sym for sym in self.symbols if explicit.get(sym.upper())}
        # 前置还没确认订上的合约（登录后的整批、跨月新进入窗口的）→ 已发过几次订阅请求；
        # 以及最早何时再发（单调时钟）。`SubscribeMarketData` 返回 0 只表示请求排进了队列，
        # 订没订上要看 OnRspSubMarketData 里逐个合约的应答。
        self._pending_subscriptions: Dict[str, int] = {}
        self._subscribe_retry_at = 0.0
        # 因"找不到合约"被永久拒绝的候选（尚未挂牌的远月）：跨月时重新考虑，否则连接不断就再也不会订它
        self._unlisted: set = set()
        self._future_tick_warned: set = set()      # 已经为未来时刻的 tick 记过日志的合约

    # ---- 连接 ----
    def _load_sdk(self):
        if self._sdk is not None:
            return self._sdk
        try:
            from openctp_ctp import mdapi   # noqa: PLC0415  延迟导入：没有凭证时不该因为缺 SDK 起不来
        except Exception as e:  # noqa: BLE001
            raise SourceError(
                "未安装 CTP 行情 SDK。请先 `pip install openctp-ctp`（Windows 有现成 wheel）；"
                f"原始错误：{e}"
            ) from e
        self._sdk = mdapi
        return mdapi

    def start(self) -> None:
        """连接行情前置、登录并订阅全部候选合约。失败抛 SourceError。"""
        missing = [name for name, value in (("md_front", self.md_front), ("broker_id", self.broker_id),
                                            ("user_id", self.user_id), ("password", self._password)) if not value]
        if missing:
            raise SourceError(f"CTP 行情配置不完整，缺少：{'、'.join(missing)}")
        sdk = self._load_sdk()
        os.makedirs(self.flow_dir, exist_ok=True)
        # CTP 会在 flow 目录里写状态文件，目录必须存在且可写，否则底层直接崩而不是报错
        # 连接与登录的状态每次都要归零：上一次失败后它们仍是置位的，不清掉的话重试会立刻
        # 按上一次的结果再抛一次同样的错——看上去"重试也连不上"，其实压根没重试。
        self._closing = False
        self._connected.clear()
        self._login_done.clear()
        self._logged_in.clear()
        self._login_error = ""
        try:
            self._spi = _MdSpi(self, sdk)
            self._api = sdk.CThostFtdcMdApi.CreateFtdcMdApi(os.path.join(self.flow_dir, ""))
            self._api.RegisterSpi(self._spi)
            self._api.RegisterFront(self.md_front)
            self._api.Init()
            if not self._connected.wait(self.connect_timeout_sec):
                raise SourceError(f"连接 CTP 行情前置超时（{self.md_front}），请确认地址与网络可达")
            if not self._login_done.wait(self.connect_timeout_sec):
                raise SourceError("CTP 行情登录超时：前置已连接但没有收到登录应答")
            if not self._logged_in.is_set():
                # 失败的登录也会唤醒上面的等待，所以必须再看一眼结果：否则口令错也会"启动成功"，
                # 然后安安静静地一条行情都收不到
                raise SourceError(self._login_error or "CTP 行情登录失败")
        except BaseException:
            # `Init()` 之后原生 API 已经带着自己的回调线程跑起来了。启动失败时调用方通常正在
            # 中止（引擎构造抛出去时，桌面壳还没挂上 engine.stop()），没人会再调 close()——
            # 那个线程与 flow 资源就那么留着，可能把退出挂住，也会干扰同一进程里的重试。
            self.close()
            raise
        # 订阅由 _on_login 在登录成功时发起（断线重连后同样要重订），这里不再重复发一次

    def subscribe_all(self) -> None:
        """登录成功后把全部候选合约挂上。先整批记作待确认，前置逐个应答成功才清：没确认的由之后的 tick 重发。"""
        with self._lock:
            self._pending_subscriptions = {cid: 0 for cid in sorted(self._owner)}
            self._unlisted.clear()
            self._subscribe_retry_at = 0.0
        # 订阅是推送式的：一次把全部候选合约挂上，之后行情由前置推过来，不存在"每个 tick 发请求"
        self._flush_pending_subscriptions()

    @property
    def pending_subscriptions(self) -> List[str]:
        """记进候选表、但前置还没确认订上的合约。"""
        with self._lock:
            return list(self._pending_subscriptions)

    def _flush_pending_subscriptions(self) -> bool:
        """把待确认的合约发给前置，等它逐个应答。

        `_owner` 里有、前置却没订上的合约是最难发现的一种故障：候选表看起来齐全，tick 却永远
        不来。所以"订上了"只以 `OnRspSubMarketData` 里该合约的成功应答为准：`SubscribeMarketData`
        返回 0 只表示请求排进了队列，请求失败（异常或非零返回码）与应答拒绝都保留待确认，
        SUBSCRIBE_RETRY_SEC 后再发。同一个合约最多发 SUBSCRIBE_MAX_ATTEMPTS 次（同步失败也算一次），一直没确认就放弃——
        没挂牌的候选每次登录都会这样，属于预期内，不算错误。
        """
        with self._lock:
            api = self._api
            ready = api is not None and self._logged_in.is_set()
            if not self._pending_subscriptions or not ready or self._clock() < self._subscribe_retry_at:
                return False
            exhausted = [cid for cid, attempts in self._pending_subscriptions.items() if attempts >= SUBSCRIBE_MAX_ATTEMPTS]
            for cid in exhausted:
                del self._pending_subscriptions[cid]
            ids = list(self._pending_subscriptions)
        if exhausted:
            log.info("放弃订阅 %d 个发了 %d 次仍未确认的合约（多半是没挂牌的候选，或前置持续拒绝订阅请求）：%s",
                     len(exhausted), SUBSCRIBE_MAX_ATTEMPTS, ", ".join(exhausted))
        if not ids:
            return False
        try:
            rc = api.SubscribeMarketData(ids, len(ids))
            failed = isinstance(rc, int) and rc != 0
            error = f"返回码 {rc}" if failed else ""
        except Exception as e:  # noqa: BLE001
            failed, error = True, str(e)
        with self._lock:
            # 每发一次就记一次，不论同步成败：前置持续同步拒绝（抛异常或非零返回码）时原来不计数，
            # 这批合约永远到不了 SUBSCRIBE_MAX_ATTEMPTS，`symbols: all` 下几百个合约每 30 秒整批重发、
            # 记一条日志，直到进程退出。失败的仍留在待确认里按退避重发，只是次数有上限了。
            # 应答可能已经同步到达并把合约从待确认里去掉了（测试里的假前置就是这样）：
            # 只给还在等的计数，别把已确认的又加回去
            for cid in ids:
                if cid in self._pending_subscriptions:
                    self._pending_subscriptions[cid] += 1
            if not failed:
                self._subscribe_retry_at = self._clock() + SUBSCRIBE_RETRY_SEC   # 给前置留出应答的时间
                log.info("CTP 已发出 %d 个合约的订阅请求（%d 个品种），等前置确认", len(ids), len(self.symbols))
                return True
            self._subscribe_retry_at = self._clock() + SUBSCRIBE_RETRY_SEC
            self._errors += 1
            self._last_err = f"订阅 {len(ids)} 个合约失败：{error}"
        log.warning("CTP 订阅 %d 个合约失败（%s），%.0f 秒后重试", len(ids), error, SUBSCRIBE_RETRY_SEC)
        return False

    def _on_subscribed(self, instrument: str, code: int, message: str) -> None:
        """前置对单个合约的订阅应答。成功或永久拒绝都从待确认里去掉；可重试的拒绝留着，退避后重发。"""
        with self._lock:
            if instrument not in self._pending_subscriptions:
                return          # 迟到或重复的应答：早已确认、或早已放弃
            if code == 0:
                del self._pending_subscriptions[instrument]
                return
            if code in PERMANENT_SUBSCRIBE_ERRORS:
                del self._pending_subscriptions[instrument]
                self._unlisted.add(instrument)
                # 未挂牌的候选合约会在这里被拒，属于预期内：只记日志，不算错误
                log.debug("CTP 订阅 %s 被拒绝（不再重试）：[%s] %s", instrument, code, message)
                return
        log.debug("CTP 订阅 %s 被拒绝（稍后重试）：[%s] %s", instrument, code, message)

    # ---- SPI 回调入口 ----
    def _on_connected(self) -> None:
        self._connected.set()
        self._request_login(0)

    def _request_login(self, attempt: int) -> None:
        """发登录请求。`ReqUserLogin` 同步返回非 0（前置刚断、请求队列满、流控）时不会有 `OnRspUserLogin`：
        不看返回码的话，启动要干等到登录超时才报一个错误的原因，重连后则一直"已连接未登录"、再也不重试。
        按退避重发有限次，仍被拒就当作登录失败明确报出来。"""
        api = self._api
        if api is None or self._closing or not self._connected.is_set() or self._logged_in.is_set():
            return
        req = self._sdk.CThostFtdcReqUserLoginField()
        req.BrokerID = self.broker_id
        req.UserID = self.user_id
        req.Password = self._password
        rc = api.ReqUserLogin(req, attempt + 1)
        if not rc:
            return
        if attempt + 1 < LOGIN_REQUEST_ATTEMPTS:
            log.warning("CTP 登录请求被即时拒绝（返回码 %s），%.0f 秒后重发", rc, LOGIN_RETRY_BASE_SEC * 2 ** attempt)
            timer = threading.Timer(LOGIN_RETRY_BASE_SEC * 2 ** attempt, self._request_login, args=(attempt + 1,))
            timer.daemon = True
            timer.start()
            return
        self._on_login(False, f"登录请求被即时拒绝（返回码 {rc}，已重发 {attempt} 次）")

    def _on_disconnected(self, reason: int) -> None:
        self._connected.clear()
        self._logged_in.clear()
        self._login_done.clear()
        with self._lock:
            self._errors += 1
            self._last_err = f"行情前置断开（0x{reason:x}），SDK 会自动重连"
        log.warning("CTP 行情前置断开：0x%x（SDK 自动重连，重连后需重新登录订阅）", reason)

    def _on_login(self, ok: bool, message: str) -> None:
        if ok:
            self._login_error = ""
            self._logged_in.set()
            self._login_done.set()
            # 断线重连后 CTP 需要重新订阅：登录成功即订阅，这是唯一的订阅入口
            self.subscribe_all()
            return
        self._login_error = f"CTP 行情登录失败：{message}"
        with self._lock:
            self._errors += 1
            self._last_err = self._login_error
        self._logged_in.clear()
        self._login_done.set()   # 唤醒 start()，让它抛出这条明确原因而不是干等到超时

    def _on_error(self, message: str) -> None:
        with self._lock:
            self._errors += 1
            self._last_err = message
        log.warning("CTP 行情错误：%s", message)

    def _tick_timestamp(self, action_day: str, update_time: str, millisec: Any) -> Tuple[float, bool]:
        """用交易所给的业务日期与时间构造时间戳，而不是用本机时钟。

        夜盘跨自然日，`ActionDay` 才是这笔 tick 真正发生的那一天；用本机时间会把夜盘的 bar
        标到错误的日期上，日内切分与"当日首根"的判断跟着全错。
        字段缺失或格式异常时退回本机时钟——总比丢掉这笔 tick 好。

        第二个返回值说明这个时刻是不是**交易所给的**。乱序判定只能拿交易所时刻互相比：
        本机时钟兜底出来的那个与交易所序列不在同一条刻度上，本机时钟快一点就会把它记成
        "最新"，之后所有正常 tick 都比它旧——那个合约的行情会就此永远冻住。
        """
        day = "".join(ch for ch in (action_day or "") if ch.isdigit())
        clock = (update_time or "").strip()
        if len(day) == 8 and clock.count(":") == 2:
            try:
                naive = datetime.strptime(f"{day} {clock}", "%Y%m%d %H:%M:%S")
                ts = naive.replace(tzinfo=self.tz).timestamp()
                ms = _finite(millisec)
                return ts + (ms / 1000.0 if ms is not None and 0 <= ms < 1000 else 0.0), True
            except ValueError:
                pass
        return time.time(), False

    def _receive_day_timestamp(self, update_time: str, millisec: Any, received: float) -> float:
        """用交易所本地的接收日期配上 UpdateTime；跨午夜时（23:59:59 的 tick 在 00:00:01 才到）退回前一天。"""
        local = datetime.fromtimestamp(received, self.tz)
        ts, _ = self._tick_timestamp(local.strftime("%Y%m%d"), update_time, millisec)
        if ts > received + MAX_TICK_FUTURE_SKEW_SEC:
            ts, _ = self._tick_timestamp((local - timedelta(days=1)).strftime("%Y%m%d"), update_time, millisec)
        return ts

    def _on_tick(self, field: Any) -> None:
        instrument = str(getattr(field, "InstrumentID", "") or "").strip()
        symbol = self._owner.get(instrument)
        if not symbol:
            return
        price = _finite(getattr(field, "LastPrice", None))
        if price is None or price <= 0:
            return                     # 尚未成交的合约，最新价是 DBL_MAX：不能当成价格发布出去
        ts, from_exchange = self._tick_timestamp(getattr(field, "ActionDay", ""),
                                                 getattr(field, "UpdateTime", ""),
                                                 getattr(field, "UpdateMillisec", 0))
        received = time.time()
        spec = self._specs.get(symbol)
        if from_exchange and ts > received + MAX_TICK_FUTURE_SKEW_SEC and spec is not None and spec.exchange == "DCE":
            # 大商所夜盘的 ActionDay 填的是**交易日**而不是自然日（周五夜盘写成下周一），按它构造会超前好几天、
            # 整个夜盘被下面的未来时刻闸门全部丢掉。只对大商所、且只在确实超前时改用交易所本地的接收日期。
            ts = self._receive_day_timestamp(getattr(field, "UpdateTime", ""),
                                             getattr(field, "UpdateMillisec", 0), received)
        if from_exchange and ts > received + MAX_TICK_FUTURE_SKEW_SEC:
            # 日期或时间写到了未来：不能让它推进该合约的时钟，否则后面所有正常 tick 都成了"迟到"
            with self._lock:
                first = instrument not in self._future_tick_warned
                self._future_tick_warned.add(instrument)
            if first:
                log.warning("CTP %s 的 tick 时刻 %s 超前本机时钟 %.0f 秒以上，已丢弃（同一合约不再重复记录）",
                            instrument, datetime.fromtimestamp(ts, self.tz).isoformat(), MAX_TICK_FUTURE_SKEW_SEC)
            return
        volume = _finite(getattr(field, "Volume", None))
        open_interest = _finite(getattr(field, "OpenInterest", None))
        settlement = _finite(getattr(field, "PreSettlementPrice", None))
        with self._lock:
            if from_exchange:
                previous_ts = self._contract_tick_ts.get(instrument)
                if previous_ts is not None and ts < previous_ts:
                    # 这条回调整条作废，**在动任何状态之前**返回：持仓量、昨结算、活跃度、选主力
                    # 都不能被一笔比自己旧的 tick 改写。同一时刻的 tick（CTP 半秒一笔，同毫秒常见）
                    # 不算迟到，照常累进。
                    return
                self._contract_tick_ts[instrument] = ts
            else:
                # 时间字段坏了，_tick_timestamp 退回了本机时钟。那个值绝不能进这条按交易所时刻
                # 排序的序列：本机时钟快出一个 bar 周期，累加器就会开一根未来的 bar，之后所有
                # 正常的交易所 tick 都比它旧而被丢掉，这个合约的行情就此冻住。
                # 改为钉在该合约最后一笔好 tick 的时刻上——价格、持仓量这些有用的东西照收，
                # 只是不让一个坏钟推进时间轴。
                anchor = self._contract_tick_ts.get(instrument)
                if anchor is None:
                    return      # 连一笔好 tick 都还没有过，没有可钉的桩，只能整条丢掉
                ts = anchor
            first_tick = instrument not in self._ticked and symbol not in self._discovery_until
            self._ticked.add(instrument)
            if open_interest is not None:
                self._open_interest[instrument] = open_interest
            if settlement is not None and settlement > 0:
                self._prev_settlement[instrument] = settlement
            self._last_tick_ts = time.time()
            now = self._clock()
            previous_tick = self._symbol_tick_at.get(symbol)
            if previous_tick is not None:
                # 封顶：休市/夜盘间隙那种长间隔只折算一点点，不能让当前主力显得已经失效
                self._activity[symbol] = self._activity.get(symbol, 0.0) + min(now - previous_tick, IDLE_GAP_CAP_SEC)
            self._symbol_tick_at[symbol] = now
            self._contract_activity[instrument] = self._activity.get(symbol, 0.0)
            if first_tick:
                # 该品种的第一条 tick：开一个观察期。启动瞬间 `_ticked` 是空的，谁先回调谁就成了
                # "当前持仓量最大"，而各候选合约的首个回调顺序完全由网络决定。不等一等的话，
                # 近月先到、真正的主力后到就会被当成一次换月，两个价位截然不同的合约被并进同一根
                # bar，凭空造出高低点与 ATR——而那只是回调顺序，不是市场发生了什么。
                self._discovery_until[symbol] = now + self.discovery_grace_sec
            self._refresh_dominant(symbol)
            if now < self._discovery_until.get(symbol, 0.0):
                return          # 观察期内只收集持仓量，不发布 bar
            # 非主力合约的 tick 只用来更新持仓量（选主力要它），不进连续序列。
            # 例外：换月已经选出来、但新主力还没有一笔被累加器收下时，**旧主力继续供数**——
            # 否则这段时间里两边的 tick 都进不去，行情会冻在旧主力最后那个收盘价上。
            publishing = self._published.get(symbol)
            if self._dominant.get(symbol) == instrument or instrument == publishing:
                # 换月之后新主力的第一笔 tick 也可能落在旧主力已经收口的 bar 里（两个合约各自
                # 有序，合到一个连续序列上就未必了）。那种情况上面的按合约判定拦不住，只能看
                # 累加器收没收下——没收下就不发布这个时刻，否则报价会倒退回去。
                if self._agg[symbol].add(ts, price, volume, open_interest, source=instrument):
                    if publishing != instrument:
                        log.info("%s 连续行情改由 %s 供数（原 %s）", symbol, instrument, publishing)
                    self._published[symbol] = instrument
                    self._last_tick_at[symbol] = ts

    def _refresh_dominant(self, symbol: str) -> None:
        """按持仓量选主力：持仓量最大的合约就是主力，这是行业通行口径。

        只在有过 tick 的候选里选——没挂牌或没人交易的合约持仓量是 0 或根本没有数据。
        显式在配置里指定了合约的品种不参与自动选择。

        **已经不报价的合约要退出候选**：到期或停牌之后，它最后那个持仓量会永远留在表里。
        如果它比所有还活着的后继都大，后继的 tick 就会一直被当成"非主力"丢掉——该品种的报价与
        K 线从此冻结，而整体健康状态还是正常的（tick 一直在来），看不出任何异常。
        失效按**观察到的交易时长**算（见 IDLE_GAP_CAP_SEC），所以午休两小时不会把主力算没了。
        全都不活跃（休市）时维持现状：没有依据的时候不换月。
        """
        if symbol in self._pinned:
            return
        clock = self._activity.get(symbol, 0.0)
        best, best_oi = None, -1.0
        for cid in self._candidates.get(symbol, []):
            if cid not in self._ticked:
                continue
            if clock - self._contract_activity.get(cid, 0.0) > self.dominant_stale_sec:
                continue
            oi = self._open_interest.get(cid, 0.0)
            if oi > best_oi:
                best, best_oi = cid, oi
        if best is None:
            return
        previous = self._dominant.get(symbol)
        if previous != best:
            log.info("%s 主力合约切换：%s → %s（持仓量 %.0f）", symbol, previous, best, best_oi)
        self._dominant[symbol] = best

    # ---- MarketSource ----
    def specs(self) -> Dict[str, ContractSpec]:
        return self._specs

    def dominant_contract(self, symbol: str) -> Optional[str]:
        """按持仓量选出的主力。数据面可能还没跟上——见 `_published` 与 `publishing_contract()`。"""
        with self._lock:
            return self._dominant.get(symbol)

    def publishing_contract(self, symbol: str) -> Optional[str]:
        """当前连续序列里的价、时刻、昨结算、持仓量来自哪个合约。"""
        with self._lock:
            return self._published.get(symbol)

    def refresh_candidates(self, today: Optional[date] = None) -> List[str]:
        """交割月往前推进时，把新进入窗口的候选合约补订上；返回新订的合约代码。

        候选表只在构造时枚举过一次，断线重连也只是把同一批 `_owner` 重订一遍。进程一直跑着
        跨过到期日的话——`auto_months: 1` 在 3 月启动订的是 3、4 月合约，4 月合约到期之后
        5 月合约没人订——这个品种就一直没有报价，直到重启。所以每个交易所日历月检查一次：
        按今天重新枚举，没订过的补订。**只增不减**：到期合约不再报价，选主力时自然按活跃度退出，
        订阅本身留着没有成本。新合约先记作待订：没登录时等登录后的 `subscribe_all()` 一并订上；
        登录着但订阅请求失败（异常或非零返回码）时，连接不断就没有下一次登录来补，所以待订的
        合约由之后每次调用（也就是引擎的每个 tick）按 SUBSCRIBE_RETRY_SEC 的间隔重试，订上才算数。
        """
        today = today or exchange_today()
        month = (today.year, today.month)
        added: List[str] = []
        with self._lock:
            if month != self._candidates_month:
                self._candidates_month = month
                for symbol in self.symbols:
                    if symbol in self._pinned:
                        continue
                    exchange = getattr(self._specs.get(symbol), "exchange", "") or ""
                    for cid in candidate_contracts(symbol, exchange, self.auto_months, today):
                        if cid in self._owner:
                            if cid in self._unlisted:
                                # 当初因未挂牌被拒、现在随日历进入窗口的远月：重新订一次
                                self._unlisted.discard(cid)
                                added.append(cid)
                            continue
                        self._owner[cid] = symbol
                        self._candidates.setdefault(symbol, []).append(cid)
                        added.append(cid)
                if added:
                    for cid in added:
                        self._pending_subscriptions.setdefault(cid, 0)
                    self._subscribe_retry_at = 0.0      # 新合约立刻试一次，不等上一次失败的退避
        if added:
            log.info("交割月推进到 %d-%02d，补订 %d 个候选合约：%s", month[0], month[1], len(added), ", ".join(added))
        self._flush_pending_subscriptions()
        return added

    def latest(self, symbols: List[str]) -> Dict[str, Quote]:
        """当前报价。没有实时 tick 的品种不返回（**预热的历史不算**）——引擎按缺报价处理。"""
        # 引擎每个 tick 都来问一次报价：顺手看一眼交易所日历有没有跨月，候选窗口要跟着往前推
        self.refresh_candidates()
        out: Dict[str, Quote] = {}
        with self._lock:
            for symbol in symbols:
                agg = self._agg.get(symbol)
                if agg is None:
                    continue
                bars = agg.snapshot()
                if not bars:
                    continue
                tick_at = self._last_tick_at.get(symbol)
                if tick_at is None:
                    # 只有预热进来的历史、还没有过（观察期之后的）实时 tick。库里的尾巴在盘中重启时
                    # 可能只有几分钟旧，直接当成当前报价的话，引擎会立刻拿它盯市、触发止损/强平，
                    # 还会开一轮要花钱的决策——而此时连哪个合约是主力都还没定下来，涨跌幅更是拿
                    # 新选出的合约的昨结算去比旧合约的收盘价。预热只给指标用，不给报价用。
                    continue
                out[symbol] = self._published_quote(symbol, bars[-1], tick_at)
        return out

    def _published_quote(self, symbol: str, last: Bar, tick_at: float) -> Quote:
        """由实时 tick 供出的报价（调用方持锁）。"""
        # 昨结算与持仓量必须来自**真正供出这个价的那个合约**，不是刚选出来的主力：
        # 混用会拼出一条自相矛盾的报价（旧合约的收盘价 ÷ 新合约的昨结算）。
        instrument = self._published.get(symbol) or self._dominant.get(symbol)
        # 涨跌幅相对昨结算：这是国内期货的通行口径，也是 CTP 直接给出的参照价
        reference = self._prev_settlement.get(instrument)
        change_pct = 0.0
        if reference:
            change_pct = (last.close / reference - 1) * 100
            if not math.isfinite(change_pct):
                change_pct = 0.0
        # 报价的时间戳用**这笔 tick 真实发生的时刻**，不是它所属 bar 的桶起点。
        # bar_seconds=300 时，10:04:59 的 tick 桶起点是 10:00:00，一过 10:05 就显得
        # "超过 300 秒没更新"，而它其实只有几秒钟——持仓品种一旦这样，整轮决策都会被拦下。
        return Quote(symbol=symbol, price=last.close, ts=tick_at, change_pct=change_pct,
                     open_interest=self._open_interest.get(instrument, 0.0))

    def closed_session_quotes(self, symbols: List[str], now: Optional[float] = None) -> Dict[str, Quote]:
        """**只给盘前研究用**的上一节收盘价：该品种最近一节已结束的交易时段的收盘，不管这一节的
        行情是本进程收到的还是预热进来的。给不出收盘的品种一律不返回。实盘执行路径不走这里，
        `latest()` 原样不动。

        两种来源，同一条界限：
        - **只有预热的历史**（午后重启，15:00 收盘后、21:00 夜盘前）：只有日盘的品种当晚不会再有
          一笔 tick，`latest()` 按设计不返回它们——预热的历史不给盯市、不给风控、不给开决策轮。
          可是 21:00 的自动研究要求每个配置品种都有报价，新鲜度只要求这次要开盘的品种；日盘品种
          一缺席，混合日夜盘的品种表在午后重启之后就永远出不了夜盘研究。这里补的正是不重启时
          本来会挂着的那条 15:00 报价。时刻取库里最后那根 bar 的桶起点（库里没记最后一笔 tick 的
          真实时刻，往前取不会错标成未来），涨跌幅没有昨结算可比、记 0。
        - **有实时 tick**（进程一直开着）：`latest()` 缓存的那条报价就是这一节的最后一笔，但它同样
          可能停在 14:00——行情源盘中断了、收盘前没接回来。研究不能因为它"在缓存里"就免检，
          所以走同一道收盘校验，通过了才原样给出（真实 tick 时刻、相对昨结算的涨跌幅）。

        界限：最后一根 bar 必须**收在最近一节已结束时段的收盘上**（允许 CLOSED_SESSION_QUOTE_GRACE_SEC
        的提前量：清淡品种收盘前几分钟可能没有报价更新，CTP 没有变化就不推）。停在 14:00 的是盘中价
        不是收盘价；停在上一个交易日的说明中间整整漏了一节——两种都不是"上一节的收盘"，宁可让研究
        报"无行情"，也不把一个盘中价或上周的价格当成收盘送进模型。
        """
        current = time.time() if now is None else float(now)
        out: Dict[str, Quote] = {}
        with self._lock:
            for symbol in symbols:
                agg, spec = self._agg.get(symbol), self._specs.get(symbol)
                if agg is None or spec is None:
                    continue
                bars = agg.snapshot()
                if not bars:
                    continue
                last = bars[-1]
                if not (math.isfinite(last.ts) and math.isfinite(last.close)) or last.close <= 0 or last.ts > current:
                    continue
                close_ts = self._latest_session_close(spec, current)
                # 比的是桶起点：最后一笔 tick 不早于桶起点，而 bar_seconds 整除 300、收盘时刻落在 5 分钟整点上，
                # 所以"桶起点 ≥ 收盘 − 提前量"恰好等于"最后一笔 tick ≥ 收盘 − 提前量"。不能再加 bar_seconds：
                # 5m bar 下那会把提前量放宽到十分钟。
                if (close_ts is None or last.ts > close_ts
                        or last.ts < close_ts - CLOSED_SESSION_QUOTE_GRACE_SEC):
                    continue
                tick_at = self._last_tick_at.get(symbol)
                if tick_at is not None:
                    out[symbol] = self._published_quote(symbol, last, tick_at)
                else:
                    out[symbol] = Quote(symbol=symbol, price=last.close, ts=last.ts, change_pct=0.0,
                                        open_interest=last.hold)
        return out

    def _latest_session_close(self, spec: ContractSpec, current: float,
                              lookback_days: int = 20) -> Optional[float]:
        """该品种最近一节**已经结束**的交易时段的收盘时刻（≤ current）。见 `latest_session_close`。"""
        return latest_session_close(spec, current, self.tz, self.holidays, lookback_days)

    def _continuous_bars(self, symbol: str) -> List[Bar]:
        """该品种的连续行情。预热的历史与实时 bar 本来就在同一个序列里。"""
        with self._lock:
            agg = self._agg.get(symbol)
            return agg.snapshot() if agg else []

    def klines(self, symbol: str, period: str, limit: int = 120) -> List[Bar]:
        bars = self._continuous_bars(symbol)
        if not bars:
            raise SourceError(f"{symbol} 尚无 CTP 行情（未收到 tick 或该品种未开市）")
        seconds = PERIOD_SECONDS.get(period)
        if seconds is None:
            raise SourceError(f"CTP 行情不支持周期 {period}")
        if seconds < self.bar_seconds:
            # 聚合只能从细到粗：bar_seconds=300 时造不出真正的 1m，返回 5m 的 bar 冒充 1m
            # 等于给指标贴错标签，不如直接报错
            raise SourceError(f"{period} 短于聚合周期 {self.bar_seconds}s，无法从 CTP tick 还原")
        if seconds % self.bar_seconds:
            # 构造时已经挡掉了，这里是第二道：以后若放宽了 bar_seconds 的取值，
            # 横跨边界的重采样也不会悄悄溜过去
            raise SourceError(f"{period} 不是聚合周期 {self.bar_seconds}s 的整数倍，重采样会横跨边界")
        if seconds == self.bar_seconds:
            return bars[-limit:]
        return resample(bars, seconds)[-limit:]

    def bars_1m(self, symbol: str) -> List[Bar]:
        """该品种已积累的基础周期 bar（持久化用），含预热的历史。"""
        return self._continuous_bars(symbol)

    def seed_bars(self, symbol: str, bars: List[Bar]) -> None:
        """用数据库里存的历史 bar 预热，避免每次重启都从零积累。

        按品种存：主力换月时这段历史照样有效（存的本来就是连续行情）。
        最后一根留作未收口的 bar，重启后同一分钟的 tick 直接并进去。
        """
        if not bars:
            return
        with self._lock:
            agg = self._agg.get(symbol)
            if agg is not None:
                agg.seed(bars)

    def _exchange_time(self, now: datetime) -> datetime:
        return now.replace(tzinfo=self.tz) if now.tzinfo is None else now.astimezone(self.tz)

    def is_open(self, now: datetime, symbol: Optional[str] = None) -> bool:
        exchange_now = self._exchange_time(now)
        syms = [symbol] if symbol else self.symbols
        return any(session_open(self._specs[s], exchange_now, self.holidays) for s in syms if s in self._specs)

    def next_open(self, now: datetime) -> Optional[datetime]:
        cands = self.next_opens(now)
        return cands[0] if cands else None

    def next_opens(self, now: datetime) -> List[datetime]:
        """每个品种各自的下一次开市时刻（去重、升序）；盘前研究据此挑受支持的相位，见 SinaFuturesSource。"""
        exchange_now = self._exchange_time(now)
        cands = {_next_open(self._specs[s], exchange_now, self.holidays) for s in self.symbols if s in self._specs}
        return sorted(c for c in cands if c)

    def health(self) -> dict:
        with self._lock:
            subscribed = len(self._ticked)
            pending = len(self._pending_subscriptions)
            detail = f"已登录，{subscribed}/{len(self._owner)} 个合约有行情"
            if pending:
                detail += f"，{pending} 个待前置确认订阅"
            return {
                "source": self.name,
                "ok": self._logged_in.is_set() and self._connected.is_set() and not self._login_error,
                "errors": self._errors,
                "last_ok": self._last_tick_ts,
                "detail": self._last_err or detail,
            }

    def close(self) -> None:
        self._closing = True
        api, self._api = self._api, None
        if api is not None:
            try:
                api.Release()
            except Exception as e:  # noqa: BLE001
                log.warning("释放 CTP 行情 API 失败（已忽略）：%s", e)
        self._spi = None


def _spi_base(sdk: Any):
    return getattr(sdk, "CThostFtdcMdSpi", object)


def _make_spi_class(sdk: Any):
    """SPI 必须继承 SDK 的基类，而基类只有拿到 sdk 之后才知道——所以在运行时造这个类。"""

    class _Spi(_spi_base(sdk)):   # type: ignore[misc]
        def __init__(self, source: "CtpMarketSource"):
            super().__init__()
            self._source = source

        def OnFrontConnected(self):
            self._source._on_connected()

        def OnFrontDisconnected(self, nReason):
            self._source._on_disconnected(int(nReason))

        def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
            code = int(getattr(pRspInfo, "ErrorID", 0) or 0) if pRspInfo is not None else 0
            message = str(getattr(pRspInfo, "ErrorMsg", "") or "") if pRspInfo is not None else ""
            self._source._on_login(code == 0, f"[{code}] {message}" if code else message)

        def OnRspError(self, pRspInfo, nRequestID, bIsLast):
            code = int(getattr(pRspInfo, "ErrorID", 0) or 0) if pRspInfo is not None else 0
            message = str(getattr(pRspInfo, "ErrorMsg", "") or "") if pRspInfo is not None else ""
            self._source._on_error(f"[{code}] {message}")

        def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
            code = int(getattr(pRspInfo, "ErrorID", 0) or 0) if pRspInfo is not None else 0
            instrument = str(getattr(pSpecificInstrument, "InstrumentID", "") or "").strip()
            message = str(getattr(pRspInfo, "ErrorMsg", "") or "") if pRspInfo is not None else ""
            if instrument:
                try:
                    self._source._on_subscribed(instrument, code, message)
                except Exception as e:  # noqa: BLE001
                    log.warning("处理 CTP 订阅应答失败（已忽略）：%s", e)

        def OnRtnDepthMarketData(self, pDepthMarketData):
            try:
                self._source._on_tick(pDepthMarketData)
            except Exception as e:  # noqa: BLE001
                # 回调跑在 SDK 自己的线程上，异常逃出去会直接掐断行情推送
                log.warning("处理 CTP tick 失败（已忽略）：%s", e)

    return _Spi


def _MdSpi(source: "CtpMarketSource", sdk: Any):
    return _make_spi_class(sdk)(source)
