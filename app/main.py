"""Invoice Chaser: FastAPI backend."""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import agent, ai, paypal
from .db import day_offset, days_overdue, db, get_setting, log, now_iso, set_setting, today


@asynccontextmanager
async def lifespan(app: FastAPI):
    db().close()  # create / migrate tables at startup
    task = asyncio.create_task(agent.run_forever())
    yield
    task.cancel()


app = FastAPI(title="Invoice Chaser", lifespan=lifespan)


@app.exception_handler(paypal.PayPalError)
async def paypal_error(request: Request, exc: paypal.PayPalError):
    """Turn PayPal failures into a readable 502 instead of a 500 crash."""
    text = str(exc)
    try:
        body = json.loads(text.split(": ", 1)[1])
        issues = "; ".join(d.get("description") or d.get("issue", "") for d in body.get("details", []))
        message = f"PayPal: {body.get('message', 'request failed')}" + (f" ({issues})" if issues else "")
    except (IndexError, ValueError, AttributeError):
        message = text[:300]
    invoice_id = request.path_params.get("invoice_id")
    log("error", message, invoice_id)
    return JSONResponse(status_code=502, content={"detail": message})


def payment_url(invoice: dict) -> str | None:
    """The payer's PayPal page for this invoice (only exists once it is sent)."""
    return invoice.get("detail", {}).get("metadata", {}).get("recipient_view_url")


def get_invoice_row(invoice_id: str) -> dict:
    with db() as con:
        r = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Invoice not found")
    return dict(r)


def record_reminder_sent(invoice_id: str, subject: str) -> None:
    with db() as con:
        con.execute("UPDATE invoices SET reminders = reminders + 1, last_reminder_on=? WHERE id=?",
                    (today().isoformat(), invoice_id))
    log("reminder_sent", f"Reminder sent via PayPal: \"{subject}\"", invoice_id)


class ChatIn(BaseModel):
    chat: str


class InvoiceIn(BaseModel):
    client_name: str
    client_email: str
    currency: str = "USD"
    items: list[dict]
    due_date: str
    note: str | None = None


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/api/extract")
async def extract(body: ChatIn):
    """Step 1: AI reads the messy chat and proposes an invoice (human reviews it)."""
    try:
        return await ai.extract_invoice(body.chat)
    except ai.AIBusyError as e:
        raise HTTPException(503, str(e))


