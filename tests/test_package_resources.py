"""Verify the executable's data manifest without building an executable."""
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace

CONFIG_FILES = {
    "config.example.yaml", "demo.yaml", "contracts_cn.json",
    "contracts_universe_cn.json", "contracts_admission_ledger_cn.json",
    "admission_source_policy_cn.json",
}


def test_pyinstaller_manifest_contains_all_runtime_configuration(monkeypatch):
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = lambda name: []
    hooks.collect_data_files = lambda name: []
    hooks.collect_all = lambda name: ([], [], [])
    for name in ("PyInstaller", "PyInstaller.utils"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    manifest = {}

    def analysis(scripts, **kwargs):
        manifest.update(kwargs)
        return SimpleNamespace(pure=[], scripts=scripts, binaries=[], datas=kwargs["datas"])

    noop = lambda *args, **kwargs: None
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "arena.spec"), init_globals={
        "Analysis": analysis, "PYZ": noop, "EXE": noop, "COLLECT": noop,
    })
    bundled = {Path(src).name for src, dst in manifest["datas"] if dst == "config"}
    assert CONFIG_FILES <= bundled
    assert "config.yaml" not in bundled
