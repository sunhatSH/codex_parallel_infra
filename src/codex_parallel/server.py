"""FastAPI hub service: task CRUD, token management, status.

Pure JSON API — the frontend lives in web-react/ (separate Vite+React app)
and the CLI in cli/ (separate package). CORS is open for the dev server;
in prod serve web-react/dist from any static host pointing at this API.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import HubConfig, load_config
from .db import bootstrap
from .models import Task, TaskItem, Token, utcnow
from .scheduler import Scheduler
from .token_pool import TokenPool


class TaskIn(BaseModel):
    name: str
    prompt_template: str
    items: list[str] = Field(default_factory=list)
    items_file: str | None = None
    # files/dirs on the HUB machine copied into every sandbox's workspace
    # before codex runs. The model sees them in its cwd.
    input_files: list[str] = Field(default_factory=list)
    concurrency: int = 8
    backend: str = "local"
    codex_config: dict = Field(default_factory=dict)


class TokenIn(BaseModel):
    label: str
    secret: str
    max_concurrency: int = 1


class TokenBulkIn(BaseModel):
    blob: str
    label_prefix: str = "bulk"
    max_concurrency: int = 1


class TokenStatusIn(BaseModel):
    status: str  # active | disabled


def create_app(cfg: HubConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    cfg.ensure_dirs()

    # 全局代理: hub 进程环境一次注入，所有子进程/库继承——
    # codex 子进程（local backend）、RT 兑换（urllib 读 *_PROXY）、e2b SDK。
    if cfg.proxy:
        os.environ["HTTP_PROXY"] = cfg.proxy
        os.environ["HTTPS_PROXY"] = cfg.proxy
        os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)

    session_factory = bootstrap(cfg.db_path)
    token_pool = TokenPool(session_factory, cfg.secrets_dir)

    app = FastAPI(title="codex-parallel-hub", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # standalone frontend; restrict in prod
        allow_methods=["*"],
        allow_headers=["*"],
    )
    scheduler: Scheduler | None = None

    def get_scheduler() -> Scheduler:
        nonlocal scheduler
        if scheduler is None:
            raise HTTPException(503, "scheduler not started")
        return scheduler

    @app.on_event("startup")
    async def _startup() -> None:
        nonlocal scheduler
        scheduler = Scheduler(session_factory, token_pool, cfg)
        await scheduler.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if scheduler is not None:
            await scheduler.stop()

    # ── health / info ────────────────────────────────────────────────── #

    @app.get("/api/health")
    async def health():
        return {"ok": True, "hub_id": scheduler.hub_id if scheduler else None}

    # ── tasks ────────────────────────────────────────────────────────── #

    @app.post("/api/tasks")
    async def create_task(t: TaskIn):
        items = list(t.items)
        if t.items_file:
            from pathlib import Path

            path = Path(t.items_file).expanduser()
            if not path.is_file():
                raise HTTPException(400, f"items_file not found: {t.items_file}")
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
            items.extend(lines)
        if not items:
            raise HTTPException(400, "no items: provide items or items_file")
        if "{{item}}" not in t.prompt_template:
            raise HTTPException(400, "prompt_template must contain {{item}}")
        # validate input files up front (fail fast, before any sandbox spins)
        from pathlib import Path as _P

        for f in t.input_files:
            p = _P(f).expanduser()
            if not p.exists():
                raise HTTPException(400, f"input_file not found: {f}")
        with session_factory() as s:
            task = Task(
                name=t.name,
                prompt_template=t.prompt_template,
                backend=t.backend,
                concurrency=t.concurrency,
                codex_config=t.codex_config,
                input_files=list(t.input_files),
            )
            s.add(task)
            s.flush()
            for i, payload in enumerate(items):
                s.add(TaskItem(task_id=task.id, seq=i, payload=payload))
            s.commit()
            tid = task.id
        sched = get_scheduler()
        # adopt (mark running + owned) then spawn the worker loop
        with session_factory() as s:
            s.execute(
                __import__("sqlalchemy").update(Task)
                .where(Task.id == tid, Task.status == "pending")
                .values(status="running", hub_id=sched.hub_id, started_at=utcnow())
            )
            s.commit()
        sched._spawn_loop(tid)
        return {"task_id": tid, "num_items": len(items)}

    @app.get("/api/tasks")
    async def list_tasks():
        with session_factory() as s:
            tasks = list(s.query(Task).order_by(Task.id.desc()).all())
            out = []
            for t in tasks:
                counts = {"pending": 0, "assigned": 0, "running": 0, "done": 0,
                          "failed": 0, "timeout": 0, "cancelled": 0}
                for it in s.query(TaskItem).filter(TaskItem.task_id == t.id):
                    counts[it.status] = counts.get(it.status, 0) + 1
                out.append({
                    "id": t.id, "name": t.name, "status": t.status,
                    "backend": t.backend, "concurrency": t.concurrency,
                    "hub_id": t.hub_id, "created_at": iso(t.created_at),
                    "started_at": iso(t.started_at), "finished_at": iso(t.finished_at),
                    "items": counts, "total": sum(counts.values()),
                })
            return out

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: int):
        with session_factory() as s:
            t = s.get(Task, task_id)
            if t is None:
                raise HTTPException(404, "task not found")
            items = (
                s.query(TaskItem)
                .filter(TaskItem.task_id == task_id)
                .order_by(TaskItem.seq)
                .all()
            )
            return {
                "id": t.id, "name": t.name, "status": t.status,
                "backend": t.backend, "concurrency": t.concurrency,
                "prompt_template": t.prompt_template,
                "codex_config": t.codex_config, "hub_id": t.hub_id,
                "created_at": iso(t.created_at), "started_at": iso(t.started_at),
                "finished_at": iso(t.finished_at),
                "items": [{
                    "id": it.id, "seq": it.seq, "status": it.status,
                    "retry_count": it.retry_count, "token_id": it.token_id,
                    "result_path": it.result_path, "error": it.error,
                    "duration_s": it.duration_s,
                    "payload": it.payload[:200] + ("…" if len(it.payload) > 200 else ""),
                } for it in items],
            }

    @app.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: int):
        get_scheduler().request_stop(task_id)
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: int):
        get_scheduler().cancel_task(task_id)
        return {"ok": True}

    @app.get("/api/tasks/{task_id}/results")
    async def task_results(task_id: int):
        out_dir = cfg.results_dir / str(task_id)
        if not out_dir.is_dir():
            return []
        out = []
        for p in sorted(out_dir.glob("item-*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except json.JSONDecodeError:
                continue
        return out

    # ── tokens ───────────────────────────────────────────────────────── #

    @app.post("/api/tokens")
    async def add_token(t: TokenIn):
        try:
            tok = token_pool.add_token(t.label, t.secret, max_concurrency=t.max_concurrency)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": tok.id, "label": tok.label, "fingerprint": tok.fingerprint}

    @app.post("/api/tokens/bulk")
    async def add_tokens_bulk(t: TokenBulkIn):
        """Paste a newline-separated token blob; each line becomes one token."""
        created = token_pool.bulk_add_tokens(
            t.blob, label_prefix=t.label_prefix, max_concurrency=t.max_concurrency
        )
        return {"created": len(created), "ids": [c.id for c in created]}

    @app.get("/api/tokens")
    async def list_tokens():
        return [serialize_token(t) for t in token_pool.list_tokens()]

    @app.get("/api/tokens/stats")
    async def token_stats():
        return token_pool.stats()

    @app.post("/api/tokens/{token_id}/status")
    async def set_token_status(token_id: int, body: TokenStatusIn):
        try:
            token_pool.set_status(token_id, body.status)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/tokens/{token_id}/concurrency")
    async def set_token_concurrency(token_id: int, body: dict):
        """Account utilization: how many sandboxes may use this auth.json
        simultaneously. Default 1 (one account, one sandbox)."""
        from sqlalchemy import update

        n = body.get("max_concurrency")
        if not isinstance(n, int) or n < 1:
            raise HTTPException(400, "max_concurrency must be int >= 1")
        with session_factory() as s:
            s.execute(
                update(Token).where(Token.id == token_id).values(max_concurrency=n)
            )
            s.commit()
        return {"ok": True, "max_concurrency": n}

    @app.post("/api/tokens/import-xlsx")
    async def import_xlsx(body: dict):
        """Import accounts from a provider xlsx.

        Two input modes:
          {"path": "/abs/path/to.xlsx"}     — file on the HUB machine
          {"blob": "<base64 xlsx>"}         — uploaded file (web upload)

        Each row's token bundle becomes one account token (auth.json secret).
        RT exchange is attempted only for expired/missing access tokens.
        """
        from .xlsx_import import import_xlsx_to_pool

        path = body.get("path", "")
        blob_b64 = body.get("blob", "")
        if blob_b64:
            import base64
            import tempfile

            data = base64.b64decode(blob_b64)
            suffix = ".xlsx"
            with tempfile.NamedTemporaryFile(
                "wb", suffix=suffix, delete=False, dir=str(cfg.root)
            ) as f:
                f.write(data)
                tmp_path = f.name
            try:
                return await asyncio.to_thread(
                    import_xlsx_to_pool,
                    token_pool,
                    tmp_path,
                    exchange=body.get("exchange", True),
                )
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        if not path:
            raise HTTPException(400, "path or blob required")
        if not Path(path).expanduser().is_file():
            raise HTTPException(400, f"file not found: {path}")
        return await asyncio.to_thread(
            import_xlsx_to_pool, token_pool, path, exchange=body.get("exchange", True)
        )

    @app.get("/", include_in_schema=False)
    async def index():
        return {
            "service": "codex-parallel-hub",
            "api": "/docs",
            "frontend": "web-react/ (npm run dev)",
        }

    # ── pool alerts ──────────────────────────────────────────────────── #

    @app.get("/api/alerts")
    async def list_alerts():
        """Active pool alerts (web 弹窗 + CLI 拉取)。"""
        from sqlalchemy import select

        from .models import PoolAlert

        with session_factory() as s:
            alerts = list(
                s.execute(
                    select(PoolAlert)
                    .where(PoolAlert.status == "active")
                    .order_by(PoolAlert.id.desc())
                    .limit(20)
                ).scalars()
            )
            return [
                {
                    "id": a.id,
                    "message": a.message,
                    "hub_id": a.hub_id,
                    "created_at": iso(a.created_at),
                }
                for a in alerts
            ]

    @app.post("/api/alerts/{alert_id}/resolve")
    async def resolve_alert(alert_id: int):
        from .models import PoolAlert

        with session_factory() as s:
            a = s.get(PoolAlert, alert_id)
            if a is None:
                raise HTTPException(404, "alert not found")
            a.status = "resolved"
            a.resolved_at = utcnow()
            s.commit()
        return {"ok": True}

    @app.exception_handler(Exception)
    async def unhandled(request, exc):
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


def serialize_token(t) -> dict:
    import datetime as dt

    cooldown_left = None
    if t.cooldown_until is not None:
        cu = t.cooldown_until
        if cu.tzinfo is None:
            cu = cu.replace(tzinfo=dt.timezone.utc)
        left = (cu - dt.datetime.now(dt.timezone.utc)).total_seconds()
        cooldown_left = max(0, round(left))
    return {
        "id": t.id,
        "label": t.label,
        "fingerprint": t.fingerprint,
        "status": t.status,
        "in_use": t.in_use,
        "max_concurrency": t.max_concurrency,
        "fail_count": t.fail_count,
        "quota_window": t.quota_window,
        "cooldown_until": iso(t.cooldown_until),
        "cooldown_s_left": cooldown_left,
        "last_used_at": iso(t.last_used_at),
        "banned_reason": t.banned_reason,
        "created_at": iso(t.created_at),
    }


def iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(create_app, factory=True, host=cfg.host, port=cfg.port)
