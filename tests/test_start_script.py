from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_launcher_prefers_the_project_venv_and_stays_ascii():
    raw = (ROOT / "scripts" / "start.bat").read_bytes()
    assert all(byte < 128 for byte in raw), "cmd batch files must remain ASCII-safe"
    script = raw.decode("ascii")

    venv_pick = 'set PY_CMD="%CD%\\.venv\\Scripts\\python.exe"'
    path_fallback = 'python -c "%VERSION_CHECK%"'
    launcher_fallback = 'py -3 -c "%VERSION_CHECK%"'
    assert venv_pick in script and path_fallback in script and launcher_fallback in script
    assert script.index(venv_pick) < script.index(path_fallback) < script.index(launcher_fallback)
    assert "sys.version_info >= (3, 11)" in script
    # "where python" also finds the Microsoft Store alias stub, which is not a usable interpreter
    commands = [l for l in script.splitlines() if not l.lstrip().lower().startswith("rem ")]
    assert not any("where python" in l for l in commands)
    assert "%PY_CMD% run.py %*" in script
    assert "exit /b %EXIT_CODE%" in script


def test_windows_build_script_checks_every_native_step():
    raw = (ROOT / "scripts" / "build_exe.ps1").read_bytes()
    # Windows PowerShell 5.1 decodes BOM-less scripts with the ANSI code page, garbling the Chinese messages
    assert raw.startswith(b"\xef\xbb\xbf")
    script = raw.decode("utf-8-sig")
    assert '.venv\\Scripts\\python.exe' in script
    assert "$LASTEXITCODE -ne 0" in script
    for step in ("-m pip install", "-m pytest", "-m PyInstaller"):
        line = next(l for l in script.splitlines() if step in l)
        assert line.lstrip().startswith("Invoke-Step"), line
