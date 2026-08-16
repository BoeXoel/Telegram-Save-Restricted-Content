from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import ContentFilterConfig
from app.upload import Uploader


class UploadThumbnailTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_uses_a_new_local_thumbnail_and_removes_it_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reader = _ThumbnailReader()
            writer = _ThumbnailWriter()
            uploader = _uploader(reader, writer)
            message = _MediaMessage("video", 1, [_thumbnail("video-thumb")])
            media_path = _media_path(root, "video.mp4")

            await uploader._upload_downloaded(_job(), [(message, media_path)], writer=writer)

            sent = writer.video_calls[0]
            thumbnail_path = Path(str(sent["thumb"]))
            self.assertEqual(reader.downloaded_file_ids, ["video-thumb"])
            self.assertTrue(writer.thumbnail_paths_existed)
            self.assertTrue(writer.thumbnail_paths_existed[0])
            self.assertEqual(thumbnail_path.parent, root)
            self.assertFalse(thumbnail_path.exists())

    async def test_album_uses_each_video_thumbnail_and_removes_them_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reader = _ThumbnailReader()
            writer = _ThumbnailWriter()
            uploader = _uploader(reader, writer)
            first = _MediaMessage("video", 1, [_thumbnail("first-thumb")])
            second = _MediaMessage("video", 2, [_thumbnail("second-thumb")])

            await uploader._upload_downloaded(
                _job(),
                [(first, _media_path(root, "first.mp4")), (second, _media_path(root, "second.mp4"))],
                writer=writer,
            )

            media = writer.media_group_calls[0]["media"]
            thumbnail_paths = [Path(str(item.thumb)) for item in media]
            self.assertEqual(reader.downloaded_file_ids, ["first-thumb", "second-thumb"])
            self.assertEqual(len(set(thumbnail_paths)), 2)
            self.assertEqual(writer.thumbnail_paths_existed, [True, True])
            self.assertTrue(all(not path.exists() for path in thumbnail_paths))

    async def test_document_uses_a_valid_source_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reader = _ThumbnailReader()
            writer = _ThumbnailWriter()
            uploader = _uploader(reader, writer)
            message = _MediaMessage("document", 3, [_thumbnail("document-thumb")])

            await uploader._upload_downloaded(
                _job(),
                [(message, _media_path(root, "archive.bin"))],
                writer=writer,
            )

            sent = writer.document_calls[0]
            thumbnail_path = Path(str(sent["thumb"]))
            self.assertEqual(reader.downloaded_file_ids, ["document-thumb"])
            self.assertTrue(writer.thumbnail_paths_existed[0])
            self.assertFalse(thumbnail_path.exists())

    async def test_invalid_or_failed_thumbnail_does_not_block_media_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reader = _ThumbnailReader(write_invalid_thumbnail=True)
            writer = _ThumbnailWriter()
            uploader = _uploader(reader, writer)
            message = _MediaMessage("video", 4, [_thumbnail("invalid-thumb")])

            await uploader._upload_downloaded(
                _job(),
                [(message, _media_path(root, "invalid.mp4"))],
                writer=writer,
            )

            self.assertEqual(len(writer.video_calls), 1)
            self.assertNotIn("thumb", writer.video_calls[0])
            self.assertFalse((root / ".thumbnail-4.jpg").exists())

    async def test_thumbnail_is_removed_when_telegram_upload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reader = _ThumbnailReader()
            writer = _ThumbnailWriter(fail_upload=True)
            uploader = _uploader(reader, writer)
            message = _MediaMessage("video", 5, [_thumbnail("failure-thumb")])

            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                await uploader._upload_downloaded(
                    _job(),
                    [(message, _media_path(root, "failure.mp4"))],
                    writer=writer,
                )

            self.assertTrue(writer.thumbnail_paths_existed[0])
            self.assertFalse((root / ".thumbnail-5.jpg").exists())


class _ThumbnailReader:
    def __init__(self, *, write_invalid_thumbnail: bool = False) -> None:
        self.write_invalid_thumbnail = write_invalid_thumbnail
        self.downloaded_file_ids: list[str] = []

    async def download_media(
        self,
        file_id: str,
        *,
        file_name: str,
        progress: object = None,
    ) -> str:
        self.downloaded_file_ids.append(file_id)
        path = Path(file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"not-a-jpeg" if self.write_invalid_thumbnail else b"\xff\xd8\xffthumbnail"
        path.write_bytes(payload)
        if progress is not None:
            await progress(len(payload), len(payload))  # type: ignore[misc]
        return str(path)


class _ThumbnailWriter:
    def __init__(self, *, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.video_calls: list[dict[str, object]] = []
        self.document_calls: list[dict[str, object]] = []
        self.media_group_calls: list[dict[str, object]] = []
        self.thumbnail_paths_existed: list[bool] = []

    async def send_video(self, **kwargs: object) -> object:
        self.video_calls.append(kwargs)
        self._record_thumbnail(kwargs.get("thumb"))
        if self.fail_upload:
            raise RuntimeError("upload failed")
        return SimpleNamespace(id=101)

    async def send_document(self, **kwargs: object) -> object:
        self.document_calls.append(kwargs)
        self._record_thumbnail(kwargs.get("thumb"))
        return SimpleNamespace(id=102)

    async def send_media_group(self, **kwargs: object) -> list[object]:
        self.media_group_calls.append(kwargs)
        for item in kwargs["media"]:  # type: ignore[index]
            self._record_thumbnail(getattr(item, "thumb", None))
        return [SimpleNamespace(id=101), SimpleNamespace(id=102)]

    def _record_thumbnail(self, value: object) -> None:
        if value is not None:
            self.thumbnail_paths_existed.append(Path(str(value)).is_file())


class _MediaMessage:
    def __init__(self, media_type: str, message_id: int, thumbnails: list[object]) -> None:
        self.id = message_id
        self.caption = None
        self.text = None
        self.caption_entities = None
        self.entities = None
        self.has_media_spoiler = False
        self.media_group_id = None
        self.video = None
        self.photo = None
        self.document = None
        self.animation = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.empty = False
        if media_type == "video":
            self.video = SimpleNamespace(
                width=1920,
                height=1080,
                duration=10,
                supports_streaming=True,
                thumbs=thumbnails,
            )
        else:
            self.document = SimpleNamespace(thumbs=thumbnails)


class _Storage:
    def __init__(self) -> None:
        self.progress_reservations: list[int] = []

    def ensure_progress_reservation(self, remaining_bytes: int) -> None:
        self.progress_reservations.append(remaining_bytes)


class _Limiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]


def _thumbnail(file_id: str) -> object:
    return SimpleNamespace(file_id=file_id, width=320, height=180, file_size=10)


def _media_path(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"media")
    return path


def _uploader(reader: _ThumbnailReader, writer: _ThumbnailWriter) -> Uploader:
    config = SimpleNamespace(
        filters=ContentFilterConfig(enabled=False, case_sensitive=False, keywords=(), regex=()),
        transfer=SimpleNamespace(drop_caption=False),
    )
    return Uploader(config, reader, writer, _Limiter(), storage=_Storage())


def _job() -> object:
    return SimpleNamespace(dest_chat_id="-1002", dest_topic_id=None)


if __name__ == "__main__":
    unittest.main()
