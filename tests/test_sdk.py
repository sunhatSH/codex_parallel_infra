"""SDK live tests against the running hub (skipped when the hub is down)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SDK = Path(__file__).resolve().parents[1] / "sdk"
if str(_SDK) not in sys.path:
    sys.path.insert(0, str(_SDK))

from codex_parallel_sdk import AsyncHubClient, HubClient, HubError  # noqa: E402


def hub_up() -> bool:
    try:
        with HubClient(timeout_s=3) as c:
            return bool(c.health().get("ok"))
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not hub_up(), reason="hub not running")


class TestSync:
    def test_health_and_stats(self):
        with HubClient() as c:
            assert c.health()["ok"]
            stats = c.token_stats()
            assert "active" in stats

    def test_submit_and_wait_with_mock(self):
        """Needs the hub's mock codex in PATH (start-all dev setup)."""
        with HubClient() as c:
            stats = c.token_stats()
            if stats["active"] < 1:
                pytest.skip("no active tokens in pool")
            task = c.submit(
                items=["sdk-e2e-item"],
                prompt="echo test: {{item}}",
                concurrency=1,
            )
            t = task.wait(timeout_s=90)
            assert t["status"] in ("done", "failed")
            if t["status"] == "done":
                rs = task.results()
                assert len(rs) == 1
                assert rs[0]["last_message"]

    def test_submit_rejects_bad_prompt(self):
        with HubClient() as c:
            with pytest.raises(HubError):
                c.submit(items=["x"], prompt="no placeholder")

    def test_utilization(self):
        with HubClient() as c:
            toks = c.tokens()
            if not toks:
                pytest.skip("no tokens")
            tid = toks[0]["id"]
            r = c.set_token_utilization(tid, toks[0]["max_concurrency"])
            assert r["ok"] is True

    def test_alerts(self):
        with HubClient() as c:
            assert isinstance(c.alerts(), list)


class TestAsync:
    def test_async_health(self):
        import asyncio

        async def go():
            async with AsyncHubClient() as c:
                return await c.health()

        assert asyncio.run(go())["ok"]

    def test_async_submit(self):
        import asyncio

        async def go():
            async with AsyncHubClient() as c:
                stats = await c.token_stats()
                if stats["active"] < 1:
                    return None
                t = await c.submit(items=["async-item"], prompt="a: {{item}}", concurrency=1)
                return await t.wait(timeout_s=90)

        r = asyncio.run(go())
        if r is not None:
            assert r["status"] in ("done", "failed")
