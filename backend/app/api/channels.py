from datetime import datetime, timezone
from dataclasses import dataclass, field
import json
from threading import Condition, Lock
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DbSession, admin_user
from app.core.crypto import decrypt_secret, encrypt_secret
from app.models import AdminAuditLog, ModelChannel, ModelRequest, User
from app.schemas.channels import ChannelSyncResponse, ChannelTestResponse, ModelChannelCreate, ModelChannelResponse, ModelChannelUpdate
from app.services.providers import api_base_url, list_remote_models

router = APIRouter(prefix="/admin/model-channels", tags=["admin-model-channels"])
Admin = Annotated[User, Depends(admin_user)]


@dataclass
class _ChannelSyncState:
    condition: Condition = field(default_factory=Condition)
    active: bool = False
    generation: int = 0
    last_result: ChannelSyncResponse | None = None


_channel_sync_states: dict[str, _ChannelSyncState] = {}
_channel_sync_states_guard = Lock()


def mask_key(value: str) -> str:
    if not value:
        return "未配置"
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}{'•' * 8}{value[-4:]}"


def _url_origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), parsed.port


def _channel_sync_state(channel_id: str) -> _ChannelSyncState:
    with _channel_sync_states_guard:
        return _channel_sync_states.setdefault(channel_id, _ChannelSyncState())


def to_response(channel: ModelChannel) -> ModelChannelResponse:
    return ModelChannelResponse(
        id=channel.id,
        name=channel.name,
        provider=channel.provider,
        protocol=channel.protocol,
        channel_type=channel.channel_type or "official",
        base_url=channel.base_url,
        api_key_masked=mask_key(decrypt_secret(channel.api_key_encrypted)),
        modality=channel.modality,
        enabled=channel.enabled,
        priority=channel.priority,
        models=channel.models_json or [],
        capabilities=channel.capabilities_json or {},
        models_synced_at=channel.models_synced_at,
        last_sync_error=channel.last_sync_error,
        last_tested_at=channel.last_tested_at,
        last_test_ok=channel.last_test_ok,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


@router.get("", response_model=list[ModelChannelResponse])
def list_channels(db: DbSession, _: Admin, modality: str | None = Query(default=None), enabled: bool | None = Query(default=None)):
    query = select(ModelChannel).order_by(ModelChannel.priority.asc(), ModelChannel.created_at.desc())
    if modality in {"text", "image", "both"}:
        query = query.where(ModelChannel.modality.in_([modality, "both"]))
    if enabled is not None:
        query = query.where(ModelChannel.enabled == enabled)
    return [to_response(item) for item in db.scalars(query).all()]


@router.get("/{channel_id}/remote-models", response_model=ChannelSyncResponse)
def remote_models(channel_id: str, db: DbSession, _: Admin):
    """Read the provider model catalog for the model manager without mutating the channel."""
    channel = db.get(ModelChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="channel not found")
    checked_url = f"{api_base_url(channel.base_url)}/models"
    try:
        remote = list_remote_models(channel)
        model_ids, capabilities = _remote_model_metadata(remote)
        if not model_ids:
            raise ValueError("provider returned no models")
        return ChannelSyncResponse(ok=True, checked_url=checked_url, models=model_ids, capabilities=capabilities, message=f"已获取 {len(model_ids)} 个远端模型")
    except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return ChannelSyncResponse(ok=False, checked_url=checked_url, models=[], capabilities={}, message="远端模型获取失败，请检查渠道配置")


@router.post("", response_model=ModelChannelResponse, status_code=status.HTTP_201_CREATED)
def create_channel(payload: ModelChannelCreate, db: DbSession, admin: Admin):
    channel = ModelChannel(
        name=payload.name.strip(), base_url=api_base_url(str(payload.base_url)), api_key_encrypted=encrypt_secret(payload.api_key),
        modality=payload.modality, models_json=payload.models, priority=payload.priority, enabled=payload.enabled,
        provider=payload.provider, protocol="openai", channel_type=payload.channel_type, capabilities_json=payload.capabilities or {}, created_by=admin.id,
    )
    db.add(channel)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="channel name already exists") from exc
    db.refresh(channel)
    db.add(AdminAuditLog(admin_id=admin.id, action="create_model_channel", metadata_json={"channel_id": channel.id, "name": channel.name}))
    db.commit()
    return to_response(channel)


