from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tarfile
from typing import Callable

import pytest
from sqlalchemy import select

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
from scripts import cleanup_test_fixtures as cleanup


TEST_USER_ID = "cleanup-test-user"
REAL_USER_ID = "cleanup-real-user"
PROJECT_ID = "cleanup-project"
TEST_THREAD_ID = "cleanup-test-thread"
REAL_THREAD_ID = "cleanup-real-thread"
TEST_MESSAGE_ID = "cleanup-test-message"
REAL_MESSAGE_ID = "cleanup-real-message"
TEST_REQUEST_ID = "cleanup-test-request"
CHILD_REQUEST_ID = "cleanup-child-request"
TEST_ASSET_ID = "cleanup-test-asset"
REAL_ASSET_ID = "cleanup-real-asset"
TEST_EXPORT_ID = "cleanup-test-export"
EXTERNAL_ENTITLEMENT_ID = "cleanup-external-entitlement"
CHANNEL_ID = "cleanup-channel"


@dataclass
class ApplyArtifacts:
    args: argparse.Namespace
    manifest_path: Path
    database_backup: Path
    asset_backup: Path
    manifest: dict


def _assert_ephemeral_test_target() -> None:
    """Make every destructive test prove it is using conftest's temp SQLite."""

    settings = get_settings()
    database_path = Path(str(engine.url.database or "")).resolve()
    storage_path = Path(settings.storage_dir).resolve()
    assert settings.environment == "test"
    assert engine.url.get_backend_name() == "sqlite"
    assert database_path.name == "test.sqlite3"
    assert database_path.parent.name.startswith("chat-workspace-tests-")
    assert storage_path.parent == database_path.parent


def _seed_fixture_graph() -> None:
    _assert_ephemeral_test_target()
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.email == "admin@example.com"))
        assert admin is not None
        test_user = User(
            id=TEST_USER_ID,
            email=f"test-{'a' * 32}@example.com",
            display_name="Cleanup Test",
            password_hash="test-hash",
        )
        real_user = User(
            id=REAL_USER_ID,
            email="cleanup-real@example.com",
            display_name="Cleanup Real",
            password_hash="real-hash",
        )
        db.add_all([test_user, real_user])
        db.flush()

        project = Project(id=PROJECT_ID, user_id=test_user.id, name="Test project")
        db.add(project)
        db.flush()
        test_thread = Thread(
            id=TEST_THREAD_ID,
            user_id=test_user.id,
            project_id=project.id,
            title="Fixture conversation",
            model="gpt-real",
        )
        # This intentionally crosses ownership boundaries. Deleting the test
        # user's project must clear this surviving thread reference.
        real_thread = Thread(
            id=REAL_THREAD_ID,
            user_id=real_user.id,
            project_id=project.id,
            title="Real conversation",
            model="gpt-real",
        )
        db.add_all([test_thread, real_thread])
        db.flush()

        test_message = Message(
            id=TEST_MESSAGE_ID,
            thread_id=test_thread.id,
            user_id=test_user.id,
            role="assistant",
            content="fixture content",
            sequence=1,
        )
        # The surviving message points at both a deleted and retained asset.
        real_message = Message(
            id=REAL_MESSAGE_ID,
            thread_id=real_thread.id,
            user_id=real_user.id,
            role="assistant",
            content="real content",
            asset_ids_json=[TEST_ASSET_ID, REAL_ASSET_ID],
            sequence=1,
        )
        db.add_all([test_message, real_message])
        db.flush()

        channel = ModelChannel(
            id=CHANNEL_ID,
            name="Cleanup channel",
            base_url="https://provider.example/v1",
            modality="text",
            models_json=["gpt-real"],
            created_by=test_user.id,
        )
        db.add(channel)
        db.flush()

        db.add_all(
            [
                RefreshToken(
                    id="cleanup-refresh-token",
                    user_id=test_user.id,
                    token_hash="cleanup-token-hash",
                    expires_at=expires_at,
                ),
                Entitlement(
                    id="cleanup-owned-entitlement",
                    user_id=test_user.id,
                    granted_by=admin.id,
                    expires_at=expires_at,
                ),
                Entitlement(
                    id=EXTERNAL_ENTITLEMENT_ID,
                    user_id=real_user.id,
                    granted_by=test_user.id,
                    expires_at=expires_at,
                ),
                ModelRequest(
                    id=TEST_REQUEST_ID,
                    user_id=test_user.id,
                    thread_id=test_thread.id,
                    message_id=test_message.id,
                    channel_id=channel.id,
                    model="gpt-real",
                    modality="text",
                ),
                ModelRequest(
                    id=CHILD_REQUEST_ID,
                    user_id=real_user.id,
                    thread_id=None,
                    message_id=None,
                    model="gpt-real",
                    modality="text",
                    parent_request_id=TEST_REQUEST_ID,
                ),
                Asset(
                    id=TEST_ASSET_ID,
                    user_id=test_user.id,
                    message_id=test_message.id,
                    kind="image",
                    storage_key="uploads/test-image.png",
                    mime_type="image/png",
                    size_bytes=4,
                ),
                Asset(
                    id=REAL_ASSET_ID,
                    user_id=real_user.id,
                    message_id=real_message.id,
                    kind="image",
                    storage_key="uploads/real-image.png",
                    mime_type="image/png",
                    size_bytes=4,
                ),
                Export(
                    id=TEST_EXPORT_ID,
                    user_id=test_user.id,
                    thread_id=test_thread.id,
                    format="json",
                    storage_key="exports/test.json",
                ),
                AdminAuditLog(
                    id="cleanup-target-audit",
                    admin_id=admin.id,
                    target_user_id=test_user.id,
                    action="test.target",
                ),
                AdminAuditLog(
                    id="cleanup-admin-audit",
                    admin_id=test_user.id,
                    target_user_id=real_user.id,
                    action="test.admin",
                ),
            ]
        )
        db.commit()


