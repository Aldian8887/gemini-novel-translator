"""Failure diagnostics: append-only JSONL log of failed batches.

v2.2.1. Helps identify WHY batches fail (ID mismatch, broken JSON,
empty text, MAX_TOKENS, HTTP retries, timeouts, ...).

The log records only error messages, batch sizes and unit IDs -- never
response bodies, request bodies, or API keys. Safe to share for debugging.

File: <workspace>/diagnostics/failures.jsonl (one line per failed attempt).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class FailureLogger:
    """Thread-safe append-only JSONL failure logger.

    Pass ``path=None`` to disable (all ``log`` calls become no-ops).
    """

    def __init__(self, path: Path | None, *, job: str = "", title: str = "") -> None:
        self._lock = threading.Lock()
        self.path = path
        self.job = job
        self.title = title
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, *, units: int = 0, runs: int = 0,
            est_tokens: int = 0, error: object = "", sample_ids: list[str] | None = None,
            extra: dict | None = None) -> None:
        """Append one failure record. ``kind`` is one of:

        - ``invalid_response``: batch failed response validation in the
          pipeline (the ``error`` message says exactly which check failed).
        - ``http_retry``: request failed at HTTP level (429/5xx/...) and will
          be retried; ``error`` carries the HTTP code.
        - ``timeout``: request timed out at HTTP level.
        - ``repack``: batch exceeded the input budget preflight and must be
          repacked smaller (not counted as a retry).
        """
        if self.path is None:
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "job": self.job,
            "title": self.title,
            "kind": kind,
            "units": units,
            "runs": runs,
            "est_input_tokens": int(est_tokens),
            "error": str(error)[:300],
        }
        if sample_ids:
            record["sample_ids"] = [str(i) for i in sample_ids[:5]]
        if extra:
            for key, value in extra.items():
                record.setdefault(str(key), value)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
