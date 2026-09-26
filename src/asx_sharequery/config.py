"""Configuration loading for the ASX share query agent.

Settings come from environment variables, optionally seeded from a ``.env`` file in the
project root. Nothing here has a hard-coded secret: the API token must be supplied by the
user.

The agent talks to any provider that implements the OpenAI *Chat Completions* API. OpenAI
- the ChatGPT API - is the default; DeepSeek is supported as an alternative. Which token
is present decides the provider, unless ``LLM_PROVIDER`` names one explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# src/asx_sharequery/config.py -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PROVIDER = "openai"

# A value that looks like a leftover placeholder in .env.example rather than a real token.
_PLACEHOLDER_PREFIXES = ("sk-your-key", "your-", "<", "changeme")


class ConfigError(RuntimeError):
    """Raised when the agent cannot be configured from the environment."""


def _read_token(name: str) -> str:
    """Return the token in ``name``, or ``""`` when unset or still a placeholder."""
    value = (os.getenv(name) or "").strip()
    if not value or value.lower().startswith(_PLACEHOLDER_PREFIXES):
        return ""
    return value


@dataclass(frozen=True)
class Provider:
    """A vendor that speaks the OpenAI Chat Completions API."""

    name: str
    label: str
    key_var: str
    base_url_var: str
    model_var: str
    default_base_url: str
    default_model: str
    key_url: str

    def token(self) -> str:
        """The configured API token, or ``""`` when it is missing."""
        return _read_token(self.key_var)

    def base_url(self) -> str:
        """The endpoint to call, overridable so any compatible host can be used."""
        raw = os.getenv(self.base_url_var) or self.default_base_url
        return raw.strip().rstrip("/")

    def model(self) -> str:
        """The model to ask for, overridable to pick a different price/size."""
        return (os.getenv(self.model_var) or self.default_model).strip()


PROVIDERS: dict[str, Provider] = {
    provider.name: provider
    for provider in (
        Provider(
            name="openai",
            label="ChatGPT (OpenAI)",
            key_var="OPENAI_API_KEY",
            base_url_var="OPENAI_BASE_URL",
            model_var="OPENAI_MODEL",
            default_base_url="https://api.openai.com/v1",
            # gpt-4o-mini is cheap, fast and - unlike the o-series and gpt-5 reasoning
            # models - accepts the plain Chat Completions request shape the framework's
            # OpenAIChatCompletionClient sends.
            default_model="gpt-4o-mini",
            key_url="https://platform.openai.com/api-keys",
        ),
        Provider(
            name="deepseek",
            label="DeepSeek",
            key_var="DEEPSEEK_API_KEY",
            base_url_var="DEEPSEEK_BASE_URL",
            model_var="DEEPSEEK_MODEL",
            default_base_url="https://api.deepseek.com/v1",
            default_model="deepseek-chat",
            key_url="https://platform.deepseek.com/api_keys",
        ),
    )
}


@dataclass(frozen=True)
class SharePointSettings:
    """App-only credentials and target for the SharePoint library reader."""

    tenant_id: str
    client_id: str
    client_secret: str
    site_url: str
    library: str

    @property
    def redacted_secret(self) -> str:
        """A safe-to-print form of the client secret."""
        if len(self.client_secret) <= 8:
            return "***"
        return f"{self.client_secret[:3]}...{self.client_secret[-3:]}"


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings for the agent."""

    api_key: str
    base_url: str
    model: str
    provider: str = DEFAULT_PROVIDER
    provider_label: str = ""
    sharepoint: SharePointSettings | None = None
    sharepoint_error: str | None = None

    @property
    def redacted_key(self) -> str:
        """A safe-to-print form of the API key."""
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


SHAREPOINT_VARS = (
    "SHAREPOINT_TENANT_ID",
    "SHAREPOINT_CLIENT_ID",
    "SHAREPOINT_CLIENT_SECRET",
    "SHAREPOINT_SITE_URL",
    "SHAREPOINT_LIBRARY",
)
"""The variables that switch the SharePoint reader on.

Public because callers that must *not* offer the feature need to name them - the
Streamlit front-end deletes them from the environment before loading settings, so a
hosted deployment anyone can reach cannot read documents even if the credentials are
dropped into its secret store by mistake.
"""


