"""
observability/tracing.py

Responsibility: trace every LLM call (latency, tokens, cost) with LangSmith.

Tracing is mandatory in this project, so there is no "no-op mode" here:
  - A missing API key or missing langsmith package raises TracingSetupError.
    main.py catches the same problem earlier, at startup, through
    observability/langsmith_setup.ensure_langsmith_ready().
  - All traces go to ONE project (config.LANGSMITH_PROJECT, the project
    that the startup check creates). The market is attached as a tag and
    as metadata, so you filter by market inside that project.
  - Each trace carries its content, not just its timing: the caller passes
    the inputs to trace_call() and records the result on the handle, so
    LangSmith shows the prompt, the reply, the model and the token counts.
  - EVERY conversation turn is traced, including turns that are escalated
    to a human before any model is called. flow_engine.py opens one
    "conversation_turn" trace per message and the model call nests inside.
  - If the LangSmith backend misbehaves in the middle of a conversation,
    the user's turn still completes, but the problem is logged as a
    warning. It is never swallowed silently.
"""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import config
from exceptions import TracingSetupError

logger = logging.getLogger(__name__)

_CLIENT = None


def _get_client():
    """
    Returns the shared LangSmith client, creating it on first use.

    Created lazily (not at import time) so that importing this module never
    fails with a raw traceback: main.py gets the chance to run the startup
    check first and print a clear message.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    if not config.LANGSMITH_API_KEY:
        raise TracingSetupError(
            "LANGSMITH_API_KEY is not set. LangSmith tracing is required: "
            "add the key to your .env file (see .env.example)."
        )
    try:
        from langsmith import Client
    except ImportError as e:
        raise TracingSetupError(
            "The langsmith package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from e

    # Endpoint and key are passed explicitly, rather than relying only on
    # env vars, since different SDK versions read different variable names.
    _CLIENT = Client(api_url=config.LANGSMITH_ENDPOINT, api_key=config.LANGSMITH_API_KEY)
    return _CLIENT


@dataclass
class TraceHandle:
    name: str
    market: str
    start_time: float
    metadata: dict
    # Filled in by record_result() / record_llm_result() and sent to
    # LangSmith when the block ends.
    _outputs: Optional[dict] = field(default=None, repr=False)
    _result_metadata: dict = field(default_factory=dict, repr=False)
    _result_tags: list = field(default_factory=list, repr=False)
    _usage: Optional[dict] = field(default=None, repr=False)
    _error: Optional[str] = field(default=None, repr=False)

    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def record_result(
        self,
        output: str,
        tags: Optional[list[str]] = None,
        error: Optional[str] = None,
        **metadata,
    ) -> None:
        """
        Call this inside the `with trace_call(...)` block to record what the
        step produced. `output` fills the Output column in LangSmith as
        plain text; `tags` show up as labels on the trace (e.g. "escalated").
        """
        self._outputs = {"output": output}
        self._result_tags = list(tags or [])
        self._result_metadata = dict(metadata)
        self._error = error

    def record_llm_result(
        self,
        text: str,
        provider: Optional[str],
        model: Optional[str],
        tokens_in: int = 0,
        tokens_out: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """
        Same as record_result(), for a model call: it also records the token
        counts and which provider/model answered.

        The reply is stored as a single assistant message and the token
        counts are reported separately, so the Output column shows the reply
        text rather than a JSON wrapper. ls_provider / ls_model_name are the
        metadata names LangSmith reads on an "llm" run to show model and cost.
        """
        self._outputs = {"role": "assistant", "content": text}
        self._usage = {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        }
        self._result_metadata = {
            "ls_provider": provider or "none",
            "ls_model_name": model or "none",
            "estimated_cost_usd": estimate_cost(model or "", tokens_in, tokens_out),
        }
        self._error = error


@contextlib.contextmanager
def trace_call(
    name: str,
    market: str,
    inputs: Optional[dict[str, Any]] = None,
    run_type: str = "chain",
    **metadata,
) -> Iterator[TraceHandle]:
    """
    Usage:
        with trace_call("conversation_turn", market="de",
                        inputs={"input": user_text}) as turn:
            ...
            with trace_call(
                "llm_call", market="de", run_type="llm",
                inputs={"messages": [{"role": "user", "content": prompt}]},
            ) as t:
                response = get_response(prompt)
                t.record_llm_result(response.text, response.provider_used, ...)
            ...
            turn.record_result(final_text, tags=["escalated"])

    Wraps the block in a LangSmith trace in the project
    config.LANGSMITH_PROJECT, tagged "market:<market>". `inputs` fills the
    Input column in LangSmith; record_result() / record_llm_result() fill
    the Output column.

    Calls can be nested: a trace_call opened inside another one becomes a
    child step of it, so one conversation turn is one row in LangSmith with
    the model call visible inside it.

    Errors raised by the code INSIDE the block are recorded on the trace
    and then re-raised unchanged, so callers see their own exception.
    """
    handle = TraceHandle(name=name, market=market, start_time=time.monotonic(), metadata=metadata)

    client = _get_client()  # raises TracingSetupError if tracing is not usable
    from langsmith.run_helpers import trace as langsmith_trace

    run = None
    run_tree = None
    try:
        run = langsmith_trace(
            name=name,
            run_type=run_type,
            inputs=inputs or {},
            project_name=config.LANGSMITH_PROJECT,
            tags=[f"market:{market}"],
            metadata={"market": market, **metadata},
            client=client,
        )
        run_tree = run.__enter__()
    except Exception as e:  # noqa: BLE001 -- backend problem, not a caller bug
        logger.warning("LangSmith trace '%s' could not be started: %s", name, e)
        run = None
        run_tree = None

    try:
        yield handle
    except BaseException as e:
        _close(run, name, type(e), e, e.__traceback__)
        raise
    else:
        _send_result(run_tree, handle)
        _close(run, name, None, None, None)


def _send_result(run_tree, handle: TraceHandle) -> None:
    """Attaches the recorded result to the run; failures are logged, never raised."""
    if run_tree is None or handle._outputs is None:
        return
    try:
        outputs = dict(handle._outputs)
        if handle._usage is not None:
            try:
                run_tree.set(usage_metadata=handle._usage)
            except (AttributeError, TypeError):
                # Older langsmith versions have no separate slot for token
                # usage; they read it from the outputs instead.
                outputs["usage_metadata"] = handle._usage
        if handle._result_tags:
            run_tree.add_tags(handle._result_tags)
        run_tree.end(
            outputs=outputs,
            error=handle._error,
            metadata=handle._result_metadata,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("LangSmith trace '%s': result could not be attached: %s", handle.name, e)


def _close(run, name: str, exc_type, exc, tb) -> None:
    """Ends the LangSmith run; a failure here is logged, never raised."""
    if run is None:
        return
    try:
        run.__exit__(exc_type, exc, tb)
    except Exception as e:  # noqa: BLE001
        logger.warning("LangSmith trace '%s' could not be closed: %s", name, e)


def estimate_cost(model_name: str, tokens_in: int, tokens_out: int) -> float:
    """
    Rough USD cost estimate using the pricing table in config.py.
    Returns 0.0 for unknown models rather than raising -- cost estimation
    is a nice-to-have for analytics, never something that should break
    the conversation if a new model isn't in the table yet.
    """
    pricing = config.MODEL_COST_PER_1K_TOKENS.get(model_name)
    if not pricing:
        return 0.0
    return (tokens_in / 1000) * pricing["input"] + (tokens_out / 1000) * pricing["output"]
