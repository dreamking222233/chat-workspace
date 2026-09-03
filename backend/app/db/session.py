from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings


def _engine_kwargs(url: str) -> dict:
    return {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}


settings = get_settings()
if settings.database_url.startswith("sqlite"):
    Path("data").mkdir(exist_ok=True)
Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)

engine = create_engine(settings.database_url, future=True, pool_pre_ping=True, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
