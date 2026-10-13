"""Failure attribution for sandbox/codex runs.

The health module consumes a CodexRunResult and decides what happens to the
token and the item:

  ┌──────────────────┬────────────────────────────────┬─────────────────────┐
  │ attribution      │ token action                   │ item action         │
  ├──────────────────┼────────────────────────────────┼─────────────────────┤
  │ account_dead     │ banned PERMANENTLY             │ retry on a NEW token│
  │ quota_5h         │ cooldown ~5h (account is fine) │ retry on a NEW token│
  │ quota_weekly     │ cooldown ~7d                   │ retry on a NEW token│
  │ quota_monthly    │ cooldown ~30d                  │ retry on a NEW token│
  │ transient        │ strike +1, cooldown 60s        │ retry SAME token    │
  │ timeout          │ strike +1                      │ retry SAME token    │
  │ failed (other)   │ strike +1                      │ retry SAME token    │
  │ 3 strikes        │ discarded (retired)            │ continue on NEW     │
  │ sandbox crash    │ strike +1                      │ retry SAME token    │
  └──────────────────┴────────────────────────────────┴─────────────────────┘

Quota ≠ ban: an account out of usage is healthy and returns automatically
when its window rolls over; a banned account never comes back. Both paths
release the token immediately and the item continues elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from .codex_runner import CodexRunResult

MAX_STRIKES = 3

QUOTA_KINDS = ("quota_5h", "quota_weekly", "quota_monthly")


@dataclass
class Attribution:
    """The decision the scheduler should act on for one failed run."""

    kind: str          # account_dead | quota_* | transient | timeout | failed
    swap_token: bool   # True → release + do NOT reuse; acquire a new one
    retire_token: bool # True → ban/discard the token outright
    cooldown_token: bool = False  # True → quota cooldown (not retired)
    retry_item: bool = True
    reason: str = ""


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
    if kind in QUOTA_KINDS:
        # usage window exhausted → park the token for the window, move on
        return Attribution(
            kind=kind,
            swap_token=True,
            retire_token=False,
            cooldown_token=True,
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
