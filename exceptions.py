"""
exceptions.py
One place for every custom exception in the project. Having a shared,
specific exception hierarchy is what makes step-by-step error handling
possible: each module catches exactly what it expects and lets everything
else bubble up rather than swallowing unknown errors silently.
"""


class SupportBotError(Exception):
    """Base class for every error raised inside this project."""


# --- LLM layer -----------------------------------------------------------
class LLMError(SupportBotError):
    """Base class for anything that goes wrong calling a language model."""


class LLMTimeoutError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMAuthError(LLMError):
    pass


class LLMProviderUnavailableError(LLMError):
    """Raised when a provider's SDK isn't installed / configured."""


class AllProvidersFailedError(LLMError):
    """Raised when both primary and fallback providers have failed."""


# --- Knowledge base layer -------------------------------------------------
class KnowledgeBaseError(SupportBotError):
    pass


class KBLoadError(KnowledgeBaseError):
    """Raised when a market's KB files can't be read from disk."""


class KBGapError(KnowledgeBaseError):
    """Raised when no KB entry scores above the minimum match threshold.
    This is not a bug -- it's a *signal* that should be logged and fed
    into the analytics/gap-detection loop, not just hidden from the user.
    """


# --- Compliance layer -------------------------------------------------------
class ComplianceCheckError(SupportBotError):
    """Raised when the compliance rule engine itself fails to run.
    Callers must treat this as 'fail closed' -> escalate to a human.
    """


# --- Observability layer -----------------------------------------------------
class LoggingError(SupportBotError):
    """Raised when the primary logging backend (SQLite) fails.
    Callers should fall back to the flat-file logger and continue --
    a logging failure must never break the user-facing conversation.
    """


class TracingSetupError(SupportBotError):
    """Raised at startup when LangSmith is not usable: missing API key,
    SDK not installed, or the service rejected / could not be reached.
    Tracing is mandatory, so callers should stop the bot with a clear
    message instead of continuing untraced.
    """
