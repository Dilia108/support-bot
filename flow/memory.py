"""
flow/memory.py

Responsibility: remember the last few turns of each conversation, so a
follow-up question ("And if I prepaid?") can be understood in context, and
count unanswered questions in a row, so a customer the bot cannot help is
handed to a person instead of hearing "I don't know" again and again.

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


# Unanswered questions in a row, per session. Kept apart from the turns
# because it must survive beyond MAX_TURNS.
_GAP_STREAK: "OrderedDict[str, int]" = OrderedDict()


def count_gap(session_id: str, kb_gap: bool) -> int:
    """
    Records whether this turn's question had no answer in the knowledge base
    and returns how many unanswered questions came in a row (0 after an
    answered one).
    """
    streak = _GAP_STREAK.get(session_id, 0) + 1 if kb_gap else 0
    _GAP_STREAK[session_id] = streak
    _GAP_STREAK.move_to_end(session_id)
    while len(_GAP_STREAK) > MAX_SESSIONS:
        _GAP_STREAK.popitem(last=False)
    return streak


def reset_gaps(session_id: str) -> None:
    """After a handover the count starts again."""
    _GAP_STREAK.pop(session_id, None)


def format_history(turns: list[Turn]) -> str:
    """Plain-text transcript for a prompt, e.g. 'Customer: ...' / 'Assistant: ...'."""
    lines = []
    for turn in turns:
        lines.append(f"Customer: {turn.user}")
        lines.append(f"Assistant: {turn.bot}")
    return "\n".join(lines)
