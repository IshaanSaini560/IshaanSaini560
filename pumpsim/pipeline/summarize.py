"""Per-launch metrics derived from the collected on-chain rows.

Conventions (read before using the numbers):
* All quote amounts are in base units (lamports for SOL coins).
* "creator" means ONLY the CreateEvent creator and the creation signer. Other wallets the creator
  may control are NOT included; wallet clustering is a later, probabilistic step.
* "non-creator" traders are addresses other than those two. They are not proven independent.
* Creator positions are reconstructed from curve trades only. Tokens moved by plain transfers are
  invisible here, so `creator_curve_net_tokens` is a curve-flow balance, not a wallet balance.
* realised P&L uses average cost. Unrealised value is the simulator's executable liquidation value
  of the remaining curve-flow balance at the end of the horizon ("mark to curve"), net of platform
  fees, NOT tokens x displayed price. It is computed only for standard SOL-paired, non-mayhem,
  not-completed curves; otherwise it is None.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from ..curve import CurveState, FeeSchedule, sell_exact_tokens_in

BUCKETS = [(0, 5), (5, 30), (30, 120), (120, 600), (600, 3600), (3600, 10**9)]


def _mcap(v_quote: int, v_token: int, supply: int) -> int:
    return v_quote * supply // v_token if v_token else 0


def summarize_launch(con: sqlite3.Connection, mint: str) -> dict:
    L = con.execute("SELECT * FROM launches WHERE mint=?", (mint,)).fetchone()
    trades = con.execute("SELECT * FROM trades WHERE mint=? ORDER BY slot, COALESCE(tx_index, order_key), order_key, seq",
                         (mint,)).fetchall()
    txs = con.execute("SELECT * FROM transactions WHERE mint=?", (mint,)).fetchall()
    supply, t0 = L["token_total_supply"], L["create_time"]
    creators = {L["creator"], L["create_user"]}
    s: dict = {k: L[k] for k in ("mint", "create_time", "is_mayhem", "is_sol_quote", "is_holder_reward",
                                 "horizon_s", "truncated", "n_tx", "n_failed_tx", "n_trades")}
    s["sim_matched"], s["sim_checked"] = L["sim_matched"], L["sim_checked"]

    # --- creation and same-slot activity
    init = [t for t in trades if t["signature"] == L["create_signature"] and t["is_buy"]]
    s["creator_initial_buy_paid"] = sum(t["quote_amount"] + t["fee"] + t["creator_fee"] for t in init if t["user"] in creators)
    s["creator_initial_tokens"] = sum(t["token_amount"] for t in init if t["user"] in creators)
    s["creator_initial_pct_supply"] = s["creator_initial_tokens"] / supply if supply else None
    same_slot = [t for t in trades if t["slot"] == L["create_slot"] and t["is_buy"] and t["user"] not in creators]
    s["create_slot_other_buyers"] = len({t["user"] for t in same_slot})
    s["create_slot_other_buy_quote"] = sum(t["quote_amount"] for t in same_slot)
    early = [t for t in trades if t["slot"] <= L["create_slot"] + 2 and t["is_buy"] and t["user"] not in creators]
    s["first3slots_other_buyers"] = len({t["user"] for t in early})
    s["first3slots_other_buy_quote"] = sum(t["quote_amount"] for t in early)

    # --- activity
    buys = [t for t in trades if t["is_buy"]]
    sells = [t for t in trades if not t["is_buy"]]
    s["n_buys"], s["n_sells"] = len(buys), len(sells)
    s["n_unique_noncreator_buyers"] = len({t["user"] for t in buys if t["user"] not in creators})
    s["n_unique_noncreator_sellers"] = len({t["user"] for t in sells if t["user"] not in creators})
    s["buy_quote_volume"] = sum(t["quote_amount"] for t in buys)
    s["sell_quote_volume"] = sum(t["quote_amount"] for t in sells)
    for a, b in BUCKETS:
        tag = f"{a}_{b if b < 10**9 else 'end'}s"
        sel = [t for t in trades if a <= t["block_time"] - t0 < b and t["user"] not in creators]
        s[f"noncreator_buy_quote_{tag}"] = sum(t["quote_amount"] for t in sel if t["is_buy"])
        s[f"noncreator_sell_quote_{tag}"] = sum(t["quote_amount"] for t in sel if not t["is_buy"])

    # --- price path (keys always present so every summary has the same columns)
    for k in ("start_mcap", "max_mcap", "time_to_max_mcap_s", "final_mcap", "final_over_max", "max_progress",
              "final_progress", "last_trade_s"):
        s[k] = None
    if trades:
        mc = [(_mcap(t["v_quote"], t["v_token"], supply), t["block_time"] - t0) for t in trades]
        peak = max(mc, key=lambda x: x[0])
        s["start_mcap"] = _mcap(L["init_virtual_quote"], L["init_virtual_token"], supply)
        s["max_mcap"], s["time_to_max_mcap_s"] = peak
        s["final_mcap"] = mc[-1][0]
        s["final_over_max"] = mc[-1][0] / peak[0] if peak[0] else None
        s["max_progress"] = max(1 - t["r_token"] / L["init_real_token"] for t in trades)
        s["final_progress"] = 1 - trades[-1]["r_token"] / L["init_real_token"]
        s["last_trade_s"] = trades[-1]["block_time"] - t0
    s["completed"] = int(L["complete_time"] is not None)
    s["time_to_complete_s"] = L["complete_time"] - t0 if L["complete_time"] else None
    s["migrated"] = int(L["migrate_time"] is not None)

    # --- creator flows (exact addresses only)
    held = cost = 0  # tokens and total cost basis (quote paid incl. fees)
    paid = received = sold_tokens = bought_tokens = 0
    realised = 0
    max_held = 0
    first_sell = first_substantial = None
    for t in trades:
        if t["user"] not in creators:
            continue
        if t["is_buy"]:
            p = t["quote_amount"] + t["fee"] + t["creator_fee"]
            paid += p
            cost += p
            held += t["token_amount"]
            bought_tokens += t["token_amount"]
            max_held = max(max_held, held)
        else:
            r = t["quote_amount"] - t["fee"] - t["creator_fee"]
            received += r
            amt = min(t["token_amount"], held) if held else t["token_amount"]
            basis = cost * amt // held if held else 0
            realised += r - basis
            if first_sell is None:
                first_sell = t["block_time"] - t0
            if first_substantial is None and held and t["token_amount"] >= 0.10 * held:
                first_substantial = t["block_time"] - t0
            cost -= basis
            held -= amt
            sold_tokens += t["token_amount"]
    s.update({
        "creator_quote_paid": paid, "creator_quote_received": received, "creator_net_cash": received - paid,
        "creator_tokens_bought": bought_tokens, "creator_tokens_sold": sold_tokens,
        "creator_curve_net_tokens": held, "creator_max_curve_tokens": max_held,
        "creator_first_sell_s": first_sell, "creator_first_substantial_sell_s": first_substantial,
        "creator_realised_pnl_avg_cost": realised, "creator_remaining_cost_basis": cost,
        "creator_fee_accrued": 0 if L["is_holder_reward"] else sum(t["creator_fee"] for t in trades),
    })
    liq, reason, moved_min = None, None, 0
    outstanding = L["init_real_token"] - trades[-1]["r_token"] if trades else 0  # tokens held by anyone, curve view
    if held > outstanding:
        # The creator address cannot hold more than everyone together: the excess left this address by
        # plain token transfer (and was likely sold from other wallets). Evidence for wallet clustering.
        moved_min = held - outstanding
        reason = "creator curve-flow balance exceeds all tokens outstanding: tokens were transferred out"
    elif held > 0 and L["complete_time"]:
        reason = "curve completed; position now trades on PumpSwap (not collected)"
    elif held > 0 and (L["is_mayhem"] or not L["is_sol_quote"]):
        reason = "mayhem / non-SOL curve not modelled by the simulator"
    elif held > 0 and trades:
        last = trades[-1]
        st = CurveState(last["v_token"], last["v_quote"], last["r_token"], last["r_quote"], supply)
        try:
            liq = sell_exact_tokens_in(st, held, FeeSchedule.flat(last["fee_bps"], last["creator_fee_bps"])).user_quote_delta
        except Exception as e:  # report, do not hide
            reason = f"simulator refused: {e}"
    s["creator_tokens_moved_off_min"] = moved_min
    s["creator_remaining_mark_to_curve_value"] = liq
    s["creator_unrealised_pnl_mark_to_curve"] = None if liq is None else liq - cost
    s["creator_mark_to_curve_unavailable_reason"] = reason

    # --- costs and failures
    s["protocol_fees"] = sum(t["fee"] for t in trades)
    s["creator_fees_total"] = sum(t["creator_fee"] for t in trades)
    s["network_fees_all_tx"] = sum(x["fee"] or 0 for x in txs)
    s["network_fees_failed_tx"] = sum(x["fee"] or 0 for x in txs if x["failed"])
    s["jito_tips_all_tx"] = sum(x["jito_tip"] or 0 for x in txs)
    s["n_failed_reached_pump"] = sum(1 for x in txs if x["failed"] and x["pump_reached"])
    s["n_failed_pump_error"] = sum(1 for x in txs if x["failed"] and x["fail_program"] == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
    s["top_failures"] = Counter(x["fail_name"] for x in txs if x["failed"]).most_common(5)
    return s


def summarize_all(con: sqlite3.Connection) -> int:
    n = 0
    for r in con.execute("SELECT mint FROM launches WHERE status='collected'").fetchall():
        con.execute("INSERT OR REPLACE INTO launch_summary VALUES (?, ?)",
                    (r["mint"], json.dumps(summarize_launch(con, r["mint"]))))
        n += 1
    con.commit()
    return n
