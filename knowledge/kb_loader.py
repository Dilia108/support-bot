"""
knowledge/kb_loader.py

Responsibility: load one supplier's knowledge base for one market from disk.

Layout: knowledge_base/<market>/<supplier>.md, e.g. knowledge_base/de/sixt.md.
Policies differ per supplier, so the files are never mixed: a question about
a SIXT booking must only ever be answered from the SIXT file.

Kept deliberately simple (plain markdown files split on '## ' headings)
so the whole KB is human-editable and diffable in git -- easy to version,
easy to review in a pull request, no database required for the demo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from exceptions import KBLoadError

KB_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge_base")


@dataclass
class KBDocument:
    market: str
    supplier: str
    heading: str
    content: str
    source_file: str


def _split_into_sections(text: str, source_file: str, market: str, supplier: str) -> list[KBDocument]:
    sections = text.split("\n## ")
    docs = []
    for i, section in enumerate(sections):
        section = section.strip()
        if not section:
            continue
        # Restore the '## ' marker we split on, except for the very first
        # section which may or may not have started with it.
        if i > 0 or not section.startswith("#"):
            section = "## " + section
        lines = section.splitlines()
        heading = lines[0].lstrip("#").strip() if lines else "Untitled"
        content = "\n".join(lines[1:]).strip()
        if content:
            docs.append(KBDocument(market=market, supplier=supplier, heading=heading, content=content, source_file=source_file))
    return docs


def load_kb(market: str, supplier: str) -> list[KBDocument]:
    """
    Loads knowledge_base/{market}/{supplier}.md.
    Raises KBLoadError if the file is missing or contains no usable
    documents -- callers are expected to catch this and degrade
    gracefully rather than crash the conversation.
    """
    filename = f"{supplier}.md"
    path = os.path.join(KB_ROOT, market, filename)
    if not os.path.isfile(path):
        raise KBLoadError(f"No knowledge base file for market '{market}', supplier '{supplier}': {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise KBLoadError(f"Failed reading knowledge base for '{market}/{supplier}': {e}") from e

    documents = _split_into_sections(text, source_file=filename, market=market, supplier=supplier)
    if not documents:
        raise KBLoadError(f"Knowledge base for '{market}/{supplier}' is empty")

    return documents
