# Phase 1: Pump.fun bonding-curve mechanics and mathematics (verified 2026-10-31)

Scope of this phase, as requested: (1) verify the *current* Pump.fun mechanics against
authoritative sources, (2) work out the mathematics, (3) build a small simulator and prove it
reproduces real on-chain behaviour. The historical backtesting pipeline, wallet clustering,
prediction models, dashboard, J7Tracker verification and legal/tax research are **not** in
this phase. Section 9 lists what is left.

Labels used throughout:

* **VERIFIED**: read from mainnet state, decoded from mainnet transactions, or stated in Pump's
  own docs, SDK or IDL. A citation is given.
* **DERIVED**: follows mathematically from verified facts. The derivation is shown.
* **HYPOTHETICAL**: a simulator calculation under stated assumptions. It is not market data.
* **ASSUMPTION**: a value you must supply, such as priority fees or failure rates.

All generated numbers come from `python -m scripts.phase1_tables`
(output: [`docs/generated/phase1_tables.md`](generated/phase1_tables.md)).

---

## 0. Short answers

| Question | Answer | Status |
|---|---|---|
| Does buying then selling the same tokens return the curve to its start? | Yes. The token reserves return exactly and the SOL reserves are within 1–2 lamports. The rounding dust always stays in the curve. | DERIVED + verified in tests |
| Does a launch with no external buyers "only cost transaction fees"? | **No.** Platform fees are 1.25% per side, so an immediate 10 SOL round trip loses **0.2469 SOL (2.469%)**. That is about 25,000 times the 2 × 5,000-lamport base fee. If you are the creator, the creator-fee share comes back and the loss is 0.1877 SOL (1.877%), less the cost of claiming it. | DERIVED, exact integer ledger in §5 |
| Does splitting a position across wallets change what the curve pays? | Not for one owner trading alone: the total is the same to within a few lamports of rounding. Every extra wallet adds only per-transaction costs. Across *different* owners, order decides who gets which price. | DERIVED + tests |
| Do sells in the same slot get the same price? | No. A leader executes transactions one after another, and each sell sees the price left by the previous one. 5 × 2 SOL sells: 45.08 → 28.74 lamports per token. | DERIVED |
| How much external buying does a first-in 10 SOL buyer need to break even? | **0.58 SOL** of independent buying that is *still held* when the operator sells (0.44 SOL if the operator is the creator). Every lamport of that profit is a loss to those later buyers. | DERIVED (closed form = integer bisection) |
| Is the curve zero-sum? | Yes. Before graduation, the sum of every trader's SOL P&L is exactly −(fees) − (SOL left in the curve). The curve creates no value. One participant's profit is other participants' loss plus fees. | DERIVED + tested identity |
| Is the starting market cap (~$3,000) a choice? | No, not for SOL-paired coins. Every coin starts at 30 virtual SOL / 1.073B virtual tokens, a market cap of 27.959 SOL (≈ US$2,680 at US$95.80/SOL on 2026-10-31). USDC-paired coins start at US$4,000. | VERIFIED |

These answers settle the mathematics. They do **not** answer whether a 10 SOL strategy has
positive expected value. That depends on how often independent buyers arrive, and whether they
are still holding when you exit. Both need the historical dataset planned for Phase 2.

---

## 1. Sources and verification method

| Ref | Source | What it established |
|---|---|---|
| D1 | `github.com/pump-fun/pump-public-docs` @ `639b87f` (2026-10-13): README, PUMP_PROGRAM_README, FEE_PROGRAM_README (+ `fees.png`), TRADE_V3, SYNTHETIC_MIGRATION, SWEEP_FEES, HOLDER_REWARDS, FEE_RECIPIENTS, BUY/SELL, COIN_CREATION | Program architecture, instruction set, fee mechanism, graduation |
| D2 | `idl/pump.json`, `idl/pump_fees.json` (same commit) | Exact account/event/instruction layouts used by the decoder |
| S1 | `@pump-fun/pump-sdk` 4.1.0 (npm, MIT): `src/bondingCurve.ts`, `src/fees.ts` | Integer buy/sell/fee formulas |
| S2 | `pump-rust-client` 0.4.1 (crates.io): `src/math/fees.rs` | Exact-input fee rule ("fees charged on the unshaved net") |
| C1 | Mainnet `Global` account `4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf`, slot 463,702,641 | Live curve parameters (snapshot: `data/onchain_config_snapshot.json`) |
| C2 | Mainnet Pump Fees `FeeConfig` accounts `8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt` (bonding curve) and `5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx` (PumpSwap) | Live fee tiers |
| C3 | Mainnet `getMinimumBalanceForRentExemption` | Current rent deposits |
| C4 | Jito block engine `getTipAccounts` | The 8 tip accounts |
| SD | Solana docs source (`solana-foundation/solana-com`, `docs/core/fees`) | Base fee 5,000 lamports/signature (50% burned), priority fee = ceil(CU price × CU limit / 1e6), 100% to the validator, charged even if the tx fails; v1 transactions set the priority fee as an absolute lamport total |
| V1 | `scripts/validate_against_chain.py` → `data/validation_report.json` | Simulator vs 431 real trades |
| V2 | `scripts/replay_launch.py` → `data/replays/HdgBqp…pump.json` | Simulator vs one coin's full history (200 trades) |
| P | Kraken and Coinbase public spot, 2026-10-31 ~13:35 UTC | SOL ≈ US$95.80 / CA$133.84, used only to convert to USD |

