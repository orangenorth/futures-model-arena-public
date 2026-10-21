import os

import pytest
from pydantic import ValidationError

from arena.config import RiskCfg, TraderCfg, load_config


def test_api_key_precedence(monkeypatch):
    monkeypatch.setenv("T_KEY", "from-env")
    t = TraderCfg(id="a", name="A", api_key_env="T_KEY")
    assert t.resolved_api_key() == "from-env" and t.is_live_llm
    t2 = TraderCfg(id="b", name="B", api_key_env="T_KEY", api_key="literal")
    assert t2.resolved_api_key() == "literal"
    t3 = TraderCfg(id="c", name="C", api_key_env="T_KEY", api_key="  ")
    assert t3.resolved_api_key() == "from-env"
    monkeypatch.delenv("T_KEY")
    assert TraderCfg(id="d", name="D", api_key_env="T_KEY").resolved_api_key() == ""
    assert not TraderCfg(id="d", name="D", api_key_env="T_KEY").is_live_llm


def test_key_mistakenly_in_env_field_is_used(monkeypatch):
    literal = "fake-key.value-with-dots"  # 含 '-' 和 '.'，不可能是环境变量名
    t = TraderCfg(id="a", name="A", api_key_env=literal)
    assert t.resolved_api_key() == literal and t.is_live_llm


def test_example_config_has_no_literal_keys():
    cfg = load_config("config/config.example.yaml")
    assert cfg.risk.target_margin_risk_min_ratio == 0.80
    assert cfg.risk.target_margin_risk_max_ratio == 0.90
    assert cfg.risk.max_margin_risk_ratio == 0.95
    assert cfg.preopen_debate.stage_timeout_sec == 60
    for t in cfg.traders:
        assert t.api_key == ""
        assert t.api_key_env.isupper(), f"{t.id}: api_key_env 应为环境变量名"


def test_example_config_routes_zhipu_glm_through_zhipu_openai_compatibility():
    traders = {t.id: t for t in load_config("config/config.example.yaml").traders}
    qwen, glm = traders["qwen"], traders["glm"]
    assert qwen.model == "qwen3.8-max"
    assert qwen.reasoning_effort == "low"
    assert qwen.max_retries == 0
    assert qwen.max_completion_tokens == 3584
    assert qwen.research_max_completion_tokens == 1536
    assert glm.name == "智谱 GLM-5.3"
    assert glm.model == "glm-5.3"
    assert glm.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert glm.base_url != qwen.base_url
    assert glm.api_key_env == "ZHIPU_API_KEY"
    assert qwen.api_key_env == "DASHSCOPE_API_KEY"
    assert glm.max_tokens == 4096
    assert glm.reasoning_effort == "low"
    assert glm.max_retries == 0


def test_example_config_configures_kimi_k3():
    kimi = {t.id: t for t in load_config("config/config.example.yaml").traders}["kimi"]
    assert kimi.model == "kimi-k3"
    assert kimi.base_url == "https://api.moonshot.cn/v1"
    assert kimi.api_style == "chat"
    assert kimi.api_key_env == "MOONSHOT_API_KEY"
    assert kimi.temperature == 1.0
    assert kimi.max_completion_tokens == 4096
    assert kimi.reasoning_effort == "low"


def test_trader_output_token_overrides_are_validated():
    assert TraderCfg(id="a", name="A").max_tokens is None
    assert TraderCfg(id="b", name="B", max_tokens=4096, reasoning_effort="low").max_tokens == 4096
    assert TraderCfg(id="c", name="C", max_completion_tokens=4096).max_completion_tokens == 4096
    assert TraderCfg(id="research", name="Research", max_completion_tokens=3584,
                     research_max_completion_tokens=1536).research_max_completion_tokens == 1536
    assert TraderCfg(id="retries", name="Retries", max_retries=0).max_retries == 0
    assert TraderCfg(id="temperature", name="Temperature", temperature=1.0).temperature == 1.0
    with pytest.raises(ValidationError):
        TraderCfg(id="d", name="D", max_tokens=0)
    with pytest.raises(ValidationError):
        TraderCfg(id="e", name="E", max_tokens=1024, max_completion_tokens=2048)
    with pytest.raises(ValidationError):
        TraderCfg(id="f", name="F", api_style="responses", max_completion_tokens=2048)
    with pytest.raises(ValidationError):
        TraderCfg(id="research-responses", name="Research responses", api_style="responses",
                  research_max_completion_tokens=1536)
    with pytest.raises(ValidationError):
        TraderCfg(id="research-small", name="Research small", research_max_completion_tokens=127)
    with pytest.raises(ValidationError):
        TraderCfg(id="research-large", name="Research large", research_max_completion_tokens=4097)
    with pytest.raises(ValidationError):
        TraderCfg(id="g", name="G", reasoning_effort="invalid")
    with pytest.raises(ValidationError):
        TraderCfg(id="h", name="H", api_style="responses", temperature=1.0)
    with pytest.raises(ValidationError):
        TraderCfg(id="negative-retries", name="Negative retries", max_retries=-1)


def test_target_margin_band_requires_a_complete_safe_pair():
    cfg = RiskCfg(target_margin_risk_min_ratio=0.80, target_margin_risk_max_ratio=0.90,
                  max_margin_risk_ratio=0.95, liquidation_risk_ratio=1.0)
    assert cfg.opening_margin_risk_cap == 0.90
    with pytest.raises(ValidationError):
        RiskCfg(target_margin_risk_min_ratio=0.80, max_margin_risk_ratio=0.95)
    with pytest.raises(ValidationError):
        RiskCfg(target_margin_risk_min_ratio=0.91, target_margin_risk_max_ratio=0.90,
                max_margin_risk_ratio=0.95)
    with pytest.raises(ValidationError):
        RiskCfg(target_margin_risk_min_ratio=0.80, target_margin_risk_max_ratio=0.96,
                max_margin_risk_ratio=0.95)
    with pytest.raises(ValidationError):
        RiskCfg(target_margin_risk_min_ratio=0.80, target_margin_risk_max_ratio=0.90,
                warning_margin_risk_ratio=0.89, max_margin_risk_ratio=0.95)
