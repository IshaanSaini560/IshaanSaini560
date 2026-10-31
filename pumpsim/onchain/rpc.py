"""Minimal read-only Solana JSON-RPC client (no signing, no sending)."""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(self, url: str = DEFAULT_RPC, min_interval: float = 0.25, retries: int = 8):
        self.url = url
        self.min_interval = min_interval
        self.retries = retries
        self._last = 0.0
        self._id = 0

    def call(self, method: str, params: list):
        for attempt in range(self.retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self._id += 1
            body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    out = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                # The public endpoint answers 429 (and sometimes 413) when a per-method quota is spent.
                if e.code in (413, 429) or e.code >= 500:
                    retry_after = e.headers.get("retry-after") if e.headers else None
                    time.sleep(float(retry_after) if retry_after else min(2 ** attempt, 30))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** attempt)
                continue
            if "error" in out:
                err = out["error"]
                if err.get("code") in (429, -32429) or "rate" in str(err).lower():
                    time.sleep(2 ** attempt)
                    continue
                raise RpcError(err)
            return out["result"]
        raise RpcError(f"{method} failed after {self.retries} attempts")

    def account_data(self, address: str) -> tuple[bytes, int]:
        """Return (raw account data, context slot)."""
        res = self.call("getAccountInfo", [address, {"encoding": "base64", "commitment": "finalized"}])
        if res["value"] is None:
            raise RpcError(f"account {address} not found")
        return base64.b64decode(res["value"]["data"][0]), res["context"]["slot"]

    def signatures(self, address: str, limit: int = 1000, before: str | None = None) -> list:
        cfg = {"limit": limit, "commitment": "finalized"}
        if before:
            cfg["before"] = before
        return self.call("getSignaturesForAddress", [address, cfg])

    def transaction(self, signature: str):
        return self.call("getTransaction", [signature, {"encoding": "json", "commitment": "finalized",
                                                        "maxSupportedTransactionVersion": 1}])
