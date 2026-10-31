"""Replay one coin's entire bonding-curve history through pumpsim, starting from a fresh curve.

Unlike validate_against_chain.py (which rebuilds the pre-trade state from each event), this
carries the *simulated* state forward from creation, so any error would compound. It is also a
first prototype of the per-launch extraction the backtesting pipeline will need.

Read-only. Usage:
    python -m scripts.replay_launch --mint <MINT>
    python -m scripts.replay_launch --auto        # pick a recent SOL-paired, non-mayhem coin
Writes data/replays/<mint>.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter

from pumpsim.curve import CurveState, FeeSchedule, buy_exact_quote_in, buy_exact_tokens_out, sell_exact_tokens_in
from pumpsim.onchain.decode import events_from_transaction, pump_instructions
from pumpsim.onchain.layouts import PUMP_PROGRAM_ID
from pumpsim.onchain.pda import bonding_curve_pda
from pumpsim.onchain.rpc import DEFAULT_RPC, Rpc
from pumpsim.params import VERIFIED

OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "replays"
EXACT_OUT = {"buy", "buy_v2", "buy_v3"}
EXACT_IN = {"buy_exact_sol_in", "buy_exact_quote_in_v2", "buy_exact_quote_in_v3"}
SELLS = {"sell", "sell_v2", "sell_v3"}
SOL_QUOTES = {"11111111111111111111111111111111", "So11111111111111111111111111111111111111112"}


def all_signatures(rpc: Rpc, address: str, max_pages: int = 20) -> list[dict]:
    out, before = [], None
    for _ in range(max_pages):
        page = rpc.signatures(address, limit=1000, before=before)
        out += page
        if len(page) < 1000:
            return out
        before = page[-1]["signature"]
    raise RuntimeError(f"more than {max_pages * 1000} signatures; pick a smaller coin")


def pick_recent_mint(rpc: Rpc, lo: int, hi: int) -> str:
    seen: set[str] = set()
    for s in rpc.signatures(PUMP_PROGRAM_ID, limit=300):
        if s.get("err"):
            continue
        tx = rpc.transaction(s["signature"])
        for name, ev in events_from_transaction(tx) if tx else []:
            if name != "TradeEvent" or ev["mint"] in seen or ev["mayhem_mode"] or ev["quote_mint"] not in SOL_QUOTES:
                continue
            seen.add(ev["mint"])
            n = len(rpc.signatures(bonding_curve_pda(ev["mint"]), limit=1000))
            if lo <= n <= hi:
                return ev["mint"]
    raise RuntimeError("no suitable coin found in sample; pass --mint")


def replay(rpc: Rpc, mint: str) -> dict:
    curve = bonding_curve_pda(mint)
    sigs = list(reversed(all_signatures(rpc, curve)))  # oldest first
    sched = VERIFIED.fee_schedule
    state: CurveState | None = None
    rows, failed, mism, users, creator = [], 0, [], Counter(), None
    t0 = None
    for s in sigs:
        if s.get("err") is not None:
            failed += 1
            continue
        tx = rpc.transaction(s["signature"])
        if not tx:
            continue
        evs = events_from_transaction(tx)
        for name, ev in evs:
            if name == "CreateEvent" and ev["mint"] == mint:
                if ev["is_mayhem_mode"] or ev["quote_mint"] not in SOL_QUOTES:
                    raise RuntimeError("mayhem or non-SOL coin: out of Phase 1 scope")
                state = CurveState.fresh(VERIFIED)
                creator, t0 = ev["creator"], ev["timestamp"]
        trades = [e for n, e in evs if n == "TradeEvent" and e["mint"] == mint]
        ixs = [(n, a) for n, a, accts in pump_instructions(tx) if not n.startswith("create") and mint in accts]
        if not trades:
            continue
        if state is None:
            raise RuntimeError("history does not start with a CreateEvent (truncated?)")
        if len(trades) != len(ixs):
            mism.append({"signature": s["signature"], "reason": "instruction/event count mismatch"})
            continue
        for (name, args), ev in zip(ixs, trades):
            if name in EXACT_OUT:
                r = buy_exact_tokens_out(state, args["amount"], sched)
            elif name in EXACT_IN:
                r = buy_exact_quote_in(state, args.get("spendable_sol_in") or args.get("spendable_quote_in"), sched)
            elif name in SELLS:
                r = sell_exact_tokens_in(state, args["amount"], sched)
            else:
                mism.append({"signature": s["signature"], "reason": f"unsupported {name}"})
                continue
            ok = (r.tokens == ev["token_amount"] and r.quote_curve == ev["sol_amount"]
                  and r.protocol_fee == ev["fee"] and r.creator_fee == ev["creator_fee"]
                  and r.after.virtual_quote_reserves == ev["virtual_sol_reserves"]
                  and r.after.virtual_token_reserves == ev["virtual_token_reserves"]
                  and r.after.real_quote_reserves == ev["real_sol_reserves"]
                  and r.after.real_token_reserves == ev["real_token_reserves"])
            if not ok:
                mism.append({"signature": s["signature"], "ix": name, "sim_tokens": r.tokens, "ev_tokens": ev["token_amount"],
                             "sim_quote": r.quote_curve, "ev_quote": ev["sol_amount"]})
                state = CurveState(ev["virtual_token_reserves"], ev["virtual_sol_reserves"], ev["real_token_reserves"],
                                   ev["real_sol_reserves"])  # resync so one error is not counted repeatedly
            else:
                state = r.after
            users[ev["user"]] += 1
            rows.append({"slot": s["slot"], "t": ev["timestamp"] - t0, "user": ev["user"], "is_creator": ev["user"] == creator,
                         "ix": name, "side": r.side, "tokens": r.tokens, "sol_curve": r.quote_curve,
                         "fees": r.total_fee, "mcap_sol_after": r.after.market_cap() / 1e9, "match": ok})
    return {
        "mint": mint, "bonding_curve": curve, "creator": creator,
        "signatures_total": len(sigs), "signatures_failed": failed,
        "trades_replayed": len(rows), "trades_exact_match": sum(r["match"] for r in rows),
        "mismatches": mism[:20], "distinct_traders": len(users),
        "final_state": state.__dict__ if state else None,
        "trades": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    ap.add_argument("--mint")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--min-sigs", type=int, default=40)
    ap.add_argument("--max-sigs", type=int, default=400)
    a = ap.parse_args()
    rpc = Rpc(a.rpc, min_interval=0.35)
    mint = a.mint or pick_recent_mint(rpc, a.min_sigs, a.max_sigs)
    res = replay(rpc, mint)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{mint}.json"
    out.write_text(json.dumps(res, indent=2, default=str) + "\n")
    print(json.dumps({k: v for k, v in res.items() if k != "trades"}, indent=2, default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
