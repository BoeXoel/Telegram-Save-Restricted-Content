from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database


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


if __name__ == "__main__":
    unittest.main()
