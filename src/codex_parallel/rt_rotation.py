"""Refresh-token persistence: the pool must always hold the LATEST RT.

ChatGPT-mode accounts rotate their refresh token on exchange (the response
carries a new RT and the old one may be single-use). If the hub loses the
newest RT, the account can only be recovered by a manual browser login —
exactly what the user warned about. So:

  - prepare_auth() extracts the RT from the secret (auth.json) it injected
    and returns it alongside
  - after a run, the scheduler hands any fresher RT back via
    TokenPool.update_secret(), which rewrites the 0600 secret file
  - codex itself refreshes in-sandbox CODEX_HOME; the runner reads the
    refreshed auth.json back after each run so rotation is captured even
    when codex did the refresh internally
"""

from __future__ import annotations

import json
from pathlib import Path


def extract_refresh_token(secret: str) -> str | None:
    """Pull the current RT out of a secret (auth.json JSON or bare RT)."""
    s = secret.strip()
    if not s.startswith("{"):
        return s or None
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        return None
    if isinstance(d, dict):
        return d.get("tokens", {}).get("refresh_token") or d.get("refresh_token")
    return None


def extract_account_id(secret: str) -> str | None:
    s = secret.strip()
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        return None
    if isinstance(d, dict):
        return d.get("tokens", {}).get("account_id")
    return None