How the validation was done (VERIFIED, V1 and V2):

* **Single-trade check (V1).** 1,000 recent signatures of the Pump program were fetched. For 400
  successful transactions, the decoder read every trade instruction's arguments and the
  `TradeEvent` it emitted. It rebuilt the pre-trade curve state, ran the simulator with the same
  arguments, and compared tokens, curve SOL, protocol fee, creator fee and post-trade reserves to
  the lamport. Result: **all 256 trades on standard SOL-paired coins matched on every field**
  (9 instruction variants: `buy`, `buy_v2`, `buy_v3`, `buy_exact_sol_in`,
  `buy_exact_quote_in_v2/v3`, `sell`, `sell_v2`, `sell_v3`). Their fee rates also matched the
  live schedule, 256 of 256. All 114 mismatches were zero-fee trades on *mayhem-mode* coins
  (the mayhem AI agent, which follows different rules; out of scope).
* **Full-history replay (V2).** One coin (`HdgBqpF1z57Tmsy7jdnJJfoRafSKi9CBKxFkBPo3pump`) was
  replayed from its `CreateEvent`, carrying the *simulated* state forward so any error would
  compound. Result: **200/200 trades exact**. The first run found one 1-lamport fee
  discrepancy. It came from an exact-input edge case (§3.3) that S2 documents; the fix is now
  in `curve.py` and has a regression test.

---

## 2. Current Pump.fun mechanics (VERIFIED unless marked)

### 2.1 Programs and accounts

| Item | Value | Ref |
|---|---|---|
| Bonding-curve program | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | D1 |
| PumpSwap AMM (post-graduation) | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | D2 |
| Pump Fees program | `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ` | D2 |
| Global config | `4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf` (PDA `["global"]`) | D1, C1 |
| Per-coin curve | PDA `["bonding-curve", mint]` | D1 (derivation re-implemented and checked against the doc examples) |

### 2.2 Curve parameters (live, slot 463,702,641)

| Parameter | Raw | Human |
|---|---|---|
| `initial_virtual_token_reserves` | 1,073,000,000,000,000 | 1.073 B tokens (6 decimals) |
| `initial_virtual_sol_reserves` | 30,000,000,000 | 30 SOL |
| `initial_real_token_reserves` | 793,100,000,000,000 | 793.1 M tokens sellable on the curve |
| `token_total_supply` | 1,000,000,000,000,000 | 1 B tokens |
| `pool_migration_fee` | 15,000,001 lamports | ≈ 0.015 SOL taken at migration |
| `initial_virtual_quote_reserves` (USDC coins) | 4,292,000,000 | 4,292 USDC virtual, i.e. US$4,000 start market cap |

The remaining 206.9 M tokens (1 B − 793.1 M) never trade on the curve. They seed the PumpSwap
pool at graduation (D1).

DERIVED from these numbers:

* Starting price: 30 / 1.073e9 SOL per token = 27.96 lamports per whole token. Starting
  displayed market cap: **27.959 SOL**.
* Graduation happens when `real_token_reserves` reaches 0. At that point virtual tokens =
  279.9 M, so virtual SOL = 30 × 1.073 / 0.2799 = **115.005 SOL**. Real SOL raised is
  **≈ 85.005 SOL** and the market cap is **≈ 410.9 SOL** (≈ US$39,400). The test
  `test_graduation_point` checks this.

