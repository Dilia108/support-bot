"""
flow/intents.py

Responsibility: figure out what the user is trying to do.

Rule-based/keyword classification on purpose: transparent, debuggable,
and reviewable in a pull request with no model calls involved. This is
the "no-code-friendly" layer of the bot -- a non-engineer can open this
file and add a keyword without touching anything else. The upgrade path
to an LLM-based classifier is a drop-in replacement for `classify_intent`
as long as it keeps returning an IntentResult.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    CHECK_BOOKING = "check_booking"
    CANCEL_BOOKING = "cancel_booking"
    REPORT_ISSUE = "report_issue"
    FAQ = "faq"
    ESCALATE = "escalate"
    UNKNOWN = "unknown"


@dataclass
class IntentResult:
    intent: Intent
    confidence: float


_KEYWORDS: dict[Intent, list[str]] = {
    Intent.CHECK_BOOKING: ["booking", "reservation", "buchung", "reserva", "check my", "status of my"],
    Intent.CANCEL_BOOKING: ["cancel", "stornieren", "cancelar", "refund my booking"],
    Intent.REPORT_ISSUE: ["problem", "issue", "broken", "not working", "fehler", "problema", "kaputt"],
    Intent.ESCALATE: ["speak to a human", "human agent", "representative", "mensch sprechen", "hablar con una persona"],
    Intent.FAQ: ["how do i", "what is", "how much", "wie kann ich", "wie viel", "como puedo", "cuanto cuesta"],
}

MIN_CONFIDENCE = 0.34  # below this we treat the intent as UNKNOWN, not a guess


def classify_intent(text: str) -> IntentResult:
    """
    Never raises. Worst case it returns Intent.UNKNOWN with confidence 0.0,
    which the flow engine routes to a safe clarification response instead
    of guessing what the user wants.
    """
    if not text or not text.strip():
        return IntentResult(intent=Intent.UNKNOWN, confidence=0.0)

    text_lower = text.lower()
    best_intent = Intent.UNKNOWN
    best_score = 0.0

    for intent, keywords in _KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text_lower)
        if hits == 0:
            continue
        score = min(1.0, hits / len(keywords) * 2)  # simple, bounded scoring
        if score > best_score:
            best_score = score
            best_intent = intent

    if best_score < MIN_CONFIDENCE:
        return IntentResult(intent=Intent.UNKNOWN, confidence=best_score)

    return IntentResult(intent=best_intent, confidence=best_score)
