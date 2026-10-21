"""Jev (System One) seat: request schema, answer mapping, gating and failure paths.

The stub client stands in for ``httpx.Client`` so these tests never touch the
network: a Jev endpoint is not part of the test environment, and the seat's own
contract is what matters here.
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena.config import AppConfig, LlmCfg, TraderCfg
from arena.db import Database
from arena.engine.loop import Engine, _is_live_mode
from arena.models import ContractSpec, Quote
from arena.traders.base import AccountView, MarketSnapshot, SymbolView
from arena.traders.jev import SIZE_LADDER, JevTrader
from arena.traders.prompt import build_prompt
from arena.traders.rule import RuleTrader

ROOT = Path(__file__).resolve().parents[1]
NAMES = {"RB0": "螺纹钢", "I0": "铁矿石"}


# ── fixtures ────────────────────────────────────────────────────────────────
def _view(symbol: str, price: float = 3400.0) -> SymbolView:
    spec = ContractSpec(symbol=symbol, name=NAMES.get(symbol, symbol), exchange="SHFE",
                        multiplier=10, tick=1.0, margin_ratio=0.18)
    quote = Quote(symbol=symbol, price=price, ts=1.0, change_pct=0.5)
    return SymbolView(symbol=symbol, spec=spec, quote=quote, ind_5m={}, ind_15m={}, ind_1h={}, bars_5m=[])


def _snapshot(symbols=("RB0", "I0")) -> MarketSnapshot:
    return MarketSnapshot(ts=1.0, round_no=1, market="replay", currency="CNY",
                          symbols={s: _view(s) for s in symbols})


def _account() -> AccountView:
    return AccountView(
        trader_id="jev", equity=1_000_000, cash=1_000_000, available=1_000_000, margin_used=0,
        realized_pnl=0, fees=0, initial_cash=1_000_000, positions=[], recent_trades=[],
        last_decision_summary="", round_no=1,
    )


def _entry(direction="long", p=0.72, conf=0.60):
    """一个品种的开仓答案：所选项概率 p，其余两项平分剩下的。"""
    rest = round((1 - p) / 2, 4)
    probs = {k: (p if k == direction else rest) for k in ("long", "short", "none")}
    return {"type": "choice", "choice": direction, "confidence": conf, "probabilities": probs}


def _answers(symbol="RB0", symbol_conf=0.60, direction="long", dir_conf=0.72, size_level=2, noul=0.10,
             symbols=("RB0", "I0")):
    """`symbol` 答 direction（所选项概率 dir_conf、分布集中度 symbol_conf），其余品种答 none。"""
    answers = {"entry:" + s: (_entry(direction, dir_conf, symbol_conf) if s == symbol
                              else _entry("none", 0.90, 0.85)) for s in symbols}
    answers["size"] = {"type": "score", "score": size_level,
                       "probabilities": {str(i): (1.0 if i == size_level else 0.0) for i in range(4)}}
    answers["stand_aside"] = {"type": "noul", "noul": noul}
    return answers


def _payload(answers=None, input_tokens=96, output_tokens=0):
    return {"model": "jev-latest", "answers": answers if answers is not None else _answers(),
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


class _StubResponse:
    def __init__(self, item):
        self._item = item

    def raise_for_status(self):
        if isinstance(self._item, Exception):
            raise self._item

    def json(self):
        return self._item


class _StubClient:
    """Replays canned payloads (or raises canned exceptions) and records requests."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def post(self, path, json=None):
        self.calls.append(SimpleNamespace(path=path, body=json))
        if not self.items:
            raise AssertionError("unexpected extra Jev request")
        return _StubResponse(self.items.pop(0))


def _trader(client=None, max_retries=0, max_retries_override=None, **cfg_kwargs) -> JevTrader:
    kwargs = {"id": "jev", "name": "Jev", "provider": "jev",
              "base_url": "https://api.typesafe.ai", "model": "jev-latest"}
    if max_retries_override is not None:
        kwargs["max_retries"] = max_retries_override      # 交易员级覆盖
    kwargs.update(cfg_kwargs)
    trader = JevTrader(TraderCfg(**kwargs), LlmCfg(max_retries=max_retries))
    if client is not None:
        trader._client = client
    return trader


def _decide(trader, snap=None, rules="规则"):
    return trader.decide(snap or _snapshot(), _account(), rules)


# ── request schema ──────────────────────────────────────────────────────────
def test_request_uses_systemone_schema_with_typed_questions():
    client = _StubClient(_payload())
    _decide(_trader(client))

    call = client.calls[0]
    assert call.path == "/v1/systemone"
    assert call.body["model"] == "jev-latest"
    assert call.body["state"].strip(), "state 不能为空：Jev 没有其它输入"
    questions = call.body["questions"]
    # 逐品种一道开仓题，没有"N 选 1 挑品种"也没有不知道指谁的全局"方向"题。
    assert set(questions) == {"entry:RB0", "entry:I0", "size", "stand_aside"}
    for sym, name in (("RB0", "螺纹钢"), ("I0", "铁矿石")):
        q = questions["entry:" + sym]
        assert q["type"] == "choice"
        assert q["criteria"] == {"long": "新开多仓", "short": "新开空仓", "none": "不新开仓"}
        assert sym in q["instructions"] and name in q["instructions"]
    assert questions["size"]["type"] == "score"
    assert questions["size"]["criteria"] == ["不开仓", "轻仓", "半仓", "重仓"]
    assert questions["stand_aside"]["type"] == "noul"
    # Every question carries instructions: the primitives are only meaningful with them.
    assert all(q.get("instructions") for q in questions.values())


def test_state_is_byte_identical_to_the_llm_seat_prompt():
    """Fairness invariant: every seat judges the same snapshot bytes."""
    client = _StubClient(_payload())
    snap = _snapshot()
    decision = _decide(_trader(client), snap=snap)
    expected = build_prompt(snap, _account(), "规则")
    assert client.calls[0].body["state"] == expected
    assert decision.prompt == expected


