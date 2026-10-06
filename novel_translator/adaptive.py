"""Shared adaptive policy. Hard project ceilings always live outside this policy."""
from __future__ import annotations

import math
import threading
from .models import canonical, digest


class AdaptiveController:
    def __init__(self, options, cache=None):
        self.lock = threading.RLock()
        self.options = options
        self.cache = cache
        self.key = "adaptive-v1:" + digest(canonical({k: getattr(options, k) for k in
            ("target_tpm", "tpm", "rpm", "workers", "target_input_tokens", "max_input_tokens",
             "auto_tune", "max_output_tokens")}))
        self.ceiling_tpm = min(options.target_tpm, options.tpm)
        self.target_tpm = min(220000, self.ceiling_tpm) if options.auto_tune else self.ceiling_tpm
        self.batch_target = options.target_input_tokens
        self.rpm = options.rpm
        self.workers = options.workers
        self.batch_streak = self.quota_streak = 0
        self.throttled = False
        self.output_ratio = 1.65
        self.successful = self.errors_429 = self.retries = self.invalid = 0
        if cache:
            saved = cache.performance_get(options.model, self.key) or {}
            for field, lower, upper in (
                ("target_tpm", 512, self.ceiling_tpm), ("batch_target", 512, options.max_input_tokens),
                ("rpm", 1, options.rpm), ("workers", 1, options.workers),
                ("output_ratio", 1.5, 4.0),
            ):
                if field == "batch_target" and not options.auto_tune:
                    continue  # Ignore reductions persisted by older manual-mode sessions.
                value = saved.get(field)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    setattr(self, field, min(upper, max(lower, value if field == "output_ratio" else int(value))))
            self.throttled = bool(saved.get("throttled", False))

    def _persist(self):
        if self.cache:
            self.cache.performance_set(self.options.model, self.key, {field: getattr(self, field) for field in
                ("target_tpm", "batch_target", "rpm", "workers", "output_ratio", "throttled")})

    def snapshot(self):
        with self.lock:
            return dict(target_tpm=self.target_tpm, target_batch_tokens=self.batch_target,
                        effective_rpm=self.rpm, workers_limit=self.workers,
                        successful=self.successful, errors_429=self.errors_429,
                        retries=self.retries, invalid_responses=self.invalid,
                        output_ratio=self.output_ratio)

    def retry(self):
        with self.lock:
            self.retries += 1

    def bad_response(self):
        with self.lock:
            self.invalid += 1
            self.batch_streak = self.quota_streak = 0
            if self.options.auto_tune:
                self.batch_target = max(512, int(self.batch_target * .90))
            self._persist()

    def quota_error(self, kind):
        with self.lock:
            self.errors_429 += 1
            self.batch_streak = self.quota_streak = 0
            self.throttled = True
            if kind in {"tpm", None, "unknown"}:
                self.target_tpm = max(512, int(self.target_tpm * .90))
            if kind in {"rpm", None, "unknown"}:
                self.rpm = max(1, math.floor(self.rpm * .8))
                self.workers = max(1, self.workers - 1)
            self._persist()

    def success(self, source_tokens=0, output_tokens=0, output_overhead=0, input_tokens=0):
        with self.lock:
            self.successful += 1
            self.batch_streak += 1
            self.quota_streak += 1
            if source_tokens and output_tokens:
                observed = max(0, output_tokens - output_overhead) / source_tokens
                # Keep a margin over the observed expansion; decay slowly.
                self.output_ratio = max(1.5, min(4.0, max(observed * 1.10, self.output_ratio * .98)))
            if self.batch_streak >= 10:
                # A tiny final/split batch is not evidence that a 40K batch fits.
                if input_tokens >= self.batch_target * .80 and self.options.auto_tune:
                    self.batch_target = min(self.options.max_input_tokens, self.batch_target + 2000)
                self.batch_streak = 0
            if self.throttled and self.quota_streak >= 20:
                self.target_tpm = min(self.ceiling_tpm, math.ceil(self.target_tpm * 1.04))
                self.rpm = min(self.options.rpm, self.rpm + 1)
                self.workers = min(self.options.workers, self.workers + 1)
                self.quota_streak = 0
                self.throttled = (self.target_tpm < self.ceiling_tpm or self.rpm < self.options.rpm)
            elif not self.throttled and self.options.auto_tune and self.quota_streak >= 10:
                self.target_tpm = min(self.ceiling_tpm, math.ceil(self.target_tpm * 1.04))
                self.quota_streak = 0
            self._persist()
