from __future__ import annotations

import json
import random
import re
import threading
import time

import httpx

from .errors import ApiError, InvalidResponse, Paused, RepackRequired
from .gemini_client import decode_json, retry_seconds
from .models import canonical, validate_response_partial
from .storage import check_cancel
from .token_budget import PromptBuilder, TokenEstimator

# v2.4.0: klien gateway OpenAI-compatible (chat completions), opt-in lewat
# options.provider == "openai". Kontrak prompt PERSIS sama dengan jalur
# Gemini (PromptBuilder yang sama, payload JSON unit yang sama, validator
# yang sama) — yang berbeda hanya transport dan bentuk respons. Gateway
# seperti bansosai menolak User-Agent bawaan pustaka HTTP (Cloudflare),
# jadi klien selalu mengirim User-Agent ala browser.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_RESPONSE = 8 * 1024 * 1024
RETRYABLE = {408, 429, 500, 502, 503, 504}

# v2.4.1: batas output per model gateway. Batch yang lebih besar membagi
# biaya tetap tiap request (instruksi sistem, overhead gateway, rangka JSON)
# ke lebih banyak teks. Model yang tidak terdaftar mempertahankan bawaan
# konservatif PromptBuilder; --max-output-tokens pengguna tetap menjadi
# batas atas. Jalur Gemini tidak memakai tabel ini.
OA_OUTPUT_CAPS = {"claude-sonnet-4-5": 32768, "qwen-3-8-max": 32768}


def alias_payload(user_text):
    """Petakan ID unit/run asli ke alias pendek selama perjalanan ke gateway.

    ID asli seperti "d5n15-text-0" memakan token di kedua arah dan mudah
    salah disalin model. Di lapisan transport ini ID diganti u0../r0.. lalu
    dipetakan kembali sebelum validasi, jadi validator, cache, dan identitas
    unit tidak pernah melihat alias. Mengembalikan (teks_baru, peta) dengan
    peta alias -> ID asli.
    """
    data = json.loads(user_text)
    unit_map, run_map = {}, {}
    run_seq = 0
    for index, row in enumerate(data.get("units", [])):
        if not isinstance(row, dict):
            continue
        alias = f"u{index}"
        unit_map[alias] = row.get("id")
        row["id"] = alias
        for run in row.get("runs", []):
            if not isinstance(run, dict):
                continue
            run_alias = f"r{run_seq}"
            run_seq += 1
            run_map[run_alias] = run.get("id")
            run["id"] = run_alias
    return canonical(data), (unit_map, run_map)


