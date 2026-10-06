"""
observability/langsmith_setup.py

Responsibility: make sure LangSmith is usable BEFORE the first conversation
turn, and that the project traces are sent to actually exists.

Tracing is mandatory in this project. That means a missing key, a missing
SDK, or a rejected login must stop the bot at startup with a clear message,
not surface later as "why are there no traces?".

Why not a bare `try: create_project() / except Exception: "may already
exist"`: that pattern also swallows auth and wrong-region errors, which are
exactly the failures this check exists to catch. Here "already exists" is
checked explicitly, and everything else is reported as a real error.
"""
from __future__ import annotations

from typing import Optional

import config
from exceptions import TracingSetupError


def ensure_langsmith_ready(project_name: Optional[str] = None) -> str:
    """
    Verifies the LangSmith connection and creates the project if needed.
    Returns the project name. Raises TracingSetupError if LangSmith cannot
    be used.
    """
    project_name = project_name or config.LANGSMITH_PROJECT

    if not config.LANGSMITH_API_KEY:
        raise TracingSetupError(
            "LANGSMITH_API_KEY is not set. LangSmith tracing is required: "
            "add the key to your .env file (see .env.example)."
        )

    try:
        from langsmith import Client
        from langsmith.utils import LangSmithConflictError
    except ImportError as e:
        raise TracingSetupError(
            "The langsmith package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from e

    # Endpoint and key are passed explicitly so this check does not depend
    # on which env var names the installed SDK version happens to read.
    client = Client(api_url=config.LANGSMITH_ENDPOINT, api_key=config.LANGSMITH_API_KEY)

    try:
        if client.has_project(project_name):
            print(f"✅ LangSmith project '{project_name}' found.")
        else:
            client.create_project(project_name)
            print(f"✅ LangSmith project '{project_name}' created.")
    except LangSmithConflictError:
        # Another process created it between the check and the create.
        print(f"✅ LangSmith project '{project_name}' found.")
    except Exception as e:  # noqa: BLE001 -- any other failure means tracing is unusable
        raise TracingSetupError(
            f"Could not set up LangSmith project '{project_name}' at "
            f"{config.LANGSMITH_ENDPOINT}: {e}. Check that LANGSMITH_API_KEY "
            "is valid and that LANGSMITH_ENDPOINT matches your account's "
            "region (EU vs US)."
        ) from e

    return project_name
