"""
flow/flow_engine.py

Responsibility: orchestrate one full turn of conversation, end to end.

This is the file a reviewer should read first -- it reads top to bottom
as a straight line through the 6 stages, and every stage is wrapped in
its own try/except with an explicit, documented fallback. Nothing here
should ever raise an unhandled exception back to main.py.

Stage order:
  1. Classify intent            (never raises -- worst case UNKNOWN)
  2. Compliance check           (on the customer's own words; fail CLOSED
                                 -> escalate on error)
  3. Escalate early if required (skip rewrite, KB + LLM entirely)
     otherwise understand the message in context (a follow-up is
                                 rewritten into a self-contained question
                                 using the last turns; on any failure the
                                 message is used as it is)
  4. Retrieve KB context        (from the customer's supplier only;
                                 KBGapError / KBLoadError -> generic fallback)
  5. Call the LLM               (llm_client already retries + falls back)
  6. Apply AI disclosure + log the full turn for analytics
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

import config
from compliance.rules import check_compliance
from exceptions import ComplianceCheckError, KBGapError, KBLoadError
from flow import memory
from flow.intents import classify_intent, Intent
from knowledge.kb_loader import load_kb
from knowledge import retrieval
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
    # The customer's message as the bot understood it. For a follow-up like
    # "And if I prepaid?" this is the rewritten, self-contained question.
    understood_as: str = ""
    # How the knowledge base was searched, shown on the turn's LangSmith
    # trace: mode, KB version, scores, and for a gap the reason (best score
    # vs. threshold). Empty for escalated turns, which skip the search.
    retrieval: dict = field(default_factory=dict)


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


def _gap_handover_response(market: str, supplier: str) -> str:
    name = config.SUPPLIER_NAMES.get(supplier, supplier)
    messages = {
        "de": f"Dazu habe ich leider auch keine Informationen zu {name}. Ich verbinde Sie mit einem Mitarbeiter, der Ihnen weiterhelfen kann.",
        "es": f"Tampoco tengo información sobre esto para {name}. Te voy a poner en contacto con un miembro de nuestro equipo.",
        "us": f"I don't have information on that for {name} either. I'm connecting you with a team member who can help.",
    }
    return messages.get(market, messages["us"])


def handle_message(session_id: str, market: str, supplier: str, user_text: str) -> TurnResult:
    """
    Runs one conversation turn inside a LangSmith trace.

    The trace wraps the WHOLE turn, not just the model call, so turns that
    never reach a model (compliance escalations) are visible in LangSmith
    too. The model call in stage 5 shows up as a step inside this trace.
    """
    # The trace id is chosen here so the conversation log can store it and
    # the dashboard can link each unanswered question to its trace.
    trace_id = str(uuid.uuid4())
    with trace_call(
        "conversation_turn",
        market=market,
        inputs={"input": user_text},
        run_id=trace_id,
        session_id=session_id,
        supplier=supplier,
    ) as turn:
        result = _run_turn(session_id, market, supplier, user_text, trace_id=trace_id)
        tags = [f"supplier:{supplier}"]
        tags += [tag for tag, on in (("escalated", result.escalated), ("kb_gap", result.kb_gap)) if on]
        turn.record_result(
            result.text,
            tags=tags,
            intent=result.intent,
            escalated=result.escalated,
            kb_gap=result.kb_gap,
            understood_as=result.understood_as,
            **result.retrieval,
        )
    return result


def _run_turn(session_id: str, market: str, supplier: str, user_text: str, trace_id: str = None) -> TurnResult:
    start_time = time.time()
    kb_gap = False
    escalated = False
    history = memory.recent_turns(session_id)

    # The compliance check always judges what the customer actually wrote,
    # so it runs on the customer's own words and their own intent.
    own_intent = classify_intent(user_text)  # never raises
    intent_result = own_intent
    question, rewrite_response = user_text, None

    # --- Stage 2 & 3: compliance check, fail CLOSED ------------------------
    try:
        compliance_result = check_compliance(market, own_intent.intent, user_text)
    except ComplianceCheckError as e:
        log_error("compliance", str(e), market=market, session_id=session_id)
        compliance_result = None  # sentinel: treat as escalate below
        escalated = True
        reason = f"compliance check failed, escalating for safety: {e}"
    else:
        escalated = compliance_result.escalate
        reason = compliance_result.reason

    # --- Understand the message in context (turns that are not escalated) --
    # A follow-up such as "And if I prepaid?" has no topic of its own, so it
    # is rewritten into a self-contained question before intent detection
    # and retrieval. Skipped for escalated turns: they need no answer, and a
    # rewrite would only blend the escalation into the previous topic.
    # Never raises: if rewriting fails, the message is used as it is. Its
    # cost is added to the turn's cost below.
    if not escalated:
        question, rewrite_response = _standalone_question(session_id, market, supplier, user_text, history)
        if question != user_text:
            intent_result = classify_intent(question)

    if escalated:
        bot_text = _escalation_response(market, reason or "compliance escalation")
        final_text = apply_disclosure(session_id, market, bot_text)
        memory.remember(session_id, user_text, bot_text)
        memory.reset_gaps(session_id)
        _log_turn(
            session_id, market, supplier, user_text, final_text, intent_result,
            trace_id=trace_id, kb_gap=False, escalated=True,
            provider_used=_provider(rewrite_response), model_used=_model(rewrite_response),
            tokens_in=_tokens_in(rewrite_response), tokens_out=_tokens_out(rewrite_response),
            latency=time.time() - start_time, error=reason, cost=_cost(rewrite_response),
        )
        return TurnResult(text=final_text, intent=intent_result.intent.value, escalated=True,
                          kb_gap=False, understood_as=question)

    # --- Stage 4: retrieve KB context ---------------------------------------
    context_text = ""
    kb_version = ""  # which dated KB file answered; recorded on the trace
    kb_scores: list = []  # similarity of the entries handed to the model
    kb_gap_reason = ""
    kb_matched: list = []  # headings of the entries handed to the model
    try:
        documents = _get_kb_documents(market, supplier)
        kb_version = documents[0].version
        results = retrieval.retrieve(question, documents)
        kb_scores = [r.score for r in results]
        kb_matched = [r.document.heading for r in results]
        context_text = "\n\n".join(f"{r.document.heading}: {r.document.content}" for r in results)
    except KBLoadError as e:
        log_error("kb_load", str(e), market=market, session_id=session_id)
        context_text = ""  # no KB available -- LLM will answer generically, flagged below
    except KBGapError as e:
        kb_gap = True
        kb_gap_reason = str(e)
        log_error("kb_gap", str(e), market=market, session_id=session_id)
        context_text = ""  # signal recorded for analytics; conversation still continues

    retrieval_info = {
        "retriever": retrieval.active_retriever(),
        "kb_version": kb_version,
        "kb_scores": kb_scores,
        "kb_matched": kb_matched,
    }
    if kb_gap:
        retrieval_info["kb_gap_reason"] = kb_gap_reason

    # --- Repeated gaps: hand over instead of another "I don't know" --------
    # The gap is still logged (kb_gap=True) so the dashboard counts it. No
    # model call is needed for a fixed handover message.
    if memory.count_gap(session_id, kb_gap) >= config.MAX_CONSECUTIVE_GAPS:
        memory.reset_gaps(session_id)
        reason = f"{config.MAX_CONSECUTIVE_GAPS} questions in a row without a knowledge base answer"
        bot_text = _gap_handover_response(market, supplier)
        final_text = apply_disclosure(session_id, market, bot_text)
        memory.remember(session_id, user_text, bot_text)
        _log_turn(
            session_id, market, supplier, user_text, final_text, intent_result,
            trace_id=trace_id, kb_gap=True, escalated=True,
            provider_used=_provider(rewrite_response), model_used=_model(rewrite_response),
            tokens_in=_tokens_in(rewrite_response), tokens_out=_tokens_out(rewrite_response),
            latency=time.time() - start_time, error=reason, cost=_cost(rewrite_response),
        )
        return TurnResult(text=final_text, intent=intent_result.intent.value, escalated=True,
                          kb_gap=True, understood_as=question, retrieval=retrieval_info)

    # --- Stage 5: call the LLM (already retries + falls back internally) ---
    prompt = _build_prompt(market, supplier, intent_result.intent, user_text, question, history, context_text, kb_gap)
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
        retriever=retrieval.active_retriever(),
        kb_scores=kb_scores,
        kb_gap_reason=kb_gap_reason,
        user_message=user_text,
        understood_as=question,
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
    memory.remember(session_id, user_text, llm_response.text)
    # Cost and tokens cover both model calls of the turn: the rewrite (if
    # there was one) and the answer.
    cost = _cost(llm_response) + _cost(rewrite_response)

    _log_turn(
        session_id, market, supplier, user_text, final_text, intent_result,
        trace_id=trace_id, kb_gap=kb_gap, escalated=escalated,
        provider_used=llm_response.provider_used, model_used=llm_response.model_used,
        tokens_in=llm_response.tokens_in + _tokens_in(rewrite_response),
        tokens_out=llm_response.tokens_out + _tokens_out(rewrite_response),
        latency=time.time() - start_time, error=llm_response.error, cost=cost,
    )

    return TurnResult(text=final_text, intent=intent_result.intent.value, escalated=escalated,
                      kb_gap=kb_gap, understood_as=question, retrieval=retrieval_info)


# --- Follow-up questions ------------------------------------------------------
_REWRITE_INSTRUCTIONS = (
    "You rewrite a customer's latest chat message into one self-contained "
    "question, so it can be understood without the conversation before it.\n"
    "Most messages already name their own subject, for example a pet, an "
    "accident, a payment or a change to the booking. Those start a new topic: "
    "return them exactly as written and add nothing from earlier messages, "
    "because mixing two topics changes what the customer asked.\n"
    "Only a message that cannot be understood on its own, because it leaves "
    "out its subject, takes the subject from the conversation. Examples:\n"
    "- After a question about cancelling: 'And if I prepaid?' becomes "
    "'How do I cancel my booking if I prepaid?'\n"
    "- After a question about a late return: 'Und bei zwei Stunden?' becomes "
    "'Was passiert, wenn ich das Auto zwei Stunden zu spät zurückgebe?'\n"
    "- After a question about cancelling: '¿Puedo llevar a mi perro?' stays "
    "'¿Puedo llevar a mi perro?'\n"
    "Keep the customer's language. Do not answer the question. Return only "
    "the question, on one line, without quotes."
)
_MAX_REWRITE_LENGTH = 300  # characters; anything longer is not a question


def _standalone_question(session_id, market, supplier, user_text, history):
    """
    Returns (question, rewrite_response). rewrite_response is the model
    response used for the rewrite, or None when no rewrite was attempted.

    No history means a first message: nothing to resolve, no model call.
    When the rewrite fails or returns something that is not a usable
    question, the original message is used, so a broken rewrite can only
    make the bot as good as it was without memory, never worse.
    """
    if not history:
        return user_text, None

    prompt = (
        f"{_REWRITE_INSTRUCTIONS}\n\n"
        f"Conversation:\n{memory.format_history(history)}\n\n"
        f"Latest message: {user_text}\n\nSelf-contained question:"
    )
    with trace_call(
        "rewrite_question",
        market=market,
        run_type="llm",
        inputs={"messages": [{"role": "user", "content": prompt}]},
        session_id=session_id,
        supplier=supplier,
    ) as trace:
        response = get_response(prompt)
        trace.record_llm_result(
            text=response.text,
            provider=response.provider_used,
            model=response.model_used,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            error=response.error,
        )

    # MockProvider only echoes its prompt, so it cannot rewrite anything.
    if response.escalate or response.provider_used in (None, "mock"):
        return user_text, response
    question = response.text.strip().strip('"\'„“”«»').strip()
    if not question or "\n" in question or len(question) > _MAX_REWRITE_LENGTH:
        log_error("rewrite", f"unusable rewrite ignored: {response.text[:80]!r}", market=market, session_id=session_id)
        return user_text, response
    return question, response


def _cost(r) -> float:
    return estimate_cost(r.model_used or "mock-model", r.tokens_in, r.tokens_out) if r else 0.0


def _tokens_in(r) -> int:
    return r.tokens_in if r else 0


def _tokens_out(r) -> int:
    return r.tokens_out if r else 0


def _provider(r):
    return r.provider_used if r else None


def _model(r):
    return r.model_used if r else None


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
    "after 30 minutes.\n"
    "5. You can only answer questions. You cannot transfer this chat, contact "
    "a person, or look up or change a booking, so never offer or promise any "
    "of these, and do not end with a question that asks whether you should. "
    "A customer who reads 'I will connect you' waits for someone who never "
    "comes."
)

# How to address the customer, per market. Without this the model switches
# between formal and informal address from one reply to the next.
_FORM_OF_ADDRESS = {
    "de": 'Address the customer with the formal German "Sie" in every reply, never "du".',
    "es": 'Address the customer with the informal Spanish "tú" in every reply, as the knowledge base does.',
}


def _build_prompt(market: str, supplier: str, intent: Intent, user_text: str, question: str,
                  history: list, context_text: str, kb_gap: bool) -> str:
    supplier_name = config.SUPPLIER_NAMES.get(supplier, supplier)
    address = _FORM_OF_ADDRESS.get(market, "")
    address = f"\n6. {address}" if address else ""
    if kb_gap or not context_text:
        grounding = (
            "No knowledge base entry was found for this question. Say plainly "
            f"that you do not have this information for {supplier_name}, and do "
            f"not guess the answer. Tell the customer to contact {supplier_name} "
            "customer service directly and to have their booking number ready."
        )
    else:
        grounding = f"Use ONLY the following knowledge base context to answer:\n{context_text}"

    # Earlier turns let the model resolve "that", "it" or "and if ...". They
    # are context only: the facts must still come from the KB context above.
    conversation = ""
    if history:
        conversation = (
            "Conversation so far (for context only; take facts only from the "
            f"knowledge base context above):\n{memory.format_history(history)}\n\n"
        )
    understood = f"\nUnderstood as: {question}" if question != user_text else ""

    # The rules come first and the context + user message stay at the end:
    # MockProvider echoes the tail of the prompt, so this keeps mock replies
    # showing the retrieved context.
    return (
        f"{_REPLY_RULES}{address}\n\n"
        f"Market: {market}\nSupplier: {supplier_name}\nDetected intent: {intent.value}\n{grounding}\n\n"
        f"{conversation}"
        f"User message: {user_text}{understood}\n\nRespond helpfully and concisely."
    )


def _log_turn(session_id, market, supplier, user_text, bot_text, intent_result, *, trace_id=None, kb_gap, escalated,
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
        trace_id=trace_id,
    )
    log_conversation(entry)