def extract_json_text(content: str) -> str:
    """Buang pagar ```json bila model membungkus responsnya."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def repair_unescaped_quotes(text: str) -> str:
    """Perbaiki JSON yang rusak karena tanda kutip ASCII mentah di dalam string.

    Sebagian model gateway (mis. Claude via aggregator) menulis teks dialog
    yang diawali/diselipkan karakter " tanpa escape, sehingga JSON-nya tidak
    sah. Pemindai ini membedakan kutip penutup struktural (diikuti , } ] :)
    dari kutip mentah di dalam teks (diikuti karakter lain) dan meng-escape
    yang mentah; karakter kontrol mentah di dalam string ikut di-escape.
    Hanya dipakai sebagai jalan kedua setelah parse ketat gagal; teks hasil
    perbaikan persis seperti yang dimaksud model.
    """
    out = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            if i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ",}]:":
                out.append(ch)
                in_string = False
            else:
                out.append('\\"')
            i += 1
            continue
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def parse_model_json(content: str):
    """Parse respons model: parse ketat dulu, perbaikan kutip sebagai cadangan."""
    text = extract_json_text(content)
    try:
        return decode_json(text)
    except InvalidResponse:
        return decode_json(repair_unescaped_quotes(text))


def normalize_payload(parsed) -> list:
    """Samakan bentuk respons gateway dengan kontrak Gemini.

    Model non-Gemini sering membungkus daftar unit sebagai
    {"units": [...]} dan menggemakan kunci ekstra (format, document, ...).
    Normalisasi membongkar bungkus itu dan membuang kunci selain id/runs/
    text SEBELUM validate_response_partial, sehingga aturan validasi yang
    berlaku persis sama seperti jalur Gemini.
    """
    if isinstance(parsed, dict):
        rows = None
        for key in ("units", "data", "translations", "results", "items"):
            if isinstance(parsed.get(key), list):
                rows = parsed[key]
                break
        if rows is None:
            for value in parsed.values():
                if isinstance(value, list):
                    rows = value
                    break
        if rows is None:
            raise InvalidResponse("Respons provider tidak memuat daftar unit.")
        parsed = rows
    if not isinstance(parsed, list):
        raise InvalidResponse("Respons provider bukan daftar unit.")
    normalized = []
    for item in parsed:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        rows = []
        raw_runs = item.get("runs")
        if isinstance(raw_runs, list):
            for row in raw_runs:
                if (isinstance(row, dict) and isinstance(row.get("id"), str)
                        and isinstance(row.get("text"), str)):
                    rows.append({"id": row["id"], "text": row["text"]})
        normalized.append({"id": item["id"], "runs": rows})
    return normalized


class OpenAICompatibleTranslator:
    """Antarmuka sama dengan GeminiTranslator (dipakai pipeline apa adanya)."""

    def __init__(self, options, limiter, cancel_event=None, status=None, client=None):
        if not options.keys():
            raise ValueError("API key provider OpenAI-compatible belum diisi.")
        self.options, self.limiter = options, limiter
        self.cancel_event, self.status = cancel_event, status
        self.controller = limiter.controller
        self.builder = PromptBuilder(options)
        cap = OA_OUTPUT_CAPS.get(options.model)
        if cap:
            self.builder.output_limit = min(cap, options.max_output_tokens)
        # v2.4.1: watchdog anggaran token sesi (khusus jalur gateway).
        # Memakai usage asli bila gateway mengembalikannya, selain itu
        # estimasi lokal. 0 = mati; jalur Gemini tidak memiliki ini.
        self._budget = max(0, int(getattr(options, "oa_token_budget", 0) or 0))
        self._spent = 0
        self._spend_lock = threading.Lock()
        self.estimator = TokenEstimator(limiter.cache, options.model)
        self.owned = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(options.timeout, connect=min(15, options.timeout)),
            follow_redirects=False, limits=httpx.Limits(max_connections=options.workers,
                                                       max_keepalive_connections=options.workers))

    def close(self):
        if self.owned:
            self.client.close()

    def _charge(self, amount):
        if amount and amount > 0:
            with self._spend_lock:
                self._spent += int(amount)

    def _spent_now(self):
        with self._spend_lock:
            return self._spent

    def _charge_response(self, parsed, estimated):
        """Catat pemakaian satu respons untuk watchdog anggaran.

        Request yang tidak pernah sampai (transport putus sebelum respons)
        tidak dicatat; gateway hanya menghitung yang diterimanya.
        """
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        if not isinstance(usage, dict):
            usage = {}

        def _num(name):
            value = usage.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        prompt_tokens = _num("prompt_tokens")
        self._charge((prompt_tokens if prompt_tokens is not None else estimated)
                     + (_num("completion_tokens") or 0))

    def body(self, units):
        return self.builder.body(units)

    def _payload(self, units):
        body = self.body(units)
        user_text, alias_maps = alias_payload(body["contents"][0]["parts"][0]["text"])
        return {"model": self.options.model,
                "messages": [
                    {"role": "system",
                     "content": body["systemInstruction"]["parts"][0]["text"]},
                    {"role": "user",
                     "content": user_text}],
                "temperature": self.options.temperature,
                "max_tokens": self.builder.output_limit,
                "response_format": {"type": "json_object"}}, alias_maps

    def _request(self, payload, key):
        # Satu panggilan REST per percobaan; tanpa retry otomatis transport.
        # Body respons tidak pernah masuk log/error (bisa memuat teks buku).
        started = time.monotonic()
        url = self.options.oa_base_url.rstrip("/") + "/chat/completions"
        with self.client.stream(
            "POST", url, json=payload,
            headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT,
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
            # Sebagian gateway membalas HTTP 200 dengan body {"error": ...};
            # klasifikasikan dari kode error-nya tanpa menampilkan isi body.
            if code == 200 and isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                error = parsed["error"]
                err_code = error.get("code")
                if isinstance(err_code, int) and 400 <= err_code < 600:
                    code = err_code
                elif "rate" in str(error.get("type", "")).lower() or "429" in str(error.get("message", "")):
                    code = 429
                else:
                    code = 502
            return code, dict(response.headers), parsed

    def _validated_response(self, units, parsed, partial=False, alias_maps=None):
        if not isinstance(parsed, dict):
            raise InvalidResponse("Respons API bukan objek.")
        choices = parsed.get("choices", [])
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise InvalidResponse("Kandidat respons tidak lengkap.")
        choice = choices[0]
        finish = choice.get("finish_reason")
        if finish == "length":
            raise InvalidResponse("Respons terpotong (finish_reason=length).")
        if not finish:
            raise InvalidResponse("Respons terpotong: finish_reason tidak ada.")
        if finish == "content_filter":
            raise ApiError("Provider memblokir bagian ini (content_filter). "
                           "Progres sebelumnya tetap tersimpan.")
        message = choice.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise InvalidResponse("Respons tidak berisi teks.")
        payload = normalize_payload(parse_model_json(content))
        if alias_maps:
            # Kembalikan ID asli sebelum validasi; alias hanya dikenal
            # lapisan transport dan tidak pernah lolos ke validator/cache.
            unit_map, run_map = alias_maps
            for item in payload:
                item["id"] = unit_map.get(item["id"], item["id"])
                for run in item["runs"]:
                    run["id"] = run_map.get(run["id"], run["id"])
        good, bad, issues = validate_response_partial(units, payload)
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
        """Seperti translate_batch, tetapi potongan valid dari respons tidak
        sempurna tetap dikembalikan untuk salvage (semantik v2.2.4)."""
        return self._translate(units, partial=True)

    def _translate(self, units, partial=False):
        body = self.body(units)
        estimated = self.estimator.estimate(body)
        hard_input = min(self.options.max_input_tokens, self.options.tpm)
        if estimated > hard_input:
            raise RepackRequired("Estimasi input melebihi anggaran; packing ulang tanpa generation request.")
        if self.builder.predicted_output(units, self.controller.snapshot()["output_ratio"]) > self.builder.output_limit * .90:
            raise RepackRequired("Preflight input/output melebihi anggaran; pecah sebelum generation request.")
        payload, alias_maps = self._payload(units)
        last = None
        attempt = 0
        rf_retry = False
        while True:
            check_cancel(self.cancel_event)
            if self._budget and self._spent_now() + estimated > self._budget:
                raise Paused(
                    f"Anggaran token sesi provider tercapai (~{self._spent_now():,} "
                    f"dari {self._budget:,} token). Progres tersimpan; naikkan "
                    "--oa-token-budget atau lanjutkan setelah kuota reset.")
            server_wait, code, key = 0.0, None, None
            try:
                with self.limiter.request(estimated) as reservation:
                    key = self.limiter.pinned_key(reservation)
                    if self.status:
                        spent_note = (f"; terpakai sesi ini ~{self._spent_now():,} token"
                                      if self._budget else "")
                        self.status(f"Mengirim batch ~{estimated:,} input token "
                                    f"(key {self.limiter.key_label(key)}); request lokal {self.limiter.requests_made}{spent_note}.")
                    code, headers, parsed = self._request(payload, key)
                    self._charge_response(parsed, estimated)
                    if code == 400 and "response_format" in payload and not rf_retry:
                        # Sebagian rute gateway menolak response_format; buang
                        # dan coba lagi tanpa mengubah hal lain.
                        rf_retry = True
                        payload = {k: v for k, v in payload.items() if k != "response_format"}
                        last = ApiError("Gateway menolak response_format; mencoba tanpa mode JSON.")
                    elif code >= 400:
                        if code == 429:
                            self.controller.quota_error(None)
                        if code not in RETRYABLE:
                            labels = {400: "Request tidak diterima; periksa dukungan model dan pengaturan API.",
                                      401: "API key tidak valid.",
                                      403: "API key tidak memiliki izin untuk model ini.",
                                      404: "Model tidak tersedia untuk API key ini."}
                            raise ApiError(labels.get(code, f"Provider menolak request (HTTP {code})."))
                        server_wait = retry_seconds(headers, parsed if isinstance(parsed, dict) else {},
                                                    self.limiter.clock())
                        if not server_wait:
                            server_wait = min(120.0, 5 * 2 ** attempt + random.uniform(0, 1))
                        self.limiter.cooldown(server_wait, key=key)
                        diag = getattr(self.limiter, "diag", None)
                        if diag is not None:
                            diag.log("http_retry", units=len(units),
                                     runs=sum(len(u.runs) for u in units),
                                     error=f"HTTP {code}")
                        last = ApiError(f"Provider sementara tidak tersedia (HTTP {code}); batas retry tercapai.")
                    elif code != 200:
                        raise ApiError(f"Respons HTTP {code} tidak didukung; redirect tidak diikuti.")
                    else:
                        usage = parsed.get("usage", {}) if isinstance(parsed, dict) else {}
                        usage = usage if isinstance(usage, dict) else {}

                        def token_value(name, src=usage):
                            value = src.get(name)
                            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

                        actual, output = token_value("prompt_tokens"), token_value("completion_tokens")
                        self.limiter.usage(reservation, actual, output)
                        self.estimator.observe(body, actual)
                        result = self._validated_response(units, parsed, partial=partial,
                                                          alias_maps=alias_maps)
                        self.limiter.usage(reservation, success=True)
                        self.controller.success(self.builder.source_tokens(units),
                                                output or 0,
                                                self.builder.output_overhead(units),
                                                actual or estimated)
                        return result
            except InvalidResponse:
                # Sama seperti jalur Gemini: jangan ulangi request cacat yang
                # identik; koordinator membelah batch dan menyimpan progres.
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as exc:
                last = ApiError("Koneksi provider terputus/timeout; batas retry tercapai.")
                diag = getattr(self.limiter, "diag", None)
                if diag is not None:
                    diag.log("timeout" if isinstance(exc, httpx.TimeoutException) else "transport_error",
                             units=len(units), runs=sum(len(u.runs) for u in units), error=exc)
            if rf_retry and last is not None and "response_format" in str(last):
                rf_retry = False
                continue
            wait = server_wait or min(120.0, 5 * 2 ** attempt + random.uniform(0, 1))
            self.limiter.cooldown(wait, key=key)
            if wait > self.options.max_retry_wait:
                raise Paused("Provider meminta jeda panjang. Progres tersimpan; lanjutkan setelah cooldown.")
            if attempt >= self.options.max_retries:
                break
            self.controller.retry()
            attempt += 1
        raise last or ApiError("Request provider gagal.")
