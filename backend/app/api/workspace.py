import base64
import binascii
import json
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DbSession, asset_user, current_user
from app.core.config import get_settings
from app.core.time import beijing_isoformat
from app.db.session import SessionLocal
from app.models import Asset, Entitlement, Export, Message, ModelChannel, ModelRequest, Project, Thread, User
from app.schemas.common import EntitlementResponse, GenerateImageToolArguments, ImageEditRequest, ImageGenerationRequest, MessageResponse, ModelOption, ProjectCreate, ProjectResponse, ProjectUpdate, RegenerateRequest, SendMessageRequest, ThreadCreate, ThreadResponse, ThreadUpdate
from app.services.access import active_entitlement, require_entitlement
from app.services.image_options import resolve_image_options
from app.services.models import available_model_options, available_models, choose_channel, model_capabilities
from app.services.platform_tools import PlatformToolStreamParser, with_platform_tool_prompt
from app.services.providers import ToolCall, image_request, now_ms, openai_text
from app.services.response_content import SearchDirectiveStreamParser, normalize_assistant_content

router = APIRouter(tags=["workspace"])
CurrentUser = Annotated[User, Depends(current_user)]
AssetUser = Annotated[User, Depends(asset_user)]
active_streams: dict[str, bool] = {}
idempotency_lock = threading.RLock()


@dataclass
class StreamState:
    request_id: str
    thread_id: str
    user_id: str
    assistant_message_id: str
    events: list[tuple[int, str]] = field(default_factory=list)
    done: bool = False
    created_at: float = field(default_factory=time.monotonic)
    lock: threading.RLock = field(default_factory=threading.RLock)


class ImageGenerationStopped(Exception):
    """Internal control flow used when an image result arrives after stop."""


# Event history is deliberately short-lived and bounded. The database remains
# the source of truth; this cache only lets a browser replay a dropped stream.
stream_states: dict[str, StreamState] = {}
idempotency_requests: dict[str, tuple[str, float]] = {}


def _cleanup_stream_states() -> None:
    cutoff = time.monotonic() - 15 * 60
    for request_id, state in list(stream_states.items()):
        if state.created_at < cutoff:
            stream_states.pop(request_id, None)
    for key, (_, created_at) in list(idempotency_requests.items()):
        if created_at < cutoff:
            idempotency_requests.pop(key, None)


def _remember_event(state: StreamState, event_id: int, frame: str) -> str:
    with state.lock:
        state.events.append((event_id, frame))
        if len(state.events) > 1000:
            del state.events[:-1000]
    return frame


def _replay_events(state: StreamState, last_event_id: int):
    cursor = max(0, last_event_id)
    while True:
        with state.lock:
            pending = [(event_id, frame) for event_id, frame in state.events if event_id > cursor]
            done = state.done
        for event_id, frame in pending:
            cursor = event_id
            yield frame
        if done:
            return
        # A sync generator keeps the implementation compatible with both the
        # local TestClient and Uvicorn's threadpool response handling.
        time.sleep(0.05)


def _permission(db, user: User):
    try:
        return require_entitlement(db, user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _project_response(project: Project, count: int = 0) -> ProjectResponse:
    return ProjectResponse(id=project.id, name=project.name, description=project.description, archived_at=project.archived_at, created_at=project.created_at, updated_at=project.updated_at, thread_count=count)


_RESPONSE_SOURCE_REQUEST_KEY = "response_source_request_id"


def _message_uses_official_channel(db, message: Message) -> bool:
    if db is None or not message.id:
        return False

    # Regeneration reuses the assistant row, so a message may be associated
    # with requests from several channel types. New writes atomically bind the
    # current content to its root request; this avoids an older official
    # request causing a later Codex response to be cleaned on reload/export.
    metadata = message.content_json if isinstance(message.content_json, dict) else {}
    source_request_id = metadata.get(_RESPONSE_SOURCE_REQUEST_KEY)
    if isinstance(source_request_id, str) and source_request_id:
        channel_type = db.scalar(
            select(ModelChannel.channel_type)
            .join(ModelRequest, ModelRequest.channel_id == ModelChannel.id)
            .where(
                ModelRequest.id == source_request_id,
                ModelRequest.message_id == message.id,
                ModelRequest.parent_request_id.is_(None),
            )
            .limit(1)
        )
        return channel_type == "official"

    # Legacy messages predate explicit source attribution. Their latest root
    # request is the best available description of the persisted content.
    channel_type = db.scalar(
        select(ModelChannel.channel_type)
        .select_from(ModelRequest)
        .join(ModelChannel, ModelChannel.id == ModelRequest.channel_id)
        .where(ModelRequest.message_id == message.id, ModelRequest.parent_request_id.is_(None))
        .order_by(ModelRequest.created_at.desc(), ModelRequest.id.desc())
        .limit(1)
    )
    return channel_type == "official"


def _visible_message_content(message: Message, db=None) -> str:
    if message.role == "assistant" and message.content_type == "text" and _message_uses_official_channel(db, message):
        return normalize_assistant_content(message.content or "")[0]
    return message.content


def _message_response(message: Message, db=None) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        role=message.role,
        content=_visible_message_content(message, db),
        content_type=message.content_type,
        sequence=message.sequence,
        created_at=message.created_at,
        asset_ids=[str(value) for value in (message.asset_ids_json or []) if str(value)],
    )


def _thread_response(thread: Thread, db=None, include_messages: bool = True) -> ThreadResponse:
    visible = [item for item in thread.messages if item.role in {"user", "assistant"} and item.content_type != "tool_call"]
    return ThreadResponse(id=thread.id, project_id=thread.project_id, title=thread.title, model=thread.model, status=thread.status, archived_at=thread.archived_at, created_at=thread.created_at, updated_at=thread.updated_at, messages=[_message_response(item, db) for item in visible] if include_messages else [])


def _usage_value(usage: dict | None, primary: str, alternate: str) -> int | None:
    if not usage:
        return None
    value = usage.get(primary, usage.get(alternate))
    return int(value) if isinstance(value, (int, float)) else None


