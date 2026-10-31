"""Integer-exact Pump.fun bonding-curve math.

Scope: SOL-paired (or any single-quote) coins on the Pump bonding-curve program
(6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P) *before* graduation. All amounts are
integers in base units: lamports for SOL (1 SOL = 1e9) and 1e-6 token units.

The formulas mirror the official TypeScript SDK (@pump-fun/pump-sdk 4.1.0,
src/bondingCurve.ts and src/fees.ts, MIT licence), which the docs say mirrors the
on-chain program. ``scripts/validate_against_chain.py`` replays real mainnet
TradeEvents through these functions and checks for exact (lamport-level) equality.

Curve model (constant product on *virtual* reserves):
    k = vQ * vT
    buy  `t` tokens : quote_net = floor(t * vQ / (vT - t)) + 1          (rounds against buyer)
    sell `t` tokens : quote_gross = floor(t * vQ / (vT + t))            (rounds against seller)
    fees            : ceil(quote_net_or_gross * bps / 10_000), protocol and creator separately
    buy  pays       : quote_net + protocol_fee + creator_fee
    sell receives   : quote_gross - protocol_fee - creator_fee
Reserves move by quote_net / quote_gross only; fees never enter the reserves.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction

BPS = 10_000
ONE_BILLION_SUPPLY = 1_000_000_000_000_000  # 1e9 tokens * 1e6 decimals


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def fee_amount(amount: int, bps: int) -> int:
    """pump-sdk fees.ts `fee()`: ceil(amount * bps / 10_000)."""
    return ceil_div(amount * bps, BPS)


class SlippageExceeded(Exception):
    """Raised where the on-chain program would fail the transaction on a slippage check."""


class CurveComplete(Exception):
    """Raised for trades on a completed curve (program error BondingCurveComplete)."""


@dataclass(frozen=True)
class Fees:
    protocol_bps: int
    creator_bps: int
    lp_bps: int = 0

    @property
    def total_bps(self) -> int:
        return self.protocol_bps + self.creator_bps + self.lp_bps


@dataclass(frozen=True)
class FeeTier:
    market_cap_lamports_threshold: int
    fees: Fees


@dataclass(frozen=True)
class FeeSchedule:
    """Market-cap-tiered schedule, as stored in the Pump Fees `FeeConfig` account."""

    tiers: tuple[FeeTier, ...]

    def for_market_cap(self, market_cap: int) -> Fees:
        # pump-sdk fees.ts `calculateFeeTier`
        if not self.tiers:
            raise ValueError("fee tiers cannot be empty")
        first = self.tiers[0]
        if market_cap < first.market_cap_lamports_threshold:
            return first.fees
        for tier in reversed(self.tiers):
            if market_cap >= tier.market_cap_lamports_threshold:
                return tier.fees
        return first.fees

    @classmethod
    def flat(cls, protocol_bps: int, creator_bps: int) -> "FeeSchedule":
        return cls((FeeTier(0, Fees(protocol_bps, creator_bps)),))


@dataclass(frozen=True)
class CurveParams:
    initial_virtual_token_reserves: int
    initial_virtual_quote_reserves: int
    initial_real_token_reserves: int
    token_total_supply: int
    pool_migration_fee: int
    fee_schedule: FeeSchedule


@dataclass(frozen=True)
class CurveState:
    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    real_quote_reserves: int
    token_total_supply: int = ONE_BILLION_SUPPLY
    complete: bool = False
    has_creator: bool = True  # program charges no creator fee while bonding_curve.creator is the zero key
    is_mayhem_mode: bool = False

    @classmethod
    def fresh(cls, params: CurveParams) -> "CurveState":
        return cls(
            virtual_token_reserves=params.initial_virtual_token_reserves,
            virtual_quote_reserves=params.initial_virtual_quote_reserves,
            real_token_reserves=params.initial_real_token_reserves,
            real_quote_reserves=0,
            token_total_supply=params.token_total_supply,
        )

    # ---- derived quantities -------------------------------------------------
    @property
    def k(self) -> int:
        return self.virtual_token_reserves * self.virtual_quote_reserves

    def spot_price(self) -> Fraction:
        """Marginal price, quote base units per token base unit (= lamports per 1e-6 token)."""
        return Fraction(self.virtual_quote_reserves, self.virtual_token_reserves)

    def market_cap(self) -> int:
        """Displayed market cap in quote base units: vQ * total_supply // vT (pump-sdk bondingCurveMarketCap)."""
        return self.virtual_quote_reserves * self.token_total_supply // self.virtual_token_reserves

    def fee_market_cap(self) -> int:
        """Market cap used for fee-tier lookup (non-mayhem coins always use a 1e9-token supply)."""
        supply = self.token_total_supply if self.is_mayhem_mode else ONE_BILLION_SUPPLY
        return self.virtual_quote_reserves * supply // self.virtual_token_reserves

    def fees(self, schedule: FeeSchedule) -> Fees:
        f = schedule.for_market_cap(self.fee_market_cap())
        return f if self.has_creator else Fees(f.protocol_bps, 0, f.lp_bps)


