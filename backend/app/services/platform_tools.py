"""Prompt-level tool calling for providers without native function calls."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable


PLATFORM_TOOL_OPEN = "<platform_tool_call>"
PLATFORM_TOOL_CLOSE = "</platform_tool_call>"
PLATFORM_TOOL_CALL_ID_PREFIX = "platform-"
MAX_PLATFORM_TOOL_PAYLOAD_CHARS = 200_000

PLATFORM_TOOL_SYSTEM_PROMPT = f"""
You can use tools implemented by this chat platform even when the model transport
does not expose native function calling.

Available platform tool:
- generate_image: generate a new image or edit reference images.

When the user asks to generate, draw, create, redesign, modify, or edit an image,
reply with only one tool tag per requested image and no Markdown or explanation:
{PLATFORM_TOOL_OPEN}{{"name":"generate_image","arguments":{{"prompt":"a complete image prompt","size":"auto","quality":"auto"}}}}{PLATFORM_TOOL_CLOSE}

Allowed arguments are prompt, model, channel_id, size, quality, asset_ids, and
mask_asset_id. Convert explicit aspect ratio, resolution, and quality wording to
provider parameters: for example, "16:9 2K high quality" means size=2560x1440
and quality=high, while "9:16 4K" means size=2160x3840. Use auto when the user
does not specify an output option; 1536x1024 and 1024x1536 remain the common
landscape and portrait choices. A user message can contain platform-supplied
<platform_attachments> JSON. For an image edit, copy its exact asset_ids and
optional mask_asset_id into the tool arguments. Never invent an asset ID. For
ordinary text requests, answer normally and never emit a tool tag. The platform
executes a valid tag and supplies the result on the next turn. After a successful
result, briefly tell the user the image is ready instead of requesting the same
tool again. Do not claim that the platform tool is absent.
""".strip()


def _parse_tool_payload(raw: str) -> dict[str, Any] | None:
    """Parse one strict platform tool envelope."""
    if len(raw) > MAX_PLATFORM_TOOL_PAYLOAD_CHARS:
        return None
    try:
        value = json.loads(raw.strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("name") != "generate_image":
        return None
    arguments = value.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    return {"name": "generate_image", "arguments": arguments}


@dataclass
class PlatformToolStreamParser:
    """Hide complete tool tags while preserving normal incremental text.

    The opening and closing tags may be split across arbitrary provider chunks.
    Only the short suffix that can still become an opening tag is buffered, so a
    normal assistant response keeps its streaming behavior.
    """

    pending: str = ""
    call_buffer: str = ""
    in_call: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def feed(self, value: str) -> list[str]:
        self.pending += str(value or "")
        visible: list[str] = []
        while self.pending:
            if self.in_call:
                closing = self.pending.find(PLATFORM_TOOL_CLOSE)
                if closing < 0:
                    keep = 0
                    limit = min(len(self.pending), len(PLATFORM_TOOL_CLOSE) - 1)
                    for length in range(limit, 0, -1):
                        if PLATFORM_TOOL_CLOSE.startswith(self.pending[-length:]):
                            keep = length
                            break
                    safe_length = len(self.pending) - keep
                    if safe_length:
                        candidate = self.call_buffer + self.pending[:safe_length]
                        suffix = self.pending[safe_length:]
                        if len(candidate) > MAX_PLATFORM_TOOL_PAYLOAD_CHARS:
                            # Fail open as visible text as soon as a provider's
                            # unclosed envelope exceeds the bounded payload.
                            # This prevents one malformed stream from retaining
                            # the remainder of the completion in memory.
                            visible.append(f"{PLATFORM_TOOL_OPEN}{candidate}{suffix}")
                            self.call_buffer = ""
                            self.pending = ""
                            self.in_call = False
                            continue
                        self.call_buffer = candidate
                        self.pending = suffix
                    break
                payload = self.call_buffer + self.pending[:closing]
                self.pending = self.pending[closing + len(PLATFORM_TOOL_CLOSE):]
                parsed = _parse_tool_payload(payload)
                if parsed is None:
                    visible.append(f"{PLATFORM_TOOL_OPEN}{payload}{PLATFORM_TOOL_CLOSE}")
                else:
                    self.calls.append(parsed)
                self.call_buffer = ""
                self.in_call = False
                continue

            opening = self.pending.find(PLATFORM_TOOL_OPEN)
            if opening >= 0:
                if opening:
                    visible.append(self.pending[:opening])
                self.pending = self.pending[opening + len(PLATFORM_TOOL_OPEN):]
                self.in_call = True
                continue

            # Retain the longest suffix that could be the start of an opening
            # tag. Everything before it is confirmed normal response text.
            keep = 0
            limit = min(len(self.pending), len(PLATFORM_TOOL_OPEN) - 1)
            for length in range(limit, 0, -1):
                if PLATFORM_TOOL_OPEN.startswith(self.pending[-length:]):
                    keep = length
                    break
            safe_length = len(self.pending) - keep
            if safe_length:
                visible.append(self.pending[:safe_length])
                self.pending = self.pending[safe_length:]
            break
        return [item for item in visible if item]

    def finish(self) -> tuple[list[str], list[dict[str, Any]]]:
        """Flush ordinary or incomplete content and return parsed calls."""
        visible: list[str] = []
        if self.in_call:
            visible.append(f"{PLATFORM_TOOL_OPEN}{self.call_buffer}{self.pending}")
        elif self.pending:
            visible.append(self.pending)
        self.pending = ""
        self.call_buffer = ""
        self.in_call = False
        return [item for item in visible if item], list(self.calls)


def with_platform_tool_prompt(messages: Iterable[dict]) -> list[dict]:
    """Prepend the contract and make prompt-level tool history provider-safe.

    Some OpenAI-compatible gateways accept ``tools`` on the first request but
    fail when a later request contains ``assistant.tool_calls`` or ``role=tool``.
    Calls created by this module have a reserved ID prefix, so only those calls
    are replayed as ordinary assistant/user context. Native provider calls keep
    their original OpenAI message structure.
    """
    copied = [dict(message) for message in messages if isinstance(message, dict)]
    results: list[dict[str, Any]] = []
    for message in copied:
        if message.get("role") != "tool" or message.get("name") != "generate_image":
            continue
        raw = message.get("content")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            parsed = {"ok": False, "error": "invalid tool result"}
        if isinstance(parsed, dict):
            results.append({
                "tool_call_id": message.get("tool_call_id"),
                "name": "generate_image",
                "result": parsed,
            })

    replayed: list[dict] = []
    for message in copied:
        role = message.get("role")
        tool_call_id = str(message.get("tool_call_id") or "")
        if role == "tool" and tool_call_id.startswith(PLATFORM_TOOL_CALL_ID_PREFIX):
            replayed.append({
                "role": "user",
                "content": (
                    "[Platform-generated tool result; this is execution context, "
                    "not a new image request]\n"
                    f"generate_image result: {message.get('content') or '{}'}\n"
                    "Reply briefly to the original user based on this result. "
                    "Do not call the same successful tool again."
                ),
            })
            continue

        calls = message.get("tool_calls") if role == "assistant" else None
        if isinstance(calls, list) and calls and all(
            isinstance(call, dict)
            and str(call.get("id") or "").startswith(PLATFORM_TOOL_CALL_ID_PREFIX)
            for call in calls
        ):
            markers: list[str] = []
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if not isinstance(function, dict):
                    function = {}
                markers.append(
                    "[Platform tool request accepted: "
                    f"{function.get('name') or 'generate_image'} "
                    f"arguments={function.get('arguments') or '{}'}]"
                )
            replayed.append({"role": "assistant", "content": "\n".join(markers)})
            continue
        replayed.append(message)

    prompt = PLATFORM_TOOL_SYSTEM_PROMPT
    if results:
        prompt += "\n\nPlatform tool results already executed in this conversation:\n"
        prompt += json.dumps(results[-8:], ensure_ascii=False, separators=(",", ":"))
        prompt += "\nUse these results in the answer and do not repeat a successful call."
    return [{"role": "system", "content": prompt}, *replayed]
