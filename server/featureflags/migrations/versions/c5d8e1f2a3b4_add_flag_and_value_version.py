import sqlalchemy as sa
from alembic import op

revision = "c5d8e1f2a3b4"
down_revision = "f3a9c41e88d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "flag",
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "value",
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("value", "version")
    op.drop_column("flag", "version")
