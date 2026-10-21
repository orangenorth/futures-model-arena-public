from arena.config import RiskCfg
from arena.traders.prompt import parse_response, rules_text


SYMS = ["RB0", "I0", "AU0"]


def test_parse_clean_json():
    txt = '{"analysis":"看多","actions":[{"symbol":"RB0","action":"open_long","size_pct":0.2,"stop_loss":3300,"take_profit":3600,"confidence":0.7,"reason":"x"}]}'
    a, acts, err = parse_response(txt, SYMS)
    assert a == "看多" and not err
    assert acts[0].symbol == "RB0" and acts[0].action == "open_long" and acts[0].stop_loss == 3300


def test_parse_fenced_and_aliases():
    txt = '```json\n{"analysis":"x","actions":[{"symbol":"rb2510","action":"buy","size_pct":20,},{"symbol":"ZZ","action":"hold"}]}\n```'
    a, acts, err = parse_response(txt, SYMS)
    assert len(acts) == 1 and acts[0].symbol == "RB0" and acts[0].action == "open_long" and acts[0].size_pct == 0.2
    assert "ZZ" in err


def test_parse_garbage():
    a, acts, err = parse_response("我认为应该买入螺纹钢", SYMS)
    assert acts == [] and err


def test_parse_text_around_json():
    txt = '好的，以下是决策：\n{"analysis":"a","actions":[]}\n以上。'
    a, acts, err = parse_response(txt, SYMS)
    assert a == "a" and acts == [] and not err


def test_rules_describe_conditional_target_risk_band_without_forcing_a_trade():
    rules = rules_text(
        RiskCfg(target_margin_risk_min_ratio=0.80, target_margin_risk_max_ratio=0.90,
                max_margin_risk_ratio=0.95),
        "cn_futures", "CNY", 80_000, 300,
    )
    assert "80%–90%" in rules
    assert "1h 与 15m 方向一致" in rules
    assert "没有有效信号时允许 hold" in rules
    assert "严禁为凑目标仓位强开" in rules
    assert "达到目标上沿 90% 后不得继续增加风险" in rules
    assert "不得超过 95%" in rules
