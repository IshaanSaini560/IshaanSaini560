"""Descriptive statistics for a collected sample (VERIFIED on-chain data; small-sample caveats apply).

Usage: python -m scripts.pilot_report --db data/pipeline/pilot_free.sqlite [--out docs/generated/pilot_free.md]
Proportions carry Wilson 95 % intervals. Nothing here is a population estimate beyond the sampled windows.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone

L = 1e9


def wilson(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return f"{k}/{n} = {p:.0%} (95% CI {max(0, c - h):.0%}–{min(1, c + h):.0%})"


def q(xs: list[float]) -> str:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return "n/a"
    if len(xs) < 4:
        return ", ".join(f"{x:.3g}" for x in xs)
    qs = statistics.quantiles(xs, n=4)
    return f"median {qs[1]:.3g} (IQR {qs[0]:.3g}–{qs[2]:.3g}; min {xs[0]:.3g}, max {xs[-1]:.3g})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    from pumpsim.pipeline import db as pdb

    con = pdb.connect(a.db)  # applies schema migrations (e.g. the not_creation column) to older databases
    out: list[str] = []
    p = out.append
    W = con.execute("SELECT * FROM frame_windows ORDER BY t_start").fetchall()
    good = [w for w in W if w["complete"]]
    p("## Sampling frame\n")
    for w in W:
        ts = datetime.fromtimestamp(w["t_start"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        p(f"- {ts} UTC, {(w['t_end'] - w['t_start']) // 60} min: "
          + (f"{w['n_success']} creations, {w['n_failed']} failed creation txs" if w["complete"]
             else "skipped (outside source history)"))
    tot_s = sum(w["n_success"] for w in good)
    tot_f = sum(w["n_failed"] for w in good)
    mins = sum((w["t_end"] - w["t_start"]) / 60 for w in good)
    sel = con.execute("SELECT COUNT(*), COALESCE(SUM(not_creation), 0) FROM frame_creations WHERE selected=1").fetchone()
    nc_share = sel[1] / sel[0] if sel[0] else 0
    p(f"\nUsable frame: {tot_s} successful frame transactions in {mins:.0f} min ({tot_s / mins * 60:,.0f}/hour); "
      f"failed {wilson(tot_f, tot_s + tot_f)}. Among sampled frame transactions, {sel[1]}/{sel[0]} were not coin "
      f"creations (PumpSwap pool creations referencing the same PDA), so the creation rate is "
      f"~{tot_s * (1 - nc_share) / mins * 60:,.0f}/hour.\n")

    rows = con.execute("SELECT l.*, s.data FROM launches l JOIN launch_summary s USING (mint) WHERE l.status='collected'").fetchall()
    S = [(r, json.loads(r["data"])) for r in rows]
    n = len(S)
    std = [(r, d) for r, d in S if r["is_sol_quote"] and not r["is_mayhem"]]
    p("## Sample composition\n")
    p(f"- Launches: {n} (simple random sample, seed recorded in frame_creations)")
    p(f"- Mayhem mode: {wilson(sum(r['is_mayhem'] for r, _ in S), n)}")
    p(f"- Not SOL-quoted: {wilson(sum(not r['is_sol_quote'] for r, _ in S), n)} "
      f"(quote mints: {dict(Counter(r['quote_mint'][:6] for r, _ in S if not r['is_sol_quote']))})")
    p(f"- Standard (SOL-paired, non-mayhem): {len(std)}")
    p(f"- Truncated at max-tx: {sum(r['truncated'] for r, _ in S)}")
    tr = con.execute("SELECT COUNT(*), SUM(sim_match) FROM trades WHERE sim_match IS NOT NULL").fetchone()
    p(f"- Simulator replay on standard coins: {tr[1]}/{tr[0]} trades exact\n")

    p("## Transactions touching the curve (all 40 launches)\n")
    tx = con.execute("SELECT COUNT(*), SUM(failed), SUM(failed AND pump_reached=0), "
                     "SUM(failed AND fail_program='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P') FROM transactions").fetchone()
    p(f"- {tx[0]:,} transactions; failed {wilson(tx[1], tx[0])}")
    p(f"- Of the failures: never reached Pump {tx[2]:,}; failed inside Pump {tx[3]:,}")
    p("- Top failure reasons: " + "; ".join(
        f"{(r[0] or '?')[:8]}… {r[1]} ×{r[2]:,}" for r in con.execute(
            "SELECT fail_program, fail_name, COUNT(*) c FROM transactions WHERE failed=1 GROUP BY 1,2 ORDER BY c DESC LIMIT 6")))
    per = [d["n_failed_tx"] / d["n_tx"] for _, d in S if d["n_tx"]]
    p(f"- Failed share per launch: {q(per)}")
    conc = con.execute("SELECT mint, COUNT(*) c FROM transactions WHERE failed=1 GROUP BY mint ORDER BY c DESC").fetchall()
    top3 = sum(r["c"] for r in conc[:3])
    p(f"- Concentration: the 3 launches with most failures account for {top3:,}/{tx[1]:,} failed transactions\n")

    p(f"## Standard launches (n = {len(std)}), first {std[0][0]['horizon_s'] // 60} minutes\n" if std else "")
    if std:
        m = len(std)
        nb = [d["n_unique_noncreator_buyers"] for _, d in std]
        p(f"- Any non-creator buyer: {wilson(sum(x >= 1 for x in nb), m)}; ≥5: {wilson(sum(x >= 5 for x in nb), m)}; "
          f"≥20: {wilson(sum(x >= 20 for x in nb), m)}")
        p(f"- Unique non-creator buyers: {q(nb)}")
        vol = lambda d: sum(v for k, v in d.items() if k.startswith("noncreator_buy_quote_")) / L  # noqa: E731
        p(f"- Non-creator buy volume, SOL: {q([vol(d) for _, d in std])}")
        early = [(d["noncreator_buy_quote_0_5s"] + d["noncreator_buy_quote_5_30s"]) / L for _, d in std]
        p(f"- …of which in the first 30 s: {q(early)}")
        p(f"- Peak market cap, SOL: {q([d['max_mcap'] / L for _, d in std if d['max_mcap']])}")
        p(f"- Peak ≥ 2× start (≥ 55.9 SOL): {wilson(sum((d['max_mcap'] or 0) >= 2 * d['start_mcap'] for _, d in std if d['start_mcap']), m)}")
        p(f"- Completed the curve (graduated) within horizon: {wilson(sum(d['completed'] for _, d in std), m)}")
        p(f"- Final / peak market cap: {q([d['final_over_max'] for _, d in std])}")
        p(f"- Time to peak, s: {q([d['time_to_max_mcap_s'] for _, d in std])}\n")
        grad = [(r, d) for r, d in std if d["completed"]]
        for r, d in grad:
            p(f"- Graduated: `{r['mint']}`: creator paid {d['creator_quote_paid'] / L:.2f} SOL in the creation tx and bought "
              f"{d['creator_tokens_bought'] / 1e6:,.0f} tokens, completing the curve at t = {d['time_to_complete_s']} s; "
              f"{d['n_unique_noncreator_buyers']} other curve buyers. Trading continues on PumpSwap (not collected), so this "
              "launch is excluded from the creator statistics below.")
        std = [(r, d) for r, d in std if not d["completed"]]
        m = len(std)
        p(f"\n### Creator address only, non-graduated standard launches (n = {m})\n")
        p("Other wallets the creator may control are NOT included.\n")
        ib = [d["creator_initial_buy_paid"] / L for _, d in std]
        p(f"- Initial buy in creation tx, SOL: {q(ib)}; no initial buy: {sum(x == 0 for x in ib)}")
        sold = [d for _, d in std if d["creator_tokens_sold"]]
        p(f"- Creator sold within horizon: {wilson(len(sold), m)}; first sell, s: {q([d['creator_first_sell_s'] for d in sold])}")
        p(f"- Creator net cash (received − paid), SOL: {q([d['creator_net_cash'] / L for _, d in std])}")
        p(f"- Realised P&L (avg cost), SOL: {q([d['creator_realised_pnl_avg_cost'] / L for _, d in std])}")
        unr = [d["creator_unrealised_pnl_mark_to_curve"] for _, d in std if d["creator_unrealised_pnl_mark_to_curve"] is not None]
        p(f"- Unrealised P&L marked to executable curve value, SOL (n={len(unr)}): {q([x / L for x in unr])}")
        comb = [(d["creator_realised_pnl_avg_cost"] + (d["creator_unrealised_pnl_mark_to_curve"] or 0)) / L
                for _, d in std if d["creator_quote_paid"] and (d["creator_curve_net_tokens"] == 0 or d["creator_unrealised_pnl_mark_to_curve"] is not None)]
        bought = [d for _, d in std if d["creator_quote_paid"]]
        excl = Counter(d["creator_mark_to_curve_unavailable_reason"] for d in bought
                       if d["creator_curve_net_tokens"] and d["creator_unrealised_pnl_mark_to_curve"] is None)
        p(f"- Realised + mark-to-curve unrealised, creators who bought (n={len(comb)} of {len(bought)}): {q(comb)}; "
          f"positive {wilson(sum(x > 0 for x in comb), len(comb))}"
          + (f". Excluded: {dict(excl)}" if excl else ""))
        moved = [(r, d) for r, d in std if d.get("creator_tokens_moved_off_min")]
        p(f"- Creator address's curve balance exceeds all tokens outstanding (so tokens were transferred to other "
          f"wallets): {wilson(len(moved), m)}" + ("; " + ", ".join(f"`{r['mint'][:8]}…` ≥ {d['creator_tokens_moved_off_min'] / 1e12:,.1f} M tokens"
                                                     for r, d in moved) if moved else ""))
        p(f"- Accrued creator fees, SOL: {q([d['creator_fee_accrued'] / L for _, d in std])} (accrued, not necessarily claimed)")
    text = "\n".join(out) + "\n"
    if a.out:
        with open(a.out, "w") as f:
            f.write(f"# Pilot report: `{a.db}`\n\nGenerated by `python -m scripts.pilot_report`. VERIFIED on-chain data "
                    "for the sampled launches only; small-sample descriptive statistics, not population estimates.\n\n" + text)
    print(text)


if __name__ == "__main__":
    main()
