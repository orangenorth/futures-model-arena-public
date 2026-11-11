# Build into a fresh dist\<build-id>\FuturesArena directory on Windows.
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# 文件带 UTF-8 BOM：Windows PowerShell 5.1 对无 BOM 脚本按系统 ANSI 代码页解码，中文会乱码
param([string]$OutputRoot)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# Every build gets a fresh directory. Never replace a portable installation.
$BuildId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
if (-not $OutputRoot) { $OutputRoot = Join-Path (Get-Location) "dist\$BuildId" }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $OutputRoot) {
    throw "Output directory already exists; choose a new -OutputRoot. Existing files were not changed: $OutputRoot"
}
New-Item -ItemType Directory -Path $OutputRoot -ErrorAction Stop | Out-Null
$PackageDir = Join-Path $OutputRoot 'FuturesArena'
$WorkDir = Join-Path (Get-Location) "build\$BuildId"

# 与 scripts\start.bat 一致：优先使用项目 .venv
$Python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$Candidates = @(
    @{Command=$Python; Prefix=@()},
    @{Command='python'; Prefix=@()},
    @{Command='py'; Prefix=@('-3')}
)
$Python = $null
$PythonPrefix = @()
$VersionCheck = "import struct, sys; sys.exit(0 if sys.version_info >= (3, 11) and struct.calcsize('P') == 8 else 1)"
foreach ($Candidate in $Candidates) {
    $Prefix = $Candidate.Prefix
    try {
        & $Candidate.Command @Prefix -c $VersionCheck *> $null
        if ($LASTEXITCODE -eq 0) {
            $Python = $Candidate.Command
            $PythonPrefix = $Prefix
            break
        }
    } catch { continue }
}
if (-not $Python) { throw 'Python 3.11+ x64 was not found in .venv, PATH or the py launcher.' }
Write-Host "Using Python: $Python $PythonPrefix"

# 外部命令失败不会触发 $ErrorActionPreference，必须逐步检查退出码
function Invoke-Step([string]$Name, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Name 失败（退出码 $LASTEXITCODE），停止打包" }
}

Invoke-Step "Install dependencies" { & $Python @PythonPrefix -m pip install -r requirements.txt pyinstaller }
# CTP 行情 SDK 不是硬依赖（源码运行按需装），但打进 exe 之后用户没法再 pip install，所以打包时必须带上
Invoke-Step "Install openctp-ctp" { & $Python @PythonPrefix -m pip install "openctp-ctp>=6.7" }
Invoke-Step "Validate the CTP SDK" { & $Python @PythonPrefix -c "from openctp_ctp import mdapi; print(mdapi.CThostFtdcMdApi.GetApiVersion())" }
Invoke-Step "Run tests" { & $Python @PythonPrefix -m pytest -q }

Invoke-Step "PyInstaller build" { & $Python @PythonPrefix -m PyInstaller arena.spec --noconfirm --distpath $OutputRoot --workpath $WorkDir }
if (-not (Test-Path -LiteralPath (Join-Path $PackageDir 'FuturesArena.exe'))) { throw 'PyInstaller did not produce FuturesArena.exe.' }

# 把可编辑的配置放到 exe 旁边（首次运行读取 config\config.yaml，没有则用模板）
New-Item -ItemType Directory -Path (Join-Path $PackageDir 'config') -ErrorAction Stop | Out-Null
Copy-Item config\config.example.yaml (Join-Path $PackageDir 'config')
Copy-Item config\demo.yaml (Join-Path $PackageDir 'config')
Copy-Item config\contracts_cn.json (Join-Path $PackageDir 'config')
Copy-Item config\contracts_universe_cn.json (Join-Path $PackageDir 'config')
Copy-Item config\contracts_admission_ledger_cn.json (Join-Path $PackageDir 'config')
Copy-Item config\admission_source_policy_cn.json (Join-Path $PackageDir 'config')
Write-Host "Build complete: $PackageDir\FuturesArena.exe" -ForegroundColor Green
Write-Host "首次使用：复制 config\config.example.yaml 为 config\config.yaml 并填入 API Key。"
