# Phase 2: historical launch-data pipeline

Status: **built and trial-tested on the public RPC. Large-scale collection is waiting for a
Helius API key** (environment variable `HELIUS_API_KEY`).

The pipeline is read-only. It never holds keys, signs, or sends transactions. All stored chain
data is decoded from mainnet transactions (VERIFIED). Derived columns are labelled as such.

## 1. Sampling design (avoiding survivorship bias)

The pipeline samples launches from the full stream of coin creations, not from coins that
became visible or successful.

* **Sampling frame.** Pump's `create` and `create_v2` instructions, and no other instruction,
  reference the constant PDA `["mint-authority"]` = `TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM`
  (checked against the IDL). Its transaction history is therefore exactly the list of creation
  attempts. On 2026-10-31 it showed about 1,500–2,000 creations per hour, of which roughly 5–9%
  failed.
* **Windows.** `frame --random-windows N --days D --minutes M --seed S` draws N disjoint windows
  with start times uniform over the last D days. Every creation in each window is recorded in
  `frame_creations`, including failed ones, so the frame size is known for weighting.
* **Sample.** `sample --n --seed` draws a seeded simple random sample, without replacement, from
  all successful creations in the frame. Coins are not filtered by type at this stage. Mayhem,
  USDC-paired, pump-coin-paired and holder-reward coins are tagged in `launches` so analyses can
  subset them openly.
* **No right-censoring.** Only creations whose whole observation horizon has already passed
  are eligible (`block_time + horizon ≤ now`).
* **Horizon and truncation.** Each launch's bonding-curve history is collected from 2 s before
  creation to `create_time + horizon` (default 6 h). Launches that exceed `--max-tx` (default
  15,000 transactions) are flagged `truncated = 1` with their real `observed_until`. These are
  the most active coins, so analyses must not drop them silently.

## 2. What is collected per launch

| Table | Content (all VERIFIED unless noted) |
|---|---|
| `frame_windows`, `frame_creations` | Every creation signature per window, failed or not; which ones were sampled, with the seed |
| `launches` | `CreateEvent` fields: creator, signer, name/symbol/uri, token program, mayhem / quote mint / holder-reward / cashback flags, initial reserves. Also collection metadata: horizon, truncation, counts, completion and migration times, simulator replay result |
| `transactions` | Every transaction touching the bonding curve in the horizon, **including failed ones**: slot, in-block index, fee, compute units, signers, Jito tip, fee-payer SOL change, failing program and error name |
| `trades` | Every `TradeEvent`: user, instruction, side, quote and token amounts, protocol / creator / buyback fees, post-trade reserves, the instruction's own limit arguments (amount, `max_sol_cost` / `min_tokens_out` / `min_sol_output`), and `sim_match` |
| `failed_attempts` | The Pump trade instruction inside each failed transaction: what was attempted, with which limits, and why it failed |
| `raw_tx` | zlib-compressed raw transactions, so analyses can be re-run without paying for the data again (`--no-raw` disables this) |
| `launch_summary` | DERIVED per-launch metrics (§4) |
| `usage_log` | Calls and estimated Helius credits per command |

**Ordering.** Helius returns each transaction's index within its block, so trades are ordered
exactly by `(slot, tx_index)`. With the standard-RPC fallback, the order returned by
`getSignaturesForAddress` is used instead (Phase 1 validated this).

**Integrity check.** For every standard SOL-paired, non-mayhem launch, each trade is re-run
through the Phase 1 simulator using the instruction's own arguments. The result is stored as
`sim_match`. Any mismatch flags either a decoding problem or a protocol change.

**Failure classification.** The program that failed a transaction is read from the log
invocation stack, not from the top-level error code (which often belongs to a router or bot).
This distinguishes:

* Pump's own failures: slippage (`TooMuchSolRequired`, `TooLittleSolReceived`,
  `BuySlippageBelowMinTokensOut`), `BondingCurveComplete`, and so on.
* Third-party programs that abort before or after Pump, such as "buy guard" programs that
  sniper bots use to cancel a buy when the price has already moved.

In a 40-transaction sample on 2026-10-31, 34 of 40 failures were outside Pump.

## 3. Conventions that matter for interpretation

