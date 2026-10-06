"""
main.py

CLI entry point for local development and demos.
Run: python main.py
"""
from __future__ import annotations

import sys
import uuid

import config
from exceptions import TracingSetupError
from flow.flow_engine import handle_message
from observability.langsmith_setup import ensure_langsmith_ready
from observability.logger import init_db


def choose_market() -> str:
    print("Available markets:", ", ".join(config.SUPPORTED_MARKETS))
    while True:
        choice = input(f"Choose a market [{config.DEFAULT_MARKET}]: ").strip().lower()
        if not choice:
            return config.DEFAULT_MARKET
        if choice in config.SUPPORTED_MARKETS:
            return choice
        print(f"Unknown market '{choice}', try one of: {config.SUPPORTED_MARKETS}")


def choose_supplier() -> str:
    names = config.SUPPLIER_NAMES
    print("Available suppliers:", ", ".join(config.SUPPORTED_SUPPLIERS))
    while True:
        choice = input(f"Which supplier is the booking with? [{config.DEFAULT_SUPPLIER}]: ").strip().lower()
        if not choice:
            return config.DEFAULT_SUPPLIER
        if choice in names:
            return choice
        print(f"Unknown supplier '{choice}', try one of: {config.SUPPORTED_SUPPLIERS}")


def main() -> None:
    # Tracing is mandatory: stop here with a clear message rather than
    # running conversations that never show up in LangSmith.
    try:
        ensure_langsmith_ready()
    except TracingSetupError as e:
        print(f"❌ Startup aborted: {e}")
        sys.exit(1)

    init_db()
    market = choose_market()
    # Answers depend on the supplier as well as the market. A real system
    # would read the supplier from the booking number instead of asking.
    supplier = choose_supplier()
    session_id = str(uuid.uuid4())
    print(
        f"\nSession started ({market}, {config.SUPPLIER_NAMES[supplier]}). "
        "Type 'exit' to quit, 'report' for analytics.\n"
    )

    while True:
        try:
            user_text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if user_text.lower() in {"exit", "quit"}:
            print("Goodbye.")
            break

        if user_text.lower() == "report":
            from analytics.dashboard import print_report
            print_report()
            continue

        if not user_text:
            continue

        result = handle_message(session_id=session_id, market=market, supplier=supplier, user_text=user_text)
        print(f"Bot: {result.text}\n")
        if result.escalated:
            print("  [flagged for human follow-up]")
        if result.kb_gap:
            print("  [knowledge base gap recorded]")


if __name__ == "__main__":
    main()
