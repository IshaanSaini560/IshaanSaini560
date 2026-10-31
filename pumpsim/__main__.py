"""Command-line front end. Hypothetical calculations only; nothing here touches a wallet.

Examples
  python -m pumpsim roundtrip --sol 10 --wallets 5 --priority-lamports 20000
  python -m pumpsim external --sol 10 --external 0.5,1,2 --sell-before-exit 0.5
  python -m pumpsim liquidate --tokens 265758513.8939 --after-buys 10,5
  python -m pumpsim breakeven --sol 10 --creator
"""
from __future__ import annotations

import argparse

from .analysis import break_even_external_integer, external_demand_scenario, liquidation_value, round_trip
from .costs import ExecutionCosts
from .curve import CurveState, buy_exact_quote_in
from .params import VERIFIED

L = 10**9


def _costs(a) -> ExecutionCosts:
    return ExecutionCosts(priority_fee_per_tx=a.priority_lamports, jito_tip_per_tx=a.tip_lamports,
                          failure_rate=a.failure_rate, include_rent_as_cost=a.count_rent)


def main() -> None:
    ap = argparse.ArgumentParser(prog="pumpsim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--priority-lamports", type=int, default=0, help="priority fee per tx (assumption)")
        p.add_argument("--tip-lamports", type=int, default=0, help="Jito tip per tx (assumption)")
        p.add_argument("--failure-rate", type=float, default=0.0, help="share of attempts that fail and retry")
        p.add_argument("--count-rent", action="store_true", help="treat refundable rent as a cost")

    p = sub.add_parser("roundtrip", help="buy then immediately sell back, nobody else trades")
    p.add_argument("--sol", type=float, required=True)
    p.add_argument("--wallets", type=int, default=1)
    p.add_argument("--creator", action="store_true", help="operator is the coin creator (creator fees come back)")
    common(p)

    p = sub.add_parser("external", help="operator buys first, external buyers follow, operator exits")
    p.add_argument("--sol", type=float, required=True)
    p.add_argument("--external", required=True, help="comma-separated external buys in SOL, e.g. 0.5,1,2")
    p.add_argument("--sell-before-exit", type=float, default=0.0, help="fraction of external tokens sold before you exit")
    p.add_argument("--externals-exit-first", action="store_true")
    p.add_argument("--creator", action="store_true")
    common(p)

    p = sub.add_parser("liquidate", help="what selling X tokens right now actually returns")
    p.add_argument("--tokens", type=float, required=True, help="whole tokens")
    p.add_argument("--after-buys", default="", help="comma-separated SOL buys applied to a fresh curve first")
    p.add_argument("--cost-basis", type=float, default=None, help="SOL you paid, for P&L")
    common(p)

    p = sub.add_parser("breakeven", help="external buying needed for the operator to break even")
    p.add_argument("--sol", type=float, required=True)
    p.add_argument("--creator", action="store_true")
    common(p)

    a = ap.parse_args()
    c = _costs(a)
    if a.cmd == "roundtrip":
        rt = round_trip(VERIFIED, int(a.sol * L), a.wallets, c, a.creator)
        print(f"spent            {a.sol:.9f} SOL over {a.wallets} wallet(s)")
        print(f"tokens           {rt.tokens / 1e6:,.6f}")
        print(f"recovered        {rt.recovered / L:.9f} SOL")
        print(f"protocol fees    {rt.protocol_fees / L:.9f} SOL")
        print(f"creator fees     {rt.creator_fees / L:.9f} SOL (rebated to you: {rt.creator_rebate / L:.9f})")
        print(f"network costs    {rt.network / L:.9f} SOL")
        print(f"net loss         {rt.net_loss / L:.9f} SOL ({rt.net_loss / (a.sol * L):.3%})")
    elif a.cmd == "external":
        ext = [int(float(x) * L) for x in a.external.split(",") if x]
        r = external_demand_scenario(VERIFIED, int(a.sol * L), ext, a.sell_before_exit, not a.externals_exit_first,
                                     c, operator_is_creator=a.creator)
        for k, v in r.items():
            print(f"{k:38s} {v / L:.9f}" if isinstance(v, (int, float)) and k != "operator_exit_avg_price_vs_entry" else f"{k:38s} {v}")
    elif a.cmd == "liquidate":
        s = CurveState.fresh(VERIFIED)
        for x in (float(v) for v in a.after_buys.split(",") if v):
            s = buy_exact_quote_in(s, int(x * L), VERIFIED.fee_schedule).after
        lv = liquidation_value(s, int(a.tokens * 1e6), VERIFIED.fee_schedule, c,
                               None if a.cost_basis is None else int(a.cost_basis * L))
        for k, v in lv.items():
            print(f"{k:38s} {v:.6f}" if isinstance(v, float) and abs(v) < 10 else f"{k:38s} {v / L:.9f} SOL")
    elif a.cmd == "breakeven":
        x = break_even_external_integer(VERIFIED, int(a.sol * L), c, a.creator)
        print("not reachable before graduation" if x is None else f"external spend needed: {x / L:.6f} SOL "
              f"(held while you exit; you must exit before them)")


if __name__ == "__main__":
    main()