def test_auth_header_is_sent_only_when_a_key_is_configured(monkeypatch):
    # This shell exports NO_PROXY with a bare "[::1]" entry, which httpx parses as a
    # host:port and rejects (InvalidURL: Invalid port ':1]'). Neutralise the proxy
    # environment so the test exercises the seat's own header logic.
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                 "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    with_key = _trader(api_key="test-key")._get_client()
    try:
        assert with_key.headers["Authorization"] == "Bearer test-key"
    finally:
        with_key.close()
    without_key = _trader()._get_client()
    try:
        assert "authorization" not in {k.lower() for k in without_key.headers}
    finally:
        without_key.close()


# ── answer mapping ──────────────────────────────────────────────────────────
def test_choice_score_noul_map_to_a_confidence_carrying_action():
    decision = _decide(_trader(_StubClient(_payload())))

    assert decision.error == ""
    assert decision.mode == "jev"
    assert len(decision.actions) == 1
    action = decision.actions[0]
    assert (action.symbol, action.action) == ("RB0", "open_long")
    assert action.size_pct == SIZE_LADDER[2]
    assert action.confidence == pytest.approx(0.72)
    assert "Jev" in action.reason


def test_short_direction_maps_to_open_short():
    decision = _decide(_trader(_StubClient(_payload(_answers(direction="short")))))
    assert [(a.symbol, a.action) for a in decision.actions] == [("RB0", "open_short")]


def test_probabilities_are_preserved_in_the_analysis_for_audit():
    decision = _decide(_trader(_StubClient(_payload(_answers(dir_conf=0.61, symbol_conf=0.37)))))
    assert "p=" in decision.analysis and "long:0.61" in decision.analysis
    assert "conf=0.37" in decision.analysis, "confidence 不门控，但要留在审计文本里"


def test_usage_is_reported_verbatim_and_raw_response_is_the_answers_json():
    decision = _decide(_trader(_StubClient(_payload(input_tokens=96, output_tokens=0))))

    assert decision.tokens_in == 96
    assert decision.tokens_out == 0
    assert json.loads(decision.raw_response)["answers"]["entry:RB0"]["choice"] == "long"
    assert "analysis" not in decision.raw_response


def test_output_tokens_are_reported_verbatim_when_the_service_bills_them():
    """托管服务实测会计输出 token（2026-10-08：8 品种一轮 142）；原样上报，不能写死为 0。"""
    decision = _decide(_trader(_StubClient(_payload(input_tokens=2899, output_tokens=142))))
    assert (decision.tokens_in, decision.tokens_out) == (2899, 142)


# ── gating ──────────────────────────────────────────────────────────────────
def test_stands_aside_when_noul_says_so():
    decision = _decide(_trader(_StubClient(_payload(_answers(noul=0.91)))))
    assert decision.actions == []
    assert decision.error == "", "空仓观望是正常决策，不是错误（否则会污染熔断与看板告警）"
    assert "观望" in decision.analysis and "0.91" in decision.analysis


def test_stands_aside_below_the_probability_gate():
    decision = _decide(_trader(_StubClient(_payload(_answers(dir_conf=0.30))), jev_min_confidence=0.55))
    assert decision.actions == []
    assert decision.error == ""
    assert "门限" in decision.analysis


def test_a_diffuse_but_decisive_answer_is_not_blocked_by_its_confidence():
    """confidence 只是分布集中度：3 选 1 的 p=0.63 实测只给 0.45。门限比的是所选项的概率。"""
    decision = _decide(_trader(_StubClient(_payload(_answers(dir_conf=0.63, symbol_conf=0.45))),
                               jev_min_confidence=0.55))
    assert [(a.symbol, a.action) for a in decision.actions] == [("RB0", "open_long")]
    assert decision.actions[0].confidence == pytest.approx(0.63)


def test_the_strongest_candidate_wins_across_symbols():
    answers = _answers(symbol="RB0", direction="long", dir_conf=0.60)
    answers["entry:I0"] = {"type": "choice", "choice": "short", "confidence": 0.70,
                           "probabilities": {"long": 0.05, "short": 0.80, "none": 0.15}}
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert [(a.symbol, a.action) for a in decision.actions] == [("I0", "open_short")]
    assert "其他候选=RB0:long(0.60)" in decision.analysis


def test_all_none_means_stand_aside_not_an_error():
    answers = _answers()
    answers["entry:RB0"] = {"type": "choice", "choice": "none", "confidence": 0.9,
                            "probabilities": {"long": 0.05, "short": 0.05, "none": 0.90}}
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.actions == [] and decision.error == ""
    assert "都判为不新开仓" in decision.analysis


def test_stands_aside_when_the_size_level_is_zero():
    decision = _decide(_trader(_StubClient(_payload(_answers(size_level=0)))))
    assert decision.actions == []
    assert decision.error == ""


def test_confidence_gate_is_per_seat_and_defaults_to_a_conservative_value():
    assert TraderCfg(id="j", name="J").jev_min_confidence == pytest.approx(0.55)
    # A higher gate only makes the seat more likely to stand aside.
    strict = _trader(_StubClient(_payload(_answers(dir_conf=0.60))), jev_min_confidence=0.90)
    assert _decide(strict).actions == []


