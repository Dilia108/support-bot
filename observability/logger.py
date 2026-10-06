"""
observability/logger.py

Responsibility: persist a record of every conversation turn and every
error, for the analytics layer to read later.

Design principle from the "error handling at every step" requirement:
logging must never be allowed to break the user-facing conversation.
If SQLite is locked, corrupted, or the disk is full, we catch that and
write to a flat JSONL file instead. We lose query-ability, not data.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from typing import Optional

import config
from exceptions import LoggingError


@dataclass
class ConversationLogEntry:
    timestamp: float
    session_id: str
    market: str
    supplier: str
    user_text: str
    bot_text: str
    intent: str
    intent_confidence: float
    kb_gap: bool
    escalated: bool
    provider_used: Optional[str]
    model_used: Optional[str]
    tokens_in: int
    tokens_out: int
    estimated_cost_usd: float
    latency_seconds: float
    error: Optional[str]


def init_db(db_path: str = config.DB_PATH) -> None:
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                session_id TEXT,
                market TEXT,
                supplier TEXT,
                user_text TEXT,
                bot_text TEXT,
                intent TEXT,
                intent_confidence REAL,
                kb_gap INTEGER,
                escalated INTEGER,
                provider_used TEXT,
                model_used TEXT,
                tokens_in INTEGER,
                tokens_out INTEGER,
                estimated_cost_usd REAL,
                latency_seconds REAL,
                error TEXT
            )
            """
        )
        # Databases created before suppliers existed have no supplier column.
        # Add it in place so old turns are kept (their supplier stays empty).
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_log)")}
        if "supplier" not in columns:
            conn.execute("ALTER TABLE conversation_log ADD COLUMN supplier TEXT")
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        # Even DB *initialization* must not crash the app on startup --
        # log_conversation() will fall back to the flat file if this
        # never succeeded.
        _write_fallback_log({"init_error": str(e), "timestamp": time.time()})


def _write_fallback_log(record: dict, path: str = config.FALLBACK_LOG_PATH) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # If even the flat-file fallback fails, we deliberately give up
        # rather than raise -- logging must never take down the bot.
        pass


def log_conversation(entry: ConversationLogEntry, db_path: str = config.DB_PATH) -> None:
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO conversation_log (
                timestamp, session_id, market, supplier, user_text, bot_text, intent,
                intent_confidence, kb_gap, escalated, provider_used, model_used,
                tokens_in, tokens_out, estimated_cost_usd, latency_seconds, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.timestamp, entry.session_id, entry.market, entry.supplier,
                entry.user_text, entry.bot_text, entry.intent, entry.intent_confidence, int(entry.kb_gap),
                int(entry.escalated), entry.provider_used, entry.model_used,
                entry.tokens_in, entry.tokens_out, entry.estimated_cost_usd,
                entry.latency_seconds, entry.error,
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        _write_fallback_log({**asdict(entry), "logging_error": str(e)})


def log_error(component: str, message: str, market: str = "", session_id: str = "") -> None:
    """Lightweight error logger used by any module for out-of-band failures
    that don't map cleanly to a full conversation turn (e.g. KB load failure
    at startup)."""
    record = {
        "timestamp": time.time(),
        "component": component,
        "message": message,
        "market": market,
        "session_id": session_id,
    }
    try:
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS error_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL, component TEXT, message TEXT, market TEXT, session_id TEXT)"
        )
        conn.execute(
            "INSERT INTO error_log (timestamp, component, message, market, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (record["timestamp"], component, message, market, session_id),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        _write_fallback_log(record)
