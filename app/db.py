"""
db.py — SQLite setup for the mock DFSP.

Tables:
  accounts       — the "real" bank accounts. Has a balance that actually changes.
  reservations   — payee-side holds: reserve -> commit OR unreserve.
  debits         — payer-side immediate debits: debit -> (optionally) refund.
  gl_entries     — every single balance change, on every account, ever. The
                   general ledger. This is what lets you actually SEE what
                   happened, in order, rather than just trusting the final
                   balance number.

Kept as plain sqlite3 (no ORM) on purpose — the whole point of this mock is to
be simple enough to read start to finish in one sitting.
"""
import sqlite3
from pathlib import Path
import os
import json

DB_PATH = Path(__file__).parent.parent / "mockbank.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us access columns by name, e.g. row["balance"]
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            currency TEXT NOT NULL,
            balance REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );

        -- Payee side: reserve (hold) -> commit (finalize) OR unreserve (cancel the hold)
        CREATE TABLE IF NOT EXISTS reservations (
            reserve_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL,  -- RESERVED, COMMITTED, RELEASED
            FOREIGN KEY (account_id) REFERENCES accounts (account_id)
        );

        -- Payer side: debit happens immediately (real money leaves), can only
        -- be reversed afterward via refund - there is no "hold" step here.
        -- home_transaction_id is the app's own reference, generated before the
        -- connector is ever called - it's the ONLY identifier the connector
        -- will have later if it needs to trigger a refund, since debit_id
        -- itself never leaves the CBS/app boundary.
        CREATE TABLE IF NOT EXISTS debits (
            debit_id TEXT PRIMARY KEY,
            home_transaction_id TEXT UNIQUE NOT NULL,
            account_id TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL,  -- COMPLETED, REFUNDED
            FOREIGN KEY (account_id) REFERENCES accounts (account_id)
        );

        -- The general ledger: one row per balance change, on any account, for
        -- any reason. Nothing here can be edited or deleted after the fact -
        -- that's the whole point of a ledger.
        CREATE TABLE IF NOT EXISTS gl_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,       -- DEBIT or CREDIT
            amount REAL NOT NULL,
            balance_after REAL NOT NULL,
            reference_type TEXT NOT NULL,   -- RESERVE, COMMIT, UNRESERVE, DEBIT, REFUND
            reference_id TEXT NOT NULL,     -- the reserve_id or debit_id this entry belongs to
            description TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts (account_id)
        );
    """)
    conn.commit()
    conn.close()


def seed_accounts():
    conn = get_connection()
    existing = conn.execute("SELECT COUNT(*) as c FROM accounts").fetchone()["c"]
    if existing == 0:
        seed_file = os.environ.get("SEED_FILE", "seeds/bluebank.json")
        with open(seed_file) as f:
            seed_data = json.load(f)
        conn.executemany(
            "INSERT INTO accounts (account_id, name, currency, balance, status) VALUES (?, ?, ?, ?, ?)",
            [(a["account_id"], a["name"], a["currency"], a["balance"], a["status"]) for a in seed_data["accounts"]],
        )
        conn.commit()
    conn.close()


def record_gl_entry(conn, account_id, entry_type, amount, balance_after, reference_type, reference_id, description):
    """Every balance change goes through here — one place, so nothing gets
    logged inconsistently. Does NOT commit — caller controls the transaction."""
    conn.execute(
        """INSERT INTO gl_entries
           (account_id, entry_type, amount, balance_after, reference_type, reference_id, description)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (account_id, entry_type, amount, balance_after, reference_type, reference_id, description),
    )

def load_bank_metadata():
    """Reads bank_name/fsp_id from the same seed file used by seed_accounts,
    so identity and seed data can never drift apart from each other."""
    seed_file = os.environ.get("SEED_FILE", "seeds/bluebank.json")
    with open(seed_file) as f:
        seed_data = json.load(f)
    return {"bank_name": seed_data["bank_name"], "fsp_id": seed_data["fsp_id"]}