def _roots(**overrides) -> dict[str, list[str]]:
    value = {key: [] for key in cleanup.MANIFEST_KEYS}
    value["user_ids"] = [TEST_USER_ID]
    value.update(overrides)
    return value


def _ids(category: dict) -> set[str]:
    return {row["id"] for row in category["rows"]}


def _sqlite_dump(path: Path, binding: dict, *, metadata_overrides: dict | None = None) -> None:
    _assert_ephemeral_test_target()
    metadata = {
        "database_fingerprint": binding["database_fingerprint"],
        "audit_state_digest": binding["audit_state"]["digest"],
        "target": cleanup.database_target_evidence(),
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    connection = sqlite3.connect(str(Path(str(engine.url.database)).resolve()))
    try:
        body = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()
    header = cleanup.SQLITE_BACKUP_METADATA_PREFIX + json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
    )
    path.write_text(header + "\n" + body, encoding="utf-8")


def _write_tar(path: Path, members: list[tuple[str, bytes, bytes | None]] | None = None) -> None:
    """Write members as ``(name, tar type, data/link target)`` tuples."""

    if members is None:
        members = [("assets/snapshot.txt", tarfile.REGTYPE, b"restorable assets")]
    with tarfile.open(path, mode="w:gz") as archive:
        for name, member_type, payload in members:
            info = tarfile.TarInfo(name)
            info.type = member_type
            if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                info.linkname = (payload or b"").decode("utf-8")
                archive.addfile(info)
            elif member_type == tarfile.DIRTYPE:
                archive.addfile(info)
            else:
                data = payload or b""
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))


def _save_manifest(artifacts: ApplyArtifacts) -> None:
    artifacts.manifest_path.write_text(
        json.dumps(artifacts.manifest, sort_keys=True),
        encoding="utf-8",
    )


def _refresh_backup_hash(artifacts: ApplyArtifacts, kind: str) -> None:
    path = artifacts.database_backup if kind == "database" else artifacts.asset_backup
    artifacts.manifest["backup_evidence"][f"{kind}_sha256"] = cleanup._sha256_file(path)
    _save_manifest(artifacts)


