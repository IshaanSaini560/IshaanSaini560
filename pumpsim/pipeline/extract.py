"""Turn one raw transaction into dataset rows for a given launch (mint)."""
from __future__ import annotations

import json

from ..onchain.decode import events_from_transaction, failure_info, pump_instructions
from ..onchain.layouts import PUMP_PROGRAM_ID

ZERO = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
SOL_QUOTES = {ZERO, WSOL}
# Verified 2026-10-31 via Jito block engine getTipAccounts.
JITO_TIP_ACCOUNTS = frozenset({
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe", "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
})
TRADE_IX = {"buy", "buy_v2", "buy_v3", "buy_exact_sol_in", "buy_exact_quote_in_v2", "buy_exact_quote_in_v3",
            "sell", "sell_v2", "sell_v3", "multi_hop_curve_swap"}


def account_keys(tx: dict) -> list[str]:
    keys = list(tx["transaction"]["message"]["accountKeys"])
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + loaded.get("writable", []) + loaded.get("readonly", [])


def _arg_amount_limit(name: str, args: dict) -> tuple[int | None, int | None]:
    if name in ("buy", "buy_v2", "buy_v3"):
        return args.get("amount"), args.get("max_sol_cost")
    if name == "buy_exact_sol_in":
        return args.get("spendable_sol_in"), args.get("min_tokens_out")
    if name in ("buy_exact_quote_in_v2", "buy_exact_quote_in_v3"):
        return args.get("spendable_quote_in"), args.get("min_tokens_out")
    if name in ("sell", "sell_v2", "sell_v3"):
        return args.get("amount"), args.get("min_sol_output")
    return None, None


def extract(tx: dict, mint: str, slot: int, tx_index: int | None, order_key: int) -> dict:
    """Rows for tables transactions / trades / failed_attempts plus launch-level events."""
    meta = tx.get("meta") or {}
    msg = tx["transaction"]["message"]
    keys = account_keys(tx)
    sig = tx["transaction"]["signatures"][0]
    n_sig = msg["header"]["numRequiredSignatures"]
    signers = keys[:n_sig]
    failed = meta.get("err") is not None
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    tip = sum(max(0, b - a) for k, a, b in zip(keys, pre, post) if k in JITO_TIP_ACCOUNTS)
    ixs = [(n, a, accts) for n, a, accts in pump_instructions(tx) if mint in accts]
    fi = failure_info(meta.get("logMessages")) if failed else {"program": None, "name": None, "pump_reached": None}
    block_time = tx.get("blockTime")
    txrow = {
        "signature": sig, "mint": mint, "slot": slot, "tx_index": tx_index, "order_key": order_key,
        "block_time": block_time, "failed": int(failed), "err": json.dumps(meta.get("err")) if failed else None,
        "fail_program": fi["program"], "fail_name": fi["name"],
        "pump_reached": None if fi["pump_reached"] is None else int(fi["pump_reached"]),
        "fee": meta.get("fee"), "n_signatures": n_sig, "cu_consumed": meta.get("computeUnitsConsumed"),
        "fee_payer": keys[0], "signers": ",".join(signers), "jito_tip": tip,
        "payer_sol_delta": (post[0] - pre[0]) if pre and post else None,
        "version": str(tx.get("version")), "pump_ix": ",".join(n for n, _, _ in ixs),
    }
    out = {"tx": txrow, "trades": [], "failed_attempts": [], "create": None, "complete": False, "migration": None}
    if failed:
        for seq, (n, a, _) in enumerate(i for i in ixs if i[0] in TRADE_IX):
            amt, lim = _arg_amount_limit(n, a)
            out["failed_attempts"].append({
                "signature": sig, "seq": seq, "mint": mint, "slot": slot, "block_time": block_time,
                "fee_payer": keys[0], "ix_name": n, "arg_amount": amt, "arg_limit": lim,
                "fail_program": fi["program"], "fail_name": fi["name"], "fee_paid": meta.get("fee"),
            })
        return out
    events = events_from_transaction(tx)
    trade_ixs = [(n, a) for n, a, _ in ixs if n in TRADE_IX]
    trade_evs = [e for n, e in events if n == "TradeEvent" and e["mint"] == mint]
    paired = len(trade_ixs) == len(trade_evs)
    for seq, ev in enumerate(trade_evs):
        ix_name = trade_ixs[seq][0] if paired else ev["ix_name"]
        amt, lim = _arg_amount_limit(*trade_ixs[seq]) if paired else (None, None)
        out["trades"].append({
            "signature": sig, "seq": seq, "mint": mint, "slot": slot, "tx_index": tx_index, "order_key": order_key,
            "block_time": ev["timestamp"], "user": ev["user"], "fee_payer": keys[0], "ix_name": ix_name,
            "event_ix_name": ev["ix_name"],  # the program's own label; differs from the instruction name for some variants
            "is_buy": int(ev["is_buy"]),
            "quote_amount": ev["quote_amount"] or ev["sol_amount"], "token_amount": ev["token_amount"],
            "fee": ev["fee"], "creator_fee": ev["creator_fee"], "buyback_fee": ev["buyback_fee"],
            "fee_bps": ev["fee_basis_points"], "creator_fee_bps": ev["creator_fee_basis_points"],
            "v_quote": ev["virtual_quote_reserves"] or ev["virtual_sol_reserves"], "v_token": ev["virtual_token_reserves"],
            "r_quote": ev["real_quote_reserves"] or ev["real_sol_reserves"], "r_token": ev["real_token_reserves"],
            "arg_amount": amt, "arg_limit": lim, "mayhem": int(bool(ev["mayhem_mode"])), "sim_match": None,
        })
    for n, e in events:
        if n == "CreateEvent" and e["mint"] == mint:
            out["create"] = e
        elif n == "CompleteEvent" and e["mint"] == mint:
            out["complete"] = True
        elif n == "CompletePumpAmmMigrationEvent" and e["mint"] == mint:
            out["migration"] = e
    return out
