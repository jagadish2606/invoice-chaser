"""Invoice Chaser: FastAPI backend."""
from dotenv import load_dotenv
load_dotenv()

import json
import sqlite3
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import ai, paypal

app = FastAPI(title="Invoice Chaser")
DB = Path("invoices.db")


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS invoices (
        id TEXT PRIMARY KEY, client_name TEXT, client_email TEXT,
        amount TEXT, currency TEXT, due_date TEXT, status TEXT,
        reminders INTEGER DEFAULT 0, data TEXT, payment_url TEXT)""")
    # Older databases were created before payment_url existed: add it in place.
    cols = {c["name"] for c in con.execute("PRAGMA table_info(invoices)")}
    if "payment_url" not in cols:
        con.execute("ALTER TABLE invoices ADD COLUMN payment_url TEXT")
    return con


def payment_url(invoice: dict) -> str | None:
    """The payer's PayPal page for this invoice (only exists once it is sent)."""
    return invoice.get("detail", {}).get("metadata", {}).get("recipient_view_url")


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
    await paypal.send_invoice(invoice_id)
    try:
        url = payment_url(await paypal.get_invoice(invoice_id))
    except Exception:
        url = None  # invoice is already sent; "Refresh status" can fill the link in later
    total = sum(float(i["unit_price"]) * float(i.get("quantity", 1)) for i in inv.items)
    with db() as con:
        con.execute("""INSERT INTO invoices (id, client_name, client_email, amount, currency,
            due_date, status, reminders, data, payment_url) VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            invoice_id, inv.client_name, inv.client_email, f"{total:.2f}",
            inv.currency, inv.due_date, "SENT", 0, json.dumps(inv.model_dump()), url))
    return {"id": invoice_id, "status": "SENT", "total": f"{total:.2f}", "payment_url": url}


@app.get("/api/invoices")
async def list_invoices():
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM invoices ORDER BY due_date")]
    for r in rows:
        r["days_overdue"] = (date.today() - date.fromisoformat(r["due_date"])).days
        r.pop("data")
    return rows


@app.post("/api/invoices/{invoice_id}/refresh")
async def refresh(invoice_id: str):
    """Pull the latest status from PayPal (useful before webhooks are set up)."""
    inv = await paypal.get_invoice(invoice_id)
    with db() as con:
        con.execute("UPDATE invoices SET status=?, payment_url=COALESCE(?, payment_url) WHERE id=?",
                    (inv["status"], payment_url(inv), invoice_id))
    return {"id": invoice_id, "status": inv["status"]}


@app.post("/api/invoices/{invoice_id}/draft-reminder")
async def draft_reminder(invoice_id: str):
    """AI writes a reminder whose tone matches how late the payment is."""
    with db() as con:
        r = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Invoice not found")
    days = (date.today() - date.fromisoformat(r["due_date"])).days
    try:
        return await ai.write_reminder(r["client_name"], f'{r["amount"]} {r["currency"]}',
                                       r["due_date"], days)
    except ai.AIBusyError as e:
        raise HTTPException(503, str(e))


class ReminderIn(BaseModel):
    subject: str
    note: str


@app.post("/api/invoices/{invoice_id}/remind")
async def send_reminder(invoice_id: str, body: ReminderIn):
    """Human-approved reminder goes out through PayPal."""
    await paypal.remind(invoice_id, body.subject, body.note)
    with db() as con:
        con.execute("UPDATE invoices SET reminders = reminders + 1 WHERE id=?", (invoice_id,))
    return {"ok": True}


@app.post("/webhooks/paypal")
async def webhook(request: Request):
    event = await request.json()
    if not await paypal.verify_webhook(dict(request.headers), event):
        raise HTTPException(400, "Invalid signature")
    if event.get("event_type") == "INVOICING.INVOICE.PAID":
        res = event.get("resource", {})
        invoice_id = res.get("invoice", res).get("id")
        with db() as con:
            con.execute("UPDATE invoices SET status='PAID' WHERE id=?", (invoice_id,))
    return {"received": True}
