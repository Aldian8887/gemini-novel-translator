"""Tests for the OpenAI-compatible gateway client (v2.4.0).

Batch A of the testing audit (12 Okt 2026): openai_client.py had 0% coverage.
All tests offline — httpx.MockTransport, no quota burned, no network.
"""
import json
from contextlib import contextmanager

import httpx
import pytest

from novel_translator.errors import ApiError, InvalidResponse
from novel_translator.models import Run, TranslationOptions, Unit
from novel_translator.openai_client import (
    USER_AGENT,
    OpenAICompatibleTranslator,
    alias_payload,
    extract_json_text,
    normalize_payload,
    parse_model_json,
    repair_unescaped_quotes,
)


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
    def performance_get(self, model, key):
        return {}


class _FakeLimiter:
    def __init__(self):
        self.controller = _FakeController()
        self.cache = _FakeCache()
        self.cooldowns = []
        self.requests_made = 0

    @contextmanager
    def request(self, estimated):
        yield object()

    def pinned_key(self, reservation):
        return "KEY1"

    def key_label(self, key):
        return "key1"

    def clock(self):
        return 1000.0

    def cooldown(self, wait, key=None):
        self.cooldowns.append(wait)

    def usage(self, reservation, *args, **kwargs):
        pass


def _options(**kw):
    base = dict(api_keys=["KEY1"], model="claude-sonnet-4-5",
                provider="openai", oa_base_url="https://gw.test/v1")
    base.update(kw)
    return TranslationOptions(**base)


def _translator(handler=None, **kw):
    transport = httpx.MockTransport(handler) if handler else httpx.MockTransport(
        lambda request: httpx.Response(500, json={}))
    client = httpx.Client(transport=transport)
    return OpenAICompatibleTranslator(_options(**kw), _FakeLimiter(), client=client)


def _unit(uid="u1", rid="r1", text="Hello world"):
    return Unit(id=uid, document="doc", context="ctx", runs=(Run(id=rid, text=text),))


def _chat(content, finish="stop"):
    return {"choices": [{"finish_reason": finish,
                         "message": {"role": "assistant", "content": content}}]}


# ------------------------------------------------------- pure functions
def test_alias_payload_roundtrip():
    user_text = json.dumps({"units": [
        {"id": "d5n15-text-0", "runs": [{"id": "d5n15-text-0-r0", "text": "Hi"}]},
        {"id": "d5n15-text-1", "runs": [{"id": "d5n15-text-1-r0", "text": "Yo"}]},
    ]})
    new_text, (unit_map, run_map) = alias_payload(user_text)
    data = json.loads(new_text)
    assert data["units"][0]["id"] == "u0"
    assert data["units"][1]["runs"][0]["id"] == "r1"
    assert unit_map == {"u0": "d5n15-text-0", "u1": "d5n15-text-1"}
    assert run_map["r0"] == "d5n15-text-0-r0"
    assert run_map["r1"] == "d5n15-text-1-r0"


def test_alias_payload_skips_non_dict_rows():
    user_text = json.dumps({"units": ["junk", {"id": "a", "runs": []}]})
    new_text, (unit_map, _) = alias_payload(user_text)
    assert unit_map == {"u1": "a"}


