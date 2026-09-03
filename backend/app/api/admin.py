from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from app.api.dependencies import DbSession, admin_user
from app.core.security import hash_password
from app.core.time import as_utc, beijing_isoformat
from app.models import AdminAuditLog, Entitlement, ModelRequest, Project, Thread, User
from app.schemas.auth import UserResponse
from app.schemas.common import EntitlementResponse, GrantEntitlementRequest, PaginatedUsers, UsageResponse

router = APIRouter(prefix="/admin", tags=["admin"])
Admin = Annotated[User, Depends(admin_user)]


def _aware(value: datetime) -> datetime:
    return as_utc(value)


def _database_time(value: datetime) -> datetime:
    """Floor user-facing timestamps because MySQL DATETIME(0) rounds values."""
    normalized = _aware(value)
    return normalized.replace(microsecond=0)


def _audit(db, admin: User, action: str, target: str | None = None, metadata: dict | None = None):
    db.add(AdminAuditLog(admin_id=admin.id, action=action, target_user_id=target, metadata_json=metadata))


@router.get("/users", response_model=PaginatedUsers)
def list_users(db: DbSession, _: Admin, q: str = "", status: str | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    query = select(User).order_by(User.created_at.desc())
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(User.email.like(pattern) | User.display_name.like(pattern))
    if status:
        query = query.where(User.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    users = db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    items = []
    now = datetime.now(timezone.utc)
    for user in users:
        entitlement = db.scalar(select(Entitlement).where(Entitlement.user_id == user.id, Entitlement.status == "active").order_by(Entitlement.expires_at.desc()))
        expires = beijing_isoformat(entitlement.expires_at) if entitlement else None
        items.append({"id": user.id, "email": user.email, "display_name": user.display_name, "role": user.role, "status": user.status, "created_at": beijing_isoformat(user.created_at), "entitlement_expires_at": expires, "entitlement_active": bool(entitlement and _aware(entitlement.starts_at) <= now < _aware(entitlement.expires_at))})
    return PaginatedUsers(items=items, total=total, page=page, page_size=page_size)


@router.get("/users/{user_id}")
def user_detail(user_id: str, db: DbSession, _: Admin):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    entitlement = db.scalar(select(Entitlement).where(Entitlement.user_id == user.id).order_by(Entitlement.expires_at.desc()))
    projects = db.scalar(select(func.count(Project.id)).where(Project.user_id == user.id)) or 0
    threads = db.scalar(select(func.count(Thread.id)).where(Thread.user_id == user.id)) or 0
    requests = db.scalar(select(func.count(ModelRequest.id)).where(ModelRequest.user_id == user.id)) or 0
    return {"user": UserResponse.model_validate(user, from_attributes=True), "entitlement": _entitlement(entitlement) if entitlement else None, "projects": projects, "threads": threads, "requests": requests}


def _entitlement(item: Entitlement) -> EntitlementResponse:
    now = datetime.now(timezone.utc)
    return EntitlementResponse(id=item.id, starts_at=item.starts_at, expires_at=item.expires_at, status=item.status, active=item.status == "active" and _aware(item.starts_at) <= now < _aware(item.expires_at))


@router.post("/users/{user_id}/entitlements", response_model=EntitlementResponse)
def grant_entitlement(user_id: str, payload: GrantEntitlementRequest, db: DbSession, admin: Admin):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    # Floor the default start time so a grant is active immediately even on
    # MySQL installations whose DATETIME(0) conversion rounds fractional
    # seconds instead of truncating them. A one-second cushion covers the
    # fractional part that is discarded by DATETIME(0).
    starts = _database_time(payload.starts_at) if payload.starts_at else _database_time(datetime.now(timezone.utc) - timedelta(seconds=1))
    if payload.expires_at:
        expires = _database_time(payload.expires_at)
    elif payload.months:
        month = starts.month - 1 + payload.months
        year, month_number = starts.year + month // 12, month % 12 + 1
        day = min(starts.day, monthrange(year, month_number)[1])
        expires = starts.replace(year=year, month=month_number, day=day)
    else:
        raise HTTPException(status_code=422, detail="months or expires_at is required")
    if _aware(expires) <= _aware(starts):
        raise HTTPException(status_code=422, detail="expires_at must be after starts_at")
    for old in db.scalars(select(Entitlement).where(Entitlement.user_id == user.id, Entitlement.status == "active")).all():
        old.status = "replaced"
    item = Entitlement(user_id=user.id, starts_at=starts, expires_at=expires, status="active", granted_by=admin.id)
    db.add(item)
    _audit(db, admin, "grant_entitlement", user.id, {"starts_at": beijing_isoformat(starts), "expires_at": beijing_isoformat(expires)})
    db.commit()
    db.refresh(item)
    return _entitlement(item)


@router.patch("/entitlements/{entitlement_id}", response_model=EntitlementResponse)
def update_entitlement(entitlement_id: str, db: DbSession, admin: Admin, action: str = Query(..., pattern="^(pause|revoke|activate)$")):
    item = db.get(Entitlement, entitlement_id)
    if not item:
        raise HTTPException(status_code=404, detail="entitlement not found")
    item.status = {"pause": "paused", "revoke": "revoked", "activate": "active"}[action]
    _audit(db, admin, f"{action}_entitlement", item.user_id)
    db.commit()
    db.refresh(item)
    return _entitlement(item)


@router.get("/usage", response_model=list[UsageResponse])
def list_usage(
    db: DbSession,
    _: Admin,
    user_id: str | None = None,
    q: str = "",
    model: str | None = None,
    modality: str | None = Query(default=None, pattern="^(text|image)$"),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    query = select(ModelRequest, User.email).join(User, User.id == ModelRequest.user_id)
    if user_id:
        query = query.where(ModelRequest.user_id == user_id)
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(User.email.like(pattern) | User.display_name.like(pattern))
    if model:
        query = query.where(ModelRequest.model.like(f"%{model.strip()}%"))
    if modality:
        query = query.where(ModelRequest.modality == modality)
    if created_after:
        query = query.where(ModelRequest.created_at >= _database_time(created_after))
    if created_before:
        query = query.where(ModelRequest.created_at < _database_time(created_before))
    query = query.order_by(ModelRequest.created_at.desc()).offset(offset).limit(limit)
    return [UsageResponse(id=item.id, user_id=item.user_id, user_email=email, thread_id=item.thread_id, model=item.model, modality=item.modality, status=item.status, input_tokens=item.input_tokens, output_tokens=item.output_tokens, latency_ms=item.latency_ms, created_at=item.created_at) for item, email in db.execute(query).all()]


@router.get("/audit-logs")
def audit_logs(db: DbSession, _: Admin, limit: int = Query(default=100, ge=1, le=500)):
    return [{"id": item.id, "admin_id": item.admin_id, "action": item.action, "target_user_id": item.target_user_id, "metadata": item.metadata_json, "created_at": beijing_isoformat(item.created_at)} for item in db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(limit)).all()]


@router.patch("/users/{user_id}/status")
def update_user_status(user_id: str, db: DbSession, admin: Admin, status: str = Query(..., pattern="^(active|disabled)$")):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="cannot disable current administrator")
    user.status = status
    _audit(db, admin, "update_user_status", user.id, {"status": status})
    db.commit()
    return {"id": user.id, "status": user.status}