def _make_artifacts(tmp_path: Path, *, roots: dict[str, list[str]] | None = None) -> ApplyArtifacts:
    _assert_ephemeral_test_target()
    approved_roots = roots or _roots()
    binding = cleanup.build_manifest_binding(approved_roots)
    database_backup = tmp_path / "database.sql"
    asset_backup = tmp_path / "assets.tar.gz"
    _sqlite_dump(database_backup, binding)
    _write_tar(asset_backup)
    manifest = {
        **binding,
        **approved_roots,
        "approved": True,
        "approved_by": "fixture-reviewer",
        "approved_at": "2026-09-02T00:00:00Z",
        "backup_evidence": {
            "database_sha256": cleanup._sha256_file(database_backup),
            "asset_sha256": cleanup._sha256_file(asset_backup),
        },
    }
    manifest_path = tmp_path / "manifest.json"
    args = argparse.Namespace(
        manifest=str(manifest_path),
        database_backup=str(database_backup),
        asset_backup=str(asset_backup),
        confirm_fingerprint=cleanup.database_fingerprint(),
        limit=1000,
    )
    artifacts = ApplyArtifacts(args, manifest_path, database_backup, asset_backup, manifest)
    _save_manifest(artifacts)
    return artifacts


def _assert_fixture_still_present() -> None:
    with SessionLocal() as db:
        assert db.get(User, TEST_USER_ID) is not None
        assert db.get(Project, PROJECT_ID) is not None
        assert db.get(Asset, TEST_ASSET_ID) is not None
        assert db.get(Thread, REAL_THREAD_ID).project_id == PROJECT_ID
        assert TEST_ASSET_ID in db.get(Message, REAL_MESSAGE_ID).asset_ids_json
        assert db.get(Entitlement, EXTERNAL_ENTITLEMENT_ID).granted_by == TEST_USER_ID
        assert db.get(ModelChannel, CHANNEL_ID).created_by == TEST_USER_ID


def test_dry_run_reports_relational_closure_and_never_mutates() -> None:
    _seed_fixture_graph()
    with SessionLocal() as db:
        before = cleanup.database_audit_state(db)

    report = cleanup.build_audit(1000)

    with SessionLocal() as db:
        assert cleanup.database_audit_state(db) == before
    assert report["mode"] == "audit"
    assert report["candidate_policy"]["automatic_deletion"] is False
    assert report["candidates"]["test_users"]["count"] == 1
    impact = report["test_user_impact"]
    assert set(impact) == {
        "users",
        "refresh_tokens",
        "entitlements",
        "projects",
        "threads",
        "messages",
        "model_requests",
        "exports",
        "assets",
        "admin_audit_logs",
    }
    assert "token_hash" not in impact["refresh_tokens"]["rows"][0]
    references = report["reference_updates"]
    assert EXTERNAL_ENTITLEMENT_ID in _ids(references["entitlements"])
    assert CHANNEL_ID in _ids(references["model_channels"])
    assert REAL_THREAD_ID in _ids(references["threads"])
    assert REAL_MESSAGE_ID in _ids(references["messages"])


def test_v2_binding_contains_exact_delete_and_reference_closure() -> None:
    _seed_fixture_graph()
    binding = cleanup.build_manifest_binding(_roots())

    assert binding["schema_version"] == 2
    assert set(binding["approved_primary_keys"]) == cleanup.APPROVED_PRIMARY_KEY_KEYS
    closure = {key: set(value) for key, value in binding["approved_primary_keys"].items()}
    assert closure["user_ids"] == {TEST_USER_ID}
    assert closure["refresh_token_ids"] == {"cleanup-refresh-token"}
    assert closure["entitlement_ids"] == {"cleanup-owned-entitlement"}
    assert closure["project_ids"] == {PROJECT_ID}
    assert closure["thread_ids"] == {TEST_THREAD_ID}
    assert closure["message_ids"] == {TEST_MESSAGE_ID}
    assert closure["request_ids"] == {TEST_REQUEST_ID, CHILD_REQUEST_ID}
    assert closure["export_ids"] == {TEST_EXPORT_ID}
    assert closure["asset_ids"] == {TEST_ASSET_ID}
    assert closure["audit_log_ids"] == {"cleanup-target-audit", "cleanup-admin-audit"}
    assert closure["entitlement_reference_ids"] == {EXTERNAL_ENTITLEMENT_ID}
    assert closure["channel_reference_ids"] == {CHANNEL_ID}
    assert closure["project_reference_thread_ids"] == {REAL_THREAD_ID}
    assert closure["asset_reference_message_ids"] == {REAL_MESSAGE_ID}


