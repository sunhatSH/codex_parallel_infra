"""DB bootstrap: engine + session factory with SQLite WAL for multi-hub safety."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, text

from .models import init_db


def bootstrap(db_path: str | Path):
    """Create engine + sessionmaker, enable WAL, create tables."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = _make_engine(path)
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.commit()
    return init_db(engine)


def _make_engine(path: Path):
    from sqlalchemy import create_engine

    return create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
