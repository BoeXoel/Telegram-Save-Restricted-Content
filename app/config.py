from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import yaml


ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    return int(value)


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


@dataclass(frozen=True)
class ChatSpec:
    chat: str
    topic_id: int | None = None
    start_id: int | None = None
    end_id: int | None = None

    @classmethod
    def from_config(cls, value: Any) -> "ChatSpec":
        if isinstance(value, dict):
            message_range = value.get("message_range") or {}
            return cls(
                chat=str(value["chat"]),
                topic_id=_as_int(value.get("topic_id")),
                start_id=_as_int(value.get("start_id", message_range.get("start"))),
                end_id=_as_int(value.get("end_id", message_range.get("end"))),
            )

        raw = str(value).strip()
        if ":" in raw and not raw.startswith("@"):
            chat, topic = raw.rsplit(":", 1)
            if topic.isdigit():
                return cls(chat=chat, topic_id=int(topic))
        return cls(chat=raw)


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    user_session: str
    sessions_dir: Path
    load_dialogs_on_start: bool
    bot_enabled: bool
    bot_token: str
    bot_session_name: str
    use_bot_for_uploads: bool


@dataclass(frozen=True)
class TransferConfig:
    include_videos: bool
    include_photos: bool
    include_text: bool
    include_documents: bool
    hide_sender: bool
    drop_caption: bool
    prefer_copy: bool
    forwarding_only: bool
    save_to_local: bool
    max_bot_upload_bytes: int
    max_upload_bytes: int
    allow_download_unknown_size: bool


@dataclass(frozen=True)
class ContentFilterConfig:
    enabled: bool
    case_sensitive: bool
    keywords: tuple[str, ...]
    regex: tuple[str, ...]


@dataclass(frozen=True)
class QueueConfig:
    db_path: Path
    max_attempts: int
    retry_backoff_seconds: list[int]
    record_skipped: bool


@dataclass(frozen=True)
class LimitsConfig:
    global_min_delay_seconds: float
    resolve_delay_seconds: float
    read_delay_seconds: float
    download_delay_seconds: float
    copy_delay_seconds: float
    upload_delay_seconds: float
    verify_delay_seconds: float
    get_messages_chunk_size: int
    floodwait_extra_min_seconds: int
    floodwait_extra_max_seconds: int

    def delay_for(self, operation: str) -> float:
        return {
            "resolve": self.resolve_delay_seconds,
            "read": self.read_delay_seconds,
            "download": self.download_delay_seconds,
            "copy": self.copy_delay_seconds,
            "upload": self.upload_delay_seconds,
            "verify": self.verify_delay_seconds,
        }.get(operation, self.global_min_delay_seconds)


@dataclass(frozen=True)
class BatchConfig:
    size: int
    pause_between_batches_seconds: int
    idle_sleep_seconds: int


