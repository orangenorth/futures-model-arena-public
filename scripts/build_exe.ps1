# 在 Windows 上打包为 dist\FuturesArena\FuturesArena.exe
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# 文件带 UTF-8 BOM：Windows PowerShell 5.1 对无 BOM 脚本按系统 ANSI 代码页解码，中文会乱码
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# 与 scripts\start.bat 一致：优先使用项目 .venv
$Python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
Write-Host "使用 Python：$Python"

# 外部命令失败不会触发 $ErrorActionPreference，必须逐步检查退出码
function Invoke-Step([string]$Name, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Name 失败（退出码 $LASTEXITCODE），停止打包" }
}

Invoke-Step "安装依赖" { & $Python -m pip install -r requirements.txt pyinstaller }
# CTP 行情 SDK 不是硬依赖（源码运行按需装），但打进 exe 之后用户没法再 pip install，所以打包时必须带上
Invoke-Step "安装 openctp-ctp" { & $Python -m pip install "openctp-ctp>=6.7" }
Invoke-Step "检查 openctp-ctp（失败则打出的 exe 无法使用 market: ctp）" { & $Python -c "import openctp_ctp.mdapi" }
Invoke-Step "测试" { & $Python -m pytest -q }

if (Test-Path dist\FuturesArena) { Remove-Item -Recurse -Force dist\FuturesArena }
Invoke-Step "PyInstaller 打包" { & $Python -m PyInstaller arena.spec --noconfirm }
if (-not (Test-Path dist\FuturesArena\FuturesArena.exe)) { throw "未生成 dist\FuturesArena\FuturesArena.exe，停止打包" }

# 把可编辑的配置放到 exe 旁边（首次运行读取 config\config.yaml，没有则用模板）
New-Item -ItemType Directory -Force dist\FuturesArena\config | Out-Null
Copy-Item config\config.example.yaml dist\FuturesArena\config\ -Force
Copy-Item config\demo.yaml dist\FuturesArena\config\ -Force
Copy-Item config\contracts_cn.json dist\FuturesArena\config\ -Force
Write-Host "打包完成：dist\FuturesArena\FuturesArena.exe" -ForegroundColor Green
Write-Host "首次使用：复制 config\config.example.yaml 为 config\config.yaml 并填入 API Key。"
