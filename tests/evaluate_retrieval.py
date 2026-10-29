"""
tests/evaluate_retrieval.py

Compares the ways of searching the knowledge base on the labelled questions
in retrieval_questions.py, for every market and supplier, and shows which
threshold works best for each.

Run from the project folder:   python tests/evaluate_retrieval.py
No LLM key and no LangSmith call needed. The first run downloads the
embedding model (about 220 MB).

Variants compared (see knowledge/retrieval.py and vector_retriever.py):
  keyword        word matching only
  vector-full    meaning, entry = heading + text
  vector-max     meaning, entry = best of heading alone / heading + text
  hybrid-full    keyword first, vector-full when keyword finds nothing
  hybrid-max     keyword first, vector-max when keyword finds nothing

"held-out" questions are phrasings the keyword rules were never tuned on:
they are the fairest test of how the bot does with real customers.
"""
from __future__ import annotations

import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

import config
from exceptions import KBGapError
from knowledge import retriever as keyword_retriever
from knowledge import vector_retriever
from knowledge.kb_loader import load_kb
from tests.retrieval_questions import HEADINGS, OPTIONAL_TOPICS, QUESTIONS

THRESHOLDS = [x / 100 for x in range(30, 75, 5)]


def keyword_answer(question, docs):
    try:
        return keyword_retriever.retrieve(question, docs, min_score=config.KB_MATCH_MIN_SCORE)[0].document.heading
    except KBGapError:
        return None


def main() -> None:
    rows = []
    for market, question, topic, held_out in QUESTIONS:
        for supplier in config.SUPPORTED_SUPPLIERS:
            docs = load_kb(market, supplier)
            expected = HEADINGS[market][topic] if topic else None
            if topic in OPTIONAL_TOPICS and expected not in {d.heading for d in docs}:
                expected = None  # this supplier has no entry on the topic: a gap is right
            rows.append({
                "where": f"{market}/{supplier}", "question": question, "expected": expected, "held_out": held_out,
                "keyword": keyword_answer(question, docs),
                "headings": [d.heading for d in docs],
                "sims": vector_retriever.similarities(question, docs),
            })

    def vector_answer(row, scoring, threshold):
        scores = [vector_retriever.entry_score(s, scoring) for s in row["sims"]]
        best = max(range(len(scores)), key=scores.__getitem__)
        return (row["headings"][best] if scores[best] >= threshold else None), scores[best]

    def answer(row, variant, threshold):
        if variant == "keyword":
            return row["keyword"]
        kind, scoring = variant.split("-")
        if kind == "hybrid" and row["keyword"] is not None:
            return row["keyword"]
        return vector_answer(row, scoring, threshold)[0]

    def stats(variant, threshold):
        answers = [answer(r, variant, threshold) for r in rows]
        ok = [a == r["expected"] for r, a in zip(rows, answers)]
        def frac(cond):
            n = sum(1 for r in rows if cond(r)); k = sum(1 for r, o in zip(rows, ok) if cond(r) and o)
            return k, n
        return {
            "total": frac(lambda r: True),
            "held-out": frac(lambda r: r["held_out"]),
            "tuned-on": frac(lambda r: not r["held_out"]),
            "answered right": frac(lambda r: r["expected"] is not None),
            "gaps kept": frac(lambda r: r["expected"] is None),
            "wrong entry": (sum(1 for r, a in zip(rows, answers) if a is not None and a != r["expected"]), len(rows)),
        }

    variants = ["keyword", "vector-full", "vector-max", "hybrid-full", "hybrid-max"]
    print(f"{len(rows)} checks ({len(QUESTIONS)} questions x {len(config.SUPPORTED_SUPPLIERS)} suppliers)")
    print(f"Chroma searches completed exactly: {vector_retriever.incomplete_searches}\n")

    print("Correct out of", len(rows), "per threshold (keyword ignores the threshold):")
    print(f"{'':<13}" + "".join(f"{t:>6.2f}" for t in THRESHOLDS))
    best = {}
    for v in variants:
        totals = [stats(v, t)["total"][0] for t in THRESHOLDS]
        print(f"{v:<13}" + "".join(f"{x:>6}" for x in totals))
        top = max(totals)
        # among equally good thresholds, prefer the highest (fewer wrong answers)
        best[v] = max(t for t, x in zip(THRESHOLDS, totals) if x == top)

    print("\nEach variant at its best threshold:")
    cols = ["total", "held-out", "tuned-on", "answered right", "gaps kept", "wrong entry"]
    print(f"{'':<19}" + "".join(f"{c:>16}" for c in cols))
    for v in variants:
        st = stats(v, best[v])
        label = v if v == "keyword" else f"{v} @{best[v]:.2f}"
        print(f"{label:<19}" + "".join(f"{f'{k}/{n}':>16}" for k, n in (st[c] for c in cols)))

    winner = "keyword" if config.RETRIEVER == "keyword" else f"{config.RETRIEVER}-{config.VECTOR_SCORING}"
    t = config.VECTOR_MIN_SCORE
    print(f"\nMistakes of the configured setting, {winner} at {t:.2f} (change it in .env):")
    for r in rows:
        a = answer(r, winner, t)
        if a != r["expected"]:
            via = "keyword" if winner.startswith("hybrid") and r["keyword"] is not None else "vector"
            _, score = vector_answer(r, config.VECTOR_SCORING, t)
            print(f"  [{r['where']}] {r['question']!r}: expected {r['expected']!r}, got {a!r} "
                  f"(via {via}; best vector score {score:.2f})")


if __name__ == "__main__":
    main()
