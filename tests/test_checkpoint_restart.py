"""Checkpoint / sandbox-restart semantics.

When a codex inside a sandbox dies, the scheduler must: kill that sandbox,
open a FRESH one, and continue the item from the last checkpoint (which for
item-level work is simply "the item itself" — codex exec is stateless per
attempt). These tests pin that contract against the real Scheduler +
LocalBackend with a programmable mock codex.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from codex_parallel.config import HubConfig
from codex_parallel.db import bootstrap
from codex_parallel.models import Task, TaskItem, WorkerRun
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
    tp.add_token("cp-acc", "sk-cp")
    return sf, tp, cfg, tmp_path


async def drive_to_completion(sched: Scheduler, sf, task_id: int, timeout_s=30):
    for _ in range(int(timeout_s / 0.2)):
        await asyncio.sleep(0.2)
        with sf() as s:
            t = s.get(Task, task_id)
            if t.status in ("done", "failed", "cancelled"):
                return t.status
    raise TimeoutError("task never reached terminal status")


class TestSandboxRestart:
    async def test_codex_crash_opens_new_sandbox(self, env, monkeypatch):
        """flaky codex dies twice, succeeds 3rd — 3 DISTINCT sandboxes used."""
        sf, tp, cfg, tmp = env
        state = tmp / "state"
        monkeypatch.setenv("MOCK_CODEX_MODE", "flaky")
        monkeypatch.setenv("MOCK_STATE_FILE", str(state))
        monkeypatch.setenv("MOCK_CODEX_SUCCESS_AT", "3")

        with sf() as s:
            task = Task(name="cp-restart", prompt_template="p {{item}}", concurrency=1)
            s.add(task)
            s.flush()
            s.add(TaskItem(task_id=task.id, seq=0, payload="x"))
            s.commit()
            tid = task.id

        sched = Scheduler(sf, tp, cfg)
        await sched.start()
        await asyncio.to_thread(sched.adopt_pending_tasks)
        sched.spawn_pending_tasks()

        status = await drive_to_completion(sched, sf, tid)
        await sched.stop()
        assert status == "done"

        # every attempt got its own WorkerRun checkpoint
        with sf() as s:
            runs = list(
                s.execute(
                    select(WorkerRun).where(WorkerRun.task_id == tid)
                ).scalars()
            )
        assert len(runs) == 3
        assert [r.status for r in runs] == ["failed", "failed", "done"]
        # 3 distinct sandbox workspaces were created (one per attempt)
        workspaces = list((cfg.workspace_root).glob("sbx-*"))
        assert len(workspaces) == 3

    async def test_hung_codex_killed_and_item_survives(self, env, monkeypatch):
        """A hung sandbox is killed on timeout; retry runs in a new sandbox."""
        sf, tp, cfg, tmp = env
        # first attempt hangs, then a state flip makes later attempts ok
        state = tmp / "hang-state"
        monkeypatch.setenv("MOCK_STATE_FILE", str(state))
        # custom mode: hang first, then ok
        shim_extra = tmp / "bin" / "codex-hang-then-ok"
        shim_extra.write_text(
            "#!/usr/bin/env bash\n"
            "n=$(cat '" + str(state) + "' 2>/dev/null || echo 0)\n"
            "n=$((n+1)); echo $n > '" + str(state) + "'\n"
            "if [ $n -lt 2 ]; then sleep 60; exit 1; fi\n"
            f'exec bash {MOCK}\n'
        )
        shim_extra.chmod(0o755)
        (tmp / "bin" / "codex").write_text(f"#!/usr/bin/env bash\nexec bash {shim_extra} \"$@\"\n")
        (tmp / "bin" / "codex").chmod(0o755)

        # shrink item timeout so the hang is caught fast
        cfg.item_timeout_s = 3

        with sf() as s:
            task = Task(name="cp-hang", prompt_template="p {{item}}", concurrency=1)
            s.add(task)
            s.flush()
            s.add(TaskItem(task_id=task.id, seq=0, payload="x"))
            s.commit()
            tid = task.id

        sched = Scheduler(sf, tp, cfg)
        await sched.start()
        await asyncio.to_thread(sched.adopt_pending_tasks)
        sched.spawn_pending_tasks()

        status = await drive_to_completion(sched, sf, tid, timeout_s=60)
        await sched.stop()
        assert status == "done"
        with sf() as s:
            runs = list(s.execute(select(WorkerRun).where(WorkerRun.task_id == tid)).scalars())
        # attempt 1 timed out, attempt 2 in a FRESH sandbox succeeded
        assert [r.status for r in runs] == ["timeout", "done"]

    async def test_stalled_sandbox_reaped_and_requeued(self, env):
        """Worker heartbeat goes stale → reaper requeues the item (checkpoint)."""
        sf, tp, cfg, tmp = env
        cfg.item_timeout_s = 3600  # long; the reaper is what must fire

        with sf() as s:
            task = Task(name="cp-reap", prompt_template="p {{item}}", concurrency=1)
            s.add(task)
            s.flush()
            s.add(TaskItem(task_id=task.id, seq=0, payload="x"))
            s.commit()
            tid = task.id

        sched = Scheduler(sf, tp, cfg)
        # tighten reaper windows for the test
        sched.health.stall_threshold_s = 0
        sched.health.interval_s = 0.2
        await sched.start()
        await asyncio.to_thread(sched.adopt_pending_tasks)
        sched.spawn_pending_tasks()

        # simulate a worker that died mid-run: mark a run running with a stale hb
        with sf() as s:
            import datetime as dt

            run = WorkerRun(
                item_id=1, task_id=tid, token_id=1,
                hub_id="hub-deadbeef", worker_slot=0, status="running",
            )
            run.heartbeat_at = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
            )
            s.add(run)
            s.execute(
                __import__("sqlalchemy").update(TaskItem)
                .where(TaskItem.id == 1)
                .values(status="running")
            )
            s.commit()

        reaped = await asyncio.to_thread(sched.health.reap_stalled)
        assert reaped == [1]
        with sf() as s:
            item = s.get(TaskItem, 1)
            # retry_count < 3 → requeued for a fresh worker + fresh sandbox
            assert item.status == "pending"
        await sched.stop()
