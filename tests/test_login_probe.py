"""Account-level login probe behavior (scheduler + LoginProber integration).

Covers the user's policy:
  - 普通下线 (transient offline)   → same account retried ≤3x, fresh sandbox
    each time (sandbox destroyed between attempts)
  - 账号问题 (auth rejection)      → token marked unusable immediately
  - probe ok                       → task proceeds in the SAME sandbox
  - pool exhausted                 → item fails + alert recorded (CLI/web)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from codex_parallel.config import HubConfig
from codex_parallel.db import bootstrap
from codex_parallel.login_probe import LoginCheckResult, LoginProber
from codex_parallel.models import PoolAlert, Task, TaskItem, Token
from codex_parallel.scheduler import Scheduler
from codex_parallel.token_pool import TokenPool

MOCK = Path(__file__).resolve().parents[1] / "scripts" / "mock_codex.sh"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "codex"
    shim.write_text(f"#!/usr/bin/env bash\nexec bash {MOCK} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    cfg = HubConfig(root=tmp_path, port=0)
    cfg.ensure_dirs()
    sf = bootstrap(cfg.db_path)
    tp = TokenPool(sf, cfg.secrets_dir)
    return sf, tp, cfg, tmp_path


async def make_task(sf, name="probe-task"):
    with sf() as s:
        task = Task(name=name, prompt_template="p {{item}}", concurrency=1)
        s.add(task)
        s.flush()
        s.add(TaskItem(task_id=task.id, seq=0, payload="x"))
        s.commit()
        return task.id


async def drive(sf, tid, timeout_s=30):
    for _ in range(int(timeout_s / 0.2)):
        await asyncio.sleep(0.2)
        with sf() as s:
            t = s.get(Task, tid)
            if t.status in ("done", "failed", "cancelled"):
                return t.status
    raise TimeoutError("task never reached terminal status")


class TestLoginProberUnit:
    async def test_ok(self, env):
        from codex_parallel.backends.local import LocalBackend

        sf, tp, cfg, tmp = env
        be = LocalBackend(workspace_root=cfg.workspace_root)
        sbx = await be.create()
        prober = LoginProber(timeout_s=10)
        r = await prober.probe(sbx, "sk-fine")
        assert r.ok and not r.account_dead and not r.transient
        await sbx.kill()

    async def test_account_dead(self, env, monkeypatch):
        from codex_parallel.backends.local import LocalBackend

        sf, tp, cfg, tmp = env
        monkeypatch.setenv("MOCK_CODEX_LOGIN", "dead")
        be = LocalBackend(workspace_root=cfg.workspace_root)
        sbx = await be.create()
        prober = LoginProber(timeout_s=10)
        r = await prober.probe(sbx, "sk-dead")
        assert not r.ok and r.account_dead and not r.transient
        await sbx.kill()

    async def test_transient(self, env, monkeypatch):
        from codex_parallel.backends.local import LocalBackend

        sf, tp, cfg, tmp = env
        monkeypatch.setenv("MOCK_CODEX_LOGIN", "offline")
        be = LocalBackend(workspace_root=cfg.workspace_root)
        sbx = await be.create()
        prober = LoginProber(timeout_s=10)
        r = await prober.probe(sbx, "sk-offline")
        assert not r.ok and not r.account_dead and r.transient
        await sbx.kill()


class TestSchedulerAccountPolicy:
    async def test_dead_account_banned_immediately(self, env, monkeypatch):
        """login probe 401 → token banned at once, item continues on another."""
        sf, tp, cfg, tmp = env
        # mock is env-driven globally; make login behavior PER-SECRET so both
        # accounts can coexist: shim reads the secret from CODEX_HOME/auth.json
        shim = tmp / "bin" / "codex"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = login ] && [ \"$2\" = status ]; then\n"
            "  if grep -q 'sk-dead' \"$CODEX_HOME/auth.json\"; then\n"
            "    echo 'Error: 401 Unauthorized - invalid api key' >&2; exit 1\n"
            "  fi\n"
            "  echo 'Logged in using an API key'; exit 0\n"
            "fi\n"
            f"exec bash {MOCK} \"$@\"\n"
        )
        shim.chmod(0o755)
        tp.add_token("good-acc", "sk-good")
        tp.add_token("dead-acc", "sk-dead")
        # good-acc was used more recently → dead-acc (null last_used) acquired first
        good = tp.list_tokens()[0]
        with sf() as s:
            t_ = s.get(Token, good.id)
            from codex_parallel.models import utcnow

            t_.last_used_at = utcnow()
            s.commit()

        tid = await make_task(sf, "probe-dead")
        sched = Scheduler(sf, tp, cfg)
        await sched.start()
        await asyncio.to_thread(sched.adopt_pending_tasks)
        sched.spawn_pending_tasks()
        status = await drive(sf, tid)
        await sched.stop()

        assert status == "done"
        toks = tp.list_tokens()
        by_label = {t.label: t for t in toks}
        assert by_label["dead-acc"].status == "banned"
        assert "401" in (by_label["dead-acc"].banned_reason or "")
        assert by_label["good-acc"].status == "active"

    async def test_offline_retried_3x_then_discard(self, env, monkeypatch):
        """普通下线: same token retried, fresh sandbox each time, 3 strikes → discard."""
        sf, tp, cfg, tmp = env
        monkeypatch.setenv("MOCK_CODEX_LOGIN", "offline")
        tp.add_token("offline-acc", "sk-off")

        tid = await make_task(sf, "probe-offline")
        sched = Scheduler(sf, tp, cfg)
        await sched.start()
        await asyncio.to_thread(sched.adopt_pending_tasks)
        sched.spawn_pending_tasks()
        status = await drive(sf, tid, timeout_s=60)
        await sched.stop()

        # token discarded after 3 strikes; pool now empty → item failed
        toks = tp.list_tokens()
        assert toks[0].status == "discarded"
        assert status == "failed"

    async def test_offline_then_recover(self, env, monkeypatch):
        """2 offline probes then healthy → item completes on same account."""
        sf, tp, cfg, tmp = env
        # use a state file to flip login behavior on the 3rd call
        state = tmp / "login-state"
        shim = tmp / "bin" / "codex"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f"n=$(cat '{state}' 2>/dev/null || echo 0)\n"
            f"n=$((n+1)); echo $n > '{state}'\n"
            "if [ \"$1\" = login ] && [ \"$2\" = status ] && [ $n -lt 3 ]; then\n"
            "  echo 'Error: network error - connection timed out' >&2; exit 1\n"
            "fi\n"
            f"exec bash {MOCK} \"$@\"\n"
        )
        shim.chmod(0o755)
        tp.add_token("flaky-acc", "sk-flaky")

        tid = await make_task(sf, "probe-recover")
        sched = Scheduler(sf, tp, cfg)
        await sched.start()
        await asyncio.to_thread(sched.adopt_pending_tasks)
        sched.spawn_pending_tasks()
        status = await drive(sf, tid, timeout_s=60)
        await sched.stop()

        assert status == "done"
        toks = tp.list_tokens()
        assert toks[0].status == "active"

    async def test_pool_exhausted_alerts(self, env):
        """池子空 → item 失败 + PoolAlert 记录（CLI/web 拉取用）。"""
        sf, tp, cfg, tmp = env
        tid = await make_task(sf, "no-tokens")
        sched = Scheduler(sf, tp, cfg)
        await sched.start()
        await asyncio.to_thread(sched.adopt_pending_tasks)
        sched.spawn_pending_tasks()
        status = await drive(sf, tid, timeout_s=20)
        await sched.stop()

        assert status == "failed"
        with sf() as s:
            alerts = list(
                s.execute(select(PoolAlert).where(PoolAlert.status == "active")).scalars()
            )
        assert len(alerts) >= 1
        assert "pool" in alerts[0].message.lower()

    async def test_token_one_account_serial_use(self, env):
        """1 token = 1 account: concurrency>1 workers must NOT double-use it."""
        sf, tp, cfg, tmp = env
        tp.add_token("single-acc", "sk-single", max_concurrency=1)

        with sf() as s:
            task = Task(name="serial", prompt_template="p {{item}}", concurrency=4)
            s.add(task)
            s.flush()
            for i in range(4):
                s.add(TaskItem(task_id=task.id, seq=i, payload=f"x{i}"))
            s.commit()
            tid = task.id

        sched = Scheduler(sf, tp, cfg)
        await sched.start()
        await asyncio.to_thread(sched.adopt_pending_tasks)
        sched.spawn_pending_tasks()
        status = await drive(sf, tid, timeout_s=60)
        await sched.stop()

        # in_use never exceeds 1 (one account, never concurrent)
        toks = tp.list_tokens()
        assert toks[0].in_use <= 1
        assert toks[0].status == "active"
