"""Invariant and regression tests for the integer-exact curve. No network access."""
import itertools
import math
import random

import pytest

from pumpsim import VERIFIED, CurveState, FeeSchedule, SlippageExceeded
from pumpsim.analysis import (
    batch_sell, break_even_external_integer, cf_break_even_external, cf_operator_gross_exit,
    cf_round_trip_loss_fraction, external_demand_scenario, liquidation_value, round_trip,
)
from pumpsim.costs import BASE_FEE_ONLY, ExecutionCosts
from pumpsim.curve import (
    buy_exact_quote_in, buy_exact_tokens_out, exact_quote_in_fee_amounts, sell_exact_tokens_in, Fees,
)

L = 10**9
SCHED = VERIFIED.fee_schedule
NOFEE = FeeSchedule.flat(0, 0)


def fresh():
    return CurveState.fresh(VERIFIED)


def test_verified_initial_curve():
    s = fresh()
    # initial displayed market cap: 30 SOL * 1e9 / 1.073e9 = 27.959 SOL
    assert s.market_cap() == 27_958_993_476
    assert s.k == 30_000_000_000 * 1_073_000_000_000_000


def test_graduation_point():
    """Buying all 793.1M real tokens raises ~85.005 SOL; curve completes at ~410.9 SOL mcap."""
    s = fresh()
    r = buy_exact_tokens_out(s, s.real_token_reserves, NOFEE)
    assert r.after.complete
    assert r.after.real_token_reserves == 0
    assert abs(r.quote_curve / L - 85.0053590568) < 1e-6
    assert abs(r.after.market_cap() / L - 410.877) < 0.01


def test_no_fee_round_trip_is_exactly_reversible_up_to_rounding():
    s = fresh()
    for spend in (1000, L, 10 * L, 50 * L):
        b = buy_exact_quote_in(s, spend, NOFEE)
        sl = sell_exact_tokens_in(b.after, b.tokens, NOFEE)
        assert sl.after.virtual_token_reserves == s.virtual_token_reserves
        # rounding always favours the curve; dust is at most a couple of lamports
        assert 0 <= b.quote_curve - sl.quote_curve <= 2
        assert 0 <= sl.after.real_quote_reserves <= 2


def test_round_trip_matches_closed_form():
    for spend in (L, 2 * L, 5 * L, 10 * L, 20 * L):
        rt = round_trip(VERIFIED, spend)
        expected = spend * cf_round_trip_loss_fraction(0.0125, 0.0125)
        assert abs(rt.curve_loss - expected) <= 5  # lamports


def test_round_trip_loss_independent_of_size_and_start_reserves():
    """Fee loss fraction ~2.469 % for any size and any virtual-reserve starting point (no other trades)."""
    for vq in (10 * L, 30 * L, 100 * L):
        s = CurveState(1_073_000_000_000_000, vq, 793_100_000_000_000, 0)
        for spend in (L, 10 * L):
            b = buy_exact_quote_in(s, spend, SCHED)
            sl = sell_exact_tokens_in(b.after, b.tokens, SCHED)
            frac = (spend - sl.user_quote_delta) / spend
            assert abs(frac - 0.0246913580) < 1e-6


def test_path_independence_same_owner_split_sells():
    """Selling a position in k pieces (any order) yields the same SOL as one sell, up to rounding."""
    s = fresh()
    b = buy_exact_quote_in(s, 10 * L, SCHED)
    one = sell_exact_tokens_in(b.after, b.tokens, SCHED).user_quote_delta
    rng = random.Random(7)
    for k in (2, 3, 5, 10):
        cuts = sorted(rng.sample(range(1, b.tokens), k - 1))
        pieces = [y - x for x, y in zip([0] + cuts, cuts + [b.tokens])]
        for order in itertools.islice(itertools.permutations(pieces), 6):
            st, total = b.after, 0
            for p in order:
                r = sell_exact_tokens_in(st, p, SCHED)
                st, total = r.after, total + r.user_quote_delta
            assert 0 <= one - total <= 3 * k  # floor/ceil dust only


def test_multi_wallet_round_trip_costs_only_extra_network_fees():
    one = round_trip(VERIFIED, 10 * L, 1)
    ten = round_trip(VERIFIED, 10 * L, 10)
    assert abs(one.curve_loss - ten.curve_loss) < 100  # lamports of rounding
    assert ten.network - one.network == 18 * BASE_FEE_ONLY.per_attempt()


