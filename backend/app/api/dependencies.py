from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import User
from app.core.security import decode_access_token

bearer = HTTPBearer(auto_error=False)
DbSession = Annotated[Session, Depends(get_db)]


def user_from_access_token(raw_token: str, db: Session) -> User:
    try:
        payload = decode_access_token(raw_token)
        user_id = str(payload.get("sub", ""))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token") from exc
    user = db.get(User, user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user is inactive")
    return user


def current_user(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], db: DbSession) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return user_from_access_token(credentials.credentials, db)


def asset_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: DbSession,
    token: str | None = Query(default=None, min_length=20),
) -> User:
    """Resolve an asset request from a header or a short-lived URL token.

    Browsers do not attach Authorization headers to an <img> element. The
    query-token path keeps generated images usable in the chat UI while still
    applying the same user/status checks as regular API requests.
    """
    raw_token = credentials.credentials if credentials else token
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return user_from_access_token(raw_token, db)


def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return user
