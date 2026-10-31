"""Scenario analysis on top of the integer-exact curve.

Everything in this module is a HYPOTHETICAL CALCULATION on the verified curve
parameters. No function here reads or represents historical market data.

Notation used in docs/PHASE1_MATH_AND_MECHANICS.md:
    V = initial virtual quote reserves (30 SOL), T = initial virtual token reserves (1.073e9)
    N = SOL that enters the curve from the operator's buy (gross spend G = N * (1 + f_b))
    X = net SOL that external buyers add to the curve after the operator and still hold at exit
    f_b, f_s = total fee rate on buys / sells (currently 0.0125 each: 0.95 % protocol + 0.30 % creator)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .costs import FREE, ExecutionCosts
from .curve import (
    CurveParams, CurveState, FeeSchedule, SlippageExceeded, TradeResult, buy_exact_quote_in,
    quote_for_tokens_in, sell_exact_tokens_in,
)

# ---------------------------------------------------------------- closed forms (real-valued)


def cf_tokens_for_net(N: float, V: float, T: float) -> float:
    """Tokens out of a fresh curve for net quote N: T*N/(V+N)."""
    return T * N / (V + N)


def cf_round_trip_loss_fraction(f_b: float, f_s: float, creator_share: float = 0.0) -> float:
    """Fraction of the gross spend lost in buy -> immediately sell back (no other trades).

    Buy fees are charged on top of the curve amount, sell fees are deducted from it:
        recovered / spent = (1 - f_s) / (1 + f_b)
    If the trader is also the coin's creator, the creator-fee part (creator_share per side,
    e.g. 0.003) returns to them as claimable creator fees:
        recovered / spent = (1 - f_s + 2*creator_share) / (1 + f_b)
    """
    return 1 - (1 - f_s + 2 * creator_share) / (1 + f_b)


def cf_operator_gross_exit(N: float, V: float, X: float) -> float:
    """Gross curve proceeds when the operator (who bought first with net N on a fresh curve) sells
    everything after external buyers have added net X and are still holding:
        gross = N * A^2 / (V*B + N*A),  A = V + N + X,  B = V + N
    Derived in the Phase 1 report; equals N when X = 0 (perfect reversibility)."""
    A, B = V + N + X, V + N
    return N * A * A / (V * B + N * A)


def cf_break_even_external(N: float, V: float, f_b: float, f_s: float, creator_share: float = 0.0) -> float:
    """External net inflow X* at which the operator's curve round trip breaks even (before network costs).

    Solves (1 - f_s + 2c) * gross(X) = (1 + f_b) * N for X; with rho = (1 + f_b)/(1 - f_s + 2c):
        A* = (rho*N + sqrt(rho^2 N^2 + 4 rho V B)) / 2,  X* = A* - B.
    (Treats the creator rebate on the exit leg as proportional to gross; small approximation.)"""
    rho = (1 + f_b) / (1 - f_s + 2 * creator_share)
    B = V + N
    A = (rho * N + math.sqrt(rho * rho * N * N + 4 * rho * V * B)) / 2
    return A - B


def cf_marginal_capture(N: float, V: float) -> float:
    """d(gross exit)/dX at X = 0: share of the first external SOL that the first-in operator can take out."""
    return N * (2 * V + N) / (V + N) ** 2


# ---------------------------------------------------------------- integer simulations

@dataclass
class Party:
    name: str
    spent: int = 0  # lamports paid into trades (incl. fees)
    received: int = 0  # lamports received from sells (after fees)
    tokens: int = 0
    protocol_fees: int = 0
    creator_fees: int = 0
    network: float = 0.0
    trades: list = field(default_factory=list)

    @property
    def curve_pnl(self) -> int:
        return self.received - self.spent

    @property
    def net_pnl(self) -> float:
        return self.received - self.spent - self.network

    def record(self, r: TradeResult, costs: ExecutionCosts) -> None:
        self.trades.append(r)
        self.protocol_fees += r.protocol_fee
        self.creator_fees += r.creator_fee
        self.network += costs.expected_per_success()
        if r.side == "buy":
            self.spent += -r.user_quote_delta
            self.tokens += r.tokens
        else:
            self.received += r.user_quote_delta
            self.tokens -= r.tokens


@dataclass
class Simulation:
    """A sequential ledger of trades on one curve. Transactions are applied strictly in the order
    given, which is how a Solana leader executes them inside a slot (see report section 4)."""

    params: CurveParams
    state: CurveState = None  # type: ignore[assignment]
    parties: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.state is None:
            self.state = CurveState.fresh(self.params)
        self.initial = self.state

    @property
    def schedule(self) -> FeeSchedule:
        return self.params.fee_schedule

    def party(self, name: str) -> Party:
        return self.parties.setdefault(name, Party(name))

    def buy(self, who: str, lamports: int, costs: ExecutionCosts = FREE, min_tokens_out: int = 0) -> TradeResult:
        r = buy_exact_quote_in(self.state, lamports, self.schedule, min_tokens_out)
        self.state = r.after
        self.party(who).record(r, costs)
        return r

    def sell(self, who: str, tokens: int | None = None, costs: ExecutionCosts = FREE, min_out: int = 0) -> TradeResult:
        p = self.party(who)
        tokens = p.tokens if tokens is None else tokens
        r = sell_exact_tokens_in(self.state, tokens, self.schedule, min_out)
        self.state = r.after
        p.record(r, costs)
        return r

    def totals(self) -> dict:
        ps = self.parties.values()
        return {
            "sum_curve_pnl": sum(p.curve_pnl for p in ps),
            "protocol_fees": sum(p.protocol_fees for p in ps),
            "creator_fees": sum(p.creator_fees for p in ps),
            "real_quote_change": self.state.real_quote_reserves - self.initial.real_quote_reserves,
        }


@dataclass(frozen=True)
class RoundTrip:
    spend: int
    n_wallets: int
    tokens: int
    recovered: int
    protocol_fees: int
    creator_fees: int
    creator_rebate: int
    network: float
    dust_left_in_curve: int

    @property
    def curve_loss(self) -> int:
        return self.spend - self.recovered

    @property
    def net_loss(self) -> float:
        return self.spend - self.recovered - self.creator_rebate + self.network


def round_trip(params: CurveParams, spend: int, n_wallets: int = 1, costs: ExecutionCosts = FREE,
               operator_is_creator: bool = False, sell_order: list[int] | None = None,
               state: CurveState | None = None) -> RoundTrip:
    """Buy `spend` lamports split evenly over `n_wallets` (sequential buys, nobody else trades),
    then sell every wallet's full balance back (in `sell_order`, default wallet order).

    If operator_is_creator, creator fees paid on these trades accrue to the operator's creator
    vault and are counted as a rebate, minus one claim transaction."""
    sim = Simulation(params, state)
    per, rem = divmod(spend, n_wallets)
    for i in range(n_wallets):
        sim.buy(f"w{i}", per + (rem if i == 0 else 0), costs)
    for i in sell_order or range(n_wallets):
        sim.sell(f"w{i}", costs=costs)
    ps = list(sim.parties.values())
    creator_fees = sum(p.creator_fees for p in ps)
    network = sum(p.network for p in ps) + n_wallets * costs.wallet_setup()
    rebate = 0
    if operator_is_creator:
        rebate = creator_fees
        network += costs.expected_per_success()  # collect_creator_fee / sweep transaction
    return RoundTrip(spend, n_wallets, sum(p.trades[0].tokens for p in ps), sum(p.received for p in ps),
                     sum(p.protocol_fees for p in ps), creator_fees, rebate, network,
                     sim.state.real_quote_reserves - sim.initial.real_quote_reserves)


def external_demand_scenario(params: CurveParams, operator_spend: int, external_buys: list[int],
                             external_sell_fraction_before_exit: float = 0.0, operator_exits_first: bool = True,
                             costs: ExecutionCosts = FREE, external_costs: ExecutionCosts = FREE,
                             operator_is_creator: bool = False) -> dict:
    """Operator buys first on a fresh curve, independent buyers then buy `external_buys` (lamports each).
    A fraction of external holdings is sold before the operator's exit; the operator then sells
    everything; finally all remaining external holders sell. Returns every party's P&L so that the
    zero-sum identity (sum of P&L = -fees - dust) can be checked."""
    sim = Simulation(params)
    sim.buy("operator", operator_spend, costs)
    for i, x in enumerate(external_buys):
        sim.buy(f"ext{i}", x, external_costs)
    ext = [n for n in sim.parties if n.startswith("ext")]

    def externals_sell(fraction: float):
        for n in ext:
            amt = int(sim.parties[n].tokens * fraction)
            if amt > 0:
                sim.sell(n, amt, external_costs)

    if not operator_exits_first:
        externals_sell(1.0)
    else:
        externals_sell(external_sell_fraction_before_exit)
    exit_trade = sim.sell("operator", costs=costs)
    externals_sell(1.0)
    op = sim.parties["operator"]
    rebate = op.creator_fees + sum(sim.parties[n].creator_fees for n in ext) if operator_is_creator else 0
    return {
        "operator_spent": op.spent,
        "operator_received": op.received,
        "operator_curve_pnl": op.curve_pnl,
        "operator_creator_fee_income": rebate,
        "operator_network": op.network + (costs.expected_per_success() if operator_is_creator else 0),
        "operator_net_pnl": op.curve_pnl + rebate - op.network - (costs.expected_per_success() if operator_is_creator else 0),
        "operator_exit_avg_price_vs_entry": float(exit_trade.avg_price() / op.trades[0].avg_price()),
        "externals_spent": sum(sim.parties[n].spent for n in ext),
        "externals_received": sum(sim.parties[n].received for n in ext),
        "externals_curve_pnl": sum(sim.parties[n].curve_pnl for n in ext),
        **sim.totals(),
    }


def break_even_external_integer(params: CurveParams, operator_spend: int, costs: ExecutionCosts = FREE,
                                operator_is_creator: bool = False, hi: int | None = None) -> int | None:
    """Smallest single external buy (lamports, placed after the operator, held through the operator's
    exit) for which the operator's net P&L >= 0. Integer bisection on the exact simulator."""
    def pnl(x: int) -> float:
        if x == 0:
            rt = round_trip(params, operator_spend, 1, costs, operator_is_creator)
            return -rt.net_loss
        return external_demand_scenario(params, operator_spend, [x], costs=costs,
                                        operator_is_creator=operator_is_creator)["operator_net_pnl"]
    if pnl(0) >= 0:
        return 0
    lo = 0
    if hi is None:  # largest external buy that still fits on the curve before graduation
        after_op = buy_exact_quote_in(CurveState.fresh(params), operator_spend, params.fee_schedule).after
        left = after_op.real_token_reserves - 1
        net = left * after_op.virtual_quote_reserves // (after_op.virtual_token_reserves - left)
        hi = net * 10_000 // (10_000 + after_op.fees(params.fee_schedule).total_bps)
    if pnl(hi) < 0:
        return None  # not reachable before graduation with the given upper bound
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if pnl(mid) >= 0 else (mid, hi)
    return hi


