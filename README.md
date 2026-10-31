# Solana memecoin launch economics: research workspace

Research and simulation only. Nothing in this repository signs, sends or automates
transactions.

## Phase 1 (this commit): mechanics, mathematics, validated simulator

* **Report:** [`docs/PHASE1_MATH_AND_MECHANICS.md`](docs/PHASE1_MATH_AND_MECHANICS.md). It
  covers verified Pump.fun mechanics (as of 2026-10-31), the bonding-curve mathematics, the
  reversibility proof, the zero-sum identity, ordering and slippage effects, and the
  liquidation-value maths.
* **Generated tables:** [`docs/generated/phase1_tables.md`](docs/generated/phase1_tables.md),
  reproducible with `python -m scripts.phase1_tables`.
* **Simulator:** `pumpsim/`, integer-exact and matching the on-chain program. On mainnet it
  matched 256 of 256 standard SOL-paired trades and a full 200-trade launch replay.
* **On-chain evidence:**
  * `data/onchain_config_snapshot.json`: live Global and FeeConfig.
  * `data/validation_report.json`: the trade-by-trade check.
  * `data/replays/`: the full-launch replay.

```
python -m pytest -q                                    # offline tests (stdlib + pytest only)
python -m pumpsim roundtrip --sol 10 --wallets 5 --priority-lamports 20000
python -m pumpsim external --sol 10 --external 0.5,1,2 --sell-before-exit 0.5
python -m pumpsim liquidate --tokens 265758513.89 --after-buys 10,5 --cost-basis 10
python -m pumpsim breakeven --sol 10 --creator
python -m scripts.fetch_onchain_config                 # re-verify parameters (read-only mainnet RPC)
python -m scripts.validate_against_chain --tx 400      # re-validate against fresh trades
python -m scripts.replay_launch --auto                 # replay one recent launch end-to-end
```

Set `SOLANA_RPC_URL` to use your own RPC. The public endpoint works, but it is rate-limited.

Requirements: Python ≥ 3.10. Only the standard library is used, plus `pytest` for the tests.
