from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret
from app.models import ModelChannel


def _effective_capabilities(channel: ModelChannel, model: str) -> set[str]:
    capability_map = channel.capabilities_json if isinstance(channel.capabilities_json, dict) else {}
    raw_configured = capability_map.get(model) or []
    if isinstance(raw_configured, dict):
        raw_configured = raw_configured.get("capabilities") or raw_configured.get("modalities") or []
    configured = {str(item) for item in raw_configured if str(item) in {"text", "image"}}
    if configured:
        return configured
    if channel.modality != "both":
        return {channel.modality}
    # Most OpenAI-compatible `/models` implementations do not advertise
    # modalities. Infer the dedicated image family from its stable name while
    # retaining `both` for a channel that only exposes generic model IDs.
    lowered = model.lower()
    image_named = any(token in lowered for token in ("image", "dall-e", "dalle", "imagen"))
    channel_has_image_named = any(any(token in str(item).lower() for token in ("image", "dall-e", "dalle", "imagen")) for item in (channel.models_json or []))
    if image_named:
        return {"image"}
    if channel_has_image_named:
        return {"text"}
    return {"text", "image"}


def available_models(db: Session, modality: str | None = None) -> list[tuple[str, str, str]]:
    query = select(ModelChannel).where(ModelChannel.enabled.is_(True)).order_by(ModelChannel.priority.asc())
    rows: list[tuple[str, str, str]] = []
    for channel in db.scalars(query).all():
        for model in channel.models_json or []:
            effective = _effective_capabilities(channel, model)
            if modality and modality not in effective:
                continue
            # Keep the historical tuple shape for callers that only need the
            # display fields. New callers can use available_model_options.
            rows.append((model, channel.modality, channel.name))
    return rows


def choose_channel(db: Session, model: str, modality: str) -> ModelChannel | None:
    query = select(ModelChannel).where(ModelChannel.enabled.is_(True)).order_by(ModelChannel.priority.asc())
    for channel in db.scalars(query).all():
        if model in (channel.models_json or []):
            effective = _effective_capabilities(channel, model)
            if modality in effective:
                return channel
    return None


def model_capabilities(channel: ModelChannel, model: str) -> list[str]:
    values = _effective_capabilities(channel, model)
    return [item for item in ("text", "image") if item in values]


def model_input_image_capabilities(channel: ModelChannel, model: str) -> dict:
    """Return normalized text-vision limits for the model option API."""
    capabilities = channel.capabilities_json if isinstance(channel.capabilities_json, dict) else {}
    model_config = capabilities.get(model) if isinstance(capabilities.get(model), dict) else {}
    supports = model_config.get("supports_input_image") if "supports_input_image" in model_config else capabilities.get("supports_input_image", True)
    if isinstance(supports, str):
        supports = supports.strip().lower() not in {"0", "false", "no", "off"}
    else:
        supports = bool(supports)
    try:
        max_images = max(1, min(8, int(model_config.get("max_input_images", capabilities.get("max_input_images", 8)))))
    except (TypeError, ValueError):
        max_images = 8
    try:
        max_bytes = max(0, int(model_config.get("input_image_max_bytes", capabilities.get("input_image_max_bytes", 0))))
    except (TypeError, ValueError):
        max_bytes = 0
    raw_detail = model_config.get("input_image_detail", capabilities.get("input_image_detail", "auto"))
    detail = str(raw_detail or "auto").strip().lower()
    if detail not in {"auto", "low", "high"}:
        detail = "auto"
    raw_mimes = model_config.get("supported_input_image_mime_types", capabilities.get("supported_input_image_mime_types", ["image/jpeg", "image/png"]))
    if isinstance(raw_mimes, str):
        raw_mimes = [raw_mimes]
    mimes = [str(item).strip().lower() for item in raw_mimes if str(item).strip()] if isinstance(raw_mimes, (list, tuple, set)) else []
    # An omitted or empty MIME policy means "use the contract defaults".  A
    # channel that supports visual input but accidentally stores an empty list
    # should not advertise a model that rejects every valid JPEG/PNG request;
    # administrators can disable vision explicitly with supports_input_image.
    if not mimes:
        mimes = ["image/jpeg", "image/png"]
    return {"supports_input_image": supports, "max_input_images": max_images if supports else 0, "input_image_max_bytes": max_bytes if supports else 0, "input_image_detail": detail if supports else "auto", "supported_input_image_mime_types": mimes if supports else []}


def available_model_options(db: Session, modality: str | None = None) -> list[dict]:
    query = select(ModelChannel).where(ModelChannel.enabled.is_(True)).order_by(ModelChannel.priority.asc(), ModelChannel.created_at.asc())
    result: list[dict] = []
    for channel in db.scalars(query).all():
        for model in channel.models_json or []:
            capabilities = model_capabilities(channel, model)
            if modality and modality not in capabilities:
                continue
            result.append({"model": model, "modality": "+".join(capabilities), "channel_name": channel.name, "channel_id": channel.id, "channel_type": channel.channel_type or "official", "capabilities": capabilities, **model_input_image_capabilities(channel, model)})
    return result


def channel_key(channel: ModelChannel) -> str:
    return decrypt_secret(channel.api_key_encrypted)
