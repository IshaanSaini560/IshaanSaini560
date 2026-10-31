"""Launch-data pipeline CLI (read-only; never signs or sends transactions).

  python -m pumpsim.pipeline frame   --random-windows 24 --days 14 --minutes 10 --seed 1 --horizon 21600
  python -m pumpsim.pipeline frame   --start 2026-10-20T14:00 --end 2026-10-20T14:30
  python -m pumpsim.pipeline sample  --n 1000 --seed 1 --horizon 21600
  python -m pumpsim.pipeline collect --limit 1000 --max-credits 200000
  python -m pumpsim.pipeline summarize
  python -m pumpsim.pipeline reprocess      # rebuild rows from stored raw transactions after a decoder fix
  python -m pumpsim.pipeline export  --out data/exports
  python -m pumpsim.pipeline status

Sources (--source): helius (default; needs HELIUS_API_KEY, uses getTransactionsForAddress),
helius-rpc (standard methods on the Helius URL), publicnode (free, keyless, ~3 days of history),
public (SOLANA_RPC_URL or public mainnet; ~1 request/s, trials only).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
from datetime import datetime, timezone

from . import db
from .collect import collect_pending, enumerate_frame, log_usage, random_windows, reprocess, sample_launches
from .sources import make_source
from .summarize import summarize_all

DEFAULT_DB = str(pathlib.Path(__file__).resolve().parents[2] / "data" / "pipeline" / "launches.sqlite")


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def export(con, out: pathlib.Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for table in ("launches", "transactions", "trades", "failed_attempts", "frame_windows"):
        rows = con.execute(f"SELECT * FROM {table}").fetchall()
        path = out / f"{table}.csv"
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            if rows:
                w.writerow(rows[0].keys())
                w.writerows(tuple(r) for r in rows)
        written.append(str(path))
    rows = [json.loads(r["data"]) for r in con.execute("SELECT data FROM launch_summary")]
    path = out / "launch_summary.csv"
    with path.open("w", newline="") as f:
        if rows:
            cols = list(dict.fromkeys(k for r in rows for k in r))  # union of keys, first-seen order
            w = csv.DictWriter(f, fieldnames=cols, restval="")
            w.writeheader()
            for r in rows:
                w.writerow({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in r.items()})
    written.append(str(path))
    return written


def status(con) -> None:
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    print(f"frame windows        {q('SELECT COUNT(*) FROM frame_windows')}")
    print(f"frame creations      {q('SELECT COUNT(*) FROM frame_creations')} "
          f"(failed {q('SELECT COUNT(*) FROM frame_creations WHERE failed=1')}, selected {q('SELECT COUNT(*) FROM frame_creations WHERE selected=1')})")
    for st, n in con.execute("SELECT status, COUNT(*) FROM launches GROUP BY status"):
        print(f"launches {st:12s} {n}")
    print(f"  mayhem             {q('SELECT COUNT(*) FROM launches WHERE is_mayhem=1')}")
    print(f"  non-SOL quote      {q('SELECT COUNT(*) FROM launches WHERE is_sol_quote=0')}")
    print(f"  truncated          {q('SELECT COUNT(*) FROM launches WHERE truncated=1')}")
    print(f"transactions         {q('SELECT COUNT(*) FROM transactions')} (failed {q('SELECT COUNT(*) FROM transactions WHERE failed=1')})")
    print(f"trades               {q('SELECT COUNT(*) FROM trades')} (sim checked {q('SELECT COUNT(*) FROM trades WHERE sim_match IS NOT NULL')}, "
          f"matched {q('SELECT COUNT(*) FROM trades WHERE sim_match=1')})")
    print(f"credits used (est.)  {q('SELECT COALESCE(SUM(credits),0) FROM usage_log'):.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="pumpsim.pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--source", default="helius", choices=["helius", "helius-rpc", "publicnode", "public"])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("frame")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--random-windows", type=int)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--minutes", type=int, default=10)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--horizon", type=int, default=21600)
    p = sub.add_parser("sample")
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--horizon", type=int, default=21600, help="observation window after creation, seconds")
    p = sub.add_parser("collect")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--max-tx", type=int, default=15000, help="per launch; launches hitting it are flagged truncated")
    p.add_argument("--max-credits", type=float, default=float("inf"))
    p.add_argument("--no-raw", action="store_true", help="do not keep raw transactions")
    sub.add_parser("summarize")
    sub.add_parser("reprocess", help="rebuild rows from stored raw transactions (no network)")
    p = sub.add_parser("export")
    p.add_argument("--out", default=str(pathlib.Path(DEFAULT_DB).parent / "exports"))
    sub.add_parser("status")
    a = ap.parse_args()

    pathlib.Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    con = db.connect(a.db)
    if a.cmd in ("summarize", "export", "status", "reprocess"):
        if a.cmd == "reprocess":
            print(f"reprocessed {reprocess(con)} launches")
        elif a.cmd == "summarize":
            print(f"summarised {summarize_all(con)} launches")
        elif a.cmd == "export":
            print("\n".join(export(con, pathlib.Path(a.out))))
        else:
            status(con)
        return
    src = make_source(a.source)
    if a.cmd == "frame":
        windows = ([(_ts(a.start), _ts(a.end))] if a.start else
                   random_windows(a.days, a.random_windows, a.minutes, a.seed, a.horizon))
        for s, e in windows:
            wid = enumerate_frame(con, src, s, e)
            r = con.execute("SELECT * FROM frame_windows WHERE window_id=?", (wid,)).fetchone()
            print(f"window {wid}: {datetime.fromtimestamp(s, timezone.utc):%Y-%m-%d %H:%M}–"
                  f"{datetime.fromtimestamp(e, timezone.utc):%H:%M} UTC  creations {r['n_success']} (+{r['n_failed']} failed)")
        log_usage(con, "frame", src, f"windows={len(windows)}")
    elif a.cmd == "sample":
        print(f"added {sample_launches(con, src, a.n, a.seed, a.horizon)} launches")
    elif a.cmd == "collect":
        collect_pending(con, src, a.limit, a.max_tx, a.max_credits, keep_raw=not a.no_raw)
    print(f"source {src.name}: {src.usage.calls} calls, ~{src.usage.credits:.0f} credits {src.usage.by_method}")


if __name__ == "__main__":
    main()