@router.patch("/{channel_id}", response_model=ModelChannelResponse)
def update_channel(channel_id: str, payload: ModelChannelUpdate, db: DbSession, admin: Admin):
    channel = db.get(ModelChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="channel not found")
    values = payload.model_dump(exclude_unset=True)
    supplied_key = values.pop("api_key", None) if "api_key" in values else None
    supplied_key = supplied_key.strip() if isinstance(supplied_key, str) else None
    normalized_base_url = api_base_url(str(values["base_url"])) if values.get("base_url") else None
    if normalized_base_url and _url_origin(normalized_base_url) != _url_origin(channel.base_url) and not supplied_key:
        # Never carry an unreadable stored secret to a different origin. An
        # administrator changing providers must prove possession of its key.
        raise HTTPException(status_code=422, detail="API Key is required when the Base URL origin changes")
    if supplied_key:
        channel.api_key_encrypted = encrypt_secret(supplied_key)
    if normalized_base_url:
        values["base_url"] = normalized_base_url
    if "models" in values:
        values["models_json"] = list(dict.fromkeys(item.strip() for item in values.pop("models") if item.strip()))
        if not values["models_json"]:
            raise HTTPException(status_code=422, detail="models must contain at least one value")
    if "capabilities" in values:
        values["capabilities_json"] = values.pop("capabilities") or {}
    for key, value in values.items():
        setattr(channel, key, value)
    channel.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="channel name already exists") from exc
    db.refresh(channel)
    db.add(AdminAuditLog(admin_id=admin.id, action="update_model_channel", metadata_json={"channel_id": channel.id}))
    db.commit()
    return to_response(channel)


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(channel_id: str, db: DbSession, admin: Admin):
    channel = db.get(ModelChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="channel not found")
    request_count = db.scalar(select(ModelRequest.id).where(ModelRequest.channel_id == channel.id).limit(1))
    if request_count:
        channel.enabled = False
        action = "disable_model_channel"
    else:
        db.delete(channel)
        action = "delete_model_channel"
    db.add(AdminAuditLog(admin_id=admin.id, action=action, metadata_json={"channel_id": channel_id, "had_requests": bool(request_count)}))
    db.commit()


@router.post("/{channel_id}/test", response_model=ChannelTestResponse)
def test_channel(channel_id: str, db: DbSession, admin: Admin):
    channel = db.get(ModelChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="channel not found")
    checked_url = f"{api_base_url(channel.base_url)}/models"
    secret = decrypt_secret(channel.api_key_encrypted)
    if not secret:
        db.add(AdminAuditLog(admin_id=admin.id, action="test_model_channel", metadata_json={"channel_id": channel.id, "ok": False}))
        db.commit()
        return ChannelTestResponse(ok=False, message="未配置 API Key", checked_url=checked_url)
    try:
        response = httpx.get(checked_url, headers={"Authorization": f"Bearer {secret}"}, timeout=8)
        if response.is_success:
            channel.last_tested_at = datetime.now(timezone.utc)
            channel.last_test_ok = True
            channel.last_sync_error = None
            db.add(AdminAuditLog(admin_id=admin.id, action="test_model_channel", metadata_json={"channel_id": channel.id, "ok": True}))
            db.commit()
            return ChannelTestResponse(ok=True, message="连接成功，渠道可用", checked_url=checked_url)
        channel.last_tested_at = datetime.now(timezone.utc)
        channel.last_test_ok = False
        db.add(AdminAuditLog(admin_id=admin.id, action="test_model_channel", metadata_json={"channel_id": channel.id, "ok": False, "status_code": response.status_code}))
        db.commit()
        return ChannelTestResponse(ok=False, message=f"渠道返回 HTTP {response.status_code}", checked_url=checked_url)
    except httpx.HTTPError:
        channel.last_tested_at = datetime.now(timezone.utc)
        channel.last_test_ok = False
        db.add(AdminAuditLog(admin_id=admin.id, action="test_model_channel", metadata_json={"channel_id": channel.id, "ok": False}))
        db.commit()
        return ChannelTestResponse(ok=False, message="连接失败，请检查 Base URL、密钥和网络", checked_url=checked_url)


