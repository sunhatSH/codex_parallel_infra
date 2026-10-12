"""Sandbox backend interface.

Mirrors the registry pattern from agentic_cl_research's
src/rollout/sandbox_client.py: the scheduler only ever calls
``make_backend(name)``, concrete backends register themselves. A backend is an
async context manager yielding a live ``Sandbox`` with ``run()``, ``upload()``,
``download()``, ``kill()``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Sandbox(Protocol):
    """A live sandbox instance. One item = one sandbox = one codex run."""

    async def run(self, cmd: list[str], *, timeout_s: int, env: dict[str, str] | None = None) -> "ExecResult":
        ...

    async def upload(self, local_path: str, remote_path: str) -> None:
        ...

    async def download(self, remote_path: str, local_path: str) -> None:
        ...

    async def kill(self) -> None:
        ...


class ExecResult:
    def __init__(self, exit_code: int, stdout: str, stderr: str, timed_out: bool = False):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxBackend(Protocol):
    name: str

    async def create(self) -> Sandbox:
        ...

    async def close(self) -> None:
        ...


_BACKENDS: dict[str, type] = {}


def register_backend(cls: type) -> type:
    _BACKENDS[cls.name] = cls
    return cls


def make_backend(name: str, **kwargs) -> "SandboxBackend":
    try:
        return _BACKENDS[name](**kwargs)
    except KeyError:
        raise ValueError(f"unknown backend {name!r}; registered: {sorted(_BACKENDS)}") from None