### 2.3 Fees on the bonding curve

* The curve's fee rates come from the Pump Fees `FeeConfig` (C2), not from `Global`. The
  bonding-curve `FeeConfig` has **one tier starting at 0 SOL market cap: protocol 95 bps,
  creator 30 bps, LP 0. Total 1.25% on every buy and every sell.** The SDK only falls back to
  the legacy `Global.fee_basis_points` = 95 and `creator_fee_basis_points` = 5 if no
  `FeeConfig` exists (S1).
* How fees are charged (S1, confirmed in V1):
  * **Buy:** fees are added *on top of* the SOL that enters the curve.
  * **Sell:** fees are *deducted from* the SOL that leaves the curve.
  * Each fee is `ceil(amount × bps / 10,000)`, rounded up separately for protocol and creator.
  * Fees never enter the curve reserves, so they do not move the price.
* **Buyback:** `Global.buyback_basis_points` = 5000. Half of the protocol fee goes to one of 8
  "buyback fee recipients", rounded down (`buyback_fee = floor(fee × 5000 / 10000)`, 264 of
  264 trades). It is a split of the 0.95% fee, **not** an extra charge.
* **Creator fee:** 0.30% of every trade accrues to the coin's `creator` and is claimed with
  `collect_creator_fee`. With v3 trades it waits on the curve until a permissionless sweep
  (D1 SWEEP_FEES). A creator can split it through fee sharing (D1 CREATOR_FEE_SHARING). On a
  *holder-rewards* coin (new, Sep 2026) the creator fee goes to holders instead of the creator
  (D1 HOLDER_REWARDS). *Cashback* coins are deprecated: no new ones can be created.
* **After graduation**, PumpSwap fees follow 25 market-cap tiers (C2). Below 420 SOL market
  cap: LP 2 + protocol 93 + creator 30 = 1.25%. Above that, the total falls from 1.20% to
  0.30% (≥ 98,240 SOL). `pumpsim/params.py: PUMPSWAP_TIERS_SOL` holds the full table.

### 2.4 Instructions (all share the same price maths)

* **Exact-output buys:** `buy`, `buy_v2`, `buy_v3` take a token `amount` and `max_sol_cost`.
* **Exact-input buys:** `buy_exact_sol_in`, `buy_exact_quote_in_v2/v3` take a budget and
  `min_tokens_out`.
* **Sells:** `sell`, `sell_v2`, `sell_v3` take `amount` and `min_sol_output`.
* **v2 vs v3:** v2 has a unified 27-account interface. v3 (Oct 2026) uses 17 accounts and keeps
  fees on the curve until swept. Prices and fees are the same (D1, and V1 confirms it).
* **Synthetic migration (v3 only):** the buy that empties the curve may continue into the
  "pool-to-be" at the price PumpSwap would charge. After that buy, every trade fails until
  `migrate` runs (D1). Not modelled in Phase 1.
* **Partial fill:** on legacy/v2 instructions, a buy larger than what is left either fails or
  partially fills.

### 2.5 Coin variants seen on mainnet now

Only the first is in Phase 1 scope:

| Variant | Notes |
|---|---|
| Standard SOL-paired | Parameters in §2.2. Simulator validated. |
| USDC-paired | Same maths, 4,292 USDC virtual quote. Fees from `stable_fee_tiers` (currently also 95/30). |
| Pump-coin-paired (Oct 2026) | Quote is another pump coin (up to depth 2). The starting reserves are derived from the quote chain. |
| Mayhem mode | Separate mayhem program, adjusted virtual parameters and agent trades at 0 fee. **Not modelled.** It was 167 of the 431 sampled trades. |
| Holder-rewards | Creator fee goes to holders. Trading maths unchanged. |

### 2.6 Network and execution facts

* **Fees:** base fee 5,000 lamports per signature. Priority fee =
  `ceil(CU_price × CU_limit / 1e6)` lamports for legacy/v0 transactions, or an absolute total
  for v1 transactions. **Fees are charged even when the transaction fails** (SD).
* **Rent:** rent deposits are lower than Pump's docs state (C3). Token account: 1,488,440
  lamports (165 B, SPL) or 1,513,840 lamports (170 B, Token-2022, used by `create_v2` coins).
  `user_volume_accumulator`: 1,346,200 lamports, once per wallet. **Rent is refundable** when
  the account is closed.