@dataclass(frozen=True)
class DownloadsConfig:
    root: Path
    active_dir: Path
    failed_dir: Path
    completed_dir: Path
    keep_failed: bool
    keep_completed: bool
    min_free_bytes: int
    max_failed_bytes: int
    max_job_bytes: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file: Path | None


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    telegram: TelegramConfig
    transfer: TransferConfig
    filters: ContentFilterConfig
    queue: QueueConfig
    limits: LimitsConfig
    batch: BatchConfig
    downloads: DownloadsConfig
    logging: LoggingConfig
    sources: list[ChatSpec] = field(default_factory=list)
    destinations: list[ChatSpec] = field(default_factory=list)

    def ensure_directories(self) -> None:
        self.telegram.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.queue.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.downloads.root.mkdir(parents=True, exist_ok=True)
        self.downloads.active_dir.mkdir(parents=True, exist_ok=True)
        self.downloads.failed_dir.mkdir(parents=True, exist_ok=True)
        self.downloads.completed_dir.mkdir(parents=True, exist_ok=True)
        if self.logging.file:
            self.logging.file.parent.mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    load_dotenv()
    config_path = Path(path).resolve()
    base_dir = config_path.parent

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    raw = _expand_env(raw)

    telegram = raw.get("telegram", {})
    transfer = raw.get("transfer", {})
    filters = raw.get("filters", {})
    queue = raw.get("queue", {})
    limits = raw.get("limits", {})
    batch = raw.get("batch", {})
    downloads = raw.get("downloads", {})
    logging_cfg = raw.get("logging", {})
    migration = raw.get("migration", {})

    api_id = _as_int(telegram.get("api_id"), 0) or 0
    api_hash = str(telegram.get("api_hash") or "")

    if api_id <= 0:
        raise ValueError("telegram.api_id is required in config.yaml or API_ID environment variable")
    if not api_hash:
        raise ValueError("telegram.api_hash is required in config.yaml or API_HASH environment variable")

    root = _path(base_dir, str(downloads.get("root", "downloads")))
    active_dir = _path(base_dir, str(downloads.get("active_dir", root / "active")))
    failed_dir = _path(base_dir, str(downloads.get("failed_dir", root / "failed")))
    completed_dir = _path(base_dir, str(downloads.get("completed_dir", root / "completed")))

    log_file = logging_cfg.get("file")

    return AppConfig(
        base_dir=base_dir,
        telegram=TelegramConfig(
            api_id=api_id,
            api_hash=api_hash,
            user_session=str(telegram.get("user_session") or "user"),
            sessions_dir=_path(base_dir, str(telegram.get("sessions_dir", "sessions"))),
            load_dialogs_on_start=_as_bool(telegram.get("load_dialogs_on_start"), False),
            bot_enabled=_as_bool((telegram.get("bot") or {}).get("enabled"), False),
            bot_token=str((telegram.get("bot") or {}).get("token") or ""),
            bot_session_name=str((telegram.get("bot") or {}).get("session_name") or "uploader_bot"),
            use_bot_for_uploads=_as_bool((telegram.get("bot") or {}).get("use_for_uploads"), True),
        ),
        transfer=TransferConfig(
            include_videos=_as_bool((transfer.get("include") or {}).get("videos"), True),
            include_photos=_as_bool((transfer.get("include") or {}).get("photos"), True),
            include_text=_as_bool((transfer.get("include") or {}).get("text"), True),
            include_documents=_as_bool((transfer.get("include") or {}).get("documents"), False),
            hide_sender=_as_bool(transfer.get("hide_sender"), True),
            drop_caption=_as_bool(transfer.get("drop_caption"), False),
            prefer_copy=_as_bool((transfer.get("native_copy") or {}).get("enabled"), True),
            forwarding_only=_as_bool((transfer.get("native_copy") or {}).get("only"), False),
            save_to_local=_as_bool(transfer.get("save_to_local"), False),
            max_bot_upload_bytes=_positive_int(
                transfer.get("max_bot_upload_bytes", 2_000 * 1024 * 1024),
                "transfer.max_bot_upload_bytes",
            ),
            max_upload_bytes=_nonnegative_int(
                transfer.get("max_upload_bytes", 0),
                "transfer.max_upload_bytes",
            ),
            allow_download_unknown_size=_as_bool(transfer.get("allow_download_unknown_size"), False),
        ),
        filters=_load_content_filters(filters),
        queue=QueueConfig(
            db_path=_path(base_dir, str(queue.get("db_path", "data/migration.sqlite3"))),
            max_attempts=int(queue.get("max_attempts", 4)),
            retry_backoff_seconds=[int(v) for v in queue.get("retry_backoff_seconds", [300, 600, 1800])],
            record_skipped=_as_bool(queue.get("record_skipped"), True),
        ),
        limits=LimitsConfig(
            global_min_delay_seconds=_as_float(limits.get("global_min_delay_seconds"), 1.0),
            resolve_delay_seconds=_as_float(limits.get("resolve_delay_seconds"), 2.0),
            read_delay_seconds=_as_float(limits.get("read_delay_seconds"), 2.0),
            download_delay_seconds=_as_float(limits.get("download_delay_seconds"), 5.0),
            copy_delay_seconds=_as_float(limits.get("copy_delay_seconds"), 10.0),
            upload_delay_seconds=_as_float(limits.get("upload_delay_seconds"), 30.0),
            verify_delay_seconds=_as_float(limits.get("verify_delay_seconds"), 2.0),
            get_messages_chunk_size=int(limits.get("get_messages_chunk_size", 100)),
            floodwait_extra_min_seconds=int(limits.get("floodwait_extra_min_seconds", 5)),
            floodwait_extra_max_seconds=int(limits.get("floodwait_extra_max_seconds", 20)),
        ),
        batch=BatchConfig(
            size=int(batch.get("size", 25)),
            pause_between_batches_seconds=int(batch.get("pause_between_batches_seconds", 1800)),
            idle_sleep_seconds=int(batch.get("idle_sleep_seconds", 30)),
        ),
        downloads=DownloadsConfig(
            root=root,
            active_dir=active_dir,
            failed_dir=failed_dir,
            completed_dir=completed_dir,
            keep_failed=_as_bool(downloads.get("keep_failed"), False),
            keep_completed=_as_bool(downloads.get("keep_completed"), False),
            min_free_bytes=_nonnegative_int(
                downloads.get("min_free_bytes", 5 * 1024 * 1024 * 1024),
                "downloads.min_free_bytes",
            ),
            max_failed_bytes=_nonnegative_int(
                downloads.get("max_failed_bytes", 0),
                "downloads.max_failed_bytes",
            ),
            max_job_bytes=_nonnegative_int(
                downloads.get("max_job_bytes", 0),
                "downloads.max_job_bytes",
            ),
        ),
        logging=LoggingConfig(
            level=str(logging_cfg.get("level", "INFO")).upper(),
            file=_path(base_dir, str(log_file)) if log_file else None,
        ),
        sources=[ChatSpec.from_config(item) for item in migration.get("sources", [])],
        destinations=[ChatSpec.from_config(item) for item in migration.get("destinations", [])],
    )


def _load_content_filters(raw: Any) -> ContentFilterConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("filters must be a mapping")

    case_sensitive = _as_bool(raw.get("case_sensitive"), False)
    keywords = _string_tuple(raw.get("keywords"), "filters.keywords")
    regex = _string_tuple(raw.get("regex"), "filters.regex")
    flags = 0 if case_sensitive else re.IGNORECASE
    for index, pattern in enumerate(regex, start=1):
        try:
            re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"filters.regex rule #{index} is invalid: {exc}") from exc

    return ContentFilterConfig(
        enabled=_as_bool(raw.get("enabled"), False),
        case_sensitive=case_sensitive,
        keywords=keywords,
        regex=regex,
    )


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str):
            raise ValueError(f"{field_name} rule #{index} must be a string")
        if item:
            result.append(item)
    return tuple(result)


def _nonnegative_int(value: Any, field_name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return result


def _positive_int(value: Any, field_name: str) -> int:
    result = _nonnegative_int(value, field_name)
    if result == 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return result
