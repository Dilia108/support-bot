"""
knowledge/retriever.py

Responsibility: find the KB document(s) most relevant to a user's message.

This uses simple word-overlap scoring on purpose -- zero extra dependencies,
zero API calls, runs instantly offline, and is easy for a reviewer to read
top to bottom. It is intentionally written so the scoring function is the
ONLY thing you'd swap out to upgrade to real embeddings:

    LangChain + a vector store (Chroma/FAISS) upgrade path:
      1. Replace `_score` with a call to an embeddings model.
      2. Replace the linear scan in `retrieve` with a vector similarity
         search (e.g. `vectorstore.similarity_search_with_score(query)`).
      3. Everything else (KBGapError handling, thresholding, callers)
         stays exactly the same.

Six rules keep word overlap honest (each one fixes a real wrong answer):
  1. Accents are folded, so "devolución", "devolucion" and "DEVOLUCIÓN"
     are the same word, and Spanish words are not cut apart at the accent.
  2. Known synonyms are mapped to the word the KB uses (_SYNONYMS), so a
     customer writing "estatus" finds the entry about "estado".
  3. Word forms are matched, so "later" finds "late", "Stornierung" finds
     "stornieren" and "Status" finds "Buchungsstatus" (_same_word).
  4. A word counts for less the more entries it appears in. "minutes"
     (one entry) is strong evidence; "booking" (most entries) is weak; and
     a weak word alone is not enough to match (_MIN_EVIDENCE).
  5. A word in an entry's heading counts double, because the heading
     says what the entry is about (_HEADING_BOOST).
  6. Words the KB never uses ("would", "happen", "agreed") are ignored
     instead of diluting the score, so a long, polite question matches as
     well as a short one. The score is the share of the RECOGNISED words
     that point at an entry.
If several entries tie for the best score, all of them are returned --
picking one by file order would hand the model the wrong entry.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from exceptions import KBGapError
from knowledge.kb_loader import KBDocument

# Any run of letters in any language (the old pattern listed only German
# special characters, so "gestión" was split into "gesti" + "n").
_WORD_RE = re.compile(r"[^\W\d_]+")


def _fold(word: str) -> str:
    """Lowercases and strips accents: 'Dónde' -> 'donde', 'Rückgabe' -> 'ruckgabe'."""
    decomposed = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# Short/common words carry almost no topical signal and cause false matches
# (e.g. "ein", "du", "the", "de", "que"). Filtering them out is what turns
# "naive word overlap" from a liability into something usable for a demo.
_STOPWORDS = {
    "de": """ich du er sie es wir ihr man sich mir mich dir dich uns
        mein meine meinen meiner meinem dein deine ihre ihren ihrer ihrem
        sein seine unser unsere der die das den dem des ein eine einen einem
        einer eines und oder aber wenn dass weil als wie was wer wo wann warum
        welche welcher welches ist sind bin war waren wird werden wurde
        kann kannst können könnte muss müssen soll sollte darf möchte will
        wollen habe hat haben hatte gibt geht mit ohne für von vom zum zur
        bei auf aus nach vor über unter bis durch gegen nicht kein keine
        auch noch schon nur sehr mehr hier dort dann bitte hallo danke
        passiert eigentlich doch nein""".split(),
    "es": """yo tú él ella nosotros un una unos unas el la los las lo le les
        me te se mi mis su sus tus nuestro nuestra que qué como cómo donde
        dónde cuando cuándo cual cuál quien por para con sin de del al en
        pero si no sí es son soy está están estoy era fue ser estar hay
        tengo tiene tienen puedo puede pueden puedes quiero quiere necesito
        hacer hago más muy ya también este esta esto ese esa eso aquí ahí
        hola gracias favor pasa pasaría sobre entre hasta desde""".split(),
    "us": """i you he she it we they me my your our their a an the and or
        but if is are am was were be been do does did can could would
        should will may might have has had what how where when which who
        why to of in on at for from with without by as that this these
        those there here not no yes please hello hi thanks thank need want
        like get happen happens about into than then so just also any
        some""".split(),
}
# Words that appear in almost every car-rental conversation, so they say
# nothing about which FAQ entry is meant. The supplier names are here too:
# the supplier is already chosen before retrieval starts.
_DOMAIN_WORDS = """car cars vehicle vehicles auto autos coche coches carro
    vehículo vehículos fahrzeug fahrzeuge mietwagen wagen
    avis sixt enterprise""".split()

_ALL_STOPWORDS = {_fold(w) for words in _STOPWORDS.values() for w in words} | {_fold(w) for w in _DOMAIN_WORDS}
_MIN_TOKEN_LENGTH = 3  # drops stray short fragments alongside stopwords

# Customer word -> the word the KB uses. Word overlap cannot know that two
# different words mean the same thing, so this is where you teach it.
# How to maintain it: when the analytics gap report shows a question the KB
# DOES answer, the customer used a word the KB doesn't -- add it here.
# Write both sides without accents and in lowercase. The map is applied to
# the question AND to the KB text, so the right-hand word only has to be
# consistent; "estado" and "estatus" both become "status" so they also match
# German/English entries that say "Status".
_SYNONYMS = {
    # English
    "modify": "change", "modification": "change", "amend": "change",
    "update": "change", "edit": "change",
    "cancellation": "cancel", "cancellations": "cancel", "cancelled": "cancel",
    "canceled": "cancel", "cancelling": "cancel", "canceling": "cancel",
    "reservation": "booking", "reservations": "booking",
    # German
    "storno": "stornieren",
    "anderung": "andern", "anderungen": "andern", "umbuchen": "andern",
    "umbuchung": "andern", "bearbeiten": "andern", "verschieben": "andern",
    "spat": "verspatet", "spater": "verspatet",
    "zuruckgeben": "ruckgabe", "zuruckgebe": "ruckgabe", "zuruckgibt": "ruckgabe",
    "zuruckbringen": "ruckgabe", "abgeben": "ruckgabe", "abgabe": "ruckgabe",
    "reservierung": "buchung", "reservierungen": "buchung",
    # Spanish
    "estado": "status", "estatus": "status",
    "cambiar": "modificar", "cambio": "modificar", "cambios": "modificar",
    "anular": "cancelar",
    "tarde": "tardia",
    "devolver": "devolucion", "devuelvo": "devolucion", "devuelves": "devolucion",
    "devuelto": "devolucion", "entregar": "devolucion", "entrego": "devolucion",
}

# Two words count as the same when they are clearly forms of one word:
#   - one is the other plus a short ending: return/returns, late/later
#   - they share a long beginning: stornieren/Stornierung, cancelar/cancelación
#   - the longer one ENDS with the shorter one, which is how German builds
#     compounds: Status/Buchungsstatus, Kosten/Mietkosten
_MIN_STEM_LENGTH = 4      # shorter words must match exactly
_MAX_EXTRA_LETTERS = 2    # "kosten" must not match "kostenlos" this way
_MIN_SHARED_BEGINNING = 7
_MIN_COMPOUND_PART = 6

# How much evidence an entry needs before it counts as a match. A word found
# in only one entry is worth 1.0; a word found in every entry is worth 0.
# With 0.6, one word that roughly half the entries share is not enough on
# its own ("booking"), but one specific word is ("minutes"), and so are two
# half-specific ones together.
_MIN_EVIDENCE = 0.6

# A word that is part of an entry's heading counts this many times.
_HEADING_BOOST = 2


@dataclass
class RetrievalResult:
    document: KBDocument
    score: float


def _tokenize(text: str) -> set[str]:
    words = {_fold(w) for w in _WORD_RE.findall(text)}
    words = {w for w in words if w not in _ALL_STOPWORDS and len(w) >= _MIN_TOKEN_LENGTH}
    return {_SYNONYMS.get(w, w) for w in words}


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if (
        len(short) >= _MIN_STEM_LENGTH
        and len(long_) - len(short) <= _MAX_EXTRA_LETTERS
        and long_.startswith(short)
    ):
        return True
    if len(short) >= _MIN_SHARED_BEGINNING and long_[:_MIN_SHARED_BEGINNING] == short[:_MIN_SHARED_BEGINNING]:
        return True
    return len(short) >= _MIN_COMPOUND_PART and long_.endswith(short)


def _word_weight(docs_with_word: int, total_docs: int) -> float:
    """1.0 for a word found in a single entry, falling to 0.0 for a word in all."""
    if total_docs <= 1:
        return 1.0
    return 1.0 - (docs_with_word - 1) / (total_docs - 1)


def _score(
    query_tokens: set[str],
    all_doc_tokens: list[set[str]],
    all_heading_tokens: list[set[str]],
) -> list[float]:
    """
    Returns one score per document, in the same order as all_doc_tokens.
    This is the function to replace when upgrading to embeddings.
    """
    total_docs = len(all_doc_tokens)

    def contains(tokens: set[str], word: str) -> bool:
        return any(_same_word(word, t) for t in tokens)

    # For each query word: which documents contain it, and in which of
    # those is it part of the heading?
    found_in = {w: [i for i, tokens in enumerate(all_doc_tokens) if contains(tokens, w)] for w in query_tokens}
    in_heading = {w: [i for i in docs if contains(all_heading_tokens[i], w)] for w, docs in found_in.items()}
    # Words no document contains are dropped here, so they cannot dilute.
    weights = {w: _word_weight(len(docs), total_docs) for w, docs in found_in.items() if docs}

    # The heading says what an entry is ABOUT, so a heading word counts
    # double. Without this, one incidental word in the body ("fecha de
    # recogida") can outweigh the topic word ("modificar").
    recognised = sum(w * (_HEADING_BOOST if in_heading[word] else 1) for word, w in weights.items())
    if recognised == 0:
        return [0.0] * total_docs

    scores = []
    for i in range(total_docs):
        matched = [(word, w) for word, w in weights.items() if i in found_in[word]]
        evidence = sum(w for _, w in matched)
        boosted = sum(w * (_HEADING_BOOST if i in in_heading[word] else 1) for word, w in matched)
        scores.append(boosted / recognised if evidence >= _MIN_EVIDENCE else 0.0)
    return scores


def retrieve(query: str, documents: list[KBDocument], min_score: float, top_k: int = 1) -> list[RetrievalResult]:
    """
    Returns the top_k best-matching documents above min_score, plus any
    further documents that tie with the last one included.
    Raises KBGapError if nothing clears the threshold -- this is the
    signal the analytics layer uses to find real knowledge gaps, so it
    must NOT be silently swallowed here.
    """
    all_doc_tokens = [_tokenize(d.heading + " " + d.content) for d in documents]
    all_heading_tokens = [_tokenize(d.heading) for d in documents]
    scores = _score(_tokenize(query), all_doc_tokens, all_heading_tokens)

    scored = [RetrievalResult(document=d, score=score) for d, score in zip(documents, scores)]
    scored.sort(key=lambda r: r.score, reverse=True)

    results = [r for r in scored[:top_k] if r.score > 0 and r.score >= min_score]
    if results:
        cutoff = results[-1].score
        results += [r for r in scored[top_k:] if r.score == cutoff]
    if not results:
        raise KBGapError(
            f"No knowledge base match above threshold {min_score} for query: '{query[:80]}'"
        )
    return results
