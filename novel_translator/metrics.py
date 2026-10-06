from __future__ import annotations

import math
import re
import time

from .token_budget import text_tokens


class ProgressMetrics:
    def __init__(self, runs, cached):
        self.amounts = {rid: (len(r.text), len(re.findall(r"\b\w+(?:['’-]\w+)*\b", r.text)),
                              text_tokens(r.text)) for rid, r in runs.items()}
        self.total = tuple(sum(a[i] for a in self.amounts.values()) for i in range(3))
        self.done = [sum(self.amounts[rid][i] for rid in cached) for i in range(3)]
        self.initial = tuple(self.done)
        self.started = time.monotonic()
        self.committed_requests = 0

    def commit(self, targets):
        for rid in targets:
            for i, amount in enumerate(self.amounts[rid]):
                self.done[i] += amount
        self.committed_requests += 1

    def snapshot(self, limiter, controller):
        state = limiter.snapshot() | controller.snapshot()
        elapsed = max(.001, time.monotonic() - self.started)
        new_chars = self.done[0] - self.initial[0]
        new_tokens = self.done[2] - self.initial[2]
        remaining_tokens = max(0, self.total[2] - self.done[2])
        source_per_request = (new_tokens / self.committed_requests if self.committed_requests
                              else state["target_batch_tokens"] * .72)
        requests = math.ceil(remaining_tokens / max(1, source_per_request))
        efficiency = 1 + state["retries"] / max(1, state["successful"])
        needed = math.ceil(requests * efficiency)
        state.update(translated_characters=self.done[0], translated_words=self.done[1],
                     translated_source_tokens=self.done[2], source_tokens_estimated=True,
                     total_source_tokens=self.total[2], session_source_tokens=new_tokens,
                     tokens_per_minute=new_tokens * 60 / elapsed,
                     characters_per_minute=new_chars * 60 / elapsed,
                     estimated_requests_remaining=requests, estimated_rpd_needed=needed,
                     eta_seconds=remaining_tokens * elapsed / new_tokens if new_tokens else None,
                     rpd_bottleneck=needed > state["rpd_remaining"],
                     elapsed_seconds=elapsed)
        return state
