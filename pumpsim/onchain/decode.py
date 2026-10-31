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
    for name in ("TradeEvent", "CreateEvent", "CompleteEvent", "PostCompleteBuyEvent", "CompletePumpAmmMigrationEvent"):
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
    """Decode a Pump instruction. Trade/create instructions get their arguments decoded
    (trailing optional args are EOF-tolerant); any other Pump instruction is returned by name
    with empty args."""
    from .layouts import INSTRUCTION_NAMES, INSTRUCTIONS

    for name, (disc, args) in INSTRUCTIONS.items():
        if data[:8] == disc:
            try:
                return name, decode_struct(data[8:], args, TYPES, allow_truncated=True)
            except Exception:
                return name, {}
    name = INSTRUCTION_NAMES.get(bytes(data[:8]))
    return (name, {}) if name else None


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


def failure_info(logs: list[str] | None) -> dict:
    """Which program failed a transaction and why, from the log invocation stack.

    Returns {"program": id or None, "code": int or None, "name": str or None,
    "pump_reached": bool}. "name" uses the Pump IDL for Pump errors and the AnchorError
    "Error Code" for other Anchor programs. A failure in another program (bot guard,
    router, aggregator) is common and is NOT a Pump slippage failure.
    """
    import re

    from .layouts import ERRORS

    stack: list[str] = []
    last_anchor: dict[str, tuple[str, int]] = {}
    out = {"program": None, "code": None, "name": None, "pump_reached": False}
    for line in logs or []:
        m = re.match(r"Program (\w+) invoke \[\d+\]", line)
        if m:
            stack.append(m.group(1))
            if m.group(1) == PUMP_PROGRAM_ID:
                out["pump_reached"] = True
            continue
        m = re.match(r"Program (\w+) success", line)
        if m:
            if stack:
                stack.pop()
            continue
        m = re.search(r"AnchorError.*Error Code: (\w+)\. Error Number: (\d+)", line)
        if m and stack:
            last_anchor[stack[-1]] = (m.group(1), int(m.group(2)))
            continue
        m = re.match(r"Program (\w+) failed: (.*)", line)
        if m and out["program"] is None:
            prog, msg = m.group(1), m.group(2)
            out["program"] = prog
            c = re.search(r"custom program error: 0x([0-9a-fA-F]+)", msg)
            if c:
                out["code"] = int(c.group(1), 16)
            if prog == PUMP_PROGRAM_ID and out["code"] is not None:
                out["name"] = ERRORS.get(out["code"], f"custom:{out['code']}")
            elif prog in last_anchor:
                out["name"] = last_anchor[prog][0]
            else:
                out["name"] = msg[:80]
    return out


def pump_error_from_logs(logs: list[str] | None) -> str | None:
    """Name of the Pump program error that failed a transaction, or None if Pump did not fail."""
    info = failure_info(logs)
    return info["name"] if info["program"] == PUMP_PROGRAM_ID else None
