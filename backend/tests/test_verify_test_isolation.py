from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_test_isolation import _requested_workers, _validate_test_records


def _write_record(
    record_dir: Path,
    temp_root: Path,
    worker_id: str,
    process_id: int,
    worker_count: int | None,
) -> None:
    test_root = (temp_root / f"test-root-{worker_id}").resolve()
    storage_dir = test_root / "assets"
    storage_dir.mkdir(parents=True)
    database_path = test_root / "test.sqlite3"
    payload = {
        "schema_version": 1,
        "database_url": f"sqlite:///{database_path}",
        "engine_url": f"sqlite:///{database_path}",
        "process_id": process_id,
        "storage_dir": str(storage_dir),
        "test_root": str(test_root),
        "worker_count": worker_count,
        "worker_id": worker_id,
    }
    (record_dir / f"{worker_id}-{process_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_validate_test_records_aggregates_every_xdist_worker(tmp_path: Path) -> None:
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    _write_record(record_dir, tmp_path, "main", 1000, None)
    _write_record(record_dir, tmp_path, "gw0", 1001, 2)
    _write_record(record_dir, tmp_path, "gw1", 1002, 2)

    result = _validate_test_records(
        record_dir,
        f"sqlite:///{tmp_path / 'configured.sqlite3'}",
        tmp_path / "configured-assets",
        parallel_requested=True,
        requested_worker_count=2,
    )

    assert result["record_count"] == 3
    assert result["declared_worker_count"] == 2
    assert result["worker_ids"] == ["gw0", "gw1"]
    assert [record["worker_id"] for record in result["records"]] == ["main", "gw0", "gw1"]
    assert len({record["database_url"] for record in result["records"]}) == 3
    assert len({record["storage_dir"] for record in result["records"]}) == 3


def test_validate_test_records_rejects_a_missing_xdist_worker(tmp_path: Path) -> None:
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    _write_record(record_dir, tmp_path, "main", 1000, None)
    _write_record(record_dir, tmp_path, "gw0", 1001, 2)

    with pytest.raises(ValueError, match="records are incomplete: expected 2, found 1"):
        _validate_test_records(
            record_dir,
            f"sqlite:///{tmp_path / 'configured.sqlite3'}",
            tmp_path / "configured-assets",
            parallel_requested=True,
            requested_worker_count=2,
        )


def test_validate_test_records_rejects_an_unexpected_worker_set(tmp_path: Path) -> None:
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    _write_record(record_dir, tmp_path, "main", 1000, None)
    _write_record(record_dir, tmp_path, "gw0", 1001, 2)
    _write_record(record_dir, tmp_path, "gw2", 1002, 2)

    with pytest.raises(ValueError, match="record set is incomplete"):
        _validate_test_records(
            record_dir,
            f"sqlite:///{tmp_path / 'configured.sqlite3'}",
            tmp_path / "configured-assets",
            parallel_requested=True,
            requested_worker_count=2,
        )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["-q"], (False, None)),
        (["-n", "2", "-q"], (True, 2)),
        (["-n2"], (True, 2)),
        (["--numprocesses=3"], (True, 3)),
        (["-n", "auto"], (True, None)),
        (["-n", "0"], (False, 0)),
    ],
)
def test_requested_workers(arguments: list[str], expected: tuple[bool, int | None]) -> None:
    assert _requested_workers(arguments) == expected
