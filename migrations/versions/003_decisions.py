"""Buyer decisions that settle failed commitment checks."""

import sqlalchemy as sa
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "decisions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("supplier_id", sa.String(100), nullable=False),
        sa.Column("po_line_id", sa.String(200), nullable=True),
        sa.Column("choice", sa.String(40), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_decisions_supplier_id", "decisions", ["supplier_id"])
    op.create_index("ix_decisions_po_line_id", "decisions", ["po_line_id"])


def downgrade():
    op.drop_index("ix_decisions_po_line_id", table_name="decisions")
    op.drop_index("ix_decisions_supplier_id", table_name="decisions")
    op.drop_table("decisions")
