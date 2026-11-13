"""Exercise the real Windows build script with synthetic native-tool boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILES = (
    "config.example.yaml", "demo.yaml", "contracts_cn.json",
    "contracts_universe_cn.json", "contracts_admission_ledger_cn.json",
    "admission_source_policy_cn.json",
)
STAGES = ("version", "dependencies", "ctp_install", "ctp_check", "tests", "packager")
# Hosted Windows PowerShell startup exceeded the former 30s/20s budgets in CI.
# These are upper bounds, not delays: every wait returns as soon as it is ready.
NATIVE_TIMEOUT_SECONDS = 120
READY_TIMEOUT_SECONDS = 120
CLEANUP_TIMEOUT_SECONDS = 30
PACKAGER_HOLD_SECONDS = READY_TIMEOUT_SECONDS + NATIVE_TIMEOUT_SECONDS + CLEANUP_TIMEOUT_SECONDS
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell entrypoint")


def _wait_for_ready_marker(process, marker, timeout=READY_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while True:
        returncode = process.poll()
        if returncode is not None:
            raise AssertionError(f"Build exited with code {returncode} before the interruption boundary")
        if marker.is_file():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Synthetic packager did not reach its wait point within {timeout}s")
        time.sleep(min(0.05, remaining))


@pytest.fixture(params=("windows-powershell", "pwsh"))
def powershell(request):
    if request.param == "windows-powershell":
        candidate = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not candidate.is_file():
            pytest.skip("Windows PowerShell is unavailable")
        return str(candidate)
    candidate = shutil.which("pwsh")
    if candidate is None:
        pytest.skip("PowerShell 7 is unavailable")
    return candidate


class BuildFixture:
    def __init__(self, root, shell):
        self.root = root
        self.shell = shell
        root.mkdir()
        (root / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/build_exe.ps1", root / "scripts/build_exe.ps1")
        (root / "config").mkdir()
        for name in CONFIG_FILES:
            shutil.copyfile(ROOT / "config" / name, root / "config" / name)
        (root / "config/config.yaml").write_bytes(b"synthetic_private_configuration: never_package\n")
        (root / "requirements.txt").write_text("", encoding="ascii")
        (root / "arena.spec").write_text("# Synthetic native-tool input\n", encoding="ascii")
        stub = root / "fake_python.py"
        stub.write_text('''import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
if args[:1] == ["-3"]:
    args = args[1:]
if "-c" in args and "sys.version_info" in args[-1]:
    stage = "version"
elif "-c" in args and "openctp_ctp" in args[-1]:
    stage = "ctp_check"
elif args[:2] == ["-m", "pip"]:
    stage = "ctp_install" if any("openctp-ctp" in a for a in args) else "dependencies"
elif args[:2] == ["-m", "pytest"]:
    stage = "tests"
elif args[:2] == ["-m", "PyInstaller"]:
    stage = "packager"
else:
    raise SystemExit("Unexpected native-tool invocation")
with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"stage": stage, "args": args, "cwd": str(pathlib.Path.cwd())}) + "\\n")
if stage == "version":
    sys.exit(1 if os.environ.get("ARENA_TEST_UNSUPPORTED_PYTHON") else 0)
if pathlib.Path.cwd().resolve() != root.resolve():
    raise SystemExit(90)
if stage == "packager":
    out = pathlib.Path(args[args.index("--distpath") + 1])
    package = out / "FuturesArena"
    package.mkdir(parents=True, exist_ok=True)
    (package / "partial.bin").write_bytes(b"synthetic partial build")
    if os.environ.get("ARENA_TEST_HOLD_PACKAGER"):
        marker = root / "waiting.tmp"
        marker.write_text(json.dumps({"pid": os.getpid(), "output": str(out)}))
        marker.replace(root / "waiting.json")
        time.sleep(float(os.environ["ARENA_TEST_PACKAGER_HOLD_SECONDS"]))
    if os.environ.get("ARENA_TEST_FAIL_STAGE") == stage:
        raise SystemExit(9)
    if not os.environ.get("ARENA_TEST_MISSING_EXE"):
        (package / "FuturesArena.exe").write_bytes(b"synthetic executable")
    layout = os.environ.get("ARENA_TEST_CONFIG_LAYOUT", "internal")
    if layout == "flat":
        (package / "config").mkdir()
        for source in (root / "config").iterdir():
            if source.name != "config.yaml":
                (package / "config" / source.name).write_bytes(source.read_bytes())
    elif layout == "file":
        (package / "config").write_bytes(b"synthetic conflicting config file")
elif os.environ.get("ARENA_TEST_FAIL_STAGE") == stage:
    raise SystemExit(9)
''', encoding="utf-8")
        # Keep CMD bytes ASCII even when interpreter and fixture paths are not.
        wrapper = '@echo off\n"%ARENA_TEST_REAL_PYTHON%" -I -S -B "%ARENA_TEST_FAKE_SCRIPT%" %*\nexit /b %errorlevel%\n'
        for name in ("python.cmd", "py.cmd"):
            (root / name).write_text(wrapper, encoding="ascii")
        (root / "temp").mkdir()
        self.env = {
            name: os.environ[name] for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT")
            if name in os.environ
        }
        self.env.update({
            "PATH": str(root) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32"),
            "TEMP": str(root / "temp"), "TMP": str(root / "temp"),
            "ARENA_TEST_REAL_PYTHON": sys.executable,
            "ARENA_TEST_FAKE_SCRIPT": str(stub),
            "ARENA_TEST_PACKAGER_HOLD_SECONDS": str(PACKAGER_HOLD_SECONDS),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        })

    def command(self, output):
        command = [self.shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(self.root / "scripts/build_exe.ps1")]
        if output is not None:
            command.extend(("-OutputRoot", str(output)))
        return command

    def start(self, output=None, extra_env=None, cwd=None):
        return subprocess.Popen(
            self.command(output), cwd=cwd or self.root,
            env={**self.env, **(extra_env or {})}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def invoke(self, output=None, extra_env=None, cwd=None):
        process = self.start(output, extra_env, cwd)
        try:
            stdout, stderr = process.communicate(timeout=NATIVE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = self.stop(process)
            raise AssertionError(
                f"Build did not complete within {NATIVE_TIMEOUT_SECONDS}s; "
                + self.diagnostics(process, stdout, stderr)
            ) from error
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def wait_until_ready(self, process):
        try:
            _wait_for_ready_marker(process, self.root / "waiting.json")
        except (TimeoutError, AssertionError) as error:
            stdout, stderr = self.stop(process)
            raise AssertionError(f"{error}; " + self.diagnostics(process, stdout, stderr)) from error

    def diagnostics(self, process, stdout, stderr):
        try:
            stages = [call["stage"] for call in self.calls()]
        except (OSError, ValueError, KeyError) as error:
            stages = [f"Stage log unavailable: {error}"]
        return (f"observed stages={stages!r}; returncode={process.returncode}; "
                f"stdout={(stdout or b'')[-4000:]!r}; stderr={(stderr or b'')[-4000:]!r}")

    @staticmethod
    def stop(process):
        if process.poll() is None:
            # Kill only this test's owned process tree, including its waiting stub.
            subprocess.run(
                [str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                 "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=CLEANUP_TIMEOUT_SECONDS,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        return process.communicate(timeout=CLEANUP_TIMEOUT_SECONDS)

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.fixture
def make_build_fixture(tmp_path, powershell):
    return lambda name="repo": BuildFixture(tmp_path / name, powershell)


@pytest.fixture
def build_fixture(make_build_fixture):
    return make_build_fixture()


def _runtime_state(installation):
    result = {}
    for relative in ("data/arena.db", "config/config.yaml", "logs/arena.log"):
        path = installation / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = ("synthetic state must survive: " + relative).encode("ascii")
        path.write_bytes(payload)
        result[path] = payload
    return result


def _assert_state(snapshot):
    assert all(path.is_file() and path.read_bytes() == data for path, data in snapshot.items())


def _assert_public_config(fixture, output):
    config = output / "FuturesArena/config"
    assert {p.name for p in config.iterdir()} == set(CONFIG_FILES)
    for name in CONFIG_FILES:
        assert (config / name).read_bytes() == (fixture.root / "config" / name).read_bytes()
    assert not list(output.rglob("config.yaml")), "Private source configuration must never be copied"


@pytest.mark.parametrize("existing", ("empty", "nonempty", "file", "installation"))
def test_existing_output_is_refused_before_tools(build_fixture, existing):
    fixture = build_fixture
    output = fixture.root / "existing-output"
    if existing == "file":
        output.write_bytes(b"synthetic existing file")
        snapshot = {output: output.read_bytes()}
    else:
        output.mkdir()
        snapshot = {}
        if existing == "nonempty":
            (output / "keep.txt").write_bytes(b"synthetic existing content")
            snapshot = {output / "keep.txt": b"synthetic existing content"}
        elif existing == "installation":
            snapshot = _runtime_state(output / "FuturesArena")
    result = fixture.invoke(output)
    assert result.returncode != 0, result.stdout + result.stderr
    assert output.exists()
    _assert_state(snapshot)
    assert not fixture.calls(), "Refuse output reuse before selecting tools or installing dependencies"


def test_build_checks_the_public_ctp_import(build_fixture):
    fixture = build_fixture
    result = fixture.invoke(fixture.root / "new-build")
    assert result.returncode == 0, result.stdout + result.stderr
    checks = [call["args"][-1] for call in fixture.calls() if call["stage"] == "ctp_check"]
    assert len(checks) == 1
    assert "from openctp_ctp import mdapi" in checks[0]
    assert "GetApiVersion" in checks[0]


def test_build_scopes_pytest_to_a_relative_tests_directory(build_fixture):
    fixture = build_fixture
    result = fixture.invoke(fixture.root / "new-build")
    assert result.returncode == 0, result.stdout + result.stderr
    tests = [call["args"] for call in fixture.calls() if call["stage"] == "tests"]
    # An implicit absolute cwd containing brackets is parsed as a pytest node ID.
    assert tests == [["-m", "pytest", "tests/", "-q"]]


@pytest.mark.parametrize("layout", ("internal", "flat"))
def test_new_package_contains_every_public_configuration_file(build_fixture, layout):
    fixture = build_fixture
    output = fixture.root / "new-build"
    result = fixture.invoke(output, {"ARENA_TEST_CONFIG_LAYOUT": layout})
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_public_config(fixture, output)


def test_packager_config_file_is_rejected_without_overwrite(build_fixture):
    fixture = build_fixture
    output = fixture.root / "new-build"
    result = fixture.invoke(output, {"ARENA_TEST_CONFIG_LAYOUT": "file"})
    assert result.returncode != 0
    assert (output / "FuturesArena/config").read_bytes() == b"synthetic conflicting config file"


def test_unsupported_python_is_rejected_before_dependency_install(build_fixture):
    fixture = build_fixture
    result = fixture.invoke(fixture.root / "new-build", {"ARENA_TEST_UNSUPPORTED_PYTHON": "1"})
    assert result.returncode != 0
    assert fixture.calls() and all(call["stage"] == "version" for call in fixture.calls())


@pytest.mark.parametrize("sibling", (False, True), ids=("literal-only", "misleading-sibling"))
def test_bracket_source_path_is_literal(make_build_fixture, sibling):
    fixture = make_build_fixture("中文 空格 [1]")
    if sibling:
        other = fixture.root.parent / "中文 空格 1"
        (other / "scripts").mkdir(parents=True)
        (other / "config").mkdir()
        (other / "keep.txt").write_bytes(b"do not build the sibling")
        sibling_snapshot = {other / "keep.txt": b"do not build the sibling"}
    output = fixture.root / "新 输出 [2]"
    result = fixture.invoke(output)
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_public_config(fixture, output)
    assert all(Path(call["cwd"]).resolve() == fixture.root.resolve() for call in fixture.calls())
    if sibling:
        _assert_state(sibling_snapshot)
        assert not (other / "dist").exists()


def test_chinese_and_spaces_without_brackets(make_build_fixture):
    fixture = make_build_fixture("中文 空格 repo")
    output = fixture.root / "中文 输出 space"
    result = fixture.invoke(output)
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_public_config(fixture, output)


def test_default_repeated_builds_preserve_legacy_and_first_output(build_fixture):
    fixture = build_fixture
    snapshot = _runtime_state(fixture.root / "dist/FuturesArena")
    first = fixture.invoke()
    assert first.returncode == 0, first.stdout + first.stderr
    first_outputs = set((fixture.root / "dist").iterdir()) - {fixture.root / "dist/FuturesArena"}
    assert len(first_outputs) == 1
    first_output = first_outputs.pop()
    _assert_public_config(fixture, first_output)
    snapshot.update({path: path.read_bytes() for path in first_output.rglob("*") if path.is_file()})
    second = fixture.invoke()
    assert second.returncode == 0, second.stdout + second.stderr
    outputs = set((fixture.root / "dist").iterdir()) - {fixture.root / "dist/FuturesArena"}
    assert len(outputs) == 2 and first_output in outputs
    _assert_public_config(fixture, (outputs - {first_output}).pop())
    _assert_state(snapshot)
    calls_before = fixture.calls()
    rejected = fixture.invoke(first_output)
    assert rejected.returncode != 0
    assert fixture.calls() == calls_before
    _assert_state(snapshot)


@pytest.mark.parametrize("stage", STAGES[1:])
def test_each_failed_native_stage_preserves_previous_install(build_fixture, stage):
    fixture = build_fixture
    snapshot = _runtime_state(fixture.root / "dist/FuturesArena")
    output = fixture.root / "failed-output"
    result = fixture.invoke(output, {"ARENA_TEST_FAIL_STAGE": stage})
    assert result.returncode != 0
    assert [call["stage"] for call in fixture.calls()] == list(STAGES[:STAGES.index(stage) + 1])
    assert output.is_dir()
    _assert_state(snapshot)
    snapshot.update({path: path.read_bytes() for path in output.rglob("*") if path.is_file()})
    calls_before = fixture.calls()
    assert fixture.invoke(output).returncode != 0
    assert fixture.calls() == calls_before
    _assert_state(snapshot)


def test_missing_executable_fails_without_copying_config(build_fixture):
    fixture = build_fixture
    snapshot = _runtime_state(fixture.root / "dist/FuturesArena")
    output = fixture.root / "missing-executable"
    result = fixture.invoke(output, {"ARENA_TEST_MISSING_EXE": "1"})
    assert result.returncode != 0
    assert (output / "FuturesArena/partial.bin").read_bytes() == b"synthetic partial build"
    assert not (output / "FuturesArena/config").exists()
    _assert_state(snapshot)


def test_interruption_preserves_partial_and_retry_uses_fresh_output(build_fixture):
    fixture = build_fixture
    snapshot = _runtime_state(fixture.root / "dist/FuturesArena")
    output = fixture.root / "interrupted-output"
    process = fixture.start(output, {"ARENA_TEST_HOLD_PACKAGER": "1"})
    try:
        fixture.wait_until_ready(process)
    finally:
        fixture.stop(process)
    assert process.returncode != 0
    partial = output / "FuturesArena/partial.bin"
    snapshot[partial] = b"synthetic partial build"
    _assert_state(snapshot)
    calls_before = fixture.calls()
    assert fixture.invoke(output).returncode != 0
    assert fixture.calls() == calls_before
    retried = fixture.invoke(fixture.root / "retry-fresh-output")
    assert retried.returncode == 0, retried.stdout + retried.stderr
    _assert_public_config(fixture, fixture.root / "retry-fresh-output")
    _assert_state(snapshot)


def test_relative_output_keeps_caller_directory_convention(build_fixture):
    fixture = build_fixture
    result = fixture.invoke("relative output", cwd=fixture.root.parent)
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_public_config(fixture, fixture.root.parent / "relative output")
    assert not (fixture.root / "relative output").exists()


def test_native_harness_budgets_cover_observed_ci_startup_delay():
    assert NATIVE_TIMEOUT_SECONDS == READY_TIMEOUT_SECONDS == 120
    assert PACKAGER_HOLD_SECONDS > READY_TIMEOUT_SECONDS + CLEANUP_TIMEOUT_SECONDS


def test_ready_marker_returns_immediately_without_sleep(tmp_path, monkeypatch):
    marker = tmp_path / "waiting.json"
    marker.write_text("{}", encoding="ascii")
    process = type("SyntheticProcess", (), {"poll": lambda self: None})()
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("Ready marker must not add a delay"))
    _wait_for_ready_marker(process, marker, timeout=0.1)


def test_ready_marker_waits_for_observation_with_a_bounded_clock(tmp_path, monkeypatch):
    marker = tmp_path / "waiting.json"
    process = type("SyntheticProcess", (), {"poll": lambda self: None})()
    clock = {"now": 0.0, "polls": 0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def advance(interval):
        clock["now"] += interval
        clock["polls"] += 1
        if clock["polls"] == 2:
            marker.write_text("{}", encoding="ascii")

    monkeypatch.setattr(time, "sleep", advance)
    _wait_for_ready_marker(process, marker, timeout=0.2)
    assert clock["polls"] == 2 and clock["now"] < 0.2


def test_ready_marker_timeout_is_bounded_without_real_waiting(tmp_path, monkeypatch):
    process = type("SyntheticProcess", (), {"poll": lambda self: None})()
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", lambda interval: clock.update(now=clock["now"] + interval))
    with pytest.raises(TimeoutError, match="did not reach"):
        _wait_for_ready_marker(process, tmp_path / "missing.json", timeout=0.1)
    assert clock["now"] <= 0.15


def test_exited_process_does_not_accept_a_stale_ready_marker(tmp_path, monkeypatch):
    marker = tmp_path / "waiting.json"
    marker.write_text("{}", encoding="ascii")
    process = type("SyntheticProcess", (), {"poll": lambda self: 17})()
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("Exited process must fail immediately"))
    with pytest.raises(AssertionError, match="exited.*17"):
        _wait_for_ready_marker(process, marker, timeout=0.1)


@pytest.mark.parametrize("boundary", ("completion", "readiness"))
def test_timeout_reports_stages_and_output_after_owned_cleanup(tmp_path, monkeypatch, boundary):
    fixture = object.__new__(BuildFixture)
    fixture.root = tmp_path
    fixture.shell = "synthetic-shell"
    cleaned = []

    class SyntheticProcess:
        args = ["synthetic-build"]
        returncode = None

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)

    process = SyntheticProcess()

    def stop(owned):
        assert owned is process
        cleaned.append(owned)
        owned.returncode = -9
        return b"captured standard output", b"captured standard error"

    monkeypatch.setattr(fixture, "start", lambda *args: process)
    monkeypatch.setattr(fixture, "stop", stop)
    monkeypatch.setattr(fixture, "calls", lambda: [{"stage": "version"}, {"stage": "dependencies"}])
    if boundary == "readiness":
        def timeout(*args, **kwargs):
            raise TimeoutError("Synthetic packager did not reach its wait point")
        monkeypatch.setitem(globals(), "_wait_for_ready_marker", timeout)
    with pytest.raises(AssertionError) as error:
        fixture.invoke() if boundary == "completion" else fixture.wait_until_ready(process)
    assert cleaned == [process]
    for expected in ("version", "dependencies", "captured standard output", "captured standard error"):
        assert expected in str(error.value)
