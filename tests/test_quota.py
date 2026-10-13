"""Quota-window cooldowns: 5h / weekly / monthly usage limits.

Policy (user's spec):
  - 用量满（任一窗口）→ token 取出，冷却对应时长（5h / 7d / 30d），账户本身没坏
  - 账户被封          → token 永久 banned
  - token 被使用后     → 状态（in_use/quota_window/cooldown_until）同步进 DB，
                        token↔auth.json 1:1 映射保存在 secrets/ 文件
"""

from __future__ import annotations

import pytest

from codex_parallel.attribution import QUOTA_KINDS, attribute
from codex_parallel.codex_runner import CodexRunResult
from codex_parallel.token_pool import TokenPool


@pytest.fixture()
def pool(tmp_path):
    from codex_parallel.db import bootstrap

    sf = bootstrap(tmp_path / "t.db")
    return TokenPool(sf, tmp_path / "secrets")


def _quota_result(window: str) -> CodexRunResult:
    return CodexRunResult(
        ok=False,
        exit_code=1,
        auth_failed=False,
        timed_out=False,
        quota_window=window,
        error=f"usage limit: {window}",
    )


class TestClassification:
    def test_quota_windows_detected(self):
        from codex_parallel.codex_runner import CodexRunner

        assert CodexRunner._quota_window("You've reached your usage limit") == "5h"
        assert CodexRunner._quota_window("Error: weekly limit reached") == "weekly"
        assert CodexRunner._quota_window("Error: monthly usage limit hit") == "monthly"
        assert CodexRunner._quota_window("429 Too Many Requests") == "5h"  # generic
        assert CodexRunner._quota_window("some random error") is None

    def test_quota_not_account_dead(self):
        r = _quota_result("5h")
        assert not r.auth_failed
        assert r.failure_kind == "quota_5h"

    def test_failure_kinds(self):
        assert _quota_result("5h").failure_kind == "quota_5h"
        assert _quota_result("weekly").failure_kind == "quota_weekly"
        assert _quota_result("monthly").failure_kind == "quota_monthly"


class TestAttribution:
    def test_quota_swaps_but_does_not_retire(self):
        for w in ("5h", "weekly", "monthly"):
            att = attribute(_quota_result(w), token_strikes=0)
            assert att.swap_token and not att.retire_token and att.cooldown_token

    def test_quota_does_not_consume_strikes(self):
        """Quota is not a failure — retrying the same token is pointless but
        it must not count toward the 3-strike discard."""
        att = attribute(_quota_result("5h"), token_strikes=99)
        assert att.cooldown_token and not att.retire_token


