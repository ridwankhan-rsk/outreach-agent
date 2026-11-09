"""
db.py — the SQLite database that remembers everything.

Why a database? The agent must NEVER forget who it has emailed, who asked to be
left alone, or how many emails went out today. Everything is stored here, so the
agent behaves the same after a reboot.

All timestamps are stored as integer "epoch seconds" (UTC). We convert to local
time only when we need "today" or "this week".
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

import config

SCHEMA = """
-- One row per person we plan to contact (or have contacted).
CREATE TABLE IF NOT EXISTS leads (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign      TEXT NOT NULL,               -- e.g. "professors"
  email         TEXT NOT NULL,               -- always stored lowercase
  name          TEXT,
  org           TEXT,                        -- company / lab / university
  org_key       TEXT,                        -- normalised org, used for the cooldown
  data_json     TEXT NOT NULL,               -- every CSV column (facts the agent may use)
  status        TEXT NOT NULL DEFAULT 'new', -- new|sent|replied|declined|suppressed|blocked|bounced
  status_reason TEXT,
  added_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_org   ON leads(org_key);

-- One row per email we actually sent (initial email or follow-up).
CREATE TABLE IF NOT EXISTS sends (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id     INTEGER NOT NULL REFERENCES leads(id),
  kind        TEXT NOT NULL,                 -- 'initial' | 'followup'
  thread_id   TEXT,                          -- Gmail thread id (used to match replies)
  message_id  TEXT,
  subject     TEXT,
  body        TEXT,
  sent_at     INTEGER NOT NULL
);

-- One row per reply (or bounce) we received.
CREATE TABLE IF NOT EXISTS replies (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id          INTEGER REFERENCES leads(id),
  gmail_message_id TEXT NOT NULL UNIQUE,     -- UNIQUE => we never process a reply twice
  thread_id        TEXT,
  sender           TEXT,
  snippet          TEXT,
  body             TEXT,
  classification   TEXT,                     -- needs_response|decline|stop_request|auto_reply|bounce
  suggested_reply  TEXT,
  received_at      INTEGER NOT NULL
);

-- "Never email these". An entry is an address, or '@domain.com' for a whole domain.
CREATE TABLE IF NOT EXISTS suppression (
  email    TEXT PRIMARY KEY,
  reason   TEXT,
  added_at INTEGER NOT NULL
);

-- Small key/value store: next allowed send time, first send time, etc.
CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Posts the automatic lead intake has already looked at (so each is judged only once).
CREATE TABLE IF NOT EXISTS intake_seen (
  source   TEXT NOT NULL,                    -- e.g. 'hn-hiring'
  post_id  TEXT NOT NULL,
  outcome  TEXT,                             -- 'queued' or the reason it was skipped
  seen_at  INTEGER NOT NULL,
  PRIMARY KEY (source, post_id)
);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add them to an
# existing database, so connect() adds any that are missing.
_NEW_LEAD_COLUMNS = {
    "source":      "TEXT NOT NULL DEFAULT 'manual'",   # 'manual' (CSV) or e.g. 'hn-hiring'
    "eligible_at": "INTEGER NOT NULL DEFAULT 0",       # earliest time we may send (grace window)
}


def _migrate(conn) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
    for col, decl in _NEW_LEAD_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open the database (creating it and its folder on first use)."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row          # rows behave like dicts
    conn.executescript(SCHEMA)              # safe to run every time (IF NOT EXISTS)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------- #
# Small key/value state
# --------------------------------------------------------------------------- #
def get_state(conn, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# --------------------------------------------------------------------------- #
# Suppression list
# --------------------------------------------------------------------------- #
def suppress(conn, email: str, reason: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO suppression(email,reason,added_at) VALUES(?,?,?)",
        (email.strip().lower(), reason, now()),
    )


def is_suppressed(conn, email: str) -> bool:
    """True if the address, or its whole domain ('@domain'), is suppressed."""
    email = email.strip().lower()
    domain = "@" + email.split("@")[-1]
    row = conn.execute(
        "SELECT 1 FROM suppression WHERE email IN (?, ?)", (email, domain)
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #
def normalise_org(org: str) -> str:
    """'  Acme, Inc. ' -> 'acme inc'  so 'Acme Inc' and 'ACME inc.' match."""
    keep = "".join(c.lower() if c.isalnum() else " " for c in (org or ""))
    return " ".join(keep.split())


def can_add_lead(conn, email: str) -> tuple[bool, str]:
    """
    Dedupe rules for importing a lead. Returns (allowed, reason_if_not).

    We refuse if the address is suppressed, is already waiting to be emailed,
    has replied/bounced, or was contacted within RECONTACT_COOLDOWN_DAYS.
    """
    email = email.strip().lower()
    if is_suppressed(conn, email):
        return False, "on suppression list"
    cutoff = now() - config.RECONTACT_COOLDOWN_DAYS * 86400
    for row in conn.execute("SELECT id,status,added_at FROM leads WHERE email=?", (email,)):
        if row["status"] in ("new", "blocked"):
            return False, "already queued"
        if row["status"] in ("replied", "bounced", "suppressed", "removed"):
            return False, f"already {row['status']}"
        last = conn.execute(
            "SELECT MAX(sent_at) AS t FROM sends WHERE lead_id=?", (row["id"],)
        ).fetchone()["t"] or row["added_at"]
        if last > cutoff:
            return False, f"contacted within {config.RECONTACT_COOLDOWN_DAYS} days"
    return True, ""


def add_lead(conn, campaign: str, row: dict, source: str = "manual",
             delay_seconds: int = 0) -> tuple[bool, str]:
    """
    Insert one lead from a row (a dict). Returns (inserted, reason).
    `delay_seconds` is a grace window: the lead will not be emailed before then.
    """
    email = (row.get("email") or "").strip().lower()
    if "@" not in email:
        return False, "missing/invalid email"
    ok, why = can_add_lead(conn, email)
    if not ok:
        return False, why
    conn.execute(
        "INSERT INTO leads(campaign,email,name,org,org_key,data_json,added_at,source,eligible_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            campaign, email, row.get("name", ""), row.get("org", ""),
            normalise_org(row.get("org", "")),
            json.dumps({k: v for k, v in row.items() if v not in (None, "")}),
            now(), source, now() + delay_seconds,
        ),
    )
    return True, ""


def set_lead_status(conn, lead_id: int, status: str, reason: str = "") -> None:
    conn.execute(
        "UPDATE leads SET status=?, status_reason=? WHERE id=?", (status, reason, lead_id)
    )


# --------------------------------------------------------------------------- #
# Counting sends (used by the caps in guard.py)
# --------------------------------------------------------------------------- #
def local_midnight_epoch() -> int:
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


def sends_since(conn, since_epoch: int, campaign: Optional[str] = None) -> int:
    if campaign:
        q = ("SELECT COUNT(*) c FROM sends s JOIN leads l ON l.id=s.lead_id "
             "WHERE s.sent_at>=? AND l.campaign=?")
        return conn.execute(q, (since_epoch, campaign)).fetchone()["c"]
    return conn.execute(
        "SELECT COUNT(*) c FROM sends WHERE sent_at>=?", (since_epoch,)
    ).fetchone()["c"]
