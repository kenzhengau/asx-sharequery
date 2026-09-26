"""Allow ``python -m asx_sharequery``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
