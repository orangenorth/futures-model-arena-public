"""桌面壳：后台线程跑 uvicorn，主线程开 pywebview 窗口（或系统浏览器 / 无界面）。"""
from __future__ import annotations

import hashlib
import logging
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import uvicorn

from .config import AppConfig
from .engine.loop import Engine
from .server.api import create_app, host_name

log = logging.getLogger("arena.desktop")


def client_host(server_host: str) -> str:
    """看板窗口/浏览器该访问的主机名——绑定地址不一定能当访问地址用。

    绑定到通配地址时 `http://0.0.0.0:<端口>/` 连不上（各平台行为还不一致），而且它不是合法的看板 Host，
    写接口会按 DNS 重绑定防护把控制请求拒掉。本机访问一律走回环，回环永远在允许集合里。
    """
    raw = (server_host or "").strip().strip("[]").lower()
    if raw in ("", "0.0.0.0"):
        return "127.0.0.1"
    if raw == "::":
        return "[::1]"
    name = host_name(server_host)
    return f"[{name}]" if ":" in name else name


def free_port(host: str, preferred: int = 0) -> int:
    if preferred:
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def acquire_instance_lock(db_path: Path) -> Optional[socket.socket]:
    """用一个由数据库路径派生的本地端口作为跨平台单实例锁；进程退出/崩溃时自动释放。返回 None 表示已有实例在运行。"""
    h = int(hashlib.sha1(str(db_path.resolve()).encode("utf-8")).hexdigest(), 16)
    port = 49152 + (h % 16000)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


class Server(uvicorn.Server):
    def install_signal_handlers(self):  # 在子线程运行时不装信号处理器
        pass


def start_server(engine: Engine, cfg: AppConfig, web_dir: Path) -> tuple[Server, threading.Thread, str]:
    port = free_port(cfg.server.host, cfg.server.port)
    app = create_app(engine, web_dir)
    config = uvicorn.Config(app, host=cfg.server.host, port=port, log_level="warning", access_log=False)
    server = Server(config)
    th = threading.Thread(target=server.run, name="arena-uvicorn", daemon=True)
    th.start()
    url = f"http://{client_host(cfg.server.host)}:{port}/"
    for _ in range(200):
        if server.started or not th.is_alive():
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError(f"API 服务启动失败（端口 {port} 可能被占用），请更换 server.port 或 --port")
    return server, th, url


def prevent_sleep(enable: bool):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0))
    except Exception as e:  # noqa: BLE001
        log.warning("设置防休眠失败: %s", e)


def run_desktop(engine: Engine, cfg: AppConfig, web_dir: Path, mode: str = "window", debug: bool = False) -> None:
    """mode: window | browser | headless"""
    server, th, url = start_server(engine, cfg, web_dir)
    engine.db.kv_set("server_url", url)
    engine.start()
    if cfg.server.prevent_sleep:
        prevent_sleep(True)
    print(f"看板地址: {url}", flush=True)
    log.info("看板地址 %s", url)
    stop_event = threading.Event()

    def _on_signal(signum, frame):  # noqa: ANN001
        log.info("收到信号 %s，准备退出", signum)
        stop_event.set()

    try:
        if mode == "window":
            try:
                import webview  # type: ignore
            except ImportError:
                log.warning("未安装 pywebview，改用系统浏览器打开")
                mode = "browser"
        if mode == "window":
            import webview  # type: ignore

            webview.create_window(cfg.competition.name, url, width=1440, height=920, min_size=(1100, 700), confirm_close=True)
            gui = "edgechromium" if sys.platform == "win32" else None
            try:
                webview.start(gui=gui, debug=debug, private_mode=False)
            except Exception as e:  # noqa: BLE001
                # 常见于缺少 WebView2 运行时或 pythonnet 加载失败；引擎已在跑，改用浏览器而不是整体退出
                log.warning("桌面窗口启动失败（%s），改用系统浏览器打开", e)
                mode = "browser"
        if mode != "window":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    signal.signal(sig, _on_signal)
                except (ValueError, OSError):
                    pass
            if mode == "browser":
                webbrowser.open(url)
            while not stop_event.is_set():
                stop_event.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("正在停止…")
        engine.stop()
        server.should_exit = True
        th.join(timeout=5)
        prevent_sleep(False)
        engine.db.close()
        # 线程池里可能仍有阻塞在网络请求上的模型调用（非守护线程），会让进程在退出时等待到超时；
        # 数据已全部落库，此时直接结束进程是安全的
        lingering = [t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon and t.is_alive()]
        if lingering:
            log.info("仍有 %d 个后台请求线程未结束，强制退出进程", len(lingering))
            logging.shutdown()
            os._exit(0)


def open_existing_instance(db_path: Path, mode: str) -> None:
    """检测到已有实例时：读取其看板地址并用浏览器打开。"""
    from .db import Database

    url = None
    try:
        db = Database(db_path)
        url = db.kv_get("server_url")
        db.close()
    except Exception:  # noqa: BLE001
        pass
    msg = f"检测到已有实例正在运行（数据库 {db_path}）。" + (f" 看板地址：{url}" if url else "")
    print(msg, flush=True)
    log.warning(msg)
    if url and mode in ("window", "browser"):
        webbrowser.open(url)
