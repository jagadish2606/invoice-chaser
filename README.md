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

## Testing with the sandbox

Use the email of your **sandbox personal account** (developer.paypal.com > Testing Tools > Sandbox Accounts) as the client email. Log in to sandbox.paypal.com as that buyer to pay the invoice, then click "Refresh status".

## License
MIT