def test_size_score_is_derived_from_probabilities_when_the_service_omits_it():
    answers = _answers()
    del answers["size"]["score"]
    answers["size"]["probabilities"] = {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.actions[0].size_pct == SIZE_LADDER[2]


# ── contract violations and transport failures ──────────────────────────────
def test_an_entry_choice_outside_the_contract_is_reported_as_an_error_and_opens_nothing():
    answers = _answers()
    answers["entry:I0"] = {"type": "choice", "choice": "sideways", "confidence": 0.9,
                           "probabilities": {"sideways": 0.9}}
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.actions == []
    assert "sideways" in decision.error


def test_missing_answer_block_is_reported_as_an_error():
    answers = _answers()
    del answers["entry:I0"]
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.actions == []
    assert "answers.entry:I0" in decision.error


def test_http_error_is_captured_without_raising():
    decision = _decide(_trader(_StubClient(ValueError("401 unauthorized"))))
    assert decision.actions == []
    assert "401" in decision.error


def test_transport_failure_after_retries_keeps_the_last_error(monkeypatch):
    monkeypatch.setattr("arena.traders.jev.time.sleep", lambda _: None)
    client = _StubClient(RuntimeError("connection reset"), RuntimeError("connection reset"))
    decision = _decide(_trader(client, max_retries=1))
    assert len(client.calls) == 2
    assert decision.actions == []
    assert "connection reset" in decision.error


def test_transport_failure_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("arena.traders.jev.time.sleep", lambda _: None)
    client = _StubClient(RuntimeError("flaky"), _payload())
    decision = _decide(_trader(client, max_retries=1))
    assert len(client.calls) == 2
    assert decision.error == ""
    assert len(decision.actions) == 1


def test_unparseable_body_is_not_retried(monkeypatch):
    """HTTP 200 with a broken body is a contract violation: retrying burns quota."""
    monkeypatch.setattr("arena.traders.jev.time.sleep", lambda _: None)
    client = _StubClient(_payload({"nothing": "useful"}), _payload())
    decision = _decide(_trader(client, max_retries=2))
    assert len(client.calls) == 1
    assert decision.actions == []
    assert "answers" in decision.error


def test_stop_event_aborts_before_calling_the_endpoint():
    stop = threading.Event()
    stop.set()
    client = _StubClient(_payload())
    trader = _trader(client, max_retries=2)
    trader._stop_event = stop
    decision = _decide(trader)
    assert client.calls == []
    assert "停止" in decision.error


def test_empty_symbol_snapshot_is_an_error_not_a_crash():
    client = _StubClient(_payload())
    decision = _decide(_trader(client), snap=_snapshot(symbols=()))
    assert client.calls == []
    assert decision.actions == [] and decision.error


# ── config and engine wiring ────────────────────────────────────────────────
def test_config_marks_a_jev_seat_with_an_endpoint_as_live():
    live = TraderCfg(id="jev", name="Jev", provider="jev", base_url="https://api.typesafe.ai")
    assert live.is_jev is True
    assert live.is_live_provider is True
    # A local schema-compatible server needs no key, so readiness must not depend on one.
    assert live.is_live_llm is False


def test_config_treats_a_jev_seat_without_endpoint_as_not_live():
    seat = TraderCfg(id="jev", name="Jev", provider="jev", base_url="")
    assert seat.is_jev is False
    assert seat.is_live_provider is False


def test_unknown_provider_is_still_rejected():
    with pytest.raises(Exception):
        TraderCfg(id="x", name="X", provider="definitely_not_a_provider")


def test_live_modes_cover_jev_so_the_seat_is_not_labelled_a_demo_strategy():
    assert _is_live_mode("llm") and _is_live_mode("jev")
    assert not _is_live_mode("rule") and not _is_live_mode("mock")


def test_roster_signature_covers_the_jev_seat_and_its_gate():
    def signature(**kwargs):
        kwargs.setdefault("base_url", "https://api.typesafe.ai/")
        kwargs.setdefault("model", "jev-latest")
        cfg = AppConfig(traders=[TraderCfg(id="jev", name="Jev", provider="jev", **kwargs)])
        return Engine._configured_roster_signature(cfg)

    assert signature()["jev"][0] == "jev"
    # A trailing slash is cosmetic; it must not look like a changed contestant.
    assert signature(base_url="https://api.typesafe.ai") == signature()
    # A changed model or gate must look like a change: mixing them silently would
    # corrupt the comparison the contest exists to make.
    assert signature(model="jev-next") != signature()
    assert signature(jev_min_confidence=0.9) != signature()


def _app_config(tmp_path, seat: TraderCfg) -> AppConfig:
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000,
                     "decision_interval_sec": 1, "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}},
                     "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[seat],
        storage={"db_path": str(tmp_path / "jev.db")},
        base_dir=ROOT,
    )


def test_engine_builds_a_jev_trader_when_the_endpoint_is_configured(tmp_path):
    seat = TraderCfg(id="jev", name="Jev", provider="jev",
                     base_url="https://api.typesafe.ai", model="jev-latest")
    cfg = _app_config(tmp_path, seat)
    engine = Engine(cfg, Database(cfg.db_path), accelerate=True)
    built = engine._build_trader(seat)
    assert isinstance(built, JevTrader)
    assert built.mode == "jev"


def test_engine_falls_back_to_the_rule_when_the_jev_endpoint_is_missing(tmp_path):
    seat = TraderCfg(id="jev", name="Jev", provider="jev", base_url="", fallback_rule="conservative")
    cfg = _app_config(tmp_path, seat)
    engine = Engine(cfg, Database(cfg.db_path), accelerate=True)
    assert isinstance(engine._build_trader(seat), RuleTrader)


# ── 异常元数据只判一次（与文本席位共用 classify_error） ────────────────────────

def _http_error(status: int, body: str = "") -> Exception:
    """A real httpx.HTTPStatusError: the seat talks httpx, not the openai SDK."""
    import httpx

    request = httpx.Request("POST", "https://api.typesafe.ai/v1/systemone")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError(f"{status} error", request=request, response=response)


def test_an_invalid_key_is_not_retried_and_says_why_in_chinese():
    """401 重试多少次都不会变好，只会把本轮拖慢；看板上也不能只剩一行英文。

    状态码在 httpx 异常上挂在 `response.status_code`（openai 挂在 `status_code`），
    两种都要认——否则 Jev 席位的 401 会被当成"没有状态码"一路重试。
    """
    client = _StubClient(_http_error(401, "Invalid API key"))
    trader = _trader(client, max_retries=2)
    decision = _decide(trader)
    assert len(client.calls) == 1, f"401 不该重试，实际请求了 {len(client.calls)} 次"
    assert "API Key 无效或未授权（401）" in decision.error
    assert "HTTPStatusError" in decision.error, "中文提示之后要保留接口原文"
    assert decision.actions == []


def test_a_missing_endpoint_is_not_retried():
    """404：base_url 或 model 写错了，重试同样没有意义。"""
    client = _StubClient(_http_error(404, "Not Found"))
    trader = _trader(client, max_retries=2)
    decision = _decide(trader)
    assert len(client.calls) == 1
    assert "接口或模型不存在（404）" in decision.error


