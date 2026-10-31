"""Offline tests for the launch-data pipeline. Trade rows below are SYNTHETIC test inputs built with
the simulator; they exist only to check the accounting code, not to represent market data."""
import time

import pytest

from pumpsim import VERIFIED, CurveState
from pumpsim.curve import buy_exact_quote_in, sell_exact_tokens_in
from pumpsim.onchain.decode import failure_info, pump_error_from_logs
from pumpsim.pipeline import db
from pumpsim.pipeline.collect import random_windows
from pumpsim.pipeline.sources import HeliusSource
from pumpsim.pipeline.summarize import summarize_launch

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
L = 10**9


def test_failure_info_pump_error():
    logs = ["Program ComputeBudget111111111111111111111111111111 invoke [1]",
            "Program ComputeBudget111111111111111111111111111111 success",
            f"Program {PUMP} invoke [1]",
            "Program log: AnchorError occurred. Error Code: TooLittleSolReceived. Error Number: 6003.",
            f"Program {PUMP} failed: custom program error: 0x1773"]
    fi = failure_info(logs)
    assert fi == {"program": PUMP, "code": 6003, "name": "TooLittleSolReceived", "pump_reached": True}
    assert pump_error_from_logs(logs) == "TooLittleSolReceived"


def test_failure_info_guard_program_is_not_a_pump_failure():
    guard = "52Yyvsugd3B7waLEFB83hdTmcwtU66tBuEzTQ7kcfoAi"
    logs = [f"Program {guard} invoke [1]",
            "Program log: AnchorError thrown in programs/pump_buy_guard/src/lib.rs:62. Error Code: "
            "QuoteReserveThresholdReached. Error Number: 6017. Error Message: 6018.",
            f"Program {guard} failed: custom program error: 0x1781"]
    fi = failure_info(logs)
    assert fi["program"] == guard and fi["name"] == "QuoteReserveThresholdReached" and not fi["pump_reached"]
    assert pump_error_from_logs(logs) is None


def test_failure_info_nested_cpi_failure():
    router = "Router1111111111111111111111111111111111111"
    logs = [f"Program {router} invoke [1]", f"Program {PUMP} invoke [2]",
            "Program log: AnchorError occurred. Error Code: BuySlippageBelowMinTokensOut. Error Number: 6042.",
            f"Program {PUMP} failed: custom program error: 0x179a",
            f"Program {router} failed: custom program error: 0x179a"]
    assert failure_info(logs)["name"] == "BuySlippageBelowMinTokensOut"


def test_random_windows_are_disjoint_and_uncensored():
    now = 1_800_000_000
    ws = random_windows(days=7, n=30, minutes=10, seed=3, horizon_s=21600, now=now)
    assert len(ws) == 30
    for (a, b), (c, d) in zip(ws, ws[1:]):
        assert b <= c
    assert all(e <= now - 21600 for _, e in ws)
    assert ws == random_windows(days=7, n=30, minutes=10, seed=3, horizon_s=21600, now=now)


class FakeRpc:
    def __init__(self, pages):
        self.pages, self.calls, self.url = pages, [], "https://x/?api-key=SECRET"

    def call(self, method, params):
        self.calls.append((method, params))
        return self.pages[len(self.calls) - 1]


def _item(i, err=None):
    return {"slot": 100 + i, "transactionIndex": i % 3, "blockTime": 1000 + i,
            "transaction": {"signatures": [f"sig{i}"], "message": {}}, "meta": {"err": err}}


def test_helius_full_history_pagination_order_and_credits(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "SECRET")
    src = HeliusSource()
    assert "SECRET" not in repr(src)
    src.rpc = FakeRpc([{"data": [_item(i) for i in range(100)], "paginationToken": "199:2"},
                       {"data": [_item(i, err={"x": 1} if i == 150 else None) for i in range(100, 160)],
                        "paginationToken": None}])
    txs, truncated = src.full_history("curve", 0, 10**10, max_tx=1000)
    assert not truncated and len(txs) == 160
    assert [t.ref.order_key for t in txs] == list(range(160))
    assert txs[150].ref.failed and txs[5].ref.tx_index == 2
    assert src.usage.credits == 20  # 10 per 100 returned, rounded up, 10 minimum
    opts = src.rpc.calls[1][1][1]
    assert opts["sortOrder"] == "asc" and opts["paginationToken"] == "199:2"
    assert opts["filters"]["status"] == "any" and opts["maxSupportedTransactionVersion"] == 1


