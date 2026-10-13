from __future__ import annotations

import pytest

from codex_parallel.attribution import MAX_STRIKES, attribute
from codex_parallel.codex_runner import CodexRunResult


def _res(kind: str, **kw) -> CodexRunResult:
    base = dict(ok=False, exit_code=1, auth_failed=False, timed_out=False)
    base.update(kw)
    if kind == "account_dead":
        base["auth_failed"] = True
    if kind == "timeout":
        base["timed_out"] = True
    if kind == "transient":
        base["transient"] = True
    return CodexRunResult(**base)


class TestAttribution:
    def test_account_dead_swaps_and_retires(self):
        att = attribute(_res("account_dead", error="401 Unauthorized"), token_strikes=0)
        assert att.swap_token and att.retire_token and att.retry_item
        assert att.kind == "account_dead"

    def test_transient_first_failure_keeps_token(self):
        att = attribute(_res("transient"), token_strikes=0)
        assert not att.swap_token and not att.retire_token and att.retry_item

    def test_transient_second_failure_keeps_token(self):
        att = attribute(_res("transient"), token_strikes=1)
        assert not att.swap_token and not att.retire_token

    def test_third_strike_discards(self):
        att = attribute(_res("transient"), token_strikes=MAX_STRIKES - 1)
        assert att.swap_token and att.retire_token
        assert "x3" in att.reason

    def test_timeout_is_transient_path(self):
        att = attribute(_res("timeout"), token_strikes=0)
        assert not att.swap_token

    def test_generic_failure_is_transient_path(self):
        att = attribute(_res("failed"), token_strikes=0)
        assert not att.swap_token

    def test_success_rejected(self):
        with pytest.raises(ValueError):
            attribute(_res("ok", ok=True), token_strikes=0)


class TestMarkerClassification:
    def test_401_detected(self):
        from codex_parallel.codex_runner import CodexRunner

        assert CodexRunner._account_dead("Error: 401 unauthorized")
        assert CodexRunner._account_dead("HTTP 403 Forbidden")
        assert CodexRunner._account_dead("account suspended by provider")

    def test_429_is_quota_not_dead_or_transient(self):
        from codex_parallel.codex_runner import CodexRunner

        assert not CodexRunner._account_dead("429 Too Many Requests")
        # 429 now maps to the 5h quota window (usage exhausted), not transient
        assert CodexRunner._quota_window("429 Too Many Requests") == "5h"
        assert CodexRunner._transient("connection reset by peer")
        assert not CodexRunner._transient("some random parse error")

    def test_failure_kind(self):
        assert _res("account_dead").failure_kind == "account_dead"
        assert _res("timeout").failure_kind == "timeout"
        assert _res("transient").failure_kind == "transient"
        assert _res("failed").failure_kind == "failed"
        assert _res("ok", ok=True).failure_kind == "done"