def test_a_server_error_is_still_retried():
    """5xx 与超时是暂时的，仍然要重试——不能把"不重试"做成一刀切。"""
    client = _StubClient(_http_error(503), _http_error(503), _payload())
    trader = _trader(client, max_retries=2)
    decision = _decide(trader)
    assert len(client.calls) == 3, "503 应当重试到成功"
    assert decision.error == ""
    assert [a.symbol for a in decision.actions] == ["RB0"]


def test_a_timeout_is_retried_and_names_the_setting():
    import httpx

    client = _StubClient(httpx.ReadTimeout("timed out"), _payload())
    trader = _trader(client, max_retries=1)
    decision = _decide(trader)
    assert len(client.calls) == 2
    assert decision.error == ""


# ── 已有仓位的处置（：不能只会开仓） ──────────────────────────────────
def _held(symbol="RB0", side="long", stop_loss=None, atr=20.0, price=3400.0):
    """一个持有该品种仓位的账户，以及带 ATR 的快照。"""
    snap = _snapshot()
    snap.symbols[symbol] = SymbolView(
        symbol=symbol, spec=_view(symbol).spec, quote=Quote(symbol=symbol, price=price, ts=1.0, change_pct=0.5),
        ind_5m={"atr14": atr}, ind_15m={}, ind_1h={}, bars_5m=[])
    acc = _account()
    acc.positions = [{"symbol": symbol, "side": side, "qty": 2, "avg_price": price,
                      "last": price, "unrealized": 0.0, "margin": 100.0,
                      "stop_loss": stop_loss, "take_profit": None, "hold_min": 30}]
    return snap, acc


def _with_position_answers(base=None, exit_noul=0.10, protect_level=0, symbol="RB0"):
    answers = base if base is not None else _answers()
    answers["exit:" + symbol] = {"type": "noul", "noul": exit_noul}
    answers["protect:" + symbol] = {
        "type": "score", "score": protect_level,
        "probabilities": {str(i): (1.0 if i == protect_level else 0.0) for i in range(4)}}
    return answers


def test_a_held_position_gets_its_own_exit_and_protect_questions():
    snap, acc = _held()
    client = _StubClient(_payload(_with_position_answers()))
    _trader(client).decide(snap, acc, "规则")

    questions = client.calls[0].body["questions"]
    assert "exit:RB0" in questions and questions["exit:RB0"]["type"] == "noul"
    assert "protect:RB0" in questions and questions["protect:RB0"]["type"] == "score"
    assert "螺纹钢" in questions["exit:RB0"]["instructions"]
    assert questions["protect:RB0"]["criteria"][0] == "不调整"


def test_no_position_means_no_position_questions():
    client = _StubClient(_payload())
    _decide(_trader(client))
    assert not [k for k in client.calls[0].body["questions"] if k.startswith(("exit:", "protect:"))]


def test_the_seat_can_close_a_position():
    """只会开仓的席位没法落袋、没法反手，竞赛结果与文本席位不可比。"""
    snap, acc = _held()
    answers = _with_position_answers(_answers(noul=0.91), exit_noul=0.80)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    closes = [a for a in decision.actions if a.action == "close"]
    assert len(closes) == 1 and closes[0].symbol == "RB0"
    assert closes[0].confidence == pytest.approx(0.80)
    assert decision.error == ""


def test_standing_aside_still_manages_the_open_position():
    """"本轮不新开仓"不等于"不管已有的仓"——想空仓观望时更需要能平掉。"""
    snap, acc = _held()
    answers = _with_position_answers(_answers(noul=0.95), exit_noul=0.77)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert [a.action for a in decision.actions] == ["close"], "观望把平仓也一起吞掉了"
    assert "观望" in decision.analysis and "平仓" in decision.analysis


def test_closing_and_opening_the_other_way_in_one_round_is_a_reversal():
    snap, acc = _held(side="long")
    answers = _with_position_answers(_answers(direction="short", dir_conf=0.81), exit_noul=0.66)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert [a.action for a in decision.actions] == ["close", "open_short"], "反手要先平再开，顺序不能乱"


def test_the_stop_is_tightened_by_the_atr_ladder():
    snap, acc = _held(side="long", stop_loss=3300.0, atr=20.0, price=3400.0)
    answers = _with_position_answers(protect_level=2)      # 1.0×ATR
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    adjust = [a for a in decision.actions if a.action == "adjust_sl_tp"]
    assert len(adjust) == 1
    assert adjust[0].stop_loss == pytest.approx(3380.0), "止损没有按 1.0×ATR 收紧到 3380"


def test_a_looser_stop_is_never_emitted():
    """把止损往外挪等于用一次决策把风控拆了。"""
    snap, acc = _held(side="long", stop_loss=3390.0, atr=20.0, price=3400.0)
    answers = _with_position_answers(protect_level=1)      # 1.5×ATR → 3370，比 3390 松
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert not [a for a in decision.actions if a.action == "adjust_sl_tp"], "放宽了止损"


def test_a_short_position_tightens_from_above():
    snap, acc = _held(side="short", stop_loss=3500.0, atr=20.0, price=3400.0)
    answers = _with_position_answers(protect_level=3)      # 0.5×ATR
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    adjust = [a for a in decision.actions if a.action == "adjust_sl_tp"]
    assert len(adjust) == 1 and adjust[0].stop_loss == pytest.approx(3410.0)


def test_no_atr_means_no_invented_stop():
    """没有可核验的 ATR 就没有算止损的依据，不能凭空造一个价位。"""
    snap, acc = _held(side="long", stop_loss=None, atr=None)
    answers = _with_position_answers(protect_level=3)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert not [a for a in decision.actions if a.action == "adjust_sl_tp"]
    assert decision.error == ""


def test_a_position_outside_this_round_snapshot_is_left_alone():
    """休市或已移出配置的品种没有可核验的价，本轮不问也不动。"""
    snap = _snapshot(("I0",))
    acc = _account()
    acc.positions = [{"symbol": "RB0", "side": "long", "qty": 2, "avg_price": 3400.0,
                      "last": 3400.0, "unrealized": 0.0, "margin": 100.0,
                      "stop_loss": None, "take_profit": None, "hold_min": 30}]
    answers = _answers(symbol="I0")
    client = _StubClient(_payload(answers))
    decision = _trader(client).decide(snap, acc, "规则")

    assert not [k for k in client.calls[0].body["questions"] if k.startswith(("exit:", "protect:"))]
    assert not [a for a in decision.actions if a.symbol == "RB0"]
    assert decision.error == ""


