"""
analytics/dashboard.py

Responsibility: turn the raw conversation log into the numbers that
actually drive decisions -- this is the "Performance Analytics" piece
of the job requirements. Per market and supplier:
  - fallback rate: questions with no knowledge base answer (KB gaps)
  - escalation rate: turns handed to a person
  - resolved by the bot: answered from the KB without a handover
  - intent recognised: the intent detector understood the request
plus the unanswered questions with a suggested next step, because the
point is to find gaps and act on them, not only to count them.

Two views of the same numbers:
  - python analytics/dashboard.py      text report in the terminal
  - http://127.0.0.1:5000/dashboard     chart page, served by web_app.py
Both call collect(), so they always agree.

Plain sqlite3, no extra dependencies.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

# Allow running this file directly (`python analytics/dashboard.py`) as well
# as importing it as a package module -- both need the project root on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from observability.logger import init_db

NO_SUPPLIER = "none"          # turns logged before suppliers existed
MAX_UNANSWERED = 200          # unanswered questions sent to the dashboard (it shows 25 at first)
PERIODS = {"all": None, "30d": 30, "7d": 7, "24h": 1}  # name -> days


def _fetchall(query: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        return conn.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _day_start(value: str | None) -> float | None:
    """'2026-10-29' -> timestamp of 00:00 local time that day; None if empty or invalid."""
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").timestamp()
    except ValueError:
        return None


def collect(
    period: str = "all",
    market: str = "all",
    trace_url_template: str | None = None,
    supplier: str = "all",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """
    All dashboard numbers for one selection:
      period     "all", "30d", "7d" or "24h"
      date_from / date_to   "YYYY-MM-DD", both days included. Either one
                 replaces the period (it becomes "custom").
      market     "all" or a market code
      supplier   "all", a supplier id, or "none" (turns logged before
                 suppliers existed)
    Unknown values are treated as "all"; invalid dates are ignored.

    trace_url_template: LangSmith address with a "{run_id}" placeholder
    (observability.tracing.run_url_template()). When given, each unanswered
    question links to its latest trace, where the rewritten question, the
    retrieval scores and the prompt show why it had no answer.
    """
    # Makes sure the table exists and has the supplier column, also when
    # this runs against an older database.
    init_db()
    period = period if period in PERIODS else "all"
    market = market if market in config.SUPPORTED_MARKETS else "all"
    supplier = supplier if supplier in config.SUPPORTED_SUPPLIERS or supplier == NO_SUPPLIER else "all"
    start, end = _day_start(date_from), _day_start(date_to)
    if end is not None:
        end = (datetime.fromtimestamp(end) + timedelta(days=1)).timestamp()  # include the whole last day
    if start is not None or end is not None:
        period = "custom"

    query = ("SELECT timestamp, market, supplier, kb_gap, escalated, estimated_cost_usd, user_text, intent, trace_id "
             "FROM conversation_log WHERE 1=1")
    params: list = []
    if period == "custom":
        if start is not None:
            query += " AND timestamp >= ?"
            params.append(start)
        if end is not None:
            query += " AND timestamp < ?"
            params.append(end)
    elif PERIODS[period]:
        query += " AND timestamp >= ?"
        params.append(time.time() - PERIODS[period] * 86400)
    if market != "all":
        query += " AND market = ?"
        params.append(market)
    if supplier == NO_SUPPLIER:
        query += " AND (supplier IS NULL OR supplier = '')"
    elif supplier != "all":
        query += " AND supplier = ?"
        params.append(supplier)
    rows = _fetchall(query, tuple(params))

    groups: dict[tuple[str, str], dict] = {}
    unanswered: dict[tuple[str, str], dict] = {}
    def empty() -> dict:
        return {"turns": 0, "gaps": 0, "escalations": 0, "resolved": 0, "intents_recognised": 0, "cost": 0.0}

    totals = empty()
    for ts, mkt, supplier, kb_gap, escalated, cost, user_text, intent, trace_id in rows:
        supplier = supplier or NO_SUPPLIER
        g = groups.setdefault((mkt, supplier), empty())
        for bucket in (g, totals):
            bucket["turns"] += 1
            bucket["gaps"] += 1 if kb_gap else 0
            bucket["escalations"] += 1 if escalated else 0
            bucket["resolved"] += 1 if not kb_gap and not escalated else 0
            bucket["intents_recognised"] += 1 if intent and intent != "unknown" else 0
            bucket["cost"] += cost or 0.0
        if kb_gap:
            # The same question asked several times in one market is one KB
            # candidate, so identical questions (ignoring case and spaces) are
            # counted together, with the suppliers they were asked for.
            text = " ".join((user_text or "").split())
            key = (mkt, text.lower())
            u = unanswered.setdefault(key, {"question": text, "market": mkt, "suppliers": set(),
                                            "count": 0, "handed_over": 0, "last_seen": 0.0, "trace_id": None})
            u["suppliers"].add(supplier)
            u["count"] += 1
            u["handed_over"] += 1 if escalated else 0
            if (ts or 0.0) >= u["last_seen"]:
                u["last_seen"] = ts or 0.0
                u["trace_id"] = trace_id or u["trace_id"]  # turns logged before trace ids have none

    def supplier_name(s: str) -> str:
        return config.SUPPLIER_NAMES.get(s, "No supplier recorded" if s == NO_SUPPLIER else s)

    group_list = [
        {
            "market": mkt, "supplier": sup, "supplier_name": supplier_name(sup),
            **g, "cost": round(g["cost"], 6),
            "gap_rate": _rate(g["gaps"], g["turns"]),
            "escalation_rate": _rate(g["escalations"], g["turns"]),
            "resolved_rate": _rate(g["resolved"], g["turns"]),
            "intent_rate": _rate(g["intents_recognised"], g["turns"]),
        }
        for (mkt, sup), g in groups.items()
    ]
    # Fixed order (market as configured, then supplier as configured), so a
    # row keeps its place when the numbers change.
    market_order = {m: i for i, m in enumerate(config.SUPPORTED_MARKETS)}
    supplier_order = {s: i for i, s in enumerate(config.SUPPORTED_SUPPLIERS)}
    group_list.sort(key=lambda g: (market_order.get(g["market"], 99), supplier_order.get(g["supplier"], 99)))

    unanswered_list = sorted(unanswered.values(), key=lambda u: (-u["count"], -u["last_seen"]))
    for u in unanswered_list:
        u["trace_url"] = trace_url_template.format(run_id=u["trace_id"]) if trace_url_template and u["trace_id"] else None
        known = [s for s in config.SUPPORTED_SUPPLIERS if s in u["suppliers"]]
        u["suppliers"] = known + sorted(u["suppliers"] - set(known))
        u["supplier_names"] = [supplier_name(s) for s in u["suppliers"]]
        # The next step, so the list is a to-do list and not only a count.
        # A question asked for several suppliers is a topic missing from the
        # market, to be researched per supplier (their policies differ).
        if known == [] :
            u["next_step"] = "Find out which supplier it was asked for, then add it to that supplier's KB"
        elif len(known) == 1:
            u["next_step"] = (f"Research {supplier_name(known[0])}'s policy and add it to a new dated file in "
                              f"knowledge_base/{u['market']}/{known[0]}/")
        else:
            u["next_step"] = (f"Missing for {len(known)} suppliers: research each policy and add it to a new "
                              f"dated file in knowledge_base/{u['market']}/<supplier>/")

    return {
        "period": period,
        "market": market,
        "supplier": supplier,
        "suppliers": [{"id": s, "name": config.SUPPLIER_NAMES[s]} for s in config.SUPPORTED_SUPPLIERS],
        "markets": config.SUPPORTED_MARKETS,
        "totals": {**totals, "cost": round(totals["cost"], 6),
                   "gap_rate": _rate(totals["gaps"], totals["turns"]),
                   "escalation_rate": _rate(totals["escalations"], totals["turns"]),
                   "resolved_rate": _rate(totals["resolved"], totals["turns"]),
                   "intent_rate": _rate(totals["intents_recognised"], totals["turns"])},
        "groups": group_list,
        "unanswered": unanswered_list[:MAX_UNANSWERED],
        "unanswered_total": len(unanswered_list),
        # The project's trace list, for the link in the page header.
        "langsmith_project_url": trace_url_template.split("/r/")[0] if trace_url_template else None,
    }


def print_report() -> None:
    data = collect()
    totals = data["totals"]
    if not totals["turns"]:
        print("No conversation data yet. Run main.py or web_app.py and chat with the bot first.")
        return

    print("=" * 60)
    print(f"SUPPORT BOT ANALYTICS -- {totals['turns']} conversation turns logged")
    print("=" * 60)
    for g in data["groups"]:
        n = g["turns"]
        print(f"\nMarket: {g['market'].upper()}  |  Supplier: {g['supplier_name']}")
        print(f"  Turns:            {n}")
        print(f"  Resolved by bot:  {g['resolved']}/{n} ({g['resolved_rate'] * 100:.1f}%)")
        print(f"  Fallback rate:    {g['gaps']}/{n} ({g['gap_rate'] * 100:.1f}%)  (no KB answer)")
        print(f"  Escalation rate:  {g['escalations']}/{n} ({g['escalation_rate'] * 100:.1f}%)")
        print(f"  Intent recognised:{g['intents_recognised']:>3}/{n} ({g['intent_rate'] * 100:.1f}%)")
        print(f"  Estimated cost:   ${g['cost']:.4f}")

    if data["unanswered"]:
        shown = data["unanswered"][:10]
        print(f"\nUnanswered questions (top {len(shown)} of {data['unanswered_total']}; "
              "candidates for new KB entries):")
        for u in shown:
            times = f" ({u['count']}x)" if u["count"] > 1 else ""
            print(f"  - [{u['market']}] {u['question']}{times} -- {', '.join(u['supplier_names'])}")
            print(f"      next: {u['next_step']}")
    print(f"\nCharts: run python web_app.py and open http://127.0.0.1:{config.WEB_PORT}/dashboard")


if __name__ == "__main__":
    print_report()
