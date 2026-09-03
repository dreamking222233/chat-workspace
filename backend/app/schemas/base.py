from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.time import as_beijing


class BeijingTimeResponse(BaseModel):
    """Response base that serializes every datetime with an explicit +08 offset."""

    # Keep response models directly compatible with SQLAlchemy ORM instances.
    # Callers may still pass ``from_attributes=True`` explicitly, but the
    # default makes the response contract hold for FastAPI/TypeAdapter paths
    # that validate an ORM object without additional flags.
    model_config = ConfigDict(from_attributes=True)

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def normalize_response_datetime(cls, value):
        return as_beijing(value) if isinstance(value, datetime) else value
