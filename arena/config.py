"""配置加载与校验。"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .exchange.contracts import load_cn_symbols


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CompetitionCfg(BaseModel):
    name: str = "四模型期货竞赛"
    market: Literal["cn_futures", "crypto", "replay", "ctp"] = "cn_futures"
    initial_cash: float = Field(80_000, gt=0, allow_inf_nan=False)
    decision_interval_sec: int = 300
    tick_interval_sec: int = 20
    snapshot_interval_sec: int = 60
    timezone: str = "Asia/Shanghai"

    @field_validator("initial_cash")
    @classmethod
    def _finite_initial_cash(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("competition.initial_cash must be finite")
        return value


class CnFuturesCfg(BaseModel):
    contracts_file: str = "config/contracts_cn.json"
    timeout_sec: float = 10


class CryptoCfg(BaseModel):
    provider: Literal["okx", "binance"] = "okx"
    timeout_sec: float = 10


class ReplayCfg(BaseModel):
    seed: int = 42
    bar_seconds: int = Field(60, ge=1)
    start_price: Dict[str, float] = Field(default_factory=dict)
    csv_dir: Optional[str] = None
    # 启动时预载的历史 bar 数（CSV 前 history 行作为历史，其后逐 tick 回放）。必须 ≥ 1：合成行情从最后一根历史 bar 续接，
    # 没有历史就没有起点，每个 tick 都会失败、回放永远没有行情
    history: int = Field(300, ge=1)

    @field_validator("start_price")
    @classmethod
    def _finite_positive_prices(cls, v: Dict[str, float]) -> Dict[str, float]:
        # NaN/Infinity 会让第一根合成 bar 取整时崩溃、程序起不来；0 或负数会合成非法的 OHLC 进入指标计算
        for sym, px in v.items():
            if not math.isfinite(px) or px <= 0:
                raise ValueError(f"replay.start_price[{sym}] 必须是有限的正数（当前 {px}）")
        return v


class CtpCfg(BaseModel):
    """CTP 行情接入（期货公司开通程序化后单独下发的一套凭证）。

    密码只认环境变量名：写进 `password` 的明文会随 config.yaml 一起躺在磁盘上，
    而这个文件是要被打包、被备份、被贴进工单的。
    """
    md_front: str = ""                     # 行情前置，形如 tcp://<前置IP>:<端口>
    broker_id: str = ""
    user_id: str = ""                      # 投资者编码；也可用 user_id_env 从环境变量读
    user_id_env: str = ""
    password_env: str = "CTP_PASSWORD"     # 只存环境变量名，绝不写明文
    password: str = ""                     # 仅供本地 config.yaml 临时使用，不要提交
    instruments: Dict[str, str] = Field(default_factory=dict)   # 品种 → 指定合约，留空则自动选主力
    auto_months: int = Field(12, ge=1, le=36)
    flow_dir: str = "data/ctp_flow"
    connect_timeout_sec: float = Field(20.0, gt=0)
    bar_seconds: int = Field(60, ge=1)        # 必须能整除 300 秒（快照每次都先要 5m）且不短于 10 秒，启动时校验
    discovery_grace_sec: float = Field(3.0, ge=0)   # 选主力的观察期：等各候选合约都报过价再开始发布 bar
    # 合约安静这么久就退出主力候选（按观察到的交易时长算，不是墙上时间）：
    # 到期/停牌的合约最后那个持仓量会永远留着，不剔除的话后继合约的行情会一直被当成非主力丢掉
    dominant_stale_sec: float = Field(300.0, ge=1)
    # 内存里每个品种保留多少根基础 bar。配小了不会报错，只是 1h 的慢指标（ema60）永远算不出来，
    # 所以行情源会按 bar_seconds 反比把它抬到够用为止；3600 根正是 bar_seconds=60 的下限。
    max_bars: int = Field(3600, ge=10)
    persist_bars: bool = True              # 1m bar 落库，重启后预热而不是从零积累

    def resolved_password(self) -> str:
        """环境变量优先；`password_env` 里若直接写了密码（历史误配）也认，但不推荐。"""
        if self.password_env:
            value = os.environ.get(self.password_env, "")
            if value:
                return value
            if len(self.password_env) > 24 and not self.password_env.isidentifier():
                return self.password_env    # 误把密码填进了 env 名字段
        return self.password

    def resolved_user_id(self) -> str:
        if self.user_id_env:
            value = os.environ.get(self.user_id_env, "")
            if value:
                return value
        return self.user_id


class MarketDataCfg(BaseModel):
    cn_futures: CnFuturesCfg = CnFuturesCfg()
    crypto: CryptoCfg = CryptoCfg()
    replay: ReplayCfg = ReplayCfg()
    ctp: CtpCfg = CtpCfg()


class RiskCfg(BaseModel):
    max_positions: int = 4
    max_leverage: float = 5
    max_notional_pct: float = 0.30
    max_size_pct: float = 0.30
    require_stop_loss: bool = True
    auto_stop_atr_mult: float = 2.0
    slippage_ticks: int = 1
    min_hold_sec: int = 120
    # Optional pre-trade cap.  It is intentionally separate from forced liquidation:
    # a strategy may not raise its projected margin risk above this ceiling, while
    # liquidation remains a last-resort protection for adverse price moves.
    # None preserves legacy competitions whose already-persisted liquidation
    # guard is intentionally below 95%.  Product/runtime configs opt in with
    # 0.95 explicitly, so a newly configured arena gets the hard admission cap
    # without rewriting historical competition semantics.
    max_margin_risk_ratio: Optional[float] = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    # Optional operating band for risk-increasing orders.  It is a target, not
    # a mandate to create a trade when the market evidence is insufficient.
    # Both values are required together so an operator cannot accidentally set
    # a one-sided objective with unclear semantics.
    target_margin_risk_min_ratio: Optional[float] = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    target_margin_risk_max_ratio: Optional[float] = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    warning_margin_risk_ratio: float = Field(0.90, gt=0, le=1, allow_inf_nan=False)
    liquidation_risk_ratio: float = Field(1.0, gt=0, le=1, allow_inf_nan=False)
    maintenance_margin_ratio: float = 0.5  # crypto 逐仓：亏损达保证金*(1-此值)强平

    @model_validator(mode="after")
    def _check_margin_risk_ratio(self):
        if self.max_margin_risk_ratio is not None and self.max_margin_risk_ratio > self.liquidation_risk_ratio:
            raise ValueError("max_margin_risk_ratio must not exceed liquidation_risk_ratio")
        if self.max_margin_risk_ratio is not None and self.warning_margin_risk_ratio > self.max_margin_risk_ratio:
            raise ValueError("warning_margin_risk_ratio must not exceed max_margin_risk_ratio")
        target_min = self.target_margin_risk_min_ratio
        target_max = self.target_margin_risk_max_ratio
        if (target_min is None) != (target_max is None):
            raise ValueError("target_margin_risk_min_ratio and target_margin_risk_max_ratio must be configured together")
        if target_min is not None and target_max is not None:
            if target_min > target_max:
                raise ValueError("target_margin_risk_min_ratio must not exceed target_margin_risk_max_ratio")
            if self.max_margin_risk_ratio is None:
                raise ValueError("target margin risk band requires max_margin_risk_ratio as a hard cap")
            if target_max > self.max_margin_risk_ratio:
                raise ValueError("target_margin_risk_max_ratio must not exceed max_margin_risk_ratio")
            if self.warning_margin_risk_ratio < target_max:
                raise ValueError("warning_margin_risk_ratio must not be below target_margin_risk_max_ratio")
        return self

    @property
    def opening_margin_risk_cap(self) -> Optional[float]:
        """Return the normal-operation ceiling for risk-increasing orders."""
        return self.target_margin_risk_max_ratio if self.target_margin_risk_max_ratio is not None else self.max_margin_risk_ratio


class LlmCfg(BaseModel):
    timeout_sec: float = Field(60, gt=0)
    max_retries: int = Field(2, ge=0)
    temperature: float = 0.3
    max_tokens: int = Field(1500, ge=1)
    json_mode: bool = True
    breaker_failures: int = Field(3, ge=1)
    breaker_skip_rounds: int = Field(3, ge=0)


class PreopenDebateCfg(BaseModel):
    """Account-free research debate scheduled before supported CN futures opens."""
    enabled: bool = False
    lead_window_sec: int = Field(1800, ge=60, le=7200)
    max_quote_age_sec: int = Field(900, ge=30, le=3600)
    # This applies only to the bounded pre-open research stages.  It gives the
    # slower Qwen rebuttal a 57s request budget (three seconds stay reserved for
    # persistence) without changing normal execution-decision timeouts.
    stage_timeout_sec: int = Field(60, ge=5, le=300)
    max_output_tokens: int = Field(700, ge=128, le=4096)


class ProductResearchCfg(BaseModel):
    """Research-only full-universe debate scheduling.

    These limits are intentionally separate.  ``candidate_batch_size`` is only
    a progress/scheduling window; it never changes the universe denominator.
    ``model_concurrency`` bounds billable requests, not the number of products
    that a run is allowed to reach.
    """
    enabled: bool = True
    candidate_batch_size: int = Field(8, ge=1, le=64)
    model_concurrency: int = Field(4, ge=1, le=16)
    max_attempts: int = Field(2, ge=1, le=5)
    manual_retry_limit: int = Field(1, ge=0, le=3)
    retry_backoff_sec: float = Field(1.0, ge=0, le=60)
    stage_timeout_sec: int = Field(75, ge=10, le=300)
    discovery_timeout_sec: float = Field(10.0, ge=1, le=60)
    discovery_workers: int = Field(4, ge=1, le=8)
    max_output_tokens: int = Field(700, ge=128, le=4096)


class TraderCfg(BaseModel):
    id: str
    name: str
    color: str = "#3b82f6"
    provider: Literal["openai_compatible", "rule", "mock", "jev"] = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    api_key: str = ""
    api_style: Literal["chat", "responses"] = "chat"
    max_tokens: Optional[int] = Field(default=None, ge=1)
    max_completion_tokens: Optional[int] = Field(default=None, ge=1)
    research_max_completion_tokens: Optional[int] = Field(default=None, ge=128, le=4096)
    max_retries: Optional[int] = Field(default=None, ge=0)
    reasoning_effort: Optional[Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    fallback_rule: Literal["trend", "meanrev", "breakout", "conservative"] = "trend"
    mock_response: str = ""  # provider=mock 时返回的固定文本（测试用）
    # Jev seats stand aside (open nothing) when the direction or symbol answer is
    # less certain than this gate. Only read by provider=jev.
    jev_min_confidence: float = Field(0.55, ge=0, le=1)

    @model_validator(mode="after")
    def _check_output_token_overrides(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("max_tokens and max_completion_tokens cannot both be set")
        if self.max_completion_tokens is not None and self.api_style != "chat":
            raise ValueError("max_completion_tokens is only supported with api_style=chat")
        if self.research_max_completion_tokens is not None and self.api_style != "chat":
            raise ValueError("research_max_completion_tokens is only supported with api_style=chat")
        if self.temperature is not None and self.api_style != "chat":
            raise ValueError("temperature override is only supported with api_style=chat")
        return self

    def resolved_api_key(self) -> str:
        literal = (self.api_key or "").strip()
        if literal:
            return literal
        env = (self.api_key_env or "").strip()
        if not env:
            return ""
        val = os.environ.get(env, "").strip()
        if val:
            return val
        # 常见误填：把 Key 本身写进了 api_key_env。环境变量名只含大写字母/数字/下划线；
        # 不像变量名的值按 Key 使用并告警，避免四个模型静默降级为演示策略。
        if not _ENV_NAME.match(env):
            logging.getLogger("arena.config").warning(
                "交易员 %s 的 api_key_env 看起来是 Key 本身而不是环境变量名，已按 Key 使用；请改写到 api_key 字段，且不要提交到仓库", self.id)
            return env
        return ""

    @property
    def is_live_llm(self) -> bool:
        return self.provider == "openai_compatible" and bool(self.resolved_api_key())

    @property
    def is_jev(self) -> bool:
        """A Jev seat is live once an endpoint is configured.

        The hosted service also needs an API key, but a local service speaking the
        same schema (e.g. LitJev) does not, so the key is deliberately not part of
        the readiness test: a missing key surfaces as an HTTP 401 on the first round
        instead of silently downgrading the seat to a rule.
        """
        return self.provider == "jev" and bool((self.base_url or "").strip())

    @property
    def is_live_provider(self) -> bool:
        """True for seats whose decisions come from a model rather than a rule."""
        return self.is_live_llm or self.is_jev


class StorageCfg(BaseModel):
    db_path: str = "data/arena.db"
    log_dir: str = "logs"


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 0
    prevent_sleep: bool = True
    allow_remote_control: bool = False
    # 绑定到 0.0.0.0 时，写接口只认这里列出的主机名（外加回环地址）：
    # 不列就没法把真正的看板地址和被 DNS 重绑定到本机的攻击者域名区分开
    allowed_hosts: List[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    competition: CompetitionCfg = CompetitionCfg()
    symbols: List[str] = Field(default_factory=lambda: ["RB0", "I0", "AU0", "CU0"])
    market_data: MarketDataCfg = MarketDataCfg()
    risk: RiskCfg = RiskCfg()
    llm: LlmCfg = LlmCfg()
    preopen_debate: PreopenDebateCfg = PreopenDebateCfg()
    product_research: ProductResearchCfg = ProductResearchCfg()
    traders: List[TraderCfg] = Field(default_factory=list)
    storage: StorageCfg = StorageCfg()
    server: ServerCfg = ServerCfg()
    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check(self):
        ids = [t.id for t in self.traders]
        if len(ids) != len(set(ids)):
            raise ValueError("traders.id 必须唯一")
        if not self.symbols:
            raise ValueError("symbols 不能为空")
        return self

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path)

    @property
    def db_path(self) -> Path:
        return self.resolve(self.storage.db_path)

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.storage.log_dir)


# `symbols` 写成这些词之一时，展开成合约表里的全部活跃品种。
SYMBOLS_ALL = {"all", "全部"}


def expand_symbols(raw: Dict[str, Any], base: Path) -> Dict[str, Any]:
    """把 `symbols: all` 展开成合约表里的活跃品种。

    为什么需要这个开关：`load_config()` 优先读 `config/config.yaml`，而那个文件在 .gitignore
    里——老装机上它还是当初那八个代码。只把模板扩到全品种，正在跑的那份一个字都不会变，
    看板上仍然是八个合约，这次改动等于没发生；要么让用户手抄七十一行，要么给一行开关。

    只对有合约表的市场成立（`cn_futures` / `ctp`）。crypto 与 replay 没有这张表，
    那里写 `all` 会得到一个明确的报错，而不是一个空的品种列表。
    """
    value = raw.get("symbols")
    if not isinstance(value, str):
        return raw
    if value.strip().lower() not in SYMBOLS_ALL:
        raise ValueError(f"symbols 只能是代码列表，或者 all（合约表里的全部活跃品种）；收到 {value!r}")
    market = str((raw.get("competition") or {}).get("market", "cn_futures"))
    if market not in ("cn_futures", "ctp"):
        raise ValueError(f"symbols: all 需要合约表，market={market} 没有；请显式列出品种代码")
    table = ((raw.get("market_data") or {}).get("cn_futures") or {}).get(
        "contracts_file", "config/contracts_cn.json")
    path = Path(table)
    resolved = path if path.is_absolute() else (base / path)
    try:
        symbols = load_cn_symbols(resolved)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"symbols: all 读不了合约表 {resolved}：{exc}") from exc
    if not symbols:
        raise ValueError(f"symbols: all 展开为空：{resolved} 里没有 active 的品种")
    out = dict(raw)
    out["symbols"] = symbols
    return out


def load_config(path: str | Path | None, base_dir: str | Path | None = None) -> AppConfig:
    """读取 YAML；path 为空时依次尝试 config/config.yaml、config/config.example.yaml。"""
    base = Path(base_dir) if base_dir else Path.cwd()
    if path is None:
        for cand in ("config/config.yaml", "config/config.example.yaml"):
            if (base / cand).exists():
                path = base / cand
                break
    if path is None:
        cfg = AppConfig()
    else:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg = AppConfig(**expand_symbols(raw, base))
    cfg.base_dir = base
    return cfg
