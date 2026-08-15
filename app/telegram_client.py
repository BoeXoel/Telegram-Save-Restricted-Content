from __future__ import annotations

import asyncio
import json
import random
import re
import signal
import time
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    PeerIdInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
)
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from app.config import AppConfig, ChatSpec
from app.errors import FloodWaitDeferred


BOT_UPLOAD_LIMIT_BYTES = 2_000 * 1024 * 1024
STANDARD_USER_UPLOAD_LIMIT_BYTES = 2_000 * 1024 * 1024
PREMIUM_USER_UPLOAD_LIMIT_BYTES = 4_000 * 1024 * 1024
_NUMERIC_CHAT_ID_RE = re.compile(r"^-?\d+$")
_PRIVATE_INVITE_LINK_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResolvedChat:
    chat_id: str
    topic_id: int | None
    title: str


@dataclass(frozen=True)
class WriterCapabilities:
    """Upload limits for the account that will actually send a local file."""

    identity: str
    account_type: str
    is_premium: bool
    max_upload_bytes: int
    account_id: int | str = "unknown"


def telegram_chat_id(value: int | str) -> int | str:
    """Convert configured numeric chat IDs back to ints for Pyrogram calls.

    Queue rows intentionally keep chat IDs as text, but Pyrogram treats an
    unknown numeric *string* as a phone number.  Passing an int preserves the
    normal channel/group peer resolution path.
    """

    text = str(value).strip()
    if _NUMERIC_CHAT_ID_RE.fullmatch(text):
        return int(text)
    return text


def is_private_invite_link(value: int | str) -> bool:
    """Return whether a value is a private Telegram invite link."""

    return bool(_PRIVATE_INVITE_LINK_RE.match(str(value).strip()))


def require_bot_destination_id(value: int | str) -> None:
    """Reject private invite links before a Bot invokes CheckChatInvite."""

    if is_private_invite_link(value):
        raise ValueError(
            "Bot writers cannot use private invite links as destinations. "
            "Use the target's -100… ID or public @username, add the Bot to the target, "
            "then run `python main.py warmup-bot`."
        )


def get_writer_capabilities(config: AppConfig, account: Any) -> WriterCapabilities:
    is_bot = bool(getattr(account, "is_bot", False))
    is_premium = bool(getattr(account, "is_premium", False)) and not is_bot
    account_id = getattr(account, "id", "unknown")

    if config.transfer.max_upload_bytes:
        limit = config.transfer.max_upload_bytes
    elif is_bot:
        limit = config.transfer.max_bot_upload_bytes or BOT_UPLOAD_LIMIT_BYTES
    elif is_premium:
        limit = PREMIUM_USER_UPLOAD_LIMIT_BYTES
    else:
        limit = STANDARD_USER_UPLOAD_LIMIT_BYTES

    account_type = "bot" if is_bot else "premium_user" if is_premium else "user"
    return WriterCapabilities(
        identity=f"{account_type}:{account_id}",
        account_type=account_type,
        is_premium=is_premium,
        max_upload_bytes=limit,
        account_id=account_id,
    )


