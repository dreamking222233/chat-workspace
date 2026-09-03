"""Remove unreferenced files from the local asset volume.

Run from the backend directory after a database backup:
    python scripts/cleanup_assets.py
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import Asset


def cleanup() -> int:
    settings = get_settings()
    root = Path(settings.storage_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        referenced = {Path(item).resolve() for item in db.scalars(select(Asset.storage_key)).all() if item}
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, settings.asset_cleanup_days))
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.resolve() in referenced:
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


if __name__ == "__main__":
    print(f"removed_assets={cleanup()}")
