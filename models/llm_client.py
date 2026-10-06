"""
models/llm_client.py

Responsibility: get a text response from a language model, no matter what.

Design goals:
  1. Never let one provider's outage break the conversation -- retry the
     primary provider a few times, then fall back to a secondary provider,
     then degrade to a safe canned response with a human handoff.
  2. Keep providers swappable. MockProvider needs zero API keys and is the
     default, so this whole project runs out of the box. Real providers
     (OpenAI, Anthropic) are optional and only imported if configured.
  3. Every call returns a structured BotResponse -- callers never have to
     guess whether something failed.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from exceptions import (
    LLMTimeoutError,
    LLMRateLimitError,
    LLMAuthError,
    LLMProviderUnavailableError,
    AllProvidersFailedError,
)


@dataclass
class BotResponse:
    text: str
    provider_used: Optional[str]
    model_used: Optional[str]
    escalate: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    latency_seconds: float = 0.0
    error: Optional[str] = None


@dataclass
class ProviderResult:
    text: str
    tokens_in: int
    tokens_out: int


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
class BaseProvider:
    name = "base"
    model_name = "base-model"

    def call(self, prompt: str, timeout: float) -> ProviderResult:
        raise NotImplementedError


class MockProvider(BaseProvider):
    """
    Deterministic, offline provider used by default so the project runs
    without any API keys. Also lets you deliberately simulate failures
    (fail_rate) to prove the fallback path actually works.
    """

    name = "mock"
    model_name = "mock-model"

    def __init__(self, fail_rate: float = 0.0, always_fail: bool = False):
        self.fail_rate = fail_rate
        self.always_fail = always_fail

    def call(self, prompt: str, timeout: float) -> ProviderResult:
        if self.always_fail or random.random() < self.fail_rate:
            raise LLMTimeoutError(f"{self.name} provider simulated a timeout")

        # Very small canned "understanding" so the demo feels alive without
        # calling out to a real model.
        reply = (
            "Based on what I found: "
            f"{prompt.strip()[-300:]}\n\n"
            "(This is a mock response -- plug in a real provider in "
            "models/llm_client.py to replace this.)"
        )
        return ProviderResult(text=reply, tokens_in=len(prompt.split()), tokens_out=len(reply.split()))


class OpenAIProvider(BaseProvider):
    name = "openai"
    model_name = config.PRIMARY_MODEL

    def __init__(self):
        if not config.OPENAI_API_KEY:
            raise LLMProviderUnavailableError("OPENAI_API_KEY is not set")
        try:
            import openai  # noqa: F401  (imported lazily on purpose)
        except ImportError as e:
            raise LLMProviderUnavailableError("openai package not installed") from e
        self._openai = openai
        self._openai.api_key = config.OPENAI_API_KEY

    def call(self, prompt: str, timeout: float) -> ProviderResult:
        try:
            resp = self._openai.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                timeout=timeout,
            )
            text = resp.choices[0].message.content
            usage = getattr(resp, "usage", None)
            return ProviderResult(
                text=text,
                tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
                tokens_out=getattr(usage, "completion_tokens", 0) if usage else 0,
            )
        except Exception as e:  # noqa: BLE001 -- narrowed to specific SDK errors below
            msg = str(e).lower()
            if "rate limit" in msg:
                raise LLMRateLimitError(str(e)) from e
            if "timeout" in msg:
                raise LLMTimeoutError(str(e)) from e
            if "auth" in msg or "api key" in msg:
                raise LLMAuthError(str(e)) from e
            raise LLMTimeoutError(str(e)) from e  # treat unknown as retryable


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    model_name = config.FALLBACK_MODEL

    def __init__(self):
        if not config.ANTHROPIC_API_KEY:
            raise LLMProviderUnavailableError("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise LLMProviderUnavailableError("anthropic package not installed") from e
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def call(self, prompt: str, timeout: float) -> ProviderResult:
        try:
            resp = self._client.messages.create(
                model=self.model_name,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
                timeout=timeout,
            )
            text = "".join(block.text for block in resp.content if hasattr(block, "text"))
            usage = getattr(resp, "usage", None)
            return ProviderResult(
                text=text,
                tokens_in=getattr(usage, "input_tokens", 0) if usage else 0,
                tokens_out=getattr(usage, "output_tokens", 0) if usage else 0,
            )
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "rate limit" in msg or "overloaded" in msg:
                raise LLMRateLimitError(str(e)) from e
            if "timeout" in msg:
                raise LLMTimeoutError(str(e)) from e
            if "auth" in msg:
                raise LLMAuthError(str(e)) from e
            raise LLMTimeoutError(str(e)) from e


# --------------------------------------------------------------------------
# Provider chain construction
# --------------------------------------------------------------------------
def _build_provider_chain() -> list[BaseProvider]:
    """
    Builds [primary, fallback] using whatever is actually configured.
    Falls back to MockProvider for anything unavailable, so the chain is
    never empty and the project always runs.
    """
    chain: list[BaseProvider] = []
    for provider_cls in (OpenAIProvider, AnthropicProvider):
        try:
            chain.append(provider_cls())
        except LLMProviderUnavailableError:
            continue
    if not chain:
        chain = [MockProvider()]
    return chain


# --------------------------------------------------------------------------
# Public entry point: this is what the rest of the app calls.
# --------------------------------------------------------------------------
def get_response(
    prompt: str,
    providers: Optional[list[BaseProvider]] = None,
    timeout: float = None,
    max_retries_per_provider: int = None,
    backoff_seconds: float = None,
) -> BotResponse:
    """
    Tries each provider in order. For each provider, retries a small number
    of times on transient errors (timeout / rate limit) with exponential
    backoff before moving to the next provider. Auth errors are NOT retried
    (retrying a bad API key never helps) -- we skip straight to the next
    provider.

    If every provider fails, we degrade gracefully: return a safe, honest
    message and flag the conversation for human escalation. We never raise
    an unhandled exception up to the conversation layer.
    """
    timeout = timeout if timeout is not None else config.LLM_TIMEOUT_SECONDS
    max_retries_per_provider = (
        max_retries_per_provider
        if max_retries_per_provider is not None
        else config.LLM_MAX_RETRIES_PER_PROVIDER
    )
    backoff_seconds = (
        backoff_seconds if backoff_seconds is not None else config.LLM_RETRY_BACKOFF_SECONDS
    )
    providers = providers if providers is not None else _build_provider_chain()

    last_error: Optional[str] = None
    start = time.monotonic()

    for provider in providers:
        for attempt in range(1, max_retries_per_provider + 1):
            try:
                result = provider.call(prompt, timeout=timeout)
                return BotResponse(
                    text=result.text,
                    provider_used=provider.name,
                    model_used=provider.model_name,
                    escalate=False,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    latency_seconds=time.monotonic() - start,
                )
            except LLMAuthError as e:
                # Retrying won't fix bad credentials -- move to next provider.
                last_error = f"{provider.name} auth error: {e}"
                break
            except (LLMTimeoutError, LLMRateLimitError) as e:
                last_error = f"{provider.name} attempt {attempt} failed: {e}"
                if attempt < max_retries_per_provider:
                    time.sleep(backoff_seconds * attempt)  # exponential-ish backoff
                continue

    # Every provider (and every retry) failed. Degrade safely.
    return BotResponse(
        text=(
            "I'm having trouble reaching our systems right now. "
            "I've flagged this conversation for a team member to follow up with you."
        ),
        provider_used=None,
        model_used=None,
        escalate=True,
        latency_seconds=time.monotonic() - start,
        error=str(AllProvidersFailedError(last_error)),
    )
