"""Tiny in-memory per-IP rate limit for the endpoints that call Gemini (protects the free quota)."""
import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

LIMIT = int(os.getenv("AI_RATE_LIMIT_PER_MINUTE", "10"))
WINDOW = 60.0
_hits: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # Render (and most hosts) put the visitor's IP first in X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ai_rate_limit(request: Request) -> None:
    """FastAPI dependency: allow LIMIT AI requests per IP per minute."""
    now = time.monotonic()
    hits = _hits[client_ip(request)]
    while hits and now - hits[0] > WINDOW:
        hits.popleft()
    if len(hits) >= LIMIT:
        wait = int(WINDOW - (now - hits[0])) + 1
        raise HTTPException(429, f"Easy there! The AI is limited to {LIMIT} requests a minute on this demo. "
                                 f"Please try again in {wait} seconds.")
    hits.append(now)
