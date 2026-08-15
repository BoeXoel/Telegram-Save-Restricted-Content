from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from pyrogram.errors import PeerIdInvalid

from app.config import ChatSpec, ContentFilterConfig
from app.scanner import Scanner
from app.telegram_client import (
    WriterCapabilities,
    telegram_chat_id,
    warmup_bot_destinations,
    warmup_user_dialogs,
)


class ChatIdNormalizationTests(unittest.TestCase):
    def test_numeric_group_ids_are_passed_to_pyrogram_as_integers(self) -> None:
        self.assertEqual(telegram_chat_id("-1001234567890"), -1001234567890)
        self.assertEqual(telegram_chat_id(" 42 "), 42)
        self.assertEqual(telegram_chat_id("@public_group"), "@public_group")
        self.assertEqual(telegram_chat_id("+15551234567"), "+15551234567")


class DestinationResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_is_checked_even_when_reader_knows_the_destination(self) -> None:
        reader = _ChatClient()
        writer = _ChatClient(error=PeerIdInvalid("PEER_ID_INVALID"))
        scanner = _scanner(reader, writer, _bot_capabilities())

        with self.assertRaisesRegex(ValueError, "warmup-bot"):
            await scanner._resolve_destinations()

        self.assertEqual(reader.calls, [])
        self.assertEqual(writer.calls, [-1002])

    async def test_bot_destination_rejects_private_invite_link_without_an_api_call(self) -> None:
        writer = _ChatClient()
        scanner = _scanner(
            _ChatClient(),
            writer,
            _bot_capabilities(),
            destination="https://t.me/+privateInvite",
        )

        with self.assertRaisesRegex(ValueError, "private invite links"):
            await scanner._resolve_destinations()

        self.assertEqual(writer.calls, [])

    async def test_user_writer_keeps_private_invite_link_support(self) -> None:
        writer = _ChatClient()
        scanner = _scanner(
            _ChatClient(),
            writer,
            _user_capabilities(),
            destination="https://t.me/+privateInvite",
        )

        destinations = await scanner._resolve_destinations()

        self.assertEqual(len(destinations), 1)
        self.assertEqual(writer.calls, ["https://t.me/+privateInvite"])


class WarmupTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_dialog_warmup_consumes_the_dialog_generator(self) -> None:
        client = _DialogClient()

        count = await warmup_user_dialogs(client)

        self.assertEqual(count, 2)
        self.assertTrue(client.started)

    async def test_bot_warmup_retries_after_an_incoming_update(self) -> None:
        bot = _WarmupBot()
        destinations = [ChatSpec(chat="-1002")]

        resolved = await warmup_bot_destinations(
            bot,
            destinations,
            _Limiter(),
            timeout_seconds=1,
            bot_username="uploader_bot",
        )

        self.assertEqual([item.chat_id for item in resolved], ["-1002"])
        self.assertGreaterEqual(len(bot.calls), 2)
        self.assertTrue(bot.removed_handler)


class _ChatClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[int | str] = []

    async def get_chat(self, chat_id: int | str) -> object:
        self.calls.append(chat_id)
        if self.error:
            raise self.error
        return SimpleNamespace(id=chat_id, title="Target", username=None)


class _DialogClient:
    def __init__(self) -> None:
        self.started = False

    async def get_dialogs(self):
        self.started = True
        yield SimpleNamespace()
        yield SimpleNamespace()


class _WarmupBot:
    def __init__(self) -> None:
        self.calls: list[int | str] = []
        self.ready = False
        self.removed_handler = False
        self._handler: object | None = None

    def add_handler(self, handler: object, group: int) -> tuple[object, int]:
        self._handler = handler
        asyncio.create_task(self._deliver_warmup_message())
        return handler, group

    def remove_handler(self, _handler: object, _group: int) -> None:
        self.removed_handler = True

    async def _deliver_warmup_message(self) -> None:
        await asyncio.sleep(0.01)
        self.ready = True
        assert self._handler is not None
        callback = getattr(self._handler, "callback")
        await callback(self, SimpleNamespace(chat=SimpleNamespace(id=-1002)))

    async def get_chat(self, chat_id: int | str) -> object:
        self.calls.append(chat_id)
        if not self.ready:
            raise PeerIdInvalid("PEER_ID_INVALID")
        return SimpleNamespace(id=chat_id, title="Target", username=None)


class _Limiter:
    async def call(self, _operation: str, function: object, *args: object, **kwargs: object) -> object:
        return await function(*args, **kwargs)  # type: ignore[misc]


class _Queue:
    pass


def _scanner(
    reader: object,
    writer: object,
    capabilities: WriterCapabilities,
    *,
    destination: str = "-1002",
) -> Scanner:
    config = SimpleNamespace(
        destinations=[ChatSpec(chat=destination)],
        filters=ContentFilterConfig(enabled=False, case_sensitive=False, keywords=(), regex=()),
    )
    return Scanner(config, _Queue(), reader, _Limiter(), writer=writer, writer_capabilities=capabilities)


def _bot_capabilities() -> WriterCapabilities:
    return WriterCapabilities(
        identity="bot:10",
        account_type="bot",
        is_premium=False,
        max_upload_bytes=100,
        account_id=10,
    )


def _user_capabilities() -> WriterCapabilities:
    return WriterCapabilities(
        identity="user:20",
        account_type="user",
        is_premium=False,
        max_upload_bytes=100,
        account_id=20,
    )


if __name__ == "__main__":
    unittest.main()
