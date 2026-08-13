from __future__ import annotations

import unittest
from types import SimpleNamespace

from pyrogram import raw

from app.config import ContentFilterConfig
from app.telegram_client import WriterCapabilities
from app.upload import Uploader


class TopicDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_forward_uses_top_message_id_for_a_forum_topic(self) -> None:
        writer = _TopicWriter(raw_topic_id=77)
        uploader = _uploader(writer, drop_caption=False)

        result = await uploader.process(_job(topic_id=77), _NeverSetEvent(), _allowed_phase)

        self.assertEqual(result.status, "copied")
        self.assertEqual(result.dest_message_ids, [99])
        self.assertIsInstance(writer.raw_request, raw.functions.messages.ForwardMessages)
        assert writer.raw_request is not None
        self.assertEqual(writer.raw_request.top_msg_id, 77)
        self.assertEqual(writer.resolved_peer_ids, [-1001, -1002])
        self.assertEqual(uploader._destination_kwargs(_job(topic_id=77)), {"reply_to_message_id": 77})

    async def test_failed_raw_topic_forward_falls_back_to_a_verified_copy(self) -> None:
        writer = _TopicWriter(raw_topic_id=None, copy_topic_id=77)
        uploader = _uploader(writer, drop_caption=False)

        result = await uploader.process(_job(topic_id=77), _NeverSetEvent(), _allowed_phase)

        self.assertEqual(result.status, "copied")
        self.assertEqual(result.dest_message_ids, [100])
        self.assertEqual(len(writer.copy_calls), 1)
        self.assertEqual(writer.copy_calls[0]["reply_to_message_id"], 77)
        self.assertEqual(writer.deleted_message_ids, [99])

    async def test_raw_forward_can_drop_media_captions_without_thread_kwarg(self) -> None:
        writer = _TopicWriter(raw_topic_id=None)
        uploader = _uploader(writer, drop_caption=True)

        result = await uploader.process(_job(topic_id=None), _NeverSetEvent(), _allowed_phase)

        self.assertEqual(result.status, "copied")
        assert writer.raw_request is not None
        self.assertTrue(writer.raw_request.drop_media_captions)
        self.assertIsNone(writer.raw_request.top_msg_id)


class _TopicWriter:
    def __init__(self, *, raw_topic_id: int | None, copy_topic_id: int | None = None) -> None:
        self.raw_topic_id = raw_topic_id
        self.copy_topic_id = copy_topic_id
        self.messages = [_TextMessage()]
        self.raw_request: object | None = None
        self.resolved_peer_ids: list[int | str] = []
        self.copy_calls: list[dict[str, object]] = []
        self.deleted_message_ids: list[int] = []

    async def get_messages(self, _chat_id: str, _message_ids: list[int]) -> list[object]:
        return self.messages

    async def resolve_peer(self, peer_id: int | str) -> object:
        self.resolved_peer_ids.append(peer_id)
        return SimpleNamespace(peer_id=peer_id)

    def rnd_id(self) -> int:
        return 12345

    async def invoke(self, request: object) -> object:
        self.raw_request = request
        reply_to = SimpleNamespace(reply_to_top_id=self.raw_topic_id, reply_to_msg_id=self.raw_topic_id)
        message = SimpleNamespace(id=99, reply_to=reply_to)
        return SimpleNamespace(
            updates=[raw.types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)]
        )

    async def copy_message(self, **kwargs: object) -> object:
        self.copy_calls.append(kwargs)
        return SimpleNamespace(
            id=100,
            reply_to_top_message_id=self.copy_topic_id,
            reply_to_message_id=self.copy_topic_id,
        )

    async def delete_messages(self, _chat_id: str, message_ids: list[int]) -> None:
        self.deleted_message_ids.extend(message_ids)


class _TextMessage:
    id = 1
    text = "A message"
    caption = None
    entities = None
    caption_entities = None
    media_group_id = None
    video = None
    photo = None
    document = None
    animation = None
    audio = None
    voice = None
    video_note = None
    empty = False


class _Limiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]


class _NeverSetEvent:
    def is_set(self) -> bool:
        return False


async def _allowed_phase(_phase: str) -> None:
    return


def _uploader(writer: _TopicWriter, *, drop_caption: bool) -> Uploader:
    config = SimpleNamespace(
        filters=ContentFilterConfig(enabled=False, case_sensitive=False, keywords=(), regex=()),
        transfer=SimpleNamespace(
            include_videos=True,
            include_photos=True,
            include_text=True,
            include_documents=True,
            prefer_copy=True,
            forwarding_only=False,
            hide_sender=False,
            drop_caption=drop_caption,
            save_to_local=False,
            allow_download_unknown_size=False,
            max_upload_bytes=0,
            max_bot_upload_bytes=100,
        ),
    )
    return Uploader(
        config,
        writer,
        writer,
        _Limiter(),
        storage=SimpleNamespace(),
        writer_capabilities=WriterCapabilities(
            identity="user:1",
            account_type="user",
            is_premium=False,
            max_upload_bytes=100,
        ),
    )


def _job(*, topic_id: int | None) -> object:
    return SimpleNamespace(
        id=1,
        source_chat_id="-1001",
        source_message_id=1,
        source_message_ids=[1],
        dest_chat_id="-1002",
        dest_topic_id=topic_id,
        file_unique_key="message-key",
    )


if __name__ == "__main__":
    unittest.main()
