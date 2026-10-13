"""HubClient: sync + async SDK over the hub REST API."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8787"
TERMINAL = ("done", "failed", "cancelled")


class HubError(Exception):
    """Hub returned an error (message = API detail)."""


class TaskHandle:
    """A submitted task; poll/wait/collect."""

    def __init__(self, client, task_id: int, num_items: int):
        self._c = client
        self.id = task_id
        self.num_items = num_items

    def status(self) -> dict:
        return self._c._get(f"/api/tasks/{self.id}")

    def is_done(self) -> bool:
        return self.status()["status"] in TERMINAL

    def wait(self, timeout_s: float | None = None, poll_s: float = 2.0) -> dict:
        """Block until the task reaches a terminal state. Returns the task."""
        t0 = time.time()
        while True:
            t = self.status()
            if t["status"] in TERMINAL:
                return t
            if timeout_s is not None and time.time() - t0 > timeout_s:
                raise TimeoutError(f"task {self.id} still {t['status']} after {timeout_s}s")
            time.sleep(poll_s)

    def results(self) -> list[dict]:
        return self._c._get(f"/api/tasks/{self.id}/results")

    def stop(self) -> None:
        self._c._post(f"/api/tasks/{self.id}/stop")

    def cancel(self) -> None:
        self._c._post(f"/api/tasks/{self.id}/cancel")


class _Base:
    """Shared method implementations (sync and async subclasses wire the transport)."""

    # concrete subclasses provide: _get, _post, _request


class HubClient(_Base):
    """Synchronous client."""

    def __init__(self, base_url: str = DEFAULT_URL, timeout_s: float = 60.0):
        self._url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self._url, timeout=timeout_s)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ── transport ────────────────────────────────────────────────────── #

    def _get(self, path: str) -> Any:
        r = self._http.get(path)
        if r.status_code >= 400:
            raise HubError(r.json().get("detail", r.text))
        return r.json()

    def _post(self, path: str, json: dict | None = None) -> Any:
        r = self._http.post(path, json=json)
        if r.status_code >= 400:
            raise HubError(r.json().get("detail", r.text))
        return r.json()

    # ── hub / tasks ──────────────────────────────────────────────────── #

    def health(self) -> dict:
        return self._get("/api/health")

    def submit(
        self,
        items: list[str] | None = None,
        items_file: str | Path | None = None,
        prompt: str = "过滤以下训练数据并给出质量分(1-10)，只输出JSON: {{item}}",
        name: str | None = None,
        concurrency: int = 8,
        backend: str | None = None,
        model: str | None = None,
        input_files: list[str | Path] | None = None,
    ) -> TaskHandle:
        """Submit a filtering task.

        items (list) or items_file (path, one per line) — at least one.
        prompt must contain {{item}}. input_files are copied into every
        sandbox's cwd before codex runs.
        """
        body: dict[str, Any] = {
            "name": name or f"sdk-{int(time.time())}",
            "prompt_template": prompt,
            "concurrency": concurrency,
        }
        if items:
            body["items"] = items
        if items_file:
            body["items_file"] = str(items_file)
        if backend:
            body["backend"] = backend
        if input_files:
            body["input_files"] = [str(f) for f in input_files]
        codex_config: dict = {}
        if model:
            codex_config["model"] = model
        body["codex_config"] = codex_config
        d = self._post("/api/tasks", body)
        return TaskHandle(self, d["task_id"], d["num_items"])

    def tasks(self) -> list[dict]:
        return self._get("/api/tasks")

    def task(self, task_id: int) -> TaskHandle:
        """Handle for an existing task (raises if unknown)."""
        self._get(f"/api/tasks/{task_id}")
        return TaskHandle(self, task_id, 0)

    # ── token pool ───────────────────────────────────────────────────── #

    def tokens(self) -> list[dict]:
        return self._get("/api/tokens")

    def token_stats(self) -> dict:
        return self._get("/api/tokens/stats")

    def add_token(self, label: str, secret: str, max_concurrency: int = 1) -> dict:
        return self._post(
            "/api/tokens",
            {"label": label, "secret": secret, "max_concurrency": max_concurrency},
        )

    def add_tokens_bulk(self, blob: str, label_prefix: str = "bulk") -> dict:
        return self._post(
            "/api/tokens/bulk", {"blob": blob, "label_prefix": label_prefix}
        )

    def import_xlsx(self, path: str | Path, exchange: bool = True) -> dict:
        """Import provider xlsx accounts (file must be on the hub machine)."""
        return self._post(
            "/api/tokens/import-xlsx", {"path": str(path), "exchange": exchange}
        )

    def set_token_status(self, token_id: int, status: str) -> dict:
        return self._post(f"/api/tokens/{token_id}/status", {"status": status})

    def set_token_utilization(self, token_id: int, max_concurrency: int) -> dict:
        """账户利用率: this account's auth.json in how many sandboxes (default 1)."""
        return self._post(
            f"/api/tokens/{token_id}/concurrency", {"max_concurrency": max_concurrency}
        )

    # ── alerts ───────────────────────────────────────────────────────── #

    def alerts(self) -> list[dict]:
        return self._get("/api/alerts")

    def resolve_alert(self, alert_id: int) -> dict:
        return self._post(f"/api/alerts/{alert_id}/resolve")


