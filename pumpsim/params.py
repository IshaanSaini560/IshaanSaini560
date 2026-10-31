"""Verified Pump.fun parameters, with provenance.

Every value below was read from mainnet account state on 2026-10-31 by
scripts/fetch_onchain_config.py (snapshot: data/onchain_config_snapshot.json,
Global at slot 463,702,641). Re-run that script before relying on these numbers:
Pump has changed fees and instructions repeatedly (May 2025, Sep 2025, Sep-Oct 2026).
"""
from __future__ import annotations

import json
import pathlib

from .curve import CurveParams, FeeSchedule, FeeTier, Fees

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
TOKEN_UNIT = 10**TOKEN_DECIMALS

SNAPSHOT_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "onchain_config_snapshot.json"

# Global (4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf) + bonding-curve FeeConfig
# (8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt), SOL-paired, non-mayhem coins.
VERIFIED = CurveParams(
    initial_virtual_token_reserves=1_073_000_000_000_000,  # 1,073,000,000 tokens
    initial_virtual_quote_reserves=30_000_000_000,  # 30 SOL
    initial_real_token_reserves=793_100_000_000_000,  # 793,100,000 tokens sellable on the curve
    token_total_supply=1_000_000_000_000_000,  # 1,000,000,000 tokens
    pool_migration_fee=15_000_001,  # lamports, deducted from raised SOL at migration
    fee_schedule=FeeSchedule.flat(protocol_bps=95, creator_bps=30),  # single tier from 0 SOL mcap
)

# Share of the protocol fee routed to the buyback recipient (Global.buyback_basis_points = 5000).
# It is a split of the 0.95 % protocol fee, not an additional charge.
BUYBACK_SHARE_OF_PROTOCOL_BPS = 5_000

# USDC-paired coins start from Global.initial_virtual_quote_reserves = 4,292 USDC.
USDC_INITIAL_VIRTUAL_QUOTE_RESERVES = 4_292_000_000

# PumpSwap canonical-pool fee tiers (FeeConfig 5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx),
# applied after graduation. (threshold SOL, lp, protocol, creator) in bps.
PUMPSWAP_TIERS_SOL = [
    (0, 2, 93, 30), (420, 20, 5, 95), (1470, 20, 5, 90), (2460, 20, 5, 85), (3440, 20, 5, 80),
    (4420, 20, 5, 75), (9820, 20, 5, 70), (14740, 20, 5, 65), (19650, 20, 5, 60), (24560, 20, 5, 55),
    (29470, 20, 5, 50), (34380, 20, 5, 45), (39300, 20, 5, 40), (44210, 20, 5, 35), (49120, 20, 5, 30),
    (54030, 20, 5, 28), (58940, 20, 5, 25), (63860, 20, 5, 23), (68770, 20, 5, 20), (73681, 20, 5, 18),
    (78590, 20, 5, 15), (83500, 20, 5, 13), (88400, 20, 5, 10), (93330, 20, 5, 8), (98240, 20, 5, 5),
]
PUMPSWAP_FEE_SCHEDULE = FeeSchedule(tuple(
    FeeTier(t * LAMPORTS_PER_SOL, Fees(protocol_bps=p, creator_bps=c, lp_bps=lp)) for t, lp, p, c in PUMPSWAP_TIERS_SOL
))


def load_snapshot_params(path: pathlib.Path = SNAPSHOT_PATH) -> CurveParams:
    """Build CurveParams from a fresh on-chain snapshot instead of the constants above."""
    snap = json.loads(path.read_text())
    g = snap["global"]["decoded"]
    tiers = tuple(
        FeeTier(t["market_cap_lamports_threshold"],
                Fees(t["fees"]["protocol_fee_bps"], t["fees"]["creator_fee_bps"], t["fees"]["lp_fee_bps"]))
        for t in snap["fee_config"]["decoded"]["fee_tiers"]
    )
    return CurveParams(
        initial_virtual_token_reserves=g["initial_virtual_token_reserves"],
        initial_virtual_quote_reserves=g["initial_virtual_sol_reserves"],
        initial_real_token_reserves=g["initial_real_token_reserves"],
        token_total_supply=g["token_total_supply"],
        pool_migration_fee=g["pool_migration_fee"],
        fee_schedule=FeeSchedule(tiers),
    )
