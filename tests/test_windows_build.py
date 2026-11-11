"""Execute the Windows packaging entrypoint against isolated tool boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILES = (
    "config.example.yaml", "demo.yaml", "contracts_cn.json",
    "contracts_universe_cn.json", "contracts_admission_ledger_cn.json",
    "admission_source_policy_cn.json",
)
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell entrypoint")


@pytest.fixture
def build_fixture(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/build_exe.ps1", tmp_path / "scripts/build_exe.ps1")
    (tmp_path / "config").mkdir()
    for name in CONFIG_FILES:
        shutil.copyfile(ROOT / "config" / name, tmp_path / "config" / name)
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    (tmp_path / "arena.spec").write_text("# Isolated fake build input\n", encoding="utf-8")
    # Replace only the external Python tool boundary; run the actual PowerShell script.
    stub = tmp_path / "fake_python.py"
    stub.write_text('''import json, pathlib, sys
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if "-c" in args and "sys.version_info" in args[-1]:
    import os
    sys.exit(1 if os.environ.get("FAKE_UNSUPPORTED_PYTHON") else 0)
if "PyInstaller" in args:
    out = pathlib.Path(args[args.index("--distpath") + 1]) if "--distpath" in args else root / "dist"
    package = out / "FuturesArena"
    package.mkdir(parents=True, exist_ok=True)
    (package / "FuturesArena.exe").write_bytes(b"synthetic executable")
''', encoding="utf-8")
    (tmp_path / "python.cmd").write_text(
        f'@echo off\n"{sys.executable}" "{stub}" %*\nexit /b %errorlevel%\n', encoding="utf-8"
    )
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32")

    def invoke(output, extra_env=None):
        return subprocess.run(
            [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
             "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(tmp_path / "scripts/build_exe.ps1"), "-OutputRoot", str(output)],
            cwd=tmp_path, env={**env, **(extra_env or {})}, capture_output=True, timeout=30,
        )
    return tmp_path, invoke


def test_rebuild_refuses_existing_output_and_preserves_runtime_state(build_fixture):
    root, invoke = build_fixture
    output = root / "dist"
    sentinels = [output / "FuturesArena" / p for p in
                 ("data/arena.db", "config/config.yaml", "logs/arena.log")]
    for path in sentinels:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic state must survive")
    result = invoke(output)
    assert result.returncode != 0, result.stdout + result.stderr
    assert all(p.read_bytes() == b"synthetic state must survive" for p in sentinels)
    assert not (root / "calls.jsonl").exists(), "Refuse output reuse before installing dependencies"


def test_build_checks_the_public_ctp_import(build_fixture):
    root, invoke = build_fixture
    result = invoke(root / "new-build")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    checks = [call[1] for call in calls if call[0] == "-c" and "openctp_ctp" in call[1]]
    assert len(checks) == 1
    assert "from openctp_ctp import mdapi" in checks[0]
    assert "GetApiVersion" in checks[0]


def test_new_package_contains_every_public_configuration_file(build_fixture):
    root, invoke = build_fixture
    output = root / "new-build"
    result = invoke(output)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in CONFIG_FILES:
        packaged = output / "FuturesArena/config" / name
        assert packaged.is_file(), name
        assert packaged.read_bytes() == (root / "config" / name).read_bytes()


def test_unsupported_python_is_rejected_before_dependency_install(build_fixture):
    root, invoke = build_fixture
    result = invoke(root / "new-build", {"FAKE_UNSUPPORTED_PYTHON": "1"})
    assert result.returncode != 0
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    assert calls and all(call[0] == "-c" for call in calls)
