"""Initial persistent evidence, cases, mail and leased jobs."""

import sqlalchemy as sa
from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("jobs_due", "jobs", ["status", "run_after"], unique=False)
    op.create_table(
        "source_files",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("parse_status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("blob_ref", sa.String(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path", "checksum"),
    )
    op.create_table(
        "system_status",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "source_facts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_file_id", sa.String(), nullable=False),
        sa.Column("record_locator", sa.String(), nullable=False),
        sa.Column("fact_type", sa.String(), nullable=False),
        sa.Column("normalized_json", sa.JSON(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["source_files.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_file_id", "record_locator"),
    )
    op.create_table(
        "purchase_order_lines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("po_number", sa.String(), nullable=False),
        sa.Column("line_number", sa.String(), nullable=False),
        sa.Column("supplier_id", sa.String(), nullable=False),
        sa.Column("part_number", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("source_fact_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_fact_id"],
            ["source_facts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("po_number", "line_number"),
    )
    op.create_index(
        op.f("ix_purchase_order_lines_supplier_id"),
        "purchase_order_lines",
        ["supplier_id"],
        unique=False,
    )
    op.create_table(
        "suppliers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_ref"],
            ["source_facts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("active_key", sa.String(), nullable=True),
        sa.Column("po_line_id", sa.String(), nullable=False),
        sa.Column("issue_type", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("evidence_summary", sa.JSON(), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("reminders", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["po_line_id"],
            ["purchase_order_lines.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key"),
    )
    op.create_index(op.f("ix_cases_key"), "cases", ["key"], unique=False)
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("proposed_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "case_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_case_events_case_id"), "case_events", ["case_id"], unique=False)
    op.create_table(
        "messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=True),
        sa.Column("external_message_id", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("sender", sa.String(), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("sent_or_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_key", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
        sa.UniqueConstraint("external_message_id"),
    )
    op.create_index(op.f("ix_messages_case_id"), "messages", ["case_id"], unique=False)
    op.create_table(
        "commitment_claims",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("po_line_id", sa.String(), nullable=False),
        sa.Column("supplier_id", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("source_fact_id", sa.String(), nullable=True),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification_status", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_fact_id"],
            ["source_facts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index(
        op.f("ix_commitment_claims_po_line_id"), "commitment_claims", ["po_line_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_commitment_claims_po_line_id"), table_name="commitment_claims")
    op.drop_table("commitment_claims")
    op.drop_index(op.f("ix_messages_case_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_case_events_case_id"), table_name="case_events")
    op.drop_table("case_events")
    op.drop_table("approvals")
    op.drop_index(op.f("ix_cases_key"), table_name="cases")
    op.drop_table("cases")
    op.drop_table("suppliers")
    op.drop_index(op.f("ix_purchase_order_lines_supplier_id"), table_name="purchase_order_lines")
    op.drop_table("purchase_order_lines")
    op.drop_table("source_facts")
    op.drop_table("system_status")
    op.drop_table("source_files")
    op.drop_index("jobs_due", table_name="jobs")
    op.drop_table("jobs")
