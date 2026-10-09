from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .errors import CacheError, Cancelled, Paused
from .models import canonical, digest


def check_cancel(event):
    if event is not None and event.is_set():
        raise Cancelled("Dijeda oleh pengguna. Progres yang tersimpan dapat dilanjutkan.")


def cancellable_sleep(seconds, event=None):
    if event is None:
        time.sleep(max(0, seconds))
    elif event.wait(max(0, seconds)):
        check_cancel(event)


@contextmanager
def workspace_lock(path: Path):
    """OS lock released on process death; no stale PID-file deletion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError):
            raise Paused("Ada proses lain memakai folder progres ini. Tunggu atau jeda proses tersebut.") from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def fsync_directory(path: Path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_file(staged: Path, destination: Path, overwrite=False, sleep=time.sleep):
    """Atomic publication; never delete an existing destination to work around a lock."""
    for attempt in range(6):
        try:
            if overwrite:
                os.replace(staged, destination)
            elif os.name == "nt":
                # Windows rename refuses to overwrite an existing path and also
                # works on removable filesystems that do not support hard links.
                os.rename(staged, destination)
            else:
                # Atomic no-clobber on local NTFS/APFS/ext4. Fail safely if unsupported.
                os.link(staged, destination)
                staged.unlink()
            fsync_directory(destination.parent)
            return
        except PermissionError:
            if attempt == 5:
                raise
            sleep(min(0.8, 0.05 * 2**attempt))
        except FileExistsError:
            # G4 (T-GEMINI-4): no-clobber contract is preserved — an existing
            # destination is never overwritten. Fallback below would clobber
            # via os.replace, so this must stay a hard error.
            raise
        except OSError:
            # G4 (T-GEMINI-4): filesystems without hardlink support (removable
            # media, exFAT, etc.) raise e.g. EOPNOTSUPP/EPERM/EXDEV/EMLINK from
            # os.link. Fall back to copy + atomic rename; atomicity is kept via
            # the staged+rename pattern. No retry: re-linking cannot succeed.
            fd, name = tempfile.mkstemp(prefix="." + destination.name + ".",
                                        suffix=".copytmp", dir=destination.parent)
            os.close(fd)
            tmp_dest = Path(name)
            try:
                shutil.copyfile(staged, tmp_dest)
                os.replace(tmp_dest, destination)
            except BaseException:
                tmp_dest.unlink(missing_ok=True)
                raise
            staged.unlink()
            fsync_directory(destination.parent)
            return


def atomic_bytes(path: Path, data: bytes, overwrite=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        publish_file(staged, path, overwrite)
    finally:
        staged.unlink(missing_ok=True)


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


class TranslationCache:
    """Single workspace writer; SQLite rollback journal + FULL durability."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = None
        try:
            self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("corrupt")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 2):
                raise CacheError("Versi database progres tidak didukung. Gunakan aplikasi yang sesuai.")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS translations (
                    job TEXT NOT NULL, run TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    target TEXT NOT NULL, checksum TEXT NOT NULL,
                    PRIMARY KEY(job, run));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, settings TEXT NOT NULL,
                    state TEXT NOT NULL, done INTEGER NOT NULL, total INTEGER NOT NULL,
                    updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    scope TEXT NOT NULL, sent REAL NOT NULL, tokens INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS request_time ON requests(scope, sent);
                CREATE TABLE IF NOT EXISTS cooldowns (
                    scope TEXT PRIMARY KEY, until REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS performance_state (
                    scope TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS request_metrics (
                    request_id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                    actual_input INTEGER, output INTEGER, success INTEGER NOT NULL DEFAULT 0);
                PRAGMA user_version=2;
            """)
        except (sqlite3.Error, CacheError) as exc:
            self.close()
            if isinstance(exc, CacheError):
                raise
            raise CacheError("Database progres tidak dapat dibuka atau rusak. Simpan salinannya dan gunakan folder progres baru; jangan hapus file lama.") from None

    @synchronized
    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @synchronized
    def load(self, job, fingerprints):
        try:
            rows = self.db.execute(
                "SELECT run,fingerprint,target,checksum FROM translations WHERE job=?", (job,))
            return {rid: target for rid, fp, target, checksum in rows
                    if fingerprints.get(rid) == fp and digest(target) == checksum}
        except sqlite3.Error:
            raise CacheError("Gagal membaca progres. Tidak ada request baru yang dikirim.") from None

    @synchronized
    def save_batch(self, job, fingerprints, targets):
        try:
            with self.db:
                self.db.executemany(
                    "INSERT OR REPLACE INTO translations VALUES (?,?,?,?,?)",
                    [(job, rid, fingerprints[rid], value, digest(value)) for rid, value in targets.items()])
        except sqlite3.Error:
            raise CacheError("Progres batch terakhir gagal disimpan (disk penuh/terkunci). Proses dihentikan; batch sebelumnya tetap tersimpan.") from None

    @synchronized
    def state(self, job, source, settings, state, done, total):
        try:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)",
                                (job, source, canonical(settings), state, done, total, time.time()))
        except sqlite3.Error:
            raise CacheError("Status progres gagal disimpan. Periksa ruang disk dan izin folder.") from None

    @synchronized
    def performance_get(self, scope, key):
        import json
        try:
            row = self.db.execute("SELECT payload FROM performance_state WHERE scope=? AND key=?",
                                  (scope, key)).fetchone()
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError):
            raise CacheError("State performa tidak dapat dibaca; cache terjemahan tetap disimpan.") from None

    @synchronized
    def performance_set(self, scope, key, value):
        try:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO performance_state VALUES (?,?,?)",
                                (scope, key, canonical(value)))
        except sqlite3.Error:
            raise CacheError("State performa gagal disimpan. Periksa ruang disk.") from None
