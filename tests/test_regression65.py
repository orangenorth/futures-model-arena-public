"""Regression 65：老装机要能用一行切到全品种。

`load_config()` 优先读 `config/config.yaml`，而那个文件在 .gitignore 里——把模板从八个代码扩到
七十一个，对正在跑的那份一个字都不会变，看板上仍然是八个合约。要么让用户手抄七十一行，
要么给一个 `symbols: all`。这里钉住后者的行为。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena.config import load_config
from arena.exchange.contracts import load_cn_symbols

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "config" / "contracts_cn.json"
DORMANT = {"WR0", "FB0", "BB0", "RR0", "RS0", "ZC0", "WH0", "PM0", "JR0", "RI0", "LR0"}


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_all_expands_to_every_active_contract(tmp_path):
    cfg = load_config(write(tmp_path, "competition:\n  market: cn_futures\nsymbols: all\n"), base_dir=ROOT)
    table = json.loads(TABLE.read_text(encoding="utf-8"))["contracts"]
    assert len(cfg.symbols) == 71
    assert set(cfg.symbols) <= set(table), "展开出了合约表里没有的代码"
    assert not (set(cfg.symbols) & DORMANT), "active: false 的品种不该被展开进来"
    assert len(cfg.symbols) == len(set(cfg.symbols)), "展开的名单有重复"


def test_all_follows_the_contract_table_it_is_pointed_at(tmp_path):
    """展开的是配置指定的那张表，不是写死的名单；`active: false` 在那张表里同样生效。"""
    table = tmp_path / "mine.json"
    table.write_text(json.dumps({"contracts": {
        "RB0": {"name": "螺纹钢", "multiplier": 10, "tick": 1},
        "I0": {"name": "铁矿石", "multiplier": 100, "tick": 0.5},
        "WR0": {"name": "线材", "multiplier": 10, "tick": 1, "active": False},
    }}, ensure_ascii=False), encoding="utf-8")
    cfg = load_config(write(tmp_path, f"""
competition:
  market: cn_futures
symbols: all
market_data:
  cn_futures:
    contracts_file: {table.name}
"""), base_dir=tmp_path)
    assert cfg.symbols == ["RB0", "I0"], "没有按配置指定的合约表展开，或没有跳过 active: false"


def test_an_explicit_list_is_left_alone(tmp_path):
    cfg = load_config(write(tmp_path, "competition:\n  market: cn_futures\nsymbols: [RB0, I0]\n"), base_dir=ROOT)
    assert cfg.symbols == ["RB0", "I0"]


def test_all_needs_a_contract_table_so_crypto_says_so(tmp_path):
    """crypto/replay 没有合约表。这里必须是一条能看懂的报错，不能是一个空名单或一个叫 all 的品种。"""
    with pytest.raises(ValueError, match="合约表"):
        load_config(write(tmp_path, "competition:\n  market: crypto\nsymbols: all\n"), base_dir=ROOT)


def test_a_misspelled_keyword_is_rejected_not_taken_as_a_symbol(tmp_path):
    """写错的关键词按字符串逐字符当成品种名单，是最坏的失败方式：静默跑起来却只有假品种。"""
    with pytest.raises(ValueError, match="symbols"):
        load_config(write(tmp_path, "competition:\n  market: cn_futures\nsymbols: everything\n"), base_dir=ROOT)


def test_the_table_marks_exactly_the_eleven_dormant_contracts():
    """82 个品种里正好那 11 个是 active: false——数字对不上说明标记漏了或多了。"""
    table = json.loads(TABLE.read_text(encoding="utf-8"))["contracts"]
    inactive = {sym for sym, c in table.items() if not c.get("active", True)}
    assert len(table) == 82
    assert inactive == DORMANT
    assert len(load_cn_symbols(TABLE)) == 71
    assert len(load_cn_symbols(TABLE, include_inactive=True)) == 82
