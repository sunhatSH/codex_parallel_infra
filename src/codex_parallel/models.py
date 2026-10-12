"""ORM models: Token / Task / TaskItem / WorkerRun.

Everything the hub knows lives here. The DB is the single source of truth so a
hub restart can recover running tasks (see scheduler.recover_orphans).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Token(Base):
    """A Codex auth credential the pool can hand out.

    secret_path points at a 0600 file under secrets_dir holding the raw
    credential (auth.json content or API key). The DB never stores the secret
    itself, only the path and a short fingerprint for display.
    """

    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(128), unique=True)
    secret_path: Mapped[str] = mapped_column(String(512))
    fingerprint: Mapped[str] = mapped_column(String(32), default="")
    # active | cooldown | banned | discarded | disabled
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    in_use: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    banned_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    banned_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cooldown_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def available(self) -> bool:
        return (
            self.status == "active"
            and self.in_use < self.max_concurrency
            and (self.cooldown_until is None or self.cooldown_until < utcnow())
        )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    prompt_template: Mapped[str] = mapped_column(Text)
    # pending | running | stopping | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    backend: Mapped[str] = mapped_column(String(32), default="local")
    concurrency: Mapped[int] = mapped_column(Integer, default=8)
    codex_config: Mapped[dict] = mapped_column(JSON, default=dict)
    hub_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["TaskItem"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class TaskItem(Base):
    __tablename__ = "task_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
    # pending | assigned | running | done | failed | timeout | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id"), nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    assigned_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    task: Mapped[Task] = relationship(back_populates="items")


class WorkerRun(Base):
    """One sandbox+codex execution attempt against one item."""

    __tablename__ = "worker_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("task_items.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id"), nullable=True)
    hub_id: Mapped[str] = mapped_column(String(64))
    worker_slot: Mapped[int] = mapped_column(Integer)
    # running | done | failed | timeout | auth_failed
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    heartbeat_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


def make_engine(db_url: str):
    return create_engine(db_url, connect_args={"timeout": 30})


def init_db(engine) -> sessionmaker:
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
