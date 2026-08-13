from __future__ import annotations


class JobError(Exception):
    """Base class for job processing failures."""

    def __init__(self, message: str = "", *, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class PermanentJobError(JobError):
    """A job cannot succeed without user/config changes."""


class RetryableJobError(JobError):
    """A job may succeed later."""


class DeferredJobError(RetryableJobError):
    """Retry later without consuming a normal transfer attempt."""

    def __init__(
        self,
        message: str = "",
        *,
        reason_code: str | None = None,
        retry_after_seconds: int = 300,
    ) -> None:
        super().__init__(message, reason_code=reason_code)
        self.retry_after_seconds = max(1, retry_after_seconds)


class DiskLowError(DeferredJobError):
    def __init__(self, message: str) -> None:
        super().__init__(message, reason_code="disk_low")


class DiskFullError(DeferredJobError):
    def __init__(self, message: str) -> None:
        super().__init__(message, reason_code="disk_full")


class JobSizeLimitError(PermanentJobError):
    def __init__(self, message: str) -> None:
        super().__init__(message, reason_code="disk_job_limit")


class RemoteConfigurationError(PermanentJobError):
    def __init__(self, message: str) -> None:
        super().__init__(message, reason_code="remote_config")


class RemotePermissionError(PermanentJobError):
    def __init__(self, message: str) -> None:
        super().__init__(message, reason_code="remote_permission_denied")


class RemoteTransferError(RetryableJobError):
    def __init__(self, message: str) -> None:
        super().__init__(message, reason_code="remote_error")


class TopicDeliveryError(PermanentJobError):
    def __init__(self, message: str) -> None:
        super().__init__(message, reason_code="topic_error")


def compact_error(exc: BaseException) -> str:
    message = f"{exc.__class__.__name__}: {exc}"
    return " ".join(message.split())[:4000]
