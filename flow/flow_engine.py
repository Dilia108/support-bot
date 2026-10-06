"""
flow/flow_engine.py

Responsibility: orchestrate one full turn of conversation, end to end.

This is the file a reviewer should read first -- it reads top to bottom
as a straight line through the 6 stages, and every stage is wrapped in
its own try/except with an explicit, documented fallback. Nothing here
should ever raise an unhandled exception back to main.py.

Stage order:
  1. Classify intent            (never raises -- worst case UNKNOWN)
  2. Compliance check           (fail CLOSED -> escalate on error)
  3. Escalate early if required (skip KB + LLM entirely, cheaper + safer)
  4. Retrieve KB context        (from the customer's supplier only;
                                 KBGapError / KBLoadError -> generic fallback)
  5. Call the LLM               (llm_client already retries + falls back)
  6. Apply AI disclosure + log the full turn for analytics
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import config
from compliance.rules import check_compliance
from exceptions import ComplianceCheckError, KBGapError, KBLoadError
from flow.intents import classify_intent, Intent
from knowledge.kb_loader import load_kb
from knowledge.retriever import retrieve
from models.disclosure import apply_disclosure
from models.llm_client import get_response
from observability.logger import ConversationLogEntry, log_conversation, log_error
from observability.tracing import trace_call, estimate_cost

# Simple in-process cache so we don't hit disk on every single message.
# A production version would invalidate this on KB file changes; as it is,
# a new dated KB version is picked up the next time the bot is started.
_KB_CACHE: dict[tuple[str, str], list] = {}


@dataclass
class TurnResult:
    text: str
    intent: str
    escalated: bool
    kb_gap: bool


def _get_kb_documents(market: str, supplier: str):
    key = (market, supplier)
    if key not in _KB_CACHE:
        _KB_CACHE[key] = load_kb(market, supplier)
    return _KB_CACHE[key]


def _escalation_response(market: str, reason: str) -> str:
    messages = {
        "de": "Ich verbinde Sie mit einem Mitarbeiter, der Ihnen weiterhelfen kann.",
        "es": "Te voy a poner en contacto con un miembro de nuestro equipo.",
        "us": "I'm connecting you with a team member who can help with this.",
    }
    return messages.get(market, messages["us"])


def handle_message(session_id: str, market: str, supplier: str, user_text: str) -> TurnResult:
    """
    Runs one conversation turn inside a LangSmith trace.

    The trace wraps the WHOLE turn, not just the model call, so turns that
    never reach a model (compliance escalations) are visible in LangSmith
    too. The model call in stage 5 shows up as a step inside this trace.
    """
    with trace_call(
        "conversation_turn",
        market=market,
        inputs={"input": user_text},
        session_id=session_id,
        supplier=supplier,
    ) as turn:
        result = _run_turn(session_id, market, supplier, user_text)
        tags = [f"supplier:{supplier}"]
        tags += [tag for tag, on in (("escalated", result.escalated), ("kb_gap", result.kb_gap)) if on]
        turn.record_result(
            result.text,
            tags=tags,
            intent=result.intent,
            escalated=result.escalated,
            kb_gap=result.kb_gap,
        )
    return result


def _run_turn(session_id: str, market: str, supplier: str, user_text: str) -> TurnResult:
    start_time = time.time()
    kb_gap = False
    escalated = False
    intent_result = classify_intent(user_text)  # never raises

    # --- Stage 2 & 3: compliance check, fail CLOSED ------------------------
    try:
        compliance_result = check_compliance(market, intent_result.intent, user_text)
    except ComplianceCheckError as e:
        log_error("compliance", str(e), market=market, session_id=session_id)
        compliance_result = None  # sentinel: treat as escalate below
        escalated = True
        reason = f"compliance check failed, escalating for safety: {e}"
    else:
        escalated = compliance_result.escalate
        reason = compliance_result.reason

    if escalated:
        bot_text = _escalation_response(market, reason or "compliance escalation")
        final_text = apply_disclosure(session_id, market, bot_text)
        _log_turn(
            session_id, market, supplier, user_text, final_text, intent_result,
            kb_gap=False, escalated=True, provider_used=None, model_used=None,
            tokens_in=0, tokens_out=0, latency=time.time() - start_time, error=reason,
        )
        return TurnResult(text=final_text, intent=intent_result.intent.value, escalated=True, kb_gap=False)

    # --- Stage 4: retrieve KB context ---------------------------------------
    context_text = ""
    kb_version = ""  # which dated KB file answered; recorded on the trace
    try:
        documents = _get_kb_documents(market, supplier)
        kb_version = documents[0].version
        results = retrieve(user_text, documents, min_score=config.KB_MATCH_MIN_SCORE)
        context_text = "\n\n".join(f"{r.document.heading}: {r.document.content}" for r in results)
    except KBLoadError as e:
        log_error("kb_load", str(e), market=market, session_id=session_id)
        context_text = ""  # no KB available -- LLM will answer generically, flagged below
    except KBGapError as e:
        kb_gap = True
        log_error("kb_gap", str(e), market=market, session_id=session_id)
        context_text = ""  # signal recorded for analytics; conversation still continues

    # --- Stage 5: call the LLM (already retries + falls back internally) ---
    prompt = _build_prompt(market, supplier, intent_result.intent, user_text, context_text, kb_gap)
    # `inputs` and record_llm_result() are what make this step readable in
    # LangSmith: without them it only shows a name and a duration.
    with trace_call(
        "llm_call",
        market=market,
        run_type="llm",
        inputs={"messages": [{"role": "user", "content": prompt}]},
        intent=intent_result.intent.value,
        session_id=session_id,
        supplier=supplier,
        kb_version=kb_version,
        user_message=user_text,
        kb_gap=kb_gap,
    ) as trace:
        llm_response = get_response(prompt)
        trace.record_llm_result(
            text=llm_response.text,
            provider=llm_response.provider_used,
            model=llm_response.model_used,
            tokens_in=llm_response.tokens_in,
            tokens_out=llm_response.tokens_out,
            error=llm_response.error,
        )

    if llm_response.escalate:
        escalated = True

    # --- Stage 6: disclosure + logging --------------------------------------
    final_text = apply_disclosure(session_id, market, llm_response.text)
    cost = estimate_cost(llm_response.model_used or "mock-model", llm_response.tokens_in, llm_response.tokens_out)

    _log_turn(
        session_id, market, supplier, user_text, final_text, intent_result,
        kb_gap=kb_gap, escalated=escalated,
        provider_used=llm_response.provider_used, model_used=llm_response.model_used,
        tokens_in=llm_response.tokens_in, tokens_out=llm_response.tokens_out,
        latency=llm_response.latency_seconds, error=llm_response.error, cost=cost,
    )

    return TurnResult(text=final_text, intent=intent_result.intent.value, escalated=escalated, kb_gap=kb_gap)


# Rules sent to the model with every question. Each one states its reason,
# because a model follows a rule more reliably when it knows what it is for.
_REPLY_RULES = (
    "You are a customer support assistant for vehicle rental bookings. Each "
    "customer has a booking with one rental supplier, named below, and the "
    "knowledge base context is that supplier's own policy. "
    "Follow these rules when you write your reply:\n"
    "1. Write plain text only. The reply is shown in a chat window that does "
    "not render formatting, so Markdown would appear as stray symbols: do not "
    "use headings (#), bold or italics (* or _), tables, or emojis. Use short "
    "sentences; if a list really helps, start each line with a hyphen.\n"
    "2. Keep every time limit, amount and condition exactly as the knowledge "
    "base states it, because a customer may act on your wording. Do not "
    "rephrase them in a way that could change the meaning: a deadline that "
    "falls BEFORE an event must not be described as AFTER it, and a fee must "
    "keep its amount and currency.\n"
    "3. Policies differ between suppliers, so answer only for the supplier "
    "named below and never use what you know about other suppliers. If the "
    "context gives different rules for different rate types (for example "
    "prepaid and pay-on-arrival), give both, because you do not know which "
    "one the customer booked.\n"
    "4. When the context says something is not stated or not published, say "
    "exactly that. Do not turn 'not stated' into 'does not exist', and do "
    "not work out a rule for the customer's case from an example: if the "
    "context only gives an example for one hour, do not claim what happens "
    "after 30 minutes."
)

# How to address the customer, per market. Without this the model switches
# between formal and informal address from one reply to the next.
_FORM_OF_ADDRESS = {
    "de": 'Address the customer with the formal German "Sie" in every reply, never "du".',
    "es": 'Address the customer with the informal Spanish "tú" in every reply, as the knowledge base does.',
}


def _build_prompt(market: str, supplier: str, intent: Intent, user_text: str, context_text: str, kb_gap: bool) -> str:
    supplier_name = config.SUPPLIER_NAMES.get(supplier, supplier)
    address = _FORM_OF_ADDRESS.get(market, "")
    address = f"\n5. {address}" if address else ""
    if kb_gap or not context_text:
        grounding = (
            "No specific knowledge base entry was found for this question. "
            "Be honest that you don't have specific information on this, "
            "and offer to connect the user with a team member."
        )
    else:
        grounding = f"Use ONLY the following knowledge base context to answer:\n{context_text}"

    # The rules come first and the context + user message stay at the end:
    # MockProvider echoes the tail of the prompt, so this keeps mock replies
    # showing the retrieved context.
    return (
        f"{_REPLY_RULES}{address}\n\n"
        f"Market: {market}\nSupplier: {supplier_name}\nDetected intent: {intent.value}\n{grounding}\n\n"
        f"User message: {user_text}\n\nRespond helpfully and concisely."
    )


def _log_turn(session_id, market, supplier, user_text, bot_text, intent_result, *, kb_gap, escalated,
              provider_used, model_used, tokens_in, tokens_out, latency, error, cost: float = 0.0):
    entry = ConversationLogEntry(
        timestamp=time.time(),
        session_id=session_id,
        market=market,
        supplier=supplier,
        user_text=user_text,
        bot_text=bot_text,
        intent=intent_result.intent.value,
        intent_confidence=intent_result.confidence,
        kb_gap=kb_gap,
        escalated=escalated,
        provider_used=provider_used,
        model_used=model_used,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        estimated_cost_usd=cost,
        latency_seconds=latency,
        error=error,
    )
    log_conversation(entry)