def liquidation_value(state: CurveState, tokens: int, schedule: FeeSchedule,
                      costs: ExecutionCosts = FREE, cost_basis: int | None = None) -> dict:
    """"If I sell my whole position right now, in one transaction, with nobody trading ahead of me,
    how much SOL do I actually get?" Separates the numbers wallets/terminals display from what
    the curve will pay."""
    spot_value = tokens * state.virtual_quote_reserves // state.virtual_token_reserves
    gross = quote_for_tokens_in(tokens, state.virtual_token_reserves, state.virtual_quote_reserves)
    r = sell_exact_tokens_in(state, tokens, schedule)
    network = costs.expected_per_success()
    out = {
        "displayed_market_cap": state.market_cap(),
        "displayed_position_value_at_spot": spot_value,
        "theoretical_curve_value_no_fees": gross,
        "price_impact_vs_spot": 1 - gross / spot_value if spot_value else 0.0,
        "protocol_fee": r.protocol_fee,
        "creator_fee": r.creator_fee,
        "network_costs": network,
        "net_sol_recovered": r.user_quote_delta - network,
        "share_of_displayed_value_recoverable": (r.user_quote_delta - network) / spot_value if spot_value else 0.0,
    }
    if cost_basis is not None:
        out["net_pnl"] = out["net_sol_recovered"] - cost_basis
    return out


