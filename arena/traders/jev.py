"""Jev (TypeSafe System One) decision trader.

Jev is TypeSafe's System One model. Instead of generating prose that has to be
parsed back into orders, it evaluates *typed questions* against a *state* and
returns typed answers with probability distributions (``choice`` / ``score`` /
``noul``). This module adapts that contract to the arena:

* ``state`` is byte-identical to the prompt the LLM seats receive (``build_prompt``),
  so every contestant judges the same snapshot. The JSON output instructions inside
  that text are inert here: a System One model emits no text.
* The decision is decomposed into atomic questions (for each symbol: open long, open
  short or no new entry; how large; stand aside or not; and -- for each open
  position -- close it or tighten its stop) and composed back into ``ActionRequest``
  objects in code -- the usage TypeSafe recommends. The seat gets the same action set
  as the text seats; without the position questions it could only ever open and then
  wait for a stop or a liquidation, which would make its results incomparable with
  theirs.
* Entry is judged **per symbol**, not as one "pick a symbol out of N" choice. A single
  N-way choice spreads probability across every acceptable candidate, so its winner
  rarely clears a gate and gets weaker as the universe grows (2026-10-08 online trial:
  the top of 8 symbols got p=0.50 / confidence 0.43). Per-item judgements are
  TypeSafe's recommended shape for ranking. The old global "direction" question also
  never said which symbol it was about.
* The gate compares ``jev_min_confidence`` with the **probability of the chosen
  option** (p(long) for a long entry). TypeSafe's ``confidence`` only summarises how
  concentrated a distribution is (a 3-way p=0.63 comes back around 0.45), so it is
  validated and kept in the audit text but does not gate. The chosen probability is
  copied into the request for the risk layer and the dashboard.

``raw_response`` holds the answers JSON rather than generated text, which removes the
truncation / empty-reply / JSON-parse failure modes the text seats suffer from.
``tokens_in`` / ``tokens_out`` are the service's own usage numbers, reported verbatim
(the hosted service does bill output tokens for the answers).

Endpoint: the hosted service (``https://api.typesafe.ai``, API key required) or any
service implementing the same schema (a local LitJev server needs no key).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

from ..config import LlmCfg, TraderCfg
from ..models import ActionRequest, Decision
from .base import AccountView, MarketSnapshot, Trader
from .llm import LlmTrader, classify_error
from .prompt import build_prompt

log = logging.getLogger("arena.traders.jev")

SYSTEMONE_PATH = "/v1/systemone"
DEFAULT_MODEL = "jev-latest"


def effective_model(tcfg: TraderCfg) -> str:
    """The model a Jev seat actually requests; signatures and trader rows must record this, not a blank config."""
    return tcfg.model or DEFAULT_MODEL


# A noul answer is the probability that the statement holds, so 0.5 is the
# neutral point: at or above it the seat declines to open anything this round.
STAND_ASIDE_THRESHOLD = 0.5

# size_pct by probability-weighted score level 0..3. Kept inside risk.max_size_pct.
SIZE_LADDER: Tuple[float, ...] = (0.0, 0.10, 0.20, 0.30)

# 平掉一个已有仓位的门限，与 STAND_ASIDE_THRESHOLD 同为 noul 的中性点。
EXIT_THRESHOLD = 0.5

# 止损收紧档位 → 距最新价多少个 ATR。None = 不调整。Jev 给不出价位，只给判断，
# 所以价位由代码按 ATR 算——这正是"模型只做判断、组合逻辑留在代码里"的用法。
STOP_LADDER: Tuple[Optional[float], ...] = (None, 1.5, 1.0, 0.5)

_DIRECTIONS = {"long": "open_long", "short": "open_short"}
# 每个品种一道开仓题。none 是合法答案（本轮不碰这个品种），不是"缺答案"。
ENTRY_PREFIX = "entry:"
ENTRY_CRITERIA = {"long": "新开多仓", "short": "新开空仓", "none": "不新开仓"}
_SIDE_TEXT = {"long": "多", "short": "空"}

# 研究辩论（赛前研究 / 全品种研究）的类型化适配。Jev 不产出文字，而辩论的结果契约要求论点文本、
# 引用的 fact id 和反证目标：判断（立场、依据、反方依据、反证对象）由 Jev 按类型化问题给出，
# 文本由代码按模板组装，并在每段文字开头标明"类型化判断，非生成文本"。
RESEARCH_STANCES = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性/分歧"}
TYPED_RESEARCH_NOTE = "类型化判断，非生成文本"


@dataclass
class JevResearchResult:
    text: str               # 与文本席位同一份 JSON 契约，交给 parse_opening / parse_rebuttal 校验
    tokens_in: int
    tokens_out: int


def _usage_count(value: Any) -> int:
    """可选遥测里的 token 计数：缺失、布尔、非数值、负数一律按 0（未知）处理，绝不抛异常。"""
    if value is None or isinstance(value, bool):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return count if count >= 0 else 0


class JevContractError(ValueError):
    """HTTP 200 但响应体不合契约（不是 JSON、不是对象、没有 answers）。不可重试。"""


class JevTrader(Trader):
    mode = "jev"

    def __init__(self, tcfg: TraderCfg, lcfg: LlmCfg, stop_event: Optional[threading.Event] = None):
        self.id = tcfg.id
        self.name = tcfg.name
        self.tcfg = tcfg
        self.lcfg = lcfg
        self._client = None
        self._stop_event = stop_event

    def _effective_max_retries(self) -> int:
        """交易员级的重试覆盖优先于全局默认——与文本席位同一条规则。

        原来这里直接读 `lcfg.max_retries`：给这个席位配 `max_retries: 0` 不生效，
        全局是 2 就照样发三次付费请求；反过来调高也不起作用，而这个字段是进竞赛身份签名的。
        """
        return self.tcfg.max_retries if self.tcfg.max_retries is not None else self.lcfg.max_retries

    def decision_timeout_budget_sec(self) -> float:
        """一轮里这个席位最多要多久。Jev 不生成正文，没有 JSON 模式回退那次额外请求。"""
        retries = self._effective_max_retries()
        return (retries + 1) * self.lcfg.timeout_sec + LlmTrader.retry_backoff_seconds(retries)

    # ── transport ────────────────────────────────────────────────────────────
    def _get_client(self):
        if self._client is None:
            import httpx

            headers = {"Content-Type": "application/json"}
            key = self.tcfg.resolved_api_key()
            if key:
                headers["Authorization"] = "Bearer " + key
            self._client = httpx.Client(base_url=(self.tcfg.base_url or "").rstrip("/"),
                                        headers=headers, timeout=self.lcfg.timeout_sec)
        return self._client

    def _call(self, state: str, questions: Dict[str, dict], *,
              timeout_sec: Optional[float] = None) -> Tuple[dict, int, int, str]:
        client = self._get_client()
        body = {"model": effective_model(self.tcfg), "state": state, "questions": questions}
        if timeout_sec is None:
            resp = client.post(SYSTEMONE_PATH, json=body)
        else:
            resp = client.post(SYSTEMONE_PATH, json=body, timeout=timeout_sec)
        resp.raise_for_status()
        # HTTP 200 之后的问题都是响应契约违约，不是传输故障：同一个端点再发一次付费请求
        # 也还是这份坏响应。单独抛 JevContractError，decide() 见到就停，不进重试。
        try:
            payload = resp.json()
        except ValueError as e:   # json.JSONDecodeError / UnicodeDecodeError 都是 ValueError
            raise JevContractError("响应不是合法 JSON: %s" % str(e)[:200]) from e
        if not isinstance(payload, dict):
            raise JevContractError("响应不是 JSON 对象")
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise JevContractError("响应缺少 answers 对象")
        # usage 是可选的计费遥测：缺了、不是对象、或数值写坏了，都不能把一份有效的 answers 判成
        # 调用失败——那会被 decide() 当成可重试错误，再发一次付费请求，最后还可能把答案整个丢掉。
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        tokens_in = _usage_count(usage.get("input_tokens"))
        tokens_out = _usage_count(usage.get("output_tokens"))
        return answers, tokens_in, tokens_out, json.dumps(payload, ensure_ascii=False)

    # ── questions ────────────────────────────────────────────────────────────
    @staticmethod
    def _managed_positions(snap: MarketSnapshot, acc: AccountView) -> List[dict]:
        """本轮可以处置的仓位：品种必须在快照里。

        不在快照里的品种（休市、或已被移出配置）没有可核验的最新价与 ATR——问了也没有依据，
        算出来的止损更是凭空的。那种仓位本轮不动。
        """
        out = []
        for pos in acc.positions:
            symbol = str(pos.get("symbol") or "").strip()
            if symbol and symbol in snap.symbols:
                out.append(pos)
        return out

    def _questions(self, snap: MarketSnapshot, acc: AccountView) -> Dict[str, dict]:
        if not snap.symbols:
            raise ValueError("本轮快照没有任何品种，无法构造问题")
        # 逐品种各问一道开仓题，而不是"N 选 1 挑品种"再加一道不知道指谁的"方向"题：
        # N 选 1 会把概率摊到所有可行的候选上，品种越多赢家越难过门限（见模块说明）。
        questions: Dict[str, dict] = {}
        for sym, view in snap.symbols.items():
            name = (view.spec.name or "").strip() or sym
            questions[ENTRY_PREFIX + sym] = {
                "type": "choice",
                "instructions": "本轮是否对 %s（%s）新开仓？若开仓，方向是什么？" % (name, sym),
                "criteria": dict(ENTRY_CRITERIA),
            }
        questions["size"] = {
            "type": "score",
            "instructions": "若本轮新开一个仓位，仓位应当多大？",
            "criteria": ["不开仓", "轻仓", "半仓", "重仓"],
        }
        questions["stand_aside"] = {
            "type": "noul",
            "instructions": "本轮是否应当空仓观望、不新开仓？",
        }
        # 每个已有仓位两道题：要不要平掉，止损要不要收紧。没有这两道，这个席位开了仓之后
        # 就只能等自动止损/止盈/强平，既不能落袋也不能反手——与文本席位的可用动作不对等。
        for pos in self._managed_positions(snap, acc):
            symbol = str(pos["symbol"]).strip()
            view = snap.symbols[symbol]
            name = (view.spec.name or "").strip() or symbol
            side = _SIDE_TEXT.get(str(pos.get("side") or ""), "")
            questions["exit:" + symbol] = {
                "type": "noul",
                "instructions": "当前持有 %s 的%s仓，现在是否应当平掉？" % (name, side),
            }
            questions["protect:" + symbol] = {
                "type": "score",
                "instructions": "若继续持有 %s 的%s仓，止损应当收紧到什么程度？" % (name, side),
                "criteria": ["不调整", "收紧到 1.5 倍 ATR", "收紧到 1.0 倍 ATR", "收紧到 0.5 倍 ATR"],
            }
        return questions

    # ── answer parsing ───────────────────────────────────────────────────────
    @staticmethod
    def _as_float(value) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        # NaN / Infinity would be rejected by the risk layer and poison size maths.
        return value if value == value and value not in (float("inf"), float("-inf")) else None

    @staticmethod
    def _level_index(key) -> Optional[int]:
        """Level keys arrive as strings in the schema ({"0": 0.05, "1": 0.30}).

        必须是**整数**档位。原来 `int(float(key))` 会把 `"3.9"` 截成 3——一个契约外的键
        被改写成一个合法的最高档，照样下单。档位之间没有"中间值"这回事。
        """
        try:
            value = float(key)
        except (TypeError, ValueError):
            return None
        if value != int(value):
            return None
        return int(value)

    @staticmethod
    def _confidence(answer: dict, key: str) -> Optional[float]:
        """答案自带的置信度。

        **"没给"和"给了但不合契约"是两件事**，这是这个模块反复栽跟头的地方：
        缺失允许（门限判定按"低于门限"处理，朝观望倒）；但只要这个键出现了，它就必须是
        0..1 的有限概率。原来 `"bad"` / `null` / `NaN` 都会被 `_as_float` 变成 `None`，
        与"故意没给"完全同一条路——而账户已有仓位时，`_manage()` 在门限返回之前就跑过了，
        于是一个坏掉的 confidence 照样能把仓位平掉。
        """
        if "confidence" not in answer:
            return None
        value = JevTrader._as_float(answer.get("confidence"))
        if value is None:
            raise ValueError(f"answers.{key}.confidence={answer.get('confidence')!r} 不是数字")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"answers.{key}.confidence={value} 不是 0..1 的概率")
        return value

    def _choice(self, answers: dict, key: str) -> Tuple[str, Optional[float], dict]:
        answer = answers.get(key)
        if not isinstance(answer, dict):
            raise ValueError(f"answers.{key} 缺失或不是对象")
        choice = answer.get("choice")
        if not isinstance(choice, str) or not choice.strip():
            raise ValueError(f"answers.{key}.choice 缺失")
        probs = answer.get("probabilities")
        return choice.strip(), self._confidence(answer, key), probs if isinstance(probs, dict) else {}

    def _entry(self, answers: dict, symbol: str) -> Tuple[str, Optional[float], Optional[float], dict]:
        """一个品种的开仓题：返回 (long|short|none, 所选项的概率, confidence, 概率分布)。

        门限比的是**所选项的概率**，所以它和 confidence 一样要区分"没给"和"给了但坏掉"：
        分布里没有所选项 → None（按低于门限处理，朝观望倒）；给了但不是 0..1 的有限数 → 整条拒绝。
        """
        key = ENTRY_PREFIX + symbol
        choice, conf, probs = self._choice(answers, key)
        choice = choice.casefold()
        if choice not in ENTRY_CRITERIA:
            raise ValueError(f"answers.{key}.choice={choice!r} 不是 long/short/none")
        if choice not in probs:
            return choice, None, conf, probs
        p = self._as_float(probs.get(choice))
        if p is None or not 0.0 <= p <= 1.0:
            raise ValueError(f"answers.{key}.probabilities[{choice!r}]={probs.get(choice)!r} 不是 0..1 的概率")
        return choice, p, conf, probs

    # 越界的档位允许的浮点噪声。3.0000000001 是同一个答案，100 不是。
    _SCORE_EPS = 1e-6

    def _score(self, answers: dict, key: str, levels: int) -> Tuple[int, Optional[float]]:
        """档位答案。**越界一律拒绝，绝不夹紧**。

        原来 `max(0, min(levels - 1, ...))` 把 `{"score": 100}` 变成最高档——一个不在契约里的
        回复直接变成"重仓"或"把止损收到最紧"。契约外的输入必须和坏掉的 noul 一样朝拒绝的方向倒。
        概率分布同理：原来对无法解析或越界的档位键静默跳过，剩下的够权重就算通过，
        于是 `{"0": 0.1, "99": 0.9}` 会被读成"档位 0"——模型真正的答案被悄悄丢掉了。
        """
        answer = answers.get(key)
        if not isinstance(answer, dict):
            raise ValueError(f"answers.{key} 缺失或不是对象")
        if "score" in answer:
            # 给了 score 但坏掉时**不能**退回分布：那等于用另一个字段把一个不合契约的答案
            # 救成合法动作（`{"score": "bad", "probabilities": {"3": 1.0}}` 原来会开最高档）。
            score = self._as_float(answer.get("score"))
            if score is None:
                raise ValueError(f"answers.{key}.score={answer.get('score')!r} 不是数字")
        else:
            score = None
        if score is None:
            # Fall back to the probability-weighted level index when the service
            # omits `score` but still returns distributions. Level keys are strings
            # in the schema, so they are parsed rather than rejected.
            probs = answer.get("probabilities")
            if isinstance(probs, dict) and probs:
                weighted = 0.0
                total = 0.0
                for level, weight in probs.items():
                    w = self._as_float(weight)
                    idx = self._level_index(level)
                    if idx is None or not 0 <= idx <= levels - 1:
                        raise ValueError(f"answers.{key}.probabilities 的档位 {level!r} 不在 0..{levels - 1} 内")
                    if w is None or not 0.0 <= w <= 1.0:
                        # 只判非负是不够的：{"0": 0.1, "3": 100} 归一化之后落在最高档，
                        # 而 100 根本不是一个概率。分布里的每个权重都必须是 0..1。
                        raise ValueError(f"answers.{key}.probabilities[{level!r}]={w} 不是 0..1 的概率")
                    weighted += idx * w
                    total += w
                if abs(total - 1.0) > self._SCORE_EPS:
                    # 合计为正还不够：{"0": 1.0, "3": 1.0} 每个值都在 0..1，归一化之后照样
                    # 落在中间档开仓——但它根本不是一个概率分布。合计必须是 1。
                    raise ValueError(f"answers.{key}.probabilities 的权重合计 {total} 不等于 1")
                score = weighted / total
        if score is None:
            raise ValueError(f"answers.{key} 既没有 score 也没有可用的 probabilities")
        if not -self._SCORE_EPS <= score <= levels - 1 + self._SCORE_EPS:
            raise ValueError(f"answers.{key} 的档位 {score} 不在 0..{levels - 1} 内")
        level = max(0, min(levels - 1, int(round(score))))
        return level, self._confidence(answer, key)

    @staticmethod
    def _noul(answers: dict, key: str) -> float:
        """必答的 noul：拿不到一个 0..1 的有限概率就整条拒绝，**绝不能当成"没有异议"**。

        `stand_aside` 与 `exit:<品种>` 都是安全方向的答案。原来非数字就返回 None，而调用方把
        None 读成"没有否决"——一个 `{"stand_aside": {}}` 的 200 响应会让席位照常开仓，
        一个坏掉的 `exit` 答案会让仓位继续留着。失效必须朝拒绝的方向倒，不能朝下单的方向倒。
        """
        answer = answers.get(key)
        if not isinstance(answer, dict):
            raise ValueError(f"answers.{key} 缺失或不是对象")
        value = JevTrader._as_float(answer.get("noul"))
        if value is None:
            raise ValueError(f"answers.{key}.noul 缺失或不是数字")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"answers.{key}.noul={value} 不是 0..1 的概率")
        return value

    def _tightened_stop(self, pos: dict, view, mult: Optional[float]) -> Optional[float]:
        """按 ATR 算出收紧后的止损；算不出、或者会放宽保护，就返回 None。

        **只收紧、不放宽**：一个已经在亏的仓位把止损往外挪，等于用一次"决策"把风控拆了。
        """
        if mult is None:
            return None
        atr = self._as_float((view.ind_5m or {}).get("atr14"))
        price = self._as_float(getattr(view.quote, "price", None))
        if atr is None or atr <= 0 or price is None or price <= 0:
            return None     # 没有可核验的 ATR 或最新价，就没有算止损的依据
        side = str(pos.get("side") or "")
        current = self._as_float(pos.get("stop_loss"))
        if side == "long":
            candidate = price - atr * mult
            if candidate <= 0 or candidate >= price:
                return None                 # 止损压到现价之上会当场触发
            if current is not None and candidate <= current:
                return None                 # 不比现有的更紧
        elif side == "short":
            candidate = price + atr * mult
            if candidate <= price:
                return None
            if current is not None and candidate >= current:
                return None
        else:
            return None
        return candidate

    def _manage(self, answers: dict, snap: MarketSnapshot,
                acc: AccountView) -> Tuple[List[ActionRequest], List[str], Dict[str, str],
                                           Dict[str, Optional[float]]]:
        """已有仓位的处置：平掉，或收紧止损。

        与 `stand_aside` 无关——"本轮不新开仓"不等于"不管已有的仓"。恰恰相反，想空仓观望时
        更需要能把仓位平掉。
        """
        # **先把两道题全部解析校验完，再构造任何动作。** 短路求值会让 `exit>=门限` 直接下平仓，
        # 跳过同一个仓位的 `protect` 答案——那道题我们问了，坏掉就说明整条响应不合契约，
        # 不能因为"这条路径用不上它"就放行。
        parsed = []
        for pos in self._managed_positions(snap, acc):
            symbol = str(pos["symbol"]).strip()
            parsed.append((pos, symbol,
                           self._noul(answers, "exit:" + symbol),
                           self._score(answers, "protect:" + symbol, len(STOP_LADDER))[0]))

        actions: List[ActionRequest] = []
        notes: List[str] = []
        # 品种 → "该仓位的方向:本轮处置"，以及该仓位现有的止损，给同品种协调用（见 _plan）
        disposition: Dict[str, str] = {}
        stops: Dict[str, Optional[float]] = {}
        for pos, symbol, exit_noul, level in parsed:
            view = snap.symbols[symbol]
            side = str(pos.get("side") or "")
            existing_stop = self._as_float(pos.get("stop_loss"))
            stops[symbol] = existing_stop
            if exit_noul >= EXIT_THRESHOLD:
                disposition[symbol] = side + ":close"
                actions.append(ActionRequest(symbol=symbol, action="close", confidence=exit_noul,
                                             reason="Jev SystemOne: exit noul=%.2f" % exit_noul))
                notes.append("%s 平仓（noul=%.2f）" % (symbol, exit_noul))
                continue
            new_sl = self._tightened_stop(pos, view, STOP_LADDER[level])
            disposition[symbol] = side + (":hold" if new_sl is None else ":adjust")
            if new_sl is None:
                notes.append("%s 持有，止损不动（档位 %d，noul=%.2f）" % (symbol, level, exit_noul))
                continue
            actions.append(ActionRequest(symbol=symbol, action="adjust_sl_tp", stop_loss=new_sl,
                                         confidence=1.0 - exit_noul,
                                         reason="Jev SystemOne: 止损收紧到 %.2f 倍 ATR" % STOP_LADDER[level]))
            notes.append("%s 止损收紧到 %.4f（%.1f×ATR）" % (symbol, new_sl, STOP_LADDER[level]))
        return actions, notes, disposition, stops

    def _plan(self, answers: dict, snap: MarketSnapshot,
              acc: AccountView) -> Tuple[str, List[ActionRequest]]:
        """Turn typed answers into position management plus at most one new entry.

        **契约校验全部在动作之前**：下面每一条"观望"出口都会照常发出持仓处置动作，
        所以只要有一项答案不合契约，就必须在 `_manage()` 跑之前整条作废——否则一个
        `entry:<品种>.choice="sideways"` 或缺一道开仓题，照样能把已有仓位平掉。
        """
        # 每个品种的开仓题都要合契约——哪怕它答的是 none、哪怕最后没选它。
        entries = [(sym,) + self._entry(answers, sym) for sym in snap.symbols]
        level, _size_conf = self._score(answers, "size", len(SIZE_LADDER))
        noul = self._noul(answers, "stand_aside")

        held, held_notes, disposition, held_stops = self._manage(answers, snap, acc)
        held_text = ("｜持仓处置：" + "；".join(held_notes)) if held_notes else ""
        gate = self.tcfg.jev_min_confidence

        # 候选 = 答了 long/short 的品种，按所选项的概率从高到低；同概率保持快照顺序。
        candidates = [e for e in entries if e[1] != "none"]
        candidates.sort(key=lambda e: -1.0 if e[2] is None else e[2], reverse=True)
        tail = "size_level=%d ｜ stand_aside noul=%.2f" % (level, noul)
        if not candidates:
            return "Jev 决策：观望，本轮不新开仓（%d 个品种都判为不新开仓）。%s%s" % (
                len(entries), tail, held_text), held

        resolved, direction, p, conf, probs = candidates[0]
        action = _DIRECTIONS[direction]
        # Distribution is kept verbatim in the audit text so a reviewer can see the
        # judgement the seat acted on, not just the winner.
        numeric_probs = [(k, float(v)) for k, v in sorted(probs.items())
                         if isinstance(v, (int, float)) and not isinstance(v, bool)]
        probs_text = (" p=" + "/".join("%s:%.2f" % (k, v) for k, v in numeric_probs)) if numeric_probs else ""
        others = "、".join("%s:%s(%s)" % (s, d, "n/a" if pp is None else "%.2f" % pp)
                          for s, d, pp, _c, _p in candidates[1:4])
        evidence = "entry=%s:%s%s conf=%s ｜ %s%s" % (
            resolved, direction, probs_text,
            "n/a" if conf is None else "%.2f" % conf,
            ("其他候选=" + others + " ｜ ") if others else "",
            tail,
        )

        if noul >= STAND_ASIDE_THRESHOLD:
            return "Jev 决策：观望，本轮不新开仓（stand_aside noul=%.2f）。%s%s" % (noul, evidence, held_text), held
        if p is None or p < gate:
            return "Jev 决策：观望，本轮不新开仓（最强候选的概率低于门限 %s）。%s%s" % (
                "n/a" if p is None else "%.2f < %.2f" % (p, gate), evidence, held_text), held
        size_pct = SIZE_LADDER[level]
        if size_pct <= 0:
            return "Jev 决策：观望，本轮不新开仓（仓位档位为 0）。%s%s" % (evidence, held_text), held
        req = ActionRequest(symbol=resolved, action=action, size_pct=size_pct,
                            confidence=p, reason="Jev SystemOne: %s" % evidence)
        actions, why = self._reconcile(held, disposition, held_stops, resolved, action, req)
        if why:
            return "Jev 决策：不新开 %s（%s）。%s%s" % (resolved, why, evidence, held_text), actions
        return "Jev 决策：%s %s size_pct=%.2f（概率门控 %.2f）。%s%s" % (
            action, resolved, size_pct, gate, evidence, held_text), actions

    @staticmethod
    def _reconcile(held: List[ActionRequest], disposition: Dict[str, str],
                   stops: Dict[str, Optional[float]], symbol: str,
                   action: str, req: ActionRequest) -> Tuple[List[ActionRequest], str]:
        """同一个品种上的持仓处置与新开仓必须先对账，不能直接拼成一串动作。

        直接拼会出三种坏结果：平掉再同向开回来（白付两次手续费和滑点，敞口没变）；
        同向加仓排在收紧止损之后，`SimExchange.open()` 自动生成的止损会把刚收紧的那个覆盖掉；
        以及"继续持有"的同时反向开仓——撮合层和风控层都会拒（需先平仓再反向开仓），
        只留下一条拒单审计。**平仓 + 反向开仓是反手，必须原样保留。**
        """
        state = disposition.get(symbol)
        if state is None:
            return held + [req], ""            # 该品种没有持仓，照常开
        side, how = state.split(":", 1)
        same_side = action == _DIRECTIONS.get(side)
        if how == "close":
            if same_side:
                return held, "本轮已决定平掉该仓位，再同向开回来只是白付手续费与滑点"
            return held + [req], ""            # 反手：先平后反向开
        if not same_side:
            return held, "仍持有%s仓且本轮不平，反向开仓会被风控拒（需先平仓）" % _SIDE_TEXT.get(side, side)
        # 同向加仓必须带上该仓位现有的止损：`RiskManager.check()` 只在 stop_loss 为空时才
        # 自动生成 `价 ± 2×ATR`，而 `SimExchange.open()` 会用它覆盖原有止损。档位选了"不调整"
        # 却被加仓顺手放宽保护，正是这个席位最不该出现的事。
        if req.stop_loss is None and stops.get(symbol) is not None:
            req = replace(req, stop_loss=stops[symbol])
        if how == "adjust":
            # 加仓要排在**同品种那条** adjust 之前（否则 open() 自动生成的止损会覆盖刚收紧的
            # 那个），而**其它品种的所有处置**都要排在加仓之前：引擎顺序执行、每一步按当前总
            # 保证金判风险度，别的品种的平仓本来就是为这次加仓腾额度的。所以不是把加仓插到
            # 那条 adjust 的位置（持仓顺序是 [adjust RB0, close I0] 时，I0 的平仓照样被挤到后面），
            # 而是把那条 adjust 单独挪到加仓之后，其余原样留在前面。
            matching = [a for a in held if a.symbol == symbol and a.action == "adjust_sl_tp"]
            others = [a for a in held if not (a.symbol == symbol and a.action == "adjust_sl_tp")]
            return others + [req] + matching, ""
        return held + [req], ""

    # ── main entry ───────────────────────────────────────────────────────────
    # ── research debate ──────────────────────────────────────────────────────
    def _research_pick(self, answers: dict, key: str, options) -> Tuple[str, float]:
        """一道研究选择题：所选项必须在给出的选项里，概率取所选项的（缺了才退到 confidence）。"""
        choice, conf, probs = self._choice(answers, key)
        if choice not in options:
            raise ValueError(f"answers.{key}.choice={choice!r} 不在给出的选项里")
        if choice in probs:
            p = self._as_float(probs.get(choice))
            if p is None or not 0.0 <= p <= 1.0:
                raise ValueError(f"answers.{key}.probabilities[{choice!r}] 不是 0..1 的概率")
            return choice, p
        if conf is None:
            raise ValueError(f"answers.{key} 既没有所选项的概率也没有 confidence")
        return choice, conf

    @staticmethod
    def _research_distribution(answers: dict) -> str:
        probs = (answers.get("stance") or {}).get("probabilities")
        if not isinstance(probs, dict):
            return ""
        parts = []
        for key, label in RESEARCH_STANCES.items():
            value = JevTrader._as_float(probs.get(key))
            if value is not None:
                parts.append("%s %.2f" % (label, value))
        return " / ".join(parts)

    def research_turn(self, stage: str, prompt: str, valid_fact_ids: set, participant_ids: set,
                      *, timeout_sec: Optional[float] = None) -> JevResearchResult:
        """研究辩论的一次发言（opening / rebuttal），一次请求、不重试——与文本席位的 research() 同一口径：
        阶段截止时间是付费边界，重试与持久化由调用方负责。

        `prompt` 原样作为 state：四个参与者判断的是同一份事实集。返回的 JSON 与文本席位同一份契约，
        由调用方用 parse_opening / parse_rebuttal 校验（fact id 必须全部来自冻结的事实集）。
        """
        if self._stop_event is not None and self._stop_event.is_set():
            raise RuntimeError("Engine is stopping")
        facts = sorted(str(fact_id) for fact_id in valid_fact_ids)
        if not facts:
            raise ValueError("研究事实集为空，无法构造问题")
        stance_q = {"type": "choice", "instructions": "只依据事实集，你对这个研究对象的立场是什么？",
                    "criteria": dict(RESEARCH_STANCES)}
        if stage == "opening":
            questions = {
                "stance": stance_q,
                "support": {"type": "choice", "instructions": "事实集里哪一条最支持你选择的立场？",
                            "criteria": {fid: "事实 " + fid for fid in facts}},
                "counter": {"type": "choice", "instructions": "事实集里哪一条是反对你这个立场的最强依据？",
                            "criteria": {fid: "事实 " + fid for fid in facts}},
            }
        elif stage == "rebuttal":
            targets = sorted(str(t) for t in participant_ids if str(t) != self.id)
            if not targets:
                raise ValueError("反证需要至少一位其他参与者")
            questions = {
                "stance": stance_q,
                "target": {"type": "choice", "instructions": "第一轮观点里，你最要反证的是哪一位参与者的论点？",
                           "criteria": {t: "参与者 " + t for t in targets}},
                "challenge": {"type": "choice", "instructions": "事实集里哪一条与你所反证的论点最冲突？",
                              "criteria": {fid: "事实 " + fid for fid in facts}},
            }
        else:
            raise ValueError(f"未知的研究阶段 {stage!r}")

        answers, tokens_in, tokens_out, _raw = self._call(prompt, questions, timeout_sec=timeout_sec)
        stance, p_stance = self._research_pick(answers, "stance", RESEARCH_STANCES)
        label = RESEARCH_STANCES[stance]
        dist = self._research_distribution(answers)
        dist_text = ("（立场分布：%s）" % dist) if dist else ""
        if stage == "opening":
            support, p_support = self._research_pick(answers, "support", facts)
            counter, p_counter = self._research_pick(answers, "counter", facts)
            payload = {
                "stance": stance, "confidence": round(p_stance, 4),
                "thesis": "%s：立场%s，p=%.2f%s；最主要的依据是 %s（p=%.2f）。"
                          % (TYPED_RESEARCH_NOTE, label, p_stance, dist_text, support, p_support),
                "supporting_fact_ids": [support],
                "counter_fact_ids": [counter],
                "counter_argument": "%s：反对这一立场的最强依据是 %s（p=%.2f）。"
                                    % (TYPED_RESEARCH_NOTE, counter, p_counter),
                "invalidation": "%s：若 %s 所述事实反转，或 %s 的分量超过它，这一立场失效。"
                                % (TYPED_RESEARCH_NOTE, support, counter),
                "uncertainties": [dist] if dist else [],
            }
        else:
            target, p_target = self._research_pick(answers, "target", targets)
            challenge, p_challenge = self._research_pick(answers, "challenge", facts)
            payload = {
                "stance": stance, "confidence": round(p_stance, 4),
                "target_trader_id": target,
                "challenge": "%s：反证 %s 的论点（p=%.2f），依据 %s（p=%.2f）。"
                             % (TYPED_RESEARCH_NOTE, target, p_target, challenge, p_challenge),
                "challenge_fact_ids": [challenge],
                "revised_thesis": "%s：修订后立场%s，p=%.2f%s。" % (TYPED_RESEARCH_NOTE, label, p_stance, dist_text),
                "invalidation": "%s：若 %s 所述事实反转，这一反证失效。" % (TYPED_RESEARCH_NOTE, challenge),
            }
        payload["generated_by"] = "typed_adapter"
        return JevResearchResult(text=json.dumps(payload, ensure_ascii=False),
                                 tokens_in=tokens_in, tokens_out=tokens_out)

    def decide(self, snap: MarketSnapshot, acc: AccountView, rules_text: str) -> Decision:
        state = build_prompt(snap, acc, rules_text)
        d = Decision(trader_id=self.id, round_no=snap.round_no, ts=time.time(), prompt=state, mode="jev")
        t0 = time.time()
        last_err = ""
        try:
            questions = self._questions(snap, acc)
        except Exception as e:  # noqa: BLE001
            d.error = f"{type(e).__name__}: {str(e)[:300]}"
            d.latency_ms = int((time.time() - t0) * 1000)
            return d

        max_retries = self._effective_max_retries()
        for attempt in range(max_retries + 1):
            if self._stop_event is not None and self._stop_event.is_set():
                last_err = "引擎停止，放弃调用"
                break
            try:
                answers, tokens_in, tokens_out, raw = self._call(state, questions)
            except JevContractError as e:
                # 与下面 _plan() 的契约违约同一口径：只发这一次，不重试。
                last_err = f"Jev 响应不可解析: {str(e)[:250]}"
                log.warning("%s Jev 响应契约违约，不重试: %s", self.id, last_err)
                break
            except Exception as e:  # noqa: BLE001
                # 异常元数据只在 classify_error 里判一次（与文本席位共用）：Key 无效、
                # 余额不足、端点/模型不存在这类错误重试也不会好，重试只是把本轮拖慢；
                # 看板上也要看到能照做的中文提示，而不是一行 httpx 原文。
                hint, retryable = classify_error(e, self.tcfg, self.lcfg)
                detail = f"{type(e).__name__}: {str(e)[:300]}"
                last_err = f"{hint}｜{detail}" if hint else detail
                log.warning("%s 第 %d 次 Jev 调用失败: %s", self.id, attempt + 1, last_err)
                if not retryable:
                    break
                if attempt < max_retries:
                    time.sleep(min(2 ** attempt, 8))
                continue

            d.raw_response = raw
            d.tokens_in, d.tokens_out = tokens_in, tokens_out
            try:
                analysis, actions = self._plan(answers, snap, acc)
            except Exception as e:  # noqa: BLE001
                # A well-formed HTTP 200 with an unusable body is a contract
                # violation, not a transport hiccup: retrying burns quota, so stop.
                last_err = f"Jev 响应不可解析: {type(e).__name__}: {str(e)[:250]}"
                break
            d.analysis, d.actions = analysis, actions
            last_err = ""
            break

        if last_err:
            d.error = last_err
            d.actions = []
        d.latency_ms = int((time.time() - t0) * 1000)
        return d
