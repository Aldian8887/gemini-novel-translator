"""Tests for the Gemini API client retry/salvage paths.

Batch C of the testing audit (12 Okt 2026): gemini_client.py had 23% coverage.
All tests offline — httpx.MockTransport, no quota burned, no network.
"""
import json
from contextlib import contextmanager

import httpx
import pytest

from novel_translator.errors import ApiError, InvalidResponse, KeyExhausted, Paused
from novel_translator.gemini_client import (
    GeminiTranslator,
    decode_json,
    no_duplicate_keys,
    quota_kind,
    retry_seconds,
)
from novel_translator.models import Run, TranslationOptions, Unit


# ---------------------------------------------------------------- fakes
class _FakeController:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"output_ratio": 1.0, "workers_limit": 2}

    def quota_error(self, kind):
        self.calls.append(("quota_error", kind))

    def success(self, *args):
        self.calls.append(("success", args))

    def retry(self):
        self.calls.append(("retry",))


class _FakeCache:
    def __init__(self):
        self.store = {}

    def performance_get(self, model, key):
        return self.store.get((model, key))

    def performance_set(self, model, key, value):
        self.store[(model, key)] = value


class _FakeLimiter:
    def __init__(self, keys=("K1", "K2")):
        self.controller = _FakeController()
        self.cache = _FakeCache()
        self.cooldowns = []
        self.requests_made = 0
        self.exhausted = set()
        self._keys = list(keys)
        self.halted = None

    @contextmanager
    def request(self, estimated, kind="generate"):
        self.requests_made += 1
        yield 1

    def pinned_key(self, reservation):
        return "K1"

    def current_key(self):
        return "K1"

    def key_label(self, key):
        return "#1"

    def clock(self):
        return 1000.0

    def cooldown(self, wait, key=None):
        self.cooldowns.append((wait, key))

    def usage(self, *args, **kwargs):
        pass

    def halt(self, message):
        self.halted = message

    def exhaust_key(self, key):
        self.exhausted.add(key)
        return len(self.exhausted) < len(self._keys)


def _options(**kw):
    base = dict(api_keys=["K1", "K2"], model="gemini-2.0-flash",
                token_count_mode="estimate")
    base.update(kw)
    return TranslationOptions(**base)


def _translator(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GeminiTranslator(_options(**kw), _FakeLimiter(), client=client)


def _unit(uid="u1", rid="r1", text="Hello world"):
    return Unit(id=uid, document="doc", context="ctx", runs=(Run(id=rid, text=text),))


def _gemini_ok(text):
    return {"candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20}}


def _content_for(*pairs):
    units = [{"id": u, "runs": [{"id": r, "text": t}]} for u, r, t in pairs]
    return json.dumps(units)


# ------------------------------------------------------- pure functions
def test_no_duplicate_keys():
    assert no_duplicate_keys([("a", 1), ("b", 2)]) == {"a": 1, "b": 2}
    with pytest.raises(InvalidResponse):
        no_duplicate_keys([("a", 1), ("a", 2)])


def test_decode_json_strict():
    assert decode_json('{"a": 1}') == {"a": 1}


def test_decode_json_rejects_duplicates():
    with pytest.raises(InvalidResponse):
        decode_json('{"a": 1, "a": 2}')


def test_decode_json_rejects_constants():
    with pytest.raises(InvalidResponse):
        decode_json('{"a": NaN}')


def test_decode_json_rejects_garbage():
    with pytest.raises(InvalidResponse):
        decode_json("bukan json")


@pytest.mark.parametrize("headers,body,expected", [
    ({"retry-after": "30"}, {}, 30.0),
    ({}, {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                 "retryDelay": "5s"}]}}, 5.0),
    ({"retry-after": "10"},
     {"error": {"details": [{"@type": "x/RetryInfo", "retryDelay": "25s"}]}}, 25.0),
    ({"retry-after": "junk"}, {}, 0.0),
    ({}, {}, 0.0),
])
def test_retry_seconds(headers, body, expected):
    assert retry_seconds(headers, body, 1000.0) == expected


