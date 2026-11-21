"""Persist reviewed, path-scoped source mappings."""

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_mappings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("table_locator", sa.String(), nullable=False),
        sa.Column("schema_fingerprint", sa.String(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("source_file_id", sa.String(), sa.ForeignKey("source_files.id"), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_source_mappings_path", "source_mappings", ["path"])


def downgrade():
    op.drop_index("ix_source_mappings_path", table_name="source_mappings")
    op.drop_table("source_mappings")
