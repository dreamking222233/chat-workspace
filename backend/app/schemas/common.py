import base64
import binascii
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.image_options import normalize_image_quality, normalize_image_size
from app.schemas.base import BeijingTimeResponse


VISION_DATA_URL_PATTERN = re.compile(r"^data:(image/(?:jpeg|png));base64,([A-Za-z0-9+/]+={0,2})$")
VISION_MAX_ENCODED_CHARS = 1_572_864
VISION_MAX_TOTAL_ENCODED_CHARS = 3_145_728
VISION_SIGNATURES = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
}


class TextMediaInput(BaseModel):
    """One browser-encoded image attached to a text-model request."""

    type: Literal["image"] = "image"
    data_url: str = Field(min_length=32, max_length=2_100_000)
    node_id: str | None = Field(default=None, max_length=128)
    asset_id: str = Field(min_length=1, max_length=36)
    mime_type: Literal["image/jpeg", "image/png"]
    width: int | None = Field(default=None, ge=1, le=20_000)
    height: int | None = Field(default=None, ge=1, le=20_000)
    detail: Literal["auto", "low", "high"] = "auto"

    @field_validator("asset_id", mode="before")
    @classmethod
    def normalize_asset_id(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("asset_id must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_data_url(self):
        match = VISION_DATA_URL_PATTERN.fullmatch(self.data_url)
        if not match or match.group(1) != self.mime_type:
            raise ValueError("视觉输入必须是合法的 JPEG/PNG Data URL")
        encoded = match.group(2)
        if len(encoded) > VISION_MAX_ENCODED_CHARS:
            raise ValueError("单张视觉输入超过 1.5 MiB 编码限制")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("视觉输入 Base64 无效") from exc
        if not decoded or not decoded.startswith(VISION_SIGNATURES[self.mime_type]):
            raise ValueError("视觉输入内容与声明的图片格式不一致")
        return self

    @property
    def encoded_length(self) -> int:
        match = VISION_DATA_URL_PATTERN.fullmatch(self.data_url)
        return len(match.group(2)) if match else 0

    @property
    def decoded_size(self) -> int:
        return len(base64.b64decode(self.data_url.partition(",")[2], validate=True))


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_update_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class ProjectResponse(BeijingTimeResponse):
    id: str
    name: str
    description: str
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    thread_count: int = 0


class ThreadCreate(BaseModel):
    title: str = Field(default="新聊天", max_length=200)
    project_id: str | None = None
    model: str = ""

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return value.strip() or "新聊天"


class ThreadUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    project_id: str | None = None

    @field_validator("title")
    @classmethod
    def normalize_update_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized


class MessageResponse(BeijingTimeResponse):
    id: str
    role: str
    content: str
    content_type: str
    sequence: int
    created_at: datetime
    asset_ids: list[str] = Field(default_factory=list)


class ThreadResponse(BeijingTimeResponse):
    id: str
    project_id: str | None
    title: str
    model: str
    status: str
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse] = Field(default_factory=list)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100000)
    model: str | None = None
    channel_id: str | None = Field(default=None, max_length=36)
    modality: Literal["text", "image"] = "text"
    # References are uploaded through /assets/upload and checked again by the
    # server before they are sent to a provider.
    asset_ids: list[str] = Field(default_factory=list, max_length=8)
    media_inputs: list["TextMediaInput"] = Field(default_factory=list, max_length=8)
    mask_asset_id: str | None = Field(default=None, max_length=160)
    size: str = Field(default="1024x1024", max_length=32)
    quality: str | None = Field(default=None, max_length=32)
    # The workspace persists one assistant image message per request.  Keep the
    # public contract honest until multi-image gallery persistence is added.
    n: Literal[1] = 1
    response_format: Literal["b64_json", "url"] = "b64_json"
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    response_options: dict[str, Any] | None = None
    enable_tools: bool = True
    tool_choice: str | dict[str, Any] | None = None

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value

    @field_validator("asset_ids")
    @classmethod
    def normalize_asset_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item and item.strip()))

    @model_validator(mode="after")
    def normalize_media_asset_ids(self):
        media_ids = [item.asset_id for item in self.media_inputs]
        self.asset_ids = list(dict.fromkeys([*self.asset_ids, *media_ids]))
        if len(self.asset_ids) > 8:
            raise ValueError("too many assets")
        if sum(item.encoded_length for item in self.media_inputs) > VISION_MAX_TOTAL_ENCODED_CHARS:
            raise ValueError("本轮视觉输入超过 3 MiB 编码限制")
        return self

    @field_validator("size")
    @classmethod
    def valid_size(cls, value: str) -> str:
        return normalize_image_size(value)

    @field_validator("quality")
    @classmethod
    def valid_quality(cls, value: str | None) -> str | None:
        return normalize_image_quality(value)