# ── 必答的 noul 不能失效朝下单方向倒 ─────────────────────────────
@pytest.mark.parametrize("broken", [{}, {"type": "noul"}, {"noul": None},
                                    {"noul": "0.9"}, {"noul": float("nan")}, {"noul": 1.5},
                                    {"noul": -0.1}])
def test_a_malformed_stand_aside_answer_never_opens_a_trade(broken):
    """`{"stand_aside": {}}` 的 200 响应原来被读成"没有否决"，席位照常开仓。

    安全方向的答案坏掉时，失效必须朝拒绝的方向倒。
    """
    answers = _answers()
    answers["stand_aside"] = dict(broken)
    decision = _decide(_trader(_StubClient(_payload(answers))))

    assert decision.actions == [], "坏掉的 stand_aside answer 放行了一笔开仓"
    assert "不可解析" in decision.error, decision.error


def test_a_malformed_exit_answer_never_silently_keeps_the_position():
    """坏掉的 exit 答案原来被读成"不用平"，仓位就那么留着。"""
    snap, acc = _held()
    answers = _with_position_answers()
    answers["exit:RB0"] = {"type": "noul"}
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert decision.actions == []
    assert "不可解析" in decision.error, decision.error


def test_a_well_formed_boundary_noul_is_still_accepted():
    """0 和 1 是合法概率，收紧不能把它们一起拒了。"""
    for value, expect_actions in ((0.0, True), (1.0, False)):
        answers = _answers()
        answers["stand_aside"] = {"type": "noul", "noul": value}
        decision = _decide(_trader(_StubClient(_payload(answers))))
        assert decision.error == "", decision.error
        assert bool(decision.actions) is expect_actions


# ── 越界档位必须拒绝而不是夹紧 ───────────────────────────────────
@pytest.mark.parametrize("bad_score", [100, 4, 3.4, -1, -0.5])
def test_an_out_of_range_score_is_rejected_not_clamped(bad_score):
    """`{"size": {"score": 100}}` 原来被夹成最高档，直接下成重仓。"""
    answers = _answers()
    answers["size"] = {"type": "score", "score": bad_score}
    decision = _decide(_trader(_StubClient(_payload(answers))))

    assert decision.actions == [], f"score={bad_score} 被夹紧后下单了"
    assert "不可解析" in decision.error, decision.error


@pytest.mark.parametrize("probs", [{"0": 0.1, "99": 0.9}, {"0": 0.5, "-1": 0.5},
                                   {"1": -0.5, "2": 1.0}, {"0": 0.0, "1": 0.0}])
def test_a_malformed_score_distribution_is_rejected(probs):
    """越界或负权重的档位原来被静默跳过，剩下的够权重就算通过——模型真正的答案被丢掉。"""
    answers = _answers()
    answers["size"] = {"type": "score", "probabilities": probs}
    decision = _decide(_trader(_StubClient(_payload(answers))))

    assert decision.actions == []
    assert "不可解析" in decision.error, decision.error


def test_the_highest_legal_level_still_works():
    """收紧不能把合法的最高档一起拒了。"""
    answers = _answers(size_level=3)
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.error == ""
    assert [a.size_pct for a in decision.actions] == [SIZE_LADDER[3]]


def test_a_legal_distribution_still_averages_into_a_level():
    answers = _answers()
    answers["size"] = {"type": "score", "probabilities": {"1": 0.5, "3": 0.5}}   # 均值 2.0
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.error == ""
    assert [a.size_pct for a in decision.actions] == [SIZE_LADDER[2]]


# ── 契约校验必须在任何动作之前 ───────────────────────────────
@pytest.mark.parametrize("answers_patch", [
    {"entry:RB0": {"type": "choice", "choice": "sideways", "confidence": 0.9}},
    {"entry:RB0": {"type": "choice", "choice": "long", "confidence": 0.9,
                   "probabilities": {"long": 1.7, "short": 0.0, "none": 0.0}}},
    {"entry:RB0": {"type": "choice", "choice": "long", "confidence": 0.9,
                   "probabilities": {"long": "bad", "short": 0.0, "none": 0.0}}},
])
def test_a_malformed_choice_invalidates_the_whole_response_including_exits(answers_patch):
    """观望出口照样会发持仓处置，所以契约不合必须在 _manage() 之前整条作废。

    否则一个 `entry:RB0.choice="sideways"`（或一个坏掉的门限概率）就能把已有仓位平掉——
    那不是模型的判断，是解析出来的意外。
    """
    snap, acc = _held()
    answers = _with_position_answers(_answers(noul=0.95), exit_noul=0.99)   # 观望且想平仓
    answers.update(answers_patch)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert decision.actions == [], "契约外的答案仍然动了仓位"
    assert "不可解析" in decision.error, decision.error


def test_a_fractional_level_key_is_rejected():
    """`{"3.9": 1.0}` 原来被 int(float(key)) 截成 3——契约外的键变成合法的最高档。"""
    answers = _answers()
    answers["size"] = {"type": "score", "probabilities": {"3.9": 1.0}}
    decision = _decide(_trader(_StubClient(_payload(answers))))

    assert decision.actions == []
    assert "不可解析" in decision.error, decision.error


@pytest.mark.parametrize("bad_conf", [100, 1.5, -0.1])
def test_an_out_of_range_confidence_is_rejected(bad_conf):
    """confidence=100 原来能过门限，还会被原样写进 ActionRequest，风控层也只判有限性。"""
    answers = _answers()
    answers["entry:RB0"] = {"type": "choice", "choice": "long", "confidence": bad_conf,
                            "probabilities": {"long": 0.9, "short": 0.05, "none": 0.05}}
    decision = _decide(_trader(_StubClient(_payload(answers))))

    assert decision.actions == []
    assert "不可解析" in decision.error, decision.error


@pytest.mark.parametrize("ok_p", [0.0, 1.0])
def test_boundary_probabilities_are_still_accepted(ok_p):
    """0 和 1 是合法概率；收紧不能把它们一起拒了（0 低于门限，走观望而不是报错）。"""
    answers = _answers(dir_conf=ok_p, symbol_conf=ok_p)
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.error == "", decision.error
    assert bool(decision.actions) is (ok_p >= 0.55)


