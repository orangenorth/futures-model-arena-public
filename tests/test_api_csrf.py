"""写接口（/api/control）拒绝跨站请求：本机浏览器打开的任意网页，其请求对端同样是回环地址，
只靠"对端是本机"挡不住；同源看板与非浏览器客户端不受影响。
另外校验 Host：DNS 重绑定能让攻击者页面拿到"同源"的请求上下文，来源头这时全部合法。"""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from arena.config import AppConfig, TraderCfg
from arena.db import Database
from arena.desktop import client_host, start_server
from arena.engine.loop import Engine
from arena.server.api import create_app

ROOT = Path(__file__).resolve().parents[1]
OPEN_RB = json.dumps({"analysis": "x", "actions": [{"symbol": "RB0", "action": "open_long", "size_pct": 0.1, "stop_loss": 1}]})
LOCAL = "http://127.0.0.1:8765"          # 看板真实开在回环地址上，测试请求也要带这样的 Host


def make_cfg(tmp_path, **server):
    return AppConfig(
        competition={"name": "t", "market": "replay", "initial_cash": 1_000_000, "decision_interval_sec": 1,
                     "tick_interval_sec": 1, "snapshot_interval_sec": 1},
        symbols=["RB0"],
        market_data={"replay": {"seed": 3, "start_price": {"RB0": 3400}}, "cn_futures": {"contracts_file": "config/contracts_cn.json"}},
        risk={"min_hold_sec": 0, "max_size_pct": 0.3},
        llm={"timeout_sec": 5, "max_retries": 0},
        traders=[TraderCfg(id="ok", name="OK", provider="mock", mock_response=OPEN_RB)],
        storage={"db_path": str(tmp_path / "e.db")},
        server=server,
        base_dir=ROOT,
    )


@pytest.fixture
def client(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    yield TestClient(create_app(eng, ROOT / "web"), base_url=LOCAL), eng
    eng.stop()
    db.close()


def pause(c, **headers):
    return c.post("/api/control", json={"action": "pause"}, headers=headers or None)


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},                                  # 恶意站点
    {"Sec-Fetch-Site": "same-site"},                                   # 本机另一个端口上的页面
    {"Origin": "https://evil.example"},                                # 无 Sec-Fetch-Site 的旧浏览器
    {"Origin": "null"},                                                # sandbox iframe / file://
])
def test_cross_site_write_is_refused(client, headers):
    c, eng = client
    r = pause(c, **headers)
    assert r.status_code == 403 and "跨站" in r.json()["detail"]
    assert eng.paused is False, "被拒绝的请求不能产生副作用"


def test_cross_site_reset_cannot_archive_the_competition(client):
    c, eng = client
    before = eng.comp_id
    r = c.post("/api/control", json={"action": "reset"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and eng.comp_id == before


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"},                                 # 看板页面
    {"Sec-Fetch-Site": "none"},                                        # 用户直接输入地址/书签
    {},                                                                # 桌面壳 / curl / 脚本：非浏览器
])
def test_same_origin_and_non_browser_writes_still_work(client, headers):
    c, eng = client
    assert pause(c, **headers).status_code == 200 and eng.paused is True
    assert c.post("/api/control", json={"action": "resume"}, headers=headers or None).status_code == 200
    assert eng.paused is False


def test_origin_matching_the_host_header_is_allowed(client):
    c, eng = client
    r = c.post("/api/control", json={"action": "pause"},
               headers={"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"})
    assert r.status_code == 200 and eng.paused is True


