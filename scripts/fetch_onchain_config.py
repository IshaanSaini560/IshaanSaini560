"""Fetch and decode the live Pump Global and FeeConfig accounts from mainnet.

Writes data/onchain_config_snapshot.json. Read-only: no keys, no transactions.
Usage: python -m scripts.fetch_onchain_config [--rpc URL]
"""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone

from pumpsim.onchain.decode import decode_account
from pumpsim.onchain.layouts import FEE_CONFIG_ACCOUNT, GLOBAL_ACCOUNT
from pumpsim.onchain.rpc import DEFAULT_RPC, Rpc

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "onchain_config_snapshot.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    args = ap.parse_args()
    rpc = Rpc(args.rpc)
    g_raw, g_slot = rpc.account_data(GLOBAL_ACCOUNT)
    f_raw, f_slot = rpc.account_data(FEE_CONFIG_ACCOUNT)
    snap = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rpc": args.rpc,
        "global": {"address": GLOBAL_ACCOUNT, "slot": g_slot, "data_len": len(g_raw),
                   "decoded": decode_account("Global", g_raw)},
        "fee_config": {"address": FEE_CONFIG_ACCOUNT, "slot": f_slot, "data_len": len(f_raw),
                       "decoded": decode_account("FeeConfig", f_raw)},
    }
    OUT.write_text(json.dumps(snap, indent=2, default=str) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
