"""Thin async client for the PayPal Invoicing API v2 (sandbox)."""
import os
import time
import httpx

BASE = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
_token = {"value": None, "expires": 0}


class PayPalError(RuntimeError):
    """PayPal answered with an error status."""


async def _get_token() -> str:
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE}/v1/oauth2/token",
            auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
            data={"grant_type": "client_credentials"},
        )
        r.raise_for_status()
        d = r.json()
    _token.update(value=d["access_token"], expires=time.time() + d["expires_in"])
    return _token["value"]


async def _call(method: str, path: str, json: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {await _get_token()}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, f"{BASE}{path}", json=json, headers=headers)
    if r.status_code >= 400:
        raise PayPalError(f"PayPal {r.status_code}: {r.text}")
    return r.json() if r.content else {}


async def create_invoice(d: dict) -> dict:
    """d = output of ai.extract_invoice()."""
    currency = d.get("currency") or os.getenv("DEFAULT_CURRENCY", "USD")
    first, _, last = (d.get("client_name") or "Client").partition(" ")
    body = {
        "detail": {
            "currency_code": currency,
            "note": d.get("note") or "Thank you for your business!",
            "payment_term": {"due_date": d["due_date"]},
        },
        "invoicer": {"name": {"given_name": os.getenv("FREELANCER_NAME", "Freelancer")}},
        "primary_recipients": [{
            "billing_info": {
                "name": {"given_name": first, "surname": last or first},
                "email_address": d["client_email"],
            }
        }],
        "items": [
            {
                "name": it["description"][:200],
                "quantity": str(it.get("quantity", 1)),
                "unit_amount": {"currency_code": currency, "value": f'{float(it["unit_price"]):.2f}'},
            }
            for it in d["items"]
        ],
    }
    return await _call("POST", "/v2/invoicing/invoices", body)


async def send_invoice(invoice_id: str) -> dict:
    return await _call("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                       {"send_to_invoicer": True})


async def get_invoice(invoice_id: str) -> dict:
    return await _call("GET", f"/v2/invoicing/invoices/{invoice_id}")


async def remind(invoice_id: str, subject: str, note: str) -> dict:
    # PayPal limit: max 2 reminders per invoice per day
    return await _call("POST", f"/v2/invoicing/invoices/{invoice_id}/remind",
                       {"subject": subject, "note": note, "send_to_invoicer": True})


async def verify_webhook(headers: dict, event: dict) -> bool:
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        return True  # skip verification in local dev
    body = {
        "auth_algo": headers.get("paypal-auth-algo"),
        "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": webhook_id,
        "webhook_event": event,
    }
    res = await _call("POST", "/v1/notifications/verify-webhook-signature", body)
    return res.get("verification_status") == "SUCCESS"
