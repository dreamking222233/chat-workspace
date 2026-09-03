"""Store the upstream channel family for model routing and administration."""

from alembic import op
import sqlalchemy as sa


revision = "0004_channel_type"
down_revision = "0003_request_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("model_channels")}
    if "channel_type" not in columns:
        op.add_column(
            "model_channels",
            sa.Column("channel_type", sa.String(20), nullable=False, server_default="official"),
        )
    op.alter_column("model_channels", "channel_type", server_default=None)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "channel_type" in {item["name"] for item in inspector.get_columns("model_channels")}:
        op.drop_column("model_channels", "channel_type")