def test_exact_in_shaved_net_fee_rule_regression():
    """Mainnet tx 5zP7Gz27...: budget 10,453,690 -> net 10,324,631, protocol fee 98,085, creator 30,974."""
    net, pf, cf = exact_quote_in_fee_amounts(10_453_690, Fees(95, 30))
    assert (net, pf, cf) == (10_324_631, 98_085, 30_974)
    assert net + pf + cf == 10_453_690


def test_exact_in_never_overspends():
    s = fresh()
    rng = random.Random(1)
    for _ in range(500):
        budget = rng.randint(10_000, 20 * L)
        r = buy_exact_quote_in(s, budget, SCHED)
        assert -r.user_quote_delta <= budget


def test_slippage_check_raises():
    s = fresh()
    b = buy_exact_quote_in(s, L, SCHED)
    with pytest.raises(SlippageExceeded):
        sell_exact_tokens_in(b.after, b.tokens, SCHED, min_quote_out=L)


def test_closed_form_gross_exit_matches_integer():
    N = 10 * L * 10_000 // 10_125
    for X in (0, L // 2, 2 * L, 10 * L):
        st = buy_exact_quote_in(fresh(), 10 * L, NOFEE)
        tokens = st.tokens
        s2 = buy_exact_quote_in(st.after, X, NOFEE).after if X else st.after
        gross = sell_exact_tokens_in(s2, tokens, NOFEE).quote_curve
        cf = cf_operator_gross_exit(10 * L, 30 * L, X)
        assert abs(gross - cf) / cf < 1e-9
    assert N > 0


def test_break_even_external_inflow_closed_form_vs_integer():
    x_int = break_even_external_integer(VERIFIED, 10 * L, costs=ExecutionCosts(signatures_per_tx=0))
    x_cf = cf_break_even_external(10 / 1.0125, 30, 0.0125, 0.0125) * 1.0125  # gross external spend
    assert abs(x_int / L - x_cf) < 0.001


def test_zero_sum_identity():
    """Sum of all traders' curve P&L = -(protocol + creator fees) - SOL left in the curve."""
    res = external_demand_scenario(VERIFIED, 10 * L, [L, 2 * L, L // 3], external_sell_fraction_before_exit=0.5)
    assert res["sum_curve_pnl"] == -(res["protocol_fees"] + res["creator_fees"]) - res["real_quote_change"]


def test_who_exits_first_wins():
    first = external_demand_scenario(VERIFIED, 10 * L, [2 * L], operator_exits_first=True)
    last = external_demand_scenario(VERIFIED, 10 * L, [2 * L], operator_exits_first=False)
    assert first["operator_curve_pnl"] > 0 > last["operator_curve_pnl"]


def test_liquidation_value_below_displayed_value():
    b = buy_exact_quote_in(fresh(), 10 * L, SCHED)
    lv = liquidation_value(b.after, b.tokens, SCHED)
    assert lv["net_sol_recovered"] < lv["theoretical_curve_value_no_fees"] < lv["displayed_position_value_at_spot"]
    # spot value / realisable gross = 1 + N/V (N = net SOL in, V = 30 SOL): ~1.329 for a 10 SOL buy
    ratio = lv["displayed_position_value_at_spot"] / lv["theoretical_curve_value_no_fees"]
    assert abs(ratio - (1 + b.quote_curve / (30 * L))) < 1e-6


def test_batch_sell_pre_batch_quotes_fail_with_tight_slippage():
    """Five equal wallets, all min_out quoted from the same pre-batch state: with 1 % tolerance only
    the first sell succeeds, because each sell lowers the price the next one sees."""
    s = fresh()
    st, bals = s, []
    for _ in range(5):
        r = buy_exact_quote_in(st, 2 * L, SCHED)
        st = r.after
        bals.append(r.tokens)
    res = batch_sell(st, bals, SCHED, slippage_bps=100, quote_basis="pre_batch")
    assert [r["ok"] for r in res["rows"]] == [True, False, False, False, False]
    seq = batch_sell(st, bals, SCHED, slippage_bps=100, quote_basis="sequential")
    assert all(r["ok"] for r in seq["rows"]) and seq["unsold_tokens"] == 0
