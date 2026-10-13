"""RT exchange cache tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_parallel.token_exchange import (
    TokenBundle,
    _ExchangeCache,
    exchange_refresh_token,
    get_exchange_cache,
)


def _auth(rt="rt-new", exp_offset=3600):
    import base64
    import time

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    at = f"{b64({'alg': 'none'})}.{b64({'exp': time.time() + exp_offset, 'https://api.openai.com/auth': {'chatgpt_account_id': 'acc-1'}})}."
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "id",
            "access_token": at,
            "refresh_token": rt,
            "account_id": "acc-1",
        },
    }


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    import codex_parallel.token_exchange as te

    c = _ExchangeCache(tmp_path / "rt_cache")
    monkeypatch.setattr(te, "_CACHE", c)
    return c


class TestCache:
    def test_put_get_roundtrip(self, cache):
        auth = _auth()
        cache.put("rt-orig", auth)
        got = cache.get("rt-orig")
        assert got == auth

    def test_miss(self, cache):
        assert cache.get("nope") is None

    def test_usable_requires_fresh_at(self, cache):
        cache.put("rt-fresh", _auth(exp_offset=3600))
        cache.put("rt-stale", _auth(exp_offset=-10))
        cache.put("rt-noat", {"tokens": {"refresh_token": "x"}})
        assert cache.lookup_usable("rt-fresh") is not None
        assert cache.lookup_usable("rt-stale") is None
        assert cache.lookup_usable("rt-noat") is None

    def test_corrupt_entry_ignored(self, cache, tmp_path):
        p = cache._path("rt-bad")
        p.write_text("not json")
        assert cache.get("rt-bad") is None

    def test_exchange_uses_cache_no_network(self, cache, monkeypatch):
        """2nd exchange of the same RT must NOT hit the network."""
        auth = _auth()
        cache.put("rt-orig", auth)

        def boom(*a, **kw):
            raise AssertionError("network was hit — cache miss!")

        from codex_parallel import token_exchange as te

        monkeypatch.setattr(te, "_auth_json_from_response", boom)
        # urlopen would raise too; if the cache works we never get there
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        out = exchange_refresh_token("rt-orig")
        assert out == auth

    def test_stale_cache_entry_refetched_via_rotated_rt(self, cache, monkeypatch):
        """Cached AT expired → re-exchange using the STORED (rotated) RT."""
        stale = _auth(rt="rt-rotated", exp_offset=-100)
        cache.put("rt-orig", stale)

        fresh = _auth(rt="rt-rotated-2", exp_offset=3600)

        def fake_urlopen(req, timeout=None):
            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                @staticmethod
                def read():
                    return json.dumps(
                        {
                            "id_token": fresh["tokens"]["id_token"],
                            "access_token": fresh["tokens"]["access_token"],
                            "refresh_token": "rt-rotated-2",
                        }
                    ).encode()


            return R()

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        out = exchange_refresh_token("rt-orig")
        assert out["tokens"]["refresh_token"] == "rt-rotated-2"
        # and the cache now holds the newest under the ORIGINAL rt key
        assert cache.get("rt-orig")["tokens"]["refresh_token"] == "rt-rotated-2"