# ── "没给"与"给了但坏掉"必须区分 ──────────────────────
@pytest.mark.parametrize("bad", ["bad", None, float("nan"), True, [0.9]])
def test_a_present_but_nonnumeric_confidence_never_touches_a_position(bad):
    """坏掉的 confidence 原来和"故意没给"走同一条路——账户有仓位时照样能把它平掉。"""
    snap, acc = _held()
    answers = _with_position_answers(_answers(), exit_noul=0.99)   # 想平仓
    answers["entry:RB0"] = {"type": "choice", "choice": "long", "confidence": bad,
                            "probabilities": {"long": 0.9, "short": 0.05, "none": 0.05}}
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert decision.actions == [], "坏掉的 confidence 动了仓位"
    assert "不可解析" in decision.error, decision.error


def test_an_omitted_chosen_probability_still_means_stand_aside():
    """门限概率缺失仍然允许，按"低于门限"处理——区分的是"没给"和"给了但坏掉"。"""
    answers = _answers()
    answers["entry:RB0"] = {"type": "choice", "choice": "long", "confidence": 0.9,
                            "probabilities": {"short": 0.1, "none": 0.1}}
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.error == "" and decision.actions == []
    assert "观望" in decision.analysis


def test_an_omitted_confidence_no_longer_blocks_an_entry():
    """confidence 不再门控：缺了它照样按所选项概率决定。"""
    answers = _answers(dir_conf=0.80)
    del answers["entry:RB0"]["confidence"]
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.error == ""
    assert [(a.symbol, a.action) for a in decision.actions] == [("RB0", "open_long")]
    assert "conf=n/a" in decision.analysis


@pytest.mark.parametrize("bad", ["bad", None, float("inf"), True])
def test_a_present_but_malformed_score_does_not_fall_back_to_the_distribution(bad):
    """`{"score": "bad", "probabilities": {"3": 1.0}}` 原来开最高档——用另一个字段把
    不合契约的答案救成了合法动作。"""
    answers = _answers()
    answers["size"] = {"type": "score", "score": bad, "probabilities": {"3": 1.0}}
    decision = _decide(_trader(_StubClient(_payload(answers))))

    assert decision.actions == []
    assert "不可解析" in decision.error, decision.error


def test_an_omitted_score_still_uses_the_distribution():
    """缺 score 才用分布——收紧不能把正常的回退路径一起堵死。"""
    answers = _answers()
    answers["size"] = {"type": "score", "probabilities": {"2": 1.0}}
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.error == ""
    assert [a.size_pct for a in decision.actions] == [SIZE_LADDER[2]]


def test_a_malformed_protect_answer_blocks_the_exit_on_the_same_position():
    """`exit>=门限` 原来直接下平仓、跳过同一仓位的 protect 答案。

    那道题是我们问的，坏掉就说明整条响应不合契约，不能因为"这条路径用不上它"就放行。
    """
    snap, acc = _held()
    answers = _with_position_answers(_answers(), exit_noul=0.99, protect_level=0)
    answers["protect:RB0"] = {"type": "score", "score": 100}
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert decision.actions == [], "坏掉的 protect 答案没拦住这条响应"
    assert "不可解析" in decision.error, decision.error


# ── 同品种的持仓处置与新开仓必须先对账 ───────────────────────────
def test_closing_and_reopening_the_same_side_is_not_emitted():
    """平掉再同向开回来：白付两次手续费与滑点，敞口一点没变。"""
    snap, acc = _held(side="long")
    answers = _with_position_answers(_answers(direction="long", dir_conf=0.81), exit_noul=0.80)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert [a.action for a in decision.actions] == ["close"], "平掉之后又同向开了回来"
    assert "白付" in decision.analysis


def test_an_add_is_ordered_before_the_stop_it_would_otherwise_overwrite():
    """加仓排在收紧止损之后时，open() 自动生成的止损会把刚收紧的那个覆盖掉。"""
    snap, acc = _held(side="long", stop_loss=3300.0, atr=20.0, price=3400.0)
    answers = _with_position_answers(_answers(direction="long", dir_conf=0.81), protect_level=2)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert [a.action for a in decision.actions] == ["open_long", "adjust_sl_tp"], \
        "收紧止损排在了加仓之前，会被 open() 的自动止损覆盖"
    assert decision.actions[-1].stop_loss == pytest.approx(3380.0)


def test_an_opposite_entry_on_a_kept_position_is_not_emitted():
    """继续持有却反向开仓：撮合层与风控层都会拒，只留一条拒单审计。"""
    snap, acc = _held(side="long", stop_loss=3300.0, atr=20.0, price=3400.0)
    answers = _with_position_answers(_answers(direction="short", dir_conf=0.81), protect_level=2)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert [a.action for a in decision.actions] == ["adjust_sl_tp"], "发出了必被拒的反向开仓"
    assert "反向开仓会被风控拒" in decision.analysis


def test_a_reversal_is_still_emitted_as_close_then_opposite_entry():
    """这条是上面三条的边界：平仓 + 反向开仓是反手，必须原样保留。"""
    snap, acc = _held(side="long")
    answers = _with_position_answers(_answers(direction="short", dir_conf=0.81), exit_noul=0.66)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")
    assert [a.action for a in decision.actions] == ["close", "open_short"]


def test_an_entry_on_an_unheld_symbol_is_unaffected():
    """没有持仓的品种照常开——对账不能把正常路径一起挡了。"""
    snap, acc = _held(side="long")
    answers = _with_position_answers(_answers(symbol="I0", dir_conf=0.81), protect_level=0)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")
    assert [(a.action, a.symbol) for a in decision.actions] == [("open_long", "I0")]