@pytest.mark.parametrize(
    "email",
    [
        f"test-{'a' * 32}@example.com",
        f"no-channel-{'b' * 32}@example.com",
        f"regenerate-no-channel-{'c' * 32}@example.com",
        "tester-0123abcd@example.com",
        "flow-89abcdef@example.com",
        f"dbg-{'1' * 32}@e.com",
    ],
)
def test_known_historical_test_email_formats_are_candidates(email: str) -> None:
    assert cleanup.TEST_EMAIL.fullmatch(email)


@pytest.mark.parametrize(
    "email",
    [
        "admin@example.com",
        "owner@example.net",
        "tester-0123abc@example.com",
        f"customer-{'2' * 32}@example.com",
    ],
)
def test_real_or_non_fixture_email_formats_are_retained(email: str) -> None:
    assert cleanup.TEST_EMAIL.fullmatch(email) is None


@pytest.mark.parametrize(
    "missing",
    [
        "schema_version",
        "approved",
        "approved_by",
        "approved_at",
        "database_fingerprint",
        "audit_state",
        "approved_primary_keys",
        "backup_evidence",
        *sorted(cleanup.MANIFEST_KEYS),
    ],
)
def test_v2_manifest_missing_any_required_field_is_rejected(tmp_path: Path, missing: str) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    artifacts.manifest.pop(missing)
    _save_manifest(artifacts)

    with pytest.raises(ValueError):
        cleanup._load_manifest(str(artifacts.manifest_path))


def test_manifest_unknown_fields_and_empty_approval_are_rejected(tmp_path: Path) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    artifacts.manifest["email_prefix"] = "test-"
    _save_manifest(artifacts)
    with pytest.raises(ValueError, match="unsupported fields"):
        cleanup._load_manifest(str(artifacts.manifest_path))

    artifacts.manifest.pop("email_prefix")
    for key in cleanup.MANIFEST_KEYS:
        artifacts.manifest[key] = []
    _save_manifest(artifacts)
    with pytest.raises(ValueError, match="no approved primary keys"):
        cleanup._load_manifest(str(artifacts.manifest_path))


