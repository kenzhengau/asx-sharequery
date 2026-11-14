"""Streamlit chat front-end for the ASX share agent.

This is the app that Streamlit Community Cloud runs. It is deliberately thin: the agent,
the tools and the answer formatting all live in ``src/asx_sharequery``, so the CLI, the
DevUI window and this page answer questions the same way. What is added here is only what
a browser needs - reading secrets, holding one conversation per browser session, and
painting the agent's streaming updates as they arrive.

Run it locally:
    uv sync --extra streamlit
    uv run streamlit run streamlit_app/app.py

On Community Cloud the entrypoint is ``streamlit_app/app.py`` and the dependency file is
``streamlit_app/requirements.txt``. See DEPLOY_STREAMLIT_CLOUD.md for the deploy steps.

Two things here are deliberate, and worth knowing before changing them:

* **Document reading is off.** ``read_sharepoint_library`` is not attached, and the
  SharePoint credentials are stripped from the environment, so the app cannot read them
  even if they are configured. A public URL has no way to tell visitors apart, and that
  tool reads an organisation's private document library on the app registration's behalf.
* **The app spends its owner's model API key.** Every question from every visitor is
  billed to the key in the secret store, which is why there is a per-session cap.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import streamlit as st

# The project is never installed into the Cloud environment: Community Cloud builds an
# environment from the requirements file next to this entrypoint, and installing the
# project from there would mean an editable install reaching out of this folder for the
# repo's pyproject.toml. Putting src/ on the path is one line, cannot fail during the
# build, and is obvious to debug if it ever needs revisiting.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_framework import Agent, AgentSession  # noqa: E402

from asx_sharequery import __version__  # noqa: E402
from asx_sharequery.agent import create_agent  # noqa: E402
from asx_sharequery.config import (  # noqa: E402
    SHAREPOINT_VARS,
    ConfigError,
    Settings,
    load_settings,
)
from asx_sharequery.preflight import check_auth  # noqa: E402

APP_TITLE = "ASX Share Query"
APP_VERSION = f"v{__version__}"

# A speed bump, not a security control: a visitor can start a new browser session and get
# a fresh allowance. The real spend control is a hard limit set on the provider account,
# because this app is public and it bills the owner's key. See the deploy guide.
MAX_TURNS_PER_SESSION = 25

_EXAMPLES = (
    "What's happening with BHP?",
    "Summarise the latest news for Commonwealth Bank",
    "How is CSL trading today?",
    "Any announcements from WES this week?",
)

_DISCLAIMER = (
    "General information from public ASX data - a delayed snapshot rather than a live "
    "feed, and not financial advice."
)

st.set_page_config(page_title=APP_TITLE, page_icon="📈", layout="centered")
st.caption(f"{APP_TITLE} {APP_VERSION}")


# --------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------


def _seed_environment_from_secrets() -> None:
    """Copy stored Streamlit secrets into the environment, where config.py reads them.

    Community Cloud has no ``.env`` file, so on a deployment the secret store is the only
    place the model key can live. A real environment variable wins over a stored secret,
    which is what lets ``OPENAI_API_KEY=... uv run streamlit run ...`` work locally without
    a stray ``secrets.toml`` silently overriding an explicit value.
    """
    try:
        stored = dict(st.secrets)
    except Exception:  # noqa: BLE001 - having no secrets configured is not an error
        return

    for name, value in stored.items():
        if isinstance(value, str) and value.strip() and not os.environ.get(name):
            os.environ[name] = value.strip()


def _disable_sharepoint() -> None:
    """Take the SharePoint credentials out of the environment.

    This deployment answers from public data, so the tool that reads an organisation's own
    document library is not attached - and its credentials are removed here as well. The
    two are belt and braces on purpose: a public app cannot tell its visitors apart, so the
    tool would hand the library's contents to anyone who asked, and deleting the variables
    means a credential left in the secret store by mistake cannot switch it back on.
    """
    for name in SHAREPOINT_VARS:
        os.environ.pop(name, None)


# --------------------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _agent() -> tuple[Settings, Agent]:
    """Build the settings and the agent, once for the whole server process.

    Cached because Streamlit re-runs this entire file on every interaction: without the
    cache, each keystroke would build a new model client. Sharing one agent across
    visitors is safe because every piece of conversation state lives on the
    ``AgentSession``, which is created per browser session - two visitors never share a
    transcript.
    """
    settings = load_settings()
    return settings, create_agent(settings, include_sharepoint=False)


def _reset_conversation(agent: Agent) -> None:
    """Start a new conversation: a new agent session, an empty transcript, zero turns."""
    st.session_state.agent_session = agent.create_session()
    st.session_state.history = []
    st.session_state.turns = 0


async def _stream_turn(
    agent: Agent, session: AgentSession, prompt: str
) -> AsyncIterator[tuple[str, str]]:
    """Yield the events of one agent run as they arrive.

    ``("text", prose)`` carries a chunk of the answer. ``("tool", name)`` carries the name
    of a tool the model just decided to call: ``AgentResponseUpdate.text`` holds the prose
    and ``.contents`` holds every content item in the chunk. Surfacing those is this
    page's equivalent of the tool panel in the DevUI window.

    Tool calls have to be reassembled rather than read off each chunk. Observed against
    DeepSeek, one call to ``get_share_research`` arrives as eleven ``function_call`` items
    sharing a single ``call_id``: the first names the tool, the next ten stream the
    arguments and carry an empty name. Reporting per item would print
    "get_share_research, a tool, a tool, ...", so a call is reported once, by id, and only
    when it names a tool.
    """
    announced: set[str] = set()

    async for update in agent.run(prompt, stream=True, session=session):
        if update.text:
            yield "text", update.text

        for content in getattr(update, "contents", None) or ():
            if getattr(content, "type", None) != "function_call":
                continue

            name = getattr(content, "name", None)
            call_id = getattr(content, "call_id", None)
            if not name or (call_id is not None and call_id in announced):
                continue
            if call_id is not None:
                announced.add(call_id)
            yield "tool", name


def _as_markdown(text: str) -> str:
    """Escape dollar signs, so prices are not typeset as maths.

    Streamlit renders whatever sits between a pair of ``$`` as KaTeX. Every answer from
    this agent quotes prices - "A$0.53", "A$0.53/A$0.55" - so a single line containing two
    of them collapses into mangled maths: "up A$0.53, a gain of 5.0 cents or 10.4%" really
    did render as `0.53, againof5.0centsor10.4`. Escaping is the right fix here because the
    agent quotes currency, it does not typeset equations. Already-escaped dollars are left
    alone so a rendered answer is not double-escaped when it is drawn again from history.
    """
    return re.sub(r"(?<!\\)\$", lambda _: r"\$", text)


def _stream_answer(
    agent: Agent, session: AgentSession, prompt: str
) -> tuple[str, list[str]]:
    """Answer one question on screen, streaming it, and say what it took to answer.

    Returns:
        ``(reply, tools)`` - the finished answer, and the distinct tool names the model
        called while producing it, in call order. ``reply`` is empty when the run failed,
        which is the signal not to record a turn.
    """
    # The tool panel comes first because it is created first, and the answer gets a slot
    # after it so the prose paints underneath the panel rather than above it.
    with st.status("Working...", expanded=True) as status:
        activity = st.caption("Contacting the ASX...")
    answer_slot = st.empty()

    tools: list[str] = []
    prose: list[str] = []

    def called() -> list[str]:
        """The distinct tools called so far, keeping the order they were called in."""
        return list(dict.fromkeys(tools))

    async def drive() -> None:
        async for kind, payload in _stream_turn(agent, session, prompt):
            if kind == "text":
                prose.append(payload)
                answer_slot.markdown(_as_markdown("".join(prose)))
            else:
                tools.append(payload)
                activity.caption(
                    "Calling " + ", ".join(f"`{name}`" for name in called()) + "..."
                )

    try:
        # Streamlit runs this script in its own thread, which has no event loop, so there
        # is nothing here for asyncio.run to collide with. One loop per answer keeps the
        # framework's async API usable from a synchronous app without a background thread.
        asyncio.run(drive())
    except Exception as exc:  # noqa: BLE001 - report it, keep the app usable
        status.update(label="The run failed", state="error", expanded=True)
        st.error(f"{type(exc).__name__}: {exc}")
        return "", called()

    reply = "".join(prose)
    if reply:
        # One clean render at the end, in case a chunk split a markdown construct.
        answer_slot.markdown(_as_markdown(reply))
    else:
        answer_slot.info("The agent finished without producing an answer.")

    called_tools = called()
    status.update(
        label=(
            f"Used {len(tools)} tool call{'s' if len(tools) != 1 else ''}"
            if tools
            else "Answered without tools"
        ),
        state="complete",
        expanded=False,
    )
    return reply, called_tools


def _answer(agent: Agent, prompt: str) -> None:
    """Answer one question and record both halves of the exchange in the transcript."""
    st.session_state.turns += 1
    st.session_state.history.append({"role": "user", "content": prompt, "tools": []})

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        reply, tools = _stream_answer(agent, st.session_state.agent_session, prompt)

    # A failed run is shown in the bubble but not stored, so it is not replayed as
    # context to the model on the next question.
    if reply:
        st.session_state.history.append(
            {"role": "assistant", "content": reply, "tools": tools}
        )


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


def _render_sidebar(settings: Settings, agent: Agent) -> None:
    """Describe what this deployment is, and offer the ways to move on from this chat."""
    with st.sidebar:
        st.subheader("This deployment")
        st.markdown(
            "```text\n"
            f"provider : {settings.provider_label or settings.provider}\n"
            f"model    : {settings.model}\n"
            f"url      : {settings.base_url}\n"
            f"api key  : {settings.redacted_key}\n"
            "```"
        )
        st.caption(
            "Public ASX data and Google News headlines only. Reading your own documents "
            "is switched off here."
        )

        if st.session_state.get("auth_warning"):
            st.warning(st.session_state.auth_warning)

        st.divider()
        if st.button("New conversation"):
            _reset_conversation(agent)
            st.rerun()

        st.divider()
        st.markdown("**Try asking**")
        for example in _EXAMPLES:
            if st.button(example, key=f"example::{example}"):
                st.session_state.queued_prompt = example

        st.divider()
        remaining = max(0, MAX_TURNS_PER_SESSION - st.session_state.turns)
        st.caption(
            f"{remaining} of {MAX_TURNS_PER_SESSION} questions left in this session."
        )
        st.caption(_DISCLAIMER)


def _render_config_error(exc: ConfigError) -> None:
    """Explain a missing or rejected model key, on an app anyone might open."""
    st.error("This app has no usable model API key, so it cannot answer questions yet.")
    st.markdown(
        "The deployment's owner has to add one:\n\n"
        "1. Open the app's **Settings -> Secrets** in the Streamlit Community Cloud "
        "workspace.\n"
        '2. Add `OPENAI_API_KEY = "sk-..."` - a Platform API key from '
        "https://platform.openai.com/api-keys, not a ChatGPT sign-in token.\n"
        "3. Save, then reboot the app.\n\n"
        "Running this locally instead? Put the same line in `.env`, or in "
        "`.streamlit/secrets.toml`."
    )
    with st.expander("What the agent reported"):
        st.code(str(exc), language="text")


def main() -> None:
    """Draw the chat page and handle one question."""
    _seed_environment_from_secrets()
    _disable_sharepoint()

    st.title(APP_TITLE)
    st.caption(
        "Ask about any company listed on the Australian Securities Exchange. The agent "
        "fetches the price, the company's own announcements and recent media coverage, "
        "then explains what is going on."
    )

    try:
        settings, agent = _agent()
    except ConfigError as exc:
        _render_config_error(exc)
        return

    if "agent_session" not in st.session_state:
        _reset_conversation(agent)

    # Once per browser session, not once per re-run: this is a network round-trip to the
    # provider, and it exists to turn a raw 401 into a sentence that says what to fix.
    if "auth_warning" not in st.session_state:
        with st.spinner("Checking the model API key..."):
            st.session_state.auth_warning = check_auth(settings)

    _render_sidebar(settings, agent)

    for message in st.session_state.history:
        with st.chat_message(message["role"]):
            st.markdown(_as_markdown(message["content"]))
            if message["tools"]:
                st.caption(
                    "Tools used: " + ", ".join(f"`{name}`" for name in message["tools"])
                )

    typed = st.chat_input("Ask about an ASX-listed share...")
    # The sidebar's example buttons cannot fill the chat box, so they queue instead; this
    # consumes the queue exactly once, on the run that the button triggered.
    prompt = typed or st.session_state.pop("queued_prompt", None)

    if not prompt:
        return

    if st.session_state.turns >= MAX_TURNS_PER_SESSION:
        st.warning(
            f"This session has reached its limit of {MAX_TURNS_PER_SESSION} questions. "
            "Use **New conversation** in the sidebar to carry on."
        )
        return

    _answer(agent, prompt)


# Streamlit executes this file top to bottom on every interaction, so the entry point is a
# plain call. The __main__ guard the other entry points use would do nothing here.
main()
