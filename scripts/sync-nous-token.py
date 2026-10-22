#!/usr/bin/env python3
"""Sync the Nous Portal OAuth token from Hermes auth to the Nexus key file.

Thin CLI over ``nexus.nous_token.sync_nous_token``. Prefer
``./nexus start``, which refreshes on start and keeps refreshing while
the door runs. This script remains for operators who want a one-shot copy.

Usage:
    python3 scripts/sync-nous-token.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from nexus.nous_token import sync_nous_token


def main() -> int:
    result = sync_nous_token()
    stream = sys.stdout if result.ok else sys.stderr
    print(result.message, file=stream)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
