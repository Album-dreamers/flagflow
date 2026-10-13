"""
Project versions come from one sequence, so they never go back: a project
deleted and created again continues above every version it had.
"""

from alembic import op

revision = "d7e2a9b4c1f6"
down_revision = "c5d8e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE project_version_seq")
    op.execute(
        "SELECT setval('project_version_seq',"
        " COALESCE((SELECT max(version) FROM project), 0) + 1, false)"
    )


def downgrade() -> None:
    op.execute("DROP SEQUENCE project_version_seq")