def test_retry_seconds_http_date():
    # Wed, 21 Oct 2015 07:28:00 GMT = 1445412480
    assert retry_seconds({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"},
                         {}, 1445412470.0) == 10.0


@pytest.mark.parametrize("body,expected", [
    ({"error": {"details": [{"@type": "x/QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerDay",
         "quotaMetric": "generativelanguage.googleapis.com/generate_requests_per_day"}]}]}}, "daily"),
    ({"error": {"details": [{"@type": "x/QuotaFailure", "violations": [
        {"quotaMetric": "tokens", "quotaValue": "0"}]}]}}, "zero"),
    ({"error": {"details": [{"@type": "x/QuotaFailure", "violations": [
        {"quotaMetric": "generate_tokens_per_minute"}]}]}}, "tpm"),
    ({"error": {"details": [{"@type": "x/QuotaFailure", "violations": [
        {"quotaMetric": "generate_requests_per_minute"}]}]}}, "rpm"),
    ({"error": {"message": "Daily limit exceeded"}}, "daily"),
    ({}, None),
    ({"error": "junk"}, None),
])
def test_quota_kind(body, expected):
    assert quota_kind(body) == expected


# ------------------------------------------------------- _request
def test_request_sends_key_header_and_model_url():
    seen = {}

    def handler(request):
        seen["key"] = request.headers["x-goog-api-key"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_gemini_ok("[]"))

    tr = _translator(handler)
    code, _, parsed = tr._request({"x": 1}, key="K1")
    assert code == 200
    assert seen["key"] == "K1"
    assert "gemini-2.0-flash:generateContent" in seen["url"]


def test_request_falls_back_to_current_key():
    seen = {}

    def handler(request):
        seen["key"] = request.headers["x-goog-api-key"]
        return httpx.Response(200, json=_gemini_ok("[]"))

    tr = _translator(handler)
    tr._request({"x": 1})
    assert seen["key"] == "K1"


def test_request_classifies_resource_exhausted():
    body = {"error": {"status": "RESOURCE_EXHAUSTED", "code": 429}}
    tr = _translator(lambda r: httpx.Response(200, json=body))
    code, _, _ = tr._request({})
    assert code == 429


def test_request_oversize_rejected():
    big = b"x" * (8 * 1024 * 1024 + 1)
    tr = _translator(lambda r: httpx.Response(200, content=big))
    with pytest.raises(InvalidResponse):
        tr._request({})


def test_request_bad_json_under_400_raises():
    tr = _translator(lambda r: httpx.Response(200, content=b"nope"))
    with pytest.raises(InvalidResponse):
        tr._request({})


# ------------------------------------------------------- _validated_response
def test_validated_happy_path():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    parsed = _gemini_ok(_content_for(("u1", "r1", "Halo dunia")))
    assert tr._validated_response([_unit()], parsed) == {"r1": "Halo dunia"}


def test_validated_skips_thought_parts():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    parsed = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "pikiran", "thought": True},
        {"text": _content_for(("u1", "r1", "Halo"))}]}}]}
    assert tr._validated_response([_unit()], parsed) == {"r1": "Halo"}


def test_validated_rejects_max_tokens():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    parsed = {"candidates": [{"finishReason": "MAX_TOKENS",
                              "content": {"parts": [{"text": "[]"}]}}]}
    with pytest.raises(InvalidResponse):
        tr._validated_response([_unit()], parsed)


def test_validated_rejects_missing_finish():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    parsed = {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}
    with pytest.raises(InvalidResponse):
        tr._validated_response([_unit()], parsed)


def test_validated_other_finish_is_api_error():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    parsed = {"candidates": [{"finishReason": "SAFETY",
                              "content": {"parts": [{"text": "[]"}]}}]}
    with pytest.raises(ApiError):
        tr._validated_response([_unit()], parsed)


def test_validated_block_reason_is_api_error():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ApiError):
        tr._validated_response([_unit()], {"promptFeedback": {"blockReason": "SAFETY"}})


def test_validated_no_text_parts():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    parsed = {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}
    with pytest.raises(InvalidResponse):
        tr._validated_response([_unit()], parsed)


def test_validated_partial_salvage():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    units = [_unit("u1", "r1"), _unit("u2", "r2")]
    parsed = _gemini_ok(_content_for(("u1", "r1", "OK"), ("u2", "r2", "   ")))
    good, bad, _ = tr._validated_response(units, parsed, partial=True)
    assert good == {"r1": "OK"} and "r2" in bad


def test_validated_html_quarantine():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    parsed = _gemini_ok(_content_for(("u1", "r1", "<i>Halo</i>")))
    good, bad, issues = tr._validated_response([_unit()], parsed, partial=True)
    assert good == {} and "r1" in bad and any("HTML" in i for i in issues)


