from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STATUSES = {"pending", "downloading", "uploading", "copied", "failed", "skipped"}
_UNSET = object()


# These columns are deliberately additive.  Existing users keep their queue
# database between releases, so schema changes must never require a rebuild.
MESSAGE_ADDITIVE_COLUMNS = {
    "reason_code": "TEXT",
    "transfer_route": "TEXT",
    "media_manifest": "TEXT",
    "remote_uri": "TEXT",
    "writer_identity": "TEXT",
    "upload_limit_bytes": "INTEGER",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.conn.close()

    def initialize(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                dest_chat_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'downloading', 'uploading', 'copied', 'failed', 'skipped')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_retry_at TEXT,
                file_unique_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source_topic_id INTEGER,
                dest_topic_id INTEGER,
                media_group_id TEXT,
                source_message_ids TEXT NOT NULL,
                dest_message_ids TEXT,
                media_type TEXT,
                file_size INTEGER,
                caption TEXT,
                verified_at TEXT,
                reason_code TEXT,
                transfer_route TEXT,
                media_manifest TEXT,
                remote_uri TEXT,
                writer_identity TEXT,
                upload_limit_bytes INTEGER
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_unique_job
                ON messages(source_chat_id, dest_chat_id, file_unique_key);
            CREATE INDEX IF NOT EXISTS idx_messages_due
                ON messages(status, next_retry_at, updated_at);
            CREATE INDEX IF NOT EXISTS idx_messages_source
                ON messages(source_chat_id, source_message_id);

            CREATE TABLE IF NOT EXISTS remote_objects (
                source_chat_id TEXT NOT NULL,
                file_unique_key TEXT NOT NULL,
                remote_uri TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (source_chat_id, file_unique_key)
            );
            """
        )
        self._migrate_message_columns()
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_reason ON messages(status, reason_code)"
        )
        self.conn.commit()

    def _migrate_message_columns(self) -> None:
        """Add optional fields introduced after the first queue schema.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` does not update existing
        tables.  Keep this migration intentionally small and idempotent so an
        older ``data/queue.sqlite3`` starts normally after an upgrade.
        """

        existing_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(messages)")
        }
        for column, definition in MESSAGE_ADDITIVE_COLUMNS.items():
            if column not in existing_columns:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cursor = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cursor

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)))

    def enqueue_message(
        self,
        *,
        source_chat_id: str,
        source_message_id: int,
        dest_chat_id: str,
        file_unique_key: str,
        source_message_ids: list[int],
        source_topic_id: int | None,
        dest_topic_id: int | None,
        media_group_id: str | None,
        media_type: str,
        file_size: int | None,
        caption: str | None,
        status: str = "pending",
        last_error: str | None = None,
        reason_code: str | None = None,
        transfer_route: str | None = None,
        media_manifest: list[dict[str, Any]] | None = None,
        remote_uri: str | None = None,
        writer_identity: str | None = None,
        upload_limit_bytes: int | None = None,
    ) -> bool:
        if status not in STATUSES:
            raise ValueError(f"Invalid message status: {status}")

        now = utc_now()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                source_chat_id, source_message_id, dest_chat_id, status, attempts,
                last_error, next_retry_at, file_unique_key, created_at, updated_at,
                source_topic_id, dest_topic_id, media_group_id, source_message_ids,
                media_type, file_size, caption, reason_code, transfer_route,
                media_manifest, remote_uri, writer_identity, upload_limit_bytes
            )
            VALUES (?, ?, ?, ?, 0, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_chat_id,
                source_message_id,
                dest_chat_id,
                status,
                last_error,
                file_unique_key,
                now,
                now,
                source_topic_id,
                dest_topic_id,
                media_group_id,
                json.dumps(source_message_ids),
                media_type,
                file_size,
                caption,
                reason_code,
                transfer_route,
                json.dumps(media_manifest) if media_manifest is not None else None,
                remote_uri,
                writer_identity,
                upload_limit_bytes,
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_status(
        self,
        job_id: int,
        status: str,
        *,
        last_error: str | None = None,
        next_retry_at: str | None = None,
        dest_message_ids: list[int] | None = None,
        verified_at: str | None = None,
        reason_code: str | None | object = _UNSET,
        transfer_route: str | None | object = _UNSET,
        media_manifest: list[dict[str, Any]] | None | object = _UNSET,
        remote_uri: str | None | object = _UNSET,
        writer_identity: str | None | object = _UNSET,
        upload_limit_bytes: int | None | object = _UNSET,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"Invalid message status: {status}")

        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, utc_now()]

        if last_error is not None:
            fields.append("last_error = ?")
            values.append(last_error[:4000])
        if next_retry_at is not None or status in {"copied", "failed", "skipped"}:
            fields.append("next_retry_at = ?")
            values.append(next_retry_at)
        if dest_message_ids is not None:
            fields.append("dest_message_ids = ?")
            values.append(json.dumps(dest_message_ids))
        if verified_at is not None:
            fields.append("verified_at = ?")
            values.append(verified_at)
        self._append_metadata_updates(
            fields,
            values,
            reason_code=reason_code,
            transfer_route=transfer_route,
            media_manifest=media_manifest,
            remote_uri=remote_uri,
            writer_identity=writer_identity,
            upload_limit_bytes=upload_limit_bytes,
        )

        values.append(job_id)
        self.execute(f"UPDATE messages SET {', '.join(fields)} WHERE id = ?", values)

    def update_metadata(
        self,
        job_id: int,
        *,
        reason_code: str | None | object = _UNSET,
        transfer_route: str | None | object = _UNSET,
        media_manifest: list[dict[str, Any]] | None | object = _UNSET,
        remote_uri: str | None | object = _UNSET,
        writer_identity: str | None | object = _UNSET,
        upload_limit_bytes: int | None | object = _UNSET,
    ) -> None:
        fields = ["updated_at = ?"]
        values: list[Any] = [utc_now()]
        self._append_metadata_updates(
            fields,
            values,
            reason_code=reason_code,
            transfer_route=transfer_route,
            media_manifest=media_manifest,
            remote_uri=remote_uri,
            writer_identity=writer_identity,
            upload_limit_bytes=upload_limit_bytes,
        )
        if len(fields) == 1:
            return
        values.append(job_id)
        self.execute(f"UPDATE messages SET {', '.join(fields)} WHERE id = ?", values)

    @staticmethod
    def _append_metadata_updates(
        fields: list[str],
        values: list[Any],
        **metadata: Any,
    ) -> None:
        for column, value in metadata.items():
            if value is _UNSET:
                continue
            fields.append(f"{column} = ?")
            if column == "media_manifest" and value is not None:
                values.append(json.dumps(value))
            else:
                values.append(value)

    def increment_attempt(self, job_id: int) -> int:
        now = utc_now()
        self.conn.execute(
            "UPDATE messages SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (now, job_id),
        )
        row = self.conn.execute("SELECT attempts FROM messages WHERE id = ?", (job_id,)).fetchone()
        self.conn.commit()
        return int(row["attempts"])

    def decrement_attempt(self, job_id: int) -> None:
        self.execute(
            """
            UPDATE messages
            SET attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), job_id),
        )

    def recover_in_progress(self) -> int:
        cursor = self.conn.execute(
            """
            UPDATE messages
            SET status = 'pending',
                last_error = COALESCE(last_error, 'Recovered after interrupted run'),
                updated_at = ?
            WHERE status IN ('downloading', 'uploading')
            """,
            (utc_now(),),
        )
        self.conn.commit()
        return cursor.rowcount

    def due_jobs(self, limit: int, *, reason_code: str | None = None) -> list[sqlite3.Row]:
        now = utc_now()
        reason_clause = ""
        params: list[Any] = [now]
        if reason_code is not None:
            reason_clause = " AND reason_code = ?"
            params.append(reason_code)
        params.append(limit)
        return self.query(
            f"""
            SELECT *
            FROM messages
            WHERE status = 'pending'
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
              {reason_clause}
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            params,
        )

    def remote_uri_for_source(self, source_chat_id: str, file_unique_key: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT remote_uri
            FROM remote_objects
            WHERE source_chat_id = ? AND file_unique_key = ?
            """,
            (source_chat_id, file_unique_key),
        ).fetchone()
        return str(row["remote_uri"]) if row else None

    def record_remote_object(self, source_chat_id: str, file_unique_key: str, remote_uri: str) -> None:
        self.execute(
            """
            INSERT INTO remote_objects (source_chat_id, file_unique_key, remote_uri, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_chat_id, file_unique_key)
            DO UPDATE SET remote_uri = excluded.remote_uri, created_at = excluded.created_at
            """,
            (source_chat_id, file_unique_key, remote_uri, utc_now()),
        )

    def copied_jobs_for_verification(self, limit: int) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT *
            FROM messages
            WHERE status = 'copied'
              AND dest_message_ids IS NOT NULL
              AND verified_at IS NULL
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        )

    def counts_by_status(self) -> dict[str, int]:
        rows = self.query("SELECT status, COUNT(*) AS count FROM messages GROUP BY status")
        return {str(row["status"]): int(row["count"]) for row in rows}
