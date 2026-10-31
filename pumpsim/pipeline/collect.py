"""Collection steps: enumerate the creation frame, sample launches, collect their curve history."""
from __future__ import annotations

import random
import sqlite3
import time
from datetime import datetime, timezone

from ..curve import CurveState, FeeSchedule, buy_exact_quote_in, buy_exact_tokens_out, sell_exact_tokens_in
from ..onchain.decode import events_from_transaction
from ..onchain.layouts import PUMP_PROGRAM_ID
from ..onchain.pda import bonding_curve_pda, find_program_address
from . import db
from .extract import SOL_QUOTES, extract
from .sources import HistorySource

MINT_AUTHORITY = find_program_address([b"mint-authority"], PUMP_PROGRAM_ID)[0]  # TSLvdd1p...; only create/create_v2 use it
EXACT_OUT = {"buy", "buy_v2", "buy_v3"}
EXACT_IN = {"buy_exact_sol_in", "buy_exact_quote_in_v2", "buy_exact_quote_in_v3"}
SELLS = {"sell", "sell_v2", "sell_v3"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_usage(con: sqlite3.Connection, command: str, src: HistorySource, detail: str = "") -> None:
    con.execute("INSERT INTO usage_log VALUES (?,?,?,?,?,?)",
                (_now(), command, src.name, src.usage.calls, src.usage.credits, detail))
    con.commit()


# ------------------------------------------------------------------ 1. sampling frame

def enumerate_frame(con: sqlite3.Connection, src: HistorySource, t_start: int, t_end: int) -> int:
    """Record every creation transaction in [t_start, t_end). Idempotent per window."""
    row = con.execute("SELECT window_id FROM frame_windows WHERE t_start=? AND t_end=?", (t_start, t_end)).fetchone()
    if row:
        return row["window_id"]
    refs = src.signatures(MINT_AUTHORITY, t_start, t_end)
    cur = con.execute(
        "INSERT INTO frame_windows (t_start, t_end, source, n_signatures, n_failed, n_success, enumerated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (t_start, t_end, src.name, len(refs), sum(r.failed for r in refs), sum(not r.failed for r in refs), _now()))
    wid = cur.lastrowid
    con.executemany("INSERT OR IGNORE INTO frame_creations (signature, window_id, slot, tx_index, block_time, failed)"
                    " VALUES (?,?,?,?,?,?)",
                    [(r.signature, wid, r.slot, r.tx_index, r.block_time, int(r.failed)) for r in refs])
    con.commit()
    return wid


def random_windows(days: int, n: int, minutes: int, seed: int, horizon_s: int, now: int | None = None) -> list[tuple[int, int]]:
    """n non-overlapping windows of `minutes`, start times uniform over the last `days` days,
    all ending early enough that every launch in them has a complete observation horizon."""
    now = now or int(time.time())
    latest_end = now - horizon_s - 120
    earliest = latest_end - days * 86400
    rng = random.Random(seed)
    span = minutes * 60
    out: list[tuple[int, int]] = []
    tries = 0
    while len(out) < n and tries < 10_000:
        tries += 1
        s = rng.randrange(earliest, latest_end - span)
        s -= s % 60
        if all(s + span <= a or s >= b for a, b in out):
            out.append((s, s + span))
    return sorted(out)


# ------------------------------------------------------------------ 2. sample

def sample_launches(con: sqlite3.Connection, src: HistorySource, n: int, seed: int, horizon_s: int) -> int:
    """Simple random sample (without replacement) of successful creations from the whole frame,
    restricted to launches whose observation horizon is already over (no right-censoring).
    Decodes each selected creation and inserts a pending launch row. Returns rows added."""
    cutoff = int(time.time()) - horizon_s - 60
    pool = [r["signature"] for r in con.execute(
        "SELECT signature FROM frame_creations WHERE failed=0 AND selected=0 AND block_time <= ? ORDER BY signature",
        (cutoff,))]
    rng = random.Random(seed)
    chosen = rng.sample(pool, min(n, len(pool)))
    refs = {r["signature"]: r for r in con.execute(
        f"SELECT * FROM frame_creations WHERE signature IN ({','.join('?' * len(chosen))})", chosen)} if chosen else {}
    from .sources import SigRef
    sig_refs = [SigRef(s, refs[s]["slot"], refs[s]["block_time"], False, refs[s]["tx_index"], 0) for s in chosen]
    added = 0
    for ft in src.transactions(sig_refs):
        creates = [e for name, e in events_from_transaction(ft.tx) if name == "CreateEvent"]
        con.execute("UPDATE frame_creations SET selected=1, sample_seed=? WHERE signature=?", (seed, ft.ref.signature))
        if not creates:
            continue
        e = creates[0]
        db.put_raw(con, ft.ref.signature, ft.tx)
        db.insert_row(con, "launches", {
            "mint": e["mint"], "create_signature": ft.ref.signature, "window_id": refs[ft.ref.signature]["window_id"],
            "bonding_curve": e["bonding_curve"], "creator": e["creator"], "create_user": e["user"],
            "create_slot": ft.ref.slot, "create_tx_index": ft.ref.tx_index, "create_time": e["timestamp"],
            "name": e["name"], "symbol": e["symbol"], "uri": e["uri"], "token_program": e["token_program"],
            "is_mayhem": int(bool(e["is_mayhem_mode"])), "quote_mint": e["quote_mint"],
            "is_sol_quote": int((e["quote_mint"] or "11111111111111111111111111111111") in SOL_QUOTES),
            "is_holder_reward": int(bool(e["is_holder_reward"])), "is_cashback": int(bool(e["is_cashback_enabled"])),
            "creator_fee_bps": e["creator_fee_bps"], "depth": e["depth"],
            "init_virtual_token": e["virtual_token_reserves"],
            "init_virtual_quote": e["virtual_quote_reserves"] or e["virtual_sol_reserves"],
            "init_real_token": e["real_token_reserves"], "token_total_supply": e["token_total_supply"],
            "horizon_s": horizon_s, "status": "pending",
        }, replace=False)
        added += 1
    con.commit()
    log_usage(con, "sample", src, f"n={n} seed={seed} added={added}")
    return added


# ------------------------------------------------------------------ 3. collect

def _replay(con: sqlite3.Connection, launch: sqlite3.Row) -> tuple[int, int]:
    """Re-run every standard-coin trade through the validated simulator; store sim_match per trade.
    State is re-synced from each event so one mismatch is counted once."""
    if launch["is_mayhem"] or not launch["is_sol_quote"]:
        return 0, 0
    st = CurveState(launch["init_virtual_token"], launch["init_virtual_quote"], launch["init_real_token"], 0,
                    launch["token_total_supply"])
    checked = matched = 0
    rows = con.execute("SELECT * FROM trades WHERE mint=? ORDER BY slot, COALESCE(tx_index, order_key), order_key, seq",
                       (launch["mint"],)).fetchall()
    for t in rows:
        sched = FeeSchedule.flat(t["fee_bps"], t["creator_fee_bps"])
        r = None
        try:
            if t["arg_amount"] is not None and t["ix_name"] in EXACT_OUT:
                r = buy_exact_tokens_out(st, t["arg_amount"], sched)
            elif t["arg_amount"] is not None and t["ix_name"] in EXACT_IN:
                r = buy_exact_quote_in(st, t["arg_amount"], sched)
            elif t["arg_amount"] is not None and t["ix_name"] in SELLS:
                r = sell_exact_tokens_in(st, t["arg_amount"], sched)
        except Exception:
            r = None
        known = t["arg_amount"] is not None and t["ix_name"] in EXACT_OUT | EXACT_IN | SELLS
        if known:
            ok = r is not None and (r.tokens, r.quote_curve, r.protocol_fee, r.creator_fee,
                                    r.after.virtual_quote_reserves, r.after.virtual_token_reserves) == (
                t["token_amount"], t["quote_amount"], t["fee"], t["creator_fee"], t["v_quote"], t["v_token"])
            checked += 1
            matched += ok
            con.execute("UPDATE trades SET sim_match=? WHERE signature=? AND seq=?", (int(ok), t["signature"], t["seq"]))
        st = CurveState(t["v_token"], t["v_quote"], t["r_token"], t["r_quote"], launch["token_total_supply"])
    return checked, matched


def collect_launch(con: sqlite3.Connection, src: HistorySource, launch: sqlite3.Row, max_tx: int,
                   keep_raw: bool = True) -> dict:
    mint = launch["mint"]
    curve = launch["bonding_curve"] or bonding_curve_pda(mint)
    t0 = launch["create_time"] - 2  # include failed attempts landing in the same second as creation
    t1 = launch["create_time"] + launch["horizon_s"]
    txs, truncated = src.full_history(curve, t0, t1, max_tx)
    return _store_launch(con, src.name, launch, txs, truncated, t1, keep_raw)


def reprocess(con: sqlite3.Connection) -> int:
    """Rebuild transactions/trades/failed_attempts for every collected launch from raw_tx
    (no network calls). Use after a decoder or extraction fix."""
    from .sources import FullTx, SigRef

    n = 0
    for launch in con.execute("SELECT * FROM launches WHERE status='collected'").fetchall():
        refs = con.execute("SELECT signature, slot, tx_index, order_key, block_time, failed FROM transactions "
                           "WHERE mint=? ORDER BY order_key", (launch["mint"],)).fetchall()
        txs = []
        for r in refs:
            raw = db.get_raw(con, r["signature"])
            if raw is None:
                raise RuntimeError(f"raw transaction missing for {r['signature']}; re-collect this launch")
            txs.append(FullTx(SigRef(r["signature"], r["slot"], r["block_time"], bool(r["failed"]), r["tx_index"],
                                     r["order_key"]), raw))
        _store_launch(con, launch["source"], launch, txs, bool(launch["truncated"]),
                      launch["create_time"] + launch["horizon_s"], keep_raw=False)
        n += 1
    return n


def _store_launch(con: sqlite3.Connection, source_name: str, launch: sqlite3.Row, txs: list, truncated: bool,
                  t1: int, keep_raw: bool) -> dict:
    mint = launch["mint"]
    for table in ("transactions", "trades", "failed_attempts"):
        con.execute(f"DELETE FROM {table} WHERE mint=?", (mint,))
    n_trades = n_failed = 0
    complete_time = migrate_time = pool = None
    for ft in txs:
        rows = extract(ft.tx, mint, ft.ref.slot, ft.ref.tx_index, ft.ref.order_key)
        db.insert_row(con, "transactions", rows["tx"])
        for t in rows["trades"]:
            db.insert_row(con, "trades", t)
        for f in rows["failed_attempts"]:
            db.insert_row(con, "failed_attempts", f)
        n_trades += len(rows["trades"])
        n_failed += rows["tx"]["failed"]
        if rows["complete"] and complete_time is None:
            complete_time = ft.ref.block_time
        if rows["migration"] and migrate_time is None:
            migrate_time, pool = ft.ref.block_time, rows["migration"]["pool"]
        if keep_raw:
            db.put_raw(con, ft.ref.signature, ft.tx)
    observed_until = (txs[-1].ref.block_time if truncated and txs else t1)
    con.execute("""UPDATE launches SET status='collected', source=?, observed_until=?, truncated=?, n_tx=?,
                   n_failed_tx=?, n_trades=?, complete_time=?, migrate_time=?, pool=?, error=NULL, collected_at=?
                   WHERE mint=?""",
                (source_name, observed_until, int(truncated), len(txs), n_failed, n_trades, complete_time,
                 migrate_time, pool, _now(), mint))
    launch = con.execute("SELECT * FROM launches WHERE mint=?", (mint,)).fetchone()
    checked, matched = _replay(con, launch)
    con.execute("UPDATE launches SET sim_checked=?, sim_matched=? WHERE mint=?", (checked, matched, mint))
    con.commit()
    return {"mint": mint, "n_tx": len(txs), "n_trades": n_trades, "n_failed": n_failed, "truncated": truncated,
            "sim": f"{matched}/{checked}"}


def collect_pending(con: sqlite3.Connection, src: HistorySource, limit: int, max_tx: int, max_credits: float,
                    keep_raw: bool = True, progress=None) -> int:
    progress = progress or (lambda msg: print(msg, flush=True))
    done = 0
    pending = con.execute("SELECT * FROM launches WHERE status IN ('pending','error') ORDER BY create_time LIMIT ?",
                          (limit,)).fetchall()
    for launch in pending:
        if src.usage.credits >= max_credits:
            progress(f"credit budget reached ({src.usage.credits:.0f} >= {max_credits:.0f}); stopping")
            break
        try:
            res = collect_launch(con, src, launch, max_tx, keep_raw)
            done += 1
            progress(f"[{done}/{len(pending)}] {res['mint']}: {res['n_tx']} tx, {res['n_trades']} trades, "
                     f"{res['n_failed']} failed, sim {res['sim']}{' TRUNCATED' if res['truncated'] else ''} "
                     f"| credits {src.usage.credits:.0f}")
        except Exception as e:  # record and move on; never silently drop a sampled launch
            con.rollback()
            con.execute("UPDATE launches SET status='error', error=? WHERE mint=?", (repr(e)[:500], launch["mint"]))
            con.commit()
            progress(f"error {launch['mint']}: {e!r}")
    log_usage(con, "collect", src, f"done={done}")
    return done
