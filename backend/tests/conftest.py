"""Make the test suite reproducible without a developer MySQL instance."""

from __future__ import annotations

import atexit
import base64
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import pytest


_WORKER_ID = os.environ.get("PYTEST_XDIST_WORKER", "main")
_TEST_ROOT = Path(tempfile.mkdtemp(prefix=f"chat-workspace-tests-{_WORKER_ID}-"))
_TEST_DATABASE = (_TEST_ROOT / "test.sqlite3").resolve()
_TEST_STORAGE = (_TEST_ROOT / "assets").resolve()

# These assignments must happen before any application import. Never inherit a
# developer or CI database URL into the interface tests.
os.environ["CHAT_ENVIRONMENT"] = "test"
os.environ["CHAT_DATABASE_URL"] = f"sqlite:///{_TEST_DATABASE}"
os.environ["CHAT_STORAGE_DIR"] = str(_TEST_STORAGE)
os.environ["CHAT_JWT_SECRET"] = "pytest-only-jwt-secret"
os.environ["CHAT_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"\0" * 32).decode()
os.environ["CHAT_MODEL_CHANNELS_JSON"] = ""
os.environ["CHAT_ADMIN_EMAIL"] = "admin@example.com"
os.environ["CHAT_ADMIN_PASSWORD"] = "change-me-now"
os.environ["CHAT_RATE_LIMIT_AUTH_REQUESTS"] = "10000"
os.environ["CHAT_RATE_LIMIT_MODEL_REQUESTS"] = "10000"

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from sqlalchemy import select  # noqa: E402

from app.api import channels as channels_api  # noqa: E402
from app.api import workspace as workspace_api  # noqa: E402
from app.core.limits import limiter  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import initialize_database  # noqa: E402
from app.models import Base, User  # noqa: E402

_settings = get_settings()
_engine_database = Path(str(engine.url.database or "")).resolve()
assert _settings.database_url == f"sqlite:///{_TEST_DATABASE}"
assert engine.url.get_backend_name() == "sqlite"
assert _engine_database == _TEST_DATABASE
assert Path(_settings.storage_dir).resolve() == _TEST_STORAGE
assert _TEST_DATABASE.is_relative_to(_TEST_ROOT.resolve())
assert _TEST_STORAGE.is_relative_to(_TEST_ROOT.resolve())
assert _settings.model_channels_json == ""

_isolation_record_dir = os.environ.get("CHAT_TEST_ISOLATION_RECORD_DIR", "").strip()
if _isolation_record_dir:
    # A directory, rather than one shared file, is intentional: pytest-xdist
    # imports this conftest in the controller and in every worker process.  A
    # worker-and-PID-specific file provides durable evidence for every process
    # without allowing later workers to overwrite earlier records.
    record_dir = Path(_isolation_record_dir).expanduser().resolve()
    record_dir.mkdir(parents=True, exist_ok=True)
    safe_worker_id = re.sub(r"[^A-Za-z0-9_.-]", "_", _WORKER_ID)
    process_id = os.getpid()
    record_path = record_dir / f"{safe_worker_id}-{process_id}.json"
    worker_count_value = os.environ.get("PYTEST_XDIST_WORKER_COUNT", "").strip()
    worker_count = int(worker_count_value) if worker_count_value.isdigit() else None
    with record_path.open("x", encoding="utf-8", errors="strict") as record_file:
        record_file.write(
            json.dumps(
            {
                "schema_version": 1,
                "database_url": f"sqlite:///{_TEST_DATABASE}",
                "engine_url": f"sqlite:///{_engine_database}",
                "process_id": process_id,
                "storage_dir": str(_TEST_STORAGE),
                "test_root": str(_TEST_ROOT.resolve()),
                "worker_count": worker_count,
                "worker_id": _WORKER_ID,
            },
            sort_keys=True,
            )
        )


def _clear_process_state() -> None:
    """Remove process-local request state that must not cross test cases."""

    workspace_api.active_streams.clear()
    workspace_api.stream_states.clear()
    workspace_api.idempotency_requests.clear()
    channels_api._channel_sync_states.clear()
    with limiter._lock:
        limiter._events.clear()


@pytest.fixture(scope="function", autouse=True)
def isolated_database_per_test():
    """Rebuild the worker-local database and assets before every test.

    Initializing the application here, rather than relying solely on a
    function-local ``TestClient`` context, also supports clients whose lifetime
    starts during module collection. Entering a normal ``TestClient`` context
    remains safe because the application's database initializer is idempotent.
    """

    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    shutil.rmtree(_TEST_STORAGE, ignore_errors=True)
    _TEST_STORAGE.mkdir(parents=True, exist_ok=True)
    _clear_process_state()
    initialize_database()

    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.email == _settings.admin_email.lower()))
        assert admin is not None
        assert admin.role == "admin"

    yield

    _clear_process_state()
    engine.dispose()


@atexit.register
def _remove_test_root() -> None:
    engine.dispose()
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