* **Jito tip accounts** (C4): `96gYZG…`, `HFqU5x…`, `Cw8CFy…`, `ADaUMi…`, `DfXygS…`, `ADuUkR…`,
  `DttWaM…`, `3AVi9T…`. A Jito bundle is all-or-nothing. A bundle that fails does not land and
  pays nothing.
* **v1 transactions** are live. RPC clients must request `maxSupportedTransactionVersion: 1`.
  Older tooling breaks on them; ours did until fixed.
* **Observed in V1** (VERIFIED for those seconds only; *not* a population estimate):
  * Failed Pump-program transactions: 334 of 1,000 signatures (13:35:50–13:36:05 UTC), 1,102
    of 2,000 (13:24:56–13:25:21) and 177 of 200 in an earlier window.
  * Median total network fee per successful transaction: 22,452 lamports. 90th percentile:
    327,097.
  * 7.7% of sampled successful transactions paid a Jito tip.
  * Measuring failure rates properly needs the Phase 2 dataset.

### 2.7 Outdated documentation found

| Claim | Where | Current value |
|---|---|---|
| `fee_basis_points: 100`, `creator_fee: 0` | D1 PUMP_PROGRAM_README `Global` example | `Global` now reads 95 / 5. Both are overridden by `FeeConfig` 95 / 30 (1.25% total). |
| "1% fee", "0.95% + 0.05% creator" | Older Pump docs and much third-party content | Superseded by the `FeeConfig` schedule (Sep 2025 onward) |
| Migration to Raydium | D1 describes `withdraw` as the now-disabled Raydium path | Graduation now migrates to PumpSwap through permissionless `migrate` / `migrate_v2` |
| Volume-accumulator rent 0.0018444 SOL | D1 BUY / TRADE_V3 | 0.0013462 SOL (C3) |
| PumpSwap tier creator fees 0.275%, 0.225%, 0.175%, 0.125%, 0.075% | D1 `fees.png` | Chain stores whole bps: 28, 23, 18, 13, 8 (C2) |
| `buy_exact_sol_in` token formula `(budget − 1) × 10⁴ / (10⁴ + bps)` | Older SDK helper (still in S1 `getBuyTokenAmountFromSolAmount`) | Program uses the `exact_quote_in_fees` net, then `net − 1`. The old formula matched 73 of 99 exact-in trades; the current one matched 99 of 99. |

---

## 3. Bonding-curve mathematics

Notation:

* `vQ`, `vT`: virtual SOL (quote) and token reserves.
* `rQ`, `rT`: real reserves. On every trade, `vQ − rQ = 30 SOL` and `vT − rT = 279.9 M`.
* `k = vQ · vT`.
* `V = 30 SOL`, `T = 1.073e9` tokens: the initial virtual reserves.
* `f = 0.0125`: total fee rate per side. Of that, `c = 0.003` is the creator part.

### 3.1 Exact integer formulas used by the program (VERIFIED: S1, S2, V1)

```
buy exact tokens t:   q_net   = floor(t·vQ / (vT − t)) + 1        user pays q_net + ceil(q_net·95/1e4) + ceil(q_net·30/1e4)
buy exact budget B:   net0    = floor(B·1e4 / (1e4 + 125)); fees on net0 (ceil each); net = net0 − max(0, net0 + fees − B)
                      t       = floor((net − 1)·vT / (vQ + net − 1))   curve receives net, user pays net + fees(net0) ≤ B
sell t tokens:        q_gross = floor(t·vQ / (vT + t))                user receives q_gross − ceil(q_gross·95/1e4) − ceil(q_gross·30/1e4)
state update:         vQ += q (buy) / −= q (sell);  vT −= t (buy) / += t (sell); real reserves move identically
```

Every rounding favours the curve. Because of the `+1` on buys and the floor on sells, `k`
never decreases.

### 3.2 Continuous form

Ignore rounding and let `s` be the tokens currently held by traders (`vT = T − s`). Then
`vQ = k/(T − s)`, and the real SOL in the curve is

```
R(s) = vQ − V = V·s / (T − s)            (convex, increasing)
```

A trade that moves holdings from `s₁` to `s₂` costs or pays exactly `R(s₂) − R(s₁)` before fees.
**So the SOL a trade moves depends only on its start and end states, not on the path between them.**

Buying with net SOL `N` from a fresh curve:

* Tokens received: `T·N/(V + N)`.
* New spot price: `(V + N)²/(V·T)`, a multiple of `((V + N)/V)²` of the start price.
* For a 10 SOL buy (N = 9.877): **26.58% of supply, price ×1.77**.