@pytest.mark.parametrize("missing", ["database_backup", "asset_backup"])
def test_apply_rejects_missing_backup_without_changes(tmp_path: Path, missing: str) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    Path(getattr(artifacts.args, missing)).unlink()

    with pytest.raises(ValueError, match="backup"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


@pytest.mark.parametrize("kind", ["database", "asset"])
def test_apply_rejects_backup_hash_mismatch_without_changes(tmp_path: Path, kind: str) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    path = artifacts.database_backup if kind == "database" else artifacts.asset_backup
    with path.open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(ValueError, match="SHA-256"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


@pytest.mark.parametrize("kind", ["database", "asset"])
def test_apply_rejects_corrupt_backup_even_when_hash_is_approved(tmp_path: Path, kind: str) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    path = artifacts.database_backup if kind == "database" else artifacts.asset_backup
    path.write_bytes(b"corrupt but reviewer-hashed backup")
    _refresh_backup_hash(artifacts, kind)

    with pytest.raises(ValueError, match="backup|tar.gz|table structures"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


@pytest.mark.parametrize(
    ("field", "replacement", "error"),
    [
        ("database_fingerprint", "0" * 64, "another database fingerprint"),
        ("audit_state_digest", "1" * 64, "approved audit state"),
        (
            "target",
            {"dialect": "sqlite", "host": "", "port": None, "database": "/wrong/target.sqlite3"},
            "target evidence",
        ),
    ],
)
def test_sql_backup_with_wrong_target_metadata_is_rejected(
    tmp_path: Path,
    field: str,
    replacement: object,
    error: str,
) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    lines = artifacts.database_backup.read_text(encoding="utf-8").splitlines()
    metadata = json.loads(lines[0][len(cleanup.SQLITE_BACKUP_METADATA_PREFIX) :])
    metadata[field] = replacement
    lines[0] = cleanup.SQLITE_BACKUP_METADATA_PREFIX + json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
    )
    artifacts.database_backup.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _refresh_backup_hash(artifacts, "database")

    with pytest.raises(ValueError, match=error):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


def test_sql_backup_must_restore_the_full_approved_state_not_only_counts_and_ids(tmp_path: Path) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    sql = artifacts.database_backup.read_text(encoding="utf-8")
    assert "Cleanup Real" in sql
    artifacts.database_backup.write_text(
        sql.replace("Cleanup Real", "Cleanup Altered", 1),
        encoding="utf-8",
    )
    _refresh_backup_hash(artifacts, "database")

    with pytest.raises(ValueError, match="contents do not match"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


@pytest.mark.parametrize("name", ["../escape", "/absolute/file", "assets\\windows-path"])
def test_asset_backup_rejects_unsafe_member_paths(tmp_path: Path, name: str) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    _write_tar(artifacts.asset_backup, [(name, tarfile.REGTYPE, b"payload")])
    _refresh_backup_hash(artifacts, "asset")

    with pytest.raises(ValueError, match="unsafe member path"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_asset_backup_rejects_symbolic_and_hard_links(tmp_path: Path, member_type: bytes) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    _write_tar(
        artifacts.asset_backup,
        [("assets/link", member_type, b"../../outside")],
    )
    _refresh_backup_hash(artifacts, "asset")

    with pytest.raises(ValueError, match="link member"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


def _mutate_retained_value() -> None:
    with SessionLocal() as db:
        db.get(User, REAL_USER_ID).display_name = "Changed after audit"
        db.commit()


def _insert_unrelated_row() -> None:
    with SessionLocal() as db:
        db.add(
            User(
                id="cleanup-late-unrelated-user",
                email="late-real@example.com",
                display_name="Late real user",
                password_hash="hash",
            )
        )
        db.commit()


def _insert_owned_relation() -> None:
    with SessionLocal() as db:
        db.add(
            RefreshToken(
                id="cleanup-late-token",
                user_id=TEST_USER_ID,
                token_hash="cleanup-late-token-hash",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        db.commit()


def _insert_soft_reference() -> None:
    with SessionLocal() as db:
        db.add(
            Entitlement(
                id="cleanup-late-grant",
                user_id=REAL_USER_ID,
                granted_by=TEST_USER_ID,
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        db.commit()


def _insert_project_reference() -> None:
    with SessionLocal() as db:
        db.add(
            Thread(
                id="cleanup-late-project-thread",
                user_id=REAL_USER_ID,
                project_id=PROJECT_ID,
                title="Late relation",
                model="gpt-real",
            )
        )
        db.commit()


def _insert_asset_reference() -> None:
    with SessionLocal() as db:
        message = db.get(Message, REAL_MESSAGE_ID)
        message.asset_ids_json = [REAL_ASSET_ID, TEST_ASSET_ID, TEST_ASSET_ID]
        db.commit()


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("retained value", _mutate_retained_value),
        ("unrelated row", _insert_unrelated_row),
        ("owned relation", _insert_owned_relation),
        ("soft reference", _insert_soft_reference),
        ("project reference", _insert_project_reference),
        ("asset reference", _insert_asset_reference),
    ],
)
def test_any_post_audit_state_or_relationship_change_is_rejected(
    tmp_path: Path,
    label: str,
    mutate: Callable[[], None],
) -> None:
    del label
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    mutate()

    with pytest.raises(ValueError, match="audit state changed"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


@pytest.mark.parametrize(
    "closure_key",
    [
        "refresh_token_ids",
        "request_ids",
        "project_reference_thread_ids",
        "asset_reference_message_ids",
    ],
)
def test_incomplete_approved_relationship_closure_is_rejected(
    tmp_path: Path,
    closure_key: str,
) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    assert artifacts.manifest["approved_primary_keys"][closure_key]
    artifacts.manifest["approved_primary_keys"][closure_key] = []
    _save_manifest(artifacts)

    with pytest.raises(ValueError, match="relationship closure changed"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


def test_approved_closure_with_extra_primary_key_is_rejected(tmp_path: Path) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    artifacts.manifest["approved_primary_keys"]["asset_ids"].append(REAL_ASSET_ID)
    _save_manifest(artifacts)

    with pytest.raises(ValueError, match="relationship closure changed"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


def test_apply_rejects_confirmed_fingerprint_mismatch_without_changes(tmp_path: Path) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    artifacts.args.confirm_fingerprint = "0" * 64

    with pytest.raises(ValueError, match="fingerprint"):
        cleanup.apply_manifest(artifacts.args)
    _assert_fixture_still_present()


def test_confirmed_v2_manifest_deletes_and_migrates_exact_closure(tmp_path: Path) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)

    result = cleanup.apply_manifest(artifacts.args)

    assert result["mode"] == "apply"
    assert result["deleted"] == {
        "admin_audit_logs": 2,
        "assets": 1,
        "entitlements": 1,
        "exports": 1,
        "messages": 1,
        "model_requests": 2,
        "projects": 1,
        "refresh_tokens": 1,
        "threads": 1,
        "users": 1,
    }
    assert result["migrated_references"] == {
        "threads.project_id": 1,
        "messages.asset_ids_json": 1,
        "model_channels.created_by": 1,
        "entitlements.granted_by": 1,
    }
    assert result["backup_validation"]["database"]["format"] == "sqlite-sql"
    assert result["backup_validation"]["assets"]["format"] == "tar.gz"
    assert result["post_audit"]["mode"] == "post_apply_audit"
    assert result["post_audit"]["candidates"]["test_users"]["count"] == 0
    assert result["classification"]["deleted"]["total"] == 12

    with SessionLocal() as db:
        assert db.get(User, TEST_USER_ID) is None
        assert db.get(User, REAL_USER_ID) is not None
        assert db.get(Project, PROJECT_ID) is None
        assert db.get(Thread, TEST_THREAD_ID) is None
        assert db.get(Message, TEST_MESSAGE_ID) is None
        assert db.get(Asset, TEST_ASSET_ID) is None
        assert db.get(ModelRequest, TEST_REQUEST_ID) is None
        assert db.get(ModelRequest, CHILD_REQUEST_ID) is None
        assert db.get(Thread, REAL_THREAD_ID).project_id is None
        assert db.get(Message, REAL_MESSAGE_ID).asset_ids_json == [REAL_ASSET_ID]
        assert db.get(Asset, REAL_ASSET_ID) is not None
        assert db.get(Entitlement, EXTERNAL_ENTITLEMENT_ID).granted_by is None
        assert db.get(ModelChannel, CHANNEL_ID).created_by is None


def test_apply_rolls_back_deletes_and_reference_updates_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_fixture_graph()
    artifacts = _make_artifacts(tmp_path)
    with SessionLocal() as db:
        before = cleanup.database_audit_state(db)

    original_delete_count = cleanup._delete_count
    calls = 0

    def fail_after_first_delete(db, model, condition):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced transactional failure")
        return original_delete_count(db, model, condition)

    monkeypatch.setattr(cleanup, "_delete_count", fail_after_first_delete)
    with pytest.raises(RuntimeError, match="transactional failure"):
        cleanup.apply_manifest(artifacts.args)

    with SessionLocal() as db:
        assert cleanup.database_audit_state(db) == before
    _assert_fixture_still_present()
