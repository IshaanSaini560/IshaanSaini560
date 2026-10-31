"""Decode Pump program accounts and events using the IDL-derived layouts."""
from __future__ import annotations

import base64

from .base58 import b58decode
from .borsh import decode_struct
from .layouts import DISCRIMINATORS, EVENT_IX_TAG, PUMP_PROGRAM_ID, TYPES


def decode_account(kind: str, data: bytes) -> dict:
    return decode_struct(data, TYPES[kind], TYPES, discriminator=DISCRIMINATORS[kind])


def decode_event_payload(payload: bytes) -> tuple[str, dict] | None:
    """payload = 8-byte event discriminator + borsh body."""
    for name in ("TradeEvent", "CreateEvent", "CompleteEvent", "PostCompleteBuyEvent"):
        if payload[:8] == DISCRIMINATORS[name]:
            return name, decode_struct(payload, TYPES[name], TYPES, discriminator=DISCRIMINATORS[name])
    return None


def events_from_transaction(tx: dict) -> list[tuple[str, dict]]:
    """Extract Pump events from a getTransaction(json) result, in execution order.

    Pump emits events through Anchor's emit_cpi! (a self-invoke whose data starts
    with EVENT_IX_TAG). Inner instructions are walked in order; "Program data:" log
    lines are used as a fallback if no CPI events were found.
    """
    msg = tx["transaction"]["message"]
    keys = list(msg["accountKeys"])
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += loaded.get("writable", []) + loaded.get("readonly", [])
    out: list[tuple[str, dict]] = []
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group["instructions"]:
            if keys[ix["programIdIndex"]] != PUMP_PROGRAM_ID:
                continue
            data = b58decode(ix["data"])
            if data[:8] != EVENT_IX_TAG:
                continue
            ev = decode_event_payload(data[8:])
            if ev:
                out.append(ev)
    if not out:
        for line in (tx.get("meta") or {}).get("logMessages") or []:
            if line.startswith("Program data: "):
                try:
                    ev = decode_event_payload(base64.b64decode(line[len("Program data: "):]))
                except Exception:
                    ev = None
                if ev:
                    out.append(ev)
    return out


def decode_instruction(data: bytes) -> tuple[str, dict] | None:
    """Decode a Pump trade/create instruction (trailing optional args are EOF-tolerant)."""
    from .layouts import INSTRUCTIONS

    for name, (disc, args) in INSTRUCTIONS.items():
        if data[:8] == disc:
            return name, decode_struct(data[8:], args, TYPES, allow_truncated=True)
    return None


def pump_instructions(tx: dict) -> list[tuple[str, dict, list[str]]]:
    """All Pump trade/create instructions in a transaction (outer and CPI), in execution order.

    Returns (name, args, account_keys) triples. Event self-CPIs are skipped.
    """
    msg = tx["transaction"]["message"]
    keys = list(msg["accountKeys"])
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += loaded.get("writable", []) + loaded.get("readonly", [])
    inner = {g["index"]: g["instructions"] for g in (tx.get("meta") or {}).get("innerInstructions") or []}
    out = []

    def visit(ix):
        if keys[ix["programIdIndex"]] != PUMP_PROGRAM_ID:
            return
        data = b58decode(ix["data"])
        if data[:8] == EVENT_IX_TAG:
            return
        dec = decode_instruction(data)
        if dec:
            out.append((dec[0], dec[1], [keys[i] for i in ix["accounts"]]))

    for idx, ix in enumerate(msg["instructions"]):
        visit(ix)
        for sub in inner.get(idx, []):
            visit(sub)
    return out
