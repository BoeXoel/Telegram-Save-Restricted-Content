from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import ContentFilterConfig, OversizedConfig, OversizedRemoteConfig, load_config
from app.db import Database
from app.errors import DiskLowError
from app.offload import RemoteOffloader
from app.telegram_client import WriterCapabilities
from app.upload import Uploader


class RemoteOffloaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_rclone_upload_uses_the_preconfigured_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "queue.sqlite3")
            database.initialize()
            offloader = RemoteOffloader(_oversized_config(), database)
            process = _CompletedProcess()

            with patch("app.offload.asyncio.create_subprocess_exec", return_value=process) as create:
                await offloader.upload_file(Path(temp_dir) / "file.bin", "archive:telegram/object/file.bin")

            self.assertEqual(
                create.call_args.args,
                ("rclone", "copyto", str(Path(temp_dir) / "file.bin"), "archive:telegram/object/file.bin", "--fast-list"),
            )
            database.close()

    async def test_low_disk_uses_a_remote_stream_without_an_active_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active_dir = Path(temp_dir) / "active"
            message = _MediaMessage(101)
            offloader = _RecordingOffloader()
            uploader = Uploader(
                _uploader_config(active_dir),
                _StreamingReader([message]),
                object(),
                _Limiter(),
                writer_capabilities=WriterCapabilities(
                    identity="bot:10",
                    account_type="bot",
                    is_premium=False,
                    max_upload_bytes=100,
                ),
                storage=_LowStorage(),
                offloader=offloader,  # type: ignore[arg-type]
            )

            result = await uploader.process(_job(), _NeverSetEvent(), _allowed_phase)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason_code, "oversized")
            self.assertEqual(result.transfer_route, "remote")
            self.assertEqual(result.remote_uri, "archive:telegram/source/object")
            self.assertEqual(offloader.streamed, [b"first", b"second"])
            self.assertTrue(offloader.recorded)
            self.assertFalse((active_dir / "job-7-remote").exists())


class RemoteObjectDatabaseTests(unittest.TestCase):
    def test_completed_source_is_reused_for_another_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "queue.sqlite3")
            database.initialize()
            offloader = RemoteOffloader(_oversized_config(), database)
            first_job = _job(dest_chat_id="destination-a")
            second_job = _job(dest_chat_id="destination-b")
            directory = offloader.directory_for(first_job)

            offloader.record_completed(first_job, directory)

            self.assertEqual(offloader.existing_uri(second_job), directory)
            self.assertNotIn("password", directory.lower())
            database.close()

    def test_enabled_webdav_requires_https_and_environment_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "telegram:\n"
                "  api_id: 1\n"
                "  api_hash: 0123456789abcdef0123456789abcdef\n"
                "transfer:\n"
                "  oversized:\n"
                "    action: remote\n"
                "    remote:\n"
                "      enabled: true\n"
                "      method: webdav\n"
                "      dest: http://cloud.example/dav\n"
                "      username: user\n"
                "      password: secret\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "HTTPS WebDAV URL"):
                load_config(config_path)


class _CompletedProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


class _RecordingOffloader:
    enabled = True

    def __init__(self) -> None:
        self.streamed: list[bytes] = []
        self.recorded = False

    def existing_uri(self, _job: object) -> None:
        return None

    def directory_for(self, _job: object) -> str:
        return "archive:telegram/source/object"

    def file_uri(self, directory: str, file_name: str) -> str:
        return f"{directory}/{file_name}"

    async def upload_stream(self, chunks: object, _remote_uri: str, *, size: int | None) -> None:
        self.assert_size = size
        async for chunk in chunks:  # type: ignore[union-attr]
            self.streamed.append(chunk)

    def record_completed(self, _job: object, _remote_uri: str) -> None:
        self.recorded = True


class _LowStorage:
    def ensure_job_reservation(self, _required_bytes: int) -> None:
        raise DiskLowError("Test disk reserve reached")


class _MediaMessage:
    def __init__(self, size: int) -> None:
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


class _StreamingReader:
    def __init__(self, messages: list[_MediaMessage]) -> None:
        self.messages = messages

    async def get_messages(self, _chat_id: str, _message_ids: list[int]) -> list[_MediaMessage]:
        return self.messages

    async def stream_media(self, _message: _MediaMessage):
        yield b"first"
        yield b"second"


class _Limiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]

    async def wait(self, _operation: str) -> None:
        return


class _NeverSetEvent:
    def is_set(self) -> bool:
        return False


async def _allowed_phase(_phase: str) -> None:
    return


def _oversized_config() -> OversizedConfig:
    return OversizedConfig(
        action="remote",
        remote=OversizedRemoteConfig(
            enabled=True,
            method="rclone",
            dest="archive:telegram",
            extra_args=("--fast-list",),
            delete_local_after=True,
            webdav_username="",
            webdav_password="",
            timeout_seconds=60,
        ),
    )


def _uploader_config(active_dir: Path) -> object:
    return SimpleNamespace(
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
            max_bot_upload_bytes=100,
            oversized=_oversized_config(),
        ),
        downloads=SimpleNamespace(
            root=active_dir.parent,
            active_dir=active_dir,
            failed_dir=active_dir.parent / "failed",
            completed_dir=active_dir.parent / "completed",
            keep_completed=False,
            keep_failed=False,
            min_free_bytes=0,
            max_failed_bytes=0,
            max_job_bytes=0,
        ),
    )


def _job(*, dest_chat_id: str = "destination") -> object:
    return SimpleNamespace(
        id=7,
        source_chat_id="-1001",
        source_message_id=1,
        source_message_ids=[1],
        dest_chat_id=dest_chat_id,
        dest_topic_id=None,
        file_unique_key="photo-key",
    )


if __name__ == "__main__":
    unittest.main()
