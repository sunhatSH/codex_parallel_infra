"""RT rotation persistence tests: the pool must always keep the newest RT."""

from __future__ import annotations

import asyncio
import json

from codex_parallel.rt_rotation import extract_account_id, extract_refresh_token
from codex_parallel.token_pool import TokenPool


def _auth(rt: str, account: str = "acc-1") -> str:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": "x",
                "access_token": "y",
                "refresh_token": rt,
                "account_id": account,
            },
        }
    )


class TestExtractors:
    def test_extract_from_auth_json(self):
        assert extract_refresh_token(_auth("rt-new")) == "rt-new"
        assert extract_account_id(_auth("rt", "acc-9")) == "acc-9"

    def test_extract_from_bare_rt(self):
        assert extract_refresh_token("rt-bare") == "rt-bare"

    def test_garbage(self):
        assert extract_refresh_token("") is None
        assert extract_refresh_token("{not json") is None
        assert extract_refresh_token(json.dumps({"no": "tokens"})) is None


class TestPoolRotation:
    def test_update_secret_rewrites(self, tmp_path):
        from codex_parallel.db import bootstrap

        sf = bootstrap(tmp_path / "t.db")
        pool = TokenPool(sf, tmp_path / "secrets")
        tok = pool.add_token("acc", _auth("rt-old"))

        assert pool.update_secret(tok.id, _auth("rt-old")) is False  # no-op
        assert pool.update_secret(tok.id, _auth("rt-new")) is True
        assert extract_refresh_token(pool.read_secret(tok.id)) == "rt-new"

    def test_scheduler_banks_rotated_rt(self, tmp_path, monkeypatch):
        """A run that returns a fresher auth.json rewrites the pool secret."""
        import os

        from codex_parallel.codex_runner import CodexRunResult
        codex_sh = tmp_path / "codex"
        # mock codex: writes a NEW auth.json into CODEX_HOME (rotation), then ok
        codex_sh.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = login ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
            "cat > \"$CODEX_HOME/auth.json\" <<'EOF'\n"
            + _auth("rt-ROTATED") + "\nEOF\n"
            "echo '{\"type\":\"agent_message\",\"msg\":\"done\"}'\n"
            "echo ok > result-last.md\n"
        )
        codex_sh.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        from codex_parallel.config import HubConfig
        from codex_parallel.db import bootstrap
        from codex_parallel.models import Task, TaskItem
        from codex_parallel.scheduler import Scheduler

        cfg = HubConfig(root=tmp_path, port=0)
        cfg.ensure_dirs()
        sf = bootstrap(cfg.db_path)
        pool = TokenPool(sf, cfg.secrets_dir)
        tok = pool.add_token("acc", _auth("rt-original"))

        with sf() as s:
            task = Task(name="rt", prompt_template="p {{item}}", concurrency=1)
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

        # the rotated RT from the sandbox must now be what the pool holds
        secret = pool.read_secret(tok.id)
        assert extract_refresh_token(secret) == "rt-ROTATED"
