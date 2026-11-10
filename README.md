# Invoice Chaser

An AI agent that gets freelancers paid. Paste a client chat, and the AI turns it into a real PayPal invoice, tracks payment, and writes follow-up reminders whose tone escalates the later the payment gets. A human approves every action.

Built for the PayPal AI Hackathon 2026 with the PayPal Invoicing API v2 (sandbox) and Google Gemini.

## Live demo

**https://invoice-chaser-pk48.onrender.com**

> **Note:** it runs on Render's free tier, so the instance sleeps when idle and the first visit may take up to a minute to wake it up. After that it's fast.

It runs on PayPal **sandbox**, so no real money moves.

## Why?

Freelancers lose time and money chasing late payments: [Bonsai](https://www.hellobonsai.com/blog/late-freelance-payment) found that 29% of freelance invoices are paid late, and IPSE research puts the time freelancers spend dealing with late payments at about 20 days a year. Invoice Chaser does the chasing, and you stay in control: **paste chat → AI creates the PayPal invoice → agent chases, you approve.**

## Architecture

```
Browser (single HTML page, AG Grid for the invoices table)
   │  JSON over HTTPS
FastAPI app (app/main.py) ── background agent loop (app/agent.py)
   ├── SQLite (app/db.py): invoices, drafts, activity log, settings
   ├── PayPal Invoicing API v2 (app/paypal.py): create, send, status, remind
   │      ▲ webhooks: INVOICING.INVOICE.PAID / CANCELLED / REFUNDED → /webhooks/paypal
   └── Google Gemini (app/ai.py): chat → invoice, reminder drafts, "why now" reasons
Hosted on Render (render.yaml Blueprint)
```

- **FastAPI** serves the API and the single-page UI (`app/static/index.html`, plain HTML/CSS/JS, no build step).
- **SQLite** stores everything in one file (`DB_PATH`, default `invoices.db`). Existing databases are migrated automatically on startup (new tables and columns are added, nothing is deleted).
- **PayPal Invoicing API v2** creates, sends and reminds; **PayPal webhooks** push payment updates instantly.
- **Gemini** (google-genai SDK) extracts invoices from chats and drafts reminders, with retries and model fallback when it's busy.
- **AG Grid Community** (loaded from jsDelivr, pinned version) powers the Invoices table: sorting, column filters, quick search, pagination.
- **Render** hosts it from `render.yaml`.

## Run locally

1. Python 3.10+ (tested on 3.10.18)
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in:
   - PayPal sandbox Client ID and Secret from developer.paypal.com > Apps & Credentials (Sandbox)
   - A free Gemini API key from aistudio.google.com
4. `uvicorn app.main:app --reload`
5. Open http://localhost:8000

## Deploy to Render

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo. Render reads `render.yaml` (free Python web service, health check `/healthz`).
3. Render asks for the secret values marked `sync: false`: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `GEMINI_API_KEY`, `FREELANCER_NAME`. Leave `PAYPAL_WEBHOOK_ID` empty for now.
4. Deploy, then open `https://<your-service>.onrender.com/healthz`. You should see `{"ok": true}`.

**Free tier notes**
- **Storage is temporary.** The SQLite file lives on the instance's disk, so the data resets on every redeploy or restart. Fine for a demo; use a Render disk or a hosted database to keep data.
- **Free instances sleep when idle** (after about 15 minutes without traffic). The background agent only runs while the app is awake; **Run agent now** always works, and the first request after sleeping takes a while.
- **AI rate limit:** `/api/extract`, reminder drafting and agent runs are limited to 10 requests per minute per visitor IP (`AI_RATE_LIMIT_PER_MINUTE`) to protect the free Gemini quota. Visitors over the limit get a friendly "try again in N seconds" message.
- **There is no login.** Anyone with the URL can use the demo (on your PayPal sandbox). Keep it on sandbox credentials.

## PayPal webhooks (instant payment updates)

Without webhooks, payments are picked up on the agent's next run or when you click Refresh. With them, PayPal tells the app the moment an invoice is paid, cancelled or refunded: the status updates, pending reminder drafts are withdrawn, the event is logged, and the UI shows a "💸 Payment received" toast.

1. Deploy first (PayPal only delivers to public HTTPS URLs).
2. On your computer, with your sandbox credentials in `.env`:
   ```
   python scripts/register_webhook.py https://<your-service>.onrender.com
   ```
   It registers `https://<your-service>.onrender.com/webhooks/paypal` for `INVOICING.INVOICE.PAID`, `CANCELLED` and `REFUNDED`, and prints `PAYPAL_WEBHOOK_ID=...`. If a webhook for that URL already exists, it prints the existing ID instead of creating a duplicate. (`python scripts/register_webhook.py --list` shows your webhooks.)
3. In Render → your service → **Environment**, set `PAYPAL_WEBHOOK_ID` to that ID and save (Render redeploys).
4. From then on every webhook's signature is verified with PayPal, and unverified events are rejected. **Always set `PAYPAL_WEBHOOK_ID` on a public deployment**: without it, signature checks are skipped (handy for local testing, unsafe in public).

## The chasing agent

The agent runs on its own every `AGENT_INTERVAL_MINUTES` (default 5) in the background, and on demand with the **Run agent now** button. On each run, for every invoice that isn't paid or cancelled, it:

1. **Checks PayPal first.** If the invoice has been paid, it records the payment, logs "Payment received", and withdraws any reminder drafts still waiting.
2. **Works out the stage** from how far the invoice is from its due date:

   | Stage | When | Tone |
   |---|---|---|
   | Pre-due | 1–3 days before the due date | Warm heads-up |
   | Friendly | 1–7 days overdue | Polite, assumes they forgot |
   | Professional | 8–14 days overdue | Clear, asks for a payment date |
   | Firm | 15+ days overdue | Firm but respectful, asks for payment within 3 days |

   Earlier than 3 days before the due date (and on the due date itself), it does nothing.
3. **Drafts a reminder** with Gemini in that tone, plus a one-sentence **"Why now"** explanation (for example: *"Invoice is 9 days overdue and the friendly reminder got no response, so escalating to a professional tone."*).
4. **Puts the draft in the approval queue.** The agent never sends anything by itself.

An error on one invoice (PayPal or Gemini) is logged and doesn't stop the others. If Gemini is busy, the AI calls retry and fall back to other models first.

### Trust and guardrails

- **The agent never sends anything.** It only drafts; a human approves (and can edit) every reminder.
- **Every draft explains itself** with a one-sentence "Why now" reason.
- **Everything is logged** in the activity log, including errors.
- **Webhooks are signature-verified** when `PAYPAL_WEBHOOK_ID` is set.
- **AI endpoints are rate-limited** per IP on the public demo.
- At most `MAX_REMINDERS` reminders per invoice (default 4).
- At least `MIN_DAYS_BETWEEN` days between reminders (default 2).
- Never more than 2 reminders per invoice per day (PayPal's own limit).
- Never a new draft while one is already waiting for approval for that invoice. Approve or reject it first, and the agent escalates on its next run.
- Never the same stage twice for the same invoice (a rejected Friendly draft means no more Friendly drafts).

### Approval flow

Open **Approval Queue**. Each draft shows the client, amount, stage, the AI's "Why now" reason, and an editable subject and message.

- **Approve & Send** sends your (possibly edited) reminder through PayPal, which emails the client. The reminder counter goes up.
- **Reject** discards it. Nothing is sent.

### Activity log

Everything is logged: invoices created and sent, agent runs, status changes, drafts (with the reason), approvals, rejections, reminders sent, payments, webhooks, time-travel changes and errors. See the **Activity** screen.

## Demo mode: time travel

To show escalation without waiting weeks, use **Demo: time travel** in the sidebar (+0, +7, +12, +18 days). It shifts the agent's idea of "today" by that many days; a banner shows when it's on. All agent date maths (stages, days overdue, gaps between reminders) uses the shifted date. PayPal's real per-day limit still uses the real date. Set it back to +0 when you're done.

Typical demo: create an invoice due in 7 days → +0 and run the agent (nothing yet) → +12 and run (Friendly draft) → approve or reject it → +18 and run (Professional draft).

## API

| Method | Path | What it does |
|---|---|---|
| POST | `/api/extract` | AI turns a chat into an invoice draft |
| POST | `/api/invoices` | Create and send a PayPal invoice |
| GET | `/api/invoices` | List invoices (with `days_overdue` and `payment_url`) |
| POST | `/api/invoices/{id}/refresh` | Pull the latest status from PayPal |
| POST | `/api/invoices/{id}/draft-reminder` | AI drafts a one-off reminder |
| POST | `/api/invoices/{id}/remind` | Send a reminder you wrote or edited |
| POST | `/api/agent/run` | Run the agent now; returns what it did per invoice |
| GET | `/api/drafts?status=pending` | Approval queue (`status` is optional: pending, sent, rejected) |
| POST | `/api/drafts/{id}/approve` | Send a draft, optionally with edited `subject` / `note` |
| POST | `/api/drafts/{id}/reject` | Reject a draft |
| GET | `/api/activity?limit=50` | Activity log, newest first |
| GET | `/api/stats` | `total_outstanding`, `overdue_count`, `paid_total`, `pending_approvals`, `invoices_count`, `last_agent_run` |
| GET / POST | `/api/settings` | Read or set `day_offset` (demo time travel) |
| POST | `/webhooks/paypal` | PayPal webhook receiver: PAID / CANCELLED / REFUNDED (signature-verified when `PAYPAL_WEBHOOK_ID` is set) |
| GET | `/healthz` | Health check for Render: `{"ok": true}` |

## Testing with the sandbox

Use the email of your **sandbox personal account** (developer.paypal.com > Testing Tools > Sandbox Accounts) as the client email. Log in to sandbox.paypal.com as that buyer to pay the invoice, then click "Refresh" (or wait for the agent's next run).

## License
MIT
