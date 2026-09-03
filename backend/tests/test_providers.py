import base64
import json
from types import SimpleNamespace

import pytest

from app.services import providers


def test_api_base_url_normalizes_host_and_preserves_version_path():
    assert providers.api_base_url("https://provider.example") == "https://provider.example/v1"
    assert providers.api_base_url("https://provider.example/v1/") == "https://provider.example/v1"
    assert providers.provider_url("https://provider.example/v1?token=ignored", "models") == "https://provider.example/v1/models"


def test_chat_payload_forwards_openai_options_and_tools():
    request = providers.ProviderTextRequest(
        model="gpt-text",
        messages=[{"role": "user", "content": "hello"}],
        temperature=0.2,
        top_p=0.8,
        max_tokens=100,
        reasoning_effort="medium",
        response_format={"type": "json_object"},
        tools=[{"type": "function", "function": {"name": "generate_image"}}],
        tool_choice="auto",
        extra={"metadata": {"trace": "sample"}},
    )
    payload = providers.build_chat_payload(request)
    assert payload["stream"] is True
    assert payload["stream_options"]["include_usage"] is True
    assert payload["temperature"] == 0.2
    assert payload["reasoning_effort"] == "medium"
    assert payload["tools"][0]["function"]["name"] == "generate_image"
    assert payload["metadata"] == {"trace": "sample"}


def test_responses_payload_flattens_function_tools():
    request = providers.ProviderTextRequest(
        model="gpt-text",
        messages=[{"role": "user", "content": "hello"}],
        response_format={"type": "json_object"},
        tools=[{"type": "function", "function": {"name": "generate_image", "description": "draw", "parameters": {"type": "object"}}}],
    )
    payload = providers.build_responses_payload(request)
    assert payload["input"][0]["role"] == "user"
    assert payload["text"] == {"format": {"type": "json_object"}}
    assert payload["tools"][0]["name"] == "generate_image"
    assert "function" not in payload["tools"][0]


def test_xhigh_reasoning_is_forwarded_for_both_openai_text_transports():
    request = providers.ProviderTextRequest(
        model="gpt-text",
        messages=[{"role": "user", "content": "solve"}],
        reasoning_effort="xhigh",
    )
    assert providers.build_chat_payload(request)["reasoning_effort"] == "xhigh"
    assert providers.build_responses_payload(request)["reasoning"] == {"effort": "xhigh"}


def test_validated_reasoning_cannot_be_overridden_by_extra_options():
    request = providers.ProviderTextRequest(
        model="gpt-text",
        messages=[{"role": "user", "content": "solve"}],
        reasoning_effort="low",
        extra={
            "reasoning_effort": "invalid",
            "reasoning": {"effort": "invalid"},
            "metadata": {"trace": "sample"},
        },
    )

    chat_payload = providers.build_chat_payload(request)
    assert chat_payload["reasoning_effort"] == "low"
    assert "reasoning" not in chat_payload
    assert chat_payload["metadata"] == {"trace": "sample"}

    responses_payload = providers.build_responses_payload(request)
    assert responses_payload["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in responses_payload
    assert responses_payload["metadata"] == {"trace": "sample"}


def test_tool_call_fragments_are_merged():
    target = {}
    result = providers.TextResult(chunks=iter(()))
    first = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call-1", "function": {"name": "generate_", "arguments": '{"prompt":"'}}
                    ]
                }
            }
        ]
    }
    second = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"name": "image", "arguments": 'draw"}'}}
                    ]
                },
                "finish_reason": "tool_calls"
            }
        ]
    }
    providers._consume_chat_item(first, target, result)
    providers._consume_chat_item(second, target, result)
    result.tool_calls = [target[0]]
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "generate_image"
    assert result.tool_calls[0].parsed_arguments() == {"prompt": "draw"}


def test_tool_call_final_message_does_not_duplicate_streamed_fragments():
    target = {}
    result = providers.TextResult(chunks=iter(()))
    providers._consume_chat_item({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "generate_image", "arguments": '{"prompt":"draw"}'}}]}}]}, target, result)
    providers._consume_chat_item({"choices": [{"message": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "generate_image", "arguments": '{"prompt":"draw"}'}}]}, "finish_reason": "tool_calls"}]}, target, result)
    assert target[0].name == "generate_image"
    assert target[0].parsed_arguments() == {"prompt": "draw"}


def test_chat_sse_error_frame_is_not_an_empty_success():
    with pytest.raises(ValueError, match="upstream unavailable"):
        providers._consume_chat_item({"error": {"message": "upstream unavailable"}}, {}, providers.TextResult(chunks=iter(())))


def test_image_response_decodes_base64():
    encoded = base64.b64encode(b"png-bytes").decode()

    class FakeResponse:
        headers = {"x-request-id": "request-1"}

        def json(self):
            return {"data": [{"b64_json": encoded, "mime_type": "image/png"}], "usage": {"output_tokens": 1}}

    parsed = providers._parse_image_response(FakeResponse())
    assert parsed.content == b"png-bytes"
    assert parsed.mime_type == "image/png"
    assert parsed.provider_request_id == "request-1"
    assert parsed.usage == {"output_tokens": 1}


