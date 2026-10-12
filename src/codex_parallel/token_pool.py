"""Token pool: acquire / release / ban, all under DB transactions.

Concurrency safety comes from SQLite WAL + BEGIN IMMEDIATE: two hub processes
racing acquire() serialize on the write lock, and the WHERE guard re-checks
availability inside the transaction so a token is never double-handed-out.
"""

from __future__ import annotations

import hashlib
import secrets as pysecrets
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select, update

from .models import Token, utcnow


class NoTokenAvailable(Exception):
    """Raised by acquire() when no token can be handed out right now."""


class TokenPool:
    def __init__(self, session_factory, secrets_dir: Path):
        self._session_factory = session_factory
        self._secrets_dir = secrets_dir

    @contextmanager
    def _session(self):
        s = self._session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ── token lifecycle ──────────────────────────────────────────────── #

    def add_token(self, label: str, secret: str, *, max_concurrency: int = 1) -> Token:
        """Persist the secret as a 0600 file, register the token as active."""
        if not label or not secret:
            raise ValueError("label and secret are required")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        fname = f"{safe}-{pysecrets.token_hex(4)}.secret"
        path = self._secrets_dir / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret)
        path.chmod(0o600)
        with self._session() as s:
            existing = s.execute(select(Token).where(Token.label == label)).scalar_one_or_none()
            if existing is not None:
                raise ValueError(f"label already exists: {label!r}")
            tok = Token(
                label=label,
                secret_path=str(path),
                fingerprint=self._fingerprint(secret),
                max_concurrency=max_concurrency,
            )
            s.add(tok)
            s.flush()
            s.refresh(tok)
            return tok

    def import_secret_file(self, label: str, secret_path: str, *, max_concurrency: int = 1) -> Token:
        """Register an existing on-disk secret (no copy) — for ops-managed files."""
        path = Path(secret_path)
        if not path.is_file():
            raise FileNotFoundError(secret_path)
        secret = path.read_text()
        with self._session() as s:
            existing = s.execute(select(Token).where(Token.label == label)).scalar_one_or_none()
            if existing is not None:
                raise ValueError(f"label already exists: {label!r}")
            tok = Token(
                label=label,
                secret_path=str(path.resolve()),
                fingerprint=self._fingerprint(secret),
                max_concurrency=max_concurrency,
            )
            s.add(tok)
            s.flush()
            s.refresh(tok)
            return tok

    def read_secret(self, token_id: int) -> str:
        with self._session() as s:
            tok = s.get(Token, token_id)
            if tok is None:
                raise KeyError(f"no token id={token_id}")
            return Path(tok.secret_path).read_text()

    # ── allocation ───────────────────────────────────────────────────── #

    def acquire(self) -> Token:
        """Hand out one available token. Raises NoTokenAvailable otherwise."""
        with self._session() as s:
            now = utcnow()
            stmt = (
                select(Token)
                .where(
                    Token.status == "active",
                    Token.in_use < Token.max_concurrency,
                )
                .order_by(Token.last_used_at.asc().nulls_first(), Token.id)
                .limit(1)
            )
            tok = s.execute(stmt).scalar_one_or_none()
            if tok is None:
                raise NoTokenAvailable()
            if tok.cooldown_until is not None and _as_utc(tok.cooldown_until) >= now:
                raise NoTokenAvailable()
            claimed = s.execute(
                update(Token)
                .where(Token.id == tok.id, Token.in_use == tok.in_use)
                .values(in_use=Token.in_use + 1, last_used_at=now)
            )
            if claimed.rowcount != 1:
                raise NoTokenAvailable()  # lost the race, caller retries
            s.refresh(tok)
            return tok

    def release(self, token_id: int) -> None:
        with self._session() as s:
            s.execute(
                update(Token)
                .where(Token.id == token_id)
                .values(in_use=Token.in_use - 1)
            )

    # ── health transitions ───────────────────────────────────────────── #

    def report_banned(self, token_id: int, reason: str = "") -> None:
        """Account dead (auth/401/403) — retire the token permanently."""
        with self._session() as s:
            s.execute(
                update(Token)
                .where(Token.id == token_id)
                .values(
                    status="banned",
                    banned_at=utcnow(),
                    banned_reason=reason[:256] or None,
                    in_use=0,
                    cooldown_until=None,
                )
            )

    def report_discard(self, token_id: int, reason: str = "") -> None:
        """3 retries exhausted — discard the account, never re-hand-out."""
        with self._session() as s:
            s.execute(
                update(Token)
                .where(Token.id == token_id)
                .values(
                    status="discarded",
                    banned_at=utcnow(),
                    banned_reason=("discarded: " + reason)[:256] or None,
                    in_use=0,
                    cooldown_until=None,
                )
            )

    def report_failure(self, token_id: int, *, cooldown_s: int = 60) -> None:
        """Transient failure — retryable, same token stays in rotation.

        The scheduler gives each item 3 attempts on the SAME token before
        calling report_discard; cooldown just spreads load off a hot token.
        """
        with self._session() as s:
            s.execute(
                update(Token)
                .where(Token.id == token_id)
                .values(
                    fail_count=Token.fail_count + 1,
                    cooldown_until=utcnow() + dt_secs(cooldown_s),
                )
            )

    def set_status(self, token_id: int, status: str) -> None:
        if status not in ("active", "disabled"):
            raise ValueError(
                "use report_banned/report_discard for retiring; "
                "status must be active|disabled"
            )
        with self._session() as s:
            s.execute(
                update(Token)
                .where(Token.id == token_id)
                .values(status=status, cooldown_until=None, banned_at=None)
            )

    def bulk_add_tokens(
        self, blob: str, *, label_prefix: str = "bulk", max_concurrency: int = 1
    ) -> list[Token]:
        """Split a newline-separated token blob into individual DB entries.

        Skips blank lines; dedupes by fingerprint within the blob. Returns the
        created tokens. A line that fails to add (e.g. duplicate label) is
        skipped, not fatal.
        """
        created: list[Token] = []
        seen_fp: set[str] = set()
        for i, raw in enumerate(blob.splitlines()):
            secret = raw.strip()
            if not secret:
                continue
            fp = self._fingerprint(secret)
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            label = f"{label_prefix}-{i:05d}"
            try:
                created.append(
                    self.add_token(label, secret, max_concurrency=max_concurrency)
                )
            except ValueError:
                continue
        return created

    def list_tokens(self) -> list[Token]:
        with self._session() as s:
            return list(s.execute(select(Token).order_by(Token.id)).scalars())

    def stats(self) -> dict:
        with self._session() as s:
            toks = list(s.execute(select(Token)).scalars())
        out = {
            "total": len(toks),
            "active": 0,
            "banned": 0,
            "discarded": 0,
            "disabled": 0,
            "in_use": 0,
        }
        for t in toks:
            if t.status == "active":
                out["active"] += 1
            elif t.status == "banned":
                out["banned"] += 1
            elif t.status == "discarded":
                out["discarded"] += 1
            elif t.status == "disabled":
                out["disabled"] += 1
            out["in_use"] += t.in_use
        return out

    @staticmethod
    def _fingerprint(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()[:12]


def dt_secs(seconds: int):
    import datetime as dt

    return dt.timedelta(seconds=seconds)


def _as_utc(dt_obj):
    """SQLite returns naive datetimes; normalize for comparison with utcnow()."""
    import datetime as dt

    if dt_obj is None or dt_obj.tzinfo is not None:
        return dt_obj
    return dt_obj.replace(tzinfo=dt.timezone.utc)