class TelegramLimiter:
    def __init__(self, config: AppConfig, logger: Any | None = None) -> None:
        self.config = config
        self.logger = logger
        self._lock = asyncio.Lock()
        self._last_global = 0.0
        self._last_by_operation: dict[str, float] = {}

    async def wait(self, operation: str) -> None:
        async with self._lock:
            now = time.monotonic()
            global_wait = self.config.limits.global_min_delay_seconds - (now - self._last_global)
            op_wait = self.config.limits.delay_for(operation) - (
                now - self._last_by_operation.get(operation, 0.0)
            )
            delay = max(0.0, global_wait, op_wait)
            if delay > 0:
                await asyncio.sleep(delay)

            finished = time.monotonic()
            self._last_global = finished
            self._last_by_operation[operation] = finished

    async def call(
        self,
        operation: str,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        while True:
            await self.wait(operation)
            try:
                return await fn(*args, **kwargs)
            except FloodWait as exc:
                extra_min = self.config.limits.floodwait_extra_min_seconds
                extra_max = self.config.limits.floodwait_extra_max_seconds
                wait = int(exc.value) + random.randint(extra_min, extra_max)
                if wait > self.config.limits.floodwait_defer_after_seconds:
                    if self.logger:
                        self.logger.warning(
                            "FloodWait from Telegram: deferring the current job for %ss",
                            wait,
                        )
                    raise FloodWaitDeferred(wait) from exc
                if self.logger:
                    self.logger.warning("FloodWait from Telegram: sleeping %ss", wait)
                await asyncio.sleep(wait)


def install_stop_handlers(stop_event: asyncio.Event) -> None:
    def request_stop(*_: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError, AttributeError):
            continue


def make_user_client(config: AppConfig) -> Client:
    return Client(
        name=config.telegram.user_session,
        api_id=config.telegram.api_id,
        api_hash=config.telegram.api_hash,
        workdir=str(config.telegram.sessions_dir),
    )


def make_bot_client(config: AppConfig) -> Client | None:
    if not config.telegram.bot_enabled:
        return None
    if not config.telegram.bot_token:
        raise ValueError("telegram.bot.enabled is true, but telegram.bot.token is empty")
    return Client(
        name=config.telegram.bot_session_name,
        api_id=config.telegram.api_id,
        api_hash=config.telegram.api_hash,
        bot_token=config.telegram.bot_token,
        workdir=str(config.telegram.sessions_dir),
    )


def _accounts_path(config: AppConfig) -> Path:
    return config.telegram.sessions_dir / "accounts.json"


def load_accounts(config: AppConfig) -> dict[str, Any]:
    path = _accounts_path(config)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_accounts(config: AppConfig, accounts: dict[str, Any]) -> None:
    _accounts_path(config).write_text(json.dumps(accounts, indent=2), encoding="utf-8")


def update_account_cache(config: AppConfig, session_name: str, user: Any) -> None:
    accounts = load_accounts(config)
    accounts[session_name] = {
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "username": user.username or "None",
        "id": user.id,
    }
    save_accounts(config, accounts)


async def interactive_login(config: AppConfig, session_name: str | None = None) -> None:
    session = session_name or input("Session name: ").strip()
    if not session:
        raise ValueError("Session name cannot be empty")

    limiter = TelegramLimiter(config)
    client = Client(
        name=session,
        api_id=config.telegram.api_id,
        api_hash=config.telegram.api_hash,
        workdir=str(config.telegram.sessions_dir),
    )
    await client.connect()
    try:
        phone = input("Phone number with country code: ").strip()
        sent_code = await limiter.call("read", client.send_code, phone)
        code = input("Login code: ").strip()
        try:
            await limiter.call("read", client.sign_in, phone, sent_code.phone_code_hash, code)
        except SessionPasswordNeeded:
            password = getpass("Two-factor password: ")
            await limiter.call("read", client.check_password, password)

        me = await limiter.call("read", client.get_me)
        update_account_cache(config, session, me)
        print(f"Logged in as {me.first_name} ({me.id}); session saved as {session}.session")
    except PhoneCodeInvalid as exc:
        raise ValueError("Invalid login code") from exc
    except PhoneCodeExpired as exc:
        raise ValueError("Login code expired") from exc
    finally:
        await client.disconnect()


async def warmup_user_dialogs(client: Client, logger: Any | None = None) -> int:
    """Populate a user session's peer cache by consuming its dialogs once."""

    count = 0
    async for _dialog in client.get_dialogs():
        count += 1
    if logger:
        logger.info("Loaded %s dialogs into the user session peer cache", count)
    return count


async def warmup_bot_destinations(
    bot: Client,
    destinations: list[ChatSpec],
    limiter: TelegramLimiter,
    *,
    timeout_seconds: int,
    bot_username: str | None = None,
    logger: Any | None = None,
) -> list[ResolvedChat]:
    """Wait for Bot updates that make configured destination peers known.

    A Bot cannot resolve a private invite link, and an unseen private
    supergroup ID may not have an access hash in a fresh session.  Incoming
    updates provide that peer information, so this short-lived command waits
    for an operator to address the Bot in the target chat.
    """

    if timeout_seconds <= 0:
        raise ValueError("warmup timeout must be greater than zero")
    if not destinations:
        raise ValueError("No destinations configured for Bot peer warmup")

    for spec in destinations:
        require_bot_destination_id(spec.chat)

    pending = list(enumerate(destinations))
    resolved: dict[int, ResolvedChat] = {}
    update_received = asyncio.Event()

    async def on_message(_client: Client, _message: Message) -> None:
        update_received.set()

    handler, group = bot.add_handler(MessageHandler(on_message), group=-100)
    # add_handler schedules its dispatcher update; let it register before we
    # tell the operator to send the warmup command.
    await asyncio.sleep(0)

    async def resolve_pending() -> list[tuple[int, ChatSpec]]:
        remaining: list[tuple[int, ChatSpec]] = []
        for index, spec in pending:
            try:
                resolved[index] = await resolve_chat(bot, limiter, spec)
                if logger:
                    logger.info("Bot peer ready: %s", spec.chat)
            except PeerIdInvalid:
                remaining.append((index, spec))
            except Exception as exc:
                raise RuntimeError(
                    f"Bot could not access destination {spec.chat}: {exc}. "
                    "Check that the Bot is a member and has permission to post there."
                ) from exc
        return remaining

    try:
        pending = await resolve_pending()
        if pending and logger:
            targets = ", ".join(spec.chat for _, spec in pending)
            command = (
                f"/warmup@{bot_username.lstrip('@')}"
                if bot_username
                else "a command addressed to the Bot"
            )
            logger.warning(
                "Bot has not met destination peer(s): %s. In each target chat, send %s "
                "within %ss to warm this session.",
                targets,
                command,
                timeout_seconds,
            )

        deadline = time.monotonic() + timeout_seconds
        while pending:
            if update_received.is_set():
                update_received.clear()
            else:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    break
                try:
                    await asyncio.wait_for(update_received.wait(), timeout=remaining_seconds)
                except TimeoutError:
                    break
                update_received.clear()
            pending = await resolve_pending()

        if pending:
            targets = ", ".join(spec.chat for _, spec in pending)
            raise TimeoutError(
                f"Bot peer warmup timed out for {targets}. Send a command mentioning the Bot in each target, "
                "then run `python main.py warmup-bot` again."
            )

        return [resolved[index] for index in range(len(destinations))]
    finally:
        bot.remove_handler(handler, group)
        await asyncio.sleep(0)


async def resolve_chat(client: Client, limiter: TelegramLimiter, spec: ChatSpec) -> ResolvedChat:
    chat = await limiter.call("resolve", client.get_chat, telegram_chat_id(spec.chat))
    title = chat.title or chat.username or str(chat.id)
    return ResolvedChat(chat_id=str(chat.id), topic_id=spec.topic_id, title=title)


def message_media_type(message: Message) -> str:
    if message.video:
        return "video"
    if message.photo:
        return "photo"
    if message.document:
        return "document"
    if message.animation:
        return "animation"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.text or message.caption:
        return "text"
    return "unsupported"


def message_file_size(message: Message) -> int | None:
    media = _message_media_object(message)
    if media is None:
        return None
    size = getattr(media, "file_size", None)
    if size is None:
        return None
    try:
        parsed = int(size)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def message_unique_key(message: Message) -> str:
    media = _message_media_object(message)
    if media:
        return str(getattr(media, "file_unique_id", None) or getattr(media, "file_id", None) or "")
    return ""


def message_caption(message: Message) -> str | None:
    return message.caption or message.text or None


def message_is_empty(message: Message | None) -> bool:
    return message is None or bool(getattr(message, "empty", False))


def _message_media_object(message: Message) -> Any | None:
    for attr in ("video", "photo", "document", "animation", "audio", "voice", "video_note"):
        media = getattr(message, attr, None)
        if media:
            return media
    return None
