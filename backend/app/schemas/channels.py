from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.schemas.base import BeijingTimeResponse


Modality = Literal["text", "image", "both"]
ChannelType = Literal["official", "codex"]
CHANNEL_CAPABILITY_KEYS = {
    "image_url_hosts",
    "allow_http_image_urls",
    "allow_private_image_urls",
    "image_edit_transport",
    "supports_input_image",
    "max_input_images",
    "input_image_max_bytes",
    "input_image_detail",
    "supported_input_image_mime_types",
}


class ModelChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: AnyHttpUrl
    api_key: str = Field(default="", max_length=2000)
    modality: Modality
    channel_type: ChannelType = "official"
    # The provider is the source of truth; the admin UI fills this after the
    # channel is created by calling `/v1/models`.
    models: list[str] = Field(default_factory=list, max_length=100)
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True
    provider: str = Field(default="openai-compatible", max_length=80)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "provider")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("base_url")
    @classmethod
    def reject_base_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        parsed = urlsplit(str(value))
        if parsed.username or parsed.password:
            raise ValueError("Base URL must not contain username or password")
        return value

    @field_validator("models")
    @classmethod
    def normalize_models(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        return normalized

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for model, values in value.items():
            key = str(model).strip()
            if not key:
                continue
            # Channel-level transport/capability keys may themselves be
            # arrays (for example ``supported_input_image_mime_types`` or
            # ``image_url_hosts``).  Handle reserved keys before the legacy
            # model-to-modalities list branch; otherwise those arrays would be
            # filtered down to only ``text``/``image`` and silently become an
            # empty policy.
            if key.startswith("_") or key in CHANNEL_CAPABILITY_KEYS:
                normalized[key] = values
            elif isinstance(values, list):
                normalized[key] = list(dict.fromkeys(str(item) for item in values if str(item) in {"text", "image"}))
            elif isinstance(values, dict):
                # Newer capability contracts keep output modalities and input
                # vision limits together under a model key.
                normalized[key] = values
        return normalized


class ModelChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: AnyHttpUrl | None = None
    api_key: str | None = Field(default=None, max_length=2000)
    modality: Modality | None = None
    channel_type: ChannelType | None = None
    models: list[str] | None = Field(default=None, max_length=100)
    priority: int | None = Field(default=None, ge=0, le=10000)
    enabled: bool | None = None
    provider: str | None = Field(default=None, max_length=80)
    capabilities: dict[str, Any] | None = None

    @field_validator("name", "provider")
    @classmethod
    def normalize_update_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("base_url")
    @classmethod
    def reject_update_base_url_credentials(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is None:
            return None
        parsed = urlsplit(str(value))
        if parsed.username or parsed.password:
            raise ValueError("Base URL must not contain username or password")
        return value

    @field_validator("models")
    @classmethod
    def normalize_update_models(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not normalized:
            raise ValueError("models must contain at least one non-empty value")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def normalize_update_capabilities(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        normalized: dict[str, Any] = {}
        for model, values in value.items():
            key = str(model).strip()
            if not key:
                continue
            # Keep reserved channel-level arrays intact; see the create
            # validator above for why this ordering matters.
            if key.startswith("_") or key in CHANNEL_CAPABILITY_KEYS:
                normalized[key] = values
            elif isinstance(values, list):
                normalized[key] = list(dict.fromkeys(str(item) for item in values if str(item) in {"text", "image"}))
            elif isinstance(values, dict):
                normalized[key] = values
        return normalized


class ModelChannelResponse(BeijingTimeResponse):
    id: str
    name: str
    provider: str
    protocol: str
    channel_type: ChannelType
    base_url: str
    api_key_masked: str
    modality: str
    enabled: bool
    priority: int
    models: list[str]
    capabilities: dict[str, Any] = Field(default_factory=dict)
    models_synced_at: datetime | None = None
    last_sync_error: str | None = None
    last_tested_at: datetime | None = None
    last_test_ok: bool | None = None
    created_at: datetime
    updated_at: datetime


class ChannelTestResponse(BaseModel):
    ok: bool
    message: str
    checked_url: str


class ChannelSyncResponse(BaseModel):
    ok: bool
    checked_url: str
    models: list[str]
    capabilities: dict[str, Any] = Field(default_factory=dict)
    message: str
