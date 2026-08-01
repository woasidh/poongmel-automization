from __future__ import annotations

from enum import StrEnum


class WorkflowType(StrEnum):
    MAIL_PROCESSING = "mail_processing"
    ORDER_STATUS_REFRESH = "order_status_refresh"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class NodeStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRY_WAIT = "RETRY_WAIT"
    SKIPPED = "SKIPPED"


class PendingStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    EXCLUDED = "EXCLUDED"
