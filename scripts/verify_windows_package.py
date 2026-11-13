"""Accept a Windows onedir package using fresh copies and synthetic offline state.

This tool never executes the supplied package in place. It does not certify GUI,
CTP connectivity, arbitrary native imports, or an upgrade of a real installation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid


PUBLIC_CONFIGS = (
    "config.example.yaml", "demo.yaml", "contracts_cn.json",
    "contracts_universe_cn.json", "contracts_admission_ledger_cn.json",
    "admission_source_policy_cn.json",
)
FORBIDDEN_DIRS = {"data", "logs", ".git", ".venv"}
FORBIDDEN_FILES = {"config.yaml", ".env", "auth.json", "credentials.json"}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16})"),
    re.compile(rb"sk-(?:proj-|svcacct-|sp-)?[A-Za-z0-9_-]{24,}"),
)
LIMITATIONS = [
    "No real installation, account, database, migration, or release was tested.",
    "Runtime checks use copies whose candidate templates are replaced with synthetic replay/rule configuration.",
    "Untouched default live-market startup is not executed.",
    "Headless startup does not certify the GUI, WebView2, CTP login, or every frozen native dependency.",
    "Credential-pattern scanning is heuristic, not a complete secrets or binary-history audit.",
    "Offline configuration and a stripped environment are used; this tool does not install an OS network filter.",
    "Persistence checks cover competition identity, nondecreasing decision count, a synthetic sentinel, and SQLite integrity; not exact balances, positions, replay cursor, or persisted pause.",
]


class VerificationError(RuntimeError):
    """A bounded package acceptance check failed."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def reject_linked_ancestors(path: Path) -> None:
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate) and is_link(candidate):
            raise VerificationError("Linked or reparse-point paths are not accepted.")


def overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def validate_inputs(package: Path, acceptance: Path, reference: Path) -> tuple[Path, Path, Path]:
    for path in (package, acceptance, reference):
        if not path.is_absolute():
            raise VerificationError("Package, acceptance root, and reference config must be absolute paths.")
        reject_linked_ancestors(path)
    package, acceptance, reference = (path.resolve() for path in (package, acceptance, reference))
    if not package.is_dir() or not reference.is_dir():
        raise VerificationError("Package and reference configuration directories must exist.")
    if os.path.lexists(acceptance):
        raise VerificationError("Acceptance root already exists; choose a new root. Nothing was changed.")
    if not acceptance.parent.is_dir():
        raise VerificationError("Acceptance root must have an existing parent directory.")
    if overlap(package, acceptance) or overlap(reference, acceptance) or overlap(package, reference):
        raise VerificationError("Package, acceptance root, and reference configuration must not overlap.")
    return package, acceptance, reference


