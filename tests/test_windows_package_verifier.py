"""Safety and manifest tests; no built executable or external service is run."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("windows_package_verifier", ROOT / "scripts/verify_windows_package.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


@pytest.fixture
def package_fixture(tmp_path):
    package = tmp_path / "built package"
    reference = tmp_path / "reference config"
    acceptance = tmp_path / "new acceptance"
    package.mkdir()
    reference.mkdir()
    (package / "FuturesArena.exe").write_bytes(b"synthetic executable; never run")
    for folder in (package / "config", package / "_internal/config"):
        folder.mkdir(parents=True)
    for name in verifier.PUBLIC_CONFIGS:
        content = (json.dumps({"fixture": name}) + "\n").encode()
        for folder in (reference, package / "config", package / "_internal/config"):
            (folder / name).write_bytes(content)
    return package, acceptance, reference


def test_manifest_requires_exact_six_matching_public_resources(package_fixture):
    package, _, reference = package_fixture
    result = verifier.inspect_package(package, reference)
    assert result["layout"] == "internal"
    assert set(result["config_hashes"]) == set(verifier.PUBLIC_CONFIGS)
    assert len(result["file_hashes"]) == 13


def test_flat_layout_supported_without_distinct_internal_tree(package_fixture):
    package, _, reference = package_fixture
    # Rename a synthetic fixture tree; never delete a package supplied by a user.
    (package / "_internal").rename(package.parent / "unused synthetic internal")
    result = verifier.inspect_package(package, reference)
    assert result["layout"] == "flat"
    assert len(result["file_hashes"]) == 7


@pytest.mark.parametrize("location", ["config", "_internal/config"])
def test_missing_or_changed_config_is_rejected(package_fixture, location):
    package, _, reference = package_fixture
    (package / location / "demo.yaml").write_text("changed: true\n", encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="hashes differ"):
        verifier.inspect_package(package, reference)
    (package / location / "demo.yaml").rename(package / location / "not-demo.yaml")
    with pytest.raises(verifier.VerificationError, match="missing"):
        verifier.inspect_package(package, reference)


@pytest.mark.parametrize("relative", ["config/config.yaml", "data/state.db", "logs/output.log", ".env", "credentials.json"])
def test_private_or_runtime_payload_is_refused(package_fixture, relative):
    package, _, reference = package_fixture
    target = package / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SYNTHETIC_PRIVATE_SENTINEL", encoding="utf-8")
    with pytest.raises(verifier.VerificationError):
        verifier.inspect_package(package, reference)
    assert target.read_text() == "SYNTHETIC_PRIVATE_SENTINEL"


def test_credential_like_payload_rejected_without_echoing_value(package_fixture):
    package, _, reference = package_fixture
    sentinel = "sk-" + "z" * 32
    (package / "extra.json").write_text(json.dumps({"api_key": sentinel}), encoding="utf-8")
    with pytest.raises(verifier.VerificationError) as error:
        verifier.inspect_package(package, reference)
    assert sentinel not in str(error.value)
    assert "value suppressed" in str(error.value)


def test_existing_acceptance_root_is_never_reused(package_fixture):
    package, acceptance, reference = package_fixture
    acceptance.mkdir()
    sentinel = acceptance / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="already exists"):
        verifier.validate_inputs(package, acceptance, reference)
    assert sentinel.read_text() == "preserve"


@pytest.mark.parametrize("kind", ["inside_package", "inside_reference", "same_reference", "relative"])
def test_unsafe_destinations_are_refused(package_fixture, kind):
    package, acceptance, reference = package_fixture
    if kind == "inside_package":
        acceptance = package / "acceptance"
    elif kind == "inside_reference":
        acceptance = reference / "acceptance"
    elif kind == "same_reference":
        reference = package
    else:
        acceptance = Path("relative-acceptance")
    with pytest.raises(verifier.VerificationError):
        verifier.validate_inputs(package, acceptance, reference)
    assert not os.path.lexists(acceptance) or acceptance == package


def test_linked_package_entry_is_refused_without_traversal(package_fixture, monkeypatch):
    package, _, _ = package_fixture
    original = verifier.is_link
    monkeypatch.setattr(verifier, "is_link", lambda path: path.name == "config" or original(path))
    with pytest.raises(verifier.VerificationError, match="link or reparse"):
        verifier.file_inventory(package)


def test_preparing_cases_only_changes_copies_and_all_candidates_are_offline(package_fixture):
    package, acceptance, reference = package_fixture
    baseline = verifier.inspect_package(package, reference)
    acceptance.mkdir()
    for case in ("external", "explicit", "bundled"):
        copied, _, expected, config_arg = verifier.prepare_case(package, acceptance, case, baseline)
        candidates = list((copied / "config").glob("*.yaml")) + list((copied / "_internal/config").glob("*.yaml"))
        for path in candidates:
            if path.name == "demo.yaml":
                continue
            cfg = json.loads(path.read_text())
            assert cfg["competition"]["market"] == "replay"
            assert all(trader["provider"] == "rule" for trader in cfg["traders"])
            assert cfg["server"]["host"] == "127.0.0.1"
        selected = copied / config_arg if config_arg else copied / ("config/config.yaml" if case == "external" else "_internal/config/config.example.yaml")
        assert json.loads(selected.read_text())["competition"]["name"] == expected
    assert verifier.file_inventory(package) == baseline["file_hashes"]


def test_copy_preserves_then_rejects_a_link_before_configuration_writes(package_fixture, monkeypatch):
    package, acceptance, reference = package_fixture
    baseline = verifier.inspect_package(package, reference)
    acceptance.mkdir()

    def simulate_racing_link(source, destination, *, symlinks):
        assert source == package and symlinks is True
        destination.mkdir()
        (destination / "unexpected-link").write_text("synthetic", encoding="utf-8")

    original = verifier.is_link
    monkeypatch.setattr(verifier.shutil, "copytree", simulate_racing_link)
    monkeypatch.setattr(verifier, "is_link", lambda path: path.name == "unexpected-link" or original(path))
    with pytest.raises(verifier.VerificationError, match="link or reparse"):
        verifier.prepare_case(package, acceptance, "external", baseline)
    assert not (acceptance / "cases/external/package/config").exists()


@pytest.mark.parametrize("connect_result, expected", [(10061, True), (0, False)])
def test_post_exit_listener_check_is_loopback_only(monkeypatch, connect_result, expected):
    calls = []

    class ProbeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            assert value <= 0.2

        def connect_ex(self, address):
            calls.append(address)
            return connect_result

    monkeypatch.setattr(verifier.socket, "socket", ProbeSocket)
    assert verifier.wait_for_port_closed(12345, timeout=0) is expected
    assert calls == [("127.0.0.1", 12345)]


def test_child_environment_drops_credentials_and_injection_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", str(tmp_path / "windows"))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("CTP_PASSWORD", "synthetic-secret")
    monkeypatch.setenv("PYTHONPATH", "synthetic-injected-path")
    monkeypatch.setenv("ARENA_CONTROL_TOKEN", "synthetic-control-token")
    environment = verifier.isolated_environment(tmp_path / "environment")
    for name in ("OPENAI_API_KEY", "CTP_PASSWORD", "PYTHONPATH", "ARENA_CONTROL_TOKEN"):
        assert name not in environment
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert Path(environment["TEMP"]).is_relative_to(tmp_path)


def test_synthetic_database_integrity_and_sentinel_are_checked(tmp_path):
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE competitions (id, name, market, started_at)")
        connection.execute("INSERT INTO competitions VALUES (1, 'SYNTHETIC', 'replay', 123)")
        connection.execute("CREATE TABLE decisions (id)")
        connection.execute("INSERT INTO decisions VALUES (1)")
    before = verifier.inspect_database(path, "synthetic-marker", create_sentinel=True)
    assert verifier.inspect_database(path, "synthetic-marker") == before
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE acceptance_sentinel SET value='changed'")
    with pytest.raises(verifier.VerificationError, match="sentinel changed"):
        verifier.inspect_database(path, "synthetic-marker")


class OwnedProcess:
    pid = 12345

    def __init__(self, graceful):
        self.returncode = None
        self.graceful = graceful
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.graceful:
            self.returncode = 0
        elif self.terminated:
            self.returncode = 1
        else:
            raise subprocess.TimeoutExpired("synthetic-owned-process", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def test_graceful_helper_targets_only_owned_console_and_stripped_env(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    environment = {"SystemRoot": "SYNTHETIC"}
    process = OwnedProcess(graceful=True)
    result = verifier.stop_owned_process(process, environment)
    assert result["graceful"] and not process.terminated
    assert calls[0][0][-1] == str(process.pid)
    assert calls[0][1]["env"] is environment
    assert "AttachConsole(int(sys.argv[1]))" in calls[0][0][-2]
    assert "SetConsoleCtrlHandler(None, True)" in calls[0][0][-2]


def test_helper_failure_forces_only_owned_process_and_does_not_claim_graceful(monkeypatch):
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    def failed_helper(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic-helper", 5)

    monkeypatch.setattr(subprocess, "run", failed_helper)
    process = OwnedProcess(graceful=False)
    result = verifier.stop_owned_process(process, {})
    assert process.terminated
    assert result["forced_termination"] and not result["graceful"]
    assert result["ctrl_c_helper_error"] == "TimeoutExpired"


def test_non_windows_real_verification_is_refused_before_writes(package_fixture, monkeypatch):
    package, acceptance, reference = package_fixture
    monkeypatch.setattr(verifier.sys, "platform", "linux")
    with pytest.raises(verifier.VerificationError, match="requires Windows"):
        verifier.verify(package, acceptance, reference)
    assert not acceptance.exists()