def test_only_the_matching_adjustment_is_reordered_behind_the_add():
    """整体把加仓提前会让**别的品种**的平仓排到它后面。

    引擎顺序执行、每一步按当前总保证金判风险度：那笔平仓本来是为这次加仓腾额度的，
    排到后面就可能让加仓撞上风险上限被拒。
    """
    snap = _snapshot()
    for sym, price, atr in (("I0", 780.0, 5.0), ("RB0", 3400.0, 20.0)):
        snap.symbols[sym] = SymbolView(symbol=sym, spec=_view(sym).spec,
                                       quote=Quote(symbol=sym, price=price, ts=1.0, change_pct=0.5),
                                       ind_5m={"atr14": atr}, ind_15m={}, ind_1h={}, bars_5m=[])
    acc = _account()
    acc.positions = [
        {"symbol": "I0", "side": "long", "qty": 1, "avg_price": 780.0, "last": 780.0,
         "unrealized": 0.0, "margin": 100.0, "stop_loss": None, "take_profit": None, "hold_min": 30},
        {"symbol": "RB0", "side": "long", "qty": 2, "avg_price": 3400.0, "last": 3400.0,
         "unrealized": 0.0, "margin": 100.0, "stop_loss": 3300.0, "take_profit": None, "hold_min": 30},
    ]
    answers = _answers(symbol="RB0", direction="long", dir_conf=0.81)
    answers = _with_position_answers(answers, exit_noul=0.99, symbol="I0")       # I0 平仓腾额度
    answers = _with_position_answers(answers, exit_noul=0.10, protect_level=2, symbol="RB0")
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert [(a.action, a.symbol) for a in decision.actions] == [
        ("close", "I0"), ("open_long", "RB0"), ("adjust_sl_tp", "RB0"),
    ], "加仓被整体提到了所有处置动作之前，把 I0 的平仓挤到了后面"


# ── 交易员级的重试覆盖必须生效 ───────────────────────────────────
def test_a_seat_level_retry_override_limits_the_billable_calls():
    """给这个席位配 max_retries: 0 时，全局的 2 不能再让它发三次付费请求。"""
    boom = RuntimeError("boom")
    client = _StubClient(boom, boom, boom)
    trader = _trader(client, max_retries=2, max_retries_override=0)
    decision = trader.decide(_snapshot(), _account(), "规则")

    assert len(client.calls) == 1, f"席位级覆盖没生效，发了 {len(client.calls)} 次"
    assert decision.error


def test_a_seat_level_retry_override_can_also_raise_the_budget():
    boom = RuntimeError("boom")
    client = _StubClient(boom, boom, boom)
    trader = _trader(client, max_retries=0, max_retries_override=2)
    trader.decide(_snapshot(), _account(), "规则")
    assert len(client.calls) == 3, "调高的席位级覆盖被忽略了"


def test_the_round_budget_counts_the_jev_seat():
    """轮超时按能力取：席位只要实现了 decision_timeout_budget_sec 就算进去。

    原来引擎按 isinstance(trader, LlmTrader) 判断，Jev 席位整个被漏掉——它那份重试预算
    没进轮超时，慢的那一轮会被引擎写成合成超时，而席位配置的重试策略还没跑完。
    """
    trader = _trader(max_retries=0, max_retries_override=2)
    assert trader.decision_timeout_budget_sec() == pytest.approx(3 * trader.lcfg.timeout_sec + 3)


def test_an_out_of_range_probability_weight_is_rejected():
    """只判非负不够：{"0": 0.1, "3": 100} 归一化后落在最高档，而 100 不是概率。"""
    answers = _answers()
    answers["size"] = {"type": "score", "probabilities": {"0": 0.1, "3": 100}}
    decision = _decide(_trader(_StubClient(_payload(answers))))

    assert decision.actions == [], "越界权重归一化之后开了最高档"
    assert "不可解析" in decision.error, decision.error


def test_a_legal_weight_of_one_is_still_accepted():
    """1.0 是合法概率，收紧不能把它一起拒了。"""
    answers = _answers()
    answers["size"] = {"type": "score", "probabilities": {"2": 1.0}}
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.error == ""
    assert [a.size_pct for a in decision.actions] == [SIZE_LADDER[2]]


def test_the_jev_roster_signature_covers_max_retries():
    """改了这个席位的 max_retries 就该另起一张排行榜：付费次数与可能拿到的决策都变了。"""
    from arena.engine.loop import Engine as _Engine

    def cfg_with(retries):
        return AppConfig(traders=[TraderCfg(id="jev", name="Jev", provider="jev",
                                            base_url="https://api.typesafe.ai", model="jev-latest",
                                            max_retries=retries)])

    a = _Engine._configured_roster_signature(cfg_with(0))
    b = _Engine._configured_roster_signature(cfg_with(3))
    assert a != b, "max_retries 变了，阵容签名却没变，旧竞赛会被直接续上"


def test_weights_that_do_not_sum_to_one_are_rejected():
    """每个值都在 0..1 还不够：{"0": 1.0, "3": 1.0} 归一化之后照样开仓，但它不是概率分布。"""
    answers = _answers()
    answers["size"] = {"type": "score", "probabilities": {"0": 1.0, "3": 1.0}}
    decision = _decide(_trader(_StubClient(_payload(answers))))

    assert decision.actions == []
    assert "不可解析" in decision.error, decision.error


def test_a_distribution_summing_to_one_is_still_accepted():
    answers = _answers()
    answers["size"] = {"type": "score", "probabilities": {"1": 0.5, "3": 0.5}}
    decision = _decide(_trader(_StubClient(_payload(answers))))
    assert decision.error == ""
    assert [a.size_pct for a in decision.actions] == [SIZE_LADDER[2]]


@pytest.mark.parametrize("provider,extra", [
    ("jev", {"base_url": "https://api.typesafe.ai", "model": "jev-latest"}),
    ("openai_compatible", {"base_url": "https://api.example.com", "model": "m"}),
])
def test_the_roster_signature_records_the_effective_retry_count(provider, extra):
    """席位没配 max_retries 时回落到全局——全局变了，这名选手的行为就变了。

    只记配置字段的话，全局从 0 改成 3 时签名里那个 None 一动不动，旧排行榜被直接续上。
    """
    from arena.config import LlmCfg
    from arena.engine.loop import Engine as _Engine

    def cfg_with(global_retries):
        return AppConfig(llm=LlmCfg(max_retries=global_retries),
                         traders=[TraderCfg(id="x", name="X", provider=provider, **extra)])

    assert _Engine._configured_roster_signature(cfg_with(0)) != \
        _Engine._configured_roster_signature(cfg_with(3)), "全局重试次数变了，签名却没变"


