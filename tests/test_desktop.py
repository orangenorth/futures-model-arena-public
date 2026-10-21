import sys
import types

import pytest

from arena import desktop


class _Db:
    def kv_set(self, *a):
        pass

    def close(self):
        pass


class _Engine:
    def __init__(self):
        self.db = _Db()
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True


class _Server:
    should_exit = False


class _Thread:
    def join(self, timeout=None):
        pass


def test_window_mode_falls_back_to_browser_when_webview_cannot_start(monkeypatch):
    fake_webview = types.ModuleType("webview")
    fake_webview.create_window = lambda *a, **k: None

    def _boom(**kwargs):
        raise RuntimeError("WebView2 runtime is not installed")

    fake_webview.start = _boom
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(desktop, "start_server", lambda *a: (_Server(), _Thread(), "http://127.0.0.1:9"))
    monkeypatch.setattr(desktop, "prevent_sleep", lambda enable: None)
    opened = []

    def _open(url):
        opened.append(url)
        raise KeyboardInterrupt  # end the browser-mode wait loop

    monkeypatch.setattr(desktop.webbrowser, "open", _open)
    monkeypatch.setattr(desktop.os, "_exit", lambda code: pytest.fail("unexpected forced exit"))
    cfg = types.SimpleNamespace(
        server=types.SimpleNamespace(prevent_sleep=False),
        competition=types.SimpleNamespace(name="arena"),
    )
    engine = _Engine()

    desktop.run_desktop(engine, cfg, web_dir=None, mode="window")

    assert opened == ["http://127.0.0.1:9"]
    assert engine.stopped
