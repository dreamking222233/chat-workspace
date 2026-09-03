from datetime import datetime, timedelta, timezone
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DbSession, current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.models import RefreshToken, User
from app.schemas.auth import AuthResponse, LoginRequest, RefreshRequest, RegisterRequest, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _refresh_value() -> str:
    return secrets.token_urlsafe(48)


def _refresh_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def response_for(user: User, db: DbSession) -> AuthResponse:
    raw_refresh = _refresh_value()
    db.add(RefreshToken(user_id=user.id, token_hash=_refresh_hash(raw_refresh), expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
    db.commit()
    return AuthResponse(access_token=create_access_token(user.id, user.role), refresh_token=raw_refresh, user=UserResponse.model_validate(user, from_attributes=True))


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: DbSession):
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="该邮箱已被注册")
    user = User(email=email, display_name=payload.display_name, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该邮箱已被注册") from exc
    db.refresh(user)
    return response_for(user, db)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: DbSession):
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="user is inactive")
    return response_for(user, db)


@router.post("/refresh", response_model=AuthResponse)
def refresh(payload: RefreshRequest, db: DbSession):
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == _refresh_hash(payload.refresh_token)))
    expires_at = stored.expires_at.replace(tzinfo=timezone.utc) if stored and stored.expires_at.tzinfo is None else stored.expires_at.astimezone(timezone.utc) if stored else None
    if not stored or stored.revoked_at or expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="refresh token expired")
    user = db.get(User, stored.user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=401, detail="user is inactive")
    stored.revoked_at = datetime.now(timezone.utc)
    return response_for(user, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: RefreshRequest, db: DbSession):
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == _refresh_hash(payload.refresh_token)))
    if stored and not stored.revoked_at:
        stored.revoked_at = datetime.now(timezone.utc)
        db.commit()


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(current_user)):
    return UserResponse.model_validate(user, from_attributes=True)
