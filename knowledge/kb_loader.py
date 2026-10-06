"""
knowledge/kb_loader.py

Responsibility: load one supplier's knowledge base for one market from disk.

Layout: knowledge_base/<market>/<supplier>/<YYYY-MM-DD>.md
    e.g. knowledge_base/de/sixt/2026-10-06.md

Policies differ per supplier, so the files are never mixed: a question about
a SIXT booking must only ever be answered from the SIXT file.

Versioning: the file name is the date a version takes effect. The loader
uses the newest file whose date is today or earlier, so
  - a policy change is a NEW file, never an edit to an old one (the old
    version stays on disk as the record of what the bot said before),
  - a file dated in the future is ignored until that day arrives, which
    lets you prepare an announced policy change in advance,
  - rolling back is deleting (or re-dating) the newest file.
Every new file gets a line in knowledge_base/CHANGELOG.md saying what
changed and why.

Kept deliberately simple (plain markdown files split on '## ' headings)
so the whole KB is human-editable and diffable in git -- easy to version,
easy to review in a pull request, no database required for the demo.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date
from typing import Optional

# Allow running this file directly (`python knowledge/kb_loader.py`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exceptions import KBLoadError

KB_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge_base")


@dataclass
class KBDocument:
    market: str
    supplier: str
    heading: str
    content: str
    source_file: str
    version: str = ""  # effective date of the KB file this entry came from


def _split_into_sections(text: str, source_file: str, market: str, supplier: str, version: str = "") -> list[KBDocument]:
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
            docs.append(KBDocument(market=market, supplier=supplier, heading=heading, content=content,
                                   source_file=source_file, version=version))
    return docs


def list_versions(market: str, supplier: str) -> list[date]:
    """
    Returns the effective dates of all KB versions for one market and
    supplier, oldest first. Files whose name is not a date are ignored.
    Raises KBLoadError if the folder does not exist.
    """
    folder = os.path.join(KB_ROOT, market, supplier)
    if not os.path.isdir(folder):
        raise KBLoadError(f"No knowledge base folder for market '{market}', supplier '{supplier}': {folder}")
    versions = []
    try:
        for filename in os.listdir(folder):
            name, ext = os.path.splitext(filename)
            if ext != ".md":
                continue
            try:
                versions.append(date.fromisoformat(name))
            except ValueError:
                continue  # e.g. notes.md -- not a dated version
    except OSError as e:
        raise KBLoadError(f"Failed listing knowledge base versions for '{market}/{supplier}': {e}") from e
    return sorted(versions)


def active_version(market: str, supplier: str, today: Optional[date] = None) -> date:
    """The newest version that is already in effect on `today`."""
    today = today or date.today()
    in_effect = [v for v in list_versions(market, supplier) if v <= today]
    if not in_effect:
        raise KBLoadError(
            f"No knowledge base version in effect for '{market}/{supplier}' on {today.isoformat()}"
        )
    return in_effect[-1]


def load_kb(market: str, supplier: str, today: Optional[date] = None) -> list[KBDocument]:
    """
    Loads the KB version in effect today for one market and supplier:
    knowledge_base/{market}/{supplier}/{newest date <= today}.md
    Raises KBLoadError if no such version exists or it contains no usable
    documents -- callers are expected to catch this and degrade
    gracefully rather than crash the conversation.
    """
    version = active_version(market, supplier, today).isoformat()
    filename = f"{version}.md"
    path = os.path.join(KB_ROOT, market, supplier, filename)

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise KBLoadError(f"Failed reading knowledge base for '{market}/{supplier}': {e}") from e

    documents = _split_into_sections(
        text, source_file=f"{supplier}/{filename}", market=market, supplier=supplier, version=version
    )
    if not documents:
        raise KBLoadError(f"Knowledge base for '{market}/{supplier}' (version {version}) is empty")

    return documents


def print_versions() -> None:
    """Shows, for every market and supplier, which KB version is live."""
    import config

    today = date.today()
    print(f"Knowledge base versions in effect on {today.isoformat()}")
    for market in config.SUPPORTED_MARKETS:
        for supplier in config.SUPPORTED_SUPPLIERS:
            try:
                versions = list_versions(market, supplier)
                live = active_version(market, supplier, today)
            except KBLoadError as e:
                print(f"  {market}/{supplier:<11} MISSING  ({e})")
                continue
            older = [v.isoformat() for v in versions if v < live]
            upcoming = [v.isoformat() for v in versions if v > today]
            notes = []
            if older:
                notes.append(f"{len(older)} older")
            if upcoming:
                notes.append("upcoming: " + ", ".join(upcoming))
            print(f"  {market}/{supplier:<11} {live.isoformat()}" + (f"  ({'; '.join(notes)})" if notes else ""))


if __name__ == "__main__":
    print_versions()
