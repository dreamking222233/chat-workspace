import json

import pytest
from pydantic import ValidationError

import app.services.platform_tools as platform_tools
from app.schemas.common import GenerateImageToolArguments
from app.services.platform_tools import (
    PLATFORM_TOOL_CLOSE,
    PLATFORM_TOOL_OPEN,
    PLATFORM_TOOL_SYSTEM_PROMPT,
    PlatformToolStreamParser,
    with_platform_tool_prompt,
)


def test_normal_text_keeps_streaming_with_only_short_prefix_delay():
    parser = PlatformToolStreamParser()
    visible = []
    visible.extend(parser.feed("这是一段普通回答，"))
    visible.extend(parser.feed("继续输出。"))
    tail, calls = parser.finish()
    visible.extend(tail)
    assert "".join(visible) == "这是一段普通回答，继续输出。"
    assert calls == []
    assert any(visible)


def test_platform_tool_tag_is_parsed_across_arbitrary_chunks():
    parser = PlatformToolStreamParser()
    raw = (
        f'{PLATFORM_TOOL_OPEN}{{"name":"generate_image","arguments":'
        f'{{"prompt":"蓝色圆形","size":"1024x1024"}}}}{PLATFORM_TOOL_CLOSE}'
    )
    visible = []
    for index in range(0, len(raw), 7):
        visible.extend(parser.feed(raw[index:index + 7]))
    tail, calls = parser.finish()
    visible.extend(tail)
    assert "".join(visible) == ""
    assert calls == [{"name": "generate_image", "arguments": {"prompt": "蓝色圆形", "size": "1024x1024"}}]


def test_multiple_tool_tags_and_surrounding_text_are_preserved_correctly():
    parser = PlatformToolStreamParser()
    first = {"name": "generate_image", "arguments": {"prompt": "first"}}
    second = {"name": "generate_image", "arguments": {"prompt": "second"}}
    value = f"before{PLATFORM_TOOL_OPEN}{json.dumps(first)}{PLATFORM_TOOL_CLOSE}middle{PLATFORM_TOOL_OPEN}{json.dumps(second)}{PLATFORM_TOOL_CLOSE}after"
    visible = parser.feed(value)
    tail, calls = parser.finish()
    assert "".join([*visible, *tail]) == "beforemiddleafter"
    assert [item["arguments"]["prompt"] for item in calls] == ["first", "second"]


def test_invalid_or_incomplete_tag_remains_visible_and_never_executes():
    invalid = PlatformToolStreamParser()
    visible = invalid.feed(f'{PLATFORM_TOOL_OPEN}{{"name":"unknown","arguments":{{}}}}{PLATFORM_TOOL_CLOSE}')
    tail, calls = invalid.finish()
    assert PLATFORM_TOOL_OPEN in "".join([*visible, *tail])
    assert calls == []

    incomplete = PlatformToolStreamParser()
    visible = incomplete.feed(f'{PLATFORM_TOOL_OPEN}{{"name":"generate_image"}}')
    tail, calls = incomplete.finish()
    assert "".join([*visible, *tail]).startswith(PLATFORM_TOOL_OPEN)
    assert calls == []


def test_prompt_includes_previous_platform_tool_result():
    messages = [
        {"role": "user", "content": "画图"},
        {"role": "tool", "name": "generate_image", "tool_call_id": "call-1", "content": '{"ok":true,"asset_id":"asset-1"}'},
    ]
    result = with_platform_tool_prompt(messages)
    assert result[0]["role"] == "system"
    assert PLATFORM_TOOL_OPEN in result[0]["content"]
    assert "asset-1" in result[0]["content"]
    assert result[1:] == messages


def test_prompt_lists_image_presets_and_attachment_contract():
    assert "1024x1536" in PLATFORM_TOOL_SYSTEM_PROMPT
    assert "1536x1024" in PLATFORM_TOOL_SYSTEM_PROMPT
    assert "2560x1440" in PLATFORM_TOOL_SYSTEM_PROMPT
    assert "<platform_attachments>" in PLATFORM_TOOL_SYSTEM_PROMPT
    assert "Never invent an asset ID" in PLATFORM_TOOL_SYSTEM_PROMPT


def test_platform_call_history_is_replayed_without_openai_tool_roles():
    messages = [
        {"role": "user", "content": "画一张图"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "platform-request-1-0",
                "type": "function",
                "function": {"name": "generate_image", "arguments": '{"prompt":"蓝色圆形"}'},
            }],
        },
        {
            "role": "tool",
            "name": "generate_image",
            "tool_call_id": "platform-request-1-0",
            "content": '{"ok":true,"asset_id":"asset-1"}',
        },
    ]
    result = with_platform_tool_prompt(messages)
    assert all(message.get("role") != "tool" for message in result)
    assert all("tool_calls" not in message for message in result)
    assert any("Platform tool request accepted" in message.get("content", "") for message in result)
    assert any("not a new image request" in message.get("content", "") for message in result)
    assert "asset-1" in result[0]["content"]


def test_native_tool_history_keeps_openai_message_structure():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-native-1",
                "type": "function",
                "function": {"name": "generate_image", "arguments": '{"prompt":"圆形"}'},
            }],
        },
        {
            "role": "tool",
            "name": "generate_image",
            "tool_call_id": "call-native-1",
            "content": '{"ok":true,"asset_id":"asset-native"}',
        },
    ]
    result = with_platform_tool_prompt(messages)
    assert result[1:] == messages


def test_oversized_open_or_closed_envelope_fails_open_without_buffering(monkeypatch):
    monkeypatch.setattr(platform_tools, "MAX_PLATFORM_TOOL_PAYLOAD_CHARS", 32)

    unclosed = PlatformToolStreamParser()
    assert unclosed.feed(PLATFORM_TOOL_OPEN) == []
    visible = unclosed.feed("x" * 33)
    tail, calls = unclosed.finish()
    assert "".join([*visible, *tail]) == PLATFORM_TOOL_OPEN + ("x" * 33)
    assert calls == []
    assert unclosed.call_buffer == ""
    assert unclosed.pending == ""
    assert unclosed.in_call is False

    closed = PlatformToolStreamParser()
    oversized = PLATFORM_TOOL_OPEN + ("y" * 33) + PLATFORM_TOOL_CLOSE
    visible = closed.feed(oversized)
    tail, calls = closed.finish()
    assert "".join([*visible, *tail]) == oversized
    assert calls == []
    assert closed.call_buffer == ""
    assert closed.pending == ""


def test_generate_image_arguments_reject_unknown_fields():
    with pytest.raises(ValidationError):
        GenerateImageToolArguments.model_validate({"prompt": "蓝色圆形", "unexpected": True})
