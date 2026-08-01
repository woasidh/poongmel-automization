from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_type: Mapped[str] = mapped_column(String(64), index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), default="SCHEDULED")
    prefect_flow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")

    nodes: Mapped[list[NodeRun]] = relationship(back_populates="workflow_run")


class NodeRun(Base):
    __tablename__ = "node_runs"
    __table_args__ = (
        Index("ix_node_runs_workflow_node", "workflow_run_id", "node_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE")
    )
    mail_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    node_key: Mapped[str] = mapped_column(String(96), index=True)
    branch_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    prefect_task_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    input_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    output_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    error_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="nodes")


class GmailMessage(Base):
    __tablename__ = "gmail_messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), index=True)
    history_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    internal_date_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sender: Mapped[str] = mapped_column(Text, default="")
    sender_email: Mapped[str] = mapped_column(Text, default="")
    recipients_json: Mapped[str] = mapped_column(Text, default="[]")
    cc_json: Mapped[str] = mapped_column(Text, default="[]")
    subject: Mapped[str] = mapped_column(Text, default="")
    labels_json: Mapped[str] = mapped_column(Text, default="[]")
    body_text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_body_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    quoted_body_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_message_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(96), nullable=True)
    collected_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    attachments: Mapped[list[MailAttachment]] = relationship(back_populates="message")


class GmailPendingMessage(Base):
    __tablename__ = "gmail_pending_messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), default="")
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queued_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MailEvent(Base):
    __tablename__ = "mail_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    gmail_message_id: Mapped[str] = mapped_column(
        ForeignKey("gmail_messages.message_id", ondelete="CASCADE"), unique=True
    )
    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(32), default="DISCOVERED", index=True)
    current_node: Mapped[str] = mapped_column(String(96), default="discover_gmail")
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MailAttachment(Base):
    __tablename__ = "mail_attachments"
    __table_args__ = (
        UniqueConstraint(
            "gmail_message_id",
            "gmail_attachment_id",
            "file_name",
            name="uq_message_attachment",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    gmail_message_id: Mapped[str] = mapped_column(
        ForeignKey("gmail_messages.message_id", ondelete="CASCADE"), index=True
    )
    gmail_attachment_id: Mapped[str] = mapped_column(String(256), default="")
    file_name: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(256), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(64), default="PENDING")
    extracted_text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_json: Mapped[str] = mapped_column(Text, default="[]")

    message: Mapped[GmailMessage] = relationship(back_populates="attachments")


class EvidenceSnapshot(Base):
    __tablename__ = "evidence_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mail_event_id: Mapped[str] = mapped_column(
        ForeignKey("mail_events.id", ondelete="CASCADE"), index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(64), index=True)
    source_key: Mapped[str] = mapped_column(String(256))
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    sha256: Mapped[str] = mapped_column(String(64))
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MonitorState(Base):
    __tablename__ = "monitor_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