def test_dns_rebinding_cannot_forge_a_same_origin_write(client):
    """攻击者把自己域名解析到 127.0.0.1，受害者访问 http://evil.example:<端口>/ 时请求就落到看板上；
    此时 Origin 与 Host 都是 evil.example、Sec-Fetch-Site 是 same-origin，来源头那套判定全部通过。"""
    c, eng = client
    for headers in ({"Host": "evil.example:8765", "Origin": "http://evil.example:8765", "Sec-Fetch-Site": "same-origin"},
                    {"Host": "evil.example:8765", "Sec-Fetch-Site": "none"},
                    {"Host": "evil.example:8765"}):                      # 连来源头都不带的脚本同样挡住
        r = c.post("/api/control", json={"action": "pause"}, headers=headers)
        assert r.status_code == 403 and "Host" in r.json()["detail"]
        assert eng.paused is False, "被拒绝的请求不能产生副作用"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_loopback_hosts_are_allowed(client, host):
    c, eng = client
    assert c.post("/api/control", json={"action": "pause"},
                  headers={"Host": host, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert eng.paused is True
    c.post("/api/control", json={"action": "resume"}, headers={"Host": host})


def test_bound_and_configured_hosts_are_allowed_but_others_are_not(tmp_path):
    """绑定到 0.0.0.0 供局域网访问时，合法主机名只能由用户在 server.allowed_hosts 里列出。

    非回环绑定还要先过对端判定，所以这里一并打开 allow_remote_control——
    本用例考察的是 Host 判定，不是"要不要开放远程控制"。
    """
    cfg = make_cfg(tmp_path, host="0.0.0.0", allowed_hosts=["arena.lan"], allow_remote_control=True)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    c = TestClient(create_app(eng, ROOT / "web"), base_url="http://arena.lan")
    assert c.post("/api/control", json={"action": "pause"},
                  headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.post("/api/control", json={"action": "reset"},
                  headers={"Host": "evil.example", "Sec-Fetch-Site": "same-origin"}).status_code == 403
    assert eng.paused is True
    eng.stop()
    db.close()


def test_reads_are_not_affected(client):
    c, _ = client
    assert c.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    assert c.get("/api/state", headers={"Host": "evil.example"}).status_code == 200


def test_a_write_route_that_never_calls_the_check_is_still_protected(tmp_path):
    """保护必须按请求方法在中间件里统一生效：以后新增的写接口（例如触发付费研究的那种）
    不能因为作者忘了调用检查就默认失去保护。"""
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    app = create_app(eng, ROOT / "web")
    calls = []

    @app.post("/api/expensive")            # 新增写接口：正文里完全没有做任何来源校验
    def expensive():
        calls.append(1)
        return {"ok": True}

    c = TestClient(app, base_url=LOCAL)
    assert c.post("/api/expensive", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert not calls, "跨站请求不能进入接口正文"
    assert c.post("/api/expensive", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert len(calls) == 1
    eng.stop()
    db.close()


@pytest.mark.parametrize("bound, expected", [
    ("0.0.0.0", "127.0.0.1"), ("", "127.0.0.1"), ("::", "[::1]"),          # 通配绑定不能当访问地址用
    ("127.0.0.1", "127.0.0.1"), ("192.168.1.10", "192.168.1.10"), ("arena.lan", "arena.lan"),
])
def test_client_host_never_hands_out_a_wildcard_address(bound, expected):
    assert client_host(bound) == expected


def test_wildcard_bind_dashboard_url_can_still_control_the_competition(tmp_path):
    """绑定 0.0.0.0 时看板地址过去是 http://0.0.0.0:<端口>/：既连不上，也不是合法的看板 Host，
    控制请求会被 Host 校验拒成 403。窗口/浏览器拿到的地址必须是真的能用的那个。"""
    cfg = make_cfg(tmp_path, host="0.0.0.0", allowed_hosts=["arena.lan"])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    server, th, url = start_server(eng, cfg, ROOT / "web")
    try:
        assert "0.0.0.0" not in url and url.startswith("http://127.0.0.1:")
        r = httpx.post(url + "api/control", json={"action": "pause"}, headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200 and eng.paused is True
    finally:
        eng.stop()
        server.should_exit = True
        th.join(timeout=5)
        db.close()


def test_other_write_methods_are_covered(client):
    c, _ = client
    app = c.app

    @app.delete("/api/expensive-delete")
    def wipe():
        return {"ok": True}

    assert c.delete("/api/expensive-delete", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.delete("/api/expensive-delete", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


# ---------- 盘前研究：新增的付费写接口同样受中间件保护 ----------

@pytest.mark.parametrize("path", ["/api/control", "/api/debates/run", "/api/debates/1/retry", "/api/research-scan/run"])
def test_every_mutation_route_is_guarded_including_the_paid_one(client, path):
    """中间件按方法统一拦截，新增写接口不必各自记得调用检查。

    `/api/debates/run` 一次最多发起八次付费模型调用，是最不能漏掉保护的那种接口。
    """
    c, _ = client
    body = {"action": "pause"} if path == "/api/control" else {}
    assert c.post(path, json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post(path, json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post(path, json=body, headers={"Host": "evil.example",
                                            "Sec-Fetch-Site": "same-origin"}).status_code == 403


def test_a_remote_peer_cannot_mutate_unless_remote_control_is_enabled(tmp_path):
    """绑定到非回环地址时，未开放远程控制的局域网调用在中间件里就被挡下。"""
    cfg = make_cfg(tmp_path, host="0.0.0.0", allowed_hosts=["arena.lan"])
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        c = TestClient(create_app(eng, ROOT / "web"), base_url="http://arena.lan", client=("10.0.0.7", 1234))
        r = c.post("/api/control", json={"action": "pause"}, headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 403 and "remote control" in r.json()["detail"]
        assert eng.paused is False
        r = c.post("/api/debates/run", json={}, headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 403 and "remote control" in r.json()["detail"]
        r = c.post("/api/research-scan/run", json={}, headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 403 and "remote control" in r.json()["detail"]
        assert c.app.state.research_scan.public_status()["running"] is False
        # 回环对端不受影响
        local = TestClient(create_app(eng, ROOT / "web"), base_url="http://arena.lan", client=("127.0.0.1", 1234))
        assert local.post("/api/control", json={"action": "pause"},
                          headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert eng.paused is True
    finally:
        eng.stop()
        db.close()


def test_control_token_is_required_once_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("ARENA_CONTROL_TOKEN", raising=False)
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    eng = Engine(cfg, db, accelerate=True)
    try:
        c = TestClient(create_app(eng, ROOT / "web", control_token="s3cret"), base_url=LOCAL)
        assert pause(c).status_code == 403
        assert pause(c, **{"X-Control-Token": "wrong"}).status_code == 403
        assert eng.paused is False
        assert pause(c, **{"X-Control-Token": "s3cret"}).status_code == 200
        assert eng.paused is True
        # 只读接口不受影响
        assert c.get("/api/state").status_code == 200
    finally:
        eng.stop()
        db.close()
