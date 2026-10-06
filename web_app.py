"""
web_app.py

Web entry point: the same bot as main.py, reachable from a browser.
Run: python web_app.py   then open http://127.0.0.1:5000

How the pieces fit:
  - The chat page (web/index.html) is only the "front door". It sends each
    customer message to this small web service and shows the reply.
  - This file checks the request and hands it to flow_engine.handle_message,
    exactly like the terminal version does. All bot logic stays where it was.
  - A WhatsApp or Teams connection would be another front door calling the
    same /api/chat endpoint.

This is a local demo server. It listens on 127.0.0.1 only and has no login
and no rate limit, so do not expose it to the internet as it is: every
message costs model tokens.
"""
from __future__ import annotations

import os
import sys
import uuid
from collections import OrderedDict

from flask import Flask, jsonify, request, send_from_directory

import config
from exceptions import SupportBotError, TracingSetupError
from flow.flow_engine import handle_message
from observability.langsmith_setup import ensure_langsmith_ready
from observability.logger import init_db

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_MESSAGE_LENGTH = 1000   # characters; longer input is refused, not cut
MAX_SESSIONS = 1000         # oldest sessions are dropped beyond this

app = Flask(__name__, static_folder=None)

# session_id -> {"market": ..., "supplier": ...}
# The market and supplier are fixed when a chat starts and kept here, so a
# browser cannot switch supplier in the middle of a conversation. In memory
# only, like the AI-disclosure state: restarting the server ends all chats.
_SESSIONS: "OrderedDict[str, dict]" = OrderedDict()


def _error(status: int, code: str, message: str):
    return jsonify({"error": code, "message": message}), status


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/config")
def api_config():
    """What the chat page needs to build its market and supplier choices."""
    return jsonify(
        {
            "markets": config.SUPPORTED_MARKETS,
            "default_market": config.DEFAULT_MARKET,
            "suppliers": [{"id": key, "name": name} for key, name in config.SUPPLIER_NAMES.items()],
            "default_supplier": config.DEFAULT_SUPPLIER,
            "max_message_length": MAX_MESSAGE_LENGTH,
        }
    )


@app.post("/api/sessions")
def api_start_session():
    """Starts a chat for one market and supplier and returns its session id."""
    data = request.get_json(silent=True) or {}
    market = str(data.get("market", "")).lower()
    supplier = str(data.get("supplier", "")).lower()
    if market not in config.SUPPORTED_MARKETS:
        return _error(400, "unknown_market", f"Unknown market. Use one of: {config.SUPPORTED_MARKETS}")
    if supplier not in config.SUPPLIER_NAMES:
        return _error(400, "unknown_supplier", f"Unknown supplier. Use one of: {config.SUPPORTED_SUPPLIERS}")

    session_id = str(uuid.uuid4())
    _SESSIONS[session_id] = {"market": market, "supplier": supplier}
    while len(_SESSIONS) > MAX_SESSIONS:
        _SESSIONS.popitem(last=False)
    return jsonify({"session_id": session_id, "market": market, "supplier": supplier})


@app.post("/api/chat")
def api_chat():
    """One conversation turn: a customer message in, the bot's reply out."""
    data = request.get_json(silent=True) or {}
    session = _SESSIONS.get(str(data.get("session_id", "")))
    if session is None:
        return _error(404, "unknown_session", "This chat has ended. Start a new chat.")

    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error(400, "empty_message", "Write a message before sending.")
    message = message.strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        return _error(400, "message_too_long", f"Messages can be up to {MAX_MESSAGE_LENGTH} characters.")

    try:
        result = handle_message(
            session_id=data["session_id"],
            market=session["market"],
            supplier=session["supplier"],
            user_text=message,
        )
    except SupportBotError as e:
        # handle_message deals with its own failures and should not get here.
        # If it does, the customer gets a plain message, never a traceback.
        app.logger.error("Conversation turn failed: %s", e)
        return _error(500, "bot_unavailable", "The assistant is not available right now. Try again in a moment.")

    return jsonify(
        {
            "reply": result.text,
            "intent": result.intent,
            "escalated": result.escalated,
            "kb_gap": result.kb_gap,
        }
    )


def main() -> None:
    # Same startup checks as the terminal version: tracing is mandatory.
    try:
        ensure_langsmith_ready()
    except TracingSetupError as e:
        print(f"❌ Startup aborted: {e}")
        sys.exit(1)
    init_db()

    print(f"Chat page: http://127.0.0.1:{config.WEB_PORT}   (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=config.WEB_PORT, debug=False)


if __name__ == "__main__":
    main()
