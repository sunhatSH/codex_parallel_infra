"""Local backend: each "sandbox" is an isolated workdir + subprocess.

Deliberately NOT a security boundary — for dev, CI, and small-scale runs where
the sandboxing is provided externally. It exists so the whole hub pipeline
(task -> token -> sandbox -> codex -> result) is testable without cloud
credentials.

Per-instance isolation:
  - own working directory under workspace_root
  - own CODEX_HOME (auth.json etc.) under the same dir
  - environment fully controlled by the caller (run(env=...))
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import uuid
from pathlib import Path

from .base import ExecResult, Sandbox, register_backend


class LocalSandbox:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.codex_home = workdir / ".codex-home"
        self._alive = True

    async def run(self, cmd, *, timeout_s: int, env: dict[str, str] | None = None) -> ExecResult:
        if not self._alive:
            return ExecResult(-1, "", "sandbox killed", False)
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        full_env.setdefault("CODEX_HOME", str(self.codex_home))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workdir),
                env=full_env,
                stdin=asyncio.subprocess.DEVNULL,  # codex reads stdin for extra input → must EOF immediately
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=64 * 1024 * 1024,
                start_new_session=True,  # own process group → killpg on timeout
            )
        except FileNotFoundError as e:
            return ExecResult(127, "", f"command not found: {e}", False)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            # kill the whole process GROUP: the shim spawns children (node,
            # vendor binaries) that plain proc.kill() leaves orphaned, and
            # proc.wait() then blocks on the still-open stdout pipe.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return ExecResult(-1, "", f"timeout after {timeout_s}s", True)
        return ExecResult(
            proc.returncode,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
            False,
        )

    async def upload(self, local_path: str, remote_path: str) -> None:
        dest = self.workdir / remote_path.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)

    async def download(self, remote_path: str, local_path: str) -> None:
        src = self.workdir / remote_path.lstrip("/")
        if not src.is_file():
            raise FileNotFoundError(remote_path)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local_path)

    async def kill(self) -> None:
        self._alive = False


@register_backend
class LocalBackend:
    name = "local"

    def __init__(self, workspace_root: str | Path = "./data/workspaces", **_ignored):
        self._root = Path(workspace_root)

    async def create(self) -> Sandbox:
        self._root.mkdir(parents=True, exist_ok=True)
        workdir = self._root / f"sbx-{uuid.uuid4().hex[:12]}"
        workdir.mkdir(parents=True)
        return LocalSandbox(workdir)

    async def close(self) -> None:
        pass
