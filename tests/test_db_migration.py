from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import Database
from app.queue import MessageQueue


class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_database_gets_additive_metadata_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    dest_chat_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
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
                    verified_at TEXT
                );
                INSERT INTO messages (
                    source_chat_id, source_message_id, dest_chat_id, status,
                    file_unique_key, created_at, updated_at, source_message_ids
                ) VALUES ('source', 1, 'destination', 'pending', 'old-job',
                          '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '[1]');
                """
            )
            connection.commit()
            connection.close()

            database = Database(path)
            database.initialize()
            database.set_status(
                1,
                "skipped",
                last_error="Skipped because it exceeds the upload limit",
                reason_code="oversized",
                transfer_route="record",
                media_manifest=[{"message_id": 1, "size": 123}],
                writer_identity="bot:123",
                upload_limit_bytes=100,
            )
            row = database.query("SELECT * FROM messages WHERE id = 1")[0]
            database.close()

            self.assertEqual(row["source_chat_id"], "source")
            self.assertEqual(row["reason_code"], "oversized")
            self.assertEqual(row["transfer_route"], "record")
            self.assertEqual(row["media_manifest"], '[{"message_id": 1, "size": 123}]')
            self.assertEqual(row["writer_identity"], "bot:123")
            self.assertEqual(row["upload_limit_bytes"], 100)

    def test_deferred_job_does_not_consume_a_transfer_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "queue.sqlite3")
            database.initialize()
            queue = MessageQueue(
                database,
                SimpleNamespace(queue=SimpleNamespace(max_attempts=4, retry_backoff_seconds=[1])),
            )
            queue.enqueue(
                source_chat_id="source",
                source_message_id=1,
                dest_chat_id="destination",
                file_unique_key="job-1",
                source_message_ids=[1],
                source_topic_id=None,
                dest_topic_id=None,
                media_group_id=None,
                media_type="photo",
                file_size=10,
                caption=None,
            )
            job = queue.fetch_due(1)[0]
            attempts = queue.start_attempt(job)
            queue.defer(
                job,
                "Not enough free disk space",
                reason_code="disk_low",
                retry_after_seconds=60,
            )
            row = database.query("SELECT attempts, status, reason_code, next_retry_at FROM messages")[0]
            database.close()

            self.assertEqual(attempts, 1)
            self.assertEqual(row["attempts"], 0)
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["reason_code"], "disk_low")
            self.assertIsNotNone(row["next_retry_at"])


if __name__ == "__main__":
    unittest.main()
