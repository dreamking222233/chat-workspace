from datetime import datetime
from types import SimpleNamespace

from app.schemas.common import UsageResponse


def _usage(created_at: str) -> UsageResponse:
    return UsageResponse(
        id="request-time",
        user_id="user-time",
        thread_id=None,
        model="time-model",
        modality="text",
        status="completed",
        input_tokens=None,
        output_tokens=None,
        latency_ms=None,
        created_at=created_at,
    )


def test_response_schema_normalizes_all_datetime_string_forms_to_beijing_time():
    assert _usage("2026-09-02T00:01:02").model_dump(mode="json")["created_at"] == "2026-09-02T08:01:02+08:00"
    assert _usage("2026-09-02T00:01:02Z").model_dump(mode="json")["created_at"] == "2026-09-02T08:01:02+08:00"
    assert _usage("2026-09-02T08:01:02+08:00").model_dump(mode="json")["created_at"] == "2026-09-02T08:01:02+08:00"


def test_response_schema_accepts_orm_without_callsite_flags_and_normalizes_datetime():
    orm_row = SimpleNamespace(
        id="request-orm",
        user_id="user-time",
        user_email=None,
        thread_id=None,
        model="time-model",
        modality="text",
        status="completed",
        input_tokens=None,
        output_tokens=None,
        latency_ms=None,
        created_at=datetime(2026, 9, 2, 0, 1, 2),
    )
    response = UsageResponse.model_validate(orm_row)
    assert response.model_dump(mode="json")["created_at"] == "2026-09-02T08:01:02+08:00"
