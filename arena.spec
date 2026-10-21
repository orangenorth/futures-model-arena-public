# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：pyinstaller arena.spec
# 产出 dist/FuturesArena/FuturesArena.exe（onedir，配合 config/ 与 data/ 目录使用）
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = (
    collect_submodules("uvicorn") + collect_submodules("anyio") + collect_submodules("httpx")
    + collect_submodules("openai") + ["webview", "webview.platforms.edgechromium", "webview.platforms.winforms", "clr_loader", "pythonnet"]
)
datas = [("web", "web"), ("config/config.example.yaml", "config"), ("config/demo.yaml", "config"), ("config/contracts_cn.json", "config")]
try:
    datas += collect_data_files("webview")
except Exception:
    pass
# CTP 行情 SDK（openctp-ctp）带原生 DLL：collect_all 一并收集模块、数据与动态库。
# build_exe.ps1 会先装好它；没装时仍能打包，只是 exe 里的 market: ctp 会报缺 SDK。
binaries = []
try:
    from PyInstaller.utils.hooks import collect_all
    _ctp_datas, _ctp_binaries, _ctp_hidden = collect_all("openctp_ctp")
    datas += _ctp_datas
    binaries += _ctp_binaries
    hiddenimports += _ctp_hidden
except Exception:
    pass

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="FuturesArena",
    debug=False,
    strip=False,
    upx=False,
    console=True,   # 保留控制台便于查看日志；稳定后可改 False
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="FuturesArena")
