"""Scheduler: the hub's core loop.

One asyncio task per running Task, one worker coroutine per concurrency slot.
Workers claim items, acquire tokens, create sandboxes, run codex, write
results. All cross-process claims go through atomic UPDATE ... WHERE guards
so multiple hub processes can share one DB safely.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import func, select, update

from .backends import make_backend
from .attribution import MAX_STRIKES, attribute
from .codex_runner import CodexRunner, CodexRunResult
from .config import HubConfig
from .health import HealthMonitor
from .models import PoolAlert, Task, TaskItem, Token, WorkerRun, utcnow
from .token_pool import _as_utc
from .token_pool import NoTokenAvailable, TokenPool

log = logging.getLogger("codex_parallel.scheduler")

WORKER_POLL_S = 2.0
TOKEN_WAIT_BASE_S = 2.0


class Scheduler:
    def __init__(self, session_factory, token_pool: TokenPool, cfg: HubConfig):
        self._sf = session_factory
        self._tokens = token_pool
        self._cfg = cfg
        self.hub_id = f"hub-{uuid.uuid4().hex[:8]}"
        self.health = HealthMonitor(session_factory)
        self._loops: dict[int, asyncio.Task] = {}
        self._backends: dict[str, object] = {}

    # ── session helper ───────────────────────────────────────────────── #

    @contextmanager
    def _session(self):
        s = self._sf()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def _backend(self, name: str):
        if name not in self._backends:
            kwargs = {}
            if name == "local":
                kwargs["workspace_root"] = self._cfg.workspace_root
            self._backends[name] = make_backend(name, **kwargs)
        return self._backends[name]

    # ── task lifecycle ───────────────────────────────────────────────── #

    def adopt_pending_tasks(self) -> list[int]:
        """Atomically claim pending tasks (multi-hub safe).

        Pure DB work: mark claimed tasks running + owned by this hub. Loop
        spawning happens on the event loop (see spawn_pending), because
        create_task needs a running loop and this method runs in a thread.
        """
        adopted: list[int] = []
        with self._session() as s:
            pending = list(
                s.execute(select(Task).where(Task.status == "pending")).scalars()
            )
            for task in pending:
                claimed = s.execute(
                    update(Task)
                    .where(Task.id == task.id, Task.status == "pending")
                    .values(status="running", hub_id=self.hub_id, started_at=utcnow())
                )
                if claimed.rowcount == 1:
                    adopted.append(task.id)
        return adopted

    def spawn_pending_tasks(self) -> list[int]:
        """Spawn loops for running tasks owned by THIS hub (event-loop side)."""
        spawned: list[int] = []
        with self._session() as s:
            mine = list(
                s.execute(
                    select(Task.id).where(
                        Task.status == "running", Task.hub_id == self.hub_id
                    )
                ).scalars()
            )
        for tid in mine:
            self._spawn_loop(tid)
            spawned.append(tid)
        return spawned

    def recover_orphans(self) -> int:
        """On startup: requeue items left mid-flight by a dead hub."""
        n = 0
        with self._session() as s:
            orphaned = list(
                s.execute(
                    select(Task).where(
                        Task.status.in_(("running", "stopping")),
                        Task.hub_id != self.hub_id,
                    )
                ).scalars()
            )
            for task in orphaned:
                # A task owned by nobody (hub gone) → reset to pending so this
                # hub (or another) adopts it. hub_id != ours + no heartbeat is
                # handled by adopt-time reset below.
                s.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(status="pending", hub_id=None)
                )
                n += 1
            # any assigned/running item of a not-running task goes back to pending
            s.execute(
                update(TaskItem)
                .where(
                    TaskItem.status.in_(("assigned", "running")),
                    TaskItem.task_id.in_(select(Task.id).where(Task.status == "pending")),
                )
                .values(status="pending", assigned_at=None)
            )
            # free tokens whose in_use leaked from a dead hub
            s.execute(update(Token).values(in_use=0))
        return n

    def request_stop(self, task_id: int) -> None:
        with self._session() as s:
            s.execute(
                update(Task)
                .where(Task.id == task_id, Task.status.in_(("running", "stopping")))
                .values(status="stopping")
            )
            s.commit()

    def cancel_task(self, task_id: int) -> None:
        self.request_stop(task_id)
        with self._session() as s:
            s.execute(
                update(TaskItem)
                .where(
                    TaskItem.task_id == task_id,
                    TaskItem.status.in_(("pending", "assigned")),
                )
                .values(status="cancelled", finished_at=utcnow())
            )
            s.commit()

    # ── item claiming ────────────────────────────────────────────────── #

    def _claim_next_item(self, task_id: int) -> TaskItem | None:
        with self._session() as s:
            stmt = (
                select(TaskItem)
                .where(TaskItem.task_id == task_id, TaskItem.status == "pending")
                .order_by(TaskItem.seq)
                .limit(1)
            )
            item = s.execute(stmt).scalar_one_or_none()
            if item is None:
                return None
            claimed = s.execute(
                update(TaskItem)
                .where(TaskItem.id == item.id, TaskItem.status == "pending")
                .values(status="assigned", assigned_at=utcnow())
            )
            if claimed.rowcount != 1:
                return None  # another hub won; caller retries
            s.refresh(item)
            return item

    def _task_status(self, task_id: int) -> str:
        with self._session() as s:
            t = s.get(Task, task_id)
            return t.status if t else "gone"

    def _should_run(self, task_id: int) -> bool:
        st = self._task_status(task_id)
        if st == "stopping":
            # stopping lets in-flight items finish but claims no new ones;
            # fully stop once nothing is pending
            return self._count_pending(task_id) > 0 and False or st == "running"
        return st == "running"

    def _count_pending(self, task_id: int) -> int:
        with self._session() as s:
            return (
                s.execute(
                    select(func.count())
                    .select_from(TaskItem)
                    .where(TaskItem.task_id == task_id, TaskItem.status == "pending")
                ).scalar()
                or 0
            )

    def _count_active_items(self, task_id: int) -> int:
        with self._session() as s:
            return (
                s.execute(
                    select(func.count())
                    .select_from(TaskItem)
                    .where(
                        TaskItem.task_id == task_id,
                        TaskItem.status.in_(("pending", "assigned", "running")),
                    )
                ).scalar()
                or 0
            )

    def _finalize_task(self, task_id: int, status: str) -> None:
        with self._session() as s:
            s.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(status=status, finished_at=utcnow(), hub_id=None)
            )

    # ── worker loop ──────────────────────────────────────────────────── #

    def _spawn_loop(self, task_id: int) -> None:
        existing = self._loops.get(task_id)
        if existing and not existing.done():
            return
        with self._session() as s:
            task = s.get(Task, task_id)
            concurrency = task.concurrency if task else 1
            backend_name = task.backend if task else "local"
            codex_config = dict(task.codex_config) if task else {}
        loop_task = asyncio.create_task(
            self._task_loop(task_id, concurrency, backend_name, codex_config),
            name=f"task-{task_id}",
        )
        self._loops[task_id] = loop_task

    async def _task_loop(
        self, task_id: int, concurrency: int, backend_name: str, codex_config: dict
    ) -> None:
        log.info("task %s: starting %d workers, backend=%s", task_id, concurrency, backend_name)
        workers = [
            asyncio.create_task(
                self._worker_loop(task_id, slot, backend_name, codex_config),
                name=f"task-{task_id}-w{slot}",
            )
            for slot in range(concurrency)
        ]
        try:
            while True:
                alive = [w for w in workers if not w.done()]
                if not alive:
                    if (
                        self._task_status(task_id) == "running"
                        and self._count_pending(task_id) > 0
                    ):
                        for i, w in enumerate(workers):
                            if w.done():
                                workers[i] = asyncio.create_task(
                                    self._worker_loop(task_id, i, backend_name, codex_config),
                                    name=f"task-{task_id}-w{i}",
                                )
                        await asyncio.sleep(WORKER_POLL_S)
                        continue
                    break  # genuinely finished
                # stopping/cancelled with nothing in flight → wind down
                if (
                    self._task_status(task_id) in ("stopping", "cancelled")
                    and self._count_active_items(task_id) == 0
                ):
                    break
                # all items terminal and no pending left → done, even if
                # workers are still in their idle poll
                if self._count_active_items(task_id) == 0:
                    for w in workers:
                        w.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    break
                await asyncio.sleep(WORKER_POLL_S)
            # task finished
            st = self._task_status(task_id)
            if st in ("stopping", "cancelled"):
                self._finalize_task(task_id, "cancelled")
            else:
                failed = self._count_items(task_id, ("failed", "timeout"))
                total = self._count_items(task_id, tuple())
                if failed >= total and total > 0:
                    self._finalize_task(task_id, "failed")
                else:
                    self._finalize_task(task_id, "done")
        finally:
            for w in workers:
                if not w.done():
                    w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._loops.pop(task_id, None)
            log.info("task %s: all workers exited", task_id)

    def _count_items(self, task_id: int, statuses: tuple) -> int:
        with self._session() as s:
            q = select(func.count()).select_from(TaskItem).where(TaskItem.task_id == task_id)
            if statuses:
                q = q.where(TaskItem.status.in_(statuses))
            return s.execute(q).scalar() or 0

    async def _worker_loop(
        self, task_id: int, slot: int, backend_name: str, codex_config: dict
    ) -> None:
        runner = CodexRunner(
            codex_bin=codex_config.get("codex_bin", "codex"),
            model=codex_config.get("model"),
            sandbox_mode=codex_config.get("sandbox_mode", "workspace-write"),
            reasoning_effort=codex_config.get("reasoning_effort"),
            extra_args=list(codex_config.get("extra_args", [])),
        )
        while True:
            st = self._task_status(task_id)
            if st != "running":
                if st == "stopping" and self._count_pending(task_id) > 0:
                    pass  # drain pending
                else:
                    return
            item = await asyncio.to_thread(self._claim_next_item, task_id)
            if item is None:
                # nothing pending: exit if stopping, else brief idle poll
                if self._task_status(task_id) != "running":
                    return
                await asyncio.sleep(WORKER_POLL_S)
                continue
            await self._process_item(item, slot, backend_name, runner)
            # yield so the task-loop supervisor gets a chance to observe state
            await asyncio.sleep(0)

    async def _process_item(
        self, item: TaskItem, slot: int, backend_name: str, runner: CodexRunner
    ) -> None:
        """One item through the checkpointed sandbox-lifecycle policy.

        Every attempt = checkpoint (WorkerRun row) + fresh sandbox. When a
        sandbox's codex dies (crash / transient / timeout / auth swap), the
        finally-block KILLS that sandbox and the loop CREATES a new one for
        the next attempt — dead sandboxes never get reused.

        A token IS an account (1:1, pool enforces max_concurrency=1). Before
        the task attempt runs, a login probe verifies the account inside the
        sandbox: ordinary offline → retry same account 3x (fresh sandbox each
        time); explicit auth rejection (401/403/invalid) → token marked
        unusable immediately and a different account is pulled.

        - account_dead      → swap to a NEW token immediately (token banned)
        - transient/timeout/failed → retry SAME token, 3 chances total
        - 3 strikes on one token → discard that token, pull a new one
        - out of tokens / attempts → item fails + pool-exhausted alert fires
        """
        task_id = item.task_id
        item_id = item.id
        with self._session() as s:
            task = s.get(Task, task_id)
            prompt = task.prompt_template.replace("{{item}}", item.payload)
        token = None
        token_strikes = 0
        total_swaps = 0
        while True:
            if token is None:
                token = await self._acquire_token_with_backoff(quick_exhausted=True)
                if token is None:
                    self._alert_pool_exhausted()
                    self._fail_item(item_id, "no tokens available")
                    return
                token_strikes = 0
            run_id = None
            sandbox = None
            try:
                sandbox = await self._backend(backend_name).create()
                with self._session() as s:
                    run = WorkerRun(
                        item_id=item_id,
                        task_id=task_id,
                        token_id=token.id,
                        hub_id=self.hub_id,
                        worker_slot=slot,
                    )
                    s.add(run)
                    s.commit()
                    run_id = run.id
                    s.execute(
                        update(TaskItem)
                        .where(TaskItem.id == item_id)
                        .values(status="running", token_id=token.id)
                    )
                    s.commit()
                secret = self._tokens.read_secret(token.id)

                # ── checkpoint: account login probe (fresh sandbox, this token)
                probe = await self._login_probe(sandbox, secret)
                if probe.account_dead:
                    # account-level failure: token unusable NOW, no retries
                    self._tokens.report_banned(token.id, reason=f"login probe: {probe.detail[:100]}")
                    log.warning(
                        "item %s: account %s dead at login (token banned)",
                        item_id, token.label,
                    )
                    token = None  # finally-block destroys this sandbox
                    total_swaps += 1
                    continue
                if not probe.ok:
                    # ordinary offline: destroy sandbox, same account, ≤3 tries
                    token_strikes += 1
                    log.info(
                        "item %s: login transient (%s), strike %d/3",
                        item_id, probe.detail[:80], token_strikes,
                    )
                    if token_strikes >= MAX_STRIKES:
                        self._tokens.report_discard(
                            token.id, reason="login transient x3"
                        )
                        token = None
                        continue
                    await asyncio.sleep(min(2**token_strikes, 15))
                    continue

                env = await runner.prepare_auth(sandbox, secret)
                hb = asyncio.create_task(
                    self._heartbeat_loop(run_id), name=f"hb-{run_id}"
                )
                try:
                    res: CodexRunResult = await runner.run(
                        sandbox, prompt, timeout_s=self._cfg.item_timeout_s, env=env
                    )
                finally:
                    hb.cancel()
                    await asyncio.gather(hb, return_exceptions=True)
                last_msg = res.last_message or await runner.fetch_last_message(sandbox)

                if res.ok:
                    result_path = self._persist_result(task_id, item_id, last_msg, res, token)
                    self._complete_item(item_id, result_path, res)
                    # token proved healthy — clear its accumulated strikes
                    with self._session() as s:
                        s.execute(
                            update(Token).where(Token.id == token.id).values(fail_count=0)
                        )
                    return

                att = attribute(res, token_strikes=token_strikes)
                if att.retire_token:
                    if att.kind == "account_dead":
                        self._tokens.report_banned(token.id, reason=att.reason)
                    else:
                        self._tokens.report_discard(token.id, reason=att.reason)
                    log.warning(
                        "item %s: token %s retired (%s)", item_id, token.label, att.reason
                    )
                else:
                    self._tokens.report_failure(token.id)

                if att.swap_token:
                    token = None
                    total_swaps += 1
                    if total_swaps > 20:  # safety valve against loops
                        self._alert_pool_exhausted()
                        self._fail_item(item_id, "too many token swaps")
                        return
                    continue
                # timeout/transient/failed → checkpoint passed; next attempt
                # runs in a NEW sandbox (kill happens in finally)
                if res.timed_out:
                    with self._session() as s:
                        s.execute(
                            update(WorkerRun)
                            .where(WorkerRun.id == run_id)
                            .values(status="timeout", finished_at=utcnow())
                        )
                token_strikes += 1
                if token_strikes >= MAX_STRIKES:
                    # this token is exhausted even without retire_token from
                    # attribute (e.g. crash path parity); discard and swap
                    self._tokens.report_discard(token.id, reason="strikes exhausted")
                    token = None
                    continue
                await asyncio.sleep(min(2**token_strikes, 15) + random.random())
            except Exception as e:  # noqa: BLE001
                log.exception("item %s crashed: %s", item_id, e)
                token_strikes += 1
                if token is not None:
                    try:
                        self._tokens.report_failure(token.id)
                        if token_strikes >= MAX_STRIKES:
                            self._tokens.report_discard(token.id, reason="crash x3")
                            token = None
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(1.0)
            finally:
                if sandbox is not None:
                    try:
                        await sandbox.kill()
                    except Exception:  # noqa: BLE001
                        pass
                if token is not None:
                    try:
                        self._tokens.release(token.id)
                    except Exception:  # noqa: BLE001
                        pass
                if run_id is not None:
                    with self._session() as s:
                        run = s.get(WorkerRun, run_id)
                        if run and run.status == "running":
                            run.status = "failed"
                            run.finished_at = utcnow()

    async def _heartbeat_loop(self, run_id: int) -> None:
        while True:
            await asyncio.sleep(self._cfg.worker_heartbeat_s)
            await self.health.beat(run_id)

    # ── login probe ──────────────────────────────────────────────────── #

    async def _login_probe(self, sandbox, secret: str):
        """Verify the account credential inside this sandbox before the task.

        Wrapped so a probe crash degrades to "transient" (ordinary offline,
        retryable) rather than killing the worker.
        """
        from .login_probe import LoginProber

        if not hasattr(self, "_prober"):
            self._prober = LoginProber(timeout_s=self._cfg.login_probe_timeout_s)
        try:
            return await self._prober.probe(sandbox, secret)
        except Exception as e:  # noqa: BLE001
            log.warning("login probe crashed: %s", e)
            from .login_probe import LoginCheckResult

            return LoginCheckResult(False, False, True, f"probe crash: {e}")

    # ── pool-exhaustion alerting ─────────────────────────────────────── #

    def _alert_pool_exhausted(self) -> None:
        """Token pool empty: record alert (DB) so CLI + web can surface it."""
        try:
            with self._session() as s:
                alert = PoolAlert(
                    message="token pool exhausted: no usable accounts (active=0, "
                    "all banned/discarded or never imported)",
                    hub_id=self.hub_id,
                )
                s.add(alert)
                s.commit()
            log.error("TOKEN POOL EXHAUSTED — import tokens via CLI/web: "
                      "codex-parallel token add-bulk")
        except Exception:  # noqa: BLE001
            log.exception("failed to record pool alert")

    async def _acquire_token_with_backoff(
        self, *, max_wait_s: float = 600.0, quick_exhausted: bool = False
    ):
        """Get a token or None.

        quick_exhausted=True → return None as soon as the pool is definitively
        empty (no active tokens at all) instead of waiting out the backoff.
        Used on retry loops where waiting would only stall the item.
        """
        if quick_exhausted:
            stats = self._tokens.stats()
            if stats["active"] == 0:
                return None
        waited = 0.0
        delay = TOKEN_WAIT_BASE_S
        while waited < max_wait_s:
            try:
                return await asyncio.to_thread(self._tokens.acquire)
            except NoTokenAvailable:
                if quick_exhausted:
                    stats = self._tokens.stats()
                    if stats["active"] == 0:
                        return None
                await asyncio.sleep(delay + random.random())
                waited += delay
                delay = min(delay * 2, 30.0)
        return None

    # ── item outcome writers ─────────────────────────────────────────── #

    def _complete_item(self, item_id: int, result_path: str, res: CodexRunResult) -> None:
        with self._session() as s:
            item = s.get(TaskItem, item_id)
            if item is None:
                return
            item.status = "done"
            item.result_path = result_path
            item.error = None
            item.finished_at = utcnow()
            if item.assigned_at is not None:
                item.duration_s = (utcnow() - _as_utc(item.assigned_at)).total_seconds()
            run = (
                s.execute(
                    select(WorkerRun)
                    .where(WorkerRun.item_id == item_id, WorkerRun.status == "running")
                )
                .scalars()
                .first()
            )
            if run:
                run.status = "done"
                run.finished_at = utcnow()
                run.exit_code = res.exit_code

    def _fail_item(self, item_id: int, error: str) -> None:
        with self._session() as s:
            item = s.get(TaskItem, item_id)
            if item is None:
                return
            item.status = "failed"
            item.error = error[:4000]
            item.finished_at = utcnow()
            if item.assigned_at is not None:
                item.duration_s = (utcnow() - _as_utc(item.assigned_at)).total_seconds()
            s.execute(
                update(WorkerRun)
                .where(WorkerRun.item_id == item_id, WorkerRun.status == "running")
                .values(status="failed", finished_at=utcnow(), error=error[:4000])
            )

    def _timeout_item(self, item_id: int, res: CodexRunResult) -> None:
        with self._session() as s:
            item = s.get(TaskItem, item_id)
            if item is None:
                return
            item.status = "timeout"
            item.error = f"timeout after {self._cfg.item_timeout_s}s"
            item.finished_at = utcnow()
            if item.assigned_at is not None:
                item.duration_s = (utcnow() - _as_utc(item.assigned_at)).total_seconds()
            s.execute(
                update(WorkerRun)
                .where(WorkerRun.item_id == item_id, WorkerRun.status == "running")
                .values(status="timeout", finished_at=utcnow())
            )

    def _persist_result(
        self, task_id: int, item_id: int, last_message: str, res: CodexRunResult, token=None
    ) -> str:
        out_dir = self._cfg.results_dir / str(task_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"item-{item_id}.json"
        payload = {
            "item_id": item_id,
            "task_id": task_id,
            "last_message": last_message,
            "exit_code": res.exit_code,
            "events": res.events[-200:],
        }
        text = __import__("json").dumps(payload, ensure_ascii=False, indent=2)
        # events carry raw codex stdout; scrub credential-shaped strings so a
        # leaked auth.json can never ride along into results storage
        if token is not None and token.fingerprint:
            secret = self._tokens.read_secret(token.id)
            if secret:
                text = text.replace(secret, "[REDACTED]")
        path.write_text(text)
        return str(path)

    # ── service control ──────────────────────────────────────────────── #

    async def start(self) -> None:
        await self.health.start()
        recovered = await asyncio.to_thread(self.recover_orphans)
        if recovered:
            log.info("recovered %d orphaned tasks", recovered)
        adopted = await asyncio.to_thread(self.adopt_pending_tasks)
        spawned = self.spawn_pending_tasks()
        log.info(
            "hub %s up: adopted=%s spawned=%s", self.hub_id, adopted, spawned
        )

    async def stop(self) -> None:
        await self.health.stop()
        for t in list(self._loops.values()):
            t.cancel()
        await asyncio.gather(*self._loops.values(), return_exceptions=True)
        self._loops.clear()
        for be in self._backends.values():
            await be.close()
