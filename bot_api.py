"""
bot_api.py

Channel API: the entry point for chat platforms such as Botpress or
Voiceflow, which run in the cloud and call this bot over the internet.
Run: python bot_api.py   (needs SUPPORT_BOT_API_KEY in .env)

Why a separate entry point instead of exposing web_app.py:
  - web_app.py is a local demo with no login. Anything reachable from the
    internet must not be open like that, because every message costs model
    tokens. This API refuses every request without the right key.
  - It runs on its own port (5001), so making it public (for example with a
    tunnel) exposes only these endpoints, not the demo page.
  - It reuses the web app's request handlers, so validation and behaviour
    are identical: one bot, several front doors.

Endpoints (all JSON; every request needs the header  X-API-Key: <key>):
  GET  /v1/config     markets and suppliers the bot supports
  POST /v1/sessions   {"market": "de", "supplier": "sixt"}  -> {"session_id": ...}
  POST /v1/chat       {"session_id": ..., "message": "..."}  ->
                      {"reply", "intent", "escalated", "kb_gap", "understood_as"}
"""
from __future__ import annotations

import hmac
import sys
import threading
import time
from collections import deque

from flask import Flask, jsonify, request

import config
import web_app
from exceptions import TracingSetupError
from observability.langsmith_setup import ensure_langsmith_ready
from observability.logger import init_db

MIN_KEY_LENGTH = 20  # a short key is guessable; refuse to start with one

app = Flask(__name__)
app.add_url_rule("/v1/config", view_func=web_app.api_config, methods=["GET"])
app.add_url_rule("/v1/sessions", view_func=web_app.api_start_session, methods=["POST"])
app.add_url_rule("/v1/chat", view_func=web_app.api_chat, methods=["POST"])

# A simple overall request budget. It is not a full rate limiter, but it
# stops a misconfigured flow (for example a loop that calls the API again
# and again) from spending tokens without limit.
_recent_requests: deque = deque()
_lock = threading.Lock()


def _within_budget() -> bool:
    now = time.monotonic()
    with _lock:
        while _recent_requests and now - _recent_requests[0] > 60:
            _recent_requests.popleft()
        if len(_recent_requests) >= config.BOT_API_MAX_REQUESTS_PER_MINUTE:
            return False
        _recent_requests.append(now)
        return True


@app.before_request
def _check_access():
    sent = request.headers.get("X-API-Key", "")
    # compare_digest takes the same time whatever the input, so the key
    # cannot be guessed character by character from response times.
    if not config.BOT_API_KEY or not hmac.compare_digest(sent.encode(), config.BOT_API_KEY.encode()):
        return jsonify({"error": "unauthorized", "message": "Missing or wrong X-API-Key header."}), 401
    if not _within_budget():
        return jsonify({"error": "too_many_requests", "message": "Too many requests. Wait a minute and try again."}), 429
    return None


@app.errorhandler(404)
def _not_found(_e):
    return jsonify({"error": "not_found", "message": "Unknown endpoint. See the docstring of bot_api.py."}), 404


def main() -> None:
    if len(config.BOT_API_KEY) < MIN_KEY_LENGTH:
        print(
            "❌ Startup aborted: set SUPPORT_BOT_API_KEY in .env to a random value of at least "
            f"{MIN_KEY_LENGTH} characters. Generate one with:\n"
            '   python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
        sys.exit(1)
    try:
        ensure_langsmith_ready()
    except TracingSetupError as e:
        print(f"❌ Startup aborted: {e}")
        sys.exit(1)
    init_db()
    from knowledge import retrieval
    print(f"Knowledge search: {retrieval.warm_up()}")

    print(f"Channel API: http://127.0.0.1:{config.BOT_API_PORT}/v1   (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=config.BOT_API_PORT, debug=False)


if __name__ == "__main__":
    main()
