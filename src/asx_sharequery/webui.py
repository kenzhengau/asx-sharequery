"""Launch the agent in Agent Framework's DevUI web chat window.

DevUI ships with Microsoft Agent Framework and serves a chat interface (plus an
OpenAI-compatible API) for a set of agents. It is a *sample app* rather than production
software, which is why it lives behind this project's optional ``web`` extra.

Run it with:
    uv sync --extra web
    uv run asx-sharequery-web
"""

from __future__ import annotations

import argparse
import secrets
import sys

from .agent import create_agent
from .config import ConfigError, load_settings
from .preflight import check_auth

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

_BANNER = """\
ASX Share Query  -  web chat window (Agent Framework DevUI)
  provider : {provider}
  model    : {model}
  endpoint : {base_url}
  api key  : {key}
  url      : http://{host}:{port}
  access   : {access}

Type in the browser instead of the terminal. Ctrl-C here stops the server.
Note DevUI is a sample app from Agent Framework, not production software.
{separator}"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asx-sharequery-web",
        description="Run the ASX share agent in Agent Framework's DevUI chat window.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to serve on (default {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            f"Interface to bind (default {DEFAULT_HOST}, localhost only). "
            "Use 0.0.0.0 to expose it to your network - only do that with auth on."
        ),
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the browser automatically.",
    )
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument(
        "--auth",
        action="store_true",
        help="Force DevUI's access token on even for a localhost bind.",
    )
    auth.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable the access token. Only allowed when bound to localhost.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``asx-sharequery-web`` console script."""
    args = _build_parser().parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error:\n  {exc}", file=sys.stderr)
        return 2

    # DevUI reports a failed model call as a raw provider payload inside its debug
    # panel, so check the token up front where the message can explain itself.
    if warning := check_auth(settings):
        print(f"\nWarning: {warning}\n", file=sys.stderr)

    try:
        from agent_framework.devui import serve
    except ImportError:
        print(
            "The web chat window needs the optional 'web' extra:\n"
            "  uv sync --extra web",
            file=sys.stderr,
        )
        return 2

    agent = create_agent(settings)

    exposed = args.host not in _LOCAL_HOSTS
    if exposed and args.no_auth:
        print(
            f"Refusing to bind {args.host} with --no-auth: that would expose this agent\n"
            "and your model API token to the network with no access control.",
            file=sys.stderr,
        )
        return 2

    # DevUI's browser UI has no way to supply the access token - it only documents the
    # token for direct API calls, and the server never prints it. Leaving auth on for a
    # localhost-only server therefore just breaks the chat window with a 401, so auth is
    # off locally and forced on as soon as the server is exposed.
    auth_enabled = exposed or args.auth
    auth_token = secrets.token_urlsafe(24) if auth_enabled else None

    print(
        _BANNER.format(
            provider=settings.provider_label or settings.provider,
            model=settings.model,
            base_url=settings.base_url,
            key=settings.redacted_key,
            host=args.host,
            port=args.port,
            access=(
                "access token required" if auth_enabled else "none (localhost only)"
            ),
            separator="-" * 68,
        )
    )
    if auth_token:
        print(f"Access token: {auth_token}\n")

    try:
        serve(
            entities=[agent],
            port=args.port,
            host=args.host,
            auto_open=not args.no_open,
            auth_enabled=auth_enabled,
            auth_token=auth_token,
        )
    except KeyboardInterrupt:
        print("\nStopped.")

    return 0


# Required for `python -m asx_sharequery.webui`, which the workspace task and the
# README's alternative launch command both use. Without it the module is imported and
# the interpreter exits 0 having done nothing at all - no banner, no server, no error.
if __name__ == "__main__":
    raise SystemExit(main())
