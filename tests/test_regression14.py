"""第十四轮审查：预算之外才完成的行情抓取结果必须在下个 tick 交付，不能被新请求替换掉。"""
import threading
import time
from pathlib import Path

from arena.exchange.contracts import load_cn_contracts
from arena.market.sina_cn import SinaFuturesSource

ROOT = Path(__file__).resolve().parents[1]
ROW = [["2026-09-16 09:05:00", "3400", "3410", "3390", "3405", "120", "100000"]]


def make_sina(symbols, timeout):
    specs, holidays = load_cn_contracts(ROOT / "config" / "contracts_cn.json")
    return SinaFuturesSource(symbols, specs, holidays, timeout=timeout)


def test_sina_latest_delivers_results_that_completed_after_the_budget():
    src = make_sina(["RB0"], timeout=0.2)      # 预算 1.2s
    src.cache_sec = 0                          # tick 间隔长于 K 线缓存：替换请求不会命中缓存
    gate = threading.Event()
    calls = []

    def gated_get(method, params, attempts=3):
        calls.append(1)
        gate.wait(5)
        return {s: ROW for s in params["symbols"]}

    src._get = gated_get  # type: ignore[method-assign]
    assert src.latest(["RB0"]) == {}           # 第一次：预算内未返回
    gate.set()
    src._inflight["RB0"].result(timeout=3)     # 预算之后才完成
    gate.clear()                               # 新发起的替换请求再次挂起
    t0 = time.time()
    q = src.latest(["RB0"])
    assert q["RB0"].price == 3405, "迟到完成的结果必须先交付，而不是被替换请求丢弃"
    assert time.time() - t0 < 2.5
    _wait_calls(calls, 2)
    assert len(calls) == 2 and "RB0" in src._inflight and not src._inflight["RB0"].done(), "替换请求仍在后台进行"
    gate.set()
    src._inflight["RB0"].result(timeout=3)
    q2 = src.latest(["RB0"])                   # 迟到结果交付；替换请求流水线式继续（缓存已关，会再请求一次）
    _wait_calls(calls, 3)
    assert q2["RB0"].price == 3405 and len(calls) == 3
    assert "RB0" in src._inflight, "替换请求应在后台继续，留待下个 tick 交付"
    src.close()


def _wait_calls(calls, n, timeout=2.0):
    """latest() 已不等待后台替换请求：给工作线程一点时间真正发出该请求，再检查请求计数。"""
    deadline = time.time() + timeout
    while len(calls) < n and time.time() < deadline:
        time.sleep(0.01)


def test_sina_latest_does_not_redeliver_consumed_results_and_reports_late_failures():
    src = make_sina(["RB0"], timeout=0.2)
    src.cache_sec = 0
    gate = threading.Event()
    mode = {"fail": False}

    def gated_get(method, params, attempts=3):
        gate.wait(5)
        if mode["fail"]:
            raise RuntimeError("upstream 502")
        return {s: ROW for s in params["symbols"]}

    src._get = gated_get  # type: ignore[method-assign]
    gate.set()
    assert src.latest(["RB0"])["RB0"].price == 3405   # 预算内返回并交付
    assert "RB0" not in src._inflight
    gate.clear()
    mode["fail"] = True
    assert src.latest(["RB0"]) == {}                  # 挂起：本次跳过
    gate.set()
    assert src._inflight["RB0"].exception(timeout=3) is not None   # 迟到的失败
    assert src.latest(["RB0"]) == {}                  # 迟到失败只记日志；随后发起的新请求同样失败 → 无报价
    assert "RB0" not in src._inflight, "失败结果同样算已交付"
    src.close()
