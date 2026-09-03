"""OpenAI-compatible provider adapters."""

import base64
import binascii
import ipaddress
import json
import socket
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.config import get_settings
from app.services.models import channel_key


def api_base_url(value: str) -> str:
    """Normalize a host or versioned API root to one stable API root."""
    raw = str(value or "").strip()
    if raw and "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("model channel Base URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("model channel Base URL credentials are not allowed")
    path = parsed.path.rstrip("/") or "/v1"
    # A bare host is the common admin input. Versioned roots and provider
    # specific prefixes are preserved exactly as entered.
    if path == "/":
        path = "/v1"
    # Query strings and fragments belong to a concrete request, not the API
    # root. Dropping them prevents endpoint concatenation such as
    # `/v1?token=x/models` and avoids persisting credentials in URLs.
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def provider_url(base_url: str, endpoint: str) -> str:
    return f"{api_base_url(base_url)}/{endpoint.lstrip('/')}"


@dataclass
class ToolCall:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""

    def parsed_arguments(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class TextResult:
    chunks: Iterator[str]
    usage: dict | None = None
    provider_request_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    raw_response: dict | None = None


@dataclass
class ImageResult:
    content: bytes
    mime_type: str
    provider_request_id: str | None = None
    usage: dict | None = None

    def __iter__(self):
        yield self.content
        yield self.mime_type
        yield self.provider_request_id


@dataclass
class ProviderTextRequest:
    model: str
    messages: list[dict]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    response_format: dict | None = None
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    extra: dict | None = None


def _merge_fragment(existing: str, fragment: object) -> str:
    """Merge a streamed string fragment without duplicating repeated finals."""
    value = str(fragment or "")
    if not value:
        return existing
    if not existing:
        return value
    if value == existing or existing.endswith(value):
        return existing
    if value.startswith(existing) or existing.startswith(value):
        return value if len(value) >= len(existing) else existing
    return existing + value


def build_chat_payload(request: ProviderTextRequest) -> dict:
    payload: dict = {
        "model": request.model,
        "messages": request.messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    optional = {
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_tokens": request.max_tokens,
        "reasoning_effort": request.reasoning_effort,
        "response_format": request.response_format,
        "tools": request.tools,
        "tool_choice": request.tool_choice,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    if request.extra:
        for key, value in request.extra.items():
            if key not in {"model", "messages", "stream", "stream_options", "tools", "tool_choice", "reasoning", "reasoning_effort"} and value is not None:
                payload[key] = value
    return payload


def build_responses_payload(request: ProviderTextRequest) -> dict:
    """Build the OpenAI Responses API equivalent of a chat request.

    Channels use Chat Completions by default; a channel can opt into this
    transport with ``capabilities_json["_text_endpoint"] = "responses"``.
    """
    def responses_message(message: dict) -> dict:
        """Translate Chat Completions content parts to Responses input parts."""
        content = message.get("content")
        if not isinstance(content, list):
            return message
        parts: list[dict] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                parts.append({
                    "type": "input_image",
                    "image_url": image.get("url") or "",
                    "detail": image.get("detail") or "auto",
                })
            elif part.get("type") == "text":
                parts.append({"type": "input_text", "text": str(part.get("text") or "")})
            elif part.get("type") in {"input_image", "input_text"}:
                parts.append(part)
        return {**message, "content": parts}

    responses_input: list[dict] = []
    for message in request.messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                responses_input.append({"type": "function_call", "call_id": call.get("id") or "", "name": function.get("name") or call.get("name") or "", "arguments": function.get("arguments") or call.get("arguments") or "{}"})
            continue
        if message.get("role") == "tool":
            responses_input.append({"type": "function_call_output", "call_id": message.get("tool_call_id") or "", "output": message.get("content") or ""})
            continue
        responses_input.append(responses_message(message))
    payload: dict = {
        "model": request.model,
        "input": responses_input,
        "stream": True,
        # Visual Data URLs are request-scoped pixels. Avoid asking the
        # Responses service to retain a copy unless a provider explicitly
        # chooses a different adapter in the future.
        "store": False,
    }
    response_text = request.response_format
    # The Responses API nests structured-output settings under `text.format`,
    # while Chat Completions accepts the format object directly. Accept both
    # internal forms and emit the official Responses shape by default.
    if isinstance(response_text, dict) and "format" not in response_text:
        response_text = {"format": response_text}
    optional = {
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_output_tokens": request.max_tokens,
        "reasoning": {"effort": request.reasoning_effort} if request.reasoning_effort else None,
        "text": response_text,
        "tool_choice": request.tool_choice,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    if request.tools:
        response_tools: list[dict] = []
        for item in request.tools:
            function = item.get("function") if isinstance(item, dict) else None
            function = function if isinstance(function, dict) else item
            if not isinstance(function, dict) or not function.get("name"):
                continue
            tool = {"type": "function", "name": function["name"]}
            for key in ("description", "parameters", "strict"):
                if function.get(key) is not None:
                    tool[key] = function[key]
            response_tools.append(tool)
        if response_tools:
            payload["tools"] = response_tools
    if request.extra:
        for key, value in request.extra.items():
            if key not in {"model", "messages", "input", "stream", "store", "tools", "tool_choice", "reasoning", "reasoning_effort"} and value is not None:
                payload[key] = value
    return payload


def _text_from_content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return ""


def _merge_tool_calls(target: dict[int, ToolCall], values: Sequence[dict]) -> None:
    for offset, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        try:
            index = int(value.get("index", offset))
        except (TypeError, ValueError):
            index = offset
        item = target.setdefault(index, ToolCall(index=index))
        if value.get("id"):
            item.id = str(value["id"])
        function = value.get("function") or {}
        if not isinstance(function, dict):
            function = {}
        if value.get("name"):
            item.name = _merge_fragment(item.name, value["name"])
        if function.get("name"):
            item.name = _merge_fragment(item.name, function["name"])
        if value.get("arguments"):
            item.arguments = _merge_fragment(item.arguments, value["arguments"])
        if function.get("arguments"):
            item.arguments = _merge_fragment(item.arguments, function["arguments"])


def _consume_chat_item(item: dict, tool_acc: dict[int, ToolCall], result: TextResult) -> str:
    # Some gateways stream an error object with HTTP 200 instead of returning
    # a non-2xx response. Surface it so the workspace records a failed request
    # rather than a misleading empty completion.
    if isinstance(item, dict) and item.get("error") is not None:
        error = item.get("error")
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ValueError(str(message or "chat completion failed")[:500])
    if item.get("id") and not result.provider_request_id:
        result.provider_request_id = str(item["id"])
    if item.get("usage"):
        result.usage = item["usage"]
    choices = item.get("choices") or []
    if not choices:
        message = item.get("message") or {}
        if message.get("tool_calls"):
            _merge_tool_calls(tool_acc, message["tool_calls"])
        return _text_from_content(message.get("content"))
    choice = choices[0] or {}
    if choice.get("finish_reason"):
        result.finish_reason = choice["finish_reason"]
    delta = choice.get("delta") or {}
    if delta.get("tool_calls"):
        _merge_tool_calls(tool_acc, delta["tool_calls"])
    if delta.get("function_call"):
        _merge_tool_calls(tool_acc, [{"index": 0, "function": delta["function_call"]}])
    message = choice.get("message") or {}
    if message.get("tool_calls"):
        _merge_tool_calls(tool_acc, message["tool_calls"])
    if message.get("function_call"):
        _merge_tool_calls(tool_acc, [{"index": 0, "function": message["function_call"]}])
    return _text_from_content(delta.get("content", message.get("content")))


def openai_responses(
    channel,
    model: str,
    messages: list[dict],
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    response_format: dict | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    extra: dict | None = None,
) -> TextResult:
    request = ProviderTextRequest(model, messages, temperature, top_p, max_tokens, reasoning_effort, response_format, tools, tool_choice, extra)
    result = TextResult(chunks=iter(()))
    tool_acc: dict[str, ToolCall] = {}
    # Responses streams identify a function call by different fields at
    # different stages (`item_id` on argument deltas, `call_id` on the final
    # output item). Keep aliases so one logical call is never executed twice.
    tool_aliases: dict[str, str] = {}
    timeout = httpx.Timeout(get_settings().model_timeout_seconds, connect=10.0)
    headers = {"Authorization": f"Bearer {channel_key(channel)}", "Content-Type": "application/json", "Accept": "text/event-stream"}

    def response_error(item: dict) -> None:
        """Raise a normalized error for gateways that report failures in a 200 SSE.

        A number of OpenAI-compatible gateways emit an `error` data frame
        after sending `response.created`, while keeping the HTTP status 200.
        Treating that frame as an empty successful completion leaves requests
        marked completed and hides the provider failure from the caller.
        """
        error = item.get("error")
        nested = item.get("response") if isinstance(item.get("response"), dict) else None
        if error is None and nested is not None:
            error = nested.get("error")
        status_value = str((nested or item).get("status") or "").lower()
        if error is None and status_value in {"failed", "cancelled", "canceled"}:
            error = (nested or item).get("incomplete_details") or status_value
        if error is None:
            return
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or "responses request failed"
        else:
            message = str(error)
        raise ValueError(str(message)[:500])

    def response_id(item: dict) -> None:
        if result.provider_request_id:
            return
        candidates = [item.get("id"), item.get("response_id")]
        nested = item.get("response")
        if isinstance(nested, dict):
            candidates.append(nested.get("id"))
        for candidate in candidates:
            if candidate:
                result.provider_request_id = str(candidate)
                return

    def get_tool_call(*candidates: dict) -> ToolCall:
        """Get/merge a ToolCall using item_id, call_id and id aliases."""
        aliases = [
            str(candidate.get(field))
            for candidate in candidates
            for field in ("item_id", "call_id", "id")
            if isinstance(candidate, dict) and candidate.get(field)
        ]
        canonical = next((tool_aliases[value] for value in aliases if value in tool_aliases), None)
        if canonical is None:
            canonical = next((value for value in aliases if value), f"call-{len(tool_acc)}")
        # If a later event supplies a new call_id, merge any object that was
        # created under the old item_id into the new canonical key.
        for alias in aliases:
            previous = tool_aliases.get(alias)
            if previous and previous != canonical and previous in tool_acc:
                old = tool_acc.pop(previous)
                current = tool_acc.setdefault(canonical, ToolCall(index=old.index, id=old.id or canonical))
                if old.name and not current.name:
                    current.name = old.name
                if old.arguments and not current.arguments:
                    current.arguments = old.arguments
                elif old.arguments and current.arguments:
                    current.arguments = _merge_fragment(old.arguments, current.arguments)
        for alias in aliases:
            tool_aliases[alias] = canonical
        call = tool_acc.setdefault(canonical, ToolCall(index=len(tool_acc), id=canonical))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("call_id"):
                call.id = str(candidate["call_id"])
            elif candidate.get("id") and not call.id:
                call.id = str(candidate["id"])
            function = candidate.get("function") if isinstance(candidate.get("function"), dict) else candidate
            if isinstance(function, dict) and function.get("name"):
                value = str(function["name"])
                call.name = _merge_fragment(call.name, value)
        return call

    def consume_response_item(item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        response_error(item)
        response_id(item)
        usage = item.get("usage")
        response_body = item.get("response") if isinstance(item.get("response"), dict) else None
        if response_body is None and isinstance(item.get("output"), list):
            response_body = item
        if response_body and response_body.get("usage"):
            usage = response_body["usage"]
        if usage:
            result.usage = usage
        event_type = str(item.get("type") or "")
        if event_type == "response.output_text.delta":
            return str(item.get("delta") or "")
        if isinstance(item.get("output_text"), str):
            return str(item["output_text"])
        if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            call = get_tool_call(item)
            if item.get("arguments") is not None:
                value = str(item.get("arguments") or "")
                call.arguments = _merge_fragment(call.arguments, value)
            elif item.get("delta"):
                call.arguments = _merge_fragment(call.arguments, item.get("delta"))
            return ""
        candidate = item.get("item") or item.get("output_item") or item
        if isinstance(candidate, dict) and candidate.get("type") == "function_call":
            call = get_tool_call(candidate)
            if candidate.get("arguments"):
                # `arguments` is complete on output_item.done; deltas are
                # already accumulated, so avoid duplicating the same value.
                value = str(candidate["arguments"])
                call.arguments = _merge_fragment(call.arguments, value)
            return ""
        if response_body and isinstance(response_body.get("output"), list):
            text_parts: list[str] = []
            for output in response_body["output"]:
                if not isinstance(output, dict):
                    continue
                if output.get("type") == "function_call":
                    call = get_tool_call(output)
                    if output.get("name"):
                        call.name = _merge_fragment(call.name, output["name"])
                    if output.get("arguments"):
                        call.arguments = _merge_fragment(call.arguments, output["arguments"])
                for part in output.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        text_parts.append(str(part.get("text") or ""))
            if event_type in {"response.completed", "response.done"}:
                result.finish_reason = "tool_calls" if tool_acc else "stop"
            return "".join(text_parts)
        if event_type in {"response.completed", "response.done"}:
            result.raw_response = response_body or item
            result.finish_reason = "tool_calls" if tool_acc else "stop"
        # Some gateways return a Chat Completions-shaped item even on this
        # endpoint; retain the generic parser as a compatibility fallback.
        return _consume_chat_item(item, {call.index: call for call in tool_acc.values()}, result)

    def chunks() -> Iterator[str]:
        client = httpx.Client(timeout=timeout)
        try:
            with client.stream("POST", provider_url(channel.base_url, "responses"), headers=headers, json=build_responses_payload(request)) as response:
                response.raise_for_status()
                result.provider_request_id = result.provider_request_id or response.headers.get("x-request-id") or response.headers.get("request-id")
                content_type = response.headers.get("content-type", "")
                if "json" in content_type and "event-stream" not in content_type:
                    body = response.json()
                    result.raw_response = body if isinstance(body, dict) else None
                    if isinstance(body, dict):
                        text = consume_response_item(body)
                        if text:
                            yield text
                else:
                    event_name = ""
                    for line in response.iter_lines():
                        if not line:
                            continue
                        if isinstance(line, bytes):
                            line = line.decode("utf-8", errors="replace")
                        line = str(line)
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            item = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(item, dict):
                            if event_name and not item.get("type"):
                                item["type"] = event_name
                            text = consume_response_item(item)
                            if text:
                                yield text
                        event_name = ""
                result.tool_calls = sorted(tool_acc.values(), key=lambda item: item.index)
        finally:
            client.close()

    result.chunks = chunks()
    return result


def openai_text(
    channel,
    model: str,
    messages: list[dict],
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    response_format: dict | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    extra: dict | None = None,
) -> TextResult:
    request = ProviderTextRequest(model, messages, temperature, top_p, max_tokens, reasoning_effort, response_format, tools, tool_choice, extra)
    if str((getattr(channel, "capabilities_json", None) or {}).get("_text_endpoint", "")).lower() == "responses":
        return openai_responses(channel, model, messages, temperature=temperature, top_p=top_p, max_tokens=max_tokens, reasoning_effort=reasoning_effort, response_format=response_format, tools=tools, tool_choice=tool_choice, extra=extra)
    url = provider_url(channel.base_url, "chat/completions")
    headers = {"Authorization": f"Bearer {channel_key(channel)}", "Content-Type": "application/json", "Accept": "text/event-stream"}
    result = TextResult(chunks=iter(()))
    tool_acc: dict[int, ToolCall] = {}
    timeout = httpx.Timeout(get_settings().model_timeout_seconds, connect=10.0)

    def chunks() -> Iterator[str]:
        client = httpx.Client(timeout=timeout)
        try:
            with client.stream("POST", url, headers=headers, json=build_chat_payload(request)) as response:
                response.raise_for_status()
                result.provider_request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                content_type = response.headers.get("content-type", "")
                if "json" in content_type and "event-stream" not in content_type:
                    body = response.json()
                    result.raw_response = body if isinstance(body, dict) else None
                    text = _consume_chat_item(body if isinstance(body, dict) else {}, tool_acc, result)
                    if text:
                        yield text
                else:
                    for line in response.iter_lines():
                        if not line:
                            continue
                        if isinstance(line, bytes):
                            line = line.decode("utf-8", errors="replace")
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            item = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(item, dict):
                            text = _consume_chat_item(item, tool_acc, result)
                            if text:
                                yield text
                result.tool_calls = [tool_acc[index] for index in sorted(tool_acc)]
        finally:
            client.close()

    result.chunks = chunks()
    return result


def _max_image_bytes() -> int:
    return max(1, int(get_settings().model_max_image_bytes))


def _decode_image_data(item: dict) -> tuple[bytes, str] | None:
    if not isinstance(item, dict):
        return None
    encoded = item.get("b64_json") or item.get("base64") or item.get("data") or item.get("result") or item.get("image")
    if isinstance(encoded, dict):
        nested = _decode_image_data(encoded)
        if nested:
            return nested
        encoded = encoded.get("url")
    if not encoded:
        return None
    if isinstance(encoded, str) and encoded.startswith("data:"):
        header, _, encoded = encoded.partition(",")
        mime = header[5:].split(";", 1)[0] or "image/png"
    else:
        mime = str(item.get("mime_type") or item.get("content_type") or "image/png")
    encoded_text = "".join(str(encoded).split())
    # Reject oversized Base64 before allocating its decoded byte buffer.
    if len(encoded_text) > ((_max_image_bytes() + 2) // 3) * 4 + 4:
        raise ValueError("image response too large")
    try:
        content = base64.b64decode(encoded_text, validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None
    if len(content) > _max_image_bytes():
        raise ValueError("image response too large")
    if not mime.lower().startswith("image/"):
        raise ValueError("image response has invalid MIME type")
    return content, mime


def _allowed_image_hosts(channel) -> set[str]:
    capabilities = getattr(channel, "capabilities_json", None)
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    configured = capabilities.get("image_url_hosts") or []
    if isinstance(configured, str):
        configured = [configured]
    hosts = {str(item).strip().lower().rstrip(".") for item in configured if str(item).strip()}
    try:
        base_host = (urlsplit(str(channel.base_url)).hostname or "").lower().rstrip(".")
        if base_host:
            hosts.add(base_host)
    except ValueError:
        pass
    return hosts


def _validate_download_target(url: str, channel=None) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("unsupported image URL")
    if parsed.username or parsed.password:
        raise ValueError("image URL credentials are not allowed")
    host = parsed.hostname.lower().rstrip(".")
    allowed_hosts = _allowed_image_hosts(channel) if channel is not None else set()
    if allowed_hosts and host not in allowed_hosts:
        raise ValueError("image URL host is not allowed")
    if parsed.scheme == "http":
        base_scheme = urlsplit(str(getattr(channel, "base_url", "https://"))).scheme if channel is not None else ""
        capabilities = getattr(channel, "capabilities_json", None)
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if base_scheme != "http" and not capabilities.get("allow_http_image_urls"):
            raise ValueError("insecure image URL")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("image URL host cannot be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        capabilities = getattr(channel, "capabilities_json", None)
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified) and not capabilities.get("allow_private_image_urls"):
            raise ValueError("image URL resolves to a non-public address")


def _download_image(url: str, channel=None) -> tuple[bytes, str]:
    # A few gateways return a path such as `/files/result.png` instead of an
    # absolute URL. Resolve only relative paths against the configured channel
    # root; absolute URLs still go through the host/DNS checks below.
    if channel is not None and not url.startswith(("data:", "http://", "https://")):
        url = urljoin(f"{api_base_url(channel.base_url)}/", url)
    if url.startswith("data:"):
        header, _, encoded = url.partition(",")
        if ";base64" not in header:
            raise ValueError("unsupported image data URL")
        mime = header[5:].split(";", 1)[0] or "image/png"
        encoded_text = "".join(str(encoded).split())
        if len(encoded_text) > ((_max_image_bytes() + 2) // 3) * 4 + 4:
            raise ValueError("image response too large")
        try:
            content = base64.b64decode(encoded_text, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ValueError("invalid image data URL") from exc
        if len(content) > _max_image_bytes():
            raise ValueError("image response too large")
        if not mime.lower().startswith("image/"):
            raise ValueError("image response has invalid MIME type")
        return content, mime
    _validate_download_target(url, channel)
    max_bytes = _max_image_bytes()
    with httpx.stream("GET", url, follow_redirects=False, timeout=get_settings().model_timeout_seconds) as response:
        response.raise_for_status()
        mime = response.headers.get("content-type", "image/png").split(";", 1)[0]
        if not mime.lower().startswith("image/"):
            raise ValueError("image response has invalid MIME type")
        content_length = response.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > max_bytes:
            raise ValueError("image response too large")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("image response too large")
            chunks.append(chunk)
    return b"".join(chunks), mime


def _parse_image_response(response: httpx.Response, channel=None) -> ImageResult:
    headers = getattr(response, "headers", {}) or {}
    content_length = headers.get("content-length")
    # JSON/base64 responses should stay bounded before parsing the body.
    max_json_bytes = _max_image_bytes() * 2 + 1024 * 1024
    if content_length and content_length.isdigit() and int(content_length) > max_json_bytes:
        raise ValueError("image response too large")
    # `httpx.post` buffers normal JSON responses, so checking the already
    # received bytes also covers providers that omit Content-Length. Test
    # doubles and alternative response objects may not expose `.content`; in
    # that case fall back to their JSON decoder.
    try:
        raw_content = response.content
    except Exception:
        raw_content = None
    if isinstance(raw_content, (bytes, bytearray)) and len(raw_content) > max_json_bytes:
        raise ValueError("image response too large")
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("image provider returned invalid JSON")
    if body.get("error") is not None:
        error = body.get("error")
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ValueError(str(message or "image provider request failed")[:500])
    entries = body.get("data") or body.get("images") or body.get("output") or []
    if isinstance(entries, dict):
        entries = [entries]
    if not entries and any(key in body for key in ("b64_json", "url", "image_url")):
        entries = [body]
    provider_request_id = headers.get("x-request-id") or headers.get("request-id") or (str(body.get("id")) if body.get("id") else None)
    for item in entries:
        if isinstance(item, str):
            content, mime = _download_image(item, channel)
            return ImageResult(content, mime, provider_request_id, body.get("usage"))
        decoded = _decode_image_data(item)
        if decoded:
            return ImageResult(decoded[0], decoded[1], provider_request_id, body.get("usage"))
        if isinstance(item, dict):
            image_url = item.get("url") or item.get("image_url") or item.get("image") or item.get("result")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if image_url:
                content, mime = _download_image(str(image_url), channel)
                return ImageResult(content, mime, provider_request_id, body.get("usage"))
    raise ValueError("image provider returned no image")


def image_request(
    channel,
    model: str,
    prompt: str,
    size: str = "1024x1024",
    *,
    quality: str | None = None,
    n: int = 1,
    response_format: str = "b64_json",
    reference_images: Sequence[tuple[str, bytes, str]] | None = None,
    mask_image: tuple[str, bytes, str] | None = None,
) -> ImageResult:
    refs = list(reference_images or [])
    common = {"model": model, "prompt": prompt, "n": n, "size": size, "response_format": response_format}
    if quality:
        common["quality"] = quality
    timeout = httpx.Timeout(get_settings().model_timeout_seconds, connect=10.0)
    auth = {"Authorization": f"Bearer {channel_key(channel)}"}
    if refs or mask_image:
        url = provider_url(channel.base_url, "images/edits")
        transport = (channel.capabilities_json or {}).get("image_edit_transport")
        if transport == "json":
            encoded = [{"filename": name, "mime_type": mime, "data": base64.b64encode(content).decode("ascii")} for name, content, mime in refs]
            payload = {**common, "image": encoded}
            if mask_image:
                payload["mask"] = {"filename": mask_image[0], "mime_type": mask_image[2], "data": base64.b64encode(mask_image[1]).decode("ascii")}
            response = httpx.post(url, headers={**auth, "Content-Type": "application/json"}, json=payload, timeout=timeout)
        else:
            fields = {key: str(value) for key, value in common.items() if value is not None}
            files: list[tuple[str, tuple[str, bytes, str]]] = [("image", (name, content, mime)) for name, content, mime in refs]
            if mask_image:
                files.append(("mask", (mask_image[0], mask_image[1], mask_image[2])))
            response = httpx.post(url, headers=auth, data=fields, files=files, timeout=timeout)
    else:
        url = provider_url(channel.base_url, "images/generations")
        response = httpx.post(url, headers={**auth, "Content-Type": "application/json"}, json=common, timeout=timeout)
    response.raise_for_status()
    return _parse_image_response(response, channel)


def list_remote_models(channel) -> list[dict]:
    response = httpx.get(provider_url(channel.base_url, "models"), headers={"Authorization": f"Bearer {channel_key(channel)}"}, timeout=8.0)
    response.raise_for_status()
    body = response.json()
    values = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(values, dict):
        values = [values]
    result: list[dict] = []
    for value in values or []:
        if isinstance(value, str):
            result.append({"id": value})
        elif isinstance(value, dict) and value.get("id"):
            result.append(value)
    return result


def now_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
