# Phase 2: historical launch-data pipeline

Status: **complete for the first dataset.** 997 launches were collected through Helius (§10),
after trials on free sources (§7–8b).

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
python -m pumpsim.pipeline reprocess                                  # rebuild rows from stored raw txs (no network)
python -m pumpsim.pipeline export  --out data/pipeline/exports       # CSV per table
python -m pumpsim.pipeline status
```

`--source helius` (default) uses `getTransactionsForAddress`. `--source helius-rpc` uses
standard methods on the Helius URL, for plans without that method. `--source publicnode` is
free and keyless but only reaches ~3 days back (§7). `--source public` uses the public mainnet
endpoint, for spot checks only. Collection is resumable: re-running `collect` picks up
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

## 7. Free data sources (no API key)

| Source | Measured 2026-10-31 | Use |
|---|---|---|
| `--source publicnode` (`solana-rpc.publicnode.com`) | ~40 requests/s burst without errors. Returns `transactionIndex` on standard methods, so intra-slot ordering is exact. **Only ~3.2 days of ledger** (first available slot 463,030,418). It rejects Python's default User-Agent, so the client sends its own. | Free pilots on launches from the last ~3 days |
| `--source public` (`api.mainnet-beta.solana.com`) | Per-method limit of about 10 calls per window (`x-ratelimit-method-limit: 10`); 429 penalties of 10 s. Paced at ~1 request/s. | Spot checks only |
| dRPC free plan | Solana is not available on the free plan | — |

**History limits are enforced, not assumed.** PublicNode is load-balanced across backends with
different retention: one backend's `getFirstAvailableBlock` was already pruned on the next. So
`history_start()` probes forward until a block time is actually served and adds a 2-hour
margin. A frame window that starts before that time is stored with `complete = 0`, is never
sampled, and is never reported as "0 launches". The first free pilot was the first run with
windows that far back: 3 of its 8 random windows (29 Oct, 16:39–20:09 UTC) fell outside
PublicNode's history, came back empty, and are now flagged. Correctly-flagged runs need the
archival Helius source for anything older than ~2.5 days.

The pipeline is paced at ≤ 10 requests/s with 4 workers on PublicNode, to be a polite user of a
free service. Anything older than ~3 days, and anything at the 500–1,000-launch scale, needs
Helius (or another archival provider).

## 8. Trial run (2026-10-31, PublicNode, 25 seconds)

Settings: one creation window (13:35–13:38 UTC), seed 42, 6 launches, 15-minute horizon.
All figures are VERIFIED on-chain data for these six coins only. They are **not** statistics
about Pump.fun launches in general.

* **Frame and sample:** 75 creation transactions in the window, 5 of them failed. Re-running with
  the same seed selected the same 6 mints.
* **Mix:** 3 standard SOL-paired coins, 1 mayhem coin and 2 non-SOL-quoted coins.
* **Data:** 222 curve transactions (40 failed), 237 trades.
* **Integrity:** **208 of 208** replay-checked trades on the standard coins matched the
  simulator exactly. The rest (mayhem and non-SOL) are excluded from the check by design.
* **Failures (40 transactions):** only 11 were Pump's own errors (10 `NotEnoughTokensToSell` in
  batch-sell transactions, 1 `TooLittleSolReceived`). The rest failed in a trading terminal's
  program (`term9…`: 23), the GMGN router's own slippage check (4) or the System program (2).
  Treating every failed transaction as a slippage failure would be wrong.
* **Example launch ("Momo"):** the creator paid 0.738 SOL and received 2.786 SOL. The first
  sell came at 9 s; the market cap peaked at 116 SOL at 8 s and was back to the 28 SOL start
  within the 15-minute horizon. 71 non-creator wallets bought.

Bugs the trial caught, now fixed and covered by tests:

* `TradeEvent.ix_name` uses its own labels (`buy_exact_quote_in` for `buy_exact_sol_in`).
  The replay must use the decoded instruction name. Both are stored now.
* Bots set slippage limits of u64::MAX ("no limit"), which overflows SQLite's signed 64-bit
  integers. Those columns are now untyped, so the value is stored exactly.
* Summaries of launches with no trades lacked the price-path columns, which broke the export.
  The columns are now always present.

## 8b. Free pilot: 40 random launches, 1-hour horizon (2026-10-31, PublicNode)

Command: `frame --random-windows 8 --days 2 --minutes 10 --seed 7 --horizon 3600`, then
`sample --n 40 --seed 7`, then `collect --max-tx 5000`. The full report, regenerated with
`python -m scripts.pilot_report`, is in [`generated/pilot_free.md`](generated/pilot_free.md). The
data is in `data/pipeline/pilot_free_exports/*.csv.gz`.

**This is VERIFIED on-chain data for 40 launches drawn from five 10-minute windows (30–31 Oct).
It is a small sample with wide confidence intervals, not an estimate for Pump.fun in general.**

* **Frame:** about 1,560 creations per hour in the usable windows. 12% of creation transactions
  failed (95% CI 10–14%). 3 of the 8 random windows fell outside PublicNode's history and were
  skipped (§7).
* **Mix:** 28% mayhem (16–43%), 30% not SOL-paired (18–45%), 18 standard. Most non-SOL coins
  were paired with other pump coins.
* **Integrity:** all 2,682 replay-checked trades on standard coins matched the simulator exactly.
* **Failures:** 82% of the 21,516 transactions touching these curves failed. 16,866 of those
  never reached Pump: bot guard and router programs aborting on purpose. Failures are highly
  concentrated: the 3 busiest launches account for 59% of them. The median launch's failure
  share is 9%.
* **Standard launches, first hour (n = 18):**
  * 89% had at least one non-creator buyer. 50% had ≥ 5 and 28% had ≥ 20.
  * The median peak market cap was 38.7 SOL (1.4× start). 17% reached 2× start.
  * The median time to peak was **2 s**, and the median final/peak ratio was 0.79.
  * One coin "graduated" at t = 0: its creator spent 86.07 SOL buying the whole curve in the
    creation transaction. It is excluded from the creator statistics, because its trading moved
    to PumpSwap, which is not collected yet.
* **Creator address only (n = 17, non-graduated):**
  * 82% sold within the hour; median first sell at 42.5 s.
  * Creators who bought and whose position could be valued (n = 14): realised plus mark-to-curve
    P&L had a median of +0.034 SOL. 9 of 14 were positive. The range was −0.044 to +1.41 SOL.
  * In 1 launch the creator address's curve balance exceeded every token outstanding. At least
    16.2 M tokens must have been transferred to other wallets. The summary now flags this instead
    of valuing a sale the curve cannot pay for, and it is direct input for wallet clustering.
  * These creator figures exclude any other wallets the creator controls, so they can understate
    or overstate the true creator-side result.

A truncated launch (`FYJnGwFN…`: 767 buyers, peak 393.6 SOL) hit the 5,000-transaction cap
inside the hour. It is flagged; Helius with a higher cap is needed to see it whole.

## 10. Main dataset: 997 launches via Helius (2026-10-31)

Command: `frame --random-windows 30 --days 14 --minutes 10 --seed 2026 --horizon 21600`, then
`sample --n 1000 --seed 2026`, then `collect --max-tx 15000`.

* **Cost:** ~69,800 estimated Helius credits in total, about 7% of the free monthly allowance.
  That includes ~2,000 credits to re-collect 38 launches after the fix below.
* **Report:** [`generated/helius_1000.md`](generated/helius_1000.md).
* **Committed data** (`data/pipeline/helius_1000_exports/`): launches, per-launch summaries,
  trades, frame windows, and the full sampling frame with selection flags.
* **Not committed:** the transaction and failed-attempt tables (56 MB compressed). They can be
  re-collected.

**Integrity**

* The Helius history for a trial launch matched the PublicNode collection exactly: same 139
  signatures, in-block indices and failure flags.
* **26,555 of 26,555** replay-checked trades on standard coins match the simulator to the
  lamport. 2 v3 buys that crossed graduation (synthetic migration) are outside the model and
  recorded as not checked.
* 3 of 1,000 sampled frame transactions were PumpSwap pool creations, not coin creations. They
  are flagged, so the sample remains a random sample of real creations.

**Bugs found by this run, fixed, with regression tests**

* **Shared transactions overwrote rows.** Multi-coin bot transactions (one transaction trading
  up to 10 coins) can touch two sampled launches. Under the old schema, which was keyed by
  signature, the second launch overwrote the first one's rows. 38 launches had lost 41
  transaction rows and 8 trade rows. Tables are now keyed by (mint, signature), the database was
  migrated, and the 38 launches were re-collected. All row counts now reconcile.
* **Transferred-in tokens were booked as profit.** When a creator address sold tokens it never
  bought on the curve, the summary counted the proceeds as zero-cost realised profit. This
  inflated the largest creator "profit" from 7.7 to 38.6 SOL. Those proceeds are now reported
  separately and never counted as profit.

**Results** (VERIFIED on-chain data; 30 random 10-minute windows, 17–31 Oct 2026; 6-hour horizon)

| | |
|---|---|
| Creation rate | ~1,740 coins per hour; 6% of creation transactions failed |
| Coin types | 29% mayhem (26–32%), 29% non-SOL quote (61 quote mints; PUMP most common, USDC 14 coins), **430 standard** |
| Transactions touching curves | 80% failed. 94% of failures never reached Pump (bot guard and router programs) |

**Standard launches (n = 430)**

| Measure | Result |
|---|---|
| Any non-creator buyer | 69% (64–73%) |
| ≥ 5 non-creator buyers | 43% |
| ≥ 20 non-creator buyers | 22% |
| Non-creator buy volume over 6 h | median **0.50 SOL** (IQR 0–10.6) |
| …of which in the first 30 s | median 0.29 SOL |
| Peak market cap | median 33.9 SOL (1.2× start) |
| Time to peak | median **2 s** |
| Reached 2× start | 19% (15–23%) |
| Graduated within 6 h | 3% (11 launches; 5 of them were creators buying the whole curve for 86.07 SOL at creation) |
| Non-creator buying after 2 min / 10 min / 1 h | 33% / 17% / 7% |
| Ended within 5% of the start market cap | 92% of launches with trades |
| Reached 2× and then fell back to the start | 14% |

**Creator address only, non-graduated standard launches (n = 419).** Other wallets the creator
controls are not included.

| Measure | Result |
|---|---|
| Sold within 6 h | 74% |
| Time to first sale | median **19 s** |
| Bought and fully exited, starting within 60 s | 54% of those who bought |
| Realised + mark-to-curve P&L | median ≈ 0 SOL; 45% positive (40–50%); range −0.27 to +7.7 SOL |
| Creator address moved tokens out | 1% |
| Creator address sold tokens received by transfer | 1%; proceeds median 2.6 SOL, max 35.6 SOL |

These signals are input for wallet-relationship scoring.

**What this does and does not say about the 10 SOL question.** Phase 1 showed that a 10 SOL
first buyer breaks even only if about 0.58 SOL of independent buying is *still held* when the
buyer exits. In this sample, the median standard launch received **0.50 SOL of non-creator
buying in total over 6 hours**. Most of it arrived in the first 30 s, the median peak came 2 s
after creation, and 92% of launches ended back at their starting market cap. That points
against a profitable first-buyer strategy for typical launches. It is not yet a backtest:

* "Non-creator" buying includes bots that exit within seconds.
* A hypothetical 10 SOL position would itself change what later traders do.
* Creator-controlled wallets are not yet separated from independent buyers.

Phase 3 (an exit-timing backtest with out-of-sample testing) is needed for a verdict.

## 9. Not yet collected (next steps)

1. **Post-graduation trading.** After migration, trades happen on the PumpSwap pool (address
   stored in `launches.pool`) and no longer touch the bonding curve. This needs a second
   collector for pool history.
2. **Wallet funding history** for relationship scoring (who funded each early buyer, and when).
3. **Mayhem-mode pricing** in the simulator. In small samples, mayhem coins were roughly 40–60%
   of new launches, so excluding them must be reported as a scope limitation.
4. **Analyses on the collected data:** exit-timing backtests (1–120 s, activity-based) with
   out-of-sample splits, external-demand frequencies, and creator outcome distributions.
