"""Where transaction history comes from.

Two interchangeable sources yield the same records:

* HeliusSource - Helius `getTransactionsForAddress` (oldest-first, time-filtered, includes
  failed transactions, returns each transaction's index within its slot). Billing per Helius
  docs (checked 2026-10-31): "signatures" mode 10 credits per call; "full" mode 10 credits per
  100 returned transactions, rounded up, 10 minimum.
* RpcSource - any standard Solana RPC (`getSignaturesForAddress` + `getTransaction`). Works
  with the public endpoint for small trials, or with a Helius RPC URL if the
  getTransactionsForAddress method is not available on the plan. It has no transaction index;
  intra-slot order is taken from the order getSignaturesForAddress returns, which the
  Phase 1 replay showed reproduces the on-chain state sequence.

The API key is read from the HELIUS_API_KEY environment variable and is never logged.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Iterator

from ..onchain.rpc import DEFAULT_RPC, Rpc, RpcError

HELIUS_URL = "https://mainnet.helius-rpc.com/"


@dataclass
class SigRef:
    signature: str
    slot: int
    block_time: int | None
    failed: bool
    tx_index: int | None  # position inside the block, when the source knows it
    order_key: int  # monotone within the returned sequence (oldest first)


@dataclass
class FullTx:
    ref: SigRef
    tx: dict  # getTransaction-shaped dict: {"transaction", "meta", "slot", "blockTime", "version"}


@dataclass
class Usage:
    calls: int = 0
    credits: float = 0.0  # Helius credits (estimated from the published price list)
    by_method: dict = field(default_factory=dict)

    def add(self, method: str, credits: float) -> None:
        self.calls += 1
        self.credits += credits
        self.by_method[method] = self.by_method.get(method, 0) + 1


class HistorySource:
    name = "abstract"

    def __init__(self) -> None:
        self.usage = Usage()

    def signatures(self, address: str, t_start: int, t_end: int) -> list[SigRef]:
        raise NotImplementedError

    def history_start(self) -> int:
        """Earliest unix time this source has complete ledger data for (0 = archival)."""
        return 0

    def full_history(self, address: str, t_start: int, t_end: int, max_tx: int) -> tuple[list[FullTx], bool]:
        """All transactions touching `address` with t_start <= blockTime < t_end, oldest first.
        Returns (transactions, truncated) where truncated means max_tx was hit."""
        raise NotImplementedError

    def transactions(self, refs: list[SigRef]) -> list[FullTx]:
        raise NotImplementedError


class RpcSource(HistorySource):
    name = "rpc"

    def __init__(self, url: str = DEFAULT_RPC, min_interval: float = 0.35, credits_per_call: float = 1.0,
                 workers: int = 1):
        super().__init__()
        self.rpc = Rpc(url, min_interval=min_interval)
        self.credits_per_call = credits_per_call
        self.workers = workers
        self._sig_cache: dict[str, dict] = {}  # address -> {"items": newest-first, "before": cursor, "done": bool}

    def history_start(self, margin_s: int = 7200) -> int:
        """Conservative earliest unix time with complete ledger data on this endpoint.

        Non-archival nodes return nothing before their first block, which would otherwise look like
        "no launches". Load-balanced endpoints (PublicNode) mix backends with different retention
        (observed 2026-10-31: getFirstAvailableBlock from one backend was already pruned on the next),
        so we probe forward from the reported first block until a block time is served, then add a
        safety margin."""
        if getattr(self, "_history_start", None) is None:
            self.usage.add("getFirstAvailableBlock", self.credits_per_call)
            first = self.rpc.call("getFirstAvailableBlock", [])
            bt = None
            for off in range(0, 400_000, 2_500):  # up to ~2 days of slots
                try:
                    self.usage.add("getBlockTime", self.credits_per_call)
                    bt = self.rpc.call("getBlockTime", [first + off])
                except RpcError:
                    continue
                if bt:
                    break
            self._history_start = (bt + margin_s) if bt else 0
        return self._history_start

    def _sig_page(self, address: str, before: str | None) -> list[dict]:
        self.usage.add("getSignaturesForAddress", self.credits_per_call)
        return self.rpc.signatures(address, limit=1000, before=before)

    def signatures(self, address: str, t_start: int, t_end: int) -> list[SigRef]:
        """Signatures with t_start <= blockTime < t_end, oldest first. Pages are cached per address,
        so enumerating several windows on the same address walks its history only once."""
        c = self._sig_cache.setdefault(address, {"items": [], "before": None, "done": False})
        while not c["done"] and (not c["items"] or (c["items"][-1].get("blockTime") or 0) >= t_start):
            page = self._sig_page(address, c["before"])
            c["items"] += page
            if len(page) < 1000:
                c["done"] = True
            else:
                c["before"] = page[-1]["signature"]
        sel = [s for s in c["items"] if s.get("blockTime") is not None and t_start <= s["blockTime"] < t_end]
        out = []
        for i, s in enumerate(reversed(sel)):
            # Some RPCs (e.g. PublicNode) also return the in-block transactionIndex: exact ordering.
            out.append(SigRef(s["signature"], s["slot"], s.get("blockTime"), s.get("err") is not None,
                              s.get("transactionIndex"), i))
        return out

    def transactions(self, refs: list[SigRef]) -> list[FullTx]:
        """Fetch full transactions, preserving input order; `workers` > 1 fetches in parallel
        (still paced by the shared min_interval)."""
        def one(r: SigRef):
            self.usage.add("getTransaction", self.credits_per_call)
            tx = self.rpc.transaction(r.signature)
            if tx is None:
                raise RpcError(f"transaction {r.signature} not available from this RPC (pruned history?)")
            if r.tx_index is None and tx.get("transactionIndex") is not None:
                r.tx_index = tx["transactionIndex"]
            return FullTx(r, tx)

        if self.workers <= 1:
            return [one(r) for r in refs]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(self.workers) as ex:
            return list(ex.map(one, refs))

    def full_history(self, address: str, t_start: int, t_end: int, max_tx: int) -> tuple[list[FullTx], bool]:
        refs = self.signatures(address, t_start, t_end)
        truncated = len(refs) > max_tx
        return self.transactions(refs[:max_tx]), truncated


class HeliusSource(HistorySource):
    """Helius getTransactionsForAddress. Requires HELIUS_API_KEY."""

    name = "helius"

    def __init__(self, api_key: str | None = None, min_interval: float = 0.1, full_page: int = 100):
        super().__init__()
        key = api_key or os.environ.get("HELIUS_API_KEY")
        if not key:
            raise RpcError("HELIUS_API_KEY is not set")
        self.rpc = Rpc(f"{HELIUS_URL}?api-key={key}", min_interval=min_interval)
        self.full_page = full_page

    def __repr__(self) -> str:  # never expose the key
        return "HeliusSource(url=https://mainnet.helius-rpc.com/?api-key=***)"

    def _gtfa(self, address: str, opts: dict) -> dict:
        try:
            return self.rpc.call("getTransactionsForAddress", [address, opts])
        except RpcError as e:  # scrub anything that could echo the URL
            raise RpcError(str(e).replace(self.rpc.url, HELIUS_URL)) from None

    def signatures(self, address: str, t_start: int, t_end: int) -> list[SigRef]:
        out: list[SigRef] = []
        token = None
        while True:
            opts = {"transactionDetails": "signatures", "sortOrder": "asc", "limit": 1000,
                    "filters": {"blockTime": {"gte": t_start, "lt": t_end}, "status": "any"}}
            if token:
                opts["paginationToken"] = token
            res = self._gtfa(address, opts)
            self.usage.add("getTransactionsForAddress:signatures", 10)
            for s in res.get("data", []):
                out.append(SigRef(s["signature"], s["slot"], s.get("blockTime"), s.get("err") is not None,
                                  s.get("transactionIndex"), len(out)))
            token = res.get("paginationToken")
            if not token or not res.get("data"):
                return out

    def full_history(self, address: str, t_start: int, t_end: int, max_tx: int) -> tuple[list[FullTx], bool]:
        out: list[FullTx] = []
        token = None
        while True:
            opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": self.full_page,
                    "encoding": "json", "maxSupportedTransactionVersion": 1,
                    "filters": {"blockTime": {"gte": t_start, "lt": t_end}, "status": "any"}}
            if token:
                opts["paginationToken"] = token
            res = self._gtfa(address, opts)
            data = res.get("data", [])
            self.usage.add("getTransactionsForAddress:full", max(10, 10 * math.ceil(len(data) / 100)))
            for item in data:
                tx = {"transaction": item["transaction"], "meta": item["meta"], "slot": item["slot"],
                      "blockTime": item.get("blockTime"), "version": item.get("version")}
                ref = SigRef(item["transaction"]["signatures"][0], item["slot"], item.get("blockTime"),
                             (item["meta"] or {}).get("err") is not None, item.get("transactionIndex"), len(out))
                out.append(FullTx(ref, tx))
                if len(out) >= max_tx:
                    return out, True
            token = res.get("paginationToken")
            if not token or not data:
                return out, False

    def transactions(self, refs: list[SigRef]) -> list[FullTx]:
        # Single lookups go through standard getTransaction on the same Helius endpoint (1 credit each).
        out = []
        for r in refs:
            self.usage.add("getTransaction", 1)
            tx = self.rpc.transaction(r.signature)
            if tx is not None:
                out.append(FullTx(r, tx))
        return out


def make_source(kind: str) -> HistorySource:
    """kind: "helius" (getTransactionsForAddress), "helius-rpc" (standard methods on the Helius URL),
    "publicnode" (free PublicNode endpoint, ~3 days of history), or "public" (SOLANA_RPC_URL or the
    public mainnet endpoint, ~1 request/s)."""
    if kind == "helius":
        return HeliusSource()
    if kind == "helius-rpc":
        key = os.environ.get("HELIUS_API_KEY")
        if not key:
            raise RpcError("HELIUS_API_KEY is not set")
        src = RpcSource(f"{HELIUS_URL}?api-key={key}", min_interval=0.1)
        src.name = "helius-rpc"
        return src
    if kind == "publicnode":
        # Free, keyless (Allnodes PublicNode). Measured 2026-10-31: ~40 req/s burst without errors,
        # but only ~3.2 days of ledger history. Paced politely at <= 10 req/s with 4 workers.
        src = RpcSource("https://solana-rpc.publicnode.com", min_interval=0.1, credits_per_call=0, workers=4)
        src.name = "publicnode"
        return src
    if kind == "public":
        # Public mainnet RPC: ~10 calls per method per window (x-ratelimit-method-limit, 2026-10-31).
        return RpcSource(DEFAULT_RPC, min_interval=1.05)
    raise ValueError(f"unknown source {kind}")