class TestPoolQuotaCooldown:
    def test_5h_cooldown(self, pool):
        tok = pool.add_token("a", "s")
        pool.report_quota_exhausted(tok.id, "5h")
        t = pool.list_tokens()[0]
        assert t.quota_window == "5h"
        assert t.status == "active"  # NOT banned — account is fine
        import pytest as _pytest

        from codex_parallel.token_pool import NoTokenAvailable

        with _pytest.raises(NoTokenAvailable):
            pool.acquire()

    def test_weekly_monthly_durations(self, pool):
        import datetime as dt

        for window, expect_hours in (("weekly", 168), ("monthly", 720)):
            tok = pool.add_token(f"t-{window}", "s")
            pool.report_quota_exhausted(tok.id, window)
        toks = {t.label: t for t in pool.list_tokens()}
        for label, t in toks.items():
            cu = t.cooldown_until.replace(tzinfo=dt.timezone.utc)
            hours = (cu - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
            expect = 168 if label == "t-weekly" else 720
            assert abs(hours - expect) < 1, (label, hours)

    def test_cooldown_expires_token_returns(self, pool):
        """Window rolls over → token usable again automatically."""
        import datetime as dt

        tok = pool.add_token("a", "s")
        pool.report_quota_exhausted(tok.id, "5h")
        # simulate window rollover
        from sqlalchemy import update

        from codex_parallel.models import Token

        with pool._session_factory() as s:
            s.execute(
                update(Token)
                .where(Token.id == tok.id)
                .values(
                    cooldown_until=dt.datetime.now(dt.timezone.utc)
                    - dt.timedelta(seconds=1)
                )
            )
            s.commit()
        got = pool.acquire()
        assert got.id == tok.id

    def test_banned_is_permanent(self, pool):
        tok = pool.add_token("a", "s")
        pool.report_banned(tok.id, "401")
        t = pool.list_tokens()[0]
        assert t.status == "banned"
        assert t.cooldown_until is None
        from codex_parallel.token_pool import NoTokenAvailable

        with pytest.raises(NoTokenAvailable):
            pool.acquire()

    def test_unknown_window_rejected(self, pool):
        tok = pool.add_token("a", "s")
        with pytest.raises(ValueError):
            pool.report_quota_exhausted(tok.id, "biweekly")


class TestStateSync:
    def test_use_syncs_db_state(self, pool):
        """acquire/release/in_use and quota fields all land in the DB row —
        the token↔auth.json mapping lives in secrets/, state lives in DB."""
        tok = pool.add_token("a", "s")
        got = pool.acquire()
        assert got.in_use == 1
        with pool._session_factory() as s:
            row = s.get(type(tok), tok.id)
            assert row.in_use == 1
            assert row.last_used_at is not None
        pool.release(tok.id)
        with pool._session_factory() as s:
            row = s.get(type(tok), tok.id)
            assert row.in_use == 0


class TestE2EQuotaSwap:
    def test_quota_token_parked_item_continues(self, tmp_path, monkeypatch):
        """mock codex 5h-limit error → token parked on cooldown, item
        finishes on the OTHER token, pool state visible in DB."""
        import asyncio
        import os

        shim = tmp_path / "codex"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = login ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
            "if grep -q 'sk-quota' \"$CODEX_HOME/auth.json\"; then\n"
            "  echo \"Error: You've reached your usage limit. Your limit will reset soon.\" >&2\n"
            "  exit 1\n"
            "fi\n"
            "echo '{\"type\":\"agent_message\",\"msg\":\"done-on-good-account\"}'\n"
            "echo ok > result-last.md\n"
        )
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        from codex_parallel.config import HubConfig
        from codex_parallel.db import bootstrap
        from codex_parallel.models import Task, TaskItem
        from codex_parallel.scheduler import Scheduler

        cfg = HubConfig(root=tmp_path, port=0)
        cfg.ensure_dirs()
        sf = bootstrap(cfg.db_path)
        pool = TokenPool(sf, cfg.secrets_dir)
        good = pool.add_token("good-acc", "sk-good")
        quota = pool.add_token("quota-acc", "sk-quota")
        # make the quota account the least-recently-used so it is picked first
        import datetime as dt

        from sqlalchemy import update

        from codex_parallel.models import Token as Tok

        with sf() as s:
            s.execute(
                update(Tok).where(Tok.id == good.id).values(
                    last_used_at=dt.datetime.now(dt.timezone.utc)
                )
            )
            s.commit()

        with sf() as s:
            task = Task(name="quota-e2e", prompt_template="p {{item}}", concurrency=1)
            s.add(task)
            s.flush()
            s.add(TaskItem(task_id=task.id, seq=0, payload="x"))
            s.commit()
            tid = task.id

        async def drive():
            sched = Scheduler(sf, pool, cfg)
            await sched.start()
            await asyncio.to_thread(sched.adopt_pending_tasks)
            sched.spawn_pending_tasks()
            for _ in range(150):
                await asyncio.sleep(0.2)
                with sf() as s:
                    t = s.get(Task, tid)
                    if t.status in ("done", "failed"):
                        return sched, t.status
            return sched, "timeout"

        sched, status = asyncio.run(drive())
        asyncio.run(sched.stop())
        assert status == "done", status

        toks = {t.label: t for t in pool.list_tokens()}
        # quota account: parked with 5h cooldown, NOT banned/discarded
        q = toks["quota-acc"]
        assert q.status == "active"
        assert q.quota_window == "5h"
        assert q.cooldown_until is not None
        # good account completed the item, strikes cleared
        assert toks["good-acc"].status == "active"
        assert toks["good-acc"].fail_count == 0

    def test_all_quota_pool_alerts(self, tmp_path, monkeypatch):
        """Every account hits quota → pool has no available token → item
        fails and a pool alert is recorded (same alarm path as empty pool)."""
        import asyncio
        import os

        shim = tmp_path / "codex"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = login ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
            "echo \"Error: weekly limit reached — wait for reset\" >&2\n"
            "exit 1\n"
        )
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        from codex_parallel.config import HubConfig
        from codex_parallel.db import bootstrap
        from codex_parallel.models import PoolAlert, Task, TaskItem
        from codex_parallel.scheduler import Scheduler
        from sqlalchemy import select

        cfg = HubConfig(root=tmp_path, port=0)
        cfg.ensure_dirs()
        sf = bootstrap(cfg.db_path)
        pool = TokenPool(sf, cfg.secrets_dir)
        pool.add_token("only-acc", "sk-only")

        with sf() as s:
            task = Task(name="all-quota", prompt_template="p {{item}}", concurrency=1)
            s.add(task)
            s.flush()
            s.add(TaskItem(task_id=task.id, seq=0, payload="x"))
            s.commit()
            tid = task.id

        async def drive():
            sched = Scheduler(sf, pool, cfg)
            await sched.start()
            await asyncio.to_thread(sched.adopt_pending_tasks)
            sched.spawn_pending_tasks()
            for _ in range(100):
                await asyncio.sleep(0.2)
                with sf() as s:
                    t = s.get(Task, tid)
                    if t.status in ("done", "failed"):
                        return sched, t.status
            return sched, "timeout"

        sched, status = asyncio.run(drive())
        asyncio.run(sched.stop())
        assert status == "failed", status
        t = pool.list_tokens()[0]
        assert t.quota_window == "weekly"
        with sf() as s:
            alerts = list(
                s.execute(select(PoolAlert).where(PoolAlert.status == "active")).scalars()
            )
        assert len(alerts) >= 1
