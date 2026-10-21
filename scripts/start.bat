@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0\.."
if not exist config\config.yaml (
  echo [INFO] No config\config.yaml found. Unconfigured model slots will use their demo fallback.
)
rem Pick the first interpreter that really runs Python 3.11+: project .venv, then PATH python, then the py launcher.
rem Running a version check instead of "where python" rejects the Microsoft Store alias stub.
set "VERSION_CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
set PY_CMD=
if exist "%CD%\.venv\Scripts\python.exe" set PY_CMD="%CD%\.venv\Scripts\python.exe"
if defined PY_CMD (
  %PY_CMD% -c "%VERSION_CHECK%" >nul 2>nul
  if errorlevel 1 (
    echo [WARN] .venv Python is broken or older than 3.11, trying the system Python.
    set PY_CMD=
  )
)
if not defined PY_CMD (
  python -c "%VERSION_CHECK%" >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
  py -3 -c "%VERSION_CHECK%" >nul 2>nul
  if not errorlevel 1 set "PY_CMD=py -3"
)
if not defined PY_CMD (
  echo [ERROR] Python 3.11+ was not found in .venv, PATH or the py launcher.
  echo [ERROR] Create .venv with Python 3.11+ and install requirements.txt first.
  pause
  exit /b 1
)
%PY_CMD% run.py %*
set "EXIT_CODE=%ERRORLEVEL%"
rem Keep the console open only when something went wrong so the error stays readable.
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