@app.post("/api/invoices")
async def create_and_send(inv: InvoiceIn):
    """Step 2: after the freelancer confirms, create and send a real PayPal invoice."""
    if not inv.client_email:
        raise HTTPException(400, "Client email is required")
    created = await paypal.create_invoice(inv.model_dump())
    invoice_id = created["id"]
    total = sum(float(i["unit_price"]) * float(i.get("quantity", 1)) for i in inv.items)
    log("invoice_created", f"Invoice for {inv.client_name}: {total:.2f} {inv.currency}, due {inv.due_date}",
        invoice_id)
    await paypal.send_invoice(invoice_id)
    try:
        url = payment_url(await paypal.get_invoice(invoice_id))
    except Exception:
        url = None  # invoice is already sent; "Refresh status" can fill the link in later
    with db() as con:
        con.execute("""INSERT INTO invoices (id, client_name, client_email, amount, currency,
            due_date, status, reminders, data, payment_url) VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            invoice_id, inv.client_name, inv.client_email, f"{total:.2f}",
            inv.currency, inv.due_date, "SENT", 0, json.dumps(inv.model_dump()), url))
    log("invoice_sent", f"Sent to {inv.client_email} via PayPal", invoice_id)
    return {"id": invoice_id, "status": "SENT", "total": f"{total:.2f}", "payment_url": url}


@app.get("/api/invoices")
async def list_invoices():
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM invoices ORDER BY due_date")]
    for r in rows:
        r["days_overdue"] = days_overdue(r["due_date"])
        r.pop("data")
    return rows


@app.post("/api/invoices/{invoice_id}/refresh")
async def refresh(invoice_id: str):
    """Pull the latest status from PayPal (useful before webhooks are set up)."""
    row = get_invoice_row(invoice_id)
    status = agent.apply_status(row, await paypal.get_invoice(invoice_id))
    return {"id": invoice_id, "status": status}


@app.post("/api/invoices/{invoice_id}/draft-reminder")
async def draft_reminder(invoice_id: str):
    """AI writes a reminder whose tone matches how late the payment is."""
    r = get_invoice_row(invoice_id)
    try:
        return await ai.write_reminder(r["client_name"], f'{r["amount"]} {r["currency"]}',
                                       r["due_date"], days_overdue(r["due_date"]))
    except ai.AIBusyError as e:
        raise HTTPException(503, str(e))


class ReminderIn(BaseModel):
    subject: str
    note: str


@app.post("/api/invoices/{invoice_id}/remind")
async def send_reminder(invoice_id: str, body: ReminderIn):
    """Human-approved reminder goes out through PayPal."""
    await paypal.remind(invoice_id, body.subject, body.note)
    record_reminder_sent(invoice_id, body.subject)
    return {"ok": True}


# ---------- Agent ----------

@app.post("/api/agent/run")
async def run_agent():
    """Run one agent cycle now. It only creates drafts; nothing is sent without approval."""
    return await agent.run_agent_cycle("manual")


# ---------- Approval queue ----------

@app.get("/api/drafts")
async def list_drafts(status: str | None = None):
    sql = """SELECT d.*, i.client_name, i.client_email, i.amount, i.currency, i.due_date,
             i.reminders, i.payment_url FROM drafts d JOIN invoices i ON i.id = d.invoice_id"""
    args = ()
    if status:
        sql += " WHERE d.status=?"
        args = (status,)
    with db() as con:
        rows = [dict(r) for r in con.execute(sql + " ORDER BY d.id DESC", args)]
    for r in rows:
        r["days_overdue"] = days_overdue(r["due_date"])
    return rows


def get_pending_draft(draft_id: int) -> dict:
    with db() as con:
        d = con.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if not d:
        raise HTTPException(404, "Draft not found")
    if d["status"] != "pending":
        raise HTTPException(409, f"This draft was already {d['status']}")
    return dict(d)


class DraftEdit(BaseModel):
    subject: str | None = None
    note: str | None = None


@app.post("/api/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, body: DraftEdit | None = None):
    """The human approves (optionally edits) a draft; only now is the reminder sent via PayPal."""
    d = get_pending_draft(draft_id)
    inv = get_invoice_row(d["invoice_id"])
    if inv["status"] in agent.CLOSED:
        raise HTTPException(409, f"Invoice is already {inv['status'].lower()}, nothing to send")
    with db() as con:
        if agent._reminders_sent_today(con, d["invoice_id"]) >= agent.MAX_PER_DAY:
            raise HTTPException(429, "PayPal allows only 2 reminders per invoice per day. Try again tomorrow.")
    subject = (body.subject if body and body.subject else d["subject"]).strip()
    note = (body.note if body and body.note else d["note"]).strip()
    edited = subject != d["subject"] or note != d["note"]
    try:
        await paypal.remind(d["invoice_id"], subject, note)
    except paypal.PayPalError:
        log("error", f"Sending approved draft #{draft_id} failed", d["invoice_id"])
        raise
    with db() as con:
        con.execute("UPDATE drafts SET status='sent', subject=?, note=?, decided_at=? WHERE id=?",
                    (subject, note, now_iso(), draft_id))
    log("draft_approved", f"Draft #{draft_id} approved{' with edits' if edited else ''}", d["invoice_id"])
    record_reminder_sent(d["invoice_id"], subject)
    return {"ok": True, "id": draft_id, "status": "sent"}


@app.post("/api/drafts/{draft_id}/reject")
async def reject_draft(draft_id: int):
    d = get_pending_draft(draft_id)
    with db() as con:
        con.execute("UPDATE drafts SET status='rejected', decided_at=? WHERE id=?", (now_iso(), draft_id))
    log("draft_rejected", f"Draft #{draft_id} rejected", d["invoice_id"])
    return {"ok": True, "id": draft_id, "status": "rejected"}


# ---------- Activity, stats, settings ----------

@app.get("/api/activity")
async def activity(limit: int = 50):
    limit = max(1, min(limit, 500))
    with db() as con:
        return [dict(r) for r in con.execute(
            """SELECT a.*, i.client_name FROM activity a LEFT JOIN invoices i ON i.id = a.invoice_id
               ORDER BY a.id DESC LIMIT ?""", (limit,))]


@app.get("/api/stats")
async def stats():
    with db() as con:
        invoices = [dict(r) for r in con.execute("SELECT amount, status, due_date FROM invoices")]
        pending = con.execute("SELECT COUNT(*) FROM drafts WHERE status='pending'").fetchone()[0]
    open_ = [i for i in invoices if i["status"] not in agent.CLOSED]
    return {
        "total_outstanding": round(sum(float(i["amount"]) for i in open_), 2),
        "overdue_count": sum(days_overdue(i["due_date"]) > 0 for i in open_),
        "paid_total": round(sum(float(i["amount"]) for i in invoices if i["status"] in agent.PAID), 2),
        "pending_approvals": pending,
        "invoices_count": len(invoices),
        "last_agent_run": get_setting("last_agent_run"),
    }


def settings_payload() -> dict:
    return {"day_offset": day_offset(), "agent_today": today().isoformat(),
            "agent_interval_minutes": agent.INTERVAL_MINUTES, "max_reminders": agent.MAX_REMINDERS,
            "min_days_between": agent.MIN_DAYS_BETWEEN}


@app.get("/api/settings")
async def get_settings():
    return settings_payload()


class SettingsIn(BaseModel):
    day_offset: int


@app.post("/api/settings")
async def update_settings(body: SettingsIn):
    """Demo time travel: shift the agent's 'today' by N days."""
    if not -365 <= body.day_offset <= 365:
        raise HTTPException(400, "day_offset must be between -365 and 365")
    old = day_offset()
    set_setting("day_offset", str(body.day_offset))
    if old != body.day_offset:
        log("settings_changed", f"Demo time travel: day offset {old:+d} → {body.day_offset:+d} "
                                f"(agent date is now {today()})")
    return settings_payload()


@app.post("/webhooks/paypal")
async def webhook(request: Request):
    event = await request.json()
    if not await paypal.verify_webhook(dict(request.headers), event):
        raise HTTPException(400, "Invalid signature")
    log("webhook_received", event.get("event_type", "unknown event"))
    if event.get("event_type") == "INVOICING.INVOICE.PAID":
        res = event.get("resource", {})
        invoice_id = res.get("invoice", res).get("id")
        with db() as con:
            row = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if row:
            agent.apply_status(dict(row), {"status": "PAID"})
    return {"received": True}
