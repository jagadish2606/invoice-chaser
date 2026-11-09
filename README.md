# Invoice Chaser

An AI agent that gets freelancers paid. Paste a client chat, and the AI turns it into a real PayPal invoice, tracks payment, and writes follow-up reminders whose tone escalates the later the payment gets. A human approves every action.

Built for the PayPal AI Hackathon 2026 with the PayPal Invoicing API v2 (sandbox) and Google Gemini.

## Setup

1. Python 3.10+
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in:
   - PayPal sandbox Client ID and Secret from developer.paypal.com > Apps & Credentials (Sandbox)
   - A free Gemini API key from aistudio.google.com
4. `uvicorn app.main:app --reload`
5. Open http://localhost:8000

Existing `invoices.db` files are migrated automatically on startup (new tables and columns are added, nothing is deleted).

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

### Guardrails

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
| POST | `/webhooks/paypal` | PayPal webhook receiver (marks invoices paid) |

## Testing with the sandbox

Use the email of your **sandbox personal account** (developer.paypal.com > Testing Tools > Sandbox Accounts) as the client email. Log in to sandbox.paypal.com as that buyer to pay the invoice, then click "Refresh" (or wait for the agent's next run).

## License
MIT
