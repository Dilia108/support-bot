"""
knowledge/vector_retriever.py

Responsibility: find the KB entry that answers a customer's question by
MEANING, not by shared words, using LangChain with a Chroma vector store.

How it works:
  1. Each FAQ entry is turned into embeddings: lists of numbers that place
     texts with similar meaning close together, across languages. Every
     entry gets two: one for its heading alone (what the entry is about)
     and one for heading + text (the details). "estatus de mi reserva" lands next to "Consultar el estado de
     la reserva" although the words differ.
  2. The entries of one market + supplier + KB version go into their own
     Chroma collection, so a SIXT question can never match an Avis entry.
  3. The question is embedded the same way and compared with both vectors
     of each entry. config.VECTOR_SCORING decides the entry's score: "full"
     (heading + text only) or "max" (the better of the two). A long entry
     text mixes several subjects (a cancellation entry also mentions fees,
     dates and refunds), which blurs its vector; the heading vector stays
     sharp. The evaluation script compares both.
  4. Below config.VECTOR_MIN_SCORE nothing is close enough: that is a
     knowledge gap (KBGapError), exactly as with the keyword retriever.

The embedding model runs locally (FastEmbed, ONNX), so no API key and no
per-question cost. It is multilingual, which matters for DE/ES/US. The model
is downloaded once on first use (about 220 MB) into .model_cache/ in the
project folder (not committed), so it survives restarts and temp clean-ups.

Scores: Chroma returns a cosine distance (0 = same direction, 2 = opposite).
The score is 1 - distance, limited to 0..1, so unrelated texts score 0
rather than a small negative number.

Indexes live in memory and are rebuilt when the bot starts. With a handful
of entries per supplier that takes well under a second; a large KB would use
a persistent Chroma directory instead.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from typing import Optional

from langchain_core.embeddings import Embeddings

import config
from exceptions import KBGapError
from knowledge.kb_loader import KBDocument
from knowledge.retriever import RetrievalResult

# Entries whose score is this close to the best one are returned with it, so
# the model sees both instead of the bot silently picking one of two equally
# good candidates.
NEAR_TIE_MARGIN = 0.03

logger = logging.getLogger(__name__)

MODEL_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".model_cache")

_lock = threading.Lock()
_embeddings = None
incomplete_searches = 0  # how often Chroma returned fewer entries than stored (shown by the evaluation)
_indexes: dict[tuple, object] = {}


class LocalEmbeddings(Embeddings):
    """
    LangChain's Embeddings interface around a local FastEmbed model.
    Written here (a few lines) instead of importing it from
    langchain-community, which is being retired.
    """

    def __init__(self, model_name: str, cache_dir: str):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _get_embeddings() -> Embeddings:
    """Loads the embedding model once per process (downloads it on first use)."""
    global _embeddings
    if _embeddings is None:
        _embeddings = LocalEmbeddings(config.EMBEDDING_MODEL, MODEL_CACHE_DIR)
    return _embeddings


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _index_key(documents: list[KBDocument]) -> tuple:
    first = documents[0]
    # The version is part of the key, so a new dated KB file gets a new index.
    return (first.market, first.supplier, first.version, len(documents))


def _get_index(documents: list[KBDocument]):
    """The Chroma collection for these documents, built on first use."""
    key = _index_key(documents)
    with _lock:
        index = _indexes.get(key)
        if index is None:
            from langchain_chroma import Chroma
            from langchain_core.documents import Document

            index = Chroma(
                collection_name="kb-" + "-".join(str(part) for part in key[:3]).replace(".", "_"),
                embedding_function=_get_embeddings(),
                # Cosine similarity: 1 = same meaning, 0 = unrelated.
                collection_metadata={"hnsw:space": "cosine"},
            )
            entries = []
            for i, d in enumerate(documents):
                entries.append(Document(page_content=d.heading, metadata={"position": i, "part": "heading"}))
                entries.append(Document(page_content=f"{d.heading}\n{d.content}", metadata={"position": i, "part": "full"}))
            index.add_documents(entries, ids=[f"{e.metadata['position']}-{e.metadata['part']}" for e in entries])
            _indexes[key] = index
    return index


def similarities(query: str, documents: list[KBDocument]) -> list[dict[str, float]]:
    """
    For each entry, in order: {"heading": ..., "full": ...}, each a
    similarity between 0 (unrelated) and 1 (same meaning).
    """
    index = _get_index(documents)
    query_vector = _get_embeddings().embed_query(query)
    expected = 2 * len(documents)
    hits = index.similarity_search_by_vector_with_relevance_scores(query_vector, k=expected)
    found = {(doc.metadata["position"], doc.metadata["part"]): 1.0 - distance for doc, distance in hits}

    # Every entry must get a score, or a good entry could silently drop out.
    # Chroma's search is approximate (built for millions of vectors); if it
    # ever returns fewer vectors than were stored, the missing ones are
    # scored exactly from the vectors Chroma holds.
    if len(found) < expected:
        global incomplete_searches
        incomplete_searches += 1
        stored = index.get(include=["embeddings", "metadatas"])
        for meta, vector in zip(stored["metadatas"], stored["embeddings"]):
            found.setdefault((meta["position"], meta["part"]), _cosine(query_vector, list(vector)))
        logger.debug("Chroma returned %d of %d vectors; scored the rest directly", len(hits), expected)
    if not found:
        # Not a knowledge gap: the store itself returned nothing.
        raise RuntimeError("Vector store returned no entries for a non-empty knowledge base")

    def clamp(value: float) -> float:
        return round(min(max(value, 0.0), 1.0), 3)

    return [
        {part: clamp(found.get((i, part), 0.0)) for part in ("heading", "full")}
        for i in range(len(documents))
    ]


def entry_score(sims: dict[str, float], scoring: str) -> float:
    return max(sims["heading"], sims["full"]) if scoring == "max" else sims["full"]


def retrieve(
    query: str,
    documents: list[KBDocument],
    min_score: Optional[float] = None,
    top_k: int = 1,
    scoring: Optional[str] = None,
) -> list[RetrievalResult]:
    """
    Same contract as knowledge.retriever.retrieve: the best matching
    entries above min_score, or KBGapError if none is close enough.
    Any other exception means the vector search itself failed; the caller
    (knowledge/retrieval.py) then falls back to keyword matching.
    """
    min_score = config.VECTOR_MIN_SCORE if min_score is None else min_score
    scoring = scoring or config.VECTOR_SCORING
    if not documents or not query.strip():
        raise KBGapError(f"No knowledge base match for query: '{query[:80]}'")

    scored = [
        RetrievalResult(document=documents[i], score=entry_score(sims, scoring))
        for i, sims in enumerate(similarities(query, documents))
    ]
    scored.sort(key=lambda r: r.score, reverse=True)

    results = [r for r in scored[:top_k] if r.score > 0 and r.score >= min_score]
    if results:
        cutoff = results[-1].score - NEAR_TIE_MARGIN
        results += [r for r in scored[top_k:] if r.score >= max(cutoff, min_score)]
    if not results:
        best = scored[0].score if scored else 0.0
        raise KBGapError(
            f"No knowledge base match above {min_score} (best {best}) for query: '{query[:80]}'"
        )
    return results
