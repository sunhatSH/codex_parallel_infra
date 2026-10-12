from __future__ import annotations

import json

import pytest

from codex_parallel.codex_runner import CodexRunner
from codex_parallel.backends.local import LocalBackend, LocalSandbox


@pytest.fixture()
async def backend(tmp_path):
    be = LocalBackend(workspace_root=tmp_path / "ws")
    yield be
    await be.close()


@pytest.fixture()
async def sandbox(backend):
    sbx = await backend.create()
    yield sbx
    await sbx.kill()


class TestArgv:
    def test_basic_argv(self):
        r = CodexRunner()
        argv = r.build_argv("hello world")
        assert argv[0] == "codex"
        assert "exec" in argv
        assert "--json" in argv
        assert argv[-1] == "hello world"
        assert "--skip-git-repo-check" in argv

    def test_model_and_effort_flags(self):
        r = CodexRunner(model="gpt-5.2", reasoning_effort="high")
        argv = r.build_argv("p")
        i = argv.index("-m")
        assert argv[i + 1] == "gpt-5.2"


class TestAuthPrep:
    async def test_bare_key_normalized(self, sandbox):
        r = CodexRunner()
        env = await r.prepare_auth(sandbox, "sk-abc123")
        f = sandbox.workdir / ".codex-home" / "auth.json"
        data = json.loads(f.read_text())
        assert data == {"OPENAI_API_KEY": "sk-abc123"}

    async def test_json_payload_preserved(self, sandbox):
        r = CodexRunner()
        payload = json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"})
        await r.prepare_auth(sandbox, payload)
        data = json.loads((sandbox.workdir / ".codex-home" / "auth.json").read_text())
        assert data["auth_mode"] == "apikey"


class TestEventParsing:
    def test_parse_and_extract(self):
        stdout = (
            'noise line\n'
            '{"type":"started","msg":""}\n'
            '{"type":"agent_message","msg":"final answer here"}\n'
            '{"broken json"\n'
        )
        events = CodexRunner._parse_events(stdout)
        assert len(events) == 2
        assert CodexRunner._extract_last_message(events) == "final answer here"


class TestLocalBackend:
    async def test_run_ok(self, sandbox):
        res = await sandbox.run(["echo", "hi"], timeout_s=10)
        assert res.ok and res.stdout.strip() == "hi"

    async def test_run_timeout(self, sandbox):
        res = await sandbox.run(["sleep", "60"], timeout_s=1)
        assert res.timed_out and not res.ok

    async def test_upload_download_roundtrip(self, sandbox, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text("payload")
        await sandbox.upload(str(src), "sub/dir/file.txt")
        dst = tmp_path / "dst.txt"
        await sandbox.download("sub/dir/file.txt", str(dst))
        assert dst.read_text() == "payload"

    async def test_env_isolation(self, sandbox):
        res = await sandbox.run(
            ["sh", "-c", "echo $MY_TEST_VAR"], timeout_s=10, env={"MY_TEST_VAR": "isolated"}
        )
        assert res.stdout.strip() == "isolated"
