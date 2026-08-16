from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from pyrogram import enums

from app.config import ContentFilterConfig
from app.upload import Uploader


class SpoilerRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_spoiler_bypasses_native_copy_and_keeps_other_entities(self) -> None:
        spoiler = _entity(enums.MessageEntityType.SPOILER)
        bold = _entity(enums.MessageEntityType.BOLD)
        message = _TextMessage(entities=[spoiler, bold])
        writer = _Writer([message])
        uploader = _uploader(writer)

        result = await uploader.process(_job(), _NeverSetEvent(), _allowed_phase)

        self.assertEqual(result.status, "copied")
        self.assertEqual(writer.copy_calls, [])
        self.assertEqual(writer.sent_text_calls[0]["entities"], [bold])
        self.assertIsNone(writer.sent_text_calls[0]["parse_mode"])

    async def test_all_text_spoilers_disable_parsing_instead_of_recreating_a_spoiler(self) -> None:
        message = _TextMessage(entities=[_entity(enums.MessageEntityType.SPOILER)])
        writer = _Writer([message])
        uploader = _uploader(writer)

        await uploader.process(_job(), _NeverSetEvent(), _allowed_phase)

        sent = writer.sent_text_calls[0]
        self.assertIsNone(sent["entities"])
        self.assertEqual(sent["parse_mode"], enums.ParseMode.DISABLED)

    async def test_native_copy_only_skips_a_spoiler_instead_of_preserving_it(self) -> None:
        message = _TextMessage(entities=[_entity(enums.MessageEntityType.SPOILER)])
        writer = _Writer([message])
        uploader = _uploader(writer, forwarding_only=True)

        result = await uploader.process(_job(), _NeverSetEvent(), _allowed_phase)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason_code, "spoiler_removal_requires_reupload")
        self.assertEqual(writer.copy_calls, [])
        self.assertEqual(writer.sent_text_calls, [])

    async def test_local_video_caption_strips_spoiler_entity(self) -> None:
        spoiler = _entity(enums.MessageEntityType.SPOILER)
        italic = _entity(enums.MessageEntityType.ITALIC)
        message = _VideoMessage(caption_entities=[spoiler, italic])
        writer = _Writer([])
        uploader = _uploader(writer)

        await uploader._upload_downloaded(
            _job(),
            [(message, Path("source.mp4"))],
            writer=writer,
        )

        sent = writer.sent_video_calls[0]
        self.assertEqual(sent["caption_entities"], [italic])
        self.assertIsNone(sent["parse_mode"])
        self.assertNotIn("has_spoiler", sent)

    async def test_album_caption_with_only_a_spoiler_disables_parsing(self) -> None:
        message = _VideoMessage(caption_entities=[_entity(enums.MessageEntityType.SPOILER)])
        second = _VideoMessage()
        writer = _Writer([])
        uploader = _uploader(writer)

        await uploader._upload_downloaded(
            _job(),
            [(message, Path("first.mp4")), (second, Path("second.mp4"))],
            writer=writer,
        )

        first = writer.media_group_calls[0]["media"][0]
        self.assertIsNone(first.caption_entities)
        self.assertEqual(first.parse_mode, enums.ParseMode.DISABLED)
        self.assertIsNone(first.has_spoiler)

    def test_media_spoiler_requires_local_reupload(self) -> None:
        writer = _Writer([])
        uploader = _uploader(writer)
        message = _VideoMessage(has_media_spoiler=True)

        self.assertTrue(uploader._messages_require_spoiler_removal([message]))


class _Writer:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages
        self.copy_calls: list[dict[str, object]] = []
        self.sent_text_calls: list[dict[str, object]] = []
        self.sent_video_calls: list[dict[str, object]] = []
        self.media_group_calls: list[dict[str, object]] = []

    async def get_messages(self, _chat_id: str, _message_ids: list[int]) -> list[object]:
        return self.messages

    async def copy_message(self, **kwargs: object) -> object:
        self.copy_calls.append(kwargs)
        return SimpleNamespace(id=100)

    async def send_message(self, **kwargs: object) -> object:
        self.sent_text_calls.append(kwargs)
        return SimpleNamespace(id=101)

    async def send_video(self, **kwargs: object) -> object:
        self.sent_video_calls.append(kwargs)
        return SimpleNamespace(id=102)

    async def send_media_group(self, **kwargs: object) -> list[object]:
        self.media_group_calls.append(kwargs)
        return [SimpleNamespace(id=103), SimpleNamespace(id=104)]


class _TextMessage:
    def __init__(self, *, entities: list[object]) -> None:
        self.id = 1
        self.text = "An ordinary message"
        self.caption = None
        self.entities = entities
        self.caption_entities = None
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


class _VideoMessage:
    def __init__(
        self,
        *,
        caption_entities: list[object] | None = None,
        has_media_spoiler: bool = False,
    ) -> None:
        self.id = 2
        self.text = None
        self.caption = "A caption"
        self.entities = None
        self.caption_entities = caption_entities
        self.has_media_spoiler = has_media_spoiler
        self.media_group_id = None
        self.video = SimpleNamespace(
            width=1920,
            height=1080,
            duration=20,
            supports_streaming=True,
            thumbs=None,
        )
        self.photo = None
        self.document = None
        self.animation = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.empty = False


class _Limiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]


class _NeverSetEvent:
    def is_set(self) -> bool:
        return False


async def _allowed_phase(_phase: str) -> None:
    return


def _entity(entity_type: enums.MessageEntityType) -> object:
    return SimpleNamespace(type=entity_type, offset=0, length=1)


def _uploader(writer: _Writer, *, forwarding_only: bool = False) -> Uploader:
    config = SimpleNamespace(
        filters=ContentFilterConfig(enabled=False, case_sensitive=False, keywords=(), regex=()),
        transfer=SimpleNamespace(
            include_videos=True,
            include_photos=True,
            include_text=True,
            include_documents=True,
            prefer_copy=True,
            forwarding_only=forwarding_only,
            hide_sender=True,
            drop_caption=False,
        ),
    )
    return Uploader(config, writer, writer, _Limiter(), storage=SimpleNamespace())


def _job() -> object:
    return SimpleNamespace(
        id=1,
        source_chat_id="-1001",
        source_message_ids=[1],
        dest_chat_id="-1002",
        dest_topic_id=None,
    )


if __name__ == "__main__":
    unittest.main()