* **"Creator"** means only the `CreateEvent` creator and the creation signer. Other wallets the
  creator may control are *not* included until the wallet-relationship stage, which will be
  probabilistic and carry confidence scores.
* **"Non-creator"** traders are not proven independent.
* **Creator positions** are rebuilt from curve trades only. Tokens moved by plain transfers are
  invisible here, so the position is a curve-flow balance (`creator_curve_net_tokens`), not a
  wallet balance.
* **Realised P&L** uses average cost.
* **Unrealised value** is the executable liquidation value of the remaining curve-flow balance
  at the end of the horizon, computed with the validated simulator and net of platform fees
  ("mark to curve"). It is never tokens × displayed price. It is only computed for standard,
  not-completed curves; otherwise it is left empty.
* **Creator fee income** is reported as *accrued*. Claiming is a separate transaction and is not
  tracked yet.

## 4. Derived per-launch metrics (`launch_summary`)

| Group | Metrics |
|---|---|
| Creation | Creator's initial buy (SOL paid, tokens, % of supply); other buyers and SOL in the creation slot and in the first 3 slots |
| Activity | Buys, sells, unique non-creator buyers and sellers; buy and sell volume |
| Timing | Non-creator buy and sell volume in time buckets 0–5 s, 5–30 s, 30–120 s, 2–10 min, 10–60 min, 60 min to end of horizon |
| Price path | Start, peak and final market cap; time to peak; final/peak ratio; peak and final curve progress; completion time; migration |
| Creator | SOL paid and received, net cash, tokens bought and sold, curve-flow balance, first sell, first sell of ≥ 10% of the position, realised P&L, mark-to-curve unrealised P&L, accrued creator fees |
| Costs and failures | Protocol and creator fees; network fees of all and of failed transactions; Jito tips; failures that reached Pump vs failed elsewhere; top failure reasons |

## 5. How to run

The API key goes in the environment settings as `HELIUS_API_KEY`. Never put it in code or chat.

```
python -m pumpsim.pipeline frame   --random-windows 24 --days 14 --minutes 10 --seed 1 --horizon 21600
python -m pumpsim.pipeline sample  --n 1000 --seed 1 --horizon 21600
python -m pumpsim.pipeline collect --limit 1000 --max-credits 200000
python -m pumpsim.pipeline summarize
python -m pumpsim.pipeline export  --out data/pipeline/exports       # CSV per table
python -m pumpsim.pipeline status
```

`--source helius` (default) uses `getTransactionsForAddress`. `--source helius-rpc` uses
standard methods on the Helius URL, for plans without that method. `--source public` uses the
public endpoint, for small trials only. Collection is resumable: re-running `collect` picks up
pending and errored launches.

## 6. Cost estimate (Helius price list, checked 2026-10-31)

| Item | Price | Credits |
|---|---|---|
| Frame enumeration | 10 credits per call of up to 1,000 signatures | 24 windows × 10 min × ~30 creations/min → 24–48 calls ≈ **500 credits** |
| Sampling | 1 credit per creation transaction (`getTransaction`) | **1,000 credits** |
| Collection | 10 credits per 100 returned transactions, rounded up | ≈ 10 × ⌈n_tx / 100⌉ per launch. If the typical launch has a few hundred transactions over 6 h, 1,000 launches are roughly 30k–100k credits. The `--max-tx` cap bounds the worst case at 1,500 per launch. |

The Free plan lists 1M credits a month. Helius does not say which plans include
`getTransactionsForAddress`. If the call is refused, use `--source helius-rpc` (1 credit per
transaction, so roughly 10× more credits and slower).

## 7. Trial run on the public RPC (2026-10-31)

See §8 below; the trial results are appended after the run.

## 9. Not yet collected (next steps)

1. **Post-graduation trading.** After migration, trades happen on the PumpSwap pool (address
   stored in `launches.pool`) and no longer touch the bonding curve. This needs a second
   collector for pool history.
2. **Wallet funding history** for relationship scoring (who funded each early buyer, and when).
3. **Mayhem-mode pricing** in the simulator. In small samples, mayhem coins were roughly 40–60%
   of new launches, so excluding them must be reported as a scope limitation.
4. **Analyses on the collected data:** exit-timing backtests (1–120 s, activity-based) with
   out-of-sample splits, external-demand frequencies, and creator outcome distributions.
