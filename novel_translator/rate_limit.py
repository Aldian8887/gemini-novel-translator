from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, time as day_time, timedelta
from zoneinfo import ZoneInfo

from .adaptive import AdaptiveController
from .errors import CacheError, Paused
from .models import canonical, digest
from .storage import check_cancel

PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_midnight(timestamp):
    current = datetime.fromtimestamp(timestamp, PACIFIC)
    return datetime.combine(current.date(), day_time(), PACIFIC).timestamp()


def next_pacific_midnight(timestamp):
    current = datetime.fromtimestamp(timestamp, PACIFIC)
    return datetime.combine(current.date() + timedelta(days=1), day_time(), PACIFIC).timestamp()


def quota_state(db, scope, key):
    # Read-only UI access also works before a v2.0.x database has the optional table.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='performance_state' AND type='table'").fetchone():
        return {}
    row = db.execute("SELECT payload FROM performance_state WHERE scope=? AND key=?", (scope, key)).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row[0])
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    raise CacheError("State RPD lokal tidak dapat dibaca; progres terjemahan tetap disimpan.")


def generation_requests_today(db, scope, timestamp):
    start = pacific_midnight(timestamp)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='request_metrics' AND type='table'").fetchone():
        # Missing metrics belong to older generation-only versions. Exclude known countTokens calls.
        return db.execute("""SELECT COUNT(*) FROM requests r
            LEFT JOIN request_metrics m ON m.request_id=r.rowid
            WHERE r.scope=? AND r.sent>=? AND (m.kind IS NULL OR m.kind='generate')""", (scope, start)).fetchone()[0]
    return db.execute("SELECT COUNT(*) FROM requests WHERE scope=? AND sent>=?", (scope, start)).fetchone()[0]


def local_rpd_used(db, scope, timestamp):
    count = generation_requests_today(db, scope, timestamp)
    saved = quota_state(db, scope, "local-rpd-v1")
    if saved.get("day") == pacific_midnight(timestamp):
        offset = saved.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise CacheError("Koreksi RPD lokal tidak valid; progres terjemahan tetap disimpan.")
        count += offset
    return max(0, count)


def set_local_rpd_used(db, scope, timestamp, used):
    """Caller holds the write transaction. Never touch requests, jobs or translations."""
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        raise ValueError("Current Local RPD Used harus bilangan bulat non-negatif.")
    saved = dict(day=pacific_midnight(timestamp), offset=used-generation_requests_today(db, scope, timestamp))
    db.execute("""INSERT INTO performance_state VALUES (?,?,?)
        ON CONFLICT(scope,key) DO UPDATE SET payload=excluded.payload""",
        (scope, "local-rpd-v1", canonical(saved)))


