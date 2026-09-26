"""End-to-end check: build the agent and ask it one question.

Requires a real model API token (``OPENAI_API_KEY``, or ``DEEPSEEK_API_KEY``) in the
environment or in ``.env``.

Usage:
    uv run python scripts/check_agent.py "What's happening with BHP?"
"""

from __future__ import annotations

import asyncio
import sys

from asx_sharequery.agent import create_agent
from asx_sharequery.config import ConfigError, load_settings
from asx_sharequery.preflight import check_auth

DEFAULT_QUESTION = "What's happening with BHP? Summarise the latest news."


async def main() -> int:
    question = " ".join(sys.argv[1:]).strip() or DEFAULT_QUESTION

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error:\n  {exc}", file=sys.stderr)
        return 2

    print(
        f"provider={settings.provider}  model={settings.model}  "
        f"endpoint={settings.base_url}  key={settings.redacted_key}"
    )
    if warning := check_auth(settings):
        print(f"\nWarning: {warning}\n", file=sys.stderr)

    print(f"\nYou: {question}\n")

    agent = create_agent(settings)

    print("Agent: ", end="", flush=True)
    async for chunk in agent.run(question, stream=True):
        if chunk.text:
            print(chunk.text, end="", flush=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
