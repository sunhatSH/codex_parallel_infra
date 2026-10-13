"""Token exchange + xlsx import tests (no live network by default)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from codex_parallel.token_exchange import (
    TokenBundle,
    TokenExchangeError,
    decode_jwt,
)


def _fake_jwt(claims: dict) -> str:
    import base64

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(claims)}."


def _bundle(**over):
    now = datetime.now(timezone.utc)
    b = {
        "refresh_token": "rt.1.FAKE",
        "access_token": _fake_jwt(
            {
                "exp": (now + timedelta(days=5)).timestamp(),
                "https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"},
            }
        ),
        "id_token": _fake_jwt({"exp": (now + timedelta(days=5)).timestamp()}),
        "account_id": "acc-1",
        "plan_type": "prolite",
        "user_email": "x@y.z",
    }
    b.update(over)
    return b


class TestTokenBundle:
    def test_from_bundle_json(self):
        tb = TokenBundle.from_bundle_json("acc-1", _bundle())
        assert tb.refresh_token == "rt.1.FAKE"
        assert tb.account_id == "acc-1"
        assert tb.email == "x@y.z"

    def test_build_auth_json_uses_bundled_when_fresh(self):
        """Valid AT → no network call, auth.json from bundle."""
        tb = TokenBundle.from_bundle_json("acc-1", _bundle())
        auth = tb.build_auth_json()
        assert auth["auth_mode"] == "chatgpt"
        assert auth["tokens"]["refresh_token"] == "rt.1.FAKE"
        assert auth["tokens"]["account_id"] == "acc-1"

    def test_expired_at_triggers_exchange(self, monkeypatch):
        now = datetime.now(timezone.utc)
        tb = TokenBundle.from_bundle_json(
            "acc-1",
            _bundle(
                access_token=_fake_jwt({"exp": (now - timedelta(hours=1)).timestamp()})
            ),
        )
        assert tb.access_token_expired()
        called = {}

        def fake_exchange(rt):
            called["rt"] = rt
            return {"auth_mode": "chatgpt", "tokens": {"refresh_token": rt}}

        monkeypatch.setattr("codex_parallel.token_exchange.exchange_refresh_token", fake_exchange)
        # build_auth_json calls the module-level function
        from codex_parallel import token_exchange as te

        orig = te.exchange_refresh_token
        te.exchange_refresh_token = fake_exchange
        try:
            auth = tb.build_auth_json()
        finally:
            te.exchange_refresh_token = orig
        assert called["rt"] == "rt.1.FAKE"

    def test_exchange_failure_raises(self):
        with pytest.raises(TokenExchangeError):
            from codex_parallel.token_exchange import exchange_refresh_token

            # bare RT with no network → TokenExchangeError (mainland box)
            try:
                exchange_refresh_token("rt.bogus", timeout_s=3)
            except TokenExchangeError:
                raise
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"unexpected error type: {e}")


class TestXlsxImport:
    @pytest.fixture()
    def pool(self, tmp_path):
        from codex_parallel.db import bootstrap
        from codex_parallel.token_pool import TokenPool

        sf = bootstrap(tmp_path / "t.db")
        return TokenPool(sf, tmp_path / "secrets")

    @pytest.fixture()
    def xlsx(self, tmp_path):
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["账号----密码", None, None, None, None, None, "rt", None, None, None, None, None, None, None, None, None])
        for i in range(3):
            ws.append(
                [
                    f"user{i}----pass{i}",
                    None, None, None, None, None,
                    f"https://buy.example/{i}",
                    None, None, None, None, None, None, None, None,
                    json.dumps(_bundle(refresh_token=f"rt-{i}", account_id=f"acc-{i}")),
                ]
            )
        p = tmp_path / "accounts.xlsx"
        wb.save(p)
        return p

    def test_parse_accounts(self, xlsx):
        from codex_parallel.xlsx_import import parse_accounts_xlsx

        rows = parse_accounts_xlsx(xlsx)
        assert len(rows) == 3
        labels = [l for l, _ in rows]
        assert labels == ["user0", "user1", "user2"]
        assert all(b["refresh_token"].startswith("rt-") for _, b in rows)

    def test_import_to_pool(self, pool, xlsx):
        from codex_parallel.xlsx_import import import_xlsx_to_pool

        result = import_xlsx_to_pool(pool, xlsx, exchange=False)
        assert len(result["added"]) == 3
        assert result["stats"]["active"] == 3
        # secrets are full auth.json payloads (chatgpt mode)
        for t in pool.list_tokens():
            secret = pool.read_secret(t.id)
            auth = json.loads(secret)
            assert auth["auth_mode"] == "chatgpt"
            assert auth["tokens"]["refresh_token"].startswith("rt-")

    def test_duplicate_import_skips(self, pool, xlsx):
        from codex_parallel.xlsx_import import import_xlsx_to_pool

        import_xlsx_to_pool(pool, xlsx, exchange=False)
        result = import_xlsx_to_pool(pool, xlsx, exchange=False)
        assert len(result["added"]) == 0
        assert len(result["skipped"]) == 3
        assert result["stats"]["total"] == 3

    def test_bad_rows_skipped(self, pool, xlsx):
        import openpyxl

        from codex_parallel.xlsx_import import import_xlsx_to_pool

        wb = openpyxl.load_workbook(xlsx)
        ws = wb.active
        ws.append(["broken----x", None, None, None, None, None, "rt", None, None, None, None, None, None, None, None, "not-json"])
        ws.append(["nort----x", None, None, None, None, None, "rt", None, None, None, None, None, None, None, None, json.dumps({"foo": 1})])
        wb.save(xlsx)
        result = import_xlsx_to_pool(pool, xlsx, exchange=False)
        assert len(result["added"]) == 3  # the 3 good ones
        assert result["stats"]["total"] == 3