def _claim_image_completion(db, request_id: str, parent_request_id: str | None = None) -> ModelRequest:
    """Lock a running image request before persisting its provider result.

    The provider call is synchronous, so closing the browser connection does
    not necessarily interrupt the upstream HTTP exchange.  The durable status
    check below makes stop and completion atomic: a stopped request discards
    late bytes, while a completion that already owns the row wins before the
    stop endpoint can report success.
    """

    request = db.scalar(
        select(ModelRequest)
        .where(ModelRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    parent_stopped = parent_request_id is not None and not active_streams.get(parent_request_id, True)
    if (
        request is None
        or request.status != "running"
        or not active_streams.get(request_id, True)
        or parent_stopped
    ):
        if request is not None and request.status == "running":
            request.status = "stopped"
        raise ImageGenerationStopped()
    return request


def _next_sequence(db, thread_id: str) -> int:
    return (db.scalar(select(func.max(Message.sequence)).where(Message.thread_id == thread_id)) or 0) + 1


def _load_reference_assets(
    db,
    user: User,
    asset_ids: list[str] | None = None,
    mask_asset_id: str | None = None,
) -> tuple[list[tuple[str, bytes, str]], tuple[str, bytes, str] | None]:
    """Resolve user-owned image assets into provider upload tuples."""
    ids = list(dict.fromkeys(str(item).strip() for item in (asset_ids or []) if str(item).strip()))
    if len(ids) > 8:
        raise HTTPException(status_code=422, detail="too many reference assets")
    if mask_asset_id:
        mask_asset_id = str(mask_asset_id).strip()
        if mask_asset_id in ids:
            raise HTTPException(status_code=422, detail="mask asset must be different from reference image")
        ids.append(mask_asset_id)
    if not ids:
        return [], None
    rows = db.scalars(select(Asset).where(Asset.user_id == user.id, Asset.id.in_(ids))).all()
    by_id = {item.id: item for item in rows}
    missing = [item for item in ids if item not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail="reference asset not found")
    max_total = max(1, int(get_settings().model_max_reference_bytes))
    total = 0
    references: list[tuple[str, bytes, str]] = []
    mask: tuple[str, bytes, str] | None = None
    for asset_id in ids:
        asset = by_id[asset_id]
        mime = (asset.mime_type or "").split(";", 1)[0].lower()
        if not mime.startswith("image/"):
            raise HTTPException(status_code=415, detail="reference asset must be an image")
        path = Path(asset.storage_key)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=404, detail="reference asset file not found") from exc
        if not content or len(content) > max_total:
            raise HTTPException(status_code=413, detail="reference assets exceed size limit")
        total += len(content)
        if total > max_total:
            raise HTTPException(status_code=413, detail="reference assets exceed size limit")
        filename = path.name or f"{asset.id}.bin"
        item = (filename, content, mime)
        if mask_asset_id and asset_id == mask_asset_id:
            mask = item
        else:
            references.append(item)
    return references, mask


def _validate_asset_ownership(db, user: User, asset_ids: list[str] | None) -> None:
    ids = list(dict.fromkeys(str(item).strip() for item in (asset_ids or []) if str(item).strip()))
    if not ids:
        return
    if len(ids) > 8:
        raise HTTPException(status_code=422, detail="too many assets")
    found = set(db.scalars(select(Asset.id).where(Asset.user_id == user.id, Asset.id.in_(ids))).all())
    if len(found) != len(ids):
        raise HTTPException(status_code=404, detail="asset not found")


def _validate_text_media_assets(db, user: User, media_inputs: list[Any]) -> None:
    """Validate the metadata asset bound to each in-memory visual input.

    Pixels are supplied by the browser as a short-lived Data URL, while the
    asset row is retained for conversation history and thumbnail rendering.
    Checking the row's owner and MIME type here prevents a caller from binding
    an arbitrary document asset (or another user's asset) to an image payload.
    The stored bytes are intentionally not read: the Data URL has already been
    validated independently and may be a browser-compressed representation of
    the original upload (for example a WebP converted to JPEG).
    """
    ids: list[str] = []
    for item in media_inputs:
        raw_id = item.get("asset_id") if isinstance(item, dict) else getattr(item, "asset_id", "")
        asset_id = str(raw_id or "").strip()
        if asset_id and asset_id not in ids:
            ids.append(asset_id)
    if not ids:
        return
    rows = db.scalars(select(Asset).where(Asset.user_id == user.id, Asset.id.in_(ids))).all()
    by_id = {item.id: item for item in rows}
    if len(by_id) != len(ids):
        # Keep the same generic response as the regular asset ownership check;
        # do not reveal whether an unknown ID belongs to another account.
        raise HTTPException(status_code=404, detail="asset not found")
    for asset_id in ids:
        mime = str(by_id[asset_id].mime_type or "").split(";", 1)[0].strip().lower()
        if not mime.startswith("image/"):
            raise HTTPException(status_code=415, detail="visual asset must be an image")


def _provider_messages(
    db,
    thread_id: str,
    exclude_message_id: str | None = None,
    media_inputs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build OpenAI Chat Completions messages, including hidden tool turns."""
    rows = db.scalars(select(Message).where(Message.thread_id == thread_id).order_by(Message.sequence.asc())).all()
    result: list[dict[str, Any]] = []
    pending_media = list(media_inputs or [])
    last_user_index = max(
        (index for index, item in enumerate(rows) if item.role == "user" and item.id != exclude_message_id),
        default=-1,
    )
    for index, item in enumerate(rows):
        if exclude_message_id and item.id == exclude_message_id:
            continue
        if item.role == "tool":
            result.append({"role": "tool", "tool_call_id": item.tool_call_id or "", "name": item.tool_name or "generate_image", "content": item.content or "{}"})
            continue
        # Generated image rows are rendered by the client and are represented
        # to the model through the corresponding tool result instead.
        if item.content_type == "image":
            continue
        if item.content_type == "tool_call" or (item.role == "assistant" and item.content_json and isinstance(item.content_json, list)):
            message: dict[str, Any] = {"role": "assistant", "content": item.content or None, "tool_calls": item.content_json or []}
            result.append(message)
            continue
        if item.role in {"user", "assistant", "system"} and item.content:
            content = item.content
            if item.role == "user":
                asset_ids = [str(value) for value in (item.asset_ids_json or []) if str(value)]
                metadata = item.content_json if isinstance(item.content_json, dict) else {}
                mask_asset_id = str(metadata.get("mask_asset_id") or "")
                # Keep the attachment manifest in the provider-facing text even
                # when the same turn also contains pixel data.  The manifest is
                # what lets the optional `generate_image` tool refer back to a
                # user's uploaded asset after the vision model has inspected it.
                # It is never persisted as part of the rendered user message.
                if asset_ids or mask_asset_id:
                    attachment_payload: dict[str, Any] = {"asset_ids": asset_ids}
                    if mask_asset_id:
                        attachment_payload["mask_asset_id"] = mask_asset_id
                    content += (
                        "\n\n<platform_attachments>"
                        + json.dumps(attachment_payload, ensure_ascii=False, separators=(",", ":"))
                        + "</platform_attachments>"
                    )
            if item.role == "user" and index == last_user_index and pending_media:
                # Keep provider-facing image parts in memory for this request.
                # The durable message stores only asset IDs and can therefore
                # be replayed without embedding multi-megabyte Base64 strings.
                content = [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": str(media["data_url"]),
                            "detail": str(media.get("detail") or "auto"),
                        },
                    }
                    for media in pending_media
                ] + [{"type": "text", "text": content}]
            result.append({"role": item.role, "content": content})
    return result


GENERATE_IMAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Generate or edit an image. Use asset_ids when a reference image should be edited.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image prompt"},
                "model": {"type": "string", "description": "Optional enabled image model"},
                "channel_id": {"type": "string", "description": "Optional image channel"},
                "size": {"type": "string", "description": "auto or WIDTHxHEIGHT; for example 2560x1440 for 16:9 2K"},
                "quality": {"type": "string", "enum": ["auto", "low", "medium", "high"]},
                "asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "mask_asset_id": {"type": "string", "description": "Optional mask image asset"},
            },
            "required": ["prompt"],
        },
    },
}


def _select_image_channel(db, model: str | None = None, channel_id: str | None = None):
    requested = (model or "").strip()
    if channel_id and not requested:
        channel = db.get(ModelChannel, channel_id)
        selected = _first_channel_model(channel, "image")
        return selected, _select_channel(db, selected, "image", channel_id) if selected else None
    if requested:
        return requested, _select_channel(db, requested, "image", channel_id)
    available = available_models(db, "image")
    if not available:
        return "", None
    selected = next((item[0] for item in available if "image" in item[0].lower() or "dall" in item[0].lower()), available[0][0])
    return selected, _select_channel(db, selected, "image", channel_id)


def _select_channel(db, model: str, modality: str, channel_id: str | None = None):
    """Select a channel, honoring a user-selected channel when supplied."""
    if channel_id:
        channel = db.get(ModelChannel, channel_id)
        if not channel or not channel.enabled or model not in (channel.models_json or []) or modality not in model_capabilities(channel, model):
            raise HTTPException(status_code=422, detail="selected channel does not provide the requested model")
        return channel
    return choose_channel(db, model, modality)


def _first_channel_model(channel: ModelChannel | None, modality: str) -> str:
    if not channel:
        return ""
    for value in channel.models_json or []:
        if modality in model_capabilities(channel, value):
            return str(value)
    return ""


def resolve_text_channel(
    db,
    requested_model: str | None,
    thread_model: str | None,
    channel_id: str | None,
    require_vision: bool = False,
) -> tuple[str, ModelChannel]:
    """Resolve one enabled text channel without falling back to local content.

    An explicitly selected channel is a strict routing constraint. Without one,
    an explicit model must exist as configured, while a stale thread model may
    fall through to the current highest-priority real text model.
    """

    requested = str(requested_model or "").strip()
    previous = str(thread_model or "").strip()
    selected_channel_id = str(channel_id or "").strip()

    if selected_channel_id:
        channel = db.get(ModelChannel, selected_channel_id)
        model = requested or previous or _first_channel_model(channel, "text")
        if (
            channel is None
            or not channel.enabled
            or not model
            or model not in (channel.models_json or [])
            or "text" not in model_capabilities(channel, model)
        ):
            raise HTTPException(status_code=422, detail="selected channel does not provide the requested text model")
        if require_vision and not _channel_supports_vision(channel, model):
            raise HTTPException(status_code=422, detail="selected text model does not support image understanding")
        return model, channel

    candidate = requested or previous
    if candidate:
        channel = choose_channel(db, candidate, "text")
        if channel is not None and (not require_vision or _channel_supports_vision(channel, candidate)):
            return candidate, channel
        if require_vision:
            for possible in db.scalars(select(ModelChannel).where(ModelChannel.enabled.is_(True)).order_by(ModelChannel.priority.asc())).all():
                if candidate in (possible.models_json or []) and "text" in model_capabilities(possible, candidate) and _channel_supports_vision(possible, candidate):
                    return candidate, possible
            # When the model came from the thread's automatic preference (as
            # opposed to an explicit request), continue to the next enabled
            # vision-capable text model instead of making an otherwise valid
            # image upload fail just because the previous turn used a
            # text-only model.
            if not requested:
                enabled_channels = db.scalars(
                    select(ModelChannel)
                    .where(ModelChannel.enabled.is_(True))
                    .order_by(ModelChannel.priority.asc())
                ).all()
                for possible in enabled_channels:
                    for possible_model in possible.models_json or []:
                        possible_model = str(possible_model)
                        if "text" in model_capabilities(possible, possible_model) and _channel_supports_vision(possible, possible_model):
                            return possible_model, possible
        if requested:
            detail = "no enabled vision text model channel" if require_vision else f"no enabled text model channel for '{requested}'"
            raise HTTPException(status_code=503, detail=detail)

    # Walk enabled channels directly instead of resolving through
    # ``choose_channel`` a second time.  A model ID may legitimately be
    # published by more than one channel; resolving the first occurrence and
    # then retrying that same ID would otherwise hide a later vision-capable
    # channel when this is a brand-new thread with no model preference.
    enabled_channels = db.scalars(
        select(ModelChannel)
        .where(ModelChannel.enabled.is_(True))
        .order_by(ModelChannel.priority.asc(), ModelChannel.created_at.asc())
    ).all()
    saw_text_model = False
    for possible in enabled_channels:
        for possible_model in possible.models_json or []:
            possible_model = str(possible_model)
            if "text" not in model_capabilities(possible, possible_model):
                continue
            saw_text_model = True
            if require_vision and not _channel_supports_vision(possible, possible_model):
                continue
            return possible_model, possible
    if not saw_text_model:
        raise HTTPException(status_code=503, detail="no enabled text model channel")
    raise HTTPException(status_code=503, detail="no enabled vision text model channel")


def _channel_supports_vision(channel: ModelChannel, model: str) -> bool:
    """Read optional visual capability metadata while preserving old channels.

    Existing channels predate visual capability fields, so an omitted flag is
    treated as provider-default support. An explicit false at either model or
    channel level is respected and prevents accidental routing to text-only
    endpoints.
    """
    capabilities = channel.capabilities_json if isinstance(channel.capabilities_json, dict) else {}
    model_config = capabilities.get(model)
    if isinstance(model_config, dict) and "supports_input_image" in model_config:
        value = model_config.get("supports_input_image")
        return value.strip().lower() not in {"0", "false", "no", "off"} if isinstance(value, str) else bool(value)
    if "supports_input_image" in capabilities:
        value = capabilities.get("supports_input_image")
        return value.strip().lower() not in {"0", "false", "no", "off"} if isinstance(value, str) else bool(value)
    return True


def _validate_text_media_for_channel(channel: ModelChannel, model: str, media_inputs: list[Any]) -> None:
    """Apply optional per-channel visual input limits after schema validation."""
    if not media_inputs:
        return
    if not _channel_supports_vision(channel, model):
        raise HTTPException(status_code=422, detail="selected text model does not support image understanding")
    capabilities = channel.capabilities_json if isinstance(channel.capabilities_json, dict) else {}
    model_config = capabilities.get(model) if isinstance(capabilities.get(model), dict) else {}
    try:
        max_images = max(1, min(8, int(model_config.get("max_input_images", capabilities.get("max_input_images", 8)))))
    except (TypeError, ValueError):
        max_images = 8
    if len(media_inputs) > max_images:
        raise HTTPException(status_code=422, detail=f"当前文本模型最多支持 {max_images} 张图片")
    allowed = model_config.get("supported_input_image_mime_types", capabilities.get("supported_input_image_mime_types"))
    if isinstance(allowed, str):
        allowed = [allowed]
    allowed_mimes = {str(value).strip().lower() for value in allowed if str(value).strip()} if isinstance(allowed, (list, tuple, set)) else set()
    # Keep an empty/omitted policy compatible with the default TextMediaInput
    # contract.  Explicitly disabling visual input remains available through
    # supports_input_image=false; an empty MIME array should not make every
    # otherwise valid JPEG/PNG request fail unexpectedly.
    if not allowed_mimes:
        allowed_mimes = {"image/jpeg", "image/png"}
    try:
        max_bytes = max(0, int(model_config.get("input_image_max_bytes", capabilities.get("input_image_max_bytes", 0))))
    except (TypeError, ValueError):
        max_bytes = 0
    for item in media_inputs:
        if isinstance(item, dict):
            mime = str(item.get("mime_type") or "").strip().lower()
            size = int(item.get("decoded_size") or 0)
            if not size:
                encoded = str(item.get("data_url") or "").partition(",")[2]
                try:
                    size = len(base64.b64decode(encoded, validate=True)) if encoded else 0
                except (binascii.Error, ValueError):
                    size = 0
        else:
            mime = str(item.mime_type).strip().lower()
            size = int(getattr(item, "decoded_size", 0) or 0)
        if mime not in allowed_mimes:
            raise HTTPException(status_code=415, detail="当前文本模型不支持该图片格式")
        if max_bytes and size > max_bytes:
            raise HTTPException(status_code=413, detail="图片超过当前文本模型的视觉输入大小限制")


def _persist_image_asset(db, user_id: str, message_id: str, content: bytes, mime: str, kind: str = "generated-image") -> tuple[Asset, str]:
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise ValueError("image provider returned empty content")
    if len(content) > max(1, int(get_settings().model_max_image_bytes)):
        raise ValueError("image response too large")
    if not str(mime or "").lower().startswith("image/"):
        raise ValueError("image provider returned invalid MIME type")
    content = bytes(content)
    suffix = mimetypes.guess_extension(mime) or ".png"
    storage_dir = Path(get_settings().storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage = storage_dir / f"{message_id}-{os.urandom(6).hex()}{suffix}"
    storage.write_bytes(content)
    asset = Asset(user_id=user_id, message_id=message_id, kind=kind, storage_key=str(storage), mime_type=mime, size_bytes=len(content))
    db.add(asset)
    db.flush()
    return asset, f"/api/v1/assets/{asset.id}"


def _image_failure_message(exc: Exception) -> str:
    """Return a stable, user-safe explanation for an upstream image failure."""
    response = getattr(exc, "response", None)
    upstream_status = getattr(response, "status_code", None)
    if upstream_status == 403:
        return "图片模型渠道尚未开通生图权限。"
    if upstream_status == 429:
        return "图片模型渠道暂无可用图片配额，请稍后重试。"
    return "图片生成失败，请检查图片模型渠道配置。"


def _image_tool_failure_message(exc: Exception) -> str:
    """Keep tool-result errors actionable without exposing raw provider details."""
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in {403, 429}:
        return _image_failure_message(exc)
    if isinstance(exc, HTTPException):
        return str(exc.detail or "图片工具调用失败")[:500]
    if isinstance(exc, (ValidationError, ValueError, OSError)):
        return str(exc or "图片工具调用失败")[:500]
    return _image_failure_message(exc)


def _invoke_text_provider(channel, model: str, messages: list[dict], options: dict[str, Any], tools: list[dict] | None, tool_choice):
    kwargs = {
        "temperature": options.get("temperature"),
        "top_p": options.get("top_p"),
        "max_tokens": options.get("max_tokens"),
        "reasoning_effort": options.get("reasoning_effort"),
        "response_format": options.get("response_format"),
        "tools": tools,
        "tool_choice": tool_choice,
        "extra": options.get("extra"),
    }
    try:
        return openai_text(channel, model, messages, **kwargs)
    except TypeError as exc:
        # Keep simple three-argument test doubles and older custom adapters
        # compatible with the expanded provider contract.
        if "unexpected keyword" not in str(exc) and "positional argument" not in str(exc):
            raise
        return openai_text(channel, model, messages)


def _invoke_image_provider(channel, model: str, prompt: str, size: str, *, quality: str | None = None, n: int = 1, response_format: str = "b64_json", reference_images=None, mask_image=None):
    try:
        return image_request(channel, model, prompt, size, quality=quality, n=n, response_format=response_format, reference_images=reference_images, mask_image=mask_image)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc) and "positional argument" not in str(exc):
            raise
        return image_request(channel, model, prompt)


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(db: DbSession, user: CurrentUser, include_archived: bool = False):
    query = select(Project).where(Project.user_id == user.id).order_by(Project.updated_at.desc())
    if not include_archived:
        query = query.where(Project.archived_at.is_(None))
    projects = db.scalars(query).all()
    return [_project_response(project, db.scalar(select(func.count(Thread.id)).where(Thread.project_id == project.id)) or 0) for project in projects]


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: DbSession, user: CurrentUser):
    project = Project(user_id=user.id, name=payload.name.strip(), description=payload.description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return _project_response(project)


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, payload: ProjectUpdate, db: DbSession, user: CurrentUser):
    project = db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user.id))
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    values = payload.model_dump(exclude_unset=True)
    if "archived" in values:
        project.archived_at = datetime.now(timezone.utc) if values.pop("archived") else None
    for key, value in values.items():
        setattr(project, key, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(project)
    return _project_response(project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, db: DbSession, user: CurrentUser):
    project = db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user.id))
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    project.archived_at = datetime.now(timezone.utc)
    db.commit()


@router.get("/threads", response_model=list[ThreadResponse])
def list_threads(db: DbSession, user: CurrentUser, project_id: str | None = None, include_archived: bool = False):
    query = select(Thread).where(Thread.user_id == user.id).order_by(Thread.updated_at.desc())
    if project_id:
        query = query.where(Thread.project_id == project_id)
    if not include_archived:
        query = query.where(Thread.archived_at.is_(None))
    threads = db.scalars(query).all()
    return [_thread_response(thread, db) for thread in threads]


@router.post("/threads", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED)
def create_thread(payload: ThreadCreate, db: DbSession, user: CurrentUser):
    if payload.project_id and not db.scalar(select(Project).where(Project.id == payload.project_id, Project.user_id == user.id, Project.archived_at.is_(None))):
        raise HTTPException(status_code=404, detail="project not found")
    thread = Thread(user_id=user.id, project_id=payload.project_id, title=payload.title.strip() or "新聊天", model=payload.model)
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return _thread_response(thread, db)


@router.get("/threads/{thread_id}", response_model=ThreadResponse)
def get_thread(thread_id: str, db: DbSession, user: CurrentUser):
    thread = db.scalar(select(Thread).where(Thread.id == thread_id, Thread.user_id == user.id))
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    return _thread_response(thread, db)


@router.patch("/threads/{thread_id}", response_model=ThreadResponse)
def update_thread(thread_id: str, payload: ThreadUpdate, db: DbSession, user: CurrentUser):
    thread = db.scalar(select(Thread).where(Thread.id == thread_id, Thread.user_id == user.id))
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    values = payload.model_dump(exclude_unset=True)
    if "archived" in values:
        thread.archived_at = datetime.now(timezone.utc) if values.pop("archived") else None
    if "project_id" in values and values["project_id"] and not db.scalar(select(Project).where(Project.id == values["project_id"], Project.user_id == user.id, Project.archived_at.is_(None))):
        raise HTTPException(status_code=404, detail="project not found")
    for key, value in values.items():
        setattr(thread, key, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(thread)
    return _thread_response(thread, db)


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thread(thread_id: str, db: DbSession, user: CurrentUser):
    thread = db.scalar(select(Thread).where(Thread.id == thread_id, Thread.user_id == user.id))
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    db.delete(thread)
    db.commit()


@router.get("/threads/{thread_id}/messages", response_model=list[MessageResponse])
def list_messages(thread_id: str, db: DbSession, user: CurrentUser):
    if not db.scalar(select(Thread.id).where(Thread.id == thread_id, Thread.user_id == user.id)):
        raise HTTPException(status_code=404, detail="thread not found")
    rows = db.scalars(select(Message).where(Message.thread_id == thread_id, Message.user_id == user.id).order_by(Message.sequence.asc())).all()
    return [_message_response(item, db) for item in rows if item.role in {"user", "assistant"} and item.content_type != "tool_call"]


@router.get("/models", response_model=list[ModelOption])
def list_available_models(db: DbSession, _: CurrentUser, modality: str | None = Query(default=None)):
    return [ModelOption.model_validate(item) for item in available_model_options(db, modality)]


def _sse(event: str, data: dict, event_id: int):
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resume_stream(
    db,
    user: User,
    thread_id: str,
    request_id: str | None,
    last_event_id: int,
    idempotency_key: str | None,
):
    """Return a replay response when a client is reconnecting an existing request."""
    _cleanup_stream_states()
    if not request_id and idempotency_key:
        mapped = idempotency_requests.get(f"{user.id}:{thread_id}:{idempotency_key}")
        if mapped:
            request_id = mapped[0]
    if not request_id:
        return None
    existing = db.scalar(select(ModelRequest).where(ModelRequest.id == request_id, ModelRequest.thread_id == thread_id, ModelRequest.user_id == user.id))
    if not existing:
        raise HTTPException(status_code=404, detail="stream request not found")
    state = stream_states.get(request_id)
    if state:
        return StreamingResponse(_replay_events(state, last_event_id), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if existing.status == "running":
        raise HTTPException(status_code=409, detail="stream request is still running")
    persisted_events = existing.events_json if isinstance(existing.events_json, list) else []
    replayed_frames = []
    for item in persisted_events:
        if not isinstance(item, dict):
            continue
        try:
            event_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            continue
        if event_id <= last_event_id or not item.get("event"):
            continue
        replayed_frames.append(_sse(str(item["event"]), item.get("data") if isinstance(item.get("data"), dict) else {}, event_id))
    if replayed_frames:
        return StreamingResponse(iter(replayed_frames), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    assistant = db.get(Message, existing.message_id) if existing.message_id else None
    content = _visible_message_content(assistant, db) if assistant else ""
    next_id = max(last_event_id + 1, 1)
    frames: list[str] = []
    image_query = select(Message).where(Message.thread_id == thread_id, Message.user_id == user.id, Message.content_type == "image")
    if assistant:
        image_query = image_query.where(Message.sequence > assistant.sequence)
    image_rows = db.scalars(image_query.order_by(Message.sequence.asc())).all()
    for offset, image in enumerate(image_rows):
        asset = db.scalar(select(Asset).where(Asset.message_id == image.id, Asset.user_id == user.id).order_by(Asset.created_at.desc()))
        if asset:
            frames.append(_sse("image.created", {"request_id": existing.id, "message_id": image.id, "asset_id": asset.id, "url": image.content, "mime_type": asset.mime_type, "resumed": True}, next_id + offset))
    completed_id = next_id + len(frames)
    if existing.status == "failed":
        frames.append(_sse("error", {"request_id": existing.id, "message": "模型请求失败", "resumed": True}, completed_id))
    else:
        frames.append(_sse("message.completed", {"message_id": existing.message_id, "content": content, "stopped": existing.status == "stopped", "resumed": True}, completed_id))
    frames.append(_sse("request.usage", {"request_id": existing.id, "input_tokens": existing.input_tokens, "output_tokens": existing.output_tokens}, completed_id + 1))
    return StreamingResponse(iter(frames), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _resume_idempotent_text(db, user: User, thread_id: str, idempotency_key: str | None):
    if not idempotency_key:
        return None
    existing = db.scalar(
        select(ModelRequest).where(
            ModelRequest.user_id == user.id,
            ModelRequest.thread_id == thread_id,
            ModelRequest.modality == "text",
            ModelRequest.idempotency_key == idempotency_key,
            ModelRequest.parent_request_id.is_(None),
        ).order_by(ModelRequest.created_at.desc())
    )
    if not existing:
        return None
    if existing.status == "running":
        state = stream_states.get(existing.id)
        if state:
            return StreamingResponse(_replay_events(state, 0), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        raise HTTPException(status_code=409, detail="request with this idempotency key is already running")
    return _resume_stream(db, user, thread_id, existing.id, 0, idempotency_key)


def _start_text_stream(
    db,
    user: User,
    thread: Thread,
    model: str,
    channel: ModelChannel,
    assistant_message: Message,
    user_message: Message | None,
    idempotency_key: str | None = None,
    request_options: dict[str, Any] | None = None,
    enable_tools: bool = True,
    tool_choice: str | dict[str, Any] | None = None,
    restore_content: str | None = None,
    restore_content_json: dict | list | None = None,
    media_inputs: list[Any] | None = None,
):
    """Persist a model request and stream text, including bounded image tools."""
    request_media = [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        for item in (media_inputs or [])
    ]
    request = ModelRequest(
        user_id=user.id,
        thread_id=thread.id,
        message_id=assistant_message.id,
        channel_id=channel.id,
        model=model,
        modality="text",
        status="running",
        idempotency_key=idempotency_key,
        events_json=[],
    )
    db.add(request)
    try:
        db.flush()
        source_metadata = dict(assistant_message.content_json) if isinstance(assistant_message.content_json, dict) else {}
        source_metadata[_RESPONSE_SOURCE_REQUEST_KEY] = request.id
        assistant_message.content_json = source_metadata
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(ModelRequest).where(ModelRequest.user_id == user.id, ModelRequest.thread_id == thread.id, ModelRequest.modality == "text", ModelRequest.idempotency_key == idempotency_key, ModelRequest.parent_request_id.is_(None)).order_by(ModelRequest.created_at.desc())) if idempotency_key else None
        if existing:
            # A second worker may have inserted its placeholder messages just
            # before the unique request index rejected its root request.
            # Regeneration intentionally reuses the existing assistant row;
            # deleting it here would remove the winner's response. New-message
            # sends still clean up both losing placeholders.
            duplicate_ids = ((user_message.id,) if user_message else ())
            for duplicate_id in duplicate_ids:
                if duplicate_id:
                    duplicate_message = db.get(Message, duplicate_id)
                    if duplicate_message:
                        db.delete(duplicate_message)
            if restore_content is not None and user_message is None:
                duplicate_target = db.get(Message, assistant_message.id)
                if duplicate_target and existing.status in {"completed", "failed", "stopped"}:
                    duplicate_target.content = restore_content
            db.commit()
            replay = _resume_stream(db, user, thread.id, existing.id, 0, idempotency_key)
            if replay:
                return replay
        raise
    if idempotency_key:
        idempotency_requests[f"{user.id}:{thread.id}:{idempotency_key}"] = (request.id, time.monotonic())
    active_streams[request.id] = True
    state = StreamState(request_id=request.id, thread_id=thread.id, user_id=user.id, assistant_message_id=assistant_message.id)
    stream_states[request.id] = state
    started = time.perf_counter()

    def generate():
        # FastAPI closes request-scoped dependencies before a streaming body is
        # fully consumed. Use a dedicated session for all generator updates so
        # the final assistant content and request status are durable.
        stream_db = SessionLocal()
        stream_request = stream_db.get(ModelRequest, request.id)
        stream_assistant = stream_db.get(Message, assistant_message.id)
        if not stream_request or not stream_assistant:
            stream_db.close()
            return
        content = ""
        event_id = 1
        completed_emitted = False
        had_error = False
        total_usage: list[dict] = []
        provider_request_ids: list[str] = []
        tool_calls_used = 0
        current_child: ModelRequest | None = None
        max_rounds = max(0, int(get_settings().model_tool_max_rounds))
        max_calls = max(0, int(get_settings().model_tool_max_calls))
        created_data = {"message_id": stream_assistant.id, "request_id": stream_request.id, "model": model}
        if user_message:
            created_data["user_message_id"] = user_message.id

        def frame(event: str, data: dict) -> str:
            nonlocal event_id
            event_id += 1
            frame_value = _sse(event, data, event_id)
            persisted = list(stream_request.events_json or [])
            persisted.append({"id": event_id, "event": event, "data": data})
            stream_request.events_json = persisted[-1000:]
            stream_db.commit()
            return _remember_event(state, event_id, frame_value)

        def mark_stopped() -> str:
            stream_request.status = "stopped"
            stream_request.latency_ms = now_ms(started)
            stream_db.commit()
            return frame("message.completed", {"message_id": stream_assistant.id, "content": content, "stopped": True})

        def update_usage(target: ModelRequest, result) -> None:
            usage = getattr(result, "usage", None)
            if usage:
                total_usage.append(usage)
                target.usage_json = usage
                target.input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
                target.output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
            provider_id = getattr(result, "provider_request_id", None)
            if provider_id:
                target.provider_request_id = provider_id
                provider_request_ids.append(provider_id)

        try:
            created_frame = _sse("message.created", created_data, event_id)
            stream_request.events_json = [{"id": event_id, "event": "message.created", "data": created_data}]
            stream_db.commit()
            yield _remember_event(state, event_id, created_frame)
            if channel is None:  # pragma: no cover - resolve_text_channel guarantees this invariant
                raise RuntimeError("resolved text channel is required")
            else:
                # The placeholder assistant row is not sent to the provider;
                # hidden tool-call/result rows are converted back to OpenAI's
                # structured message shape by `_provider_messages`.
                provider_messages = _provider_messages(
                    stream_db,
                    thread.id,
                    exclude_message_id=stream_assistant.id,
                    media_inputs=request_media,
                )
                image_tool_enabled = enable_tools and bool(available_models(stream_db, "image")) and max_rounds > 0 and max_calls > 0
                provider_tools = [GENERATE_IMAGE_TOOL] if image_tool_enabled else None
                rounds = 0
                while True:
                    if not active_streams.get(stream_request.id, True):
                        completed_emitted = True
                        yield mark_stopped()
                        return
                    rounds += 1
                    child = ModelRequest(
                        user_id=user.id,
                        thread_id=thread.id,
                        message_id=stream_assistant.id,
                        channel_id=channel.id,
                        model=model,
                        modality="text",
                        status="running",
                        parent_request_id=stream_request.id,
                        turn_index=rounds,
                    )
                    stream_db.add(child)
                    stream_db.commit()
                    current_child = child
                    options = request_options or {}
                    request_messages = with_platform_tool_prompt(provider_messages) if image_tool_enabled else provider_messages
                    result = _invoke_text_provider(channel, model, request_messages, options, provider_tools, tool_choice)
                    platform_parser = PlatformToolStreamParser() if image_tool_enabled else None
                    search_parser = SearchDirectiveStreamParser() if channel.channel_type == "official" else None

                    def emit_visible_chunk(value: str):
                        nonlocal content
                        if not value:
                            return None
                        content += value
                        stream_assistant.content = content
                        stream_db.commit()
                        return frame("message.delta", {"message_id": stream_assistant.id, "delta": value})

                    def route_provider_text(value: str) -> list[str]:
                        routed: list[str] = []
                        visible, activities = search_parser.feed(value) if search_parser else ([value], [])
                        for activity in activities:
                            routed.append(frame("search.started", {
                                "request_id": stream_request.id,
                                "message_id": stream_assistant.id,
                                "query": activity.query,
                                "original_query": activity.original_query,
                                "mode": activity.mode,
                                "index": activity.index,
                            }))
                        for visible_chunk in visible:
                            visible_frame = emit_visible_chunk(visible_chunk)
                            if visible_frame:
                                routed.append(visible_frame)
                        return routed

                    for chunk in (getattr(result, "chunks", None) or ()):
                        if not active_streams.get(stream_request.id, True):
                            child.status = "stopped"
                            stream_db.commit()
                            completed_emitted = True
                            yield mark_stopped()
                            return
                        if not chunk:
                            continue
                        visible_chunks = platform_parser.feed(str(chunk)) if platform_parser else [str(chunk)]
                        for visible_chunk in visible_chunks:
                            yield from route_provider_text(visible_chunk)
                    platform_calls: list[dict[str, Any]] = []
                    if platform_parser:
                        tail_chunks, platform_calls = platform_parser.finish()
                        for tail_chunk in tail_chunks:
                            yield from route_provider_text(tail_chunk)
                    search_tail, search_activities = search_parser.finish() if search_parser else ([], [])
                    for activity in search_activities:
                        yield frame("search.started", {
                            "request_id": stream_request.id,
                            "message_id": stream_assistant.id,
                            "query": activity.query,
                            "original_query": activity.original_query,
                            "mode": activity.mode,
                            "index": activity.index,
                        })
                    for tail_chunk in search_tail:
                        visible_frame = emit_visible_chunk(tail_chunk)
                        if visible_frame:
                            yield visible_frame
                    update_usage(child, result)
                    child.status = "completed"
                    child.latency_ms = now_ms(started)
                    stream_db.commit()
                    calls = list(getattr(result, "tool_calls", None) or [])
                    # Native function calls remain the preferred transport. If
                    # the provider returned only prompt-level tags, convert
                    # those strict envelopes into the same internal ToolCall
                    # structure and reuse the existing validated tool loop.
                    if not calls and platform_calls:
                        calls = [
                            ToolCall(
                                index=index,
                                id=f"platform-{stream_request.id[:8]}-{rounds}-{index}",
                                name=str(item["name"]),
                                arguments=json.dumps(item["arguments"], ensure_ascii=False, separators=(",", ":")),
                            )
                            for index, item in enumerate(platform_calls)
                        ]
                    if not calls:
                        break
                    if rounds >= max_rounds or tool_calls_used >= max_calls:
                        had_error = True
                        notice = "工具调用次数已达到本次请求上限。"
                        content += f"\n\n{notice}"
                        stream_assistant.content = content
                        stream_db.commit()
                        yield frame("message.delta", {"message_id": stream_assistant.id, "delta": f"\n\n{notice}"})
                        break
                    remaining = max_calls - tool_calls_used
                    calls = calls[:remaining]
                    normalized_calls: list[dict] = []
                    for index, call in enumerate(calls):
                        call_id = str(getattr(call, "id", "") or f"call-{stream_request.id[:8]}-{rounds}-{index}")
                        call_name = str(getattr(call, "name", "") or "")
                        call_args = str(getattr(call, "arguments", "") or "{}")
                        normalized_calls.append({"id": call_id, "type": "function", "function": {"name": call_name, "arguments": call_args}})
                    call_message = Message(
                        thread_id=thread.id,
                        user_id=user.id,
                        role="assistant",
                        content="",
                        content_type="tool_call",
                        content_json=normalized_calls,
                        sequence=_next_sequence(stream_db, thread.id),
                    )
                    stream_db.add(call_message)
                    stream_db.commit()
                    for index, call in enumerate(calls):
                        call_id = normalized_calls[index]["id"]
                        call_name = normalized_calls[index]["function"]["name"]
                        raw_arguments = normalized_calls[index]["function"]["arguments"]
                        yield frame("tool.started", {"request_id": stream_request.id, "tool_call_id": call_id, "tool_name": call_name, "arguments": raw_arguments})
                        tool_payload: dict[str, Any]
                        generated_image_data: dict[str, Any] | None = None
                        image_request_row: ModelRequest | None = None
                        try:
                            if call_name != "generate_image":
                                raise ValueError("unsupported tool")
                            args = GenerateImageToolArguments.model_validate(json.loads(raw_arguments or "{}"))
                            image_options = resolve_image_options(args.prompt, args.size, args.quality)
                            image_model, image_channel = _select_image_channel(stream_db, args.model, args.channel_id)
                            if not image_channel or not image_model:
                                raise ValueError("no enabled image model channel")
                            refs, mask = _load_reference_assets(stream_db, user, args.asset_ids, args.mask_asset_id)
                            image_request_row = ModelRequest(
                                user_id=user.id,
                                thread_id=thread.id,
                                message_id=None,
                                channel_id=image_channel.id,
                                model=image_model,
                                modality="image",
                                status="running",
                                parent_request_id=stream_request.id,
                                turn_index=rounds,
                            )
                            stream_db.add(image_request_row)
                            stream_db.commit()
                            active_streams[image_request_row.id] = True
                            image_result = _invoke_image_provider(
                                image_channel,
                                image_model,
                                args.prompt,
                                image_options.size,
                                quality=image_options.quality,
                                reference_images=refs,
                                mask_image=mask,
                            )
                            if hasattr(image_result, "content"):
                                image_content = image_result.content
                                image_mime = image_result.mime_type
                                image_provider_id = image_result.provider_request_id
                                image_usage = image_result.usage
                            else:
                                image_content, image_mime, image_provider_id = image_result
                                image_usage = None
                            image_request_row = _claim_image_completion(
                                stream_db,
                                image_request_row.id,
                                stream_request.id,
                            )
                            image_request_row.provider_request_id = image_provider_id
                            image_request_row.usage_json = image_usage
                            image_request_row.input_tokens = _usage_value(image_request_row.usage_json, "prompt_tokens", "input_tokens")
                            image_request_row.output_tokens = _usage_value(image_request_row.usage_json, "completion_tokens", "output_tokens")
                            if image_request_row.usage_json:
                                total_usage.append(image_request_row.usage_json)
                            if image_request_row.provider_request_id:
                                provider_request_ids.append(image_request_row.provider_request_id)
                            image_message = Message(
                                thread_id=thread.id,
                                user_id=user.id,
                                role="assistant",
                                content="",
                                content_type="image",
                                asset_ids_json=list(args.asset_ids) + ([args.mask_asset_id] if args.mask_asset_id else []),
                                sequence=_next_sequence(stream_db, thread.id),
                            )
                            stream_db.add(image_message)
                            stream_db.flush()
                            asset, image_url = _persist_image_asset(stream_db, user.id, image_message.id, image_content, image_mime)
                            image_message.content = image_url
                            image_message.asset_ids_json = [asset.id] + list(args.asset_ids) + ([args.mask_asset_id] if args.mask_asset_id else [])
                            image_request_row.message_id = image_message.id
                            image_request_row.status = "completed"
                            image_request_row.latency_ms = now_ms(started)
                            stream_db.commit()
                            generated_image_data = {"message_id": image_message.id, "asset_id": asset.id, "url": image_url, "model": image_model, "mime_type": image_mime}
                            yield frame("image.created", {"request_id": stream_request.id, **generated_image_data})
                            tool_payload = {"ok": True, **generated_image_data}
                        except ImageGenerationStopped:
                            if image_request_row is not None:
                                image_request_row.status = "stopped"
                                image_request_row.latency_ms = now_ms(started)
                            stream_db.commit()
                            raise
                        except (ValidationError, ValueError, OSError, HTTPException) as exc:
                            if image_request_row is not None and image_request_row.status == "running":
                                image_request_row.status = "failed"
                                image_request_row.error_code = type(exc).__name__[:90]
                                image_request_row.latency_ms = now_ms(started)
                            tool_payload = {"ok": False, "error": _image_tool_failure_message(exc)}
                        except Exception as exc:  # provider-specific failures become tool results
                            if image_request_row is not None and image_request_row.status == "running":
                                image_request_row.status = "failed"
                                image_request_row.error_code = type(exc).__name__[:90]
                                image_request_row.latency_ms = now_ms(started)
                            tool_payload = {"ok": False, "error": _image_tool_failure_message(exc)}
                        finally:
                            if image_request_row is not None:
                                active_streams.pop(image_request_row.id, None)
                        tool_result = Message(
                            thread_id=thread.id,
                            user_id=user.id,
                            role="tool",
                            content=json.dumps(tool_payload, ensure_ascii=False),
                            content_type="tool",
                            content_json=tool_payload,
                            tool_call_id=call_id,
                            tool_name=call_name,
                            sequence=_next_sequence(stream_db, thread.id),
                        )
                        stream_db.add(tool_result)
                        stream_db.commit()
                        tool_calls_used += 1
                        yield frame("tool.completed", {"request_id": stream_request.id, "tool_call_id": call_id, "tool_name": call_name, **tool_payload})
                    provider_messages = _provider_messages(
                        stream_db,
                        thread.id,
                        exclude_message_id=stream_assistant.id,
                        media_inputs=request_media,
                    )
                    if rounds >= max_rounds:
                        break
            # Some compatible gateways wrap an entire Markdown response in a
            # redundant ```markdown fence. Replace the streamed draft with the
            # normalized body in the completion event and persisted message.
            if channel.channel_type == "official":
                content = normalize_assistant_content(content)[0]
            stream_assistant.content = content
            # Aggregate usage from all provider turns on the root request.
            stream_request.status = "failed" if had_error else "completed"
            stream_request.latency_ms = now_ms(started)
            stream_request.usage_json = {"turns": total_usage} if total_usage else None
            input_tokens = [value for value in (_usage_value(item, "prompt_tokens", "input_tokens") for item in total_usage) if value is not None]
            output_tokens = [value for value in (_usage_value(item, "completion_tokens", "output_tokens") for item in total_usage) if value is not None]
            stream_request.input_tokens = sum(input_tokens) if input_tokens else None
            stream_request.output_tokens = sum(output_tokens) if output_tokens else None
            stream_request.provider_request_id = provider_request_ids[-1] if provider_request_ids else None
            stream_db.commit()
            completed_emitted = True
            yield frame("message.completed", {"message_id": stream_assistant.id, "content": content, "tool_calls": tool_calls_used})
            yield frame("request.usage", {"request_id": stream_request.id, "input_tokens": stream_request.input_tokens, "output_tokens": stream_request.output_tokens, "tool_calls": tool_calls_used})
        except ImageGenerationStopped:
            stream_request.status = "stopped"
            stream_request.latency_ms = now_ms(started)
            stream_db.commit()
            completed_emitted = True
            yield frame("message.completed", {"message_id": stream_assistant.id, "content": content, "stopped": True})
        except Exception as exc:
            if current_child is not None and current_child.status == "running":
                current_child.status = "failed"
                current_child.error_code = type(exc).__name__[:90]
            stream_request.status = "failed"
            stream_request.error_code = type(exc).__name__[:90]
            stream_request.latency_ms = now_ms(started)
            if restore_content is not None:
                # Regeneration failures keep the previously confirmed answer.
                # Restore its source binding in the same commit so the page,
                # reload, copy action and every export format stay consistent.
                stream_assistant.content = restore_content
                stream_assistant.content_json = restore_content_json
            else:
                stream_assistant.content = content or "请求处理失败，请检查模型渠道配置后重试。"
            stream_db.commit()
            completed_emitted = True
            yield frame("error", {"request_id": stream_request.id, "message": "模型请求失败"})
        finally:
            # If the HTTP client disappears before the provider finishes, mark
            # the request stopped and leave a replayable completion event.
            if not completed_emitted and stream_request.status == "running":
                stream_request.status = "stopped"
                stream_request.latency_ms = now_ms(started)
                try:
                    stream_db.commit()
                    event_id += 1
                    stopped_data = {"message_id": stream_assistant.id, "content": content, "stopped": True}
                    persisted = list(stream_request.events_json or [])
                    persisted.append({"id": event_id, "event": "message.completed", "data": stopped_data})
                    stream_request.events_json = persisted[-1000:]
                    stream_db.commit()
                    _remember_event(state, event_id, _sse("message.completed", stopped_data, event_id))
                except Exception:
                    stream_db.rollback()
            active_streams.pop(stream_request.id, None)
            with state.lock:
                state.done = True
            stream_db.close()

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/threads/{thread_id}/messages/stream")
def stream_message(
    thread_id: str,
    payload: SendMessageRequest,
    db: DbSession,
    user: CurrentUser,
    request_id: str | None = Query(default=None, min_length=20, max_length=80),
    last_event_id: int = Header(default=0, alias="Last-Event-ID", ge=0),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=16, max_length=120),
):
    _permission(db, user)

    # A reconnect supplies the request id emitted by message.created. Replay
    # only events belonging to the same user and thread, so ids cannot be used
    # to read another user's stream.
    replay = _resume_stream(db, user, thread_id, request_id, last_event_id, idempotency_key)
    if replay:
        return replay

    thread = db.scalar(select(Thread).where(Thread.id == thread_id, Thread.user_id == user.id, Thread.archived_at.is_(None)))
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    if payload.modality != "text":
        raise HTTPException(status_code=422, detail="use image-generations for image requests")
    _validate_asset_ownership(db, user, payload.asset_ids)
    _validate_text_media_assets(db, user, payload.media_inputs)
    media_inputs = [item.model_dump(mode="json") for item in payload.media_inputs]
    for item in media_inputs:
        if item.get("asset_id") not in payload.asset_ids:
            raise HTTPException(status_code=422, detail="visual asset must be attached to the message")
    if payload.mask_asset_id:
        if payload.mask_asset_id in payload.asset_ids:
            raise HTTPException(status_code=422, detail="mask asset must be different from reference image")
        _validate_asset_ownership(db, user, [payload.mask_asset_id])
    with idempotency_lock:
        duplicate = _resume_idempotent_text(db, user, thread_id, idempotency_key)
        if duplicate:
            return duplicate
        model, channel = resolve_text_channel(
            db,
            payload.model,
            thread.model,
            payload.channel_id,
            require_vision=bool(media_inputs),
        )
        _validate_text_media_for_channel(channel, model, payload.media_inputs)
        sequence = (db.scalar(select(func.max(Message.sequence)).where(Message.thread_id == thread.id)) or 0) + 1
        user_message = Message(
            thread_id=thread.id,
            user_id=user.id,
            role="user",
            content=payload.content,
            content_type="text",
            content_json={"mask_asset_id": payload.mask_asset_id} if payload.mask_asset_id else None,
            asset_ids_json=list(payload.asset_ids),
            sequence=sequence,
        )
        assistant_message = Message(thread_id=thread.id, user_id=user.id, role="assistant", content="", content_type="text", sequence=sequence + 1)
        thread.model = model
        if thread.title == "新聊天":
            thread.title = payload.content[:60]
        request_options = {
            "temperature": payload.temperature,
            "top_p": payload.top_p,
            "max_tokens": payload.max_tokens,
            "reasoning_effort": payload.reasoning_effort,
            "response_format": payload.response_options.get("response_format") if payload.response_options else None,
            "extra": payload.response_options,
        }
        db.add_all([user_message, assistant_message])
        thread.updated_at = datetime.now(timezone.utc)
        db.commit()
        return _start_text_stream(
            db,
            user,
            thread,
            model,
            channel,
            assistant_message,
            user_message,
            idempotency_key,
            request_options=request_options,
            enable_tools=payload.enable_tools,
            tool_choice=payload.tool_choice,
            media_inputs=media_inputs,
        )


@router.post("/threads/{thread_id}/messages/stop")
def stop_message(thread_id: str, db: DbSession, user: CurrentUser):
    requests = db.scalars(
        select(ModelRequest).where(
            ModelRequest.thread_id == thread_id,
            ModelRequest.user_id == user.id,
            ModelRequest.status == "running",
        ).order_by(ModelRequest.created_at.desc()).with_for_update()
    ).all()
    if not requests:
        return {"stopped": False}
    # A text request and its image-tool child can both be running.  Stop every
    # active row in the thread so the child cannot save a late image and the
    # parent cannot resume with another provider turn.
    for request in requests:
        active_streams[request.id] = False
        request.status = "stopped"
    db.commit()
    return {"stopped": True, "request_id": requests[0].id, "request_ids": [request.id for request in requests]}


@router.post("/threads/{thread_id}/regenerate")
def regenerate_message(
    thread_id: str,
    payload: RegenerateRequest,
    db: DbSession,
    user: CurrentUser,
    request_id: str | None = Query(default=None, min_length=20, max_length=80),
    last_event_id: int = Header(default=0, alias="Last-Event-ID", ge=0),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=16, max_length=120),
):
    """Stream a fresh completion into an existing assistant message."""
    _permission(db, user)
    replay = _resume_stream(db, user, thread_id, request_id, last_event_id, idempotency_key)
    if replay:
        return replay
    thread = db.scalar(select(Thread).where(Thread.id == thread_id, Thread.user_id == user.id, Thread.archived_at.is_(None)))
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    # A reconnect after a process restart has no in-memory idempotency map.
    # Resolve the durable root request before mutating the target assistant
    # row; otherwise a completed retry could briefly (or permanently) erase
    # the already stored answer.
    with idempotency_lock:
        durable_replay = _resume_idempotent_text(db, user, thread_id, idempotency_key)
        if durable_replay:
            return durable_replay
        return _regenerate_message_locked(
            thread_id,
            payload,
            db,
            user,
            thread,
            idempotency_key,
        )


def _regenerate_message_locked(
    thread_id: str,
    payload: RegenerateRequest,
    db,
    user: User,
    thread: Thread,
    idempotency_key: str | None,
):
    """Prepare a regenerate request while the idempotency lock is held."""
    if payload.assistant_message_id:
        target = db.scalar(select(Message).where(Message.id == payload.assistant_message_id, Message.thread_id == thread.id, Message.user_id == user.id, Message.role == "assistant", Message.content_type == "text").with_for_update())
        if not target:
            raise HTTPException(status_code=404, detail="text assistant message not found")
    else:
        target = db.scalars(select(Message).where(Message.thread_id == thread.id, Message.user_id == user.id, Message.role == "assistant", Message.content_type == "text").order_by(Message.sequence.desc()).limit(1).with_for_update()).first()
        if not target:
            raise HTTPException(status_code=422, detail="no assistant message to regenerate")
    query = select(Message).where(Message.thread_id == thread.id, Message.user_id == user.id, Message.role == "user")
    query = query.where(Message.sequence < target.sequence)
    prompt = db.scalars(query.order_by(Message.sequence.desc()).limit(1)).first()
    if not prompt:
        raise HTTPException(status_code=422, detail="no user message to regenerate")
    active_request_id = db.scalar(_active_regeneration_request_query(target.id))
    if active_request_id:
        raise HTTPException(status_code=409, detail="assistant message is already being regenerated")
    model, channel = resolve_text_channel(db, payload.model, thread.model, payload.channel_id)
    previous_content = target.content
    previous_content_json = dict(target.content_json) if isinstance(target.content_json, dict) else target.content_json
    target.content = ""
    thread.model = model
    thread.updated_at = datetime.now(timezone.utc)
    # Keep the row lock and pending content change until `_start_text_stream`
    # atomically creates the root request and binds it as the content source.
    request_options = {
        "temperature": payload.temperature,
        "top_p": payload.top_p,
        "max_tokens": payload.max_tokens,
        "reasoning_effort": payload.reasoning_effort,
        "extra": payload.response_options,
    }
    return _start_text_stream(
        db,
        user,
        thread,
        model,
        channel,
        target,
        None,
        idempotency_key,
        request_options=request_options,
        enable_tools=payload.enable_tools,
        tool_choice=payload.tool_choice,
        restore_content=previous_content,
        restore_content_json=previous_content_json,
    )


def _active_regeneration_request_query(message_id: str):
    """Build a current read for the root request guarding one assistant row."""
    return (
        select(ModelRequest.id).where(
            ModelRequest.message_id == message_id,
            ModelRequest.parent_request_id.is_(None),
            ModelRequest.status == "running",
        )
        # MySQL/InnoDB defaults to REPEATABLE READ.  This must be a locking
        # current read: a concurrent transaction may have created the running
        # request while this transaction was waiting for the assistant lock.
        .limit(1)
        .with_for_update()
    )


def _run_image_generation(thread_id: str, payload: ImageGenerationRequest, db, user: User, idempotency_key: str | None = None):
    _permission(db, user)
    thread = db.scalar(select(Thread).where(Thread.id == thread_id, Thread.user_id == user.id, Thread.archived_at.is_(None)))
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    if idempotency_key:
        previous = db.scalar(select(ModelRequest).where(ModelRequest.thread_id == thread.id, ModelRequest.user_id == user.id, ModelRequest.idempotency_key == idempotency_key, ModelRequest.modality == "image").order_by(ModelRequest.created_at.desc()))
        if previous and previous.status == "completed":
            message = db.get(Message, previous.message_id) if previous.message_id else None
            asset = db.scalar(select(Asset).where(Asset.message_id == previous.message_id, Asset.user_id == user.id).order_by(Asset.created_at.desc())) if previous.message_id else None
            if message and asset:
                return {"request_id": previous.id, "message_id": message.id, "user_message_id": None, "asset_id": asset.id, "url": message.content, "model": previous.model, "mime_type": asset.mime_type, "status": "completed", "replayed": True}
        if previous and previous.status == "running":
            raise HTTPException(status_code=409, detail="image request is already running")
        if previous and previous.status in {"failed", "stopped"}:
            raise HTTPException(status_code=409, detail="previous image request ended; use a new idempotency key")
    model, channel = _select_image_channel(db, payload.model, payload.channel_id)
    if not channel or not model:
        detail = f"no enabled image model channel for '{payload.model}'" if payload.model else "no enabled image model channel"
        raise HTTPException(status_code=503, detail=detail)
    references, mask = _load_reference_assets(db, user, payload.asset_ids, payload.mask_asset_id)
    image_options = resolve_image_options(payload.prompt, payload.size, payload.quality)
    sequence = _next_sequence(db, thread.id)
    user_message = Message(
        thread_id=thread.id,
        user_id=user.id,
        role="user",
        content=payload.prompt,
        content_type="text",
        asset_ids_json=list(payload.asset_ids) + ([payload.mask_asset_id] if payload.mask_asset_id else []),
        sequence=sequence,
    )
    assistant_message = Message(thread_id=thread.id, user_id=user.id, role="assistant", content="", content_type="image", sequence=sequence + 1)
    thread.model = model
    if thread.title == "新聊天":
        thread.title = payload.prompt[:60]
    db.add_all([user_message, assistant_message])
    thread.updated_at = datetime.now(timezone.utc)
    db.commit()
    request = ModelRequest(user_id=user.id, thread_id=thread.id, message_id=assistant_message.id, channel_id=channel.id, model=model, modality="image", status="running", idempotency_key=idempotency_key)
    db.add(request)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(ModelRequest).where(ModelRequest.user_id == user.id, ModelRequest.thread_id == thread.id, ModelRequest.modality == "image", ModelRequest.idempotency_key == idempotency_key).order_by(ModelRequest.created_at.desc())) if idempotency_key else None
        if existing and existing.status == "completed":
            message = db.get(Message, existing.message_id) if existing.message_id else None
            asset = db.scalar(select(Asset).where(Asset.message_id == existing.message_id, Asset.user_id == user.id).order_by(Asset.created_at.desc())) if existing.message_id else None
            if message and asset:
                # The placeholder messages were committed before the request
                # row. Remove the losing pair when two workers race on the
                # same idempotency key, otherwise a retry leaves a duplicate
                # user prompt in the thread history.
                for duplicate_id in (assistant_message.id, user_message.id):
                    duplicate = db.get(Message, duplicate_id)
                    if duplicate:
                        db.delete(duplicate)
                db.commit()
                return {"request_id": existing.id, "message_id": message.id, "user_message_id": None, "asset_id": asset.id, "url": message.content, "model": existing.model, "mime_type": asset.mime_type, "status": "completed", "replayed": True}
        if existing:
            for duplicate_id in (assistant_message.id, user_message.id):
                duplicate = db.get(Message, duplicate_id)
                if duplicate:
                    db.delete(duplicate)
            db.commit()
        raise HTTPException(status_code=409, detail="request with this idempotency key is already running") from None
    active_streams[request.id] = True
    started = time.perf_counter()
    try:
        result = _invoke_image_provider(
            channel,
            model,
            payload.prompt,
            image_options.size,
            quality=image_options.quality,
            n=payload.n,
            response_format=payload.response_format,
            reference_images=references,
            mask_image=mask,
        )
        if hasattr(result, "content"):
            content = result.content
            mime = result.mime_type
            provider_id = result.provider_request_id
            usage = result.usage
        else:  # compatibility with simple tuple-returning test doubles
            content, mime, provider_id = result
            usage = None
        request = _claim_image_completion(db, request.id)
        asset, image_url = _persist_image_asset(db, user.id, assistant_message.id, content, mime)
        assistant_message.content = image_url
        assistant_message.asset_ids_json = [asset.id]
        request.status = "completed"
        request.provider_request_id = provider_id
        request.usage_json = usage
        request.input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
        request.output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
        request.latency_ms = now_ms(started)
        db.commit()
        return {"request_id": request.id, "message_id": assistant_message.id, "user_message_id": user_message.id, "asset_id": asset.id, "url": image_url, "model": model, "mime_type": mime, "status": "completed"}
    except ImageGenerationStopped as exc:
        request.status = "stopped"
        request.latency_ms = now_ms(started)
        assistant_message.content = "图片生成已停止。"
        assistant_message.content_type = "text"
        db.commit()
        raise HTTPException(status_code=409, detail="image generation stopped") from exc
    except HTTPException as exc:
        request.status = "failed"
        request.error_code = type(exc).__name__[:90]
        request.latency_ms = now_ms(started)
        assistant_message.content = _image_failure_message(exc)
        # A failed image call has no Asset. Persist it as text so history
        # reloads render the failure message instead of treating it as a URL.
        assistant_message.content_type = "text"
        db.commit()
        raise
    except Exception as exc:
        request.status = "failed"
        request.error_code = type(exc).__name__[:90]
        request.latency_ms = now_ms(started)
        failure_message = _image_failure_message(exc)
        assistant_message.content = failure_message
        # See the HTTPException branch above: do not leave a dangling image
        # message when the provider rejects generation.
        assistant_message.content_type = "text"
        db.commit()
        raise HTTPException(status_code=502, detail=failure_message) from exc
    finally:
        active_streams.pop(request.id, None)


@router.post("/threads/{thread_id}/image-generations")
def generate_image(thread_id: str, payload: ImageGenerationRequest, db: DbSession, user: CurrentUser, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=16, max_length=120)):
    return _run_image_generation(thread_id, payload, db, user, idempotency_key)


@router.post("/threads/{thread_id}/image-edits")
def edit_image(thread_id: str, payload: ImageEditRequest, db: DbSession, user: CurrentUser, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=16, max_length=120)):
    return _run_image_generation(thread_id, payload, db, user, idempotency_key)


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, db: DbSession, user: AssetUser):
    asset = db.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id))
    if not asset or not os.path.exists(asset.storage_key):
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(asset.storage_key, media_type=asset.mime_type, headers={"Cache-Control": "private, max-age=300", "Referrer-Policy": "no-referrer"})


@router.get("/threads/{thread_id}/export")
def export_thread(thread_id: str, db: DbSession, user: CurrentUser, format: str = Query(default="markdown", pattern="^(json|markdown|txt)$")):
    thread = db.scalar(select(Thread).where(Thread.id == thread_id, Thread.user_id == user.id))
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    messages = [item for item in db.scalars(select(Message).where(Message.thread_id == thread.id).order_by(Message.sequence)).all() if item.role in {"user", "assistant"} and item.content_type != "tool_call"]
    if format == "json":
        body = json.dumps({"thread": {"id": thread.id, "title": thread.title}, "messages": [{"role": item.role, "content": _visible_message_content(item, db), "created_at": beijing_isoformat(item.created_at)} for item in messages]}, ensure_ascii=False, indent=2)
        media = "application/json"
        suffix = "json"
    elif format == "txt":
        body = "\n\n".join(f"{item.role}:\n{_visible_message_content(item, db)}" for item in messages)
        media = "text/plain; charset=utf-8"
        suffix = "txt"
    else:
        body = f"# {thread.title}\n\n" + "\n\n".join(f"**{item.role}**\n\n{_visible_message_content(item, db)}" for item in messages)
        media = "text/markdown; charset=utf-8"
        suffix = "md"
    export = Export(user_id=user.id, thread_id=thread.id, format=format, status="completed")
    db.add(export)
    db.commit()
    db.refresh(export)
    return Response(content=body, media_type=media, headers={"Content-Disposition": f'attachment; filename="chat-{thread.id}.{suffix}"', "X-Export-ID": export.id, "Access-Control-Expose-Headers": "X-Export-ID, Content-Disposition"})


@router.get("/exports/{export_id}")
def get_export(export_id: str, db: DbSession, user: CurrentUser):
    export = db.scalar(select(Export).where(Export.id == export_id, Export.user_id == user.id))
    if not export:
        raise HTTPException(status_code=404, detail="export not found")
    return {
        "id": export.id,
        "thread_id": export.thread_id,
        "format": export.format,
        "status": export.status,
        "created_at": beijing_isoformat(export.created_at),
        "download_url": f"/api/v1/threads/{export.thread_id}/export?format={export.format}",
    }


@router.get("/me/entitlement", response_model=EntitlementResponse | None)
def my_entitlement(db: DbSession, user: CurrentUser):
    item = active_entitlement(db, user)
    if not item:
        return None
    return EntitlementResponse(id=item.id, starts_at=item.starts_at, expires_at=item.expires_at, status=item.status, active=True)


@router.post("/assets/upload")
def upload_asset(db: DbSession, user: CurrentUser, file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith(("image/", "text/", "application/pdf")):
        raise HTTPException(status_code=415, detail="unsupported asset type")
    limit = 15 * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            break
    if total > limit:
        raise HTTPException(status_code=413, detail="asset too large")
    content = b"".join(chunks)
    suffix = Path(file.filename or "upload.bin").suffix[:10]
    # The upload directory may not exist on a fresh deployment (it is normally
    # a mounted, ignored runtime volume). Create it before writing the first
    # user asset so the text-vision upload path works without a manual mkdir.
    storage_dir = Path(get_settings().storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage = storage_dir / f"upload-{os.urandom(10).hex()}{suffix}"
    storage.write_bytes(content)
    asset = Asset(user_id=user.id, kind="upload", storage_key=str(storage), mime_type=file.content_type, size_bytes=len(content))
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return {"id": asset.id, "url": f"/api/v1/assets/{asset.id}", "mime_type": asset.mime_type, "size_bytes": asset.size_bytes}