def batch_sell(state: CurveState, balances: list[int], schedule: FeeSchedule, slippage_bps: int,
               quote_basis: str = "pre_batch", costs: ExecutionCosts = FREE,
               interleave: dict[int, tuple[str, int]] | None = None) -> dict:
    """Execute one sell per wallet, strictly in list order, as a leader would within one slot.

    quote_basis="pre_batch": every wallet's min_out is computed from the state *before* the batch
      (what a tool does when it builds all transactions at once).
    quote_basis="sequential": each min_out is computed from the state the previous sell left
      (only possible if transactions are built after each other lands, or atomically in one tx).
    interleave: {index: ("buy"|"sell", amount)} external trades inserted before wallet `index`.
    A failed sell keeps its tokens and still pays base + priority fee (non-bundle submission)."""
    s = state
    rows, total_net, failed_tokens = [], 0, 0
    pre_quotes = [sell_exact_tokens_in(state, b, schedule).user_quote_delta for b in balances]
    fail_fee = costs.signatures_per_tx * 5_000 + costs.priority_fee_per_tx
    for i, bal in enumerate(balances):
        if interleave and i in interleave:
            side, amt = interleave[i]
            s = (buy_exact_quote_in(s, amt, schedule) if side == "buy" else sell_exact_tokens_in(s, amt, schedule)).after
        expected = pre_quotes[i] if quote_basis == "pre_batch" else sell_exact_tokens_in(s, bal, schedule).user_quote_delta
        min_out = expected * (10_000 - slippage_bps) // 10_000
        try:
            r = sell_exact_tokens_in(s, bal, schedule, min_out)
            s = r.after
            net = r.user_quote_delta - costs.per_attempt()
            rows.append({"wallet": i, "ok": True, "proceeds": r.user_quote_delta, "min_out": min_out,
                         "avg_price_lamports_per_token": float(r.avg_price()) * 1e6})
            total_net += net
        except SlippageExceeded:
            rows.append({"wallet": i, "ok": False, "proceeds": 0, "min_out": min_out})
            total_net -= fail_fee
            failed_tokens += bal
    return {"rows": rows, "total_net": total_net, "unsold_tokens": failed_tokens, "final_state": s}
