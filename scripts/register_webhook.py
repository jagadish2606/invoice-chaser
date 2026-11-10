"""Register Invoice Chaser's PayPal webhook and print its ID (for PAYPAL_WEBHOOK_ID).

Usage:
    python scripts/register_webhook.py https://your-app.onrender.com
    python scripts/register_webhook.py --list          # just show existing webhooks

Uses PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET / PAYPAL_BASE_URL from .env.
If a webhook for the same URL already exists, its ID is printed instead of creating a duplicate.
"""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
EVENTS = ["INVOICING.INVOICE.PAID", "INVOICING.INVOICE.CANCELLED", "INVOICING.INVOICE.REFUNDED"]


def token(client: httpx.Client) -> str:
    try:
        auth = (os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"])
    except KeyError as e:
        sys.exit(f"Missing {e.args[0]} in .env")
    r = client.post(f"{BASE}/v1/oauth2/token", auth=auth, data={"grant_type": "client_credentials"})
    if r.status_code != 200:
        sys.exit(f"PayPal login failed ({r.status_code}): check your client ID and secret")
    return r.json()["access_token"]


def list_webhooks(client: httpx.Client, headers: dict) -> list:
    r = client.get(f"{BASE}/v1/notifications/webhooks", headers=headers)
    r.raise_for_status()
    return r.json().get("webhooks", [])


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    with httpx.Client(timeout=30) as client:
        headers = {"Authorization": f"Bearer {token(client)}", "Content-Type": "application/json"}
        hooks = list_webhooks(client, headers)

        if args[0] == "--list":
            print(f"{len(hooks)} webhook(s) on {BASE}:")
            for h in hooks:
                print(f"  {h['id']}  {h['url']}  [{', '.join(e['name'] for e in h.get('event_types', []))}]")
            return

        base_url = args[0].rstrip("/")
        if not base_url.startswith("https://"):
            sys.exit("The URL must start with https:// (PayPal only delivers webhooks over HTTPS).")
        url = base_url if base_url.endswith("/webhooks/paypal") else f"{base_url}/webhooks/paypal"

        existing = next((h for h in hooks if h["url"].rstrip("/") == url), None)
        if existing:
            names = {e["name"] for e in existing.get("event_types", [])}
            missing = [e for e in EVENTS if e not in names and "*" not in names]
            print(f"A webhook for {url} already exists.")
            if missing:
                print(f"Note: it isn't subscribed to {', '.join(missing)}. Add them in the PayPal developer dashboard.")
            webhook_id = existing["id"]
        else:
            r = client.post(f"{BASE}/v1/notifications/webhooks", headers=headers,
                            json={"url": url, "event_types": [{"name": e} for e in EVENTS]})
            if r.status_code >= 400:
                sys.exit(f"PayPal refused to create the webhook ({r.status_code}): {r.text}")
            webhook_id = r.json()["id"]
            print(f"Created webhook for {url}")
            print(f"Events: {', '.join(EVENTS)}")

    print(f"\nPAYPAL_WEBHOOK_ID={webhook_id}")
    print("Set this on Render (Environment tab), then redeploy so signatures are verified.")


if __name__ == "__main__":
    main()