### 3.3 Reversibility theorem

*Claim.* Start at state S₀ and buy `t` tokens for net `N`, with no other trade on the curve.
Then sell the same `t`. Without fees the curve returns to S₀ and pays back exactly `N`. With
the integer rounding above, `vT` returns exactly and the curve keeps 0–2 lamports.

*Proof.* The buy moves holdings from `s₀` to `s₀ + t` and costs `R(s₀ + t) − R(s₀)`. The sell
moves holdings from `s₀ + t` back to `s₀` and pays `R(s₀ + t) − R(s₀)`. In integers, the buy
charges `floor(·) + 1` and the sell pays `floor(·)` of the same ratio, so the curve keeps at
most 1–2 lamports. ∎

The exact 10 SOL ledger is in §5 (1 lamport of dust).
`test_no_fee_round_trip_is_exactly_reversible_up_to_rounding` checks budgets from 1,000
lamports to 50 SOL.

### 3.4 With fees: the minimum cost of a no-buyer launch

A gross spend `G` puts `N = G/(1 + f)` into the curve. Selling it back pays `N` gross, of which
the trader keeps `N(1 − f)`. So

```
recovered / spent = (1 − f)/(1 + f)            → loss = 2f/(1 + f) = 2.4691 % at f = 1.25 %
if you are the creator (creator fees come back to you):
recovered / spent = (1 − f + 2c)/(1 + f)       → loss = 1.8765 %
```

**The loss fraction does not depend on trade size or on the starting reserves `V`.**
Table D checks V = 10 to 100 SOL, and `test_round_trip_loss_independent_of_size_and_start_reserves`
tests it. So the claim that a launch with no buyers "only costs transaction fees" is false
under current fees. Platform fees are about 0.25 SOL per 10 SOL round trip; network fees are
0.00001 SOL with the base fee only.

### 3.5 Splitting across wallets (one owner, nobody else trading)

By path independence (§3.2), selling `t` tokens in `k` pieces from any wallets, in any order,
returns the same gross as one sell, to within a few lamports of rounding. Fees are linear in
the amount, so their total is also the same, again to within rounding.

**So multiple wallets create no curve-level advantage. They only add per-transaction costs:**

* `k − 1` extra transactions on each side, each paying base + priority fee (+ tip).
* A refundable rent deposit per wallet.

Table A2 shows this: 10 wallets at "aggressive" fees turn a 2.469% loss into 2.870%. Test:
`test_path_independence_same_owner_split_sells`.

### 3.6 Zero-sum identity

Sum the SOL flows of every trade on one curve before graduation:

```
Σ_traders (SOL received − SOL paid) = −(protocol fees + creator fees) − (rQ_end − rQ_start)
```

When every holder has sold, `rQ_end − rQ_start` is just rounding dust. **The curve is a closed
pot: no participant can make SOL that another participant did not put in.** The creator is
the one exception: the creator-fee income is paid by every trader, including the creator.

Test: `test_zero_sum_identity`. This identity sets the frame for the profitability question.
Any positive return on the curve is a transfer from other traders.

### 3.7 First-in operator with external buyers (closed form)

Setup: the operator buys first with net `N` on a fresh curve. Independent buyers then add net
`X`. The operator then sells everything while those buyers still hold. Let `A = V + N + X` and
`B = V + N`. The operator's gross exit is

```
gross(X) = N · A² / (V·B + N·A)                          (gross(0) = N: reversibility)
d gross/dX at X = 0  = N(2V + N)/(V + N)²                 (share of the first external SOL the operator captures)
break-even (non-creator): (1 − f)·gross(X*) = (1 + f)·N
    → A* = [ρN + √(ρ²N² + 4ρVB)]/2,  ρ = (1 + f)/(1 − f),  X* = A* − B
```

*Derivation.* After the operator's buy, `vT = VT/(V + N)` and the operator holds `TN/(V + N)`.
After the external buys, `vT = VT/A`. Selling the operator's tokens raises `vT` to
`T[V/A + N/B]`, which gives `vQ = V/(V/A + N/B)`. Then
`gross = A − V/(V/A + N/B) = N A²/(VB + NA)`.

Results (Table C):

