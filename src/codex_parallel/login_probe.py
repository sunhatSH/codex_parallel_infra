"""Per-account login verification inside a sandbox.

A token IS an account (1:1, max_concurrency=1 enforced by the pool). Before
burning a task attempt on real work, verify the credential can actually
authenticate codex in this sandbox:

  1. write the secret into the sandbox's CODEX_HOME/auth.json
  2. run `codex login status` (or a lightweight API probe) with a short timeout
  3. classify the outcome:
       ok            → account healthy, proceed with the task
       account_dead  → 401/403/invalid → token must be marked unusable NOW
       transient     → network/5xx → ordinary offline, retryable
"""

from __future__ import annotations

from dataclasses import dataclass

from .codex_runner import CodexRunner


@dataclass
class LoginCheckResult:
    ok: bool
    account_dead: bool   # True → mark token unusable, no retries
    transient: bool      # True → ordinary offline, retry (≤3) with sandbox destroyed
    detail: str = ""


class LoginProber:
    """Runs the credential check inside a live sandbox.

    probe() shells out to `codex login status` with the sandbox's CODEX_HOME
    pointing at the freshly-written auth.json. codex exit code + stderr decide
    the classification. A missing/zero-length output is treated as transient
    (the sandbox may still be warming up), NOT account death — the account
    path requires an explicit auth rejection.
    """

    def __init__(self, runner: CodexRunner | None = None, timeout_s: int = 60):
        self._runner = runner or CodexRunner()
        self.timeout_s = timeout_s

    async def probe(self, sandbox, secret: str) -> LoginCheckResult:
        from .codex_runner import CodexRunner

        env = await self._runner.prepare_auth(sandbox, secret)
        full_env = dict(env)
        full_env.setdefault("CODEX_HOME", ".codex-home")

        res = await sandbox.run(
            [self._runner.codex_bin, "login", "status"],
            timeout_s=self.timeout_s,
            env=full_env,
        )
        combined = (res.stderr or "") + (res.stdout or "")
        lowered = combined.lower()

        if res.ok:
            # `codex login status` exits 0 when logged in
            if "not logged in" in lowered or "login required" in lowered:
                return LoginCheckResult(False, True, False, combined[:300])
            return LoginCheckResult(True, False, False, combined[:300])

        # explicit rejection → the account itself is dead
        if CodexRunner._account_dead(combined):
            return LoginCheckResult(False, True, False, combined[:300])
        # anything else (network, 5xx, weird exit) → ordinary offline
        return LoginCheckResult(False, False, True, combined[:300])
