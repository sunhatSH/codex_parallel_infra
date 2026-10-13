"""E2B backend: Tencent Agent Runtime (E2B-compatible).

Pattern copied from agentic_cl_research's E2BSandbox: the e2b_code_interpreter
SDK reads E2B_API_KEY / E2B_DOMAIN from env and talks to the Tencent endpoint.
The heavy SDK import is deferred to create() so dev boxes without it still work.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

from . import e2b_http1_patch  # noqa: F401 — flips SDK to HTTP/1.1 on import
from .base import ExecResult, Sandbox, register_backend


class E2BSandboxWrapper(Sandbox):
    def __init__(self, sb: Any, timeout_s: int):
        self._sb = sb
        self._timeout = timeout_s

    async def run(self, cmd, *, timeout_s: int, env: dict[str, str] | None = None) -> ExecResult:
        shell = " ".join(shlex.quote(c) for c in cmd)
        # envd runs commands via sh -c; export prefix is the reliable way to
        # inject per-run env (verified live against Tencent Agent Runtime).
        if env:
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            shell = f"export {exports} && {shell}"
        try:
            loop_out = await self._sb.commands.run(shell, timeout=timeout_s)
        except Exception as e:  # noqa: BLE001
            # TimeoutException → timeout; CommandExitException carries the
            # result on it; anything else is an infra error.
            msg = str(e)
            if "timeout" in msg.lower() or "timed out" in msg.lower():
                return ExecResult(-1, "", msg, True)
            res = getattr(e, "result", None)
            if res is not None and hasattr(res, "exit_code"):
                return ExecResult(
                    res.exit_code,
                    (res.stdout or ""),
                    (res.stderr or "") or msg,
                    False,
                )
            raise
        return ExecResult(
            loop_out.exit_code,
            (loop_out.stdout or ""),
            (loop_out.stderr or ""),
            False,
        )

    async def upload(self, local_path: str, remote_path: str) -> None:
        # ensure parent dirs exist for nested relative paths
        parent = remote_path.rsplit("/", 1)[0] if "/" in remote_path else None
        if parent:
            await self._sb.commands.run(f"mkdir -p {shlex.quote(parent)}", timeout=15)
        with open(local_path, "rb") as f:
            await self._sb.files.write(remote_path, f.read())

    async def download(self, remote_path: str, local_path: str) -> None:
        data = await self._sb.files.read(remote_path)
        if isinstance(data, str):
            data = data.encode()
        with open(local_path, "wb") as f:
            f.write(data)

    async def kill(self) -> None:
        try:
            await self._sb.kill()
        except Exception:
            pass


@register_backend
class E2BBackend:
    name = "e2b"

    def __init__(self, template: str | None = None, timeout_s: int = 1800, **_ignored):
        self._template = template or os.environ.get(
            "E2B_TEMPLATE", "agentic-cl-sandbox"
        )
        self._timeout = timeout_s

    async def create(self) -> Sandbox:
        from e2b import AsyncSandbox as E2BAsyncSandbox

        api_key = os.environ.get("E2B_API_KEY")
        domain = os.environ.get("E2B_DOMAIN")
        if not api_key or not domain:
            raise RuntimeError("E2B_API_KEY and E2B_DOMAIN must be set for e2b backend")
        # AGS keys carry an ark_ prefix; the e2b SDK would reject it unless
        # validation is disabled (same switch agentic_cl_research uses).
        os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")
        sb = await E2BAsyncSandbox.create(template=self._template, timeout=self._timeout)
        return E2BSandboxWrapper(sb, self._timeout)

    async def close(self) -> None:
        pass
