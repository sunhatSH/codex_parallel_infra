"""E2B backend: Tencent Agent Runtime (E2B-compatible).

Pattern copied from agentic_cl_research's E2BSandbox: the e2b_code_interpreter
SDK reads E2B_API_KEY / E2B_DOMAIN from env and talks to the Tencent endpoint.
The heavy SDK import is deferred to create() so dev boxes without it still work.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

from .base import ExecResult, Sandbox, register_backend


class E2BSandboxWrapper(Sandbox):
    def __init__(self, sb: Any, timeout_s: int):
        self._sb = sb
        self._timeout = timeout_s

    async def run(self, cmd, *, timeout_s: int, env: dict[str, str] | None = None) -> ExecResult:
        shell = " ".join(shlex.quote(c) for c in cmd)
        prefix = ""
        if env:
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            prefix = f"export {exports} && "
        loop_out = await asyncio_wrap(self._sb.commands.run, prefix + shell, timeout=timeout_s)
        return ExecResult(
            loop_out.exit_code,
            (loop_out.stdout or ""),
            (loop_out.stderr or ""),
            False,
        )

    async def upload(self, local_path: str, remote_path: str) -> None:
        await asyncio_wrap(self._sb.files.write, remote_path, open(local_path, "rb").read())

    async def download(self, remote_path: str, local_path: str) -> None:
        data = await asyncio_wrap(self._sb.files.read, remote_path)
        with open(local_path, "wb") as f:
            f.write(data)

    async def kill(self) -> None:
        try:
            await asyncio_wrap(self._sb.kill)
        except Exception:
            pass


async def asyncio_wrap(fn, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


@register_backend
class E2BBackend:
    name = "e2b"

    def __init__(self, template: str | None = None, timeout_s: int = 1800, **_ignored):
        self._template = template or os.environ.get(
            "E2B_TEMPLATE", "agentic-cl-sandbox"
        )
        self._timeout = timeout_s

    async def create(self) -> Sandbox:
        from e2b_code_interpreter import Sandbox as E2BSandbox

        api_key = os.environ.get("E2B_API_KEY")
        domain = os.environ.get("E2B_DOMAIN")
        if not api_key or not domain:
            raise RuntimeError("E2B_API_KEY and E2B_DOMAIN must be set for e2b backend")
        sb = await asyncio_wrap(
            E2BSandbox.create, template=self._template, timeout=self._timeout
        )
        return E2BSandboxWrapper(sb, self._timeout)

    async def close(self) -> None:
        pass