class AsyncTaskHandle:
    def __init__(self, client, task_id: int, num_items: int):
        self._c = client
        self.id = task_id
        self.num_items = num_items

    async def status(self) -> dict:
        return await self._c._get(f"/api/tasks/{self.id}")

    async def wait(self, timeout_s: float | None = None, poll_s: float = 2.0) -> dict:
        import asyncio

        t0 = time.time()
        while True:
            t = await self.status()
            if t["status"] in TERMINAL:
                return t
            if timeout_s is not None and time.time() - t0 > timeout_s:
                raise TimeoutError(f"task {self.id} still {t['status']} after {timeout_s}s")
            await asyncio.sleep(poll_s)

    async def results(self) -> list[dict]:
        return await self._c._get(f"/api/tasks/{self.id}/results")

    async def stop(self) -> None:
        await self._c._post(f"/api/tasks/{self.id}/stop")

    async def cancel(self) -> None:
        await self._c._post(f"/api/tasks/{self.id}/cancel")


class AsyncHubClient:
    """Async client — same surface as HubClient with async methods."""

    def __init__(self, base_url: str = DEFAULT_URL, timeout_s: float = 60.0):
        self._url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._url, timeout=timeout_s)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.close()

    async def _get(self, path: str) -> Any:
        r = await self._http.get(path)
        if r.status_code >= 400:
            raise HubError(r.json().get("detail", r.text))
        return r.json()

    async def _post(self, path: str, json: dict | None = None) -> Any:
        r = await self._http.post(path, json=json)
        if r.status_code >= 400:
            raise HubError(r.json().get("detail", r.text))
        return r.json()

    async def health(self) -> dict:
        return await self._get("/api/health")

    async def submit(self, **kw) -> AsyncTaskHandle:
        kw.pop("self", None)
        body: dict[str, Any] = {
            "name": kw.get("name") or f"sdk-{int(time.time())}",
            "prompt_template": kw.get(
                "prompt", "过滤以下训练数据并给出质量分(1-10)，只输出JSON: {{item}}"
            ),
            "concurrency": kw.get("concurrency", 8),
        }
        if kw.get("items"):
            body["items"] = kw["items"]
        if kw.get("items_file"):
            body["items_file"] = str(kw["items_file"])
        if kw.get("backend"):
            body["backend"] = kw["backend"]
        if kw.get("input_files"):
            body["input_files"] = [str(f) for f in kw["input_files"]]
        codex_config: dict = {}
        if kw.get("model"):
            codex_config["model"] = kw["model"]
        body["codex_config"] = codex_config
        d = await self._post("/api/tasks", body)
        return AsyncTaskHandle(self, d["task_id"], d["num_items"])

    async def tasks(self) -> list[dict]:
        return await self._get("/api/tasks")

    async def task(self, task_id: int) -> AsyncTaskHandle:
        await self._get(f"/api/tasks/{task_id}")
        return AsyncTaskHandle(self, task_id, 0)

    async def tokens(self) -> list[dict]:
        return await self._get("/api/tokens")

    async def token_stats(self) -> dict:
        return await self._get("/api/tokens/stats")

    async def add_token(self, label: str, secret: str, max_concurrency: int = 1) -> dict:
        return await self._post(
            "/api/tokens",
            {"label": label, "secret": secret, "max_concurrency": max_concurrency},
        )

    async def add_tokens_bulk(self, blob: str, label_prefix: str = "bulk") -> dict:
        return await self._post(
            "/api/tokens/bulk", {"blob": blob, "label_prefix": label_prefix}
        )

    async def import_xlsx(self, path: str | Path, exchange: bool = True) -> dict:
        return await self._post(
            "/api/tokens/import-xlsx", {"path": str(path), "exchange": exchange}
        )

    async def set_token_status(self, token_id: int, status: str) -> dict:
        return await self._post(f"/api/tokens/{token_id}/status", {"status": status})

    async def set_token_utilization(self, token_id: int, max_concurrency: int) -> dict:
        return await self._post(
            f"/api/tokens/{token_id}/concurrency", {"max_concurrency": max_concurrency}
        )

    async def alerts(self) -> list[dict]:
        return await self._get("/api/alerts")

    async def resolve_alert(self, alert_id: int) -> dict:
        return await self._post(f"/api/alerts/{alert_id}/resolve")
