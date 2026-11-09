"""AI layer (Google Gemini, free tier): turns messy chats into invoice data, and writes reminders."""
import asyncio
import os
from datetime import date
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

load_dotenv()  # make sure .env is loaded even if this module is imported first
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = os.getenv("AI_MODEL", "gemini-3.5-flash")
FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.6-flash"]
RETRY_DELAYS = [2, 4, 6]  # seconds to wait before each retry
RETRYABLE_CODES = {429, 500, 503}  # rate limited / server error / overloaded


class AIBusyError(Exception):
    """Every model was overloaded or rate limited."""

    def __init__(self):
        super().__init__("AI is busy right now, please try again in a minute.")


class Item(BaseModel):
    description: str
    quantity: float
    unit_price: float


class InvoiceDraft(BaseModel):
    client_name: str
    client_email: str = Field(description="Empty string if not mentioned")
    currency: str = Field(description="ISO code like USD, EUR")
    items: list[Item]
    due_date: str = Field(description="YYYY-MM-DD")
    note: str = Field(description="Short friendly note for the client")
    missing_info: list[str] = Field(description="Things the freelancer must confirm, e.g. email or price")


class Reminder(BaseModel):
    subject: str
    note: str


class Reason(BaseModel):
    reason: str = Field(description="One sentence, under 30 words")


async def _ask(prompt: str, schema: type[BaseModel]) -> dict:
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
    )
    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    for model in models:
        # First try, then one retry after each delay; move to the next model if all fail.
        for delay in [0] + RETRY_DELAYS:
            await asyncio.sleep(delay)
            try:
                resp = await client.aio.models.generate_content(
                    model=model, contents=prompt, config=config)
                return schema.model_validate_json(resp.text).model_dump()
            except errors.APIError as e:
                if e.code not in RETRYABLE_CODES:
                    raise
                print(f"Gemini {model} returned {e.code}, retrying...")
    raise AIBusyError()


async def extract_invoice(chat_text: str) -> dict:
    today = date.today().isoformat()
    prompt = (
        f"Today is {today}. A freelancer pasted this conversation with a client. "
        "Extract the agreed work as invoice line items. Resolve relative dates "
        "('Friday', 'end of month') to YYYY-MM-DD. If no due date, use 7 days from today. "
        "Never invent an email or price; leave it empty/0 and list it in missing_info instead.\n\n"
        f"<conversation>\n{chat_text}\n</conversation>"
    )
    return await _ask(prompt, InvoiceDraft)


async def write_reminder(client_name: str, amount: str, due_date: str, days_overdue: int) -> dict:
    if days_overdue <= 0:
        tone = "a warm, friendly heads-up before the due date"
    elif days_overdue <= 7:
        tone = "polite and friendly, assume they simply forgot"
    elif days_overdue <= 14:
        tone = "professional and clear, ask for a payment date"
    else:
        tone = "firm but respectful, say it is significantly overdue and request payment within 3 days"
    prompt = (
        f"Write a payment reminder to {client_name} for an invoice of {amount}, "
        f"due {due_date} ({days_overdue} days overdue). Tone: {tone}. "
        "Note under 80 words, no placeholders, no sign-off name."
    )
    return await _ask(prompt, Reminder)


async def explain_action(facts: str) -> str:
    """One sentence telling the freelancer why the agent is drafting a reminder right now."""
    prompt = (
        "You are an invoice-chasing assistant. Based only on these facts, write ONE sentence "
        "explaining to the freelancer why you are drafting a payment reminder now and why this tone. "
        "Example: 'Invoice is 9 days overdue and the friendly reminder got no response, so "
        "escalating to a professional tone.' Do not invent facts.\n\n"
        f"Facts:\n{facts}"
    )
    return (await _ask(prompt, Reason))["reason"]
