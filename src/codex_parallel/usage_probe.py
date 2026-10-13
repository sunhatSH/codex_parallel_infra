"""Account usage (quota window) probing against the Codex backend.

After each completed task the hub asks the account how much of its usage
windows remains. Policy (user's spec): if the tightest active window has
less than 1/50 (2%) remaining, treat it as exhausted and park the token
for that window — same cooldown path as an explicit 429 usage-limit error.

Endpoint (reverse-engineered from codex_cli_rs 0.143 traffic):
  GET https://chatgpt.com/backend-api/codex/usage
  Authorization: Bearer <access_token>
  chatgpt-account-id: <account_id>

Response shape (prolite plan, live-verified 2026-10-13):
  rate_limit.primary_window = {
    used_percent, limit_window_seconds, reset_after_seconds, reset_at }
  limit_window_seconds: 604800 = weekly (7d). 5h windows report 18000.
  secondary_window: same shape or null.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
QUOTA_FLOOR_PCT = 2.0  # <1/50 of the window remaining → treat as exhausted

# window classification by duration
WINDOW_BY_SECONDS = {
    5 * 3600: "5h",
    18000: "5h",
    7 * 24 * 3600: "weekly",
    604800: "weekly",
    30 * 24 * 3600: "monthly",
    2592000: "monthly",
}


class UsageProbeError(Exception):
    """Could not read usage (network / auth / unexpected shape)."""


@dataclass
class UsageWindows:
    """Parsed usage state; remaining_pct per active window."""

    windows: list[dict]  # [{"window": "5h"|"weekly"|"monthly", "used_pct": float, "reset_after_s": int}]
    limit_reached: bool = False

    def floor_hit(self, floor_pct: float = QUOTA_FLOOR_PCT) -> str | None:
        """Window whose remaining share is below floor (or already capped),
        or None when everything has headroom."""
        for w in self.windows:
            remaining = 100.0 - w["used_pct"]
            if w["used_pct"] >= 100.0 or remaining < floor_pct:
                return w["window"]
        return None


def _classify_window(limit_window_seconds: int) -> str:
    w = WINDOW_BY_SECONDS.get(limit_window_seconds)
    if w:
        return w
    # unknown duration → bucket by length
    if limit_window_seconds <= 6 * 3600:
        return "5h"
    if limit_window_seconds <= 8 * 24 * 3600:
        return "weekly"
    return "monthly"


def probe_usage(access_token: str, account_id: str, *, proxy: str | None = None,
                timeout_s: int = 45) -> UsageWindows:
    """Fetch the account's usage windows. Raises UsageProbeError on failure."""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "codex_cli_rs/0.143.0",
            "chatgpt-account-id": account_id,
            "originator": "codex_cli_rs",
        },
    )
    try:
        with opener.open(req, timeout=timeout_s) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise UsageProbeError(f"HTTP {e.code}: {e.read().decode()[:200]}") from e
    except Exception as e:  # noqa: BLE001
        raise UsageProbeError(f"network: {e}") from e

    rl = d.get("rate_limit") or {}
    out = []
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key)
        if not w:
            continue
        secs = w.get("limit_window_seconds") or 0
        used = float(w.get("used_percent") or 0)
        out.append(
            {
                "window": _classify_window(secs),
                "used_pct": used,
                "reset_after_s": w.get("reset_after_seconds") or 0,
            }
        )
    return UsageWindows(windows=out, limit_reached=bool(rl.get("limit_reached")))
