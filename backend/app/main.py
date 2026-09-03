from contextlib import asynccontextmanager
import logging
import json
import time
from uuid import uuid4
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy import inspect as sqlalchemy_inspect

from app.api import admin, auth, channels, workspace
from app.api.dependencies import current_user
from app.core.config import get_settings
from app.core.security import hash_password
from app.core.crypto import encrypt_secret
from app.services.providers import api_base_url
from app.models import ModelChannel
from app.core.limits import limiter
from app.db.session import engine, SessionLocal
from app.models import Base, User
from app.schemas.auth import UserResponse

settings = get_settings()
logger = logging.getLogger("chat_workspace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(auth.router, prefix="/api/v1")
app.include_router(channels.router, prefix="/api/v1")
app.include_router(workspace.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")


@app.get("/api/v1/me", response_model=UserResponse, include_in_schema=False)
def current_profile(user=Depends(current_user)):
    return UserResponse.model_validate(user, from_attributes=True)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    request.state.request_id = request_id
    path = request.url.path
    category = "auth" if path in {"/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/refresh"} else "model" if path.endswith("/messages/stream") or path.endswith("/image-generations") or path.endswith("/image-edits") or path.endswith("/regenerate") else None
    if category:
        client_host = request.client.host if request.client else "unknown"
        limit = settings.rate_limit_auth_requests if category == "auth" else settings.rate_limit_model_requests
        allowed, retry_after = limiter.allow(f"{category}:{client_host}", limit, settings.rate_limit_window_seconds)
        if not allowed:
            response = JSONResponse(status_code=429, content={"code": "RATE_LIMITED", "message": "请求过于频繁，请稍后重试", "request_id": request_id, "details": {"category": category}}, headers={"Retry-After": str(retry_after), "X-Request-ID": request_id})
            return response
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    logger.info("request method=%s path=%s status=%s duration_ms=%s request_id=%s", request.method, path, response.status_code, int((time.perf_counter() - started) * 1000), request_id)
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID") or uuid4().hex
    detail = exc.detail if isinstance(exc.detail, dict) else {"reason": exc.detail}
    return JSONResponse(status_code=exc.status_code, content={"code": f"HTTP_{exc.status_code}", "message": str(exc.detail), "request_id": request_id, "details": detail}, headers={"X-Request-ID": request_id})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID") or uuid4().hex
    return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": "请求参数校验失败", "request_id": request_id, "details": jsonable_encoder(exc.errors())}, headers={"X-Request-ID": request_id})


def initialize_database() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_schema_compatibility()
    with SessionLocal() as db:
        if not db.scalar(select(User).where(User.email == settings.admin_email.lower())):
            db.add(User(email=settings.admin_email.lower(), display_name="Administrator", role="admin", password_hash=hash_password(settings.admin_password)))
            db.commit()
        _seed_model_channels(db)


def _ensure_schema_compatibility() -> None:
    """Add v2 columns when a development database predates Alembic.

    `create_all` only creates missing tables; it deliberately does not alter
    existing tables. This small idempotent bridge keeps local SQLite fixtures
    bootable while production deployments continue to use Alembic.
    """
    additions = {
        "messages": {
            "content_json": "JSON",
            "tool_call_id": "VARCHAR(160)",
            "tool_name": "VARCHAR(120)",
            "asset_ids_json": "JSON",
        },
        "model_requests": {
            "parent_request_id": "VARCHAR(36)",
            "turn_index": "INTEGER DEFAULT 0",
            "idempotency_key": "VARCHAR(160)",
            "events_json": "JSON",
        },
        "model_channels": {
            "channel_type": "VARCHAR(20) DEFAULT 'official'",
            "capabilities_json": "JSON",
            "models_synced_at": "DATETIME",
            "last_sync_error": "VARCHAR(500)",
            "last_tested_at": "DATETIME",
            "last_test_ok": "BOOLEAN",
        },
    }
    indexes = {
        "messages": {"ix_messages_tool_call_id": "tool_call_id"},
        "model_requests": {
            "ix_model_requests_parent_request_id": "parent_request_id",
            "ix_model_requests_idempotency_key": "idempotency_key",
        },
    }
    try:
        inspector = sqlalchemy_inspect(engine)
        with engine.begin() as connection:
            for table, columns in additions.items():
                if not inspector.has_table(table):
                    continue
                existing = {item["name"] for item in inspector.get_columns(table)}
                for name, column_type in columns.items():
                    if name in existing:
                        continue
                    try:
                        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
                    except Exception as exc:  # pragma: no cover - vendor-specific DDL
                        logger.warning("schema compatibility column failed table=%s column=%s error=%s", table, name, type(exc).__name__)
            for table, table_indexes in indexes.items():
                if not inspector.has_table(table):
                    continue
                existing_indexes = {item["name"] for item in inspector.get_indexes(table)} | {item["name"] for item in inspector.get_unique_constraints(table) if item.get("name")}
                for index_name, column in table_indexes.items():
                    if index_name in existing_indexes:
                        continue
                    try:
                        connection.exec_driver_sql(f"CREATE INDEX {index_name} ON {table} ({column})")
                    except Exception as exc:  # pragma: no cover - vendor-specific DDL
                        logger.warning("schema compatibility index failed index=%s error=%s", index_name, type(exc).__name__)
            if inspector.has_table("model_requests"):
                existing_indexes = {item["name"] for item in inspector.get_indexes("model_requests")} | {item["name"] for item in inspector.get_unique_constraints("model_requests") if item.get("name")}
                if "ux_model_requests_idempotency" not in existing_indexes:
                    try:
                        connection.exec_driver_sql("CREATE UNIQUE INDEX ux_model_requests_idempotency ON model_requests (user_id, thread_id, idempotency_key, modality)")
                    except Exception as exc:  # pragma: no cover - vendor-specific DDL
                        logger.warning("schema compatibility unique index failed error=%s", type(exc).__name__)
    except Exception as exc:  # pragma: no cover - startup should still expose diagnostics
        logger.warning("schema compatibility check failed error=%s", type(exc).__name__)


def _seed_model_channels(db) -> None:
    """Create/update optional channels declared in CHAT_MODEL_CHANNELS_JSON."""
    raw = settings.model_channels_json.strip()
    if not raw:
        return
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("CHAT_MODEL_CHANNELS_JSON is not valid JSON")
        return
    if not isinstance(entries, list):
        logger.warning("CHAT_MODEL_CHANNELS_JSON must be a JSON array")
        return
    for item in entries:
        if not isinstance(item, dict) or not item.get("name") or not item.get("base_url"):
            continue
        channel = db.scalar(select(ModelChannel).where(ModelChannel.name == str(item["name"]).strip()))
        if channel is None:
            channel = ModelChannel(name=str(item["name"]).strip(), base_url=api_base_url(str(item["base_url"])), api_key_encrypted=encrypt_secret(str(item.get("api_key", ""))), modality=str(item.get("modality", "both")), enabled=bool(item.get("enabled", True)), priority=int(item.get("priority", 100)), models_json=[str(value).strip() for value in item.get("models", []) if str(value).strip()], provider=str(item.get("provider", "openai-compatible")), protocol="openai", channel_type=str(item.get("channel_type", "official")), capabilities_json=item.get("capabilities") or {})
            db.add(channel)
        else:
            # Existing admin edits win; only fill an empty model list/key.
            if not channel.models_json and item.get("models"):
                channel.models_json = [str(value).strip() for value in item["models"] if str(value).strip()]
            if not channel.api_key_encrypted and item.get("api_key"):
                channel.api_key_encrypted = encrypt_secret(str(item["api_key"]))
    db.commit()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}