def test_image_response_accepts_common_data_alias():
    encoded = base64.b64encode(b"png-bytes").decode()

    class FakeResponse:
        headers = {}

        def json(self):
            return {"data": [{"data": encoded, "mime_type": "image/png"}]}

    parsed = providers._parse_image_response(FakeResponse())
    assert parsed.content == b"png-bytes"


def test_image_response_surfaces_json_error_frame():
    class FakeResponse:
        headers = {}

        def json(self):
            return {"error": {"message": "image quota exhausted"}}

    with pytest.raises(ValueError, match="image quota exhausted"):
        providers._parse_image_response(FakeResponse())


def test_image_request_forwards_custom_size_and_quality(monkeypatch):
    encoded = base64.b64encode(b"png-bytes").decode()
    captured = {}

    class FakeResponse:
        headers = {}
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"b64_json": encoded, "mime_type": "image/png"}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(providers.httpx, "post", fake_post)
    monkeypatch.setattr(providers, "channel_key", lambda _channel: "TOKEN")
    channel = SimpleNamespace(base_url="https://provider.example/v1", capabilities_json={})

    providers.image_request(channel, "gpt-image-2", "直播间截图", "2048x1152", quality="high")

    assert captured["url"] == "https://provider.example/v1/images/generations"
    assert captured["json"]["size"] == "2048x1152"
    assert captured["json"]["quality"] == "high"


def test_base64_output_is_rejected_before_decode_when_over_limit(monkeypatch):
    monkeypatch.setattr(providers, "_max_image_bytes", lambda: 4)
    encoded = base64.b64encode(b"012345").decode()
    with pytest.raises(ValueError, match="too large"):
        providers._decode_image_data({"b64_json": encoded, "mime_type": "image/png"})


def test_image_url_requires_http_scheme_and_public_target():
    with pytest.raises(ValueError):
        providers._validate_download_target("file:///tmp/image.png")


def test_responses_stream_merges_item_and_call_id_aliases(monkeypatch):
    """Responses argument deltas and final items must become one tool call."""

    lines = [
        'data: {"type":"response.created","response":{"id":"resp-1","status":"in_progress"}}',
        'data: {"type":"response.output_item.added","item":{"id":"fc-item","type":"function_call","call_id":"call-1","name":"generate_image"}}',
        'data: {"type":"response.output_text.delta","delta":"before "}',
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc-item","delta":"{\\"prompt\\":\\"draw\\"}"}',
        'data: {"type":"response.output_item.done","item":{"id":"fc-item","type":"function_call","call_id":"call-1","name":"generate_image","arguments":"{\\"prompt\\":\\"draw\\"}"}}',
        'data: {"type":"response.completed","response":{"id":"resp-1","status":"completed","usage":{"input_tokens":3,"output_tokens":2}}}',
        'data: [DONE]',
    ]

    class FakeResponse:
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(lines)

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *_):
            return False

    class FakeClient:
        def __init__(self, **_):
            pass

        def stream(self, *_, **__):
            return FakeStream()

        def close(self):
            return None

    monkeypatch.setattr(providers.httpx, "Client", FakeClient)
    monkeypatch.setattr(providers, "channel_key", lambda _channel: "TOKEN")
    channel = SimpleNamespace(base_url="https://provider.example/v1", capabilities_json={})
    result = providers.openai_responses(channel, "gpt-text", [{"role": "user", "content": "hello"}])

    assert "".join(result.chunks) == "before "
    assert result.provider_request_id == "resp-1"
    assert result.usage == {"input_tokens": 3, "output_tokens": 2}
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].parsed_arguments() == {"prompt": "draw"}


def test_responses_sse_error_frame_fails_the_stream(monkeypatch):
    class FakeResponse:
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(['data: {"type":"error","error":{"message":"upstream unavailable"}}'])

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *_):
            return False

    class FakeClient:
        def __init__(self, **_):
            pass

        def stream(self, *_, **__):
            return FakeStream()

        def close(self):
            return None

    monkeypatch.setattr(providers.httpx, "Client", FakeClient)
    monkeypatch.setattr(providers, "channel_key", lambda _channel: "TOKEN")
    channel = SimpleNamespace(base_url="https://provider.example/v1", capabilities_json={})
    result = providers.openai_responses(channel, "gpt-text", [{"role": "user", "content": "hello"}])
    with pytest.raises(ValueError, match="upstream unavailable"):
        list(result.chunks)


def test_responses_event_line_supplies_missing_event_type(monkeypatch):
    """Gateways may put the Responses event name in `event:` only."""

    lines = [
        "event: response.output_text.delta",
        'data: {"delta":"hello"}',
        "event: response.completed",
        'data: {"response":{"id":"resp-event","status":"completed"}}',
        "data: [DONE]",
    ]

    class FakeResponse:
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(lines)

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *_):
            return False

    class FakeClient:
        def __init__(self, **_):
            pass

        def stream(self, *_, **__):
            return FakeStream()

        def close(self):
            return None

    monkeypatch.setattr(providers.httpx, "Client", FakeClient)
    monkeypatch.setattr(providers, "channel_key", lambda _channel: "TOKEN")
    channel = SimpleNamespace(base_url="https://provider.example/v1", capabilities_json={})
    result = providers.openai_responses(channel, "gpt-text", [{"role": "user", "content": "hello"}])

    assert "".join(result.chunks) == "hello"
    assert result.provider_request_id == "resp-event"
    assert result.finish_reason == "stop"
