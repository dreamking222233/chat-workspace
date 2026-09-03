"""Persist stream events and enforce request idempotency."""

from alembic import op
import sqlalchemy as sa


revision = "0003_request_events"
down_revision = "0002_model_provider_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("model_requests")}
    if "events_json" not in columns:
        op.add_column("model_requests", sa.Column("events_json", sa.JSON(), nullable=True))
    indexes = {item["name"] for item in inspector.get_indexes("model_requests")}
    indexes |= {item["name"] for item in inspector.get_unique_constraints("model_requests") if item.get("name")}
    indexes |= {item["name"] for item in inspector.get_unique_constraints("model_requests") if item.get("name")}
    if "ux_model_requests_idempotency" not in indexes:
        op.create_index(
            "ux_model_requests_idempotency",
            "model_requests",
            ["user_id", "thread_id", "idempotency_key", "modality"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("model_requests")}
    if "ux_model_requests_idempotency" in indexes:
        op.drop_index("ux_model_requests_idempotency", table_name="model_requests")
    columns = {item["name"] for item in inspector.get_columns("model_requests")}
    if "events_json" in columns:
        op.drop_column("model_requests", "events_json")
