"""SQLite storage for the launch dataset.

Every column filled from chain data is VERIFIED (decoded from mainnet transactions).
Derived columns live in launch_summary and say so in their names
(e.g. *_mark_to_curve_* = simulator liquidation value, not a displayed market cap).
"""
from __future__ import annotations

import json
import sqlite3
import zlib

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Sampling frame: every creation transaction seen in each enumerated time window.
CREATE TABLE IF NOT EXISTS frame_windows (
    window_id INTEGER PRIMARY KEY,
    t_start INTEGER NOT NULL, t_end INTEGER NOT NULL,      -- unix seconds, [t_start, t_end)
    source TEXT, n_signatures INTEGER, n_failed INTEGER, n_success INTEGER, enumerated_at TEXT,
    complete INTEGER DEFAULT 1,  -- 0 = window not fully covered by the source's ledger history; never sample it
    UNIQUE (t_start, t_end)
);
CREATE TABLE IF NOT EXISTS frame_creations (
    signature TEXT PRIMARY KEY, window_id INTEGER NOT NULL, slot INTEGER, tx_index INTEGER,
    block_time INTEGER, failed INTEGER, selected INTEGER DEFAULT 0, sample_seed INTEGER,
    not_creation INTEGER DEFAULT 0  -- 1 = selected, but the tx created no coin (e.g. PumpSwap CreatePool referencing the PDA)
);

-- One row per sampled launch.
CREATE TABLE IF NOT EXISTS launches (
    mint TEXT PRIMARY KEY, create_signature TEXT UNIQUE, window_id INTEGER,
    bonding_curve TEXT, creator TEXT, create_user TEXT,
    create_slot INTEGER, create_tx_index INTEGER, create_time INTEGER,
    name TEXT, symbol TEXT, uri TEXT, token_program TEXT,
    is_mayhem INTEGER, quote_mint TEXT, is_sol_quote INTEGER, is_holder_reward INTEGER,
    is_cashback INTEGER, creator_fee_bps INTEGER, depth INTEGER,
    init_virtual_token INTEGER, init_virtual_quote INTEGER, init_real_token INTEGER, token_total_supply INTEGER,
    horizon_s INTEGER, status TEXT,                         -- pending | collected | error
    source TEXT, observed_until INTEGER, truncated INTEGER,
    n_tx INTEGER, n_failed_tx INTEGER, n_trades INTEGER,
    sim_checked INTEGER, sim_matched INTEGER,
    complete_time INTEGER, migrate_time INTEGER, pool TEXT,
    error TEXT, collected_at TEXT
);

-- Every transaction touching the bonding curve inside the observation horizon.
CREATE TABLE IF NOT EXISTS transactions (
    signature TEXT PRIMARY KEY, mint TEXT NOT NULL, slot INTEGER, tx_index INTEGER, order_key INTEGER,
    block_time INTEGER, failed INTEGER, err TEXT,
    fail_program TEXT, fail_name TEXT, pump_reached INTEGER,
    fee INTEGER, n_signatures INTEGER, cu_consumed INTEGER, fee_payer TEXT, signers TEXT,
    jito_tip INTEGER, payer_sol_delta INTEGER, version TEXT, pump_ix TEXT
);
CREATE INDEX IF NOT EXISTS tx_mint ON transactions (mint, slot, order_key);

-- Every successful curve trade (from TradeEvent; amounts are on-chain values).
CREATE TABLE IF NOT EXISTS trades (
    signature TEXT, seq INTEGER, mint TEXT, slot INTEGER, tx_index INTEGER, order_key INTEGER, block_time INTEGER,
    user TEXT, fee_payer TEXT, ix_name TEXT, event_ix_name TEXT, is_buy INTEGER,
    quote_amount INTEGER, token_amount INTEGER, fee INTEGER, creator_fee INTEGER, buyback_fee INTEGER,
    fee_bps INTEGER, creator_fee_bps INTEGER,
    v_quote INTEGER, v_token INTEGER, r_quote INTEGER, r_token INTEGER,
    arg_amount, arg_limit, mayhem INTEGER, sim_match INTEGER,   -- untyped: u64 args may exceed i64
    PRIMARY KEY (signature, seq)
);
CREATE INDEX IF NOT EXISTS trades_mint ON trades (mint, slot, order_key, seq);
CREATE INDEX IF NOT EXISTS trades_user ON trades (user);

-- Pump trade instructions inside FAILED transactions (what was attempted, and why it failed).
CREATE TABLE IF NOT EXISTS failed_attempts (
    signature TEXT, seq INTEGER, mint TEXT, slot INTEGER, block_time INTEGER, fee_payer TEXT,
    ix_name TEXT, arg_amount, arg_limit, fail_program TEXT, fail_name TEXT, fee_paid INTEGER,  -- args untyped
    PRIMARY KEY (signature, seq)
);

-- Raw transactions (zlib JSON) so analyses can be re-run without paying for the data again.
CREATE TABLE IF NOT EXISTS raw_tx (signature TEXT PRIMARY KEY, z BLOB);

-- Derived per-launch metrics (rebuilt by `summarize`).
CREATE TABLE IF NOT EXISTS launch_summary (mint TEXT PRIMARY KEY, data TEXT);

CREATE TABLE IF NOT EXISTS usage_log (
    at TEXT, command TEXT, source TEXT, calls INTEGER, credits REAL, detail TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    if "not_creation" not in {r[1] for r in con.execute("PRAGMA table_info(frame_creations)")}:
        con.execute("ALTER TABLE frame_creations ADD COLUMN not_creation INTEGER DEFAULT 0")
    if "complete" not in {r[1] for r in con.execute("PRAGMA table_info(frame_windows)")}:
        con.execute("ALTER TABLE frame_windows ADD COLUMN complete INTEGER DEFAULT 1")
    cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
    if "event_ix_name" not in cols:  # databases created before this column existed
        con.execute("ALTER TABLE trades ADD COLUMN event_ix_name TEXT")
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con


def put_raw(con: sqlite3.Connection, signature: str, tx: dict) -> None:
    con.execute("INSERT OR REPLACE INTO raw_tx VALUES (?, ?)",
                (signature, zlib.compress(json.dumps(tx, separators=(",", ":")).encode(), 6)))


def get_raw(con: sqlite3.Connection, signature: str) -> dict | None:
    row = con.execute("SELECT z FROM raw_tx WHERE signature = ?", (signature,)).fetchone()
    return json.loads(zlib.decompress(row["z"])) if row else None


I64_MAX = 2**63 - 1


def _sqlite_value(v):
    """SQLite integers are signed 64-bit. On-chain u64 values above that (e.g. a slippage limit of
    u64::MAX = 18446744073709551615, which bots use for "no limit") are stored exactly as decimal
    TEXT instead of being altered. Read them back with int()."""
    if isinstance(v, int) and not isinstance(v, bool) and (v > I64_MAX or v < -I64_MAX - 1):
        return str(v)
    return v


def insert_row(con: sqlite3.Connection, table: str, row: dict, replace: bool = True) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    con.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", tuple(_sqlite_value(v) for v in row.values()))
