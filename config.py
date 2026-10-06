"""
config.py
Central place for all settings. Nothing here should crash on import
if a real API key is missing. LLM provider keys stay optional (the bot
falls back to MockProvider), but LangSmith tracing is REQUIRED: the
startup check in observability/langsmith_setup.py refuses to run the bot
without a working LANGSMITH_API_KEY.
"""
import os

# Load .env if python-dotenv is installed. os.getenv() alone does not read
# a .env file, so without this the values in .env are never picked up
# unless you export them in your shell.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# --- Markets this bot supports -------------------------------------------------
SUPPORTED_MARKETS = ["de", "es", "us"]
DEFAULT_MARKET = "de"

# --- Suppliers ---------------------------------------------------------------
# Answers depend on the rental supplier as well as the market, so each
# supplier has its own FAQ file: knowledge_base/<market>/<supplier>.md.
# Key = file name and log value; value = name shown to the customer.
# (A real deployment would look the supplier up from the booking number.)
SUPPLIER_NAMES = {"avis": "Avis", "sixt": "SIXT", "enterprise": "Enterprise"}
SUPPORTED_SUPPLIERS = list(SUPPLIER_NAMES)
DEFAULT_SUPPLIER = "avis"

# --- LLM provider settings ------------------------------------------------------
# Real keys are read from the environment. If they are absent, llm_client.py
# falls back to MockProvider automatically -- the whole project runs with
# zero API keys configured.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "gpt-4o-mini")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "claude-haiku-4-5")

LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "8"))
LLM_MAX_RETRIES_PER_PROVIDER = int(os.getenv("LLM_MAX_RETRIES_PER_PROVIDER", "2"))
LLM_RETRY_BACKOFF_SECONDS = float(os.getenv("LLM_RETRY_BACKOFF_SECONDS", "1.5"))

# --- Observability ---------------------------------------------------------------
# LangSmith tracing is mandatory, not optional. A missing or invalid key is
# caught at startup by observability/langsmith_setup.ensure_langsmith_ready().
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY", "")
# Project that is created at startup if it doesn't exist yet.
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "support-bot")
# LangSmith defaults to the US endpoint. EU-hosted accounts must point at
# this endpoint explicitly, or traces silently fail to authenticate.
LANGSMITH_ENDPOINT = os.getenv("LANGSMITH_ENDPOINT", "https://eu.api.smith.langchain.com")
# Always on. Kept as a name so existing code that checks it keeps working.
LANGSMITH_TRACING_ENABLED = True

# The LangSmith SDK reads these straight from the environment, so they are
# forced here rather than trusted to be present in .env. Both the current
# LANGSMITH_* and legacy LANGCHAIN_* names are set, since different SDK
# versions read different ones.
os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ.setdefault("LANGSMITH_ENDPOINT", LANGSMITH_ENDPOINT)
os.environ.setdefault("LANGCHAIN_ENDPOINT", LANGSMITH_ENDPOINT)
os.environ.setdefault("LANGSMITH_PROJECT", LANGSMITH_PROJECT)
os.environ.setdefault("LANGCHAIN_PROJECT", LANGSMITH_PROJECT)

# Rough per-1K-token cost table (USD) used for cost estimation in analytics.
# Update these to match your actual provider pricing -- these are placeholders.
MODEL_COST_PER_1K_TOKENS = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "claude-haiku-4-5": {"input": 0.0008, "output": 0.004},
    "mock-model": {"input": 0.0, "output": 0.0},
}

# --- Storage -----------------------------------------------------------------
DB_PATH = os.getenv("SUPPORT_BOT_DB_PATH", "support_bot.db")
FALLBACK_LOG_PATH = os.getenv("SUPPORT_BOT_FALLBACK_LOG", "fallback_log.jsonl")

# --- Compliance ----------------------------------------------------------------
# Text shown once per session so the bot always identifies itself as an AI.
AI_DISCLOSURE_TEXT = {
    "de": "Hinweis: Sie sprechen mit einem KI-gestützten Assistenten.",
    "es": "Aviso: Estás hablando con un asistente basado en IA.",
    "us": "Notice: You're chatting with an AI-powered assistant.",
}

# Topics that must always be escalated to a human, regardless of KB content.
# Kept simple/keyword-based here; swap for an LLM-based classifier later if needed.
ESCALATION_KEYWORDS = [
    "lawsuit", "legal action", "sue", "compensation claim",
    "accident", "injury", "insurance claim", "refund guarantee",
    "klage", "unfall", "verletzung",  # DE
    "demanda", "accidente", "indemnizacion",  # ES
]

KB_MATCH_MIN_SCORE = float(os.getenv("KB_MATCH_MIN_SCORE", "0.15"))
