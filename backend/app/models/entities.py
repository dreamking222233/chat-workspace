from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user", index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    entitlements: Mapped[list["Entitlement"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    threads: Mapped[list["Thread"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Entitlement(Base):
    __tablename__ = "entitlements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    granted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(back_populates="entitlements")


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    user: Mapped[User] = relationship(back_populates="projects")
    threads: Mapped[list["Thread"]] = relationship(back_populates="project")


class Thread(Base):
    __tablename__ = "threads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="新聊天")
    model: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    user: Mapped[User] = relationship(back_populates="threads")
    project: Mapped[Project | None] = relationship(back_populates="threads")
    messages: Mapped[list["Message"]] = relationship(back_populates="thread", cascade="all, delete-orphan", order_by="Message.sequence")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(20), default="text")
    # Structured provider metadata is kept separately from rendered text so
    # tool calls and multimodal references can be replayed without guessing
    # from the display string.
    content_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    tool_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    asset_ids_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    thread: Mapped[Thread] = relationship(back_populates="messages")


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    storage_key: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelRequest(Base):
    __tablename__ = "model_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id", ondelete="SET NULL"), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("model_channels.id", ondelete="SET NULL"), nullable=True)
    model: Mapped[str] = mapped_column(String(120))
    modality: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parent_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    turn_index: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    events_json: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    format: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="completed")
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    admin_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(120))
    target_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ModelChannel(Base):
    __tablename__ = "model_channels"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(80), default="openai-compatible")
    protocol: Mapped[str] = mapped_column(String(30), default="openai")
    # Upstream family: official ChatGPT reverse proxy or Codex GPT reverse proxy.
    # The value is metadata used by routing/UI; both continue to use OpenAI wire format.
    channel_type: Mapped[str] = mapped_column(String(20), default="official", index=True)
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    modality: Mapped[str] = mapped_column(String(20), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    models_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Optional per-model modality map, e.g. {"gpt-5": ["text"],
    # "gpt-image-2": ["image"]}. Older rows continue to use channel.modality.
    capabilities_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    models_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
