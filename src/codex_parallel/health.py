"""Health tracking: worker heartbeats + stalled-run reaping.

Every worker run gets a WorkerRun row with a heartbeat_at the worker refreshes
while its sandbox is busy. A supervisor coroutine scans for runs whose
heartbeat is stale beyond the threshold — those sandboxes are presumed dead
(killed OOM, network partition, backend hiccup) and the item is requeued so
another worker picks it up.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select, update

from .models import TaskItem, Token, WorkerRun, utcnow


class HealthMonitor:
    def __init__(self, session_factory, *, stall_threshold_s: int = 180, interval_s: int = 30):
        self._session_factory = session_factory
        self.stall_threshold_s = stall_threshold_s
        self.interval_s = interval_s
        self._task: asyncio.Task | None = None

    async def beat(self, run_id: int) -> None:
        def _beat():
            with self._session_factory() as s:
                s.execute(
                    update(WorkerRun)
                    .where(WorkerRun.id == run_id)
                    .values(heartbeat_at=utcnow())
                )
                s.commit()

        await asyncio.to_thread(_beat)

    def reap_stalled(self) -> list[int]:
        """Mark runs with stale heartbeats failed; requeue their items.

        Returns the list of reaped item ids (for logging/tests).
        """
        cutoff = utcnow() - dt.timedelta(seconds=self.stall_threshold_s)
        reaped: list[int] = []
        with self._session_factory() as s:
            stalled = list(
                s.execute(
                    select(WorkerRun).where(
                        WorkerRun.status == "running",
                        WorkerRun.heartbeat_at < cutoff,
                    )
                ).scalars()
            )
            for run in stalled:
                item = s.get(TaskItem, run.item_id)
                if item is not None and item.status in ("running", "assigned"):
                    if item.retry_count < 3:
                        item.status = "pending"
                        item.assigned_at = None
                    else:
                        item.status = "failed"
                        item.error = "stalled too many times"
                        item.finished_at = utcnow()
                    reaped.append(item.id)
                run.status = "failed"
                run.error = f"heartbeat stale > {self.stall_threshold_s}s"
                run.finished_at = utcnow()
                if run.token_id is not None:
                    s.execute(
                        update(Token).where(Token.id == run.token_id).values(
                            in_use=Token.in_use - 1
                        )
                    )
            s.commit()
        return reaped

    async def start(self) -> None:
        if self._task is None or self._task.done():

            async def loop():
                while True:
                    await asyncio.sleep(self.interval_s)
                    try:
                        reaped = await asyncio.to_thread(self.reap_stalled)
                        if reaped:
                            self._log(f"reaped stalled items: {reaped}")
                    except Exception as e:  # noqa: BLE001
                        self._log(f"reaper error: {e}")

            self._task = asyncio.create_task(loop(), name="health-reaper")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @staticmethod
    def _log(msg: str) -> None:
        import logging

        logging.getLogger("codex_parallel.health").warning(msg)
