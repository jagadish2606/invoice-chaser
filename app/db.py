"""SQLite storage: schema/migrations, settings, activity log, and the agent's clock."""
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB = Path("invoices.db")


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS invoices (
        id TEXT PRIMARY KEY, client_name TEXT, client_email TEXT,
        amount TEXT, currency TEXT, due_date TEXT, status TEXT,
        reminders INTEGER DEFAULT 0, data TEXT, payment_url TEXT, last_reminder_on TEXT)""")
    # Older databases were created before these columns existed: add them in place.
    cols = {c["name"] for c in con.execute("PRAGMA table_info(invoices)")}
    for col in ("payment_url", "last_reminder_on"):
        if col not in cols:
            con.execute(f"ALTER TABLE invoices ADD COLUMN {col} TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT, stage TEXT,
        subject TEXT, note TEXT, reason TEXT, status TEXT DEFAULT 'pending',
        created_at TEXT, decided_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, invoice_id TEXT,
        action TEXT, detail TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return con


def now_iso() -> str:
    """Real wall-clock time (UTC), used for timestamps."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_setting(key: str, default: str | None = None) -> str | None:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as con:
        con.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def day_offset() -> int:
    return int(get_setting("day_offset", "0"))


def today() -> date:
    """The agent's 'today': the real date plus the demo time-travel offset."""
    return date.today() + timedelta(days=day_offset())


def days_overdue(due_date: str) -> int:
    return (today() - date.fromisoformat(due_date)).days


def log(action: str, detail: str = "", invoice_id: str | None = None) -> None:
    with db() as con:
        con.execute("INSERT INTO activity (timestamp, invoice_id, action, detail) VALUES (?,?,?,?)",
                    (now_iso(), invoice_id, action, detail))
