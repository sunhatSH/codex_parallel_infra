"""Refresh-token → auth.json conversion (stdlib-only, mirrors user's script).

Codex CLI's chatgpt auth mode needs auth.json with id_token/access_token/
refresh_token/account_id. Accounts arrive as either:
  - a bare refresh token (RT)  → exchange it at auth.openai.com (network)
  - a full token bundle JSON   → contains RT + AT + account_id already

The exchange follows the exact protocol of the user's reference script:
POST /oauth/token with grant_type=refresh_token, codex_cli_rs UA, and the
same client_id. On success we persist the auth.json. When the network path
is unavailable (mainland egress), a bundle with an unexpired access_token
can be used directly — codex refreshes AT from RT itself once it can reach
the auth endpoint.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib import error, parse, request

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"


class TokenExchangeError(Exception):
    """RT exchange failed (network / rejected / malformed response)."""


def decode_jwt(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _auth_json_from_response(result: dict, fallback_rt: str) -> dict:
    for field in ("id_token", "access_token"):
        v = result.get(field)
        if not isinstance(v, str) or not v.strip():
            raise TokenExchangeError(f"response missing {field}")
    claims = decode_jwt(result["id_token"])
    account_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    if not account_id:
        claims = decode_jwt(result["access_token"])
        account_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    if not account_id:
        raise TokenExchangeError("chatgpt_account_id missing from token")
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": result["id_token"],
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token") or fallback_rt,
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


def exchange_refresh_token(refresh_token: str, *, timeout_s: int = 120) -> dict:
    """Bare RT → auth.json (network call to auth.openai.com)."""
    data = parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": "openid profile email",
        }
    ).encode()
    req = request.Request(
        TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs/0.200.1",
            "originator": "codex_cli_rs",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout_s) as response:
            result = json.load(response)
    except error.HTTPError as e:
        raise TokenExchangeError(f"HTTP {e.code} from auth endpoint") from e
    except (error.URLError, TimeoutError, OSError) as e:
        raise TokenExchangeError(f"network error: {e}") from e
    return _auth_json_from_response(result, refresh_token)


@dataclass
class TokenBundle:
    """A parsed account record ready for auth.json construction."""

    label: str
    refresh_token: str
    account_id: str | None = None
    access_token: str | None = None
    id_token: str | None = None
    plan_type: str | None = None
    email: str | None = None

    @classmethod
    def from_bundle_json(cls, label: str, bundle: dict) -> "TokenBundle":
        return cls(
            label=label,
            refresh_token=bundle["refresh_token"],
            account_id=bundle.get("account_id"),
            access_token=bundle.get("access_token"),
            id_token=bundle.get("id_token"),
            plan_type=bundle.get("plan_type"),
            email=bundle.get("user_email"),
        )

    def access_token_expired(self) -> bool:
        if not self.access_token:
            return True
        try:
            claims = decode_jwt(self.access_token)
            exp = claims.get("exp", 0)
            return datetime.now(timezone.utc).timestamp() >= exp
        except Exception:  # noqa: BLE001
            return True

    def build_auth_json(self) -> dict:
        """Build auth.json: use bundled tokens when the AT is fresh (even
        without an id_token — codex only requires it for display; AT+RT is
        enough to run); exchange the RT when AT is missing/expired."""
        if self.access_token and not self.access_token_expired():
            return self._from_bundled()
        # access token missing/expired → exchange (network)
        return exchange_refresh_token(self.refresh_token)

    def _from_bundled(self) -> dict:
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": self.id_token or "",
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "account_id": self.account_id,
            },
            "last_refresh": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
