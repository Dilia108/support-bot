"""
compliance/rules.py

Responsibility: decide whether a message must go to a human instead of
getting an AI-generated answer.

Critical design principle: FAIL CLOSED. If this module raises an
unexpected error, the caller (flow_engine.py) must treat that as "escalate
to a human," never as "skip the check and answer anyway." Getting this
backwards is the difference between a minor bug and a compliance incident.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from exceptions import ComplianceCheckError
from flow.intents import Intent


@dataclass
class ComplianceResult:
    escalate: bool
    reason: str | None = None


def check_compliance(market: str, intent: Intent, user_text: str) -> ComplianceResult:
    """
    Returns escalate=True whenever the message touches a regulated or
    high-stakes topic (legal, medical, financial guarantees, accidents),
    regardless of what the knowledge base might otherwise be able to answer.

    Raises ComplianceCheckError only for genuinely unexpected failures
    (e.g. a bad market code); callers must catch this and escalate anyway.
    """
    if market not in config.SUPPORTED_MARKETS:
        # Fail closed: we don't recognize this market's rules, so we can't
        # certify the response is compliant. Escalate rather than guess.
        raise ComplianceCheckError(f"Unsupported market for compliance check: '{market}'")

    text_lower = (user_text or "").lower()

    for keyword in config.ESCALATION_KEYWORDS:
        if keyword in text_lower:
            return ComplianceResult(
                escalate=True,
                reason=f"Message matched restricted topic keyword: '{keyword}'",
            )

    if intent == Intent.ESCALATE:
        return ComplianceResult(escalate=True, reason="User explicitly requested a human agent")

    return ComplianceResult(escalate=False, reason=None)
