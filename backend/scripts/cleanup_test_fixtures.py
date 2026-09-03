"""Audit and, with an approved primary-key manifest, remove test fixtures.

The default mode is read-only and only reports candidates.  Candidate names or
email patterns are never accepted as deletion criteria.  Applying a cleanup
requires an explicit manifest, readable database and asset backups, and the
fingerprint printed by a prior audit of the same database.

Run from ``backend``::

    python scripts/cleanup_test_fixtures.py --output ../data/mock-data-audit.json

    python scripts/cleanup_test_fixtures.py --apply \
      --manifest /absolute/path/approved-manifest.json \
      --database-backup /absolute/path/database.sql \
      --asset-backup /absolute/path/assets.tar.gz \
      --confirm-fingerprint SHA256_FROM_AUDIT
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
import tarfile
from typing import Any

from sqlalchemy import create_engine, delete, func, or_, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

TEST_EMAIL = re.compile(
    r"^(?:(?:test|isolated|stream|missing-channel|no-channel|channel-mismatch|"
    r"regenerate|regenerate-replay|regenerate-no-channel|platform-tool|"
    r"stop-image|stop-tool-image|archived-project|x|z|q|r|live|final)-"
    r"[0-9a-f]{32}@example\.com|(?:tester|flow)-[0-9a-f]{8}@example\.com|"
    r"dbg-[0-9a-f]{32}@e\.com)$"
)

MANIFEST_SCHEMA_VERSION = 2
MANIFEST_KEYS = {
    "user_ids",
    "thread_ids",
    "message_ids",
    "request_ids",
    "asset_ids",
    "audit_log_ids",
}

APPROVED_PRIMARY_KEY_KEYS = {
    *MANIFEST_KEYS,
    "refresh_token_ids",
    "entitlement_ids",
    "project_ids",
    "export_ids",
    "entitlement_reference_ids",
    "channel_reference_ids",
    "project_reference_thread_ids",
    "asset_reference_message_ids",
}

PRIMARY_KEY_TABLES = {
    "user_ids": User,
    "refresh_token_ids": RefreshToken,
    "entitlement_ids": Entitlement,
    "project_ids": Project,
    "thread_ids": Thread,
    "message_ids": Message,
    "request_ids": ModelRequest,
    "export_ids": Export,
    "asset_ids": Asset,
    "audit_log_ids": AdminAuditLog,
    "entitlement_reference_ids": Entitlement,
    "channel_reference_ids": ModelChannel,
    "project_reference_thread_ids": Thread,
    "asset_reference_message_ids": Message,
}

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SQLITE_BACKUP_METADATA_PREFIX = "-- cleanup-backup-target: "

REPORT_FIELDS: dict[str, tuple[str, ...]] = {
    "users": ("id", "email", "display_name", "created_at"),
    "refresh_tokens": ("id", "user_id", "expires_at", "revoked_at", "created_at"),
    "entitlements": ("id", "user_id", "granted_by", "status", "created_at"),
    "projects": ("id", "user_id", "name", "created_at"),
    "threads": ("id", "user_id", "project_id", "title", "model", "created_at"),
    "messages": ("id", "thread_id", "user_id", "role", "created_at"),
    "model_requests": ("id", "thread_id", "message_id", "user_id", "model", "status", "created_at"),
    "exports": ("id", "thread_id", "user_id", "storage_key", "status", "created_at"),
    "assets": ("id", "user_id", "message_id", "storage_key", "created_at"),
    "admin_audit_logs": ("id", "admin_id", "target_user_id", "action", "created_at"),
    "model_channels": ("id", "name", "created_by", "created_at"),
}


def database_fingerprint() -> str:
    """Return a stable hash without exposing credentials in reports."""

    url = make_url(str(engine.url)).set(password=None)
    normalized = url.render_as_string(hide_password=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def table_counts(db) -> dict[str, int]:
    return {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in TABLE_MODELS
    }


def database_target_evidence() -> dict[str, Any]:
    """Return non-secret target fields that a restore artifact can prove."""

    url = make_url(str(engine.url))
    database = str(url.database or "")
    if url.get_backend_name() == "sqlite" and database and database != ":memory:":
        database = str(Path(database).expanduser().resolve())
    return {
        "dialect": url.get_backend_name(),
        "host": (url.host or "").lower(),
        "port": url.port,
        "database": database,
    }


def _canonical_state_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, dict):
        return {
            str(key): _canonical_state_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_state_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def database_audit_state(db) -> dict[str, Any]:
    """Hash every persisted row/column without exposing field values.

    This deliberately covers all cleanup-related tables, rather than only row
    counts.  A changed foreign key, JSON asset reference, or newly inserted
    relation therefore invalidates an approval made against an older audit.
    """

    digest = hashlib.sha256()
    digest.update(b"cleanup-audit-state-v1\n")
    counts: dict[str, int] = {}
    for model in TABLE_MODELS:
        table = model.__table__
        columns = list(table.columns)
        schema = [
            {
                "name": column.name,
                "type": str(column.type),
                "nullable": bool(column.nullable),
                "primary_key": bool(column.primary_key),
            }
            for column in columns
        ]
        digest.update(
            json.dumps(
                {"table": table.name, "schema": schema},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")

        rows = db.scalars(select(model).order_by(model.id)).all()
        counts[table.name] = len(rows)
        for row in rows:
            payload = {
                column.name: _canonical_state_value(getattr(row, column.name))
                for column in columns
            }
            digest.update(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            digest.update(b"\n")

    return {
        "version": 1,
        "digest": digest.hexdigest(),
        "table_counts": counts,
    }


def _serialize_rows(rows: list[Any], fields: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    return [{field: getattr(row, field) for field in fields} for row in rows[:limit]]


def _ordered_rows(rows_by_id: dict[str, Any]) -> list[Any]:
    """Return deterministic rows after candidates from several edges are merged."""

    return sorted(rows_by_id.values(), key=lambda row: (str(row.created_at), row.id))


def _message_asset_ids(messages: list[Message]) -> set[str]:
    """Collect explicit asset references stored on message payloads."""

    return {
        str(asset_id)
        for message in messages
        for asset_id in (message.asset_ids_json or [])
        if asset_id is not None and str(asset_id).strip()
    }


def _messages_referencing_assets(
    db,
    asset_ids: set[str],
    *,
    excluded_message_ids: set[str] | None = None,
) -> list[Message]:
    """Return surviving messages whose JSON payload references deleted assets.

    ``asset_ids_json`` is deliberately inspected through the ORM instead of a
    dialect-specific JSON expression.  Cleanup is an offline operation and a
    full scan keeps the approval closure identical on SQLite and MySQL.
    """

    if not asset_ids:
        return []
    excluded = excluded_message_ids or set()
    return [
        message
        for message in db.scalars(select(Message).order_by(Message.id)).all()
        if message.id not in excluded
        and asset_ids.intersection(_message_asset_ids([message]))
    ]


def _row_map(rows: list[Any]) -> dict[str, Any]:
    return {row.id: row for row in rows}


def _merge_groups(*groups: dict[str, list[Any]]) -> dict[str, list[Any]]:
    """Merge report groups by table and primary key without double counting."""

    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for table, rows in group.items():
            merged.setdefault(table, {}).update((row.id, row) for row in rows)
    return {table: _ordered_rows(rows) for table, rows in merged.items()}


def _subtract_group(source: dict[str, list[Any]], excluded: dict[str, list[Any]]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for table, rows in source.items():
        excluded_ids = {row.id for row in excluded.get(table, [])}
        remaining = [row for row in rows if row.id not in excluded_ids]
        if remaining:
            result[table] = remaining
    return result


def _serialize_group(group: dict[str, list[Any]], limit: int) -> dict[str, dict[str, Any]]:
    return {
        table: {
            "count": len(rows),
            "rows": _serialize_rows(rows, REPORT_FIELDS[table], limit),
        }
        for table, rows in group.items()
    }


def _classification_bucket(group: dict[str, list[Any]] | dict[str, int]) -> dict[str, Any]:
    if not group:
        return {"total": 0, "by_table": {}}
    counts = {
        table: len(rows) if isinstance(rows, list) else int(rows)
        for table, rows in group.items()
        if (len(rows) if isinstance(rows, list) else int(rows)) > 0
    }
    return {"total": sum(counts.values()), "by_table": dict(sorted(counts.items()))}


def _expand_message_request_graph(
    db,
    messages_by_id: dict[str, Message],
    requests_by_id: dict[str, ModelRequest],
) -> tuple[dict[str, Message], dict[str, ModelRequest]]:
    """Follow message/request and request parent/child edges to a fixed point."""

    while True:
        previous_counts = (len(messages_by_id), len(requests_by_id))
        message_ids = set(messages_by_id)
        request_ids = set(requests_by_id)

        missing_message_ids = {
            request.message_id
            for request in requests_by_id.values()
            if request.message_id and request.message_id not in message_ids
        }
        if missing_message_ids:
            linked_messages = db.scalars(select(Message).where(Message.id.in_(missing_message_ids))).all()
            messages_by_id.update((message.id, message) for message in linked_messages)

        message_ids = set(messages_by_id)
        conditions = []
        if message_ids:
            conditions.append(ModelRequest.message_id.in_(message_ids))
        if request_ids:
            conditions.append(ModelRequest.parent_request_id.in_(request_ids))
            missing_parent_ids = {
                request.parent_request_id
                for request in requests_by_id.values()
                if request.parent_request_id and request.parent_request_id not in request_ids
            }
            if missing_parent_ids:
                parents = db.scalars(select(ModelRequest).where(ModelRequest.id.in_(missing_parent_ids))).all()
                requests_by_id.update((request.id, request) for request in parents)
        if conditions:
            linked_requests = db.scalars(select(ModelRequest).where(or_(*conditions))).all()
            requests_by_id.update((request.id, request) for request in linked_requests)

        if previous_counts == (len(messages_by_id), len(requests_by_id)):
            return messages_by_id, requests_by_id


def _expand_apply_request_ids(db, message_ids: set[str], request_ids: set[str]) -> set[str]:
    """Expand deletes only to requests owned by selected messages/requests.

    Audit traversal is intentionally bidirectional so reviewers see the whole
    impact. Apply traversal is narrower: selecting a request never implicitly
    authorizes deleting its message or parent request.
    """

    while True:
        conditions = []
        if message_ids:
            conditions.append(ModelRequest.message_id.in_(message_ids))
        if request_ids:
            conditions.append(ModelRequest.parent_request_id.in_(request_ids))
        if not conditions:
            return request_ids
        linked_ids = set(db.scalars(select(ModelRequest.id).where(or_(*conditions))).all())
        previous_count = len(request_ids)
        request_ids.update(linked_ids)
        if len(request_ids) == previous_count:
            return request_ids


def _normalize_primary_keys(value: dict[str, Any], keys: set[str], label: str) -> dict[str, list[str]]:
    unknown = set(value) - keys
    missing = keys - set(value)
    if unknown or missing:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unknown:
            details.append(f"unsupported {sorted(unknown)}")
        raise ValueError(f"{label} must contain the exact primary-key fields ({'; '.join(details)})")
    normalized: dict[str, list[str]] = {}
    for key in keys:
        values = value[key]
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise ValueError(f"{label}.{key} must be a list of non-empty primary-key strings")
        normalized[key] = sorted(set(values))
    return normalized


def _require_existing_roots(db, roots: dict[str, set[str]]) -> None:
    root_models = {
        "user_ids": User,
        "thread_ids": Thread,
        "message_ids": Message,
        "request_ids": ModelRequest,
        "asset_ids": Asset,
        "audit_log_ids": AdminAuditLog,
    }
    for key, model in root_models.items():
        requested = roots[key]
        if not requested:
            continue
        existing = set(db.scalars(select(model.id).where(model.id.in_(requested))).all())
        missing = requested - existing
        if missing:
            raise ValueError(f"approved root primary keys no longer exist for {key}: {sorted(missing)}")


def approved_primary_key_closure(db, roots_value: dict[str, Any]) -> dict[str, list[str]]:
    """Compute the exact rows deleted or reference-updated by root approval."""

    roots = _normalize_primary_keys(roots_value, MANIFEST_KEYS, "manifest roots")
    root_sets = {key: set(values) for key, values in roots.items()}
    _require_existing_roots(db, root_sets)

    user_ids = root_sets["user_ids"]
    thread_ids = set(root_sets["thread_ids"])
    message_ids = set(root_sets["message_ids"])
    request_ids = set(root_sets["request_ids"])
    asset_ids = set(root_sets["asset_ids"])
    audit_ids = set(root_sets["audit_log_ids"])
    refresh_token_ids: set[str] = set()
    entitlement_ids: set[str] = set()
    project_ids: set[str] = set()
    export_ids: set[str] = set()
    entitlement_reference_ids: set[str] = set()
    channel_reference_ids: set[str] = set()
    project_reference_thread_ids: set[str] = set()
    asset_reference_message_ids: set[str] = set()

    if user_ids:
        refresh_token_ids.update(
            db.scalars(select(RefreshToken.id).where(RefreshToken.user_id.in_(user_ids))).all()
        )
        entitlement_ids.update(
            db.scalars(select(Entitlement.id).where(Entitlement.user_id.in_(user_ids))).all()
        )
        project_ids.update(db.scalars(select(Project.id).where(Project.user_id.in_(user_ids))).all())
        thread_ids.update(db.scalars(select(Thread.id).where(Thread.user_id.in_(user_ids))).all())
        message_ids.update(db.scalars(select(Message.id).where(Message.user_id.in_(user_ids))).all())
        request_ids.update(
            db.scalars(select(ModelRequest.id).where(ModelRequest.user_id.in_(user_ids))).all()
        )
        export_ids.update(db.scalars(select(Export.id).where(Export.user_id.in_(user_ids))).all())
        asset_ids.update(db.scalars(select(Asset.id).where(Asset.user_id.in_(user_ids))).all())
        audit_ids.update(
            db.scalars(
                select(AdminAuditLog.id).where(
                    or_(
                        AdminAuditLog.admin_id.in_(user_ids),
                        AdminAuditLog.target_user_id.in_(user_ids),
                    )
                )
            ).all()
        )
        entitlement_reference_ids.update(
            db.scalars(select(Entitlement.id).where(Entitlement.granted_by.in_(user_ids))).all()
        )
        channel_reference_ids.update(
            db.scalars(select(ModelChannel.id).where(ModelChannel.created_by.in_(user_ids))).all()
        )
    if thread_ids:
        message_ids.update(db.scalars(select(Message.id).where(Message.thread_id.in_(thread_ids))).all())
        request_ids.update(
            db.scalars(select(ModelRequest.id).where(ModelRequest.thread_id.in_(thread_ids))).all()
        )
        export_ids.update(db.scalars(select(Export.id).where(Export.thread_id.in_(thread_ids))).all())

    if project_ids:
        project_reference_thread_ids.update(
            db.scalars(select(Thread.id).where(Thread.project_id.in_(project_ids))).all()
        )
        # Threads already approved for deletion do not also need a reference
        # update. Keeping both actions disjoint makes exact row counts useful.
        project_reference_thread_ids.difference_update(thread_ids)

    request_ids = _expand_apply_request_ids(db, message_ids, request_ids)
    if message_ids:
        selected_messages = db.scalars(select(Message).where(Message.id.in_(message_ids))).all()
        asset_ids.update(db.scalars(select(Asset.id).where(Asset.message_id.in_(message_ids))).all())
        referenced_asset_ids = _message_asset_ids(selected_messages)
        if referenced_asset_ids:
            asset_ids.update(
                db.scalars(select(Asset.id).where(Asset.id.in_(referenced_asset_ids))).all()
            )

    asset_reference_message_ids.update(
        message.id
        for message in _messages_referencing_assets(
            db,
            asset_ids,
            excluded_message_ids=message_ids,
        )
    )

    # Owned entitlements are deleted; only surviving rows need their audit
    # reference cleared. Keep these two actions disjoint in the approval.
    entitlement_reference_ids.difference_update(entitlement_ids)
    closure = {
        "user_ids": user_ids,
        "thread_ids": thread_ids,
        "message_ids": message_ids,
        "request_ids": request_ids,
        "asset_ids": asset_ids,
        "audit_log_ids": audit_ids,
        "refresh_token_ids": refresh_token_ids,
        "entitlement_ids": entitlement_ids,
        "project_ids": project_ids,
        "export_ids": export_ids,
        "entitlement_reference_ids": entitlement_reference_ids,
        "channel_reference_ids": channel_reference_ids,
        "project_reference_thread_ids": project_reference_thread_ids,
        "asset_reference_message_ids": asset_reference_message_ids,
    }
    return {key: sorted(closure[key]) for key in sorted(APPROVED_PRIMARY_KEY_KEYS)}


def build_manifest_binding(roots_value: dict[str, Any]) -> dict[str, Any]:
    """Create reproducible v2 fields for reviewer approval from one snapshot."""

    with SessionLocal() as db:
        audit_state = database_audit_state(db)
        closure = approved_primary_key_closure(db, roots_value)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "database_fingerprint": database_fingerprint(),
        "audit_state": audit_state,
        "approved_primary_keys": closure,
    }


def _linked_assets(db, messages: list[Message], seed_assets: list[Asset] | None = None) -> list[Asset]:
    assets_by_id = _row_map(seed_assets or [])
    message_ids = {message.id for message in messages}
    referenced_asset_ids = _message_asset_ids(messages)
    conditions = []
    if message_ids:
        conditions.append(Asset.message_id.in_(message_ids))
    if referenced_asset_ids:
        conditions.append(Asset.id.in_(referenced_asset_ids))
    if conditions:
        linked = db.scalars(select(Asset).where(or_(*conditions))).all()
        assets_by_id.update((asset.id, asset) for asset in linked)
    return _ordered_rows(assets_by_id)


def _local_demo_closure(db, local_threads: list[Thread]) -> dict[str, list[Any]]:
    """Build the report-only relational closure rooted at every local marker."""

    thread_ids = {thread.id for thread in local_threads}
    message_conditions = [
        Message.id.like("local-%"),
        Message.content.like("%local-%"),
        Message.content.like("%本地演示回复%"),
        Message.content.like("%已切换演示回复%"),
    ]
    if thread_ids:
        message_conditions.append(Message.thread_id.in_(thread_ids))
    messages = db.scalars(select(Message).where(or_(*message_conditions))).all()

    marker_assets = db.scalars(
        select(Asset).where(or_(Asset.id.like("local-%"), Asset.storage_key.like("%local-%")))
    ).all()
    marker_asset_message_ids = {asset.message_id for asset in marker_assets if asset.message_id}
    if marker_asset_message_ids:
        messages.extend(db.scalars(select(Message).where(Message.id.in_(marker_asset_message_ids))).all())

    request_conditions = [ModelRequest.id.like("local-%"), ModelRequest.model.like("local-%")]
    if thread_ids:
        request_conditions.append(ModelRequest.thread_id.in_(thread_ids))
    requests = db.scalars(select(ModelRequest).where(or_(*request_conditions))).all()
    messages_by_id, requests_by_id = _expand_message_request_graph(db, _row_map(messages), _row_map(requests))

    assets = _linked_assets(db, _ordered_rows(messages_by_id), marker_assets)

    export_conditions = [Export.id.like("local-%"), Export.storage_key.like("%local-%")]
    if thread_ids:
        export_conditions.append(Export.thread_id.in_(thread_ids))
    exports = db.scalars(select(Export).where(or_(*export_conditions))).all()

    return {
        "threads": local_threads,
        "messages": _ordered_rows(messages_by_id),
        "model_requests": _ordered_rows(requests_by_id),
        "assets": assets,
        "exports": _ordered_rows(_row_map(exports)),
    }


def _test_user_impact(db, test_users: list[User]) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    """Report every row that an approved test-user cleanup would affect."""

    user_ids = {user.id for user in test_users}
    empty_impact = {
        "users": [],
        "refresh_tokens": [],
        "entitlements": [],
        "projects": [],
        "threads": [],
        "messages": [],
        "model_requests": [],
        "exports": [],
        "assets": [],
        "admin_audit_logs": [],
    }
    if not user_ids:
        return empty_impact, {
            "entitlements": [],
            "model_channels": [],
            "threads": [],
            "messages": [],
        }

    refresh_tokens = db.scalars(select(RefreshToken).where(RefreshToken.user_id.in_(user_ids))).all()
    entitlements = db.scalars(select(Entitlement).where(Entitlement.user_id.in_(user_ids))).all()
    projects = db.scalars(select(Project).where(Project.user_id.in_(user_ids))).all()
    threads = db.scalars(select(Thread).where(Thread.user_id.in_(user_ids))).all()
    thread_ids = {thread.id for thread in threads}

    message_conditions = [Message.user_id.in_(user_ids)]
    request_conditions = [ModelRequest.user_id.in_(user_ids)]
    export_conditions = [Export.user_id.in_(user_ids)]
    if thread_ids:
        message_conditions.append(Message.thread_id.in_(thread_ids))
        request_conditions.append(ModelRequest.thread_id.in_(thread_ids))
        export_conditions.append(Export.thread_id.in_(thread_ids))
    messages = db.scalars(select(Message).where(or_(*message_conditions))).all()
    requests = db.scalars(select(ModelRequest).where(or_(*request_conditions))).all()
    messages_by_id, requests_by_id = _expand_message_request_graph(db, _row_map(messages), _row_map(requests))

    owned_assets = db.scalars(select(Asset).where(Asset.user_id.in_(user_ids))).all()
    assets = _linked_assets(db, _ordered_rows(messages_by_id), owned_assets)
    exports = db.scalars(select(Export).where(or_(*export_conditions))).all()
    audit_logs = db.scalars(
        select(AdminAuditLog).where(
            or_(AdminAuditLog.admin_id.in_(user_ids), AdminAuditLog.target_user_id.in_(user_ids))
        )
    ).all()

    impact = {
        "users": test_users,
        "refresh_tokens": _ordered_rows(_row_map(refresh_tokens)),
        "entitlements": _ordered_rows(_row_map(entitlements)),
        "projects": _ordered_rows(_row_map(projects)),
        "threads": _ordered_rows(_row_map(threads)),
        "messages": _ordered_rows(messages_by_id),
        "model_requests": _ordered_rows(requests_by_id),
        "exports": _ordered_rows(_row_map(exports)),
        "assets": assets,
        "admin_audit_logs": _ordered_rows(_row_map(audit_logs)),
    }

    owned_entitlement_ids = set(_row_map(entitlements))
    external_grants = db.scalars(
        select(Entitlement).where(
            Entitlement.granted_by.in_(user_ids),
            Entitlement.id.not_in(owned_entitlement_ids) if owned_entitlement_ids else True,
        )
    ).all()
    channels = db.scalars(select(ModelChannel).where(ModelChannel.created_by.in_(user_ids))).all()
    project_ids = set(_row_map(projects))
    external_project_threads = (
        db.scalars(
            select(Thread).where(Thread.project_id.in_(project_ids))
        ).all()
        if project_ids
        else []
    )
    owned_thread_ids = set(_row_map(threads))
    external_project_threads = [
        thread for thread in external_project_threads if thread.id not in owned_thread_ids
    ]
    selected_message_ids = set(messages_by_id)
    asset_reference_messages = _messages_referencing_assets(
        db,
        set(_row_map(assets)),
        excluded_message_ids=selected_message_ids,
    )
    reference_updates = {
        "entitlements": _ordered_rows(_row_map(external_grants)),
        "model_channels": _ordered_rows(_row_map(channels)),
        "threads": _ordered_rows(_row_map(external_project_threads)),
        "messages": _ordered_rows(_row_map(asset_reference_messages)),
    }
    return impact, reference_updates


def build_audit(
    limit: int,
    *,
    deleted: dict[str, int] | None = None,
    mode: str = "audit",
) -> dict[str, Any]:
    with SessionLocal() as db:
        audit_state = database_audit_state(db)
        users = db.scalars(select(User).order_by(User.created_at)).all()
        test_users = [user for user in users if TEST_EMAIL.fullmatch(user.email or "")]

        local_threads = db.scalars(
            select(Thread)
            .where(or_(Thread.model.like("local-%"), Thread.id.like("local-%")))
            .order_by(Thread.created_at)
        ).all()
        local_impact = _local_demo_closure(db, local_threads)
        test_impact, reference_updates = _test_user_impact(db, test_users)
        retained = _merge_groups(local_impact, test_impact, reference_updates)
        unconfirmed_local = _subtract_group(local_impact, test_impact)
        needs_manual_migration = _merge_groups(unconfirmed_local, reference_updates)

        candidates = {
            "test_users": {
                "count": len(test_users),
                "rows": _serialize_rows(test_users, REPORT_FIELDS["users"], limit),
            },
            "local_demo_threads": {
                "count": len(local_impact["threads"]),
                "rows": _serialize_rows(local_impact["threads"], REPORT_FIELDS["threads"], limit),
            },
            "local_demo_messages": {
                "count": len(local_impact["messages"]),
                "rows": _serialize_rows(local_impact["messages"], REPORT_FIELDS["messages"], limit),
            },
            "local_demo_requests": {
                "count": len(local_impact["model_requests"]),
                "rows": _serialize_rows(local_impact["model_requests"], REPORT_FIELDS["model_requests"], limit),
            },
            "local_demo_assets": {
                "count": len(local_impact["assets"]),
                "rows": _serialize_rows(local_impact["assets"], REPORT_FIELDS["assets"], limit),
            },
            "local_demo_exports": {
                "count": len(local_impact["exports"]),
                "rows": _serialize_rows(local_impact["exports"], REPORT_FIELDS["exports"], limit),
            },
        }
        classification = {
            "deleted": _classification_bucket(deleted or {}),
            "retained": _classification_bucket(retained),
            "needs_manual_migration": _classification_bucket(needs_manual_migration),
        }

        return {
            "mode": mode,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database_fingerprint": database_fingerprint(),
            "audit_state": audit_state,
            "database_dialect": engine.url.get_backend_name(),
            "storage_root": str(Path(get_settings().storage_dir).resolve()),
            "table_counts": table_counts(db),
            "candidate_policy": {
                "automatic_deletion": False,
                "test_email_pattern": TEST_EMAIL.pattern,
                "local_marker_fields": [
                    "threads.id/model",
                    "messages.id/content",
                    "model_requests.id/model",
                    "assets.id/storage_key",
                    "exports.id/storage_key",
                ],
                "note": "Candidate patterns are report-only; apply mode accepts primary keys only.",
                "relationship_closure": {
                    "roots": "test-user ownership plus rows carrying local-* markers",
                    "edges": [
                        (
                            "users.id -> refresh_tokens/entitlements/projects/threads/"
                            "messages/model_requests/exports/assets.user_id"
                        ),
                        "users.id -> admin_audit_logs.admin_id/target_user_id",
                        "threads.id -> messages/model_requests/exports.thread_id",
                        "projects.id -> surviving threads.project_id (cleared on apply)",
                        "messages.id <-> model_requests.message_id",
                        "model_requests.id <-> model_requests.parent_request_id",
                        "messages.id -> assets.message_id",
                        "messages.asset_ids_json -> assets.id",
                        "assets.id -> surviving messages.asset_ids_json (removed on apply)",
                    ],
                    "scope": (
                        "local-marker candidates are report-only; apply uses the separately "
                        "approved deletion and surviving-reference closure"
                    ),
                },
                "classification": {
                    "deleted": "rows deleted by the immediately preceding confirmed apply, otherwise zero",
                    "retained": "all currently retained test-user impact and local-marker candidates",
                    "needs_manual_migration": (
                        "unconfirmed local-marker rows plus retained references to candidate test users"
                    ),
                },
            },
            "candidates": candidates,
            "test_user_impact": _serialize_group(test_impact, limit),
            "reference_updates": _serialize_group(reference_updates, limit),
            "classification": classification,
            "next_step": (
                "Review retained and needs_manual_migration rows after the confirmed cleanup."
                if mode == "post_apply_audit"
                else "Review candidates and create an approved primary-key manifest; no row was changed."
            ),
        }


def _backup_path(path_value: str, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"{label} must be a readable non-empty file: {path}")
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise ValueError(f"{label} is not readable: {path}") from exc
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sha256(value: Any, label: str) -> str:
    normalized = str(value or "").lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _validate_audit_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "digest", "table_counts"}:
        raise ValueError("manifest audit_state requires version, digest and exact table_counts")
    if value.get("version") != 1:
        raise ValueError("manifest audit_state version is unsupported")
    digest = _validate_sha256(value.get("digest"), "manifest audit_state.digest")
    counts = value.get("table_counts")
    expected_tables = {model.__tablename__ for model in TABLE_MODELS}
    if not isinstance(counts, dict) or set(counts) != expected_tables:
        raise ValueError("manifest audit_state.table_counts must cover every cleanup table")
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts.values()):
        raise ValueError("manifest audit_state.table_counts values must be non-negative integers")
    return {
        "version": 1,
        "digest": digest,
        "table_counts": {table: counts[table] for table in sorted(counts)},
    }


def _load_manifest(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest must be readable UTF-8 JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be a JSON object")
    if raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            "manifest schema_version=2 is required; regenerate state and closure from a fresh audit"
        )
    if (
        raw.get("approved") is not True
        or not str(raw.get("approved_by", "")).strip()
        or not str(raw.get("approved_at", "")).strip()
    ):
        raise ValueError("manifest requires approved=true, approved_by and approved_at")
    allowed = MANIFEST_KEYS | {
        "schema_version",
        "approved",
        "approved_by",
        "approved_at",
        "database_fingerprint",
        "audit_state",
        "approved_primary_keys",
        "backup_evidence",
        "note",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"manifest contains unsupported fields: {sorted(unknown)}")

    roots = _normalize_primary_keys(
        {key: raw.get(key) for key in MANIFEST_KEYS},
        MANIFEST_KEYS,
        "manifest roots",
    )
    if not any(roots[key] for key in MANIFEST_KEYS):
        raise ValueError("manifest contains no approved primary keys")
    fingerprint = _validate_sha256(raw.get("database_fingerprint"), "manifest database_fingerprint")
    audit_state = _validate_audit_state(raw.get("audit_state"))
    if not isinstance(raw.get("approved_primary_keys"), dict):
        raise ValueError("manifest approved_primary_keys full closure is required")
    approved_primary_keys = _normalize_primary_keys(
        raw["approved_primary_keys"],
        APPROVED_PRIMARY_KEY_KEYS,
        "manifest approved_primary_keys",
    )
    for key in MANIFEST_KEYS:
        if not set(roots[key]).issubset(approved_primary_keys[key]):
            raise ValueError(f"manifest approved_primary_keys.{key} must include every approved root")

    backup_evidence = raw.get("backup_evidence")
    if not isinstance(backup_evidence, dict) or set(backup_evidence) != {
        "database_sha256",
        "asset_sha256",
    }:
        raise ValueError("manifest backup_evidence requires database_sha256 and asset_sha256")
    raw.update(roots)
    raw["database_fingerprint"] = fingerprint
    raw["audit_state"] = audit_state
    raw["approved_primary_keys"] = approved_primary_keys
    raw["backup_evidence"] = {
        "database_sha256": _validate_sha256(
            backup_evidence.get("database_sha256"),
            "manifest backup_evidence.database_sha256",
        ),
        "asset_sha256": _validate_sha256(
            backup_evidence.get("asset_sha256"),
            "manifest backup_evidence.asset_sha256",
        ),
    }
    return raw


def _created_sql_tables(sql: str) -> set[str]:
    pattern = re.compile(
        r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+"
        r"(?:`([^`]+)`|\"([^\"]+)\"|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))",
        re.IGNORECASE,
    )
    return {next(value for value in match.groups() if value) for match in pattern.finditer(sql)}


def _local_hosts_match(left: str, right: str) -> bool:
    local = {"localhost", "127.0.0.1", "::1"}
    return left == right or (left in local and right in local)


def _mysql_insert_statement(sql: str, table: str) -> str:
    quoted_table = re.escape(table)
    pattern = re.compile(
        rf"\bINSERT\s+INTO\s+(?:`{quoted_table}`|\"{quoted_table}\"|{quoted_table})"
        rf"(?:\s*\([^;]*?\))?\s+VALUES\s+.*?;(?=\s*(?:\n|$))",
        re.IGNORECASE | re.DOTALL,
    )
    return "\n".join(match.group(0) for match in pattern.finditer(sql))


def _assert_mysql_backup_primary_keys(sql: str, approved: dict[str, list[str]]) -> None:
    expected_by_table: dict[str, set[str]] = {}
    for key, ids in approved.items():
        if ids:
            expected_by_table.setdefault(PRIMARY_KEY_TABLES[key].__tablename__, set()).update(ids)
    for table, ids in expected_by_table.items():
        statements = _mysql_insert_statement(sql, table)
        if not statements:
            raise ValueError(f"database backup has no restorable data for approved table {table}")
        for primary_key in ids:
            escaped = re.escape(primary_key)
            # Standard mysqldump places the primary-key `id` first in each
            # VALUES tuple. Requiring tuple position avoids matching a foreign
            # key or JSON string elsewhere in the same INSERT statement.
            pattern = re.compile(rf"(?:\bVALUES\s*|,\s*)\(\s*'{escaped}'\s*(?:,|\))")
            if not pattern.search(statements):
                raise ValueError(
                    f"database backup is missing approved primary key {table}.{primary_key}"
                )


def _validate_sqlite_backup(
    sql: str,
    manifest: dict[str, Any],
    required_tables: set[str],
) -> None:
    first_line = sql.splitlines()[0] if sql.splitlines() else ""
    if not first_line.startswith(SQLITE_BACKUP_METADATA_PREFIX):
        raise ValueError("SQLite SQL backup is missing cleanup target evidence")
    try:
        metadata = json.loads(first_line[len(SQLITE_BACKUP_METADATA_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise ValueError("SQLite SQL backup target evidence is invalid JSON") from exc
    if not isinstance(metadata, dict) or set(metadata) != {
        "database_fingerprint",
        "audit_state_digest",
        "target",
    }:
        raise ValueError("SQLite SQL backup target evidence is incomplete")
    if metadata["database_fingerprint"] != manifest["database_fingerprint"]:
        raise ValueError("SQLite SQL backup belongs to another database fingerprint")
    if metadata["audit_state_digest"] != manifest["audit_state"]["digest"]:
        raise ValueError("SQLite SQL backup was not captured from the approved audit state")
    if metadata["target"] != database_target_evidence():
        raise ValueError("SQLite SQL backup target evidence does not match the configured database")

    restored = sqlite3.connect(":memory:")
    restored_engine = None
    try:
        denied = {
            getattr(sqlite3, "SQLITE_ATTACH", -1),
            getattr(sqlite3, "SQLITE_DETACH", -1),
        }

        def authorize(action, _arg1, _arg2, _database, _trigger):
            return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK

        restored.set_authorizer(authorize)
        restored.executescript(sql)
        restored_tables = {
            row[0]
            for row in restored.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not required_tables.issubset(restored_tables):
            raise ValueError("SQLite SQL backup did not restore every required table")
        for table, expected_count in manifest["audit_state"]["table_counts"].items():
            actual_count = int(restored.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if actual_count != expected_count:
                raise ValueError(
                    f"SQLite SQL backup row count mismatch for {table}: "
                    f"expected {expected_count}, restored {actual_count}"
                )
        for key, ids in manifest["approved_primary_keys"].items():
            if not ids:
                continue
            table = PRIMARY_KEY_TABLES[key].__tablename__
            placeholders = ",".join("?" for _ in ids)
            restored_ids = {
                row[0]
                for row in restored.execute(
                    f'SELECT id FROM "{table}" WHERE id IN ({placeholders})',
                    ids,
                ).fetchall()
            }
            missing = set(ids) - restored_ids
            if missing:
                raise ValueError(
                    f"SQLite SQL backup is missing approved primary keys in {table}: {sorted(missing)}"
                )
        # Counts and selected primary keys alone do not prove that a dump can
        # restore the approved state: an unrelated row could have been edited
        # while preserving both. Load the restored database through the same
        # ORM mappings and compare the complete canonical state digest.
        restored_engine = create_engine(
            "sqlite://",
            creator=lambda: restored,
            future=True,
        )
        with Session(restored_engine) as restored_db:
            restored_state = database_audit_state(restored_db)
        if restored_state != manifest["audit_state"]:
            raise ValueError(
                "SQLite SQL backup contents do not match the approved audit state"
            )
        quick_check = restored.execute("PRAGMA quick_check").fetchone()
        if not quick_check or quick_check[0] != "ok":
            raise ValueError("SQLite SQL backup restored with integrity errors")
        foreign_key_errors = restored.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ValueError("SQLite SQL backup restored with foreign-key errors")
    except (sqlite3.Error, SQLAlchemyError) as exc:
        raise ValueError(f"SQLite SQL backup cannot be restored: {exc}") from exc
    finally:
        if restored_engine is not None:
            restored_engine.dispose()
        restored.close()


def _validate_database_backup(path_value: str, manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = _backup_path(path_value, "database backup")
    digest = _sha256_file(path)
    if digest != manifest["backup_evidence"]["database_sha256"]:
        raise ValueError("database backup SHA-256 does not match the approved manifest")
    try:
        sql = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("database backup must be a readable UTF-8 SQL dump") from exc

    required_tables = {model.__tablename__ for model in TABLE_MODELS}
    created_tables = _created_sql_tables(sql)
    missing_tables = required_tables - created_tables
    if missing_tables:
        raise ValueError(
            f"database backup is missing required table structures: {sorted(missing_tables)}"
        )

    target = database_target_evidence()
    if sql.startswith(SQLITE_BACKUP_METADATA_PREFIX):
        if target["dialect"] != "sqlite":
            raise ValueError("SQLite SQL backup does not match the configured database dialect")
        _validate_sqlite_backup(sql, manifest, required_tables)
        backup_format = "sqlite-sql"
    elif re.search(r"^-- MySQL dump\b", sql, re.MULTILINE):
        if target["dialect"] not in {"mysql", "mariadb"}:
            raise ValueError("MySQL SQL backup does not match the configured database dialect")
        header = re.search(
            r"^-- Host:\s*(\S+)\s+Database:\s*(\S+)\s*$",
            sql,
            re.MULTILINE,
        )
        if not header:
            raise ValueError("MySQL SQL backup is missing Host/Database target evidence")
        backup_host, backup_database = header.groups()
        if not _local_hosts_match(backup_host.lower(), str(target["host"])):
            raise ValueError("MySQL SQL backup host does not match the configured database")
        if backup_database != target["database"]:
            raise ValueError("MySQL SQL backup database does not match the configured database")
        if not re.search(r"^-- Dump completed on .+$", sql, re.MULTILINE):
            raise ValueError("MySQL SQL backup has no completion marker and may be truncated")
        for table, count in manifest["audit_state"]["table_counts"].items():
            if count > 0 and not _mysql_insert_statement(sql, table):
                raise ValueError(f"MySQL SQL backup has no data for non-empty table {table}")
        _assert_mysql_backup_primary_keys(sql, manifest["approved_primary_keys"])
        backup_format = "mysql-dump"
    else:
        raise ValueError("database backup is not a supported complete SQLite or MySQL SQL dump")

    return path, {
        "sha256": digest,
        "format": backup_format,
        "required_tables": sorted(required_tables),
        "target": target,
    }


def _safe_tar_member_name(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise ValueError(f"asset backup contains an unsafe member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"asset backup contains an unsafe member path: {name!r}")
    return path


def _validate_asset_backup(path_value: str, manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = _backup_path(path_value, "asset backup")
    digest = _sha256_file(path)
    if digest != manifest["backup_evidence"]["asset_sha256"]:
        raise ValueError("asset backup SHA-256 does not match the approved manifest")

    member_count = 0
    file_count = 0
    total_file_bytes = 0
    names: set[str] = set()
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise ValueError("asset backup tar.gz contains no restorable members")
            for member in members:
                normalized = str(_safe_tar_member_name(member.name))
                if normalized in names:
                    raise ValueError(f"asset backup contains a duplicate member: {normalized}")
                names.add(normalized)
                member_count += 1
                if member.issym() or member.islnk():
                    raise ValueError(f"asset backup contains a link member: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise ValueError(f"asset backup contains a special member: {member.name}")
                if member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ValueError(f"asset backup file cannot be read: {member.name}")
                    restored_size = 0
                    while chunk := extracted.read(1024 * 1024):
                        restored_size += len(chunk)
                    if restored_size != member.size:
                        raise ValueError(f"asset backup file is truncated: {member.name}")
                    file_count += 1
                    total_file_bytes += restored_size
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError(f"asset backup is not a complete readable tar.gz archive: {exc}") from exc

    return path, {
        "sha256": digest,
        "format": "tar.gz",
        "members": member_count,
        "files": file_count,
        "file_bytes": total_file_bytes,
    }


def _delete_count(db, model, condition) -> int:
    return int(db.execute(delete(model).where(condition)).rowcount or 0)


def _lock_cleanup_state(db) -> None:
    """Prevent relationship changes while state and closure are revalidated."""

    if db.get_bind().dialect.name not in {"mysql", "mariadb", "postgresql"}:
        return
    # A full ordered range lock is intentionally conservative. Cleanup is an
    # offline administrative operation, and preventing new FK/JSON edges is
    # more important than allowing concurrent writes during its short window.
    for model in TABLE_MODELS:
        db.scalars(select(model.id).order_by(model.id).with_for_update()).all()


def _require_exact_count(action: str, actual: int, expected: int) -> int:
    if actual != expected:
        raise ValueError(f"{action} affected {actual} rows; approved closure requires exactly {expected}")
    return actual


def apply_manifest(args: argparse.Namespace) -> dict[str, Any]:
    manifest = _load_manifest(args.manifest)
    fingerprint = database_fingerprint()
    if args.confirm_fingerprint != fingerprint or manifest["database_fingerprint"] != fingerprint:
        raise ValueError("database fingerprint does not match the audited target")
    database_backup, database_backup_validation = _validate_database_backup(
        args.database_backup,
        manifest,
    )
    asset_backup, asset_backup_validation = _validate_asset_backup(args.asset_backup, manifest)

    roots = {key: manifest[key] for key in MANIFEST_KEYS}
    approved = manifest["approved_primary_keys"]
    primary_keys = {key: set(values) for key, values in approved.items()}
    user_ids = primary_keys["user_ids"]
    thread_ids = primary_keys["thread_ids"]
    message_ids = primary_keys["message_ids"]
    request_ids = primary_keys["request_ids"]
    asset_ids = primary_keys["asset_ids"]
    audit_ids = primary_keys["audit_log_ids"]
    refresh_token_ids = primary_keys["refresh_token_ids"]
    entitlement_ids = primary_keys["entitlement_ids"]
    project_ids = primary_keys["project_ids"]
    export_ids = primary_keys["export_ids"]
    entitlement_reference_ids = primary_keys["entitlement_reference_ids"]
    channel_reference_ids = primary_keys["channel_reference_ids"]
    project_reference_thread_ids = primary_keys["project_reference_thread_ids"]
    asset_reference_message_ids = primary_keys["asset_reference_message_ids"]

    with SessionLocal() as db:
        deleted: dict[str, int] = {}
        migrated_references: dict[str, int] = {}
        with db.begin():
            _lock_cleanup_state(db)
            current_state = database_audit_state(db)
            if current_state != manifest["audit_state"]:
                raise ValueError(
                    "database audit state changed after approval; run a fresh audit and approve again"
                )
            current_closure = approved_primary_key_closure(db, roots)
            if current_closure != approved:
                raise ValueError(
                    "database relationship closure changed after approval; run a fresh audit and approve again"
                )

            before = dict(current_state["table_counts"])
            if project_reference_thread_ids:
                migrated_references["threads.project_id"] = int(
                    db.execute(
                        update(Thread)
                        .where(Thread.id.in_(project_reference_thread_ids))
                        .values(project_id=None)
                    ).rowcount
                    or 0
                )
                _require_exact_count(
                    "threads.project_id update",
                    migrated_references["threads.project_id"],
                    len(project_reference_thread_ids),
                )
            if asset_reference_message_ids:
                reference_messages = db.scalars(
                    select(Message)
                    .where(Message.id.in_(asset_reference_message_ids))
                    .order_by(Message.id)
                ).all()
                _require_exact_count(
                    "messages.asset_ids_json lookup",
                    len(reference_messages),
                    len(asset_reference_message_ids),
                )
                migrated_count = 0
                for message in reference_messages:
                    previous_ids = list(message.asset_ids_json or [])
                    filtered_ids = [
                        asset_id
                        for asset_id in previous_ids
                        if str(asset_id) not in asset_ids
                    ]
                    if filtered_ids == previous_ids:
                        raise ValueError(
                            "messages.asset_ids_json reference changed after closure validation"
                        )
                    message.asset_ids_json = filtered_ids
                    migrated_count += 1
                migrated_references["messages.asset_ids_json"] = _require_exact_count(
                    "messages.asset_ids_json update",
                    migrated_count,
                    len(asset_reference_message_ids),
                )
            if channel_reference_ids:
                migrated_references["model_channels.created_by"] = int(
                    db.execute(
                        update(ModelChannel)
                        .where(ModelChannel.id.in_(channel_reference_ids))
                        .values(created_by=None)
                    ).rowcount
                    or 0
                )
                _require_exact_count(
                    "model_channels.created_by update",
                    migrated_references["model_channels.created_by"],
                    len(channel_reference_ids),
                )
            if entitlement_reference_ids:
                migrated_references["entitlements.granted_by"] = int(
                    db.execute(
                        update(Entitlement)
                        .where(Entitlement.id.in_(entitlement_reference_ids))
                        .values(granted_by=None)
                    ).rowcount
                    or 0
                )
                _require_exact_count(
                    "entitlements.granted_by update",
                    migrated_references["entitlements.granted_by"],
                    len(entitlement_reference_ids),
                )
            if asset_ids:
                deleted[Asset.__tablename__] = _delete_count(db, Asset, Asset.id.in_(asset_ids))
                _require_exact_count("asset delete", deleted[Asset.__tablename__], len(asset_ids))
            if request_ids:
                deleted[ModelRequest.__tablename__] = _delete_count(db, ModelRequest, ModelRequest.id.in_(request_ids))
                _require_exact_count(
                    "model request delete",
                    deleted[ModelRequest.__tablename__],
                    len(request_ids),
                )
            if export_ids:
                deleted[Export.__tablename__] = _delete_count(db, Export, Export.id.in_(export_ids))
                _require_exact_count("export delete", deleted[Export.__tablename__], len(export_ids))
            if message_ids:
                deleted[Message.__tablename__] = _delete_count(db, Message, Message.id.in_(message_ids))
                _require_exact_count("message delete", deleted[Message.__tablename__], len(message_ids))
            if thread_ids:
                deleted[Thread.__tablename__] = _delete_count(db, Thread, Thread.id.in_(thread_ids))
                _require_exact_count("thread delete", deleted[Thread.__tablename__], len(thread_ids))
            if audit_ids:
                deleted[AdminAuditLog.__tablename__] = _delete_count(db, AdminAuditLog, AdminAuditLog.id.in_(audit_ids))
                _require_exact_count(
                    "admin audit log delete",
                    deleted[AdminAuditLog.__tablename__],
                    len(audit_ids),
                )
            if refresh_token_ids:
                deleted[RefreshToken.__tablename__] = _delete_count(
                    db,
                    RefreshToken,
                    RefreshToken.id.in_(refresh_token_ids),
                )
                _require_exact_count(
                    "refresh token delete",
                    deleted[RefreshToken.__tablename__],
                    len(refresh_token_ids),
                )
            if entitlement_ids:
                deleted[Entitlement.__tablename__] = _delete_count(db, Entitlement, Entitlement.id.in_(entitlement_ids))
                _require_exact_count(
                    "entitlement delete",
                    deleted[Entitlement.__tablename__],
                    len(entitlement_ids),
                )
            if project_ids:
                deleted[Project.__tablename__] = _delete_count(db, Project, Project.id.in_(project_ids))
                _require_exact_count("project delete", deleted[Project.__tablename__], len(project_ids))
            if user_ids:
                deleted[User.__tablename__] = _delete_count(db, User, User.id.in_(user_ids))
                _require_exact_count("user delete", deleted[User.__tablename__], len(user_ids))
            after = table_counts(db)

    post_audit = build_audit(
        getattr(args, "limit", 100),
        deleted=deleted,
        mode="post_apply_audit",
    )

    return {
        "mode": "apply",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "database_fingerprint": fingerprint,
        "approved_by": manifest["approved_by"],
        "approved_at": manifest["approved_at"],
        "database_backup": str(database_backup),
        "asset_backup": str(asset_backup),
        "backup_validation": {
            "database": database_backup_validation,
            "assets": asset_backup_validation,
        },
        "approved_primary_keys": approved,
        "deleted": deleted,
        "migrated_references": migrated_references,
        "table_counts_before": before,
        "table_counts_after": after,
        "classification": post_audit["classification"],
        "post_audit": post_audit,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Optional JSON report path")
    parser.add_argument("--limit", type=int, default=100, help="Maximum rows shown per audit category")
    parser.add_argument("--apply", action="store_true", help="Apply an approved primary-key manifest")
    parser.add_argument("--manifest")
    parser.add_argument("--database-backup")
    parser.add_argument("--asset-backup")
    parser.add_argument("--confirm-fingerprint")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 10_000:
        parser.error("--limit must be between 1 and 10000")
    if args.apply and not all((args.manifest, args.database_backup, args.asset_backup, args.confirm_fingerprint)):
        parser.error("--apply requires --manifest, --database-backup, --asset-backup and --confirm-fingerprint")
    return args


def main() -> int:
    args = parse_args()
    result = apply_manifest(args) if args.apply else build_audit(args.limit)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
