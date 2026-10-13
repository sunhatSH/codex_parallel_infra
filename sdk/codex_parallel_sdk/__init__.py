"""codex_parallel SDK — programmatic client for the hub.

Upstream services use this instead of the CLI/REST:

    from codex_parallel_sdk import HubClient

    hub = HubClient("http://127.0.0.1:8787")
    task = hub.submit(
        items=["数据1", "数据2"],
        prompt="过滤以下训练数据并打分: {{item}}",
        concurrency=10,
    )
    task.wait()                     # block until terminal state
    for r in task.results():
        print(r["last_message"])

Sync and async share one implementation (httpx.Client / AsyncClient).
The SDK adds nothing the REST API doesn't do — it's an ergonomic wrapper:
typed methods, polling, task handles, and honest errors.
"""

from .client import AsyncHubClient, HubClient, HubError, TaskHandle

__all__ = ["HubClient", "AsyncHubClient", "TaskHandle", "HubError"]
__version__ = "0.1.0"
