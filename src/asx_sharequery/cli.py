"""Interactive command-line chat loop for the ASX share agent."""

from __future__ import annotations

import argparse
import asyncio
import sys

from agent_framework import Agent, AgentSession

from .agent import create_agent
from .config import PROJECT_ROOT, ConfigError, Settings, load_settings
from .preflight import check_auth

_BANNER = """\
ASX Share Query  -  Microsoft Agent Framework
  provider  : {provider}
  model     : {model}
  endpoint  : {base_url}
  api key   : {key}

Ask about any ASX-listed share, for example:
  "What's happening with BHP?"
  "Summarise the latest news for Commonwealth Bank"
  "How is CSL trading today?"

Type 'exit' (or press Ctrl-D) to quit.
{separator}"""

_EXIT_COMMANDS = {"exit", "quit", ":q", "bye"}

_NO_STDIN_HELP = """\
No interactive input was available, so there was nothing to type into.

This happens when the program is started by a tool or a task rather than typed into
a terminal panel, or when its input is redirected from an empty source.

To chat interactively:
  1. Open the terminal panel with Ctrl+`  (or View > Terminal)
  2. Click + to create a new terminal
  3. cd {project}
  4. uv run asx-sharequery

To ask a single question instead:
  uv run asx-sharequery "What is happening with BHP?"
"""


def _provider_line(settings: Settings) -> str:
    """One line naming the provider that will answer, for a non-interactive run.

    The interactive banner already says this, but a one-shot run prints no banner and its
    stdout is often piped, so this line goes to stderr.
    """
    return (
        f"Using {settings.provider_label or settings.provider} "
        f"({settings.model}) at {settings.base_url}, key {settings.redacted_key}"
    )


async def _ask(agent: Agent, session: AgentSession, question: str) -> None:
    """Send one question to the agent and stream the reply to stdout."""
    print("\nAgent: ", end="", flush=True)
    try:
        async for chunk in agent.run(question, stream=True, session=session):
            if chunk.text:
                print(chunk.text, end="", flush=True)
    except Exception as exc:  # keep the session alive on a transient failure
        print(f"\n[error] {exc.__class__.__name__}: {exc}", file=sys.stderr)
    print()


async def _run(questions: list[str]) -> int:
    """Run the one-shot questions, or the interactive loop when none were given."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error:\n  {exc}", file=sys.stderr)
        return 2

    # Cheap one-off probe, so a bad token is explained here instead of surfacing later
    # as a raw 401 payload in the middle of an answer.
    if warning := check_auth(settings):
        print(f"\nWarning: {warning}\n", file=sys.stderr)

    agent = create_agent(settings)
    session = agent.create_session()

    if questions:
        print(_provider_line(settings), file=sys.stderr)
        for question in questions:
            print(f"\nYou: {question}")
            await _ask(agent, session, question)
        return 0

    print(
        _BANNER.format(
            provider=settings.provider_label or settings.provider,
            model=settings.model,
            base_url=settings.base_url,
            key=settings.redacted_key,
            separator="-" * 68,
        )
    )

    answered = 0
    while True:
        try:
            # input() is blocking, so keep it off the event loop.
            question = (await asyncio.to_thread(input, "\nYou: ")).strip()
        except KeyboardInterrupt:
            print()
            break
        except EOFError:
            # Either Ctrl-D, or a terminal that has no interactive input at all.
            print()
            if answered == 0:
                print(_NO_STDIN_HELP.format(project=PROJECT_ROOT), file=sys.stderr)
                return 3
            break

        if not question:
            continue
        if question.lower() in _EXIT_COMMANDS:
            break

        await _ask(agent, session, question)
        answered += 1

    print("Bye.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``asx-sharequery`` console script."""
    parser = argparse.ArgumentParser(
        prog="asx-sharequery",
        description=(
            "Chat with an agent about ASX-listed shares "
            "(Microsoft Agent Framework + ChatGPT / OpenAI)."
        ),
    )
    parser.add_argument(
        "question",
        nargs="*",
        help=(
            "Ask this question, print the answer, then exit. "
            "Omit it to start the interactive chat loop."
        ),
    )
    args = parser.parse_args(argv)
    question = " ".join(args.question).strip()

    try:
        return asyncio.run(_run([question] if question else []))
    except KeyboardInterrupt:
        return 130
