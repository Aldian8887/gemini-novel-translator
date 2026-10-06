from __future__ import annotations

import json
import random
import re
import time
import threading
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from .errors import ApiError, InvalidResponse, KeyExhausted, Paused, RepackRequired
from .models import canonical, digest, validate_response_partial
from .rate_limit import next_pacific_midnight
from .storage import check_cancel
from .token_budget import PromptBuilder, TokenEstimator

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_RESPONSE = 8 * 1024 * 1024


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponse("Respons JSON memiliki key duplikat.")
        result[key] = value
    return result


def decode_json(text):
    try:
        return json.loads(text, object_pairs_hook=no_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(InvalidResponse("Konstanta JSON tidak valid.")))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
        raise InvalidResponse("Respons JSON rusak atau terpotong.") from None


def retry_seconds(headers, body, now):
    values = []
    header = headers.get("retry-after", "")
    if header:
        try:
            values.append(float(header))
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                values.append(date.timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                pass
    error = body.get("error", {}) if isinstance(body, dict) else {}
    details = error.get("details", []) if isinstance(error, dict) else []
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict) and detail.get("@type", "").endswith("RetryInfo"):
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", str(detail.get("retryDelay", "")))
            if match:
                values.append(float(match[1]))
    return max([0.0, *(v for v in values if 0 <= v < float("inf"))])


def quota_kind(body):
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return None
    error = body["error"]
    labels, zero = [], False
    details = error.get("details", [])
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict) or not detail.get("@type", "").endswith("QuotaFailure"):
            continue
        violations = detail.get("violations", [])
        for violation in violations if isinstance(violations, list) else []:
            if isinstance(violation, dict):
                labels.append(str(violation.get("quotaId", "")) + " " + str(violation.get("quotaMetric", "")))
                zero |= str(violation.get("quotaValue", "")) == "0"
    # Only classify; never expose service messages that may include prompt/key data.
    label = " ".join(labels) or str(error.get("message", ""))
    if re.search(r"per.?day|daily|requests.?per.?day", label, re.I):
        return "daily"
    if zero:
        return "zero"
    if re.search(r"token|TPM", label, re.I):
        return "tpm"
    if re.search(r"per.?minute|RPM|request.*minute", label, re.I):
        return "rpm"
    return None


