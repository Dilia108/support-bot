"""
models/disclosure.py

Responsibility: guarantee the bot identifies itself as an AI.

Important design choice: this is enforced in CODE, not left to the LLM's
good behavior in a system prompt. A prompt can be ignored under certain
inputs; a function that runs on every outgoing message cannot. This also
gives you a clean compliance answer: "disclosure is structurally
guaranteed, here's the function," rather than "we asked the model nicely."
"""
from __future__ import annotations

from dataclasses import dataclass

import config


@dataclass
class SessionState:
    session_id: str
    market: str
    has_disclosed: bool = False


# In-memory session store for this demo. Swap for Redis/DB in production
# so disclosure state survives a restart / scales across workers.
_SESSIONS: dict[str, SessionState] = {}


def get_or_create_session(session_id: str, market: str) -> SessionState:
    if session_id not in _SESSIONS:
        _SESSIONS[session_id] = SessionState(session_id=session_id, market=market)
    return _SESSIONS[session_id]


def apply_disclosure(session_id: str, market: str, text: str) -> str:
    """
    Prepends the AI disclosure notice on the first message of a session.
    On every later message, appends a very short persistent tag instead --
    visible but not intrusive.
    """
    session = get_or_create_session(session_id, market)
    disclosure_text = config.AI_DISCLOSURE_TEXT.get(market, config.AI_DISCLOSURE_TEXT["us"])

    if not session.has_disclosed:
        session.has_disclosed = True
        return f"{disclosure_text}\n\n{text}"

    return f"{text}\n\n[AI]"
