"""Assemble and observe one `codex exec` run inside a sandbox.

Responsibilities (and nothing else):
  - build the argv for `codex exec` from prompt + per-task codex_config
  - prepare an isolated CODEX_HOME on the sandbox and write auth.json there
  - run it via the backend's Sandbox.run
  - classify the outcome: ok / auth_failed / timeout / failed

The runner is backend-agnostic: it only needs a Sandbox with run/upload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CodexRunResult:
    ok: bool
    exit_code: int
    auth_failed: bool
    timed_out: bool
    transient: bool = False
    stdout: str = ""
    stderr: str = ""
    last_message: str = ""
    events: list[dict] = field(default_factory=list)
    error: str = ""
    watchdog: str = ""  # raw /tmp/.watchdog/status JSON line from the sandbox
    refreshed_auth: str = ""  # auth.json content read back after the run (RT rotation)

    @property
    def failure_kind(self) -> str:
        """Failure attribution for the health module.

        account_dead  → credential rejected; scheduler must swap to a NEW token
        transient     → retry the SAME token (3 chances total)
        timeout/failed → infra-level failure, also retried on same token
        """
        if self.ok:
            return "done"
        if self.timed_out:
            return "timeout"
        if self.auth_failed:
            return "account_dead"
        if self.transient:
            return "transient"
        return "failed"


# Markers meaning the ACCOUNT is dead → scheduler swaps in a new token.
ACCOUNT_DEAD_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid_api_key",
    "account suspended",
    "account disabled",
    "token expired",
    "session expired",
    "not logged in",
    "login required",
)

# Markers meaning a transient problem → same token gets retried (3 chances).
TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "502",
    "503",
    "504",
    "overloaded",
    "network",
    "connection reset",
    "connection refused",
    "econnreset",
    "temporarily unavailable",
)


@dataclass
class CodexRunner:
    codex_bin: str = "codex"
    model: str | None = None
    sandbox_mode: str = "workspace-write"
    reasoning_effort: str | None = None
    extra_args: list[str] = field(default_factory=list)

    def build_argv(self, prompt: str) -> list[str]:
        argv = [
            self.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            self.sandbox_mode,
            "--json",
            "-o",
            "result-last.md",
        ]
        if self.model:
            argv += ["-m", self.model]
        if self.reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={self.reasoning_effort!r}"]
        argv += self.extra_args
        argv.append("--")
        argv.append(prompt)
        return argv

    async def prepare_auth(self, sandbox, token_secret: str) -> dict[str, str]:
        """Write auth.json into the sandbox's CODEX_HOME; return the env overlay.

        Accepts three secret shapes:
          - full auth.json JSON (auth_mode/tokens) → used verbatim (chatgpt)
          - JSON with OPENAI_API_KEY → used verbatim (apikey)
          - bare API key            → wrapped as apikey auth.json
          - bare refresh token ("rt.") → wrapped as a minimal chatgpt-mode
            auth.json; codex will exchange/refresh it on first contact
        """
        secret = token_secret.strip()
        if secret.startswith("{"):
            try:
                parsed = json.loads(secret)
                if "OPENAI_API_KEY" in parsed or "auth_mode" in parsed:
                    payload = secret
                else:
                    payload = json.dumps({"OPENAI_API_KEY": secret})
            except json.JSONDecodeError:
                payload = json.dumps({"OPENAI_API_KEY": secret})
        elif secret.startswith("rt.") or secret.count(".") >= 2:
            # bare refresh token → chatgpt-mode skeleton; codex fills the rest
            payload = json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": None,
                    "tokens": {"refresh_token": secret},
                }
            )
        else:
            payload = json.dumps({"OPENAI_API_KEY": secret})

        await sandbox.upload_text(".codex-home/auth.json", payload) if hasattr(
            sandbox, "upload_text"
        ) else await self._upload_text_via_file(sandbox, payload)
        return {}

    async def _upload_text_via_file(self, sandbox, payload: str) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(payload)
            tmp = f.name
        try:
            await sandbox.upload(tmp, ".codex-home/auth.json")
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def run(
        self,
        sandbox,
        prompt: str,
        *,
        timeout_s: int,
        env: dict[str, str] | None = None,
    ) -> CodexRunResult:
        full_env = dict(env or {})
        full_env.setdefault("CODEX_HOME", ".codex-home")

        # In-sandbox watchdog (plain shell supervisor, not an agent): watches
        # the codex PID and persists status to /tmp/.watchdog/status so the
        # hub can learn the outcome even if the streaming connection dies
        # (AGS gateway GOAWAY / network partition). Started per-run, best
        # effort — a sandbox without the script simply runs without it.
        wd = await self._start_watchdog(sandbox)

        res = await sandbox.run(
            self.build_argv(prompt), timeout_s=timeout_s, env=full_env
        )

        # If the stream died mid-run, the watchdog file is the fallback
        # source of truth for what happened to codex.
        wd_status = await self._read_watchdog(sandbox, wd)

        # RT rotation capture: codex refreshes tokens in its in-sandbox
        # CODEX_HOME during the run. Read the refreshed auth.json back so
        # the pool never loses the newest refresh token (losing it means a
        # manual browser login to recover the account).
        refreshed_auth = await self._read_refreshed_auth(sandbox)

        events = self._parse_events(res.stdout)
        last_message = self._extract_last_message(events)
        combined = res.stderr + res.stdout
        auth_failed = (
            not res.ok
            and not res.timed_out
            and self._account_dead(combined)
        )
        transient = (
            not res.ok and not res.timed_out and not auth_failed and self._transient(combined)
        )
        return CodexRunResult(
            ok=res.ok,
            exit_code=res.exit_code,
            auth_failed=auth_failed,
            timed_out=res.timed_out,
            transient=transient,
            stdout=res.stdout[-20000:],
            stderr=res.stderr[-8000:],
            last_message=last_message,
            events=events,
            error=(res.stderr or res.stdout)[-4000:] if not res.ok else "",
            watchdog=wd_status,
            refreshed_auth=refreshed_auth,
        )

    async def _start_watchdog(self, sandbox) -> bool:
        """Install + spawn the sandbox-side supervisor. Returns True if up."""
        try:
            script = Path(__file__).resolve().parents[2] / "docker" / "sandbox" / "watchdog.sh"
            if not script.is_file():
                return False
            # figure out the codex PID: we launch codex via `sh -c "... & echo $!"`
            # — simpler: the wrapper prints the pid, watchdog reads it. Here we
            # start the watchdog pointed at the NEXT codex process by having it
            # watch "the newest codex process". Shell-friendliest: pgrep -n.
            await sandbox.run(
                [
                    "sh", "-c",
                    "command -v pgrep >/dev/null || exit 0; "
                    "nohup sh /opt/codex-parallel/watchdog.sh "
                    "$(pgrep -n -f 'codex exec' || echo 0) >/dev/null 2>&1 & "
                    "exit 0",
                ],
                timeout_s=15,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _read_watchdog(self, sandbox, started: bool) -> str:
        """Pull the watchdog status file; empty when unavailable."""
        if not started:
            return ""
        try:
            import tempfile

            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                tmp = f.name
            await sandbox.download("/tmp/.watchdog/status", tmp)
            content = Path(tmp).read_text().strip()
            Path(tmp).unlink(missing_ok=True)
            return content[-2000:]
        except Exception:  # noqa: BLE001
            return ""

    async def _read_refreshed_auth(self, sandbox) -> str:
        """Read the sandbox's post-run auth.json (codex may have refreshed
        the tokens inside its CODEX_HOME). Empty on any failure — the pool
        keeps the previous secret then."""
        try:
            import tempfile

            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                tmp = f.name
            await sandbox.download(".codex-home/auth.json", tmp)
            content = Path(tmp).read_text().strip()
            Path(tmp).unlink(missing_ok=True)
            # sanity: must be a JSON object with tokens to be worth persisting
            if content.startswith("{") and '"tokens"' in content:
                return content
            return ""
        except Exception:  # noqa: BLE001
            return ""

    async def fetch_last_message(self, sandbox, remote_path: str = "result-last.md") -> str:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            tmp = f.name
        try:
            await sandbox.download(remote_path, tmp)
            return Path(tmp).read_text()
        except (FileNotFoundError, Exception):
            return ""
        finally:
            Path(tmp).unlink(missing_ok=True)

    # ── parsing helpers ──────────────────────────────────────────────── #

    @staticmethod
    def _parse_events(stdout: str) -> list[dict]:
        events = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    @staticmethod
    def _extract_last_message(events: list[dict]) -> str:
        for ev in reversed(events):
            msg = ev.get("msg") or ev.get("message") or ""
            etype = ev.get("type") or ""
            if "agent_message" in str(etype) and msg:
                return str(msg)
        return ""

    @staticmethod
    def _account_dead(combined: str) -> bool:
        lowered = combined.lower()
        return any(marker in lowered for marker in ACCOUNT_DEAD_MARKERS)

    @staticmethod
    def _transient(combined: str) -> bool:
        lowered = combined.lower()
        return any(marker in lowered for marker in TRANSIENT_MARKERS)
