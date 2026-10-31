"""Bundled vs solo launches: all-in P&L of the creator + same-bundle wallets, and outside demand.

"Bundle-like" wallets = non-creator wallets buying in the creation transaction or in the unbroken run of
successful transactions on this curve immediately after it in the same slot (Jito bundles execute
contiguously). This is an INFERENCE of coordination, not proof of common ownership: an independent
sniper's own bundle can also land right after. Funding-graph analysis is needed to confirm.

All-in group P&L = SOL received from sells + executable curve value of tokens still held at the horizon
(capped at tokens outstanding) - SOL paid for buys - network fees and Jito tips on transactions the group
paid for (failed ones too) - coin-creation overhead (account rent etc.) + accrued creator fees.
Usage: python -m scripts.bundle_report --db data/pipeline/launches.sqlite --out docs/generated/bundles_1000.md
"""
from __future__ import annotations

import argparse
import math
import statistics

from pumpsim.curve import CurveState, FeeSchedule, sell_exact_tokens_in
from pumpsim.pipeline import db

L = 1e9


def wilson(k: int, n: int) -> str:
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return f"{k}/{n} = {p:.0%} (95% CI {max(0, c - h):.0%}–{min(1, c + h):.0%})"


def q(xs: list[float], fmt: str = "+.3f") -> str:
    xs = sorted(xs)
    a, m, b = statistics.quantiles(xs, n=4)
    return f"median {m:{fmt}} (IQR {a:{fmt}} to {b:{fmt}})"


def analyse(con) -> dict:
    out: dict = {"bundle": [], "solo": [], "n_std": 0, "no_buy": 0}
    for l in con.execute("SELECT * FROM launches WHERE status='collected' AND is_sol_quote=1 AND is_mayhem=0 "
                         "AND complete_time IS NULL"):
        out["n_std"] += 1
        creators = {l["creator"], l["create_user"]}
        succ = {r["tx_index"]: r["signature"] for r in con.execute(
            "SELECT signature, tx_index FROM transactions WHERE mint=? AND slot=? AND failed=0", (l["mint"], l["create_slot"]))}
        contiguous, nxt = {l["create_signature"]}, l["create_tx_index"]
        while nxt is not None and (nxt + 1) in succ:
            nxt += 1
            contiguous.add(succ[nxt])
        trades = con.execute("SELECT * FROM trades WHERE mint=? ORDER BY slot, COALESCE(tx_index, order_key), seq",
                             (l["mint"],)).fetchall()
        bundle_w = {t["user"] for t in trades if t["is_buy"] and t["signature"] in contiguous and t["user"] not in creators}
        group = creators | bundle_w
        paid = sum(t["quote_amount"] + t["fee"] + t["creator_fee"] for t in trades if t["user"] in group and t["is_buy"])
        if paid == 0:
            out["no_buy"] += 1
            continue
        recv = sum(t["quote_amount"] - t["fee"] - t["creator_fee"] for t in trades if t["user"] in group and not t["is_buy"])
        held = sum((t["token_amount"] if t["is_buy"] else -t["token_amount"]) for t in trades if t["user"] in group)
        outstanding = l["init_real_token"] - trades[-1]["r_token"]
        liq = 0
        if min(held, outstanding) > 0:
            last = trades[-1]
            liq = sell_exact_tokens_in(CurveState(last["v_token"], last["v_quote"], last["r_token"], last["r_quote"]),
                                       min(held, outstanding),
                                       FeeSchedule.flat(last["fee_bps"], last["creator_fee_bps"])).user_quote_delta
        net = con.execute(f"SELECT COALESCE(SUM(fee),0) + COALESCE(SUM(jito_tip),0) FROM transactions WHERE mint=? AND "
                          f"fee_payer IN ({','.join('?' * len(group))})", (l["mint"], *group)).fetchone()[0]
        ct = con.execute("SELECT payer_sol_delta, fee, jito_tip, fee_payer FROM transactions WHERE mint=? AND signature=?",
                         (l["mint"], l["create_signature"])).fetchone()
        overhead = 0
        if ct:
            own = sum(t["quote_amount"] + t["fee"] + t["creator_fee"] for t in trades
                      if t["signature"] == l["create_signature"] and t["is_buy"] and t["user"] == ct["fee_payer"])
            overhead = max(0, -(ct["payer_sol_delta"] or 0) - own - (ct["fee"] or 0) - (ct["jito_tip"] or 0))
        fees_in = 0 if l["is_holder_reward"] else sum(t["creator_fee"] for t in trades)
        ext = [t for t in trades if t["user"] not in group]
        out["bundle" if bundle_w else "solo"].append({
            "paid": paid, "allin": recv + liq - paid - net - overhead + fees_in, "net": net, "overhead": overhead,
            "wallets": len(bundle_w), "ext_buy": sum(t["quote_amount"] for t in ext if t["is_buy"]),
            "ext_net": sum((t["quote_amount"] if t["is_buy"] else -t["quote_amount"]) for t in ext),
            "ext_buyers": len({t["user"] for t in ext if t["is_buy"]}),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    res = analyse(db.connect(a.db))
    lines = [f"Standard (SOL-paired, non-mayhem), non-graduated launches: {res['n_std']}; "
             f"no buy by creator or bundle wallets: {res['no_buy']}.\n"]
    for k, name in (("bundle", "Bundle-like: creator + same-bundle wallets as one group"),
                    ("solo", "Solo: creator buys, no bundle-like wallets")):
        R = res[k]
        n = len(R)
        lines += [f"## {name} (n = {n})\n",
                  f"- Group SOL deployed: {q([r['paid'] / L for r in R], '.2f')}"
                  + (f"; bundle wallets per launch: {q([r['wallets'] for r in R], '.0f')}" if k == "bundle" else ""),
                  f"- Network fees + Jito tips paid by the group: {q([r['net'] / L for r in R], '.4f')} SOL; "
                  f"coin-creation overhead: {q([r['overhead'] / L for r in R], '.4f')} SOL",
                  f"- **All-in group P&L** (incl. accrued creator fees): {q([r['allin'] / L for r in R])} SOL; "
                  f"positive {wilson(sum(r['allin'] > 0 for r in R), n)}",
                  f"- All-in P&L as % of SOL deployed: {q([100 * r['allin'] / r['paid'] for r in R], '+.1f')} %",
                  f"- Buying by wallets outside the group: {q([r['ext_buy'] / L for r in R], '.2f')} SOL from "
                  f"{q([r['ext_buyers'] for r in R], '.0f')} wallets",
                  f"- Sum over the {n} launches: group all-in P&L {sum(r['allin'] for r in R) / L:+.2f} SOL; outside "
                  f"wallets' net SOL still in the curve at the horizon {sum(r['ext_net'] for r in R) / L:+.2f} SOL "
                  "(their unrealised position; the curve is zero-sum apart from fees)\n"]
    text = "\n".join(lines)
    if a.out:
        with open(a.out, "w") as f:
            f.write("# Bundled vs solo launches\n\nGenerated by `python -m scripts.bundle_report`. VERIFIED on-chain "
                    "trades; 'bundle-like' is an inference of coordination, not proof of common ownership. "
                    "Descriptive, not causal.\n\n" + text)
    print(text)


if __name__ == "__main__":
    main()
