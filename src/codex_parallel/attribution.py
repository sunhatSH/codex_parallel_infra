"""Failure attribution for sandbox/codex runs.

The health module consumes a CodexRunResult and decides what happens to the
token and the item:

  ┌─────────────────┬──────────────────────────┬─────────────────────────┐
  │ attribution     │ token action             │ item action             │
  ├─────────────────┼──────────────────────────┼─────────────────────────┤
  │ account_dead    │ banned (never re-handed) │ retry on a NEW token    │
  │ transient       │ strike +1, cooldown 60s  │ retry SAME token        │
  │ timeout         │ strike +1                │ retry SAME token        │
  │ failed (other)  │ strike +1                │ retry SAME token        │
  │ 3 strikes       │ discarded (retired)      │ continue on a NEW token │
  │ sandbox crash   │ strike +1                │ retry SAME token        │
  └─────────────────┴──────────────────────────┴─────────────────────────┘

Strikes live on the Token row (fail_count); the "3 strikes" check uses the
in-flight counter so a token already carrying old strikes discards sooner —
both views must agree, so we reset fail_count on successful use.
"""

from __future__ import annotations

from dataclasses import dataclass

from .codex_runner import CodexRunResult

MAX_STRIKES = 3


@dataclass
class Attribution:
    """The decision the scheduler should act on for one failed run."""

    kind: str          # account_dead | transient | timeout | failed
    swap_token: bool   # True → release + do NOT reuse; acquire a new one
    retire_token: bool # True → ban/discard the token outright
    retry_item: bool   # True → item goes back into rotation (same or new token)
    reason: str


def attribute(res: CodexRunResult, *, token_strikes: int) -> Attribution:
    """Decide the token/item policy for one failed run.

    `token_strikes` = failures already charged to the CURRENT token for this
    item; the 3-strike counter is per (item, token): a swap resets it, and a
    success resets the token's global fail_count.
    """
    kind = res.failure_kind
    if kind == "account_dead":
        return Attribution(
            kind=kind,
            swap_token=True,
            retire_token=True,
            retry_item=True,
            reason=res.error or kind,
        )
    if kind == "done":
        raise ValueError("cannot attribute a successful run")
    # transient / timeout / plain failure: same token, unless strikes run out
    strikes = token_strikes + 1
    if strikes >= MAX_STRIKES:
        return Attribution(
            kind=kind,
            swap_token=True,
            retire_token=True,
            retry_item=True,
            reason=f"{kind} x{MAX_STRIKES}",
        )
    return Attribution(
        kind=kind,
        swap_token=False,
        retire_token=False,
        retry_item=True,
        reason=kind,
    )
