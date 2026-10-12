from __future__ import annotations

import threading

import pytest

from codex_parallel.models import Token
from codex_parallel.token_pool import NoTokenAvailable, TokenPool


@pytest.fixture()
def pool(tmp_path):
    from codex_parallel.db import bootstrap

    sf = bootstrap(tmp_path / "test.db")
    return TokenPool(sf, tmp_path / "secrets")


class TestAddToken:
    def test_add_and_read_secret(self, pool, tmp_path):
        tok = pool.add_token("acc-1", "sk-secret-value-123")
        assert tok.status == "active"
        assert tok.fingerprint
        secret = pool.read_secret(tok.id)
        assert secret == "sk-secret-value-123"
        # secret file is 0600
        import stat

        mode = stat.S_IMODE((tmp_path / "secrets").glob("*.secret").__next__().stat().st_mode)
        assert mode == 0o600

    def test_duplicate_label_rejected(self, pool):
        pool.add_token("dup", "s1")
        with pytest.raises(ValueError, match="already exists"):
            pool.add_token("dup", "s2")

    def test_bulk_add_splits_lines(self, pool):
        blob = "tok-a\n\ntok-b\ntok-a\ntok-c\n"
        created = pool.bulk_add_tokens(blob, label_prefix="imp")
        assert len(created) == 3  # blank line skipped, in-blob dup dropped
        secrets = {pool.read_secret(t.id) for t in created}
        assert secrets == {"tok-a", "tok-b", "tok-c"}
        stats = pool.stats()
        assert stats == {"total": 3, "active": 3, "banned": 0, "discarded": 0,
                         "disabled": 0, "in_use": 0}


class TestAcquireRelease:
    def test_acquire_increments_in_use(self, pool):
        tok = pool.add_token("acc", "s")
        got = pool.acquire()
        assert got.id == tok.id
        assert got.in_use == 1
        pool.release(got.id)
        got2 = pool.acquire()
        assert got2.id == tok.id
        assert got2.in_use == 1

    def test_max_concurrency_respected(self, pool):
        pool.add_token("acc", "s", max_concurrency=2)
        pool.acquire()
        pool.acquire()
        with pytest.raises(NoTokenAvailable):
            pool.acquire()

    def test_no_tokens(self, pool):
        with pytest.raises(NoTokenAvailable):
            pool.acquire()

    def test_banned_not_handed_out(self, pool):
        tok = pool.add_token("acc", "s")
        pool.report_banned(tok.id, reason="401")
        with pytest.raises(NoTokenAvailable):
            pool.acquire()
        t = pool.list_tokens()[0]
        assert t.status == "banned"
        assert t.banned_reason == "401"

    def test_discarded_not_handed_out(self, pool):
        tok = pool.add_token("acc", "s")
        pool.report_discard(tok.id, reason="transient x3")
        with pytest.raises(NoTokenAvailable):
            pool.acquire()

    def test_cooldown_blocks_until_elapsed(self, pool):
        tok = pool.add_token("acc", "s")
        pool.report_failure(tok.id, cooldown_s=3600)
        with pytest.raises(NoTokenAvailable):
            pool.acquire()

    def test_round_robin_least_recently_used(self, pool):
        pool.add_token("a", "s1")
        pool.add_token("b", "s2")
        pool.add_token("c", "s3")
        first = pool.acquire()
        second = pool.acquire()
        third = pool.acquire()
        ids = [first.id, second.id, third.id]
        assert len(set(ids)) == 3


class TestConcurrency:
    def test_threaded_acquire_never_exceeds_max(self, pool):
        pool.add_token("acc", "s", max_concurrency=3)
        acquired = []
        lock = threading.Lock()

        def worker():
            try:
                tok = pool.acquire()
                with lock:
                    acquired.append(tok.in_use)
                pool.release(tok.id)
            except NoTokenAvailable:
                pass

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(n <= 3 for n in acquired)

    def test_set_status_disable_enable(self, pool):
        tok = pool.add_token("acc", "s")
        pool.set_status(tok.id, "disabled")
        with pytest.raises(NoTokenAvailable):
            pool.acquire()
        pool.set_status(tok.id, "active")
        got = pool.acquire()
        assert got.id == tok.id