def test_helius_truncation(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "k")
    src = HeliusSource()
    src.rpc = FakeRpc([{"data": [_item(i) for i in range(100)], "paginationToken": "t"}])
    txs, truncated = src.full_history("curve", 0, 10**10, max_tx=50)
    assert truncated and len(txs) == 50


def _synthetic_launch(con, creator="CREATOR", mint="MINT"):
    """Creator buys 2 SOL at creation, an outside wallet buys 3 SOL, creator sells half."""
    s0 = CurveState.fresh(VERIFIED)
    db.insert_row(con, "launches", {
        "mint": mint, "create_signature": "c", "bonding_curve": "bc", "creator": creator, "create_user": creator,
        "create_slot": 10, "create_tx_index": 0, "create_time": 1000, "is_mayhem": 0, "is_sol_quote": 1,
        "is_holder_reward": 0, "init_virtual_token": s0.virtual_token_reserves,
        "init_virtual_quote": s0.virtual_quote_reserves, "init_real_token": s0.real_token_reserves,
        "token_total_supply": s0.token_total_supply, "horizon_s": 600, "status": "collected", "truncated": 0,
        "n_tx": 3, "n_failed_tx": 0, "n_trades": 3})
    steps = [("c", 10, CREATOR := creator, "buy", 2 * L), ("x", 11, "OTHER", "buy", 3 * L), ("y", 12, creator, "sell", None)]
    st, held, trades = s0, 0, []
    for sig, slot, user, side, amt in steps:
        if side == "buy":
            r = buy_exact_quote_in(st, amt, VERIFIED.fee_schedule)
            if user == CREATOR:
                held += r.tokens
        else:
            r = sell_exact_tokens_in(st, held // 2, VERIFIED.fee_schedule)
            held -= held // 2
        st = r.after
        db.insert_row(con, "trades", {
            "signature": sig, "seq": 0, "mint": mint, "slot": slot, "tx_index": 0, "order_key": slot,
            "block_time": 1000 + slot - 10, "user": user, "ix_name": "buy_exact_sol_in" if side == "buy" else "sell",
            "is_buy": int(side == "buy"), "quote_amount": r.quote_curve, "token_amount": r.tokens,
            "fee": r.protocol_fee, "creator_fee": r.creator_fee, "buyback_fee": r.protocol_fee // 2,
            "fee_bps": 95, "creator_fee_bps": 30, "v_quote": st.virtual_quote_reserves, "v_token": st.virtual_token_reserves,
            "r_quote": st.real_quote_reserves, "r_token": st.real_token_reserves, "mayhem": 0})
        trades.append(r)
    con.commit()
    return trades, st, held


def test_summary_creator_accounting(tmp_path):
    con = db.connect(str(tmp_path / "t.sqlite"))
    (b, x, s), st, held = _synthetic_launch(con)
    out = summarize_launch(con, "MINT")
    assert out["creator_quote_paid"] == -b.user_quote_delta
    assert out["creator_quote_received"] == s.user_quote_delta
    assert out["creator_curve_net_tokens"] == held
    # realised: proceeds minus average cost of the half sold
    basis_sold = (-b.user_quote_delta) * s.tokens // b.tokens
    assert out["creator_realised_pnl_avg_cost"] == s.user_quote_delta - basis_sold
    # unrealised is executable liquidation value of the remaining half, not tokens x price
    liq = sell_exact_tokens_in(st, held, VERIFIED.fee_schedule).user_quote_delta
    assert out["creator_remaining_mark_to_curve_value"] == liq
    assert out["creator_unrealised_pnl_mark_to_curve"] == liq - out["creator_remaining_cost_basis"]
    assert out["n_unique_noncreator_buyers"] == 1 and out["creator_first_sell_s"] == 2
    assert out["creator_initial_buy_paid"] == -b.user_quote_delta
    assert out["creator_fee_accrued"] == b.creator_fee + x.creator_fee + s.creator_fee


def test_u64_max_limit_is_stored_exactly(tmp_path):
    con = db.connect(str(tmp_path / "t.sqlite"))
    db.insert_row(con, "failed_attempts", {"signature": "s", "seq": 0, "mint": "m", "arg_limit": 2**64 - 1})
    v = con.execute("SELECT arg_limit FROM failed_attempts").fetchone()[0]
    assert int(v) == 2**64 - 1


def test_rpc_source_signature_cache_and_order():
    from pumpsim.pipeline.sources import RpcSource
    src = RpcSource("http://unused")
    # newest-first pages, as getSignaturesForAddress returns them; 1000-item page then a short page
    page1 = [{"signature": f"s{i}", "slot": 5000 - i, "blockTime": 5000 - i, "err": None, "transactionIndex": i % 7}
             for i in range(1000)]
    page2 = [{"signature": f"s{i}", "slot": 5000 - i, "blockTime": 5000 - i, "err": {"x": 1}} for i in range(1000, 1500)]
    pages = iter([page1, page2])
    calls = []
    src._sig_page = lambda address, before: calls.append(before) or next(pages)
    a = src.signatures("addr", 4500, 4600)
    assert [r.block_time for r in a] == list(range(4500, 4600))  # oldest first, half-open window
    assert a[0].tx_index == 500 % 7
    b = src.signatures("addr", 3600, 3700)  # needs the second page
    assert len(b) == 100 and all(r.failed for r in b)
    c = src.signatures("addr", 4500, 4600)  # served from cache
    assert [r.signature for r in c] == [r.signature for r in a]
    assert calls == [None, "s999"]


def test_windows_outside_source_history_are_skipped_not_zero(tmp_path):
    from pumpsim.pipeline.collect import enumerate_frame
    from pumpsim.pipeline.sources import HistorySource, SigRef

    class Src(HistorySource):
        name = "fake"

        def history_start(self):
            return 10_000

        def signatures(self, address, t_start, t_end):
            return [SigRef(f"s{t}", t, t, False, 0, i) for i, t in enumerate(range(t_start, t_end, 100))]

    con = db.connect(str(tmp_path / "t.sqlite"))
    src = Src()
    old = enumerate_frame(con, src, 1_000, 2_000)  # before the source's history
    new = enumerate_frame(con, src, 20_000, 21_000)
    rows = {r["window_id"]: r for r in con.execute("SELECT * FROM frame_windows")}
    assert rows[old]["complete"] == 0 and rows[old]["n_signatures"] is None  # unknown, not "0 launches"
    assert rows[new]["complete"] == 1 and rows[new]["n_success"] == 10
    assert con.execute("SELECT COUNT(*) FROM frame_creations WHERE window_id=?", (old,)).fetchone()[0] == 0


def test_summary_flags_tokens_transferred_off_creator_address(tmp_path):
    """Creator buys on the curve; another wallet sells tokens it never bought on the curve (they were
    transferred from the creator). The creator's curve-flow balance then exceeds everything outstanding,
    so the summary must flag the transfer and refuse to value a sale the curve cannot pay for."""
    con = db.connect(str(tmp_path / "t.sqlite"))
    (b, x, s), st, held = _synthetic_launch(con)
    # OTHER sells its own tokens plus 40 % of the creator's remaining balance (received by transfer)
    amt = x.tokens + int(held * 0.4)
    r = sell_exact_tokens_in(st, amt, VERIFIED.fee_schedule)
    st2 = r.after
    db.insert_row(con, "trades", {
        "signature": "z", "seq": 0, "mint": "MINT", "slot": 13, "tx_index": 0, "order_key": 13, "block_time": 1003,
        "user": "OTHER", "ix_name": "sell", "is_buy": 0, "quote_amount": r.quote_curve, "token_amount": amt,
        "fee": r.protocol_fee, "creator_fee": r.creator_fee, "buyback_fee": 0, "fee_bps": 95, "creator_fee_bps": 30,
        "v_quote": st2.virtual_quote_reserves, "v_token": st2.virtual_token_reserves,
        "r_quote": st2.real_quote_reserves, "r_token": st2.real_token_reserves, "mayhem": 0})
    con.commit()
    out = summarize_launch(con, "MINT")
    outstanding = 793_100_000_000_000 - st2.real_token_reserves
    assert out["creator_tokens_moved_off_min"] == held - outstanding > 0
    assert out["creator_remaining_mark_to_curve_value"] is None
    assert "transferred out" in out["creator_mark_to_curve_unavailable_reason"]