@dataclass(frozen=True)
class TradeResult:
    side: str  # "buy" | "sell"
    tokens: int  # token base units moved between curve and user
    quote_curve: int  # quote that entered (buy) or left (sell) the curve reserves
    protocol_fee: int
    creator_fee: int
    fees_bps: Fees
    before: CurveState
    after: CurveState

    @property
    def total_fee(self) -> int:
        return self.protocol_fee + self.creator_fee

    @property
    def user_quote_delta(self) -> int:
        """Change in the trader's quote balance from the curve trade alone (excludes network costs)."""
        if self.side == "buy":
            return -(self.quote_curve + self.total_fee)
        return self.quote_curve - self.total_fee

    def avg_price(self) -> Fraction:
        """Average all-in price paid/received, quote base units per token base unit."""
        return Fraction(abs(self.user_quote_delta), self.tokens) if self.tokens else Fraction(0)


# ---- raw curve quotes (no fees), straight from pump-sdk ----------------------

def quote_for_tokens_out(tokens: int, v_token: int, v_quote: int) -> int:
    """getBuySolAmountFromTokenAmountQuote: floor(t*vQ/(vT-t)) + 1."""
    return tokens * v_quote // (v_token - tokens) + 1


def tokens_for_quote_in(quote_in: int, v_token: int, v_quote: int) -> int:
    """getBuyTokenAmountFromSolAmountQuote: floor(q*vT/(vQ+q))."""
    return quote_in * v_token // (v_quote + quote_in)


def quote_for_tokens_in(tokens: int, v_token: int, v_quote: int) -> int:
    """getSellSolAmountFromTokenAmountQuote: floor(t*vQ/(vT+t))."""
    return tokens * v_quote // (v_token + tokens)


# ---- state transitions -------------------------------------------------------

def _apply_buy(s: CurveState, tokens: int, quote_net: int) -> CurveState:
    real_token = s.real_token_reserves - tokens
    return replace(
        s,
        virtual_token_reserves=s.virtual_token_reserves - tokens,
        virtual_quote_reserves=s.virtual_quote_reserves + quote_net,
        real_token_reserves=real_token,
        real_quote_reserves=s.real_quote_reserves + quote_net,
        complete=real_token == 0,
    )


def _apply_sell(s: CurveState, tokens: int, quote_gross: int) -> CurveState:
    return replace(
        s,
        virtual_token_reserves=s.virtual_token_reserves + tokens,
        virtual_quote_reserves=s.virtual_quote_reserves - quote_gross,
        real_token_reserves=s.real_token_reserves + tokens,
        real_quote_reserves=s.real_quote_reserves - quote_gross,
    )


def buy_exact_tokens_out(s: CurveState, tokens: int, schedule: FeeSchedule,
                         max_quote_cost: int | None = None) -> TradeResult:
    """`buy` / `buy_v2` / `buy_v3` (curve leg only): buy an exact token amount.

    A request above real_token_reserves is clamped to what is left (legacy partial-fill
    behaviour). The v3 "synthetic migration" leg past the curve is NOT modelled here.
    """
    if s.complete:
        raise CurveComplete("bonding curve is complete")
    if tokens <= 0:
        raise ValueError("tokens must be > 0")
    tokens = min(tokens, s.real_token_reserves)
    fees = s.fees(schedule)
    quote_net = quote_for_tokens_out(tokens, s.virtual_token_reserves, s.virtual_quote_reserves)
    pf, cf = fee_amount(quote_net, fees.protocol_bps), fee_amount(quote_net, fees.creator_bps)
    if max_quote_cost is not None and quote_net + pf + cf > max_quote_cost:
        raise SlippageExceeded(f"cost {quote_net + pf + cf} > max {max_quote_cost}")
    return TradeResult("buy", tokens, quote_net, pf, cf, fees, s, _apply_buy(s, tokens, quote_net))