def test_a_same_side_add_carries_the_existing_stop_when_protection_is_unchanged():
    """档位选"不调整"时，加仓不能顺手把已有的止损放宽。

    加仓请求不带止损 → RiskManager 自动生成 `价 ± 2×ATR` → SimExchange.open() 用它覆盖
    原有止损。原有止损更紧时，一条明确说"不调整保护"的回复反而把保护放松了。
    """
    snap, acc = _held(side="long", stop_loss=3390.0, atr=20.0, price=3400.0)
    answers = _with_position_answers(_answers(direction="long", dir_conf=0.81), protect_level=0)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert [a.action for a in decision.actions] == ["open_long"]
    assert decision.actions[0].stop_loss == pytest.approx(3390.0), \
        "加仓没带上原有止损，自动止损会把它放宽"


def test_an_entry_on_a_symbol_without_a_position_keeps_its_own_stop_handling():
    """没有持仓的品种不受影响：止损仍由风控按规则生成。"""
    snap, acc = _held(side="long", stop_loss=3390.0)
    answers = _with_position_answers(_answers(symbol="I0", dir_conf=0.81), protect_level=0)
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    entries = [a for a in decision.actions if a.action == "open_long"]
    assert len(entries) == 1 and entries[0].symbol == "I0"
    assert entries[0].stop_loss is None, "给没有持仓的品种硬塞了别人的止损"


def test_unrelated_closes_stay_ahead_of_the_add_whatever_the_position_order():
    """持仓顺序是 [RB0, I0] 时处置动作是 [adjust RB0, close I0]：把加仓插到那条 adjust 的位置，
    I0 的平仓照样被挤到加仓之后（第 23 轮审查）。只有同品种的 adjust 该挪到加仓之后。"""
    snap = _snapshot()
    for sym, price, atr in (("RB0", 3400.0, 20.0), ("I0", 780.0, 5.0)):
        snap.symbols[sym] = SymbolView(symbol=sym, spec=_view(sym).spec,
                                       quote=Quote(symbol=sym, price=price, ts=1.0, change_pct=0.5),
                                       ind_5m={"atr14": atr}, ind_15m={}, ind_1h={}, bars_5m=[])
    acc = _account()
    acc.positions = [
        {"symbol": "RB0", "side": "long", "qty": 2, "avg_price": 3400.0, "last": 3400.0,
         "unrealized": 0.0, "margin": 100.0, "stop_loss": 3300.0, "take_profit": None, "hold_min": 30},
        {"symbol": "I0", "side": "long", "qty": 1, "avg_price": 780.0, "last": 780.0,
         "unrealized": 0.0, "margin": 100.0, "stop_loss": None, "take_profit": None, "hold_min": 30},
    ]
    answers = _answers(symbol="RB0", direction="long", dir_conf=0.81)
    answers = _with_position_answers(answers, exit_noul=0.10, protect_level=2, symbol="RB0")
    answers = _with_position_answers(answers, exit_noul=0.99, symbol="I0")       # I0 平仓腾额度
    decision = _trader(_StubClient(_payload(answers))).decide(snap, acc, "规则")

    assert [(a.action, a.symbol) for a in decision.actions] == [
        ("close", "I0"), ("open_long", "RB0"), ("adjust_sl_tp", "RB0"),
    ], "I0 的平仓被排到了 RB0 加仓之后"


# ── 第 33 轮：usage 是可选遥测，写坏了不能否定一份有效的 answers ─────────────────
@pytest.mark.parametrize("usage, expected_in", [
    (None, 0),                                              # 根本没有 usage
    ("n/a", 0),                                             # 不是对象
    ({"input_tokens": "abc", "output_tokens": None}, 0),    # 数值写坏
    ({"input_tokens": True, "output_tokens": -3}, 0),       # 布尔与负数
    ({"input_tokens": 12.7}, 12),                           # 能转成整数的照收
])
def test_malformed_usage_telemetry_does_not_invalidate_the_answers(usage, expected_in):
    """原来 `usage.get()`/`int()` 直接抛异常：decide() 把它当成可重试的调用失败，再发一次付费请求，
    最后还可能把有效答案整个丢掉。计费遥测坏了只影响计数，不影响决策，也不触发重试。"""
    payload = {"model": "jev-latest", "answers": _answers()}
    if usage is not None:
        payload["usage"] = usage
    client = _StubClient(payload)
    decision = _decide(_trader(client, max_retries=2))

    assert decision.error == "", decision.error
    assert decision.actions and decision.actions[0].symbol == "RB0"
    assert decision.tokens_in == expected_in and decision.tokens_out == 0
    assert len(client.calls) == 1, "遥测写坏引发了重试"


# ── HTTP 200 但响应体不合契约，不当传输错误重试 ─────────────────────
class _MalformedJsonResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return json.loads("{not json")


class _MalformedJsonClient(_StubClient):
    def post(self, path, json=None):
        self.calls.append(SimpleNamespace(path=path, body=json))
        return _MalformedJsonResponse()


@pytest.mark.parametrize("client_factory", [
    lambda: _MalformedJsonClient(),                                  # 不是合法 JSON
    lambda: _StubClient(["answers"], ["answers"], ["answers"]),      # 不是对象
    lambda: _StubClient({"model": "jev-latest"}, {}, {}),            # 没有 answers
    lambda: _StubClient({"answers": "n/a"}, {}, {}),                 # answers 不是对象
])
def test_a_malformed_response_body_is_not_retried(client_factory):
    """原来这些校验在 _call() 里抛 ValueError，decide() 当成可重试的传输错误，默认配置下发三次付费请求。"""
    client = client_factory()
    decision = _decide(_trader(client, max_retries=2))

    assert len(client.calls) == 1, f"响应契约违约被重试了 {len(client.calls)} 次"
    assert decision.error.startswith("Jev 响应不可解析"), decision.error
    assert decision.actions == []


def test_a_transport_error_is_still_retried():
    client = _StubClient(RuntimeError("boom"), _payload())
    decision = _decide(_trader(client, max_retries=2))
    assert len(client.calls) == 2
    assert decision.error == "", decision.error
