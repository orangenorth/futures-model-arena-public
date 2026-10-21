"""离线导出阶段性结果：python scripts/export_results.py [--db data/arena.db] [--out results]

生成 leaderboard.csv、daily.csv、trades.csv、equity.csv，不需要启动软件。
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arena.db import Database, LEGACY_COMPETITION_TIMEZONE, competition_timezone  # noqa: E402
from arena.engine import metrics  # noqa: E402


def competition_tz(comp: dict) -> ZoneInfo:
    """Use the persisted competition timezone, or the stable legacy timezone when absent."""
    try:
        return ZoneInfo(competition_timezone(comp) or LEGACY_COMPETITION_TIMEZONE)
    except Exception:  # noqa: BLE001
        return ZoneInfo(LEGACY_COMPETITION_TIMEZONE)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/arena.db")
    p.add_argument("--out", default="results")
    p.add_argument("--competition", type=int, default=None, help="竞赛 id（默认进行中的竞赛，其次最近一场）")
    a = p.parse_args()
    db = Database(a.db)
    if a.competition is not None:
        comp = next((c for c in db.list_competitions() if int(c["id"]) == a.competition), None)
    else:
        comp = db.active_competition() or (db.list_competitions() or [None])[0]
    if not comp:
        print("数据库中没有竞赛" if a.competition is None else f"数据库中没有竞赛 #{a.competition}")
        return
    cid = comp["id"]
    tz = competition_tz(comp)

    def fmt_ts(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    traders = db.traders(cid)
    accounts = db.load_accounts(cid)
    with open(out / "leaderboard.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["trader", "name", "cash", "realized_pnl", "fees", "max_drawdown_pct", "win_rate", "closed_trades", "total_trades"])
        for t in traders:
            acc = accounts.get(t["trader_id"])
            series = db.equity_values(cid, t["trader_id"])
            st = metrics.trade_stats(db.all_trades(cid, t["trader_id"]))
            w.writerow([t["trader_id"], t["name"], acc.cash if acc else "", acc.realized_pnl if acc else "", acc.fees if acc else "",
                        round(metrics.max_drawdown(series), 3) if series else "", round(st["win_rate"], 1), st["closed_trades"], st["total_trades"]])
    with open(out / "daily.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["day", "trader", "start_equity", "end_equity", "pnl", "trades", "fees"])
        for d in db.daily(cid, 10000):
            w.writerow([d["day"], d["trader_id"], d["start_equity"], d["end_equity"], d["pnl"], d["trades"], d["fees"]])
    with open(out / "trades.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["time", "trader", "symbol", "action", "qty", "price", "fee", "realized_pnl", "round", "reason"])
        for t in db.trades(cid, limit=10**7):
            w.writerow([fmt_ts(t["ts"]), t["trader_id"], t["symbol"], t["action"], t["qty"], t["price"], t["fee"], t["realized_pnl"], t["round_no"], t.get("reason", "")])
    with open(out / "equity.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["trader", "time", "equity"])
        for tid, pts in db.equity_series(cid, max_points=10**7).items():
            for ts, eq in pts:
                w.writerow([tid, fmt_ts(ts), eq])
    print(f"已导出到 {out.resolve()}（竞赛 #{cid} {comp['name']}，第 {comp['round_no']} 轮，时间按 {tz.key}）")


if __name__ == "__main__":
    main()
