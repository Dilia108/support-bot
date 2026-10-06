"""
analytics/dashboard.py

Responsibility: turn the raw conversation log into the numbers that
actually drive decisions -- this is the "Performance Analytics" piece
of the job requirements: fallback/gap rate, escalation rate, and cost
per market and supplier, so you can say *what you'd do next*, not just report a count.

Kept as plain sqlite3 + string formatting so it runs with zero extra
dependencies. Swap the print statements for Plotly/Tableau/Streamlit
once you want a real dashboard.
"""
from __future__ import annotations

import os
import sqlite3
import sys

# Allow running this file directly (`python analytics/dashboard.py`) as well
# as importing it as a package module from main.py -- both need the project
# root on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from observability.logger import init_db

NO_SUPPLIER = "no supplier recorded"


def _fetchall(query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cur = conn.execute(query, params)
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def print_report() -> None:
    # Makes sure the table exists and has the supplier column, also when
    # this file is run on its own against an older database.
    init_db()
    rows = _fetchall(
        "SELECT market, supplier, kb_gap, escalated, estimated_cost_usd, user_text "
        "FROM conversation_log"
    )
    if not rows:
        print("No conversation data yet. Run main.py and chat with the bot first.")
        return

    # One group per market + supplier. Turns logged before suppliers existed
    # have no supplier; they are shown as their own group rather than hidden.
    groups: dict[tuple[str, str], dict] = {}
    gap_questions: list[str] = []
    for market, supplier, kb_gap, escalated, cost, user_text in rows:
        supplier = supplier or NO_SUPPLIER
        g = groups.setdefault((market, supplier), {"turns": 0, "gaps": 0, "escalations": 0, "cost": 0.0})
        g["turns"] += 1
        g["gaps"] += 1 if kb_gap else 0
        g["escalations"] += 1 if escalated else 0
        g["cost"] += cost or 0.0
        if kb_gap:
            gap_questions.append(f"[{market}/{supplier}] {user_text}")

    print("=" * 60)
    print(f"SUPPORT BOT ANALYTICS -- {len(rows)} conversation turns logged")
    print("=" * 60)

    for (market, supplier), g in sorted(groups.items()):
        count = g["turns"]
        name = config.SUPPLIER_NAMES.get(supplier, supplier)
        print(f"\nMarket: {market.upper()}  |  Supplier: {name}")
        print(f"  Turns:            {count}")
        print(f"  KB gap rate:      {g['gaps']}/{count} ({(g['gaps'] / count * 100):.1f}%)")
        print(f"  Escalation rate:  {g['escalations']}/{count} ({(g['escalations'] / count * 100):.1f}%)")
        print(f"  Estimated cost:   ${g['cost']:.4f}")

    if gap_questions:
        shown = gap_questions[-10:]
        print(f"\nKnowledge gaps found (latest {len(shown)} of {len(gap_questions)}; candidates for new KB entries):")
        for q in shown:
            print(f"  - {q}")


if __name__ == "__main__":
    print_report()
