"""第二十九/三十轮审查：品种映射按完整代码匹配——国内品种比较完整字母代码，加密合约比较完整交易对（含计价币）。"""
import json

from arena.traders.prompt import _symbol_key, parse_response


def _actions(symbol, known):
    text = json.dumps({"analysis": "x", "actions": [{"symbol": symbol, "action": "open_long", "size_pct": 0.1}]})
    analysis, actions, err = parse_response(text, known)
    return [a.symbol for a in actions], err


def test_symbol_key():
    assert _symbol_key("RB2510") == ("RB", False) and _symbol_key("rb0") == ("RB", False) and _symbol_key("RB") == ("RB", False)
    assert _symbol_key("IF0") == ("IF", False) and _symbol_key("I0") == ("I", False)
    assert _symbol_key("BTC-USDT-SWAP") == ("BTCUSDT", True) and _symbol_key("BTC/USDT") == ("BTCUSDT", True)
    assert _symbol_key("BTC-USD-SWAP") == ("BTCUSD", True) and _symbol_key("btcusdt") == ("BTCUSDT", False)
    assert _symbol_key("2510") == ("", False)


def test_prefix_of_another_root_is_not_remapped():
    known = ["RB0", "I0", "AU0", "TA0"]
    assert _actions("IF0", known) == ([], "未知品种 IF0"), "股指 IF0 不能映射成铁矿石 I0"
    assert _actions("AUG", known) == ([], "未知品种 AUG")
    assert _actions("T2512", known) == ([], "未知品种 T2512"), "国债 T 不能映射成 PTA TA0"
    assert _actions("RBB", known) == ([], "未知品种 RBB")
    assert _actions("RB/USDT", known) == ([], "未知品种 RB/USDT")


def test_full_root_forms_still_map():
    known = ["RB0", "I0", "AU0", "TA0"]
    assert _actions("rb2510", known) == (["RB0"], "")
    assert _actions("RB", known) == (["RB0"], "")
    assert _actions("I2509", known) == (["I0"], "")
    assert _actions("ta0", known) == (["TA0"], "")


def test_crypto_requires_the_complete_pair():
    known = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert _actions("btc-usdt-swap", known) == (["BTC-USDT-SWAP"], "")
    assert _actions("BTC/USDT", known) == (["BTC-USDT-SWAP"], ""), "分隔符变体可映射"
    assert _actions("BTCUSDT", known) == (["BTC-USDT-SWAP"], ""), "连写变体可映射"
    assert _actions("BTC-USDT-PERP", known) == (["BTC-USDT-SWAP"], "")
    assert _actions("BTC-USD-SWAP", known) == ([], "未知品种 BTC-USD-SWAP"), "计价币不同的合约不能映射到 USDT 合约"
    assert _actions("BTC/USDC", known) == ([], "未知品种 BTC/USDC")
    assert _actions("BTC", known) == ([], "未知品种 BTC"), "只写基础币不够完整"
    assert _actions("BTC-USDT-SWAP", ["BTC-USDT-SWAP", "BTC-USD-SWAP"]) == (["BTC-USDT-SWAP"], "")
    assert _actions("BTCUSD", ["BTC-USDT-SWAP", "BTC-USD-SWAP"]) == (["BTC-USD-SWAP"], "")
