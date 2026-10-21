"""国内期货合约表的结构校验：全品种表是手写数据，字段写错不会让任何现有测试失败，
但会直接导致保证金、手续费或开市判定算错，所以在这里把不变量钉住。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from arena.config import load_config
from arena.exchange.contracts import load_cn_contracts, session_open

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "config" / "contracts_cn.json"
SH = ZoneInfo("Asia/Shanghai")
EXCHANGES = {"SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"}


@pytest.fixture(scope="module")
def raw():
    return json.loads(TABLE.read_text(encoding="utf-8"))["contracts"]


def test_every_contract_has_a_usable_spec(raw):
    for code, c in raw.items():
        assert code.endswith("0") and code[:-1].isupper(), f"{code} 不是新浪主力连续代码"
        assert c["exchange"] in EXCHANGES, f"{code} 交易所未知：{c['exchange']}"
        assert c["name"], f"{code} 缺名称"
        assert c["multiplier"] > 0 and c["tick"] > 0, f"{code} 乘数/最小变动必须为正"
        assert 0 < c["margin_ratio"] < 1, f"{code} 保证金比例应在 0~1 之间：{c['margin_ratio']}"
        assert c["fee_per_lot"] >= 0 and c["fee_rate"] >= 0, f"{code} 手续费不能为负"
        assert not (c["fee_per_lot"] and c["fee_rate"]), f"{code} 两种手续费只能取一种（非零者生效）"


def test_sessions_are_ordered_and_night_uses_24_plus(raw):
    """夜盘跨日必须写成 24+ 小时（session_open 按此归属前一交易日），不能写成 01:00。"""
    for code, c in raw.items():
        assert c["sessions"], f"{code} 没有交易时段"
        prev_end = -1
        for start, end in c["sessions"]:
            s, e = _minutes(start), _minutes(end)
            assert s < e, f"{code} 时段 {start}-{end} 起止颠倒"
            assert s > prev_end, f"{code} 时段未按先后排列或重叠：{start}-{end}"
            prev_end = e
        assert prev_end <= 27 * 60, f"{code} 夜盘收盘时间越界：{c['sessions'][-1]}"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def test_the_universe_covers_every_exchange_a_domestic_account_can_reach(raw):
    """一个权限齐备的境内期货账户能覆盖六家交易所；少一家说明表被删漏了。"""
    assert {c["exchange"] for c in raw.values()} == EXCHANGES
    assert len(raw) >= 80, f"全品种表不该缩水到 {len(raw)} 个"


@pytest.mark.parametrize("code, when, expected", [
    ("AU0", "2026-09-18 01:30", True),    # 贵金属夜盘到次日 02:30
    ("AU0", "2026-09-18 03:00", False),
    ("CU0", "2026-09-18 00:30", True),    # 有色夜盘到次日 01:00
    ("RB0", "2026-09-18 22:00", True),    # 黑色夜盘到 23:00
    ("RB0", "2026-09-18 23:30", False),
    ("SF0", "2026-09-18 22:00", False),   # 硅铁没有夜盘
    ("IF0", "2026-09-18 09:45", True),    # 股指 09:30 才开盘
    ("IF0", "2026-09-18 09:20", False),
    ("T0", "2026-09-18 15:10", True),     # 国债收盘比股指晚 15 分钟
    ("IF0", "2026-09-18 15:10", False),
])
def test_session_boundaries_per_product_family(code, when, expected):
    specs, holidays = load_cn_contracts(TABLE)
    at = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=SH)
    assert session_open(specs[code], at, holidays) is expected


def test_example_config_only_lists_symbols_the_table_defines():
    """配置里写了表里没有的代码时，引擎起不来——这种错要在测试里就暴露。

    看的是**展开之后**的有效品种：模板现在写 `symbols: all`，按 YAML 原文读到的是那个词本身。
    """
    cfg = load_config(ROOT / "config" / "config.example.yaml", base_dir=ROOT)
    table = json.loads(TABLE.read_text(encoding="utf-8"))["contracts"]
    unknown = [s for s in cfg.symbols if s not in table]
    assert not unknown, f"配置引用了合约表里没有的品种：{unknown}"