def _load_sharepoint() -> tuple[SharePointSettings | None, str | None]:
    """Read the SharePoint settings, tolerating the feature being switched off.

    Returns:
        ``(settings, problem)``. Both are ``None`` when SharePoint was never configured.
        When it is only partly configured, ``settings`` is ``None`` and ``problem`` says
        what is missing. Misconfiguration is deliberately not fatal: a typo in these
        variables should not stop you querying shares.
    """
    raw = {name: (os.getenv(name) or "").strip() for name in SHAREPOINT_VARS}
    if not any(raw.values()):
        return None, None

    missing = [name for name, value in raw.items() if not value]
    if missing:
        return None, (
            "the SharePoint integration is partially configured; missing "
            + ", ".join(missing)
        )

    return (
        SharePointSettings(
            tenant_id=raw["SHAREPOINT_TENANT_ID"],
            client_id=raw["SHAREPOINT_CLIENT_ID"],
            client_secret=raw["SHAREPOINT_CLIENT_SECRET"],
            site_url=raw["SHAREPOINT_SITE_URL"],
            library=raw["SHAREPOINT_LIBRARY"],
        ),
        None,
    )


def _resolve_provider(requested: str | None = None) -> Provider:
    """Choose a provider and make sure it has a usable API token.

    An explicit request (the ``provider`` argument or ``LLM_PROVIDER``) wins even when its
    token is missing, so a typo fails loudly instead of quietly using a different vendor.
    Otherwise the first provider with a token set is used.

    Args:
        requested: Provider name to use, or ``None`` to auto-detect.

    Raises:
        ConfigError: if the named provider is unknown, or no provider has a token.
    """
    explicit = (requested or os.getenv("LLM_PROVIDER") or "").strip().lower()

    if explicit:
        if explicit not in PROVIDERS:
            raise ConfigError(
                f"Unknown LLM_PROVIDER {explicit!r}.\n"
                "  Known providers: " + ", ".join(PROVIDERS)
            )
        provider = PROVIDERS[explicit]
        if not provider.token():
            raise ConfigError(
                f"LLM_PROVIDER={explicit} is set but {provider.key_var} is missing.\n"
                f"  Put your {provider.label} token in .env:  "
                f"{provider.key_var}=...\n"
                f"  Get one from {provider.key_url}"
            )
        return provider

    for provider in PROVIDERS.values():
        if provider.token():
            return provider

    raise ConfigError(
        "No model API token is set. Add one to .env:\n"
        f"  {PROVIDERS['openai'].key_var}=sk-...        "
        f"ChatGPT API token, from {PROVIDERS['openai'].key_url}\n"
        f"  {PROVIDERS['deepseek'].key_var}=sk-...      "
        f"alternative, from {PROVIDERS['deepseek'].key_url}\n"
        f"  ...or export it:  export {PROVIDERS['openai'].key_var}=sk-..."
    )


def load_settings(
    *, env_file: Path | None = None, provider: str | None = None
) -> Settings:
    """Load settings from the environment, after seeding from ``.env``.

    Args:
        env_file: ``.env`` path to read. Defaults to the one in the project root.
        provider: Force a provider (``"openai"`` or ``"deepseek"``) instead of letting the
            available tokens decide.

    Raises:
        ConfigError: if no provider has a usable API token.
    """
    load_dotenv(dotenv_path=env_file or PROJECT_ROOT / ".env")

    chosen = _resolve_provider(provider)
    sharepoint, sharepoint_error = _load_sharepoint()

    return Settings(
        api_key=chosen.token(),
        base_url=chosen.base_url(),
        model=chosen.model(),
        provider=chosen.name,
        provider_label=chosen.label,
        sharepoint=sharepoint,
        sharepoint_error=sharepoint_error,
    )