@router.post("/{channel_id}/sync-models", response_model=ChannelSyncResponse)
def sync_models(channel_id: str, db: DbSession, admin: Admin):
    """Fetch `/models`, merge IDs, and retain explicit capability overrides."""
    channel = db.get(ModelChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="channel not found")
    checked_url = f"{api_base_url(channel.base_url)}/models"
    state = _channel_sync_state(channel_id)
    with state.condition:
        if state.active:
            observed_generation = state.generation
            while state.active:
                state.condition.wait()
            if state.generation != observed_generation and state.last_result is not None:
                message = "模型已由并发请求同步" if state.last_result.ok else state.last_result.message
                return state.last_result.model_copy(update={"message": message})
        # Only one waiter can claim the next run because the condition lock is
        # held until `active` is set. All later arrivals enter the wait branch.
        state.active = True

    result: ChannelSyncResponse | None = None
    try:
        result = _sync_models_once(channel, checked_url, db, admin)
        return result
    finally:
        # Publish the completed result and wake every waiter atomically. Keeping
        # `active` true until this point closes the former completion-window race.
        with state.condition:
            state.last_result = result
            state.generation += 1
            state.active = False
            state.condition.notify_all()


def _sync_models_once(channel: ModelChannel, checked_url: str, db, admin: User) -> ChannelSyncResponse:
    try:
        remote = list_remote_models(channel)
        remote_ids, inferred_capabilities = _remote_model_metadata(remote)
        if not remote_ids:
            raise ValueError("provider returned no models")
        # The remote `/models` response is the sole source of truth. Replacing
        # the persisted list also removes models that the provider retired.
        channel.models_json = remote_ids
        capabilities = dict(channel.capabilities_json or {})
        for model_id, inferred in inferred_capabilities.items():
            if model_id not in capabilities:
                capabilities[model_id] = inferred
        channel.capabilities_json = capabilities
        channel.models_synced_at = datetime.now(timezone.utc)
        channel.last_sync_error = None
        db.add(AdminAuditLog(admin_id=admin.id, action="sync_model_channel", metadata_json={"channel_id": channel.id, "count": len(remote_ids)}))
        db.commit()
        return ChannelSyncResponse(ok=True, checked_url=checked_url, models=channel.models_json or [], capabilities=capabilities, message=f"已同步 {len(remote_ids)} 个模型")
    except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        channel.last_sync_error = type(exc).__name__[:480]
        channel.last_tested_at = datetime.now(timezone.utc)
        channel.last_test_ok = False
        db.add(AdminAuditLog(admin_id=admin.id, action="sync_model_channel", metadata_json={"channel_id": channel.id, "ok": False}))
        db.commit()
        return ChannelSyncResponse(ok=False, checked_url=checked_url, models=channel.models_json or [], capabilities=channel.capabilities_json or {}, message="模型同步失败，请检查渠道配置")


def _remote_model_metadata(remote: list[dict]) -> tuple[list[str], dict[str, list[str]]]:
    model_ids = list(dict.fromkeys(str(item["id"]).strip() for item in remote if item.get("id")))
    capabilities = {model_id: (["image"] if "image" in model_id.lower() else ["text"]) for model_id in model_ids}
    return model_ids, capabilities
