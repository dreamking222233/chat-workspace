from datetime import datetime, timezone
from zoneinfo import ZoneInfo


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def as_utc(value: datetime) -> datetime:
    """Normalize database timestamps; naive MySQL DATETIME values represent UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def as_beijing(value: datetime) -> datetime:
    return as_utc(value).astimezone(BEIJING_TIMEZONE)


def beijing_isoformat(value: datetime) -> str:
    return as_beijing(value).isoformat()
