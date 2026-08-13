from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import ContentFilterConfig
from app.errors import DiskFullError, DiskLowError
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

    async def test_enospc_removes_the_active_job_instead_of_caching_it_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active_dir = Path(temp_dir) / "active"
            message = _EnospcMediaMessage(10)
            uploader = _uploader(
                active_dir,
                [message],
                limit=100,
                keep_failed=True,
                max_failed_bytes=100,
            )

            with self.assertRaises(DiskFullError):
                await uploader.process(_job(), _NeverSetEvent(), _allowed_phase)

            self.assertTrue(message.download_called)
            self.assertFalse((active_dir / "job-7").exists())
            self.assertFalse((active_dir.parent / "failed" / "job-7").exists())

    async def test_low_disk_space_blocks_the_job_before_any_download_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active_dir = Path(temp_dir) / "active"
            message = _MediaMessage(10)
            uploader = _uploader(active_dir, [message], limit=100, storage=_LowStorage())

            with self.assertRaises(DiskLowError):
                await uploader.process(_job(), _NeverSetEvent(), _unexpected_phase)

            self.assertFalse((active_dir / "job-7").exists())
            self.assertFalse(message.download_called)

    async def test_explicit_premium_fallback_uses_the_reader_after_permission_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active_dir = Path(temp_dir) / "active"
            message = _DownloadableMediaMessage(150)
            reader = _PremiumFallbackReader([message], can_post=True)
            uploader = _uploader(
                active_dir,
                [message],
                limit=100,
                reader=reader,
                writer=_BotWriter(),
                allow_premium_user_fallback=True,
                fallback_writer=reader,
                fallback_writer_capabilities=WriterCapabilities(
                    identity="premium_user:30",
                    account_type="premium_user",
                    is_premium=True,
                    max_upload_bytes=200,
                    account_id=30,
                ),
            )

            result = await uploader.process(_job(), _NeverSetEvent(), _allowed_phase)

            self.assertEqual(result.status, "copied")
            self.assertEqual(result.writer_identity, "premium_user:30")
            self.assertEqual(result.upload_limit_bytes, 200)
            self.assertTrue(message.download_called)
            self.assertEqual(reader.checked_members, [("-1002", 30)])
            self.assertEqual(len(reader.sent_photos), 1)
            self.assertFalse((active_dir / "job-7").exists())

    async def test_premium_fallback_does_not_download_when_target_is_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active_dir = Path(temp_dir) / "active"
            message = _DownloadableMediaMessage(150)
            reader = _PremiumFallbackReader([message], can_post=False)
            uploader = _uploader(
                active_dir,
                [message],
                limit=100,
                reader=reader,
                writer=_BotWriter(),
                allow_premium_user_fallback=True,
                fallback_writer=reader,
                fallback_writer_capabilities=WriterCapabilities(
                    identity="premium_user:30",
                    account_type="premium_user",
                    is_premium=True,
                    max_upload_bytes=200,
                    account_id=30,
                ),
            )

            result = await uploader.process(_job(), _NeverSetEvent(), _unexpected_phase)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason_code, "oversized")
            self.assertFalse(message.download_called)
            self.assertEqual(reader.checked_members, [("-1002", 30)])
            self.assertEqual(reader.sent_photos, [])


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


class _EnospcMediaMessage(_MediaMessage):
    async def download(self, **kwargs: object) -> str:
        self.download_called = True
        path = Path(str(kwargs["file_name"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")


class _DownloadableMediaMessage(_MediaMessage):
    async def download(self, **kwargs: object) -> str:
        self.download_called = True
        path = Path(str(kwargs["file_name"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        size = self.photo.file_size
        path.write_bytes(b"x" * int(size))
        return str(path)


class _Reader:
    def __init__(self, messages: list[_MediaMessage]) -> None:
        self.messages = messages

    async def get_messages(self, _chat_id: str, _message_ids: list[int]) -> list[_MediaMessage]:
        return self.messages


class _BotWriter:
    async def send_photo(self, **_kwargs: object) -> object:
        raise AssertionError("The bot must not upload through the Premium fallback path")


class _PremiumFallbackReader(_Reader):
    def __init__(self, messages: list[_MediaMessage], *, can_post: bool) -> None:
        super().__init__(messages)
        self.can_post = can_post
        self.checked_members: list[tuple[str, int]] = []
        self.sent_photos: list[dict[str, object]] = []

    async def get_chat(self, _chat_id: str) -> object:
        return SimpleNamespace(type="supergroup")

    async def get_chat_member(self, chat_id: str, user_id: int) -> object:
        self.checked_members.append((chat_id, user_id))
        return SimpleNamespace(
            status="member" if self.can_post else "restricted",
            permissions=SimpleNamespace(can_send_messages=self.can_post),
            privileges=None,
        )

    async def send_photo(self, **kwargs: object) -> object:
        self.sent_photos.append(kwargs)
        return SimpleNamespace(id=101)


class _Limiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]


class _LowStorage:
    def ensure_job_reservation(self, _required_bytes: int) -> None:
        raise DiskLowError("Test disk reserve reached")


class _NeverSetEvent:
    def is_set(self) -> bool:
        return False


async def _unexpected_phase(_phase: str) -> None:
    raise AssertionError("A rejected file must not enter a transfer phase")


async def _allowed_phase(_phase: str) -> None:
    return


def _capability_config(*, max_upload_bytes: int = 0) -> object:
    return SimpleNamespace(
        transfer=SimpleNamespace(
            max_upload_bytes=max_upload_bytes,
            max_bot_upload_bytes=BOT_UPLOAD_LIMIT_BYTES,
        )
    )


def _uploader(
    active_dir: Path,
    messages: list[_MediaMessage],
    *,
    limit: int,
    keep_failed: bool = False,
    max_failed_bytes: int = 0,
    storage: object | None = None,
    reader: object | None = None,
    writer: object | None = None,
    allow_premium_user_fallback: bool = False,
    fallback_writer: object | None = None,
    fallback_writer_capabilities: WriterCapabilities | None = None,
) -> Uploader:
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
            allow_premium_user_fallback=allow_premium_user_fallback,
            max_upload_bytes=0,
            max_bot_upload_bytes=limit,
        ),
        downloads=SimpleNamespace(
            root=active_dir.parent,
            active_dir=active_dir,
            failed_dir=active_dir.parent / "failed",
            completed_dir=active_dir.parent / "completed",
            keep_completed=False,
            keep_failed=keep_failed,
            min_free_bytes=0,
            max_failed_bytes=max_failed_bytes,
            max_job_bytes=0,
        ),
    )
    return Uploader(
        config,
        reader or _Reader(messages),
        writer or object(),
        _Limiter(),
        writer_capabilities=WriterCapabilities(
            identity="bot:10",
            account_type="bot",
            is_premium=False,
            max_upload_bytes=limit,
        ),
        fallback_writer=fallback_writer,
        fallback_writer_capabilities=fallback_writer_capabilities,
        storage=storage,
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