class RegenerateRequest(BaseModel):
    """Request a fresh assistant completion for an existing conversation."""

    assistant_message_id: str | None = None
    model: str | None = None
    channel_id: str | None = Field(default=None, max_length=36)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    enable_tools: bool = True
    tool_choice: str | dict[str, Any] | None = None
    response_options: dict[str, Any] | None = None


class ImageGenerationRequest(BaseModel):
    # `content` was used by the first UI build; accept it as an input alias so
    # older clients keep working while the public contract uses `prompt`.
    prompt: str = Field(min_length=1, max_length=100000, validation_alias=AliasChoices("prompt", "content"))
    model: str | None = None
    channel_id: str | None = Field(default=None, max_length=36)
    size: str = "1024x1024"
    quality: str | None = None
    n: Literal[1] = 1
    response_format: Literal["b64_json", "url"] = "b64_json"
    asset_ids: list[str] = Field(default_factory=list, max_length=8)
    mask_asset_id: str | None = Field(default=None, max_length=160)

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must not be blank")
        return normalized

    @field_validator("asset_ids")
    @classmethod
    def normalize_asset_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item and item.strip()))

    @field_validator("size")
    @classmethod
    def valid_image_size(cls, value: str) -> str:
        return normalize_image_size(value)

    @field_validator("quality")
    @classmethod
    def valid_image_quality(cls, value: str | None) -> str | None:
        return normalize_image_quality(value)

    @model_validator(mode="after")
    def validate_mask(self):
        if self.mask_asset_id and self.mask_asset_id in self.asset_ids:
            raise ValueError("mask asset must be different from reference image")
        if self.mask_asset_id and not self.asset_ids:
            raise ValueError("mask asset requires a reference image")
        return self


class ImageEditRequest(ImageGenerationRequest):
    """Explicit image-edit contract; at least one reference asset is needed."""

    @model_validator(mode="after")
    def require_reference(self):
        if not self.asset_ids:
            raise ValueError("at least one reference asset is required")
        return self


class GenerateImageToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=100000)
    model: str | None = None
    channel_id: str | None = Field(default=None, max_length=36)
    size: str = "1024x1024"
    quality: str | None = None
    asset_ids: list[str] = Field(default_factory=list, max_length=8)
    mask_asset_id: str | None = Field(default=None, max_length=160)

    @field_validator("prompt")
    @classmethod
    def normalize_tool_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must not be blank")
        return normalized

    @field_validator("size")
    @classmethod
    def valid_tool_size(cls, value: str) -> str:
        return normalize_image_size(value)

    @field_validator("quality")
    @classmethod
    def valid_tool_quality(cls, value: str | None) -> str | None:
        return normalize_image_quality(value)

    @field_validator("asset_ids")
    @classmethod
    def normalize_tool_asset_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item and item.strip()))

    @model_validator(mode="after")
    def validate_tool_mask(self):
        if self.mask_asset_id and self.mask_asset_id in self.asset_ids:
            raise ValueError("mask asset must be different from reference image")
        if self.mask_asset_id and not self.asset_ids:
            raise ValueError("mask asset requires a reference image")
        return self


class ModelOption(BaseModel):
    model: str
    modality: str
    channel_name: str
    channel_id: str | None = None
    channel_type: str = "official"
    capabilities: list[str] = Field(default_factory=list)
    supports_input_image: bool = True
    max_input_images: int = 8
    input_image_max_bytes: int = 0
    input_image_detail: Literal["auto", "low", "high"] = "auto"
    supported_input_image_mime_types: list[str] = Field(default_factory=lambda: ["image/jpeg", "image/png"])


class EntitlementResponse(BeijingTimeResponse):
    id: str
    starts_at: datetime
    expires_at: datetime
    status: str
    active: bool


class GrantEntitlementRequest(BaseModel):
    months: int | None = Field(default=None, ge=1, le=120)
    starts_at: datetime | None = None
    expires_at: datetime | None = None


class UsageResponse(BeijingTimeResponse):
    id: str
    user_id: str
    user_email: str | None = None
    thread_id: str | None
    model: str
    modality: str
    status: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    created_at: datetime


class PaginatedUsers(BaseModel):
    items: list[dict]
    total: int
    page: int
    page_size: int
