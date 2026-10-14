"""
flow/memory.py

Responsibility: remember the last few turns of each conversation, so a
follow-up question ("And if I prepaid?") can be understood in context.

Kept small on purpose:
  - Only the last MAX_TURNS turns per session are kept. Older turns rarely
    help with a follow-up, and every turn kept is sent to the model again,
    which costs tokens.
  - In memory only, like the AI-disclosure state in models/disclosure.py:
    restarting the bot forgets all conversations. A production version
    would keep this in Redis or a database, keyed by session id.
  - Sessions beyond MAX_SESSIONS are dropped oldest-first, so a long-running
    server cannot grow without limit.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass

MAX_TURNS = 3
MAX_SESSIONS = 1000


@dataclass(frozen=True)
class Turn:
    user: str
    bot: str  # the bot's reply WITHOUT the AI-disclosure notice


_HISTORY: "OrderedDict[str, deque[Turn]]" = OrderedDict()


def recent_turns(session_id: str) -> list[Turn]:
    """The last turns of this session, oldest first. Empty for a new chat."""
    return list(_HISTORY.get(session_id, ()))


def remember(session_id: str, user: str, bot: str) -> None:
    turns = _HISTORY.get(session_id)
    if turns is None:
        turns = deque(maxlen=MAX_TURNS)
        _HISTORY[session_id] = turns
    _HISTORY.move_to_end(session_id)
    turns.append(Turn(user=user, bot=bot))
    while len(_HISTORY) > MAX_SESSIONS:
        _HISTORY.popitem(last=False)


def format_history(turns: list[Turn]) -> str:
    """Plain-text transcript for a prompt, e.g. 'Customer: ...' / 'Assistant: ...'."""
    lines = []
    for turn in turns:
        lines.append(f"Customer: {turn.user}")
        lines.append(f"Assistant: {turn.bot}")
    return "\n".join(lines)