class GeminiTranslator:
    def __init__(self, options, limiter, cancel_event=None, status=None, client=None):
        if not options.keys():
            raise ValueError("API key Gemini belum diisi.")
        self.options, self.limiter = options, limiter
        self.cancel_event, self.status = cancel_event, status
        self.controller = limiter.controller
        self.builder = PromptBuilder(options)
        self.estimator = TokenEstimator(limiter.cache, options.model)
        self.count_lock = threading.Lock()
        self.count_disabled = False
        self.owned = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(options.timeout, connect=min(15, options.timeout)),
            follow_redirects=False, limits=httpx.Limits(max_connections=options.workers,
                                                       max_keepalive_connections=options.workers))

    def close(self):
        if self.owned:
            self.client.close()

    def system_instruction(self):
        return self.body([])["systemInstruction"]["parts"][0]["text"]

    def body(self, units):
        return self.builder.body(units)

    def _request(self, body, endpoint="generateContent", key=None):
        # A fresh REST call per attempt. No SDK/transport automatic retries, no
        # redirects, key only in a header. Response bodies never enter logs/errors.
        # key: pinned key dari reservasi limiter; fallback ke key aktif.
        api_key = key if key is not None else self.limiter.current_key()
        started = time.monotonic()
        with self.client.stream(
            "POST", f"{BASE_URL}/{self.options.model}:{endpoint}", json=body,
            headers={"x-goog-api-key": api_key,
                     "Content-Type": "application/json"}) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise InvalidResponse("Respons API terlalu besar.")
                if time.monotonic() - started > self.options.timeout:
                    raise httpx.ReadTimeout("deadline")
                chunks.append(chunk)
            raw = b"".join(chunks)
            try:
                parsed = decode_json(raw)
            except InvalidResponse:
                if response.status_code < 400:
                    raise
                parsed = {}
            code = response.status_code
            if code == 200 and isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                error = parsed["error"]
                if error.get("status") == "RESOURCE_EXHAUSTED" or error.get("code") == 429:
                    code = 429
            return code, dict(response.headers), parsed

    def _count(self, body, estimate):
        """Auto: one initial calibration per model/workspace; usage takes over.

        Always: exact preflight per body hash. All count calls reserve the SAME
        local quota conservatively, avoiding assumptions about endpoint quotas.
        """
        mode = self.options.token_count_mode
        if mode == "estimate" or self.count_disabled:
            return None
        with self.count_lock:
            if mode == "auto" and self.estimator.samples:
                return None
            key = "count:" + digest(canonical(body))
            cached = self.limiter.cache.performance_get(self.options.model, key)
            if isinstance(cached, int) and cached > 0:
                self.estimator.observe(body, cached)
                return cached
            request_body = {"generateContentRequest": dict(body, model="models/" + self.options.model)}
            with self.limiter.request(estimate, "count") as reservation:
                api_key = self.limiter.pinned_key(reservation)
                try:
                    code, headers, parsed = self._request(request_body, "countTokens", key=api_key)
                except KeyExhausted:
                    return None  # key habis; generateContent akan merotasi sendiri
                except (httpx.HTTPError, InvalidResponse):
                    pass
                else:
                    if code == 200 and isinstance(parsed, dict):
                        count = parsed.get("totalTokens")
                        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                            self.limiter.usage(reservation, count)
                            self.estimator.observe(body, count)
                            self.limiter.cache.performance_set(self.options.model, key, count)
                            return count
                    if code == 429:
                        kind = quota_kind(parsed)
                        self.controller.quota_error(kind)
                        if kind in {"daily", "zero"}:
                            try:
                                self._quota_stop(kind, api_key)
                            except KeyExhausted:
                                return None
                        wait = retry_seconds(headers, parsed, self.limiter.clock()) or 5.0
                        self.limiter.cooldown(wait, key=api_key)
                self.count_disabled = True
                if self.status:
                    self.status("countTokens tidak tersedia; memakai estimasi berkalibrasi dan token aktual dari respons.")
                return None

    def _quota_stop(self, kind, key):
        # Kuota harian/nol pada SATU key: tandai habis dan rotasi bila masih ada
        # key lain. Hanya bila semua key habis, hentikan sesi seperti dulu.
        if self.limiter.exhaust_key(key):
            raise KeyExhausted(f"Kuota harian key {self.limiter.key_label(key)} habis; "
                               "beralih ke key berikutnya.")
        if kind == "daily":
            self.limiter.cooldown(next_pacific_midnight(self.limiter.clock()) - self.limiter.clock(),
                                  key=key)
            message = ("Kuota harian Gemini habis untuk semua API key. Semua request yang sudah aktif "
                       "diselesaikan dan hasil valid disimpan; lanjutkan setelah reset Pasifik atau tambah key.")
        else:
            message = ("Kuota model pada semua API key bernilai nol. "
                       "Periksa akses Free Tier/model di Google AI Studio.")
        self.limiter.halt(message)
        raise Paused(message)

    def _validated_response(self, units, parsed, partial=False):
        if not isinstance(parsed, dict):
            raise InvalidResponse("Respons API bukan objek.")
        feedback = parsed.get("promptFeedback", {})
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ApiError("Gemini memblokir bagian ini. Bagian tidak dilewati; progres sebelumnya tetap tersimpan.")
        candidates = parsed.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            raise InvalidResponse("Kandidat respons tidak lengkap.")
        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish == "MAX_TOKENS":
            raise InvalidResponse("MAX_TOKENS: respons terpotong.")
        if not finish:
            raise InvalidResponse("Respons terpotong: finishReason tidak ada.")
        if finish != "STOP":
            raise ApiError("Gemini tidak menyelesaikan bagian ini (filter/kebijakan/finishReason). Progres sebelumnya tetap tersimpan.")
        content = candidate.get("content", {})
        if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
            raise InvalidResponse("Konten respons tidak valid.")
        texts = [p["text"] for p in content.get("parts", []) if isinstance(p, dict)
                 and not p.get("thought") and isinstance(p.get("text"), str)]
        if not texts:
            raise InvalidResponse("Respons tidak berisi teks.")
        # v2.2.4: validasi per potongan. Mode parsial mengembalikan run valid
        # beserta run rusak agar pipeline bisa menyelamatkan isi respons;
        # mode ketat mempertahankan perilaku lama (satu cacat = batch gagal).
        good, bad, issues = validate_response_partial(units, decode_json("".join(texts)))
        runmap = {r.id: r for u in units for r in u.runs}
        for rid in list(good):
            if re.search(r"</?[A-Za-z][^>]*>", good[rid]) and not re.search(r"</?[A-Za-z][^>]*>", runmap[rid].text):
                del good[rid]
                bad.add(rid)
                if "Respons menambahkan markup HTML ke slot teks." not in issues:
                    issues.append("Respons menambahkan markup HTML ke slot teks.")
        if partial:
            return good, bad, issues
        if bad:
            raise InvalidResponse(issues[0] if issues else "Respons batch tidak lengkap.",
                                  same_batch_retry=True)
        return good

    def translate_batch(self, units):
        return self._translate(units, partial=False)

    def translate_batch_partial(self, units):
        """Seperti translate_batch, tetapi respons tidak sempurna tidak dibuang:
        mengembalikan (terjemahan_valid, id_run_rusak, alasan) untuk salvage."""
        return self._translate(units, partial=True)

    def _translate(self, units, partial=False):
        body = self.body(units)
        estimated = self.estimator.estimate(body)
        hard_input = min(self.options.max_input_tokens, self.options.tpm)
        if estimated > hard_input:
            raise RepackRequired("Estimasi input melebihi anggaran; packing ulang tanpa generation request.")
        counted = self._count(body, estimated)
        estimated = counted if counted is not None else self.estimator.estimate(body)
        if estimated > hard_input or self.builder.predicted_output(
                units, self.controller.snapshot()["output_ratio"]) > self.builder.output_limit * .90:
            raise RepackRequired("Preflight input/output melebihi anggaran; pecah sebelum generation request.")
        last = None
        attempt = 0
        while True:
            check_cancel(self.cancel_event)
            server_wait, code, key = 0.0, None, None
            try:
                with self.limiter.request(estimated) as reservation:
                    # Key di-pin pada reservasi agar rotasi antar worker tidak
                    # menukar key di tengah request.
                    key = self.limiter.pinned_key(reservation)
                    if self.status:
                        self.status(f"Mengirim batch ~{estimated:,} input token "
                                    f"(key {self.limiter.key_label(key)}); request lokal {self.limiter.requests_made}.")
                    code, headers, parsed = self._request(body, key=key)
                    if code >= 400:
                        kind = quota_kind(parsed) if code == 429 else None
                        if code == 429:
                            self.controller.quota_error(kind)
                        if kind in {"daily", "zero"}:
                            self._quota_stop(kind, key)
                        if code not in {408, 429, 500, 502, 503, 504}:
                            labels = {400: "Request tidak diterima; periksa dukungan model dan pengaturan API.",
                                      401: "API key tidak valid.",
                                      403: "API key/proyek tidak memiliki izin atau wilayah tidak didukung.",
                                      404: "Model tidak tersedia untuk API key ini."}
                            raise ApiError(labels.get(code, f"Gemini menolak request (HTTP {code})."))
                        server_wait = retry_seconds(headers, parsed, self.limiter.clock())
                        # Install cooldown before releasing the shared worker slot.
                        if not server_wait:
                            server_wait = min(120.0, 5 * 2 ** attempt + random.uniform(0, 1))
                        self.limiter.cooldown(server_wait, key=key)
                        diag = getattr(self.limiter, "diag", None)
                        if diag is not None:
                            diag.log("http_retry", units=len(units),
                                     runs=sum(len(u.runs) for u in units),
                                     error=f"HTTP {code}")
                        last = ApiError(f"Gemini sementara tidak tersedia (HTTP {code}); batas retry tercapai.")
                    elif code != 200:
                        raise ApiError(f"Respons HTTP {code} tidak didukung; redirect tidak diikuti.")
                    else:
                        usage = parsed.get("usageMetadata", {}) if isinstance(parsed, dict) else {}
                        usage = usage if isinstance(usage, dict) else {}
                        def token_value(name):
                            value = usage.get(name)
                            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                        actual, output = token_value("promptTokenCount"), token_value("candidatesTokenCount")
                        self.limiter.usage(reservation, actual, output)
                        self.estimator.observe(body, actual)
                        result = self._validated_response(units, parsed, partial=partial)
                        self.limiter.usage(reservation, success=True)
                        thoughts = token_value("thoughtsTokenCount") or 0
                        self.controller.success(self.builder.source_tokens(units),
                                                (output or 0) + (thoughts if isinstance(thoughts, int) else 0),
                                                self.builder.output_overhead(units), actual or estimated)
                        return result
            except KeyExhausted as exc:
                # Rotasi key: batch yang sama dicoba ulang dengan key berikutnya.
                # Tidak memakan budget max_retries.
                if self.status:
                    self.status(str(exc))
                continue
            except InvalidResponse:
                # No identical malformed/truncated retry: coordinator splits and
                # checkpoints each successful half independently.
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as exc:
                last = ApiError("Koneksi Gemini terputus/timeout; batas retry tercapai.")
                diag = getattr(self.limiter, "diag", None)
                if diag is not None:
                    diag.log("timeout" if isinstance(exc, httpx.TimeoutException) else "transport_error",
                             units=len(units), runs=sum(len(u.runs) for u in units), error=exc)
            wait = server_wait or min(120.0, 5 * 2 ** attempt + random.uniform(0, 1))
            # Persistent cooldown also constrains OTHER workers and retries.
            self.limiter.cooldown(wait, key=key)
            if wait > self.options.max_retry_wait:
                raise Paused("Gemini meminta jeda panjang. Progres tersimpan; lanjutkan setelah cooldown.")
            if attempt >= self.options.max_retries:
                break
            self.controller.retry()
            attempt += 1
        raise last or ApiError("Request Gemini gagal.")
