"""
knowledge/retrieval.py

Responsibility: choose how the knowledge base is searched, and never let a
search failure break a conversation.

Two retrievers, one contract (best entries above a threshold, or KBGapError),
combined in three modes (SUPPORT_BOT_RETRIEVER):
  - "vector":  LangChain + Chroma, matching by meaning across languages.
               See knowledge/vector_retriever.py.
  - "keyword": the original word-overlap matcher. No downloads, no extra
               packages. See knowledge/retriever.py.
  - "hybrid":  (default) keyword first; only when it finds nothing, the vector search
               decides. Keyword matching is precise on the words the FAQ
               uses; the vector search catches rephrasings it misses.
               Combining both is common practice ("hybrid search").

If the vector search cannot run (packages not installed, model download
blocked, a corrupt cache), the bot logs the reason once and answers with
the keyword retriever instead, so customers still get answers. A knowledge
gap is not a failure: KBGapError is passed on unchanged in both modes.
"""
from __future__ import annotations

import logging

import config
from exceptions import KBGapError
from knowledge import retriever as keyword_retriever
from knowledge.retriever import RetrievalResult

logger = logging.getLogger(__name__)

_vector_failed_reason = ""  # set once the vector search has failed in this process


def _uses_vector() -> bool:
    return config.RETRIEVER in ("vector", "hybrid") and not _vector_failed_reason


def active_retriever() -> str:
    """Which mode answers right now: 'vector', 'hybrid' or 'keyword'."""
    return config.RETRIEVER if _uses_vector() else "keyword"


def warm_up() -> str:
    """
    Loads the embedding model at startup, so the download (first run only)
    or a failure happens before the first customer question, not during it.
    Returns a one-line status for the startup message.
    """
    global _vector_failed_reason
    if config.RETRIEVER not in ("vector", "hybrid"):
        return "keyword matching (SUPPORT_BOT_RETRIEVER=keyword)"
    try:
        from knowledge import vector_retriever

        vector_retriever._get_embeddings().embed_query("warm-up")
        label = "keyword + LangChain/Chroma (hybrid)" if config.RETRIEVER == "hybrid" else "LangChain + Chroma"
        return f"{label}, model {config.EMBEDDING_MODEL}"
    except Exception as e:  # noqa: BLE001
        _vector_failed_reason = f"{type(e).__name__}: {e}"
        return f"keyword matching, because vector search is unavailable ({_vector_failed_reason[:120]})"


def _keyword(query: str, documents: list) -> list[RetrievalResult]:
    return keyword_retriever.retrieve(query, documents, min_score=config.KB_MATCH_MIN_SCORE)


def retrieve(query: str, documents: list) -> list[RetrievalResult]:
    global _vector_failed_reason
    if not _uses_vector():
        return _keyword(query, documents)

    if config.RETRIEVER == "hybrid":
        try:
            return _keyword(query, documents)
        except KBGapError:
            pass  # no keyword match: let the vector search decide

    try:
        from knowledge import vector_retriever

        return vector_retriever.retrieve(query, documents)
    except KBGapError:
        raise
    except Exception as e:  # noqa: BLE001 -- any failure: fall back, don't break the turn
        _vector_failed_reason = f"{type(e).__name__}: {e}"
        logger.warning("Vector search unavailable, using keyword matching instead: %s", _vector_failed_reason)
    return _keyword(query, documents)
