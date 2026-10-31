"""Network / execution cost model. Every number here is an explicit, user-adjustable ASSUMPTION
unless marked VERIFIED.

VERIFIED (2026-10-31):
  * Base fee 5,000 lamports per signature (Solana docs, solana.com/docs/core/fees).
  * Rent-exempt minimums from mainnet getMinimumBalanceForRentExemption:
      165-byte SPL token account  -> 1,488,440 lamports
      170-byte Token-2022 ATA     -> 1,513,840 lamports  (create_v2 coins are Token-2022)
      137-byte user_volume_accumulator -> 1,346,200 lamports
    pump-public-docs still quotes 0.0018444 SOL for the accumulator (pre-rent-change value).
    Rent is a refundable deposit (recovered when the account is closed), not a fee.
  * Jito tip accounts (block engine getTipAccounts) - see scripts/validate_against_chain.py.

ASSUMPTIONS (set them to what you actually pay):
  * priority fee per transaction, Jito tip per bundle/transaction, failure probability.
A failed *regular* transaction that lands still pays base + priority fee. A Jito bundle that
fails simulation is not landed, so it pays nothing (but also does nothing).
"""
from __future__ import annotations

from dataclasses import dataclass

BASE_FEE_PER_SIGNATURE = 5_000  # VERIFIED
RENT_TOKEN_ACCOUNT_SPL = 1_488_440  # VERIFIED, refundable
RENT_TOKEN_ACCOUNT_2022 = 1_513_840  # VERIFIED, refundable
RENT_USER_VOLUME_ACCUMULATOR = 1_346_200  # VERIFIED, refundable, once per wallet


@dataclass(frozen=True)
class ExecutionCosts:
    priority_fee_per_tx: int = 0  # ASSUMPTION, lamports (= CU limit * CU price / 1e6)
    jito_tip_per_tx: int = 0  # ASSUMPTION, lamports
    signatures_per_tx: int = 1
    failure_rate: float = 0.0  # ASSUMPTION: probability a landed tx fails and must be retried
    include_rent_as_cost: bool = False  # rent is refundable; set True for a worst case where accounts stay open

    def per_attempt(self) -> int:
        return self.signatures_per_tx * BASE_FEE_PER_SIGNATURE + self.priority_fee_per_tx + self.jito_tip_per_tx

    def expected_per_success(self) -> float:
        """Expected lamports spent per *successful* transaction, if each failed attempt pays base+priority
        fee (not the tip) and is retried until success (geometric number of attempts)."""
        if not 0 <= self.failure_rate < 1:
            raise ValueError("failure_rate must be in [0, 1)")
        fail_cost = self.signatures_per_tx * BASE_FEE_PER_SIGNATURE + self.priority_fee_per_tx
        expected_failures = self.failure_rate / (1 - self.failure_rate)
        return self.per_attempt() + expected_failures * fail_cost

    def wallet_setup(self, token2022: bool = True) -> int:
        """Refundable deposits a fresh wallet locks on its first buy of a coin."""
        if not self.include_rent_as_cost:
            return 0
        return (RENT_TOKEN_ACCOUNT_2022 if token2022 else RENT_TOKEN_ACCOUNT_SPL) + RENT_USER_VOLUME_ACCUMULATOR


BASE_FEE_ONLY = ExecutionCosts()  # one signature, no priority fee, no tip, no failures
FREE = BASE_FEE_ONLY  # backwards-compatible alias; note it still includes the 5,000-lamport base fee
# Illustrative only: values in the range seen in a small 2026-10-31 mainnet sample
# (median network fee per Pump tx ~25,000 lamports). NOT a recommendation.
TYPICAL = ExecutionCosts(priority_fee_per_tx=20_000, jito_tip_per_tx=0)
AGGRESSIVE = ExecutionCosts(priority_fee_per_tx=1_000_000, jito_tip_per_tx=1_000_000)
