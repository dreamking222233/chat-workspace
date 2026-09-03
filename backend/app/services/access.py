from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Entitlement, User


def active_entitlement(db: Session, user: User) -> Entitlement | None:
    if user.role == "admin":
        return None
    now = datetime.now(timezone.utc)
    entitlements = db.scalars(select(Entitlement).where(Entitlement.user_id == user.id, Entitlement.status == "active").order_by(Entitlement.expires_at.desc())).all()
    for item in entitlements:
        expires = item.expires_at.replace(tzinfo=timezone.utc) if item.expires_at.tzinfo is None else item.expires_at.astimezone(timezone.utc)
        starts = item.starts_at.replace(tzinfo=timezone.utc) if item.starts_at.tzinfo is None else item.starts_at.astimezone(timezone.utc)
        if starts <= now < expires:
            return item
    return None


def require_entitlement(db: Session, user: User) -> Entitlement | None:
    if user.status != "active":
        raise PermissionError("account is inactive")
    entitlement = active_entitlement(db, user)
    if user.role != "admin" and entitlement is None:
        raise PermissionError("active entitlement required")
    return entitlement