def test_extract_json_text_strips_fences():
    assert extract_json_text('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json_text('```\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json_text('  {"a": 1}  ') == '{"a": 1}'


def test_repair_unescaped_quotes():
    broken = '{"text": "dia bilang "halo" padanya"}'
    fixed = repair_unescaped_quotes(broken)
    assert json.loads(fixed) == {"text": 'dia bilang "halo" padanya'}


def test_repair_unescaped_quotes_keeps_escaped():
    text = '{"text": "sudah \\"escape\\" benar"}'
    assert repair_unescaped_quotes(text) == text


def test_repair_unescaped_quotes_escapes_raw_newline():
    fixed = repair_unescaped_quotes('{"text": "baris1\nbaris2"}')
    assert json.loads(fixed) == {"text": "baris1\nbaris2"}


def test_parse_model_json_strict():
    assert parse_model_json('{"a": 1}') == {"a": 1}


def test_parse_model_json_repairs():
    content = '```json\n{"text": "kata "penting" di sini"}\n```'
    assert parse_model_json(content) == {"text": 'kata "penting" di sini'}


def test_parse_model_json_garbage_raises():
    with pytest.raises(InvalidResponse):
        parse_model_json("bukan json sama sekali {{{")


@pytest.mark.parametrize("wrapper", ["units", "data", "translations", "results", "items"])
def test_normalize_payload_unwraps(wrapper):
    parsed = {wrapper: [{"id": "u0", "runs": [{"id": "r0", "text": "x"}],
                         "format": "junk", "document": "junk"}]}
    out = normalize_payload(parsed)
    assert out == [{"id": "u0", "runs": [{"id": "r0", "text": "x"}]}]


def test_normalize_payload_plain_list():
    parsed = [{"id": "u0", "runs": [{"id": "r0", "text": "x"}]}]
    assert normalize_payload(parsed) == parsed


def test_normalize_payload_no_list_raises():
    with pytest.raises(InvalidResponse):
        normalize_payload({"nothing": "here"})


def test_normalize_payload_skips_bad_items():
    out = normalize_payload([{"id": "u0", "runs": []}, {"nope": 1}, "junk"])
    assert out == [{"id": "u0", "runs": []}]


# ------------------------------------------------------- constructor
def test_init_requires_key():
    with pytest.raises(ValueError):
        OpenAICompatibleTranslator(TranslationOptions(), _FakeLimiter())


def test_init_applies_oa_output_cap():
    tr = _translator()
    assert tr.builder.output_limit == 32768  # OA_OUTPUT_CAPS claude-sonnet-4-5


def test_init_user_budget_capped_by_options():
    tr = _translator(max_output_tokens=8000)
    assert tr.builder.output_limit == 8000


# ------------------------------------------------------- _payload
def test_payload_shape():
    tr = _translator()
    payload, alias_maps = tr._payload([_unit()])
    assert payload["model"] == "claude-sonnet-4-5"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.2
    # IDs aliased in transit, originals kept in maps
    assert '"u0"' in payload["messages"][1]["content"]
    assert '"d5n15' not in payload["messages"][1]["content"]
    unit_map, run_map = alias_maps
    assert unit_map == {"u0": "u1"} and run_map == {"r0": "r1"}


# ------------------------------------------------------- _request
def test_request_sends_browser_ua_and_bearer():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_chat("{}"))

    tr = _translator(handler)
    code, _headers, parsed = tr._request({"model": "m"}, "KEY1")
    assert code == 200
    assert seen["ua"] == USER_AGENT
    assert seen["auth"] == "Bearer KEY1"
    assert seen["url"].endswith("/chat/completions")
    assert parsed["choices"][0]["finish_reason"] == "stop"


def test_request_429_passthrough():
    tr = _translator(lambda r: httpx.Response(429, json={"error": "slow"}))
    code, _, _ = tr._request({}, "KEY1")
    assert code == 429


def test_request_200_error_body_classified_429():
    body = {"error": {"type": "rate_limit_error", "message": "slow down"}}
    tr = _translator(lambda r: httpx.Response(200, json=body))
    code, _, _ = tr._request({}, "KEY1")
    assert code == 429


def test_request_200_error_body_numeric_code():
    body = {"error": {"code": 401, "message": "bad key"}}
    tr = _translator(lambda r: httpx.Response(200, json=body))
    code, _, _ = tr._request({}, "KEY1")
    assert code == 401


def test_request_200_invalid_json_raises():
    tr = _translator(lambda r: httpx.Response(200, content=b"bukan json"))
    with pytest.raises(InvalidResponse):
        tr._request({}, "KEY1")


def test_request_oversize_rejected():
    big = b"x" * (8 * 1024 * 1024 + 1)
    tr = _translator(lambda r: httpx.Response(200, content=big))
    with pytest.raises(InvalidResponse):
        tr._request({}, "KEY1")


# ------------------------------------------------------- _validated_response
def _validated(tr, content, **kw):
    return tr._validated_response([_unit()], _chat(content, kw.get("finish", "stop")),
                                  partial=kw.get("partial", False),
                                  alias_maps=kw.get("alias_maps"))


def test_validated_happy_path():
    tr = _translator()
    content = '{"units": [{"id": "u1", "runs": [{"id": "r1", "text": "Halo dunia"}]}]}'
    assert _validated(tr, content) == {"r1": "Halo dunia"}


def test_validated_remaps_aliases():
    tr = _translator()
    content = '{"units": [{"id": "u0", "runs": [{"id": "r0", "text": "Halo"}]}]}'
    out = _validated(tr, content, alias_maps=({"u0": "u1"}, {"r0": "r1"}))
    assert out == {"r1": "Halo"}


def test_validated_rejects_length_finish():
    tr = _translator()
    with pytest.raises(InvalidResponse):
        _validated(tr, "{}", finish="length")


def test_validated_rejects_missing_finish():
    tr = _translator()
    with pytest.raises(InvalidResponse):
        _validated(tr, "{}", finish="")


def test_validated_content_filter_is_api_error():
    tr = _translator()
    with pytest.raises(ApiError):
        _validated(tr, "{}", finish="content_filter")


def test_validated_rejects_empty_content():
    tr = _translator()
    with pytest.raises(InvalidResponse):
        _validated(tr, "   ")


def test_validated_rejects_bad_shape():
    tr = _translator()
    with pytest.raises(InvalidResponse):
        tr._validated_response([_unit()], {"choices": []})
    with pytest.raises(InvalidResponse):
        tr._validated_response([_unit()], "bukan dict")


def test_validated_html_injection_quarantined():
    tr = _translator()
    content = '{"units": [{"id": "u1", "runs": [{"id": "r1", "text": "<b>Halo</b>"}]}]}'
    good, bad, issues = _validated(tr, content, partial=True)
    assert good == {} and "r1" in bad
    assert any("HTML" in i for i in issues)


def test_validated_partial_salvage():
    tr = _translator()
    units = [_unit("u1", "r1"), _unit("u2", "r2")]
    content = ('{"units": [{"id": "u1", "runs": [{"id": "r1", "text": "OK"}]},'
               ' {"id": "u2", "runs": [{"id": "r2", "text": "   "}]}]}')
    good, bad, _ = tr._validated_response(units, _chat(content), partial=True,
                                          alias_maps=None)
    assert good == {"r1": "OK"} and "r2" in bad


# ------------------------------------------------------- budget watchdog
def test_charge_response_uses_real_usage():
    tr = _translator()
    tr._charge_response({"usage": {"prompt_tokens": 100, "completion_tokens": 25}}, 999)
    assert tr._spent_now() == 125


def test_charge_response_falls_back_to_estimate():
    tr = _translator()
    tr._charge_response({}, 999)
    assert tr._spent_now() == 999


def test_charge_response_ignores_bool_usage():
    tr = _translator()
    tr._charge_response({"usage": {"prompt_tokens": True, "completion_tokens": 10}}, 50)
    assert tr._spent_now() == 60  # True bukan int valid -> hanya completion + estimasi


# ------------------------------------------------------- translate_batch e2e
def test_translate_batch_happy_path():
    def handler(request):
        body = json.loads(request.content)
        assert "response_format" in body  # mode JSON dipakai percobaan pertama
        content = '{"units": [{"id": "u0", "runs": [{"id": "r0", "text": "Halo dunia"}]}]}'
        return httpx.Response(200, json=_chat(content))

    tr = _translator(handler)
    assert tr.translate_batch([_unit()]) == {"r1": "Halo dunia"}


def test_translate_batch_retries_without_response_format():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(400, json={"error": "bad"})
        content = '{"units": [{"id": "u0", "runs": [{"id": "r0", "text": "OK"}]}]}'
        return httpx.Response(200, json=_chat(content))

    tr = _translator(handler)
    assert tr.translate_batch([_unit()]) == {"r1": "OK"}
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_translate_batch_budget_pause():
    tr = _translator(oa_token_budget=10)  # estimasi unit kecil > 10
    with pytest.raises(Exception) as exc:
        tr.translate_batch([_unit(text="x" * 5000)])
    assert "Anggaran token" in str(exc.value)


def test_translate_batch_non_retryable_400():
    tr = _translator(lambda r: httpx.Response(400, json={"error": "nope"}),
                     max_retries=0)
    with pytest.raises(ApiError):
        tr.translate_batch([_unit()])


def test_translate_batch_transport_timeout_gives_api_error():
    def handler(request):
        raise httpx.ConnectTimeout("boom")

    tr = _translator(handler, max_retries=0)
    with pytest.raises(ApiError) as exc:
        tr.translate_batch([_unit()])
    assert "terputus/timeout" in str(exc.value)


def test_translate_batch_long_cooldown_pauses():
    tr = _translator(lambda r: httpx.Response(503, json={}), max_retry_wait=0.001,
                     max_retries=5)
    with pytest.raises(Exception) as exc:
        tr.translate_batch([_unit()])
    assert "jeda panjang" in str(exc.value)
