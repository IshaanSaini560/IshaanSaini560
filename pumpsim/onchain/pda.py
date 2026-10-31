"""Program-derived address derivation (Solana find_program_address), pure Python."""
from __future__ import annotations

import hashlib

from .base58 import b58decode, b58encode

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def is_on_curve(b: bytes) -> bool:
    """True if 32 bytes decompress to an Ed25519 point (same test as curve25519-dalek decompress)."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    y %= _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x = (u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P)) % _P
    vx2 = (v * x * x) % _P
    if vx2 == u % _P:
        return True
    if vx2 == (-u) % _P:
        return True  # x * sqrt(-1) is a root
    return False


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    pid = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not is_on_curve(h):
            return b58encode(h), bump
    raise ValueError("no viable bump")


def bonding_curve_pda(mint: str, program_id: str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P") -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], program_id)[0]
