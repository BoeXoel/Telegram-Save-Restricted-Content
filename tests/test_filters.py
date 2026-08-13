from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import ChatSpec, ContentFilterConfig, load_config
from app.filters import ContentFilter
from app.scanner import Scanner
from app.telegram_client import ResolvedChat, WriterCapabilities
from app.upload import Uploader


class ContentFilterTests(unittest.TestCase):
    def test_disabled_filter_does_not_match(self) -> None:
        content_filter = ContentFilter(
            ContentFilterConfig(
                enabled=False,
                case_sensitive=False,
                keywords=("sponsor",),
                regex=(r"https?://",),
            )
        )

        self.assertIsNone(content_filter.match_text("Sponsor: https://example.test"))

    def test_keyword_match_is_case_insensitive_by_default(self) -> None:
        content_filter = ContentFilter(
            ContentFilterConfig(
                enabled=True,
                case_sensitive=False,
                keywords=("sponsor",),
                regex=(),
            )
        )

        match = content_filter.match_text("This post has a SPONSOR message")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.reason_code, "ad_filtered")
        self.assertEqual(match.reason, "Skipped by keyword filter rule #1")

    def test_regex_match_uses_a_privacy_safe_reason(self) -> None:
        content_filter = ContentFilter(
            ContentFilterConfig(
                enabled=True,
                case_sensitive=False,
                keywords=(),
                regex=(r"https?://\S+",),
            )
        )

        match = content_filter.match_texts(["A caption", "Visit HTTPS://ads.example/test"])

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.reason, "Skipped by regex filter rule #1")
        self.assertNotIn("ads.example", match.reason)

    def test_config_defaults_to_disabled_and_rejects_invalid_regex(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "telegram:\n"
                "  api_id: 1\n"
                "  api_hash: 0123456789abcdef0123456789abcdef\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertFalse(config.filters.enabled)
            self.assertFalse(config.filters.case_sensitive)
            self.assertEqual(config.filters.keywords, ())
            self.assertFalse(config.transfer.allow_premium_user_fallback)

            config_path.write_text(
                "telegram:\n"
                "  api_id: 1\n"
                "  api_hash: 0123456789abcdef0123456789abcdef\n"
                "filters:\n"
                "  regex: ['[']\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "filters.regex rule #1 is invalid"):
                load_config(config_path)


class FilterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_records_an_entire_matching_album_as_skipped(self) -> None:
        config = _test_config()
        queue = _RecordingQueue()
        scanner = Scanner(config, queue, _FakeReader([_FakeMessage(caption="Sponsor details")]), _FakeLimiter())

        await scanner._scan_source(
            ChatSpec(chat="source", start_id=1, end_id=1),
            [ResolvedChat(chat_id="destination", topic_id=None, title="Destination")],
            _NeverSetEvent(),
        )

        self.assertEqual(len(queue.calls), 1)
        job = queue.calls[0]
        self.assertEqual(job["status"], "skipped")
        self.assertEqual(job["reason_code"], "ad_filtered")
        self.assertEqual(job["last_error"], "Skipped by keyword filter rule #1")

    async def test_processing_rechecks_filters_for_existing_queue_jobs(self) -> None:
        config = _test_config()
        uploader = Uploader(
            config,
            _FakeReader([_FakeMessage(caption="SPONSOR details")]),
            object(),
            _FakeLimiter(),
        )
        job = SimpleNamespace(source_chat_id="source", source_message_ids=[1])

        result = await uploader.process(job, _NeverSetEvent(), _unexpected_phase)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason_code, "ad_filtered")
        self.assertEqual(result.reason, "Skipped by keyword filter rule #1")

    async def test_scan_marks_known_oversized_media_for_the_special_processor(self) -> None:
        config = _test_config()
        config.filters = ContentFilterConfig(enabled=False, case_sensitive=False, keywords=(), regex=())
        queue = _RecordingQueue()
        scanner = Scanner(
            config,
            queue,
            _FakeReader([_FakeMessage(size=101)]),
            _FakeLimiter(),
            writer_capabilities=WriterCapabilities(
                identity="bot:10",
                account_type="bot",
                is_premium=False,
                max_upload_bytes=100,
            ),
        )

        await scanner._scan_source(
            ChatSpec(chat="source", start_id=1, end_id=1),
            [ResolvedChat(chat_id="destination", topic_id=None, title="Destination")],
            _NeverSetEvent(),
        )

        self.assertEqual(queue.calls[0]["status"], "pending")
        self.assertEqual(queue.calls[0]["reason_code"], "oversized")
        self.assertEqual(queue.calls[0]["transfer_route"], "pending")


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        return True


class _FakeLimiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]


class _FakeReader:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages

    async def get_chat(self, chat_id: str) -> object:
        return SimpleNamespace(id=chat_id, title=chat_id, username=None)

    async def get_messages(self, _chat_id: str, _message_ids: list[int]) -> list[object]:
        return self.messages


class _FakeMessage:
    def __init__(self, *, caption: str | None = None, size: int = 10) -> None:
        self.id = 1
        self.caption = caption
        self.text = None
        self.media_group_id = None
        self.video = None
        self.photo = SimpleNamespace(file_size=size, file_unique_id="photo-1", file_id="photo-1")
        self.document = None
        self.animation = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.empty = False


class _NeverSetEvent:
    def is_set(self) -> bool:
        return False


async def _unexpected_phase(_status: str) -> None:
    raise AssertionError("A filtered queued message must not start a transfer")


def _test_config() -> object:
    return SimpleNamespace(
        filters=ContentFilterConfig(
            enabled=True,
            case_sensitive=False,
            keywords=("sponsor",),
            regex=(),
        ),
        queue=SimpleNamespace(record_skipped=True),
        limits=SimpleNamespace(get_messages_chunk_size=100),
        transfer=SimpleNamespace(
            include_videos=True,
            include_photos=True,
            include_text=True,
            include_documents=True,
        ),
        downloads=SimpleNamespace(),
    )


if __name__ == "__main__":
    unittest.main()
