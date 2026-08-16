from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import ContentFilterConfig
from app.upload import Uploader


class VideoMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_video_preserves_positive_dimensions_and_duration(self) -> None:
        writer = _Writer()
        uploader = _uploader(writer)
        message = _VideoMessage(width=1920, height=1080, duration=95)

        await uploader._upload_downloaded(_job(), [(message, Path("landscape.mp4"))], writer=writer)

        sent = writer.video_calls[0]
        self.assertEqual(sent["width"], 1920)
        self.assertEqual(sent["height"], 1080)
        self.assertEqual(sent["duration"], 95)
        self.assertTrue(sent["supports_streaming"])

    async def test_album_preserves_metadata_for_each_video(self) -> None:
        writer = _Writer()
        uploader = _uploader(writer)
        first = _VideoMessage(width=1920, height=1080, duration=95)
        second = _VideoMessage(width=1080, height=1920, duration=44)

        await uploader._upload_downloaded(
            _job(),
            [(first, Path("landscape.mp4")), (second, Path("portrait.mp4"))],
            writer=writer,
        )

        media = writer.media_group_calls[0]["media"]
        self.assertEqual((media[0].width, media[0].height, media[0].duration), (1920, 1080, 95))
        self.assertEqual((media[1].width, media[1].height, media[1].duration), (1080, 1920, 44))

    async def test_invalid_metadata_is_omitted_instead_of_sent_as_zero(self) -> None:
        writer = _Writer()
        uploader = _uploader(writer)
        message = _VideoMessage(width=0, height="invalid", duration=-1)

        await uploader._upload_downloaded(_job(), [(message, Path("unknown.mp4"))], writer=writer)

        sent = writer.video_calls[0]
        self.assertNotIn("width", sent)
        self.assertNotIn("height", sent)
        self.assertNotIn("duration", sent)

    async def test_partially_valid_metadata_keeps_only_valid_values(self) -> None:
        writer = _Writer()
        uploader = _uploader(writer)
        message = _VideoMessage(width=1280, height=0, duration=30)

        await uploader._upload_downloaded(_job(), [(message, Path("partial.mp4"))], writer=writer)

        sent = writer.video_calls[0]
        self.assertEqual(sent["width"], 1280)
        self.assertEqual(sent["duration"], 30)
        self.assertNotIn("height", sent)

    async def test_source_streaming_flag_is_preserved_when_known(self) -> None:
        writer = _Writer()
        uploader = _uploader(writer)
        message = _VideoMessage(width=1280, height=720, duration=30, supports_streaming=False)

        await uploader._upload_downloaded(_job(), [(message, Path("download.mp4"))], writer=writer)

        self.assertFalse(writer.video_calls[0]["supports_streaming"])


class _Writer:
    def __init__(self) -> None:
        self.video_calls: list[dict[str, object]] = []
        self.media_group_calls: list[dict[str, object]] = []

    async def send_video(self, **kwargs: object) -> object:
        self.video_calls.append(kwargs)
        return SimpleNamespace(id=101)

    async def send_media_group(self, **kwargs: object) -> list[object]:
        self.media_group_calls.append(kwargs)
        return [SimpleNamespace(id=101), SimpleNamespace(id=102)]


class _VideoMessage:
    def __init__(
        self,
        *,
        width: object,
        height: object,
        duration: object,
        supports_streaming: object = None,
    ) -> None:
        self.id = 1
        self.caption = None
        self.text = None
        self.caption_entities = None
        self.entities = None
        self.media_group_id = None
        self.video = SimpleNamespace(
            width=width,
            height=height,
            duration=duration,
            supports_streaming=supports_streaming,
        )
        self.photo = None
        self.document = None
        self.animation = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.empty = False


def _uploader(writer: _Writer) -> Uploader:
    config = SimpleNamespace(
        filters=ContentFilterConfig(enabled=False, case_sensitive=False, keywords=(), regex=()),
        transfer=SimpleNamespace(drop_caption=False),
    )
    return Uploader(config, writer, writer, _Limiter(), storage=SimpleNamespace())


def _job() -> object:
    return SimpleNamespace(dest_chat_id="-1002", dest_topic_id=None)


class _Limiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