def file_inventory(root: Path, *, hygiene: bool = True) -> dict[str, str]:
    """Hash ordinary files only; never traverse a link or reparse point."""
    if is_link(root):
        raise VerificationError("The package root must not be a link.")
    result = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            if is_link(current_path / name):
                raise VerificationError("Package contains a link or reparse point.")
        if hygiene and any(name.casefold() in FORBIDDEN_DIRS for name in directories):
            raise VerificationError("Package contains a runtime or development directory.")
        for name in files:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            lower = name.casefold()
            if hygiene and (
                lower in FORBIDDEN_FILES or lower.startswith(".env.")
                or lower.endswith((".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".log"))
            ):
                raise VerificationError(f"Forbidden runtime or credential filename: {relative}")
            if hygiene and path.suffix.casefold() in {".yaml", ".yml", ".json", ".txt", ".ini", ".pem", ".py", ".js", ".html"}:
                # Scan all bytes in bounded chunks, retaining overlap for tokens.
                with path.open("rb") as stream:
                    tail = b""
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        sample = tail + chunk
                        if any(pattern.search(sample) for pattern in SECRET_PATTERNS):
                            raise VerificationError(f"Credential-like payload found; value suppressed: {relative}")
                        tail = sample[-512:]
            result[relative] = sha256(path)
    return result


def inspect_package(package: Path, reference: Path) -> dict:
    inventory = file_inventory(package)
    if "FuturesArena.exe" not in inventory:
        raise VerificationError("FuturesArena.exe is missing from the package root.")
    internal = package / "_internal" if (package / "_internal").is_dir() else package
    checks = {}
    for name in PUBLIC_CONFIGS:
        source = reference / name
        external = package / "config" / name
        bundled = internal / "config" / name
        if not all(path.is_file() and not is_link(path) for path in (source, external, bundled)):
            raise VerificationError(f"Public configuration is missing or linked: {name}")
        values = [sha256(path) for path in (source, external, bundled)]
        if len(set(values)) != 1:
            raise VerificationError(f"Public configuration hashes differ: {name}")
        checks[name] = values[0]
    # The exact references are expected to be checked-out public templates.
    # Validate literal credential fields, not environment-variable names.
    import yaml
    for name in ("config.example.yaml", "demo.yaml"):
        document = yaml.safe_load((reference / name).read_text(encoding="utf-8")) or {}
        for trader in document.get("traders", []):
            if trader.get("api_key"):
                raise VerificationError("A public template contains a literal API key; value suppressed.")
            env_name = trader.get("api_key_env", "")
            if env_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
                raise VerificationError("A public template has a credential-like environment reference.")
        ctp = (document.get("market_data") or {}).get("ctp") or {}
        if any(ctp.get(key) for key in ("password", "user_id", "broker_id", "md_front")):
            raise VerificationError("A public template contains CTP account configuration; values suppressed.")
    native = [name for name in inventory if name.casefold().endswith((".dll", ".pyd"))]
    return {"layout": "internal" if internal != package else "flat", "config_hashes": checks,
            "file_hashes": inventory, "native_file_count": len(native),
            "ctp_native_files": [name for name in native if "ctp" in name.casefold()],
            "webview_files": [name for name in inventory if "webview" in name.casefold()]}


def synthetic_config(name: str) -> dict:
    return {
        "competition": {"name": name, "market": "replay", "initial_cash": 100000,
                        "tick_interval_sec": 1, "decision_interval_sec": 3, "snapshot_interval_sec": 2},
        "symbols": ["RB0"],
        "market_data": {"cn_futures": {"contracts_file": "config/contracts_cn.json"},
                        "replay": {"seed": 1729, "bar_seconds": 30, "history": 300,
                                   "start_price": {"RB0": 3400}}},
        "traders": [{"id": "acceptance-rule", "name": "Synthetic rule seat", "provider": "rule",
                     "fallback_rule": "conservative", "api_key": "", "api_key_env": ""}],
        "preopen_debate": {"enabled": False}, "product_research": {"enabled": False},
        "storage": {"db_path": "data/acceptance.db", "log_dir": "logs"},
        "server": {"host": "127.0.0.1", "port": 0, "prevent_sleep": False, "allow_remote_control": False},
    }


def write_synthetic(path: Path, name: str) -> None:
    # JSON is valid YAML. All content is generated here, never copied from a user.
    path.write_text(json.dumps(synthetic_config(name), indent=2) + "\n", encoding="utf-8")


def isolated_environment(root: Path) -> dict[str, str]:
    """Allowlist Windows essentials; do not inherit provider credentials or Python hooks."""
    system = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
    if not system:
        raise VerificationError("SystemRoot is required on Windows.")
    for child in ("home", "temp", "appdata", "localappdata"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return {"SystemRoot": system, "WINDIR": system,
            "PATH": str(Path(system) / "System32"), "COMSPEC": str(Path(system) / "System32/cmd.exe"),
            "TEMP": str(root / "temp"), "TMP": str(root / "temp"), "USERPROFILE": str(root / "home"),
            "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "localappdata"),
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost,::1"}


def request_json(port: int, path: str, body: dict | None = None) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        payload = json.dumps(body) if body is not None else None
        connection.request("POST" if body is not None else "GET", path, payload,
                           {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
        response = connection.getresponse()
        if response.status != 200:
            raise VerificationError(f"Local API returned HTTP {response.status}.")
        return json.loads(response.read())
    finally:
        connection.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port_closed(port: int, timeout: float = 3) -> bool:
    """Check only the selected loopback port; never kill an unexpected listener."""
    deadline = time.monotonic() + timeout
    while True:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


CTRL_C_HELPER = r'''
import ctypes, sys, time
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.FreeConsole()
if not kernel.AttachConsole(int(sys.argv[1])):
    sys.exit(11)
if not kernel.SetConsoleCtrlHandler(None, True):
    sys.exit(12)
if not kernel.GenerateConsoleCtrlEvent(0, 0):
    sys.exit(13)
time.sleep(0.25)
kernel.FreeConsole()
'''


def stop_owned_process(process: subprocess.Popen, environment: dict[str, str], timeout: float = 20) -> dict:
    """Signal only the fresh console owned by this Popen; never the caller's console."""
    helper_code = None
    forced = False
    helper_error = None
    if process.poll() is None:
        try:
            helper = subprocess.run(
                [sys.executable, "-I", "-c", CTRL_C_HELPER, str(process.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False, env=environment,
            )
            helper_code = helper.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            helper_error = type(exc).__name__
        try:
            process.wait(timeout=timeout if helper_code == 0 else 1)
        except subprocess.TimeoutExpired:
            forced = True
            process.terminate()  # Exact owned process handle, never a name or process tree.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return {"exit_code": process.returncode, "ctrl_c_helper_exit_code": helper_code,
            "ctrl_c_helper_error": helper_error,
            "forced_termination": forced,
            "graceful": helper_code == 0 and process.returncode == 0 and not forced}


def run_once(package: Path, case_root: Path, expected_name: str, config_arg: str | None,
             label: str, timeout: float) -> dict:
    port = free_port()
    args = [str(package / "FuturesArena.exe"), "--headless", "--accelerate", "--port", str(port)]
    if config_arg:
        args += ["--config", config_arg]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # A separate hidden console makes Ctrl+C narrowly scoped.
    result = {}
    environment = isolated_environment(case_root / "environment")
    with (case_root / f"{label}.stdout.log").open("wb") as stdout, (case_root / f"{label}.stderr.log").open("wb") as stderr:
        process = subprocess.Popen(args, cwd=package, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE)
        try:
            deadline = time.monotonic() + timeout
            state = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise VerificationError(f"Owned package exited before acceptance, code {process.returncode}.")
                try:
                    health = request_json(port, "/api/health")
                    candidate = request_json(port, "/api/state")
                    if candidate.get("competition", {}).get("name") != expected_name:
                        raise VerificationError("Local API identity does not match this synthetic case.")
                    if candidate["competition"]["market"] != "replay" or not candidate.get("traders") or any(t.get("mode") != "rule" for t in candidate["traders"]):
                        raise VerificationError("The synthetic run was not replay/rule-only.")
                    if candidate["status"]["paused"]:
                        request_json(port, "/api/control", {"action": "resume"})
                    if (health.get("ok") and candidate.get("quotes") and candidate["competition"]["round_no"] >= 1
                            and request_json(port, "/api/decisions").get("items")):
                        state = candidate
                        break
                except (OSError, http.client.HTTPException, json.JSONDecodeError):
                    pass
                time.sleep(0.2)
            if state is None:
                raise VerificationError("Synthetic package did not become ready before the timeout.")
            request_json(port, "/api/control", {"action": "pause"})
            state = request_json(port, "/api/state")
            if not state["status"]["paused"]:
                raise VerificationError("Synthetic pause was not acknowledged.")
            result.update({"competition_id": state["competition"]["id"],
                           "round_no": state["competition"]["round_no"], "paused": True,
                           "synthetic_name": expected_name, "trader_count": len(state["traders"])})
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            result["shutdown"] = stop_owned_process(process, environment)
            result["shutdown"]["loopback_port_closed"] = wait_for_port_closed(port)
            (case_root / f"{label}.result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not result["shutdown"]["graceful"]:
        raise VerificationError("Owned package did not complete a graceful Ctrl+C shutdown; inspect case logs.")
    if not result["shutdown"]["loopback_port_closed"]:
        raise VerificationError("The selected loopback port still has a listener after the owned process exited.")
    return result


def inspect_database(path: Path, marker: str, *, create_sentinel: bool = False) -> dict:
    if not path.is_file():
        raise VerificationError("Synthetic database was not created.")
    mode = "rw" if create_sentinel else "ro"
    with closing(sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise VerificationError("Synthetic SQLite integrity check failed.")
        if create_sentinel:
            connection.execute("CREATE TABLE acceptance_sentinel (value TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO acceptance_sentinel VALUES (?)", (marker,))
            connection.commit()
        if connection.execute("SELECT value FROM acceptance_sentinel").fetchall() != [(marker,)]:
            raise VerificationError("Synthetic persistence sentinel changed.")
        rows = connection.execute("SELECT id, name, market, started_at FROM competitions ORDER BY id").fetchall()
        decisions = connection.execute("SELECT count(*) FROM decisions").fetchone()[0]
        if decisions < 1:
            raise VerificationError("No synthetic decision was persisted.")
        return {"integrity": "ok", "competitions": rows, "decision_count": decisions,
                "sentinel_sha256": hashlib.sha256(marker.encode()).hexdigest()}


def prepare_case(source: Path, acceptance: Path, case: str, baseline: dict) -> tuple[Path, Path, str, str | None]:
    case_root = acceptance / "cases" / case
    case_root.mkdir(parents=True)
    package = case_root / "package"
    # Preserve an unexpected link introduced during copying, then reject it in
    # the inventory check; never follow it into unrelated user files.
    shutil.copytree(source, package, symlinks=True)
    if file_inventory(package) != baseline["file_hashes"]:
        raise VerificationError("The package copy does not match the immutable input.")
    internal = package / "_internal" if baseline["layout"] == "internal" else package
    prefix = "acceptance-" + uuid.uuid4().hex
    # Every candidate is offline before any executable is started. A precedence
    # bug must fail the marker check, not contact a live provider.
    write_synthetic(package / "config/config.example.yaml", prefix + "-external-template")
    if internal != package:
        write_synthetic(internal / "config/config.example.yaml", prefix + "-bundled-template")
    config_arg = None
    if case == "external":
        expected = prefix + "-external-runtime"
        write_synthetic(package / "config/config.yaml", expected)
    elif case == "explicit":
        write_synthetic(package / "config/config.yaml", prefix + "-external-runtime")
        expected = prefix + "-explicit"
        write_synthetic(package / "config/acceptance-explicit.yaml", expected)
        config_arg = "config/acceptance-explicit.yaml"
    elif case == "bundled":
        expected = prefix + ("-bundled-template" if internal != package else "-external-template")
    else:
        raise VerificationError("Unknown synthetic acceptance case.")
    return package, case_root, expected, config_arg


def verify(package: Path, acceptance: Path, reference: Path, timeout: float = 45) -> dict:
    if sys.platform != "win32":
        raise VerificationError("Real frozen package acceptance requires Windows.")
    package, acceptance, reference = validate_inputs(package, acceptance, reference)
    baseline = inspect_package(package, reference)
    acceptance.mkdir()  # Exclusive reservation. Existing roots are never reused.
    report = {"status": "running", "source_package": str(package), "acceptance_root": str(acceptance),
              "baseline": baseline, "cases": {}, "limitations": LIMITATIONS,
              "meter": {"last_token_usage": None, "coverage": "not available"}}
    try:
        for case in ("external", "explicit", "bundled"):
            copied, case_root, marker, config_arg = prepare_case(package, acceptance, case, baseline)
            first = run_once(copied, case_root, marker, config_arg, "first", timeout)
            db = copied / "data/acceptance.db"
            before = inspect_database(db, marker, create_sentinel=True)
            outcome = {"first_run": first, "database": before}
            report["cases"][case] = outcome
            if case == "external":
                restarted = run_once(copied, case_root, marker, config_arg, "restart", timeout)
                after = inspect_database(db, marker)
                if restarted["competition_id"] != first["competition_id"] or after["competitions"] != before["competitions"]:
                    raise VerificationError("Restart did not preserve the synthetic competition identity.")
                if after["decision_count"] < before["decision_count"]:
                    raise VerificationError("Restart lost synthetic decision rows.")
                outcome.update({"restart": restarted, "database_after_restart": after})
        report["status"] = "passed"
        if baseline["layout"] == "flat":
            report["limitations"] = [*LIMITATIONS, "Flat layout has one shared template location; separate bundled/external template priority is not distinguishable."]
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            report["source_unchanged"] = file_inventory(package) == baseline["file_hashes"]
        except (OSError, VerificationError) as exc:
            report["source_unchanged"] = False
            report["source_recheck_error"] = type(exc).__name__
        if not report["source_unchanged"]:
            report["status"] = "failed"
            report["error"] = "Source package changed during acceptance. No restoration was attempted."
        (acceptance / "acceptance-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["source_unchanged"]:
        raise VerificationError("Source package changed during acceptance.")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="Immutable onedir package containing FuturesArena.exe")
    parser.add_argument("--acceptance-root", type=Path, required=True, help="Fresh absolute directory; must not exist")
    parser.add_argument("--reference-config", type=Path, required=True, help="Separate checked-out public config directory")
    parser.add_argument("--timeout", type=float, default=45, help="Startup deadline per run, in seconds")
    args = parser.parse_args(argv)
    if not 5 <= args.timeout <= 120:
        parser.error("--timeout must be between 5 and 120 seconds")
    try:
        report = verify(args.package, args.acceptance_root, args.reference_config, args.timeout)
    except Exception as exc:
        print(f"Package acceptance failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": report["status"], "source_unchanged": report["source_unchanged"],
                      "report": str(args.acceptance_root / "acceptance-report.json")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
