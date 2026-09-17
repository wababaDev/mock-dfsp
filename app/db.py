"""
db.py — SQLite setup for the Blue Bank mock.

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

DB_PATH = Path(__file__).parent.parent / "bluebank.db"


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
    """Insert test accounts, only if the table is empty."""
    conn = get_connection()
    existing = conn.execute("SELECT COUNT(*) as c FROM accounts").fetchone()["c"]
    if existing == 0:
        conn.executemany(
            "INSERT INTO accounts (account_id, name, currency, balance, status) VALUES (?, ?, ?, ?, ?)",
            [
                ("260970000000", "Mercy Uzumaki", "XTS", 10000.0, "active"),
                ("260970000001", "Faith Nara", "XTS", 1200.0, "active"),
                ("260970000002", "Selina Uchiha", "XTS", 0.0, "active"),
                ("260970000003", "Peace Yagami", "XTS", 10000.0, "active"),
                ("260970000004", "John Aizen", "XTS", 0.0, "blocked"),
            ],
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