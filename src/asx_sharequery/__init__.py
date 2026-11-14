"""ASX share query agent built on Microsoft Agent Framework + ChatGPT (OpenAI)."""

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"
__version__ = _VERSION_FILE.read_text(encoding="utf-8").strip()
