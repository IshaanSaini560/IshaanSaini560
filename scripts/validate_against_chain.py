"""Replay real mainnet Pump TradeEvents through pumpsim and check for exact equality.

For each successful transaction touching the Pump program we decode every trade
instruction (with its arguments) and the TradeEvent it emitted, rebuild the curve
state *before* the trade from the event's post-trade reserves, run the simulator
with the same instruction arguments and compare tokens, curve quote, fees and the
resulting reserves to the lamport.

Also records simple execution-cost observations from the same sample
(transaction failure rate, network fee, Jito tips). These are *sample statistics
from a short window*, not population estimates.

Read-only. Usage: python -m scripts.validate_against_chain --tx 300
Writes data/validation_report.json.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
from collections import Counter
from datetime import datetime, timezone

from pumpsim.curve import (
    CurveState, FeeSchedule, Fees, buy_exact_quote_in, buy_exact_tokens_out, exact_quote_in_net, fee_amount,
    quote_for_tokens_out, sell_exact_tokens_in, tokens_for_quote_in,
)
from pumpsim.onchain.decode import events_from_transaction, pump_instructions
from pumpsim.onchain.layouts import PUMP_PROGRAM_ID
from pumpsim.onchain.rpc import DEFAULT_RPC, Rpc
from pumpsim.params import BUYBACK_SHARE_OF_PROTOCOL_BPS, VERIFIED

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "validation_report.json"
ZERO = "11111111111111111111111111111111"
# Verified 2026-10-31 via Jito block engine getTipAccounts.
JITO_TIP_ACCOUNTS = {
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe", "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
}
EXACT_OUT = {"buy", "buy_v2", "buy_v3"}
EXACT_IN = {"buy_exact_sol_in", "buy_exact_quote_in_v2", "buy_exact_quote_in_v3"}
SELLS = {"sell", "sell_v2", "sell_v3"}


def pre_state(ev: dict) -> CurveState:
    vq, vt = ev["virtual_quote_reserves"] or ev["virtual_sol_reserves"], ev["virtual_token_reserves"]
    rq, rt = ev["real_quote_reserves"] or ev["real_sol_reserves"], ev["real_token_reserves"]
    q, t = ev["quote_amount"] or ev["sol_amount"], ev["token_amount"]
    if ev["is_buy"]:
        return CurveState(vt + t, vq - q, rt + t, rq - q, has_creator=ev["creator"] != ZERO)
    return CurveState(vt - t, vq + q, rt - t, rq + q, has_creator=ev["creator"] != ZERO)


def legacy_exact_in_tokens(s: CurveState, budget: int, fees: Fees) -> int:
    """pump-sdk getBuyTokenAmountFromSolAmount (older formula), kept as an alternative hypothesis."""
    inp = (budget - 1) * 10_000 // (fees.protocol_bps + fees.creator_bps + 10_000)
    return min(tokens_for_quote_in(inp, s.virtual_token_reserves, s.virtual_quote_reserves), s.real_token_reserves)


def check(name: str, args: dict, ev: dict) -> dict:
    s = pre_state(ev)
    q = ev["quote_amount"] or ev["sol_amount"]
    sched = FeeSchedule.flat(ev["fee_basis_points"], ev["creator_fee_basis_points"])
    res = {"ix": name, "is_buy": ev["is_buy"], "mayhem": ev["mayhem_mode"],
           "sol_quote": ev["quote_mint"] == ZERO or ev["quote_mint"] == "So11111111111111111111111111111111111111112",
           "fee_bps": ev["fee_basis_points"], "creator_bps": ev["creator_fee_basis_points"]}
    if name in SELLS:
        r = sell_exact_tokens_in(s, args["amount"], sched)
    elif name in EXACT_OUT:
        r = buy_exact_tokens_out(s, args["amount"], sched)
    elif name in EXACT_IN:
        budget = args.get("spendable_sol_in") or args.get("spendable_quote_in")
        fees = s.fees(sched)
        net = exact_quote_in_net(budget, fees)
        t_v3 = min(tokens_for_quote_in(net - 1, s.virtual_token_reserves, s.virtual_quote_reserves), s.real_token_reserves)
        t_legacy = legacy_exact_in_tokens(s, budget, fees)
        res["exact_in_hypotheses"] = {
            "tokens_v3_formula": t_v3 == ev["token_amount"],
            "tokens_legacy_formula": t_legacy == ev["token_amount"],
            "charged_exact_out_price": quote_for_tokens_out(ev["token_amount"], s.virtual_token_reserves, s.virtual_quote_reserves) == q,
            "charged_full_net": net == q,
        }
        res["budget_unspent"] = budget - (q + ev["fee"] + ev["creator_fee"])
        r = buy_exact_quote_in(s, budget, sched)
    else:
        return {**res, "skipped": "unsupported ix"}
    res["match"] = {
        "tokens": r.tokens == ev["token_amount"],
        "quote_curve": r.quote_curve == q,
        "protocol_fee": r.protocol_fee == ev["fee"],
        "creator_fee": r.creator_fee == ev["creator_fee"],
        "post_virtual_reserves": (r.after.virtual_quote_reserves, r.after.virtual_token_reserves)
        == ((ev["virtual_quote_reserves"] or ev["virtual_sol_reserves"]), ev["virtual_token_reserves"]),
    }
    res["all_match"] = all(res["match"].values())
    if ev["buyback_fee_basis_points"]:
        res["buyback_is_half_protocol_floor"] = ev["buyback_fee"] == ev["fee"] * ev["buyback_fee_basis_points"] // 10_000
        res["buyback_is_half_protocol_ceil"] = ev["buyback_fee"] == fee_amount(ev["fee"], ev["buyback_fee_basis_points"])
    res["schedule_matches_verified"] = (
        s.fees(VERIFIED.fee_schedule).protocol_bps == ev["fee_basis_points"]
        and s.fees(VERIFIED.fee_schedule).creator_bps == ev["creator_fee_basis_points"]
    )
    return res


def lamport_flows(tx: dict) -> dict:
    """Fee payer's network fee and SOL sent to Jito tip accounts in this transaction."""
    meta = tx["meta"]
    keys = list(tx["transaction"]["message"]["accountKeys"])
    keys += (meta.get("loadedAddresses") or {}).get("writable", [])
    keys += (meta.get("loadedAddresses") or {}).get("readonly", [])
    tip = sum(post - pre for k, pre, post in zip(keys, meta["preBalances"], meta["postBalances"]) if k in JITO_TIP_ACCOUNTS)
    return {"network_fee": meta["fee"], "jito_tip": max(tip, 0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    ap.add_argument("--tx", type=int, default=300, help="successful transactions to fetch")
    args = ap.parse_args()
    rpc = Rpc(args.rpc, min_interval=0.35)

    sigs, before, ok = [], None, []
    while len(ok) < args.tx:
        page = rpc.signatures(PUMP_PROGRAM_ID, limit=1000, before=before)
        if not page:
            break
        sigs += page
        ok += [s for s in page if s.get("err") is None]
        before = page[-1]["signature"]
    ok = ok[: args.tx]
    window = (min(s["blockTime"] for s in sigs if s.get("blockTime")), max(s["blockTime"] for s in sigs if s.get("blockTime")))

    results, skipped, flows = [], Counter(), []
    for i, s in enumerate(ok):
        tx = rpc.transaction(s["signature"])
        if not tx or tx["meta"].get("err"):
            skipped["unavailable"] += 1
            continue
        evs = [e for n, e in events_from_transaction(tx) if n == "TradeEvent"]
        ixs = [(n, a) for n, a, _ in pump_instructions(tx) if not n.startswith("create")]
        if not evs:
            skipped["no_trade_event"] += 1
            continue
        if len(evs) != len(ixs):
            skipped["ix_event_count_mismatch(multi-hop/CPI variants)"] += 1
            continue
        flows.append(lamport_flows(tx))
        for (name, a), ev in zip(ixs, evs):
            try:
                r = check(name, a, ev)
            except Exception as e:  # report, never hide
                r = {"ix": name, "error": repr(e)}
            r["signature"] = s["signature"]
            results.append(r)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(ok)} transactions")

    checked = [r for r in results if "all_match" in r]
    sol = [r for r in checked if r["sol_quote"] and not r["mayhem"]]
    exin = [r for r in checked if "exact_in_hypotheses" in r]
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rpc": args.rpc,
        "signature_window_unix": window,
        "signatures_scanned": len(sigs),
        "signatures_failed": sum(1 for s in sigs if s.get("err") is not None),
        "transactions_fetched": len(ok),
        "skipped": dict(skipped),
        "trades_checked": len(checked),
        "trades_all_match": sum(r["all_match"] for r in checked),
        "by_instruction": {k: {"n": sum(1 for r in checked if r["ix"] == k),
                               "all_match": sum(1 for r in checked if r["ix"] == k and r["all_match"])}
                           for k in sorted({r["ix"] for r in checked})},
        "sol_paired_non_mayhem": {"n": len(sol), "all_match": sum(r["all_match"] for r in sol),
                                  "fee_schedule_matches_verified": sum(r["schedule_matches_verified"] for r in sol)},
        "mayhem": {"n": sum(1 for r in checked if r["mayhem"]),
                   "all_match": sum(1 for r in checked if r["mayhem"] and r["all_match"])},
        "exact_in_hypotheses": {h: sum(r["exact_in_hypotheses"][h] for r in exin) for h in
                                (exin[0]["exact_in_hypotheses"] if exin else {})} | {"n": len(exin)},
        "exact_in_budget_unspent_lamports": sorted(r["budget_unspent"] for r in exin)[:50],
        "buyback": {"n": sum(1 for r in checked if "buyback_is_half_protocol_floor" in r),
                    "floor_half": sum(1 for r in checked if r.get("buyback_is_half_protocol_floor")),
                    "ceil_half": sum(1 for r in checked if r.get("buyback_is_half_protocol_ceil"))},
        "errors": [r for r in results if "error" in r][:20],
        "mismatch_categories": {
            f"mayhem={m} zero_fee={z}": sum(1 for r in checked if not r["all_match"] and r["mayhem"] == m
                                            and (r["fee_bps"] == 0) == z)
            for m in (True, False) for z in (True, False)
        },
        "mismatches": [r for r in checked if not r["all_match"]][:20],
        "execution_costs_sample": {
            "n_tx": len(flows),
            "network_fee_lamports_median": statistics.median(f["network_fee"] for f in flows) if flows else None,
            "network_fee_lamports_p90": sorted(f["network_fee"] for f in flows)[int(0.9 * len(flows))] if flows else None,
            "share_with_jito_tip": sum(1 for f in flows if f["jito_tip"] > 0) / len(flows) if flows else None,
            "jito_tip_lamports_median_when_present": statistics.median([f["jito_tip"] for f in flows if f["jito_tip"] > 0] or [0]),
        },
    }
    OUT.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("mismatches", "errors", "exact_in_budget_unspent_lamports")}, indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
