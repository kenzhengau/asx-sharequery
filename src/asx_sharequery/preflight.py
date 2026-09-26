"""Startup sanity check that the configured model API accepts the configured token.

A rejected token reaches the chat window as a raw provider payload - a 401 JSON blob
wrapped in an exception - which says nothing about what to fix. Probing the provider once
at startup turns that into a plain-English warning, and recognises the common mistake of
pasting a ChatGPT / Codex *sign-in* token where a Platform API key belongs.

The check is deliberately advisory: a network failure is ignored rather than fatal, so
being offline never stops the app from starting.
"""

from __future__ import annotations

import httpx

from .config import PROVIDERS, Settings

TIMEOUT_SECONDS = 10.0

# Both supported providers serve GET /models behind the same bearer header, which makes
# it a cheap way to ask "do you accept this token?" without spending a completion.
_MODELS_PATH = "/models"


def check_auth(settings: Settings) -> str | None:
    """Probe the provider and describe any authentication problem in plain English.

    Args:
        settings: Resolved settings. Only ``base_url``, ``api_key`` and ``provider``
            are used.

    Returns:
        A warning to show the user, or ``None`` when the token works - or when the
        provider could not be reached, since that is not this check's business.
    """
    provider = PROVIDERS.get(settings.provider)
    key_var = provider.key_var if provider else "the API key"
    key_url = provider.key_url if provider else "your provider's key page"
    alternative = "deepseek" if settings.provider == "openai" else "openai"
    url = f"{settings.base_url}{_MODELS_PATH}"

    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return None

    if response.status_code == 200:
        return None

    if response.status_code == 401:
        # Two signals that the value is the wrong *kind* of credential: the provider says
        # so outright, or it is not shaped like a Platform key at all. Both supported
        # providers issue keys beginning with "sk-"; a ChatGPT / Codex sign-in token is a
        # session credential for a different, subscription-scoped service.
        enforcement = (
            "access_enforcement" in response.text or "no_matching_rule" in response.text
        )
        wrong_shape = not settings.api_key.startswith("sk-")

        if enforcement or wrong_shape:
            reason = (
                "the provider answered 'rejected_by_access_enforcement'"
                if enforcement
                else "the value does not start with 'sk-'"
            )
            return (
                f"{key_var} cannot be used here: {reason}.\n"
                "  A ChatGPT / Codex sign-in token does not work against the API - the\n"
                "  Codex CLI authenticates against a different, subscription-scoped\n"
                "  service, so the API refuses the credential.\n"
                f"  Fix: create a Platform key at {key_url},\n"
                f"  ...or run on the other provider:  LLM_PROVIDER={alternative}"
            )

        return (
            f"{key_var} was rejected (HTTP 401 Unauthorized).\n"
            "  Check the token is complete, current, and not truncated by the paste."
        )

    if response.status_code == 403:
        return (
            f"{key_var} was accepted but access was denied (HTTP 403 Forbidden).\n"
            "  The token may lack permission for this endpoint, or the account may be"
            " suspended."
        )

    if response.status_code == 429:
        return (
            f"{url} refused the request (HTTP 429): rate limited or out of credit.\n"
            "  Check the account's billing and quota."
        )

    if response.status_code == 404:
        return (
            f"{url} returned HTTP 404, so the endpoint looks wrong.\n"
            "  Check the base URL setting - it should be the API root, for example\n"
            "  https://api.openai.com/v1 - and that it has no trailing /models."
        )

    return f"{url} returned HTTP {response.status_code}: {response.text[:200]}"
