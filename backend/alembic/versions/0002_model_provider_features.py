"""Provider metadata, tool-call and asset-reference fields."""

from alembic import op
import sqlalchemy as sa


revision = "0002_model_provider_features"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All additions are nullable (or have a server default) so existing
    # conversations and channels remain readable during a rolling deploy.
    # The development bootstrap may already have added these columns; inspect
    # first so `alembic upgrade head` remains safe and can mark the revision.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {table: {item["name"] for item in inspector.get_columns(table)} for table in ("messages", "model_requests", "model_channels")}
    existing_indexes = {
        table: {item["name"] for item in inspector.get_indexes(table)} | {item["name"] for item in inspector.get_unique_constraints(table) if item.get("name")}
        for table in ("messages", "model_requests")
    }

    def add_column(table: str, column: sa.Column) -> None:
        if column.name not in existing_columns[table]:
            op.add_column(table, column)
            existing_columns[table].add(column.name)

    def add_index(name: str, table: str, columns: list[str]) -> None:
        if name not in existing_indexes[table]:
            op.create_index(name, table, columns)
            existing_indexes[table].add(name)

    add_column("messages", sa.Column("content_json", sa.JSON(), nullable=True))
    add_column("messages", sa.Column("tool_call_id", sa.String(160), nullable=True))
    add_column("messages", sa.Column("tool_name", sa.String(120), nullable=True))
    add_column("messages", sa.Column("asset_ids_json", sa.JSON(), nullable=True))
    add_index("ix_messages_tool_call_id", "messages", ["tool_call_id"])

    add_column("model_requests", sa.Column("parent_request_id", sa.String(36), nullable=True))
    add_column("model_requests", sa.Column("turn_index", sa.Integer(), nullable=False, server_default="0"))
    add_column("model_requests", sa.Column("idempotency_key", sa.String(160), nullable=True))
    add_index("ix_model_requests_parent_request_id", "model_requests", ["parent_request_id"])
    add_index("ix_model_requests_idempotency_key", "model_requests", ["idempotency_key"])

    add_column("model_channels", sa.Column("capabilities_json", sa.JSON(), nullable=True))
    add_column("model_channels", sa.Column("models_synced_at", sa.DateTime(timezone=True), nullable=True))
    add_column("model_channels", sa.Column("last_sync_error", sa.String(500), nullable=True))
    add_column("model_channels", sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True))
    add_column("model_channels", sa.Column("last_test_ok", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_model_requests_idempotency_key", table_name="model_requests")
    op.drop_index("ix_model_requests_parent_request_id", table_name="model_requests")
    op.drop_column("model_requests", "idempotency_key")
    op.drop_column("model_requests", "turn_index")
    op.drop_column("model_requests", "parent_request_id")

    op.drop_index("ix_messages_tool_call_id", table_name="messages")
    op.drop_column("messages", "asset_ids_json")
    op.drop_column("messages", "tool_name")
    op.drop_column("messages", "tool_call_id")
    op.drop_column("messages", "content_json")

    op.drop_column("model_channels", "last_test_ok")
    op.drop_column("model_channels", "last_tested_at")
    op.drop_column("model_channels", "last_sync_error")
    op.drop_column("model_channels", "models_synced_at")
    op.drop_column("model_channels", "capabilities_json")
