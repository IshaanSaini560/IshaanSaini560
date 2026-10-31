"""Tiny Borsh reader driven by field specs copied from the official Anchor IDL.

Layouts that were appended by later program upgrades may be missing from older
accounts; ``decode_struct(..., allow_truncated=True)`` returns ``None`` for any
trailing field that does not fit, mirroring the guidance in
pump-public-docs/docs/PUMP_PROGRAM_README.md ("read the missing trailing fields
as 0 / false / Pubkey::default()").
"""
from __future__ import annotations

import struct
from typing import Any

from .base58 import b58encode


class Reader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.pos = offset

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise EOFError(f"need {n} bytes at {self.pos}, have {self.remaining()}")
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def read(self, ty: Any, types: dict) -> Any:
        if isinstance(ty, str):
            if ty == "bool":
                return self.take(1)[0] != 0
            if ty == "u8":
                return self.take(1)[0]
            if ty == "u16":
                return struct.unpack("<H", self.take(2))[0]
            if ty == "u32":
                return struct.unpack("<I", self.take(4))[0]
            if ty == "u64":
                return struct.unpack("<Q", self.take(8))[0]
            if ty == "i64":
                return struct.unpack("<q", self.take(8))[0]
            if ty == "u128":
                return int.from_bytes(self.take(16), "little", signed=False)
            if ty == "i128":
                return int.from_bytes(self.take(16), "little", signed=True)
            if ty == "pubkey":
                return b58encode(self.take(32))
            if ty == "string":
                n = struct.unpack("<I", self.take(4))[0]
                return self.take(n).decode("utf-8", "replace")
            if ty in types:
                return decode_fields(self, types[ty], types)
            raise ValueError(f"unknown type {ty}")
        if "array" in ty:
            inner, n = ty["array"]
            return [self.read(inner, types) for _ in range(n)]
        if "vec" in ty:
            n = struct.unpack("<I", self.take(4))[0]
            return [self.read(ty["vec"], types) for _ in range(n)]
        if "option" in ty:
            tag = self.take(1)[0]
            return self.read(ty["option"], types) if tag else None
        if "defined" in ty:
            return self.read(ty["defined"], types)
        raise ValueError(f"unknown type {ty}")


def decode_fields(r: Reader, fields: list, types: dict, allow_truncated: bool = False) -> dict:
    out: dict = {}
    for name, ty in fields:
        start = r.pos
        try:
            out[name] = r.read(ty, types)
        except EOFError:
            if not allow_truncated:
                raise
            r.pos = start
            out[name] = None
    return out


def decode_struct(data: bytes, fields: list, types: dict, discriminator: bytes | None = None,
                  allow_truncated: bool = True) -> dict:
    offset = 0
    if discriminator is not None:
        if data[:8] != discriminator:
            raise ValueError("discriminator mismatch")
        offset = 8
    r = Reader(data, offset)
    return decode_fields(r, fields, types, allow_truncated=allow_truncated)