class RateLimiter:
    """One atomic reservation ledger for ALL workers, keys and jobs in a workspace.

    v2.2.0: akuntansi RPD/RPM/TPM per API key (satu scope per key per model),
    karena kuota Google berlaku per project, bukan per key. reserve() memilih
    key yang masih berkuota secara round-robin dan mem-pin key tersebut pada
    request; translator memakai key yang di-pin agar tidak ada balapan dengan
    rotasi. exhaust_key() menandai satu key habis tanpa menghentikan key lain.

    countTokens calls are conservatively charged to local RPM/TPM only. Local
    RPD counts generation attempts, including retries. Other
    applications/projects cannot be observed by a local ledger.
    """
    def __init__(self, cache, options, cancel_event=None, status=None,
                 clock=time.time, sleep=None, controller=None):
        self.cache, self.options = cache, options
        self.cancel_event, self.status, self.clock = cancel_event, status, clock
        self.controller = controller or AdaptiveController(options, cache)
        self.changed = threading.Condition(cache.lock)
        self.sleep = sleep
        self.requests_made = 0
        self.active = 0
        self.stopped = None
        self.session = {}
        self._keys = options.keys()
        self._key_index = 0
        self._key_lock = threading.Lock()
        # Urutan lock: cache.lock (via self.changed) -> _key_lock. Jangan dibalik.

    # ---- multi-key helpers ----

    def _key_scope(self, key):
        """Scope akuntansi per key. Digest saja; key asli tidak pernah masuk DB."""
        return f"{self.options.model}:{digest(key)[:16]}"

    def current_key(self):
        """Key aktif saat ini (best-effort; untuk request gunakan pinned_key)."""
        with self._key_lock:
            if not self._keys:
                raise ValueError("API key Gemini belum diisi.")
            return self._keys[self._key_index]

    def pinned_key(self, request_id):
        """Key yang di-pin pada satu reservasi request."""
        with self.changed:
            return self.session[request_id]["key"]

    def key_label(self, key):
        return self.options.key_label(key)

    def _key_used(self, db, key, now):
        return local_rpd_used(db, self._key_scope(key), now)

    def _select_key(self, db, now):
        """Pilih key yang masih punya kuota RPD, round-robin dari posisi aktif.

        Dipanggil dengan cache write lock dipegang (di dalam reserve()).
        Kembalikan None bila semua key habis.
        """
        with self._key_lock:
            start = self._key_index
            total = len(self._keys)
        for offset in range(total):
            index = (start + offset) % total
            key = self._keys[index]
            if self._key_used(db, key, now) < self.options.rpd:
                with self._key_lock:
                    self._key_index = index
                return key
        return None

    def _available_count(self, now):
        try:
            with self.cache.lock:
                return sum(1 for key in self._keys
                           if self._key_used(self.cache.db, key, now) < self.options.rpd)
        except (sqlite3.Error, CacheError):
            return 0

    def exhaust_key(self, key):
        """Tandai satu key mencapai batas harian (counter lokal = batas).

        Kembalikan True bila masih ada key lain yang berkuota.
        """
        scope = self._key_scope(key)
        try:
            with self.changed, self.cache.db:
                self.cache.db.execute("BEGIN IMMEDIATE")
                set_local_rpd_used(self.cache.db, scope, self.clock(), self.options.rpd)
        except sqlite3.Error:
            raise CacheError("Gagal menandai key habis; proses dihentikan.") from None
        return self._available_count(self.clock()) > 0

    def wait(self, seconds, message="Menunggu kuota rolling 60 detik"):
        if self.status:
            self.status(f"{message}: {seconds:.1f} detik.")
        if self.sleep is not None:
            self.sleep(max(0, seconds))
        else:
            # Notification wakes early when usage reconciliation frees capacity.
            # Poll cancellation at most once/second; no fixed request pacing.
            with self.changed:
                self.changed.wait(timeout=max(0, min(seconds, 1.0)))
        check_cancel(self.cancel_event)

    def halt(self, message):
        with self.changed:
            self.stopped = message
            self.changed.notify_all()

    def cooldown(self, seconds, key=None):
        """Cooldown per key: 429 di satu project tidak menghentikan key lain."""
        if not self._keys:
            return
        scope = self._key_scope(key) if key is not None else self._key_scope(self.current_key())
        try:
            with self.changed, self.cache.db:
                self.cache.db.execute(
                    "INSERT INTO cooldowns VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET until=MAX(until,excluded.until)",
                    (scope, self.clock() + seconds))
                self.changed.notify_all()
        except sqlite3.Error:
            raise CacheError("Gagal menyimpan cooldown API; proses dihentikan.") from None

    def reserve(self, tokens, kind="generate", acquire_slot=False):
        if not isinstance(tokens, int) or tokens < 0:
            raise ValueError("Reservasi token tidak valid.")
        if tokens > self.options.tpm:
            raise Paused("Satu request melebihi batas TPM lokal. Turunkan target input/request.")
        while True:
            check_cancel(self.cancel_event)
            state = self.controller.snapshot()
            try:
                with self.changed, self.cache.db:
                    # Acquire the SQLite write lock BEFORE reading quota/time;
                    # also protects independent connections to this workspace.
                    self.cache.db.execute("BEGIN IMMEDIATE")
                    now = self.clock()
                    if self.stopped:
                        raise Paused(self.stopped)
                    if self.requests_made >= self.options.max_requests:
                        raise Paused("Batas request sesi tercapai. Progres tersimpan; lanjutkan sesi berikutnya.")
                    # Keep schema v2 and its three-column requests table intact.
                    self.cache.db.execute("DELETE FROM requests WHERE sent < ?", (now - 172800,))
                    self.cache.db.execute("DELETE FROM request_metrics WHERE request_id NOT IN (SELECT rowid FROM requests)")
                    if kind == "generate":
                        if not self._keys:
                            raise ValueError("API key Gemini belum diisi.")
                        key = self._select_key(self.cache.db, now)
                        if key is None:
                            raise Paused("Batas RPD lokal tercapai untuk semua API key. "
                                         "Progres tersimpan; reset tengah malam waktu Pasifik atau tambah key.")
                    else:
                        # countTokens tidak kena batas RPD; pakai key aktif.
                        key = self.current_key()
                    scope = self._key_scope(key)
                    self.cache.db.execute("DELETE FROM cooldowns WHERE scope=? AND until<=?", (scope, now))
                    row = self.cache.db.execute("SELECT until FROM cooldowns WHERE scope=?", (scope,)).fetchone()
                    cooldown = max(0, row[0] - now) if row else 0
                    if cooldown > self.options.max_retry_wait:
                        raise Paused("Cooldown Gemini masih aktif. Lanjutkan setelah kuota/jeda server pulih.")
                    window = list(self.cache.db.execute(
                        "SELECT sent,tokens FROM requests WHERE scope=? AND sent>? ORDER BY sent",
                        (scope, now - 60)))
                    wait = cooldown
                    rpm = min(self.options.rpm, state["effective_rpm"])
                    # A batch packed before a downshift must still fit alone.
                    # Small target headroom accommodates conservative in-flight
                    # estimates (e.g. seven ~34K actual prompts). Hard TPM remains
                    # absolute; never shrink a batch simply to consume leftover TPM.
                    margin = min(state["target_tpm"] * .02, tokens * .05 * state["workers_limit"])
                    soft_tpm = min(self.options.tpm, max(tokens, int(state["target_tpm"] + margin)))
                    if len(window) >= rpm:
                        wait = max(wait, window[-rpm][0] + 60 - now)
                    total = sum(n for _, n in window)
                    for sent, amount in window:
                        if total + tokens <= soft_tpm:
                            break
                        wait = max(wait, sent + 60 - now)
                        total -= amount
                    last = self.cache.db.execute(
                        "SELECT MAX(sent) FROM requests WHERE scope=?", (scope,)).fetchone()[0]
                    if last is not None and self.options.delay_seconds:
                        wait = max(wait, last + self.options.delay_seconds - now)
                    if acquire_slot and self.active >= state["workers_limit"]:
                        wait = max(wait, 1.0)  # notification on completion, not pacing
                    if wait <= 0:
                        row = self.cache.db.execute("INSERT INTO requests VALUES (?,?,?)", (scope, now, tokens))
                        request_id = row.lastrowid
                        self.cache.db.execute("INSERT INTO request_metrics(request_id,kind) VALUES (?,?)",
                                              (request_id, kind))
                        self.requests_made += 1
                        self.session[request_id] = dict(kind=kind, input=tokens, output=None,
                                                        actual=False, success=False, key=key)
                        if acquire_slot:
                            self.active += 1
                        return request_id
            except sqlite3.Error:
                raise CacheError("Gagal mencatat penggunaan API; request dibatalkan agar batas tidak terlewati.") from None
            self.wait(wait)

    @contextmanager
    def request(self, tokens, kind="generate"):
        request_id = self.reserve(tokens, kind, acquire_slot=True)
        try:
            yield request_id
        finally:
            with self.changed:
                self.active -= 1
                self.changed.notify_all()

    def usage(self, request_id, input_tokens=None, output_tokens=None, success=False):
        def valid(n):
            return isinstance(n, int) and not isinstance(n, bool) and n >= 0
        try:
            with self.changed, self.cache.db:
                entry = self.session[request_id]
                if valid(input_tokens) and input_tokens > 0:
                    self.cache.db.execute("UPDATE requests SET tokens=? WHERE rowid=?", (input_tokens, request_id))
                    self.cache.db.execute("UPDATE request_metrics SET actual_input=? WHERE request_id=?",
                                          (input_tokens, request_id))
                    entry.update(input=input_tokens, actual=True)
                if valid(output_tokens):
                    self.cache.db.execute("UPDATE request_metrics SET output=? WHERE request_id=?", (output_tokens, request_id))
                    entry["output"] = output_tokens
                if success:
                    self.cache.db.execute("UPDATE request_metrics SET success=1 WHERE request_id=?", (request_id,))
                    entry["success"] = True
                self.changed.notify_all()
        except sqlite3.Error:
            raise CacheError("Pencatatan token aktual gagal. Request berikutnya dihentikan.") from None

    def snapshot(self):
        with self.cache.lock:
            now = self.clock()
            key = self._keys[self._key_index] if self._keys else None
            scope = self._key_scope(key) if key else self.options.model
            rpm, tpm = self.cache.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM requests WHERE scope=? AND sent>?",
                (scope, now - 60)).fetchone()
            daily = local_rpd_used(self.cache.db, scope, now)
            success = [r for r in self.session.values() if r["kind"] == "generate" and r["success"]]
            outputs = [r["output"] for r in success if r["output"] is not None]
            keys_available = sum(1 for k in self._keys
                                 if local_rpd_used(self.cache.db, self._key_scope(k), now) < self.options.rpd)
            return dict(rpm=rpm, rpm_limit=self.options.rpm, tpm=tpm, tpm_hard=self.options.tpm,
                        rpd_used=daily, rpd_limit=self.options.rpd, official_rpd=self.options.official_rpd,
                        rpd_remaining=max(0, self.options.rpd-daily),
                        reset_at=next_pacific_midnight(now), workers_active=self.active,
                        avg_input_tokens=sum(r["input"] for r in success)/len(success) if success else 0,
                        avg_output_tokens=sum(outputs)/len(outputs) if outputs else None,
                        measured_input_requests=sum(r["actual"] for r in success),
                        count_tokens_calls=sum(r["kind"] == "count" for r in self.session.values()),
                        active_key=self.options.key_label(key) if key else "—",
                        keys_total=len(self._keys), keys_available=keys_available)
