"""Tests for the per-key quota ledger (v2.2.0).

Batch B of the testing audit (12 Okt 2026): rate_limit.py had 33% coverage.
All tests offline — real SQLite via tmp_path, fake clock + fake sleep,
no quota burned, no network.
"""
import sqlite3
import time

import pytest

from novel_translator.errors import CacheError, Paused
from novel_translator.models import TranslationOptions
from novel_translator.rate_limit import (
    RateLimiter,
    generation_requests_today,
    local_rpd_used,
    next_pacific_midnight,
    pacific_midnight,
    quota_state,
    set_local_rpd_used,
)
from novel_translator.storage import TranslationCache


# ---------------------------------------------------------------- helpers
@pytest.fixture
def cache(tmp_path):
    return TranslationCache(tmp_path / "cache.db")


@pytest.fixture
def clock():
    now = [1_728_700_000.0]
    return now


@pytest.fixture
def limiter(cache, clock):
    sleeps = []
    options = TranslationOptions(api_keys=["K1", "K2"], model="m", rpm=13,
                                 tpm=248000, rpd=490, max_requests=490,
                                 max_retry_wait=120.0)
    rl = RateLimiter(cache, options,
                     clock=lambda: clock[0],
                     sleep=lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    rl.sleeps = sleeps
    return rl


def _memdb():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE performance_state (scope TEXT, key TEXT, payload TEXT, "
               "PRIMARY KEY(scope, key))")
    db.execute("CREATE TABLE requests (scope TEXT, sent REAL, tokens INTEGER)")
    return db


# ------------------------------------------------------- pure functions
def test_pacific_midnight_properties():
    t = 1_728_700_000.0
    mid = pacific_midnight(t)
    assert mid <= t < mid + 86400
    assert next_pacific_midnight(t) - mid == 86400


def test_quota_state_missing_table():
    db = sqlite3.connect(":memory:")
    assert quota_state(db, "s", "k") == {}


def test_quota_state_roundtrip():
    db = _memdb()
    assert quota_state(db, "s", "k") == {}
    db.execute("INSERT INTO performance_state VALUES (?,?,?)", ("s", "k", '{"a": 1}'))
    assert quota_state(db, "s", "k") == {"a": 1}


def test_quota_state_bad_json_raises():
    db = _memdb()
    db.execute("INSERT INTO performance_state VALUES (?,?,?)", ("s", "k", "bukan json"))
    with pytest.raises(CacheError):
        quota_state(db, "s", "k")


def test_set_local_rpd_used_validation():
    db = _memdb()
    with pytest.raises(ValueError):
        set_local_rpd_used(db, "s", 1_728_700_000.0, -1)
    with pytest.raises(ValueError):
        set_local_rpd_used(db, "s", 1_728_700_000.0, True)


def test_local_rpd_used_counts_requests_plus_offset():
    db = _memdb()
    now = 1_728_700_000.0
    db.execute("INSERT INTO requests VALUES (?,?,?)", ("s", now - 10, 100))
    db.execute("INSERT INTO requests VALUES (?,?,?)", ("s", now - 90000, 100))  # kemarin
    assert local_rpd_used(db, "s", now) == 1
    set_local_rpd_used(db, "s", now, 7)
    assert local_rpd_used(db, "s", now) == 7


def test_local_rpd_used_bad_offset_raises():
    db = _memdb()
    now = 1_728_700_000.0
    db.execute("INSERT INTO performance_state VALUES (?,?,?)",
               ("s", "local-rpd-v1", '{"day": %d, "offset": "x"}' % int(pacific_midnight(now))))
    with pytest.raises(CacheError):
        local_rpd_used(db, "s", now)


def test_generation_requests_today_scope_isolated():
    db = _memdb()
    now = 1_728_700_000.0
    db.execute("INSERT INTO requests VALUES (?,?,?)", ("a", now - 5, 1))
    db.execute("INSERT INTO requests VALUES (?,?,?)", ("b", now - 5, 1))
    assert generation_requests_today(db, "a", now) == 1


# ------------------------------------------------------- RateLimiter
def test_key_scope_hides_raw_key(limiter):
    scope = limiter._key_scope("K1-RAHASIA")
    assert "RAHASIA" not in scope
    assert scope.startswith("m:")
    assert limiter._key_scope("K1-RAHASIA") == scope  # deterministik


def test_current_key_no_keys_raises(cache, clock):
    options = TranslationOptions(model="m")
    rl = RateLimiter(cache, options, clock=lambda: clock[0], sleep=lambda s: None)
    with pytest.raises(ValueError):
        rl.current_key()


def test_reserve_happy_path(limiter):
    rid = limiter.reserve(1000)
    assert isinstance(rid, int)
    assert limiter.requests_made == 1
    assert limiter.pinned_key(rid) in ("K1", "K2")
    assert limiter.key_label(limiter.pinned_key(rid)).startswith("#")


def test_reserve_rejects_bad_tokens(limiter):
    with pytest.raises(ValueError):
        limiter.reserve(-5)
    with pytest.raises(ValueError):
        limiter.reserve("x")


def test_reserve_over_tpm_pauses(limiter):
    with pytest.raises(Paused):
        limiter.reserve(999_999_999)


def test_reserve_max_requests_pauses(limiter):
    limiter.options.max_requests = 1
    limiter.reserve(100)
    with pytest.raises(Paused):
        limiter.reserve(100)


def test_rpm_window_enforced_then_frees(limiter, clock):
    limiter.options.rpm = 2
    limiter.reserve(100)
    limiter.reserve(100)
    limiter.sleeps.clear()
    limiter.reserve(100)  # window penuh -> tidur, lalu lolos setelah clock maju
    assert limiter.sleeps and limiter.sleeps[0] > 0
    assert limiter.requests_made == 3


def test_exhaust_key_rotates(limiter):
    assert limiter.exhaust_key("K1") is True   # K2 masih tersedia
    rid = limiter.reserve(100)
    assert limiter.pinned_key(rid) == "K2"     # rotasi ke key sehat
    assert limiter.exhaust_key("K2") is False  # semua habis


def test_reserve_all_keys_exhausted_pauses(limiter):
    limiter.exhaust_key("K1")
    limiter.exhaust_key("K2")
    with pytest.raises(Paused):
        limiter.reserve(100)


def test_cooldown_forces_wait(limiter):
    limiter.cooldown(30, key="K1")
    limiter.sleeps.clear()
    rid = limiter.reserve(100)
    assert limiter.sleeps and limiter.sleeps[0] > 0
    assert limiter.pinned_key(rid) in ("K1", "K2")


def test_halt_stops_reserve(limiter):
    limiter.halt("berhenti")
    with pytest.raises(Paused):
        limiter.reserve(100)


def test_request_context_manager_releases_slot(limiter):
    with limiter.request(100) as rid:
        assert limiter.active == 1
        assert limiter.pinned_key(rid) in ("K1", "K2")
    assert limiter.active == 0


def test_usage_updates_session_and_snapshot(limiter):
    rid = limiter.reserve(1000)
    limiter.usage(rid, input_tokens=800, output_tokens=200, success=True)
    snap = limiter.snapshot()
    assert snap["avg_input_tokens"] == 800
    assert snap["avg_output_tokens"] == 200
    assert snap["measured_input_requests"] == 1
    assert snap["rpm"] >= 1


def test_usage_rejects_bool_tokens(limiter):
    rid = limiter.reserve(1000)
    limiter.usage(rid, input_tokens=True, output_tokens=-3)  # diabaikan diam-diam
    entry = limiter.session[rid]
    assert entry["input"] == 1000 and entry["output"] is None


def test_snapshot_shape(limiter):
    snap = limiter.snapshot()
    for key in ("rpm", "rpm_limit", "tpm", "tpm_hard", "rpd_used", "rpd_limit",
                "rpd_remaining", "reset_at", "workers_active", "keys_total",
                "keys_available", "active_key"):
        assert key in snap
    assert snap["keys_total"] == 2 and snap["keys_available"] == 2
    assert snap["reset_at"] == next_pacific_midnight(1_728_700_000.0)


def test_key_label_unknown_key(cache, clock):
    options = TranslationOptions(api_keys=["K1"], model="m")
    rl = RateLimiter(cache, options, clock=lambda: clock[0], sleep=lambda s: None)
    assert rl.key_label("K1").startswith("#1 (")
    assert rl.key_label("ASING").startswith("#0 (")


def test_wait_uses_injected_sleep(limiter):
    limiter.wait(2.5)
    assert limiter.sleeps == [2.5]


def test_count_kind_bypasses_rpd(limiter):
    limiter.exhaust_key("K1")
    limiter.exhaust_key("K2")
    rid = limiter.reserve(100, kind="count")  # countTokens tidak kena batas RPD
    assert limiter.pinned_key(rid) in ("K1", "K2")
