"""四模型期货竞赛 - 入口。

用法：
  python run.py                      # 桌面窗口（pywebview）
  python run.py --browser            # 用系统浏览器打开看板
  python run.py --headless           # 只跑引擎与 API，不打开界面
  python run.py --config config/demo.yaml --accelerate   # 离线演示，加速运行
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path


def app_base_dir() -> Path:
    """源码运行时为仓库根目录；PyInstaller 打包后为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """打包后只读资源（web/、config/ 模板）所在目录。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return app_base_dir()


def setup_logging(log_dir: Path, verbose: bool):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_dir / "arena.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)


def main(argv=None):
    p = argparse.ArgumentParser(description="四模型期货竞赛")
    p.add_argument("--config", "-c", help="配置文件路径（默认 config/config.yaml，其次 config/config.example.yaml）")
    p.add_argument("--browser", action="store_true", help="用系统浏览器打开看板")
    p.add_argument("--headless", action="store_true", help="不打开界面，只运行引擎与 API")
    p.add_argument("--accelerate", action="store_true", help="加速模式：忽略开市判断，秒级决策（测试/演示）")
    p.add_argument("--port", type=int, help="固定 API 端口")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)

    base = app_base_dir()
    os.chdir(base)
    sys.path.insert(0, str(base))
    from arena.config import load_config
    from arena.db import Database
    from arena.engine.loop import Engine, RolloverBlocked
    from arena.exchange.contracts import ContractSpecError, preflight_contract_specs
    from arena.desktop import acquire_instance_lock, open_existing_instance, run_desktop

    cfg_path = args.config
    if cfg_path is None:
        for cand in (base / "config" / "config.yaml", resource_dir() / "config" / "config.example.yaml", base / "config" / "config.example.yaml"):
            if cand.exists():
                cfg_path = cand
                break
    cfg = load_config(cfg_path, base_dir=base)
    if not (base / "config" / "contracts_cn.json").exists():
        cfg.market_data.cn_futures.contracts_file = str(resource_dir() / "config" / "contracts_cn.json")
    if args.port:
        cfg.server.port = args.port
    try:
        # Preflight before the instance lock and before the database is opened:
        # merely attempting to start with an unusable contract table must not
        # create a schema, nor disturb the competition already recorded in it.
        preflight_contract_specs(cfg.competition.market, cfg.symbols,
                                 cfg.resolve(cfg.market_data.cn_futures.contracts_file))
    except ContractSpecError as e:
        print(f"Cannot start: {e}", file=sys.stderr)
        return 4
    setup_logging(cfg.log_dir, args.debug)
    logging.getLogger("arena").info("配置文件: %s", cfg_path)

    mode = "headless" if args.headless else ("browser" if args.browser else "window")
    lock = acquire_instance_lock(cfg.db_path)
    if lock is None:
        open_existing_instance(cfg.db_path, mode)
        return 2
    db = Database(cfg.db_path)
    try:
        engine = Engine(cfg, db, accelerate=args.accelerate)
    except RolloverBlocked as e:
        # 配置变化要切换竞赛，但旧竞赛有触发成交还没写进数据库：不能丢掉它，也不能带着它归档
        logging.getLogger("arena").error("无法启动：%s", e)
        print(f"无法启动：{e}", file=sys.stderr)
        lock.close()
        return 3
    except ContractSpecError as e:
        # The preflight above already covers the normal path; this catches a table
        # edited between preflight and construction.  Never substitute another
        # product's terms for a symbol whose own terms are missing or invalid.
        logging.getLogger("arena").error("Cannot start: %s", e)
        print(f"Cannot start: {e}", file=sys.stderr)
        lock.close()
        return 4
    web_dir = resource_dir() / "web"
    try:
        run_desktop(engine, cfg, web_dir, mode=mode, debug=args.debug)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
