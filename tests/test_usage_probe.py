"""Usage-floor cooldown tests: <1/50 remaining → window cooldown."""

from __future__ import annotations

import pytest

from codex_parallel.usage_probe import UsageWindows, _classify_window, probe_usage


def _win(window, used, reset=3600):
    return {"window": window, "used_pct": used, "reset_after_s": reset}


class TestFloorHit:
    def test_plenty_left(self):
        u = UsageWindows(windows=[_win("weekly", 10.0)])
        assert u.floor_hit() is None

    def test_below_floor(self):
        # used 98.5% → remaining 1.5% < 2% floor
        u = UsageWindows(windows=[_win("weekly", 98.5)])
        assert u.floor_hit() == "weekly"

    def test_exactly_floor_is_ok(self):
        u = UsageWindows(windows=[_win("5h", 98.0)])  # remaining exactly 2%
        assert u.floor_hit() is None  # not strictly below

    def test_capped(self):
        u = UsageWindows(windows=[_win("monthly", 100.0)])
        assert u.floor_hit() == "monthly"

    def test_limit_reached_flag_alone(self):
        u = UsageWindows(windows=[], limit_reached=True)
        assert u.windows == []

    def test_tightest_window_wins(self):
        u = UsageWindows(windows=[_win("weekly", 5.0), _win("5h", 99.5)])
        assert u.floor_hit() == "5h"


class TestClassify:
    def test_durations(self):
        assert _classify_window(18000) == "5h"
        assert _classify_window(604800) == "weekly"
        assert _classify_window(2592000) == "monthly"
        assert _classify_window(7200) == "5h"  # unknown small
        assert _classify_window(30 * 24 * 3600) == "monthly"


class TestLiveProbe:
    def test_probe_real_account(self):
        """Live probe against the backend (needs proxy + fresh AT)."""
        import json
        import os

        auth_path = "/tmp/codex-t3/.codex-home/auth.json"
        if not os.path.isfile(auth_path):
            pytest.skip("no live auth fixture")
        auth = json.load(open(auth_path))
        try:
            u = probe_usage(
                auth["tokens"]["access_token"],
                auth["tokens"]["account_id"],
                proxy="http://10.120.0.246:7890",
            )
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"live probe unavailable: {e}")
        assert isinstance(u.windows, list)
        for w in u.windows:
            assert w["window"] in ("5h", "weekly", "monthly")
            assert 0.0 <= w["used_pct"] <= 100.0