* A 10 SOL first buyer breaks even when **0.5806 SOL** of later buying (0.5735 SOL net of the
  buyers' own fees) is still held at the moment of exit. The integer bisection agrees with the
  closed form to < 0.001 SOL (`test_break_even_external_inflow_closed_form_vs_integer`).
* As the creator, the break-even is 0.4357 SOL.
* The first-external-SOL capture rate rises with size: 6% for a 1 SOL operator, 43% for 10 SOL,
  64% for 20 SOL.

**The order of exits decides everything** (Table B, `test_who_exits_first_wins`). With 2 SOL
of external buying:

| Who exits first | Operator P&L | External buyers' P&L |
|---|---|---|
| Operator | +0.613 SOL | −0.909 SOL |
| External buyers | −0.247 SOL (the plain round-trip loss) | — |
| Half the external tokens sold before the operator exits | +0.169 SOL | — |

"External buying volume" is the wrong metric on its own. What matters is **net external
holdings at the moment you exit**, and whether your sell executes before theirs.

### 3.8 Same-slot execution, ordering and slippage

* **Same slot does not mean the same price.** A leader executes transactions in sequence, and
  each one reads the state left by the previous one. Five 2 SOL wallets selling back-to-back in
  one slot got average prices of 45.08, 39.04, 34.65, 31.33 and 28.74 lamports per token
  (Table F).
* **Ordering among your own wallets** does not change your total (§3.5). Ordering relative to
  *other people's* transactions does (§3.7).
* **Slippage tolerance interacts with batching.** A batch tool may compute every wallet's
  `min_sol_output` from the same pre-batch quote. Then each later sell sees a lower price and
  fails its own check. With 5 equal wallets:

  | Tolerance | Sells that land (of 5) | Net SOL recovered (of 9.753 possible) | Tokens left unsold |
  |---|---|---|---|
  | 1% | 1 | 2.99 | 199 M |
  | 15% | 2 | — | — |
  | 30% | 3 | — | — |

  Each failed sell still pays base + priority fee (SD). Quoting each sell from the state the
  previous one leaves, or putting all sells in one transaction or bundle, lands all 5
  (`test_batch_sell_pre_batch_quotes_fail_with_tight_slippage`).
* **Unsold tokens after a failure** are not lost in a closed system: retrying recovers the
  same total minus extra fees (path independence). In a live market, the delay exposes them
  to other sellers.
* **Non-atomic multi-wallet buys** leave gaps that third parties can fill (Table H). Take a
  10 SOL launch spread over 5 separate transactions. If someone inserts a 3 SOL buy after your
  first wallet and sells before you exit, they make **+1.316 SOL** and your loss grows from
  0.247 to **1.637 SOL**. Creating the coin and buying in a single transaction (`create` +
  `buy` in one tx) leaves no gap at all.

### 3.9 Displayed value vs executable liquidation value

For a position bought with net `N` from a fresh curve, nobody else trading:

```
displayed value (tokens × spot price) = N(V + N)/V          → overstates what the curve pays by a factor of 1 + N/V
curve value (sell everything, no fees) = N
net recovered                           = N(1 − f) − network costs
```

For the 10 SOL position (Table E):

| Measure | Value |
|---|---|
| Displayed market cap | 49.40 SOL (≈ US$4,730) |
| Displayed position value | 13.128 SOL |
| What a single sell can return before fees | 9.877 SOL (24.8% price impact vs spot) |
| What you actually get after fees | **9.753 SOL** |

After +5 SOL of external buys, the display says 16.58 SOL and the executable net is 11.95 SOL
(72% of the displayed figure). `python -m pumpsim liquidate` runs this calculator.

### 3.10 Does the starting market cap matter?

* For SOL-paired coins the start is fixed in SOL (V = 30). The USD figure only moves with the
  SOL price.
* Counterfactually varying `V` (Table D): the **round-trip fee loss is unchanged (2.469%)**.
  What changes is the share of supply a given SOL buy acquires, the price multiple, and the
  break-even inflow. For a 10 SOL buy, a smaller `V` means a larger share and a lower
  break-even. The economics depend on the ratio **N/V**, not on the dollar market cap.

### 3.11 Graduation boundary

The curve completes when the last real token is bought, after ≈ 85.005 SOL has been raised
(§2.2). A position still held then moves to the PumpSwap pool, which has its own fee tiers
(§2.3). Fees are 1.25% below 420 SOL market cap, so a fresh graduate's round-trip cost is
similar. Pool depth at migration is ≈ 85 SOL less the 0.015 SOL migration fee, against
206.9 M tokens. Post-graduation modelling is Phase 2.

---

## 4. The three 13A scenarios: 10 SOL, operator buys first

All rows are HYPOTHETICAL (Table B). Assumptions: base fee only, operator not the creator,
operator exits first unless stated.

| Scenario | Interpretation | Operator net P&L |
|---|---|---|
| No external buyers | Round trip | **−0.247 SOL** (−2.47%) |
| A few buyers, 0.25–0.5 SOL total, held | Below break-even | −0.141 to −0.034 SOL |
| A few buyers, 1 SOL, half sold before you exit | Below break-even | −0.037 SOL |
| Significant buying, 5 SOL, held, you exit first | Profit comes from those buyers | **+1.95 SOL** (buyers: −2.32 SOL) |
| Significant buying, 5 SOL, they sell half first | | +0.77 SOL |
| Any amount, external buyers exit before you | They recover their SOL; you get the round trip | −0.247 SOL |

Real outcomes also depend on bots that buy in the same or next slot and exit within seconds.
In the one replayed launch (VERIFIED, n = 1, illustrative only):

* The creator bought 0.675 SOL at t = 0 s and sold at t = 6 s.
* The market cap peaked at 139 SOL at t = 19 s and was back at 28.0 SOL (the start level) by
  t = 36 s.
* 91 distinct wallets traded.
* 50 of 175 transactions touching the curve failed.

How often each scenario occurs is **unknown until Phase 2**.

---

## 5. Exact reversibility ledger (13B), 10 SOL, nobody else trades

DERIVED by the integer simulator; every field is reproduced by the code validated in §1.

| State | virtual SOL | real SOL | virtual tokens | real tokens |
|---|---:|---:|---:|---:|
| initial | 30,000,000,000 | 0 | 1,073,000,000,000,000 | 793,100,000,000,000 |
| after buy | 39,876,543,209 | 9,876,543,209 | 807,241,486,106,100 | 527,341,486,106,100 |
| after sell | 30,000,000,001 | 1 | 1,073,000,000,000,000 | 793,100,000,000,000 |

| Item | Lamports |
|---|---:|
| Budget (`buy_exact_sol_in`) | 10,000,000,000 |
| Net into curve | 9,876,543,209 |
| Buy protocol fee / creator fee | 93,827,161 / 29,629,630 |
| Tokens received (base units) | 265,758,513,893,900 (265.76 M tokens, 26.58% of supply) |
| Gross out of curve on sell | 9,876,543,208 |
| Sell protocol fee / creator fee | 93,827,161 / 29,629,630 |
| **SOL received** | **9,753,086,417** |
| **Net loss** | **246,913,583** (platform fees 246,913,582 + 1 lamport dust) |
| Network (assumption: 1 signature, no priority) | + 10,000 |

How each execution factor in 13B changes this result:

| Factor | Effect |
|---|---|
| Trading fees | 2 × 1.25%. This is the dominant cost: 0.2469 SOL. |
| Creator fees | 0.0593 SOL of the above. It comes back if you are the creator, after a claim/sweep transaction. |
| Priority fees / Jito tips | Your assumption. 0.001 + 0.001 SOL per transaction adds 0.004 SOL for one wallet and 0.040 SOL for 10 wallets (Table A2). |
| Integer rounding | ≤ 2 lamports per trade, always in the curve's favour. |
| Transaction ordering / concurrent trades | The only factor that can change the result by more than fees. Table H shows a range from −1.64 SOL to +0.12 SOL depending on who gets in between and who exits first. |
| Slippage | Tight tolerance plus pre-batch quoting causes failures and stranded tokens (Table F). |
| Failed transactions | Each one costs base + priority fee and leaves the position open. |
| Migration | If the curve completes while you hold, your exit is on PumpSwap at pool depth (§3.11). |

---

## 6. J7Tracker questions answered by mathematics alone

J7Tracker's actual implementation was **not verified** in this phase. I made no claims about
its features, batch size, atomicity or submission path. The answers below follow from curve
mathematics and apply to any tool.

1. *Does sell order change total recovery?* Not among your own wallets when nobody else
   trades (§3.5, a few lamports at most). Yes relative to other traders (§3.7).
2. *Does batch selling give an execution advantage?* It cannot raise the curve's total payout.
   It costs extra per-transaction fees. Its only possible benefit is speed and atomicity
   relative to *other* sellers, and that depends on the tool's actual submission path (Phase 2).
3. *Does same-slot execution guarantee similar prices?* No (§3.8).
4. *How much can ordering matter?* Between −1.64 and +0.12 SOL on a 10 SOL position in the
   Table H examples. It scales with how much other people buy or sell in between.
5. *What if some wallet transactions fail?* The fee is paid, the tokens stay unsold, and later
   sells face whatever state exists then (Table F).
6. *J7Tracker's actual mechanics vs its advertising:* **open.** It needs its documentation plus
   on-chain inspection of transactions it submits.
7. *Theoretical vs executable liquidation value:* §3.9 and Table E.

---

## 7. The simulator (`pumpsim/`)

| Module | Purpose |
|---|---|
| `curve.py` | Integer-exact state, buy (exact-out and exact-in), sell, fee tiers. Validated: 256/256 trades plus a 200-trade full replay. |
| `params.py` | Verified constants with provenance; `load_snapshot_params()` rebuilds them from a fresh on-chain snapshot. |
| `analysis.py` | Closed forms, round trips, multi-wallet, external-demand scenarios, zero-sum ledger, break-even, liquidation calculator, batch-sell slippage. |
| `costs.py` | Network cost model. Every non-verified number is an explicit assumption. |
| `onchain/` | Read-only RPC client, IDL-derived Borsh decoder, PDA derivation, event and instruction decoding. |
| `scripts/` | `fetch_onchain_config`, `validate_against_chain`, `replay_launch`, `phase1_tables` |

Commands:

```
python -m pytest -q                                   # 16 offline tests
python -m scripts.fetch_onchain_config                # re-verify parameters (mainnet, read-only)
python -m scripts.validate_against_chain --tx 400     # re-validate against fresh trades
python -m scripts.replay_launch --auto                # replay one recent launch end-to-end
python -m scripts.phase1_tables --sol-usd <price>     # regenerate every table in this report
python -m pumpsim roundtrip --sol 10 --wallets 5 --priority-lamports 20000
python -m pumpsim external --sol 10 --external 0.5,1,2 --sell-before-exit 0.5
python -m pumpsim liquidate --tokens 265758513.89 --after-buys 10,5 --cost-basis 10
python -m pumpsim breakeven --sol 10 [--creator]
```

Known limitations:

* No mayhem coins.
* No synthetic-migration leg or post-graduation PumpSwap trading.
* No pump-coin-quoted chain pricing.
* Fee tiers are modelled but currently flat on the curve.
* The public RPC is heavily rate-limited (`getSignaturesForAddress` ≈ 10 per window), which is
  enough for validation but not for a 1,000-launch dataset.

---

## 8. What the mathematics already rules out

* **"A no-buyer launch only costs gas."** False: about 2.5% of capital per round trip (1.9% as
  the creator).
* **"More wallets help the exit."** False for one owner. They only add costs and failure
  surface.
* **"Same block means same price."** False.
* **Market cap as a measure of what you own.** Displayed value overstates executable value by a
  factor of 1 + N/V, even before fees.
* **Positive expected value inside the curve without other people's losses.** Impossible by the
  zero-sum identity. Any profit requires later buyers who still hold when you exit, or
  creator-fee income from their volume.

That last point matters for the legitimacy boundary you set. Mathematically, a launch strategy
that profits on the curve has the same payoff structure as selling into later buyers. Whether
it is lawful turns on representations, disclosure and whether demand is genuine, not on the
maths. Phase 4 (legal) needs to address it directly.

---

## 9. Proposed next phase (not started; awaiting your go-ahead)

1. **Data access.** The public RPC is too slow for 500–1,000 launches. Options: a paid RPC
   with archival history (Helius / Triton / QuickNode), or an indexer (Bitquery, Dune `pumpdotfun`
   tables). These need your decision and possibly an API key.
2. **Launch extractor.** Generalise `replay_launch.py`: for each coin, store every trade with
   slot, intra-slot order, signer, fees, failures and Jito tips in SQLite. Sample launches
   *at random from the creation stream*, not from survivors.
3. **Empirical rates.** Measure how often independent buying arrives, how much and for how
   long. Capture sniper share, failure rates and holder concentration.
4. **Exit-timing backtests** (1–120 s, activity-based) with out-of-sample splits, using the
   validated simulator for every hypothetical fill.
5. **Wallet-relationship scoring** (funding graph, timing) with explicit confidence levels.
6. **J7Tracker verification** and the Canadian legal/tax review.
