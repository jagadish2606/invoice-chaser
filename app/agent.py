"""Autonomous chasing agent: checks open invoices and drafts reminders for human approval.

It never sends anything itself. Every reminder waits in the approval queue (drafts table).
"""
import asyncio
import os
from datetime import date, datetime, timezone

from . import ai, paypal
from .db import db, days_overdue, log, now_iso, set_setting, today

INTERVAL_MINUTES = float(os.getenv("AGENT_INTERVAL_MINUTES", "5"))
MAX_REMINDERS = int(os.getenv("MAX_REMINDERS", "4"))
MIN_DAYS_BETWEEN = int(os.getenv("MIN_DAYS_BETWEEN", "2"))
MAX_PER_DAY = 2  # PayPal allows at most 2 reminders per invoice per day

PAID = {"PAID", "MARKED_AS_PAID"}
CLOSED = PAID | {"CANCELLED"}
STAGE_LABELS = {"pre_due": "Pre-due", "friendly": "Friendly", "professional": "Professional", "firm": "Firm"}

_lock = asyncio.Lock()


def stage_for(days: int) -> str | None:
    """days = days past the due date (negative = not due yet)."""
    if -3 <= days <= -1:
        return "pre_due"
    if 1 <= days <= 7:
        return "friendly"
    if 8 <= days <= 14:
        return "professional"
    if days >= 15:
        return "firm"
    return None  # too early, or due today


def withdraw_pending_drafts(invoice_id: str, why: str) -> None:
    with db() as con:
        n = con.execute("UPDATE drafts SET status='rejected', decided_at=? WHERE invoice_id=? AND status='pending'",
                        (now_iso(), invoice_id)).rowcount
    if n:
        log("draft_rejected", f"{n} pending draft(s) withdrawn automatically: {why}", invoice_id)


def apply_status(inv: dict, remote: dict) -> str:
    """Save PayPal's latest status for an invoice row and log what changed. Returns the new status."""
    status = remote["status"]
    url = remote.get("detail", {}).get("metadata", {}).get("recipient_view_url")
    with db() as con:
        con.execute("UPDATE invoices SET status=?, payment_url=COALESCE(?, payment_url) WHERE id=?",
                    (status, url, inv["id"]))
    if status != inv["status"]:
        log("status_changed", f"{inv['status']} → {status}", inv["id"])
        if status in PAID:
            log("payment_received", f"{inv['amount']} {inv['currency']} from {inv['client_name']}", inv["id"])
        if status in CLOSED:
            withdraw_pending_drafts(inv["id"], f"invoice is {status.lower()}")
    return status


def _reminders_sent_today(con, invoice_id: str) -> int:
    real_today = datetime.now(timezone.utc).date().isoformat()
    return con.execute("SELECT COUNT(*) FROM activity WHERE invoice_id=? AND action='reminder_sent' "
                       "AND substr(timestamp, 1, 10)=?", (invoice_id, real_today)).fetchone()[0]


def _facts(inv: dict, days: int, stage: str, history: list) -> str:
    timing = f"{days} days overdue" if days > 0 else f"due in {-days} days"
    past = "; ".join(f"{STAGE_LABELS.get(h['stage'], h['stage'])} reminder {h['status']}"
                     f" ({(h['decided_at'] or h['created_at'])[:10]})" for h in history) or "none"
    return (f"- Client: {inv['client_name']}\n- Amount: {inv['amount']} {inv['currency']}\n"
            f"- Due date: {inv['due_date']}, today is {today()}, so the invoice is {timing}\n"
            f"- PayPal status: {inv['status']} (not paid)\n"
            f"- Reminders sent so far: {inv['reminders']} (last on {inv['last_reminder_on'] or 'never'})\n"
            f"- Earlier reminder drafts: {past}\n"
            f"- New tone chosen: {STAGE_LABELS[stage]}")


async def check_invoice(inv: dict) -> str:
    """Process one open invoice. Returns a short human-readable outcome."""
    status = apply_status(inv, await paypal.get_invoice(inv["id"]))
    if status in PAID:
        return "Payment received"
    if status in CLOSED:
        return f"Closed ({status})"

    days = days_overdue(inv["due_date"])
    stage = stage_for(days)
    if not stage:
        return f"No action yet ({'due today' if days == 0 else f'due in {-days} days'})"

    with db() as con:
        history = [dict(r) for r in con.execute(
            "SELECT stage, status, created_at, decided_at FROM drafts WHERE invoice_id=? ORDER BY id", (inv["id"],))]
        sent_today = _reminders_sent_today(con, inv["id"])
    label = STAGE_LABELS[stage]
    if inv["reminders"] >= MAX_REMINDERS:
        return f"Skipped: already sent the maximum of {MAX_REMINDERS} reminders"
    if any(h["status"] == "pending" for h in history):
        return "Skipped: a draft is already waiting for your approval"
    if any(h["stage"] == stage for h in history):
        return f"Skipped: a {label} reminder was already drafted"
    if inv["last_reminder_on"] and (today() - date.fromisoformat(inv["last_reminder_on"])).days < MIN_DAYS_BETWEEN:
        return f"Skipped: last reminder was less than {MIN_DAYS_BETWEEN} days ago"
    if sent_today >= MAX_PER_DAY:
        return f"Skipped: PayPal's limit of {MAX_PER_DAY} reminders per day reached"

    draft = await ai.write_reminder(inv["client_name"], f"{inv['amount']} {inv['currency']}", inv["due_date"], days)
    try:
        reason = await ai.explain_action(_facts(inv, days, stage, history))
    except ai.AIBusyError:
        timing = f"{days} days overdue" if days > 0 else f"due in {-days} days"
        reason = f"Invoice is {timing} and still unpaid, so drafting a {label.lower()} reminder."
    with db() as con:
        con.execute("INSERT INTO drafts (invoice_id, stage, subject, note, reason, status, created_at) "
                    "VALUES (?,?,?,?,?, 'pending', ?)",
                    (inv["id"], stage, draft["subject"], draft["note"], reason, now_iso()))
    log("draft_created", f"{label} reminder drafted. Why: {reason}", inv["id"])
    return f"Drafted {label} reminder"


async def run_agent_cycle(trigger: str = "scheduled") -> dict:
    if _lock.locked():
        return {"skipped": True, "message": "The agent is already running"}
    async with _lock:
        with db() as con:
            invoices = [dict(r) for r in con.execute(
                f"SELECT * FROM invoices WHERE status NOT IN ({','.join('?' * len(CLOSED))})", tuple(CLOSED))]
        results = []
        for inv in invoices:
            try:
                outcome = await check_invoice(inv)
            except Exception as e:  # one bad invoice must not stop the others
                outcome = f"Error: {e}"[:300]
                log("error", f"Agent error: {e}"[:500], inv["id"])
            results.append({"invoice_id": inv["id"], "client_name": inv["client_name"], "outcome": outcome})
        drafted = sum(r["outcome"].startswith("Drafted") for r in results)
        ran_at = now_iso()
        set_setting("last_agent_run", ran_at)
        log("agent_run", f"{trigger.capitalize()} run (agent date {today()}): checked {len(invoices)} "
                         f"open invoice(s), {drafted} new draft(s)")
        return {"ran_at": ran_at, "agent_date": today().isoformat(), "checked": len(invoices),
                "drafted": drafted, "results": results}


async def run_forever() -> None:
    """Background loop started by the app's lifespan."""
    while True:
        await asyncio.sleep(INTERVAL_MINUTES * 60)
        try:
            await run_agent_cycle("scheduled")
        except Exception as e:
            log("error", f"Scheduled agent run failed: {e}"[:500])
