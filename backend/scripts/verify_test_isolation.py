"""Run pytest and prove that it did not write to the configured development DB.

The script takes read-only table-count snapshots of the configured database
before and after a pytest subprocess.  The subprocess receives explicit test
secrets and an isolated SQLite configuration; ``tests/conftest.py`` records the
actual settings/engine/storage paths so this script can verify them as well.

Run from ``backend``::

    python scripts/verify_test_isolation.py -q
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.models import (
    AdminAuditLog,
    Asset,
    Entitlement,
    Export,
    Message,
    ModelChannel,
    ModelRequest,
    Project,
    RefreshToken,
    Thread,
    User,
)


TABLE_MODELS = (
    User,
    RefreshToken,
    Entitlement,
    Project,
    Thread,
    Message,
    Asset,
    ModelRequest,
    Export,
    AdminAuditLog,
    ModelChannel,
)


def _configured_fingerprint() -> str:
    sanitized = make_url(str(engine.url)).set(password=None).render_as_string(hide_password=True)
    return hashlib.sha256(sanitized.encode("utf-8")).hexdigest()


def _snapshot() -> dict[str, Any]:
    with SessionLocal() as db:
        counts = {
            model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
            for model in TABLE_MODELS
        }
    return {
        "database_fingerprint": _configured_fingerprint(),
        "dialect": engine.url.get_backend_name(),
        "counts": counts,
    }


def _resolved_sqlite_path(url_value: str) -> Path:
    url = make_url(url_value)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError(f"test engine is not a file-backed SQLite database: {url.get_backend_name()}")
    return Path(url.database).expanduser().resolve()


def _validate_test_record(
    record_path: Path,
    configured_database: str,
    configured_storage: Path,
) -> dict[str, Any]:
    if not record_path.is_file():
        raise ValueError(f"pytest isolation record is missing: {record_path.name}")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    required = {"database_url", "engine_url", "storage_dir", "test_root", "worker_id"}
    if not required.issubset(record) or any(not isinstance(record[key], str) or not record[key] for key in required):
        raise ValueError("test isolation record is incomplete")
    if record.get("schema_version") != 1:
        raise ValueError("test isolation record has an unsupported schema version")
    process_id = record.get("process_id")
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        raise ValueError("test isolation record has an invalid process ID")
    worker_id = record["worker_id"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", worker_id):
        raise ValueError("test isolation record has an invalid worker ID")
    if record_path.name != f"{worker_id}-{process_id}.json":
        raise ValueError("test isolation record filename does not match its worker and process")
    worker_count = record.get("worker_count")
    if worker_count is not None and (
        isinstance(worker_count, bool) or not isinstance(worker_count, int) or worker_count <= 0
    ):
        raise ValueError("test isolation record has an invalid worker count")
    settings_path = _resolved_sqlite_path(record["database_url"])
    engine_path = _resolved_sqlite_path(record["engine_url"])
    storage_path = Path(record["storage_dir"]).expanduser().resolve()
    test_root = Path(record["test_root"]).expanduser().resolve()
    if settings_path != engine_path:
        raise ValueError("test settings and SQLAlchemy engine point to different databases")
    if not settings_path.is_relative_to(test_root) or not storage_path.is_relative_to(test_root):
        raise ValueError("test database or assets are outside the worker-specific test root")
    configured_url = make_url(configured_database)
    if configured_url.get_backend_name() == "sqlite" and configured_url.database:
        if settings_path == Path(configured_url.database).expanduser().resolve():
            raise ValueError("pytest reused the configured non-test SQLite database")
    if storage_path == configured_storage or configured_storage in storage_path.parents:
        raise ValueError("pytest reused the configured non-test asset directory")
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in test_root.parents:
        raise ValueError("test database or assets are outside the operating-system temporary directory")
    return {
        "database_url": record["database_url"],
        "engine_url": record["engine_url"],
        "process_id": process_id,
        "record_file": record_path.name,
        "storage_dir": str(storage_path),
        "test_root": str(test_root),
        "worker_count": worker_count,
        "worker_id": worker_id,
    }


def _requested_workers(pytest_args: list[str]) -> tuple[bool, int | None]:
    """Return whether xdist was explicitly requested and its fixed count, if any."""

    for index, argument in enumerate(pytest_args):
        value: str | None = None
        if argument in {"-n", "--numprocesses"}:
            if index + 1 < len(pytest_args):
                value = pytest_args[index + 1]
        elif argument.startswith("--numprocesses="):
            value = argument.partition("=")[2]
        elif argument.startswith("-n="):
            value = argument.partition("=")[2]
        elif argument.startswith("-n") and len(argument) > 2:
            value = argument[2:]
        if value is None:
            continue
        if value.isdigit():
            count = int(value)
            return count > 0, count
        return True, None
    return False, None


def _validate_test_records(
    record_dir: Path,
    configured_database: str,
    configured_storage: Path,
    *,
    parallel_requested: bool,
    requested_worker_count: int | None,
) -> dict[str, Any]:
    if not record_dir.is_dir():
        raise ValueError("pytest did not create CHAT_TEST_ISOLATION_RECORD_DIR")
    record_paths = sorted(record_dir.glob("*.json"))
    if not record_paths:
        raise ValueError("pytest did not write any test isolation records")
    records = [
        _validate_test_record(record_path, configured_database, configured_storage)
        for record_path in record_paths
    ]

    worker_ids = [record["worker_id"] for record in records]
    process_ids = [record["process_id"] for record in records]
    database_paths = [_resolved_sqlite_path(record["database_url"]) for record in records]
    storage_paths = [Path(record["storage_dir"]) for record in records]
    test_roots = [Path(record["test_root"]) for record in records]
    for label, values in (
        ("worker IDs", worker_ids),
        ("process IDs", process_ids),
        ("database paths", database_paths),
        ("asset paths", storage_paths),
        ("test roots", test_roots),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"test isolation records contain duplicate {label}")

    controller_records = [record for record in records if record["worker_id"] == "main"]
    worker_records = [record for record in records if record["worker_id"] != "main"]
    if len(controller_records) != 1:
        raise ValueError("pytest must write exactly one controller/serial isolation record")
    if controller_records[0]["worker_count"] is not None:
        raise ValueError("pytest controller/serial record unexpectedly declares a worker count")

    declared_worker_count = 0
    if worker_records:
        declared_counts = {record["worker_count"] for record in worker_records}
        if None in declared_counts or len(declared_counts) != 1:
            raise ValueError("pytest workers did not declare one consistent worker count")
        declared_worker_count = int(declared_counts.pop())
        if len(worker_records) != declared_worker_count:
            raise ValueError(
                "pytest worker isolation records are incomplete: "
                f"expected {declared_worker_count}, found {len(worker_records)}"
            )
        expected_worker_ids = {f"gw{index}" for index in range(declared_worker_count)}
        actual_worker_ids = {record["worker_id"] for record in worker_records}
        if actual_worker_ids != expected_worker_ids:
            raise ValueError(
                "pytest worker isolation record set is incomplete: "
                f"expected {sorted(expected_worker_ids)}, found {sorted(actual_worker_ids)}"
            )
    elif parallel_requested:
        raise ValueError("parallel pytest was requested but no worker isolation records were written")

    if requested_worker_count is not None and declared_worker_count != requested_worker_count:
        raise ValueError(
            "pytest worker isolation records do not match the requested worker count: "
            f"expected {requested_worker_count}, found {declared_worker_count}"
        )

    return {
        "declared_worker_count": declared_worker_count,
        "parallel_requested": parallel_requested,
        "record_count": len(records),
        "record_directory": str(record_dir),
        "records": sorted(records, key=lambda record: (record["worker_id"] != "main", record["worker_id"])),
        "requested_worker_count": requested_worker_count,
        "worker_ids": sorted(record["worker_id"] for record in worker_records),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    args, pytest_args = parser.parse_known_args()
    args.pytest_args = pytest_args
    return args


def main() -> int:
    args = parse_args()
    before = _snapshot()
    configured_database = str(engine.url)
    configured_storage = Path(get_settings().storage_dir).expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="chat-workspace-isolation-check-") as temp_value:
        temp_root = Path(temp_value).resolve()
        record_dir = temp_root / "records"
        env = os.environ.copy()
        env.pop("CHAT_TEST_ISOLATION_RECORD", None)
        env.update(
            {
                "CHAT_DATABASE_URL": f"sqlite:///{temp_root / 'requested-test.sqlite3'}",
                "CHAT_STORAGE_DIR": str(temp_root / "requested-assets"),
                "CHAT_JWT_SECRET": secrets.token_hex(32),
                "CHAT_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
                "CHAT_MODEL_CHANNELS_JSON": "",
                "CHAT_RATE_LIMIT_AUTH_REQUESTS": "10000",
                "CHAT_RATE_LIMIT_MODEL_REQUESTS": "10000",
                "CHAT_TEST_ISOLATION_RECORD_DIR": str(record_dir),
            }
        )
        pytest_args = args.pytest_args or ["-q"]
        parallel_requested, requested_worker_count = _requested_workers(pytest_args)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *pytest_args],
            cwd=BACKEND_ROOT,
            env=env,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if completed.stdout:
            print(completed.stdout, file=sys.stderr, end="" if completed.stdout.endswith("\n") else "\n")
        try:
            test_records = _validate_test_records(
                record_dir,
                configured_database,
                configured_storage,
                parallel_requested=parallel_requested,
                requested_worker_count=requested_worker_count,
            )
        except Exception as exc:
            test_records = {"error": str(exc)}

    after = _snapshot()
    unchanged = before == after
    record_valid = "error" not in test_records
    result = {
        "pytest_exit_code": completed.returncode,
        "configured_database_fingerprint": before["database_fingerprint"],
        "configured_database_dialect": before["dialect"],
        "configured_table_counts_before": before["counts"],
        "configured_table_counts_after": after["counts"],
        "configured_database_unchanged": unchanged,
        "test_isolation_records": test_records,
        "test_isolation_valid": record_valid,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if completed.returncode == 0 and unchanged and record_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