# ------------------------------------------------------- _count
def _count_translator(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    lim = _FakeLimiter()
    return GeminiTranslator(_options(token_count_mode="auto", **kw), lim, client=client), lim


def test_count_estimate_mode_skips():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    assert tr._count({"x": 1}, 100) is None


def test_count_uses_cache():
    tr, lim = _count_translator(lambda r: httpx.Response(200, json={}))
    from novel_translator.models import canonical, digest
    body = {"contents": [{"parts": [{"text": "x"}]}]}
    key = "count:" + digest(canonical(body))
    lim.cache.performance_set(tr.options.model, key, 4321)
    assert tr._count(body, 100) == 4321


def test_count_stores_result():
    def handler(request):
        if "countTokens" in str(request.url):
            return httpx.Response(200, json={"totalTokens": 777})
        return httpx.Response(200, json=_gemini_ok("[]"))

    tr, lim = _count_translator(handler)
    body = {"contents": [{"parts": [{"text": "x"}]}]}
    assert tr._count(body, 100) == 777
    from novel_translator.models import canonical, digest
    key = "count:" + digest(canonical(body))
    assert lim.cache.performance_get(tr.options.model, key) == 777


def test_count_429_daily_rotates_key():
    def handler(request):
        return httpx.Response(429, json={"error": {"details": [{"@type": "x/QuotaFailure",
            "violations": [{"quotaMetric": "generate_requests_per_day"}]}]}})

    tr, lim = _count_translator(handler)
    assert tr._count({"contents": [{"parts": [{"text": "x"}]}]}, 100) is None
    assert "K1" in lim.exhausted  # key ditandai habis, rotasi diserahkan ke generate


# ------------------------------------------------------- _quota_stop
def test_quota_stop_rotates_when_spare_key():
    tr = _translator(lambda r: httpx.Response(200, json={}))
    with pytest.raises(KeyExhausted):
        tr._quota_stop("daily", "K1")


def test_quota_stop_pauses_when_all_exhausted():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    lim = _FakeLimiter(keys=("K1",))  # cuma 1 key -> exhaust = habis semua
    tr = GeminiTranslator(_options(), lim, client=client)
    with pytest.raises(Paused):
        tr._quota_stop("daily", "K1")
    assert lim.halted is not None


# ------------------------------------------------------- translate_batch
def test_translate_batch_happy_path():
    tr = _translator(lambda r: httpx.Response(200, json=_gemini_ok(
        _content_for(("u1", "r1", "Halo dunia")))))
    assert tr.translate_batch([_unit()]) == {"r1": "Halo dunia"}


def test_translate_batch_429_then_success():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "slow"}})
        return httpx.Response(200, json=_gemini_ok(_content_for(("u1", "r1", "OK"))))

    tr = _translator(handler)
    assert tr.translate_batch([_unit()]) == {"r1": "OK"}
    assert len(calls) == 2
    assert tr.limiter.cooldowns  # cooldown dicatat


def test_translate_batch_invalid_response_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=_gemini_ok("bukan json"))

    tr = _translator(handler)
    with pytest.raises(InvalidResponse):
        tr.translate_batch([_unit()])
    assert len(calls) == 1  # tidak diulang; koordinator yang membelah


def test_translate_batch_400_label():
    tr = _translator(lambda r: httpx.Response(400, json={}), max_retries=0)
    with pytest.raises(ApiError) as exc:
        tr.translate_batch([_unit()])
    assert "tidak diterima" in str(exc.value)


def test_translate_batch_transport_error_retries_then_raises():
    def handler(request):
        raise httpx.ConnectTimeout("boom")

    tr = _translator(handler, max_retries=0)
    with pytest.raises(ApiError) as exc:
        tr.translate_batch([_unit()])
    assert "terputus/timeout" in str(exc.value)


def test_translate_batch_partial_salvage_e2e():
    tr = _translator(lambda r: httpx.Response(200, json=_gemini_ok(
        _content_for(("u1", "r1", "OK"), ("u2", "r2", "   ")))))
    units = [_unit("u1", "r1"), _unit("u2", "r2")]
    good, bad, _ = tr.translate_batch_partial(units)
    assert good == {"r1": "OK"} and "r2" in bad
