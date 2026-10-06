"""Local semantic settings and manual quota controls; never persist API credentials."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import TranslationOptions, canonical
from .storage import TranslationCache, atomic_bytes, workspace_lock


def load_preferences(workspace):
    path = Path(workspace) / "preferences.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(workspace, options):
    atomic_bytes(Path(workspace) / "preferences.json",
                 (canonical(options.settings()) + "\n").encode("utf-8"), overwrite=True)


def checkpoints(workspace):
    path = Path(workspace) / "cache" / "translations.sqlite3"
    if not path.exists():
        return []
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            rows = db.execute("SELECT id,source,settings,state,done,total FROM jobs ORDER BY updated DESC LIMIT 30")
            return [dict(id=i, source=source, settings=json.loads(settings), state=state, done=done, total=total)
                    for i, source, settings, state, done, total in rows]
    except (sqlite3.Error, ValueError):
        return []


def stored_quota(workspace, model, key=None):
    """RPD lokal terpakai. key=None memakai scope lama (kompatibilitas);
    beri key untuk akuntansi per key pada v2.2.0+."""
    import time
    from .errors import CacheError
    from .models import digest
    from .rate_limit import next_pacific_midnight, local_rpd_used
    now = time.time()
    scope = f"{model}:{digest(key)[:16]}" if key else model
    path = Path(workspace) / "cache" / "translations.sqlite3"
    used = 0
    if path.exists():
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                used = local_rpd_used(db, scope, now)
        except (sqlite3.Error, CacheError):
            return None, next_pacific_midnight(now)
    return used, next_pacific_midnight(now)


def stored_quota_limits(workspace, model):
    from .errors import CacheError
    from .rate_limit import quota_state
    path = Path(workspace) / "cache" / "translations.sqlite3"
    if path.exists():
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                return quota_state(db, model, "local-rpd-limits-v1")
        except (sqlite3.Error, CacheError):
            pass
    return {}


def save_local_quota(workspace, model, *, used=None, official_rpd=None, rpd=None, key=None):
    """Manual edits require an idle workspace; preserve all progress and quota history.

    key=None memakai scope lama; beri key untuk mengoreksi counter satu key saja.
    """
    import time
    from .models import digest
    from .rate_limit import set_local_rpd_used
    values = {k: v for k, v in dict(official_rpd=official_rpd, rpd=rpd).items() if v is not None}
    TranslationOptions(model=model, **values).validate()
    scope = f"{model}:{digest(key)[:16]}" if key else model
    root = Path(workspace) / "cache"
    with workspace_lock(root / "workspace.lock"), TranslationCache(root / "translations.sqlite3") as cache:
        with cache.lock, cache.db:
            cache.db.execute("BEGIN IMMEDIATE")
            if used is not None:
                set_local_rpd_used(cache.db, scope, time.time(), used)
            if values:
                cache.db.execute("""INSERT INTO performance_state VALUES (?,?,?)
                    ON CONFLICT(scope,key) DO UPDATE SET payload=excluded.payload""",
                    (model, "local-rpd-limits-v1", canonical(values)))
