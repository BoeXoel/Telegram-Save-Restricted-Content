from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import ContentFilterConfig
from app.telegram_client import (
    BOT_UPLOAD_LIMIT_BYTES,
    PREMIUM_USER_UPLOAD_LIMIT_BYTES,
    STANDARD_USER_UPLOAD_LIMIT_BYTES,
    WriterCapabilities,
    get_writer_capabilities,
)
from app.upload import Uploader


class WriterCapabilityTests(unittest.TestCase):
    def test_detects_bot_standard_and_premium_limits(self) -> None:
        config = _capability_config()

        bot = get_writer_capabilities(config, SimpleNamespace(id=10, is_bot=True, is_premium=True))
        user = get_writer_capabilities(config, SimpleNamespace(id=20, is_bot=False, is_premium=False))
        premium = get_writer_capabilities(config, SimpleNamespace(id=30, is_bot=False, is_premium=True))

        self.assertEqual((bot.account_type, bot.max_upload_bytes), ("bot", BOT_UPLOAD_LIMIT_BYTES))
        self.assertEqual((user.account_type, user.max_upload_bytes), ("user", STANDARD_USER_UPLOAD_LIMIT_BYTES))
        self.assertEqual(
            (premium.account_type, premium.max_upload_bytes),
            ("premium_user", PREMIUM_USER_UPLOAD_LIMIT_BYTES),
        )

    def test_explicit_upload_limit_overrides_account_detection(self) -> None:
        config = _capability_config(max_upload_bytes=123)

        capability = get_writer_capabilities(config, SimpleNamespace(id=30, is_bot=False, is_premium=True))

        self.assertEqual(capability.max_upload_bytes, 123)


class LocalUploadLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_album_never_creates_an_active_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active_dir = Path(temp_dir) / "active"
            messages = [_MediaMessage(10), _MediaMessage(101)]
            uploader = _uploader(active_dir, messages, limit=100)

            result = await uploader.process(_job(), _NeverSetEvent(), _unexpected_phase)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason_code, "oversized")
            self.assertIn("source chat -1001, message 1", result.reason)
            self.assertIn("101 bytes", result.reason)
            self.assertFalse((active_dir / "job-7").exists())
            self.assertFalse(any(message.download_called for message in messages))

    async def test_unknown_size_is_not_downloaded_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active_dir = Path(temp_dir) / "active"
            message = _MediaMessage(None)
            uploader = _uploader(active_dir, [message], limit=100)

            result = await uploader.process(_job(), _NeverSetEvent(), _unexpected_phase)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason_code, "unknown_size")
            self.assertIn("source chat -1001, message 1", result.reason)
            self.assertFalse((active_dir / "job-7").exists())
            self.assertFalse(message.download_called)


class _MediaMessage:
    def __init__(self, size: int | None) -> None:
        self.id = 1
        self.caption = None
        self.text = None
        self.caption_entities = None
        self.entities = None
        self.media_group_id = None
        self.video = None
        self.photo = SimpleNamespace(file_size=size, file_unique_id="photo", file_id="photo")
        self.document = None
        self.animation = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.empty = False
        self.download_called = False

    async def download(self, **_kwargs: object) -> str:
        self.download_called = True
        raise AssertionError("A rejected file must not be downloaded")


class _Reader:
    def __init__(self, messages: list[_MediaMessage]) -> None:
        self.messages = messages

    async def get_messages(self, _chat_id: str, _message_ids: list[int]) -> list[_MediaMessage]:
        return self.messages


class _Limiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]


class _NeverSetEvent:
    def is_set(self) -> bool:
        return False


async def _unexpected_phase(_phase: str) -> None:
    raise AssertionError("A rejected file must not enter a transfer phase")


def _capability_config(*, max_upload_bytes: int = 0) -> object:
    return SimpleNamespace(
        transfer=SimpleNamespace(
            max_upload_bytes=max_upload_bytes,
            max_bot_upload_bytes=BOT_UPLOAD_LIMIT_BYTES,
        )
    )


def _uploader(active_dir: Path, messages: list[_MediaMessage], *, limit: int) -> Uploader:
    config = SimpleNamespace(
        filters=ContentFilterConfig(enabled=False, case_sensitive=False, keywords=(), regex=()),
        transfer=SimpleNamespace(
            include_videos=True,
            include_photos=True,
            include_text=True,
            include_documents=True,
            prefer_copy=False,
            forwarding_only=False,
            hide_sender=True,
            drop_caption=False,
            save_to_local=False,
            allow_download_unknown_size=False,
            max_upload_bytes=0,
            max_bot_upload_bytes=limit,
        ),
        downloads=SimpleNamespace(active_dir=active_dir, keep_completed=False, keep_failed=False),
    )
    return Uploader(
        config,
        _Reader(messages),
        object(),
        _Limiter(),
        writer_capabilities=WriterCapabilities(
            identity="bot:10",
            account_type="bot",
            is_premium=False,
            max_upload_bytes=limit,
        ),
    )


def _job() -> object:
    return SimpleNamespace(
        id=7,
        source_chat_id="-1001",
        source_message_ids=[1],
        dest_chat_id="-1002",
        dest_topic_id=None,
    )


if __name__ == "__main__":
    unittest.main()
