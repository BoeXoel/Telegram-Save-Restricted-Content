from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.report import build_oversized_report, caption_summary, redact_uri, source_link


class OversizedReportTests(unittest.TestCase):
    def test_report_expands_an_album_into_individual_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "queue.sqlite3")
            database.initialize()
            database.enqueue_message(
                source_chat_id="-10012345",
                source_message_id=10,
                dest_chat_id="-10067890",
                file_unique_key="album-key",
                source_message_ids=[10, 11],
                source_topic_id=77,
                dest_topic_id=None,
                media_group_id="album",
                media_type="album",
                file_size=30,
                caption="A caption\nwith spacing",
                status="skipped",
                last_error="File is above the writer limit",
                reason_code="oversized",
                transfer_route="remote",
                remote_uri="https://name:password@cloud.example/archive/object",
                writer_identity="bot:42",
                upload_limit_bytes=20,
                media_manifest=[
                    {"message_id": 10, "type": "photo", "size": 10},
                    {"message_id": 11, "type": "video", "size": 20},
                ],
            )

            report = build_oversized_report(database)
            csv_path = root / "oversized.csv"
            report.write_csv(csv_path)
            database.close()

            self.assertEqual(len(report.rows), 2)
            self.assertEqual(report.rows[0]["source_link"], "https://t.me/c/12345/77/10")
            self.assertEqual(report.rows[1]["media_type"], "video")
            self.assertEqual(report.rows[0]["caption_summary"], "A caption with spacing")
            self.assertEqual(report.rows[0]["remote_uri"], "https://cloud.example/archive/object")
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["size_bytes"], "20")

    def test_helpers_produce_safe_compact_fields(self) -> None:
        self.assertEqual(source_link("@source", 3, None), "https://t.me/source/3")
        self.assertEqual(source_link("-999", 3, None), "")
        self.assertEqual(redact_uri("https://user:secret@example.test/path"), "https://example.test/path")
        self.assertEqual(caption_summary("x" * 200), "x" * 159 + "…")


if __name__ == "__main__":
    unittest.main()