def exact_quote_in_fee_amounts(budget: int, fees: Fees) -> tuple[int, int, int]:
    """pump `exact_quote_in_fees` -> (net, protocol_fee, creator_fee) for a fee-inclusive budget.

    net0 = floor(budget * 10_000 / (10_000 + total_bps)); each fee = ceil(net0 * bps / 10_000);
    if net0 + fees > budget the excess is shaved off the net *but the fees stay as computed on
    net0* (pump-rust-client 0.4.1 math/fees.rs `exact_in_fee_amounts`: "charged on the unshaved
    net as the programs do"). Confirmed on mainnet: a shaved trade reports fee 98,085 lamports
    where fees on the shaved net would give 98,084.
    """
    total = fees.protocol_bps + fees.creator_bps
    net = budget if total == 0 else budget * BPS // (total + BPS)
    pf, cf = fee_amount(net, fees.protocol_bps), fee_amount(net, fees.creator_bps)
    if net + pf + cf > budget:
        net -= net + pf + cf - budget
    return net, pf, cf


def exact_quote_in_net(budget: int, fees: Fees) -> int:
    return exact_quote_in_fee_amounts(budget, fees)[0]


def buy_exact_quote_in(s: CurveState, spendable: int, schedule: FeeSchedule,
                       min_tokens_out: int = 0) -> TradeResult:
    """`buy_exact_sol_in` / `buy_exact_quote_in_v2|v3` (curve leg only).

    net    = exact_quote_in_net(spendable)            # budget left after protocol+creator fees
    tokens = floor((net - 1) * vT / (vQ + net - 1))
    The full `net` enters the reserves and fees are charged on the unshaved net (see
    exact_quote_in_fee_amounts), so the trader spends (almost) the entire budget. Confirmed against mainnet events (both the
    token formula and "full net charged") by scripts/validate_against_chain.py;
    the alternative "charge only the exact-out price of `tokens`" matched 0 trades.
    """
    if s.complete:
        raise CurveComplete("bonding curve is complete")
    fees = s.fees(schedule)
    net, pf, cf = exact_quote_in_fee_amounts(spendable, fees)
    if net <= 1:
        raise ValueError("budget too small")
    tokens = tokens_for_quote_in(net - 1, s.virtual_token_reserves, s.virtual_quote_reserves)
    if tokens > s.real_token_reserves:
        raise NotImplementedError("budget crosses graduation; partial fill / synthetic migration not modelled")
    if tokens <= 0:
        raise ValueError("budget buys zero tokens")
    if tokens < min_tokens_out:
        raise SlippageExceeded(f"tokens {tokens} < min {min_tokens_out}")
    return TradeResult("buy", tokens, net, pf, cf, fees, s, _apply_buy(s, tokens, net))


def sell_exact_tokens_in(s: CurveState, tokens: int, schedule: FeeSchedule,
                         min_quote_out: int = 0) -> TradeResult:
    """`sell` / `sell_v2` / `sell_v3`: sell an exact token amount; fees are deducted from the proceeds."""
    if s.complete:
        raise CurveComplete("bonding curve is complete")
    if tokens <= 0:
        raise ValueError("tokens must be > 0")
    fees = s.fees(schedule)
    gross = quote_for_tokens_in(tokens, s.virtual_token_reserves, s.virtual_quote_reserves)
    if gross > s.real_quote_reserves:
        raise ValueError("sell exceeds real quote reserves")
    pf, cf = fee_amount(gross, fees.protocol_bps), fee_amount(gross, fees.creator_bps)
    if gross - pf - cf < min_quote_out:
        raise SlippageExceeded(f"proceeds {gross - pf - cf} < min {min_quote_out}")
    return TradeResult("sell", tokens, gross, pf, cf, fees, s, _apply_sell(s, tokens, gross))
