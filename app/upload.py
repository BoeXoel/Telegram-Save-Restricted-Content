from __future__ import annotations

import asyncio
import errno
import random
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from pyrogram import Client, raw
from pyrogram.errors import (
    BadRequest,
    ChannelInvalid,
    ChannelPrivate,
    ChatForwardsRestricted,
    ChatWriteForbidden,
    MediaEmpty,
)
from pyrogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message

from app.config import AppConfig
from app.errors import (
    DiskFullError,
    DiskLowError,
    JobSizeLimitError,
    PermanentJobError,
    RetryableJobError,
    TopicDeliveryError,
)
from app.filters import ContentFilter, FilterMatch
from app.offload import RemoteOffloader
from app.queue import MessageJob
from app.storage import DownloadStorage
from app.telegram_client import (
    TelegramLimiter,
    WriterCapabilities,
    message_caption,
    message_file_size,
    message_is_empty,
    message_media_type,
)


PhaseCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class UploadResult:
    status: str
    dest_message_ids: list[int] = field(default_factory=list)
    reason: str = ""
    reason_code: str | None = None
    transfer_route: str | None = None
    remote_uri: str | None = None
    writer_identity: str | None = None
    upload_limit_bytes: int | None = None


class Uploader:
    def __init__(
        self,
        config: AppConfig,
        reader: Client,
        writer: Client,
        limiter: TelegramLimiter,
        logger: Any | None = None,
        writer_capabilities: WriterCapabilities | None = None,
        fallback_writer: Client | None = None,
        fallback_writer_capabilities: WriterCapabilities | None = None,
        storage: DownloadStorage | None = None,
        offloader: RemoteOffloader | None = None,
    ) -> None:
        self.config = config
        self.reader = reader
        self.writer = writer
        self.limiter = limiter
        self.logger = logger
        self.writer_capabilities = writer_capabilities
        self.fallback_writer = fallback_writer
        self.fallback_writer_capabilities = fallback_writer_capabilities
        self.storage = storage or DownloadStorage(config.downloads)
        self.offloader = offloader
        self.content_filter = ContentFilter(config.filters)

    async def process(
        self,
        job: MessageJob,
        stop_event: asyncio.Event,
        on_phase: PhaseCallback,
    ) -> UploadResult:
        messages = await self._load_source_messages(job)
        filter_match = self._filter_match(messages)
        if filter_match:
            return UploadResult(
                status="skipped",
                reason=filter_match.reason,
                reason_code=filter_match.reason_code,
            )
        messages = [msg for msg in messages if self._message_should_process(msg)]

        if not messages:
            return UploadResult(
                status="skipped",
                reason="Source messages missing or filtered out",
                reason_code="source_missing",
            )

        text_only = all(message_media_type(message) == "text" for message in messages)

        if self._should_use_native_copy(job):
            try:
                await on_phase("uploading")
                return await self._copy_or_forward(job, messages)
            except ChatForwardsRestricted as exc:
                if self.config.transfer.forwarding_only:
                    return UploadResult(status="skipped", reason=f"Forward/copy restricted: {exc}")
                if self.logger:
                    self.logger.warning("Native copy failed for job %s; falling back to download/upload", job.id)
            except (ChannelPrivate, ChannelInvalid, ChatWriteForbidden, MediaEmpty) as exc:
                raise PermanentJobError(str(exc)) from exc
            except BadRequest as exc:
                if self.config.transfer.forwarding_only:
                    raise PermanentJobError(str(exc)) from exc
                if self.logger:
                    self.logger.warning("Native copy failed for job %s; falling back: %s", job.id, exc)

        if self.config.transfer.forwarding_only:
            return UploadResult(status="skipped", reason="forwarding_only is enabled and native copy was unavailable")

        if text_only:
            await on_phase("uploading")
            return await self._send_text(job, messages[0])

        return await self._download_and_upload(job, messages, stop_event, on_phase)

    async def _send_text(self, job: MessageJob, message: Message) -> UploadResult:
        text = message.text or message.caption or ""
        if not text:
            return UploadResult(status="skipped", reason="Text message was empty")
        result = await self.limiter.call(
            "upload",
            self.writer.send_message,
            chat_id=job.dest_chat_id,
            text=text,
            entities=message.entities or message.caption_entities,
            **self._destination_kwargs(job),
        )
        await self._ensure_topic_delivery(job, result)
        return UploadResult(
            status="copied",
            dest_message_ids=self._result_message_ids(result),
            transfer_route="telegram",
        )

    async def _load_source_messages(self, job: MessageJob) -> list[Message]:
        result = await self.limiter.call(
            "read",
            self.reader.get_messages,
            job.source_chat_id,
            job.source_message_ids,
        )
        if not isinstance(result, list):
            result = [result]
        return [msg for msg in result if not message_is_empty(msg)]

    def _should_use_native_copy(self, job: MessageJob) -> bool:
        if not self.config.transfer.prefer_copy:
            return False
        if self.writer is not self.reader:
            return False
        return True

    async def _copy_or_forward(self, job: MessageJob, messages: list[Message]) -> UploadResult:
        if self.config.transfer.hide_sender:
            result = await self._copy_messages(job, messages)
        elif job.dest_topic_id or self.config.transfer.drop_caption:
            try:
                return await self._raw_forward(job, messages)
            except Exception as forward_error:
                if not job.dest_topic_id:
                    raise
                if self.logger:
                    self.logger.warning(
                        "Raw topic forward failed for job %s; trying copy fallback: %s",
                        job.id,
                        forward_error,
                    )
                try:
                    result = await self._copy_messages(job, messages)
                except Exception as copy_error:
                    raise TopicDeliveryError(
                        "Unable to forward or copy the message into the configured forum topic"
                    ) from copy_error
        else:
            result = await self.limiter.call(
                "copy",
                self.writer.forward_messages,
                chat_id=job.dest_chat_id,
                from_chat_id=job.source_chat_id,
                message_ids=[msg.id for msg in messages],
            )

        await self._ensure_topic_delivery(job, result)

        return UploadResult(
            status="copied",
            dest_message_ids=self._result_message_ids(result),
            transfer_route="telegram",
        )

    async def _copy_messages(self, job: MessageJob, messages: list[Message]) -> Any:
        kwargs = self._destination_kwargs(job)
        first = messages[0]
        if len(messages) > 1 and first.media_group_id:
            result = await self.limiter.call(
                "copy",
                self.writer.copy_media_group,
                chat_id=job.dest_chat_id,
                from_chat_id=job.source_chat_id,
                message_id=first.id,
                captions="" if self.config.transfer.drop_caption else None,
                **kwargs,
            )
        else:
            result = await self.limiter.call(
                "copy",
                self.writer.copy_message,
                chat_id=job.dest_chat_id,
                from_chat_id=job.source_chat_id,
                message_id=first.id,
                caption="" if self.config.transfer.drop_caption else None,
                **kwargs,
            )
        await self._ensure_topic_delivery(job, result)
        return result

    async def _raw_forward(self, job: MessageJob, messages: list[Message]) -> UploadResult:
        from_peer = await self.limiter.call(
            "copy",
            self.writer.resolve_peer,
            self._peer_id(job.source_chat_id),
        )
        to_peer = await self.limiter.call(
            "copy",
            self.writer.resolve_peer,
            self._peer_id(job.dest_chat_id),
        )
        request = raw.functions.messages.ForwardMessages(
            from_peer=from_peer,
            id=[int(message.id) for message in messages],
            random_id=self._random_ids(len(messages)),
            to_peer=to_peer,
            drop_media_captions=True if self.config.transfer.drop_caption else None,
            top_msg_id=job.dest_topic_id,
        )
        updates = await self.limiter.call("copy", self.writer.invoke, request)
        forwarded = [
            update.message
            for update in getattr(updates, "updates", [])
            if isinstance(
                update,
                (
                    raw.types.UpdateNewMessage,
                    raw.types.UpdateNewChannelMessage,
                    raw.types.UpdateNewScheduledMessage,
                ),
            )
        ]
        dest_message_ids = [int(message.id) for message in forwarded if getattr(message, "id", None)]
        if not dest_message_ids:
            raise TopicDeliveryError("Telegram did not return destination messages for the raw forward")
        if job.dest_topic_id and not all(
            self._raw_message_is_in_topic(message, job.dest_topic_id) for message in forwarded
        ):
            await self._cleanup_misplaced_messages(job, dest_message_ids)
            raise TopicDeliveryError("Raw forward did not arrive in the configured forum topic")
        return UploadResult(
            status="copied",
            dest_message_ids=dest_message_ids,
            transfer_route="telegram",
        )

    async def _download_and_upload(
        self,
        job: MessageJob,
        messages: list[Message],
        stop_event: asyncio.Event,
        on_phase: PhaseCallback,
    ) -> UploadResult:
        local_writer, local_capabilities, validation_result = await self._select_local_writer(
            job,
            messages,
        )
        if validation_result:
            if (
                validation_result.reason_code == "oversized"
                and self.offloader
                and self.offloader.enabled
            ):
                return await self._offload_oversized(job, messages, stop_event, on_phase)
            return validation_result

        reservation_bytes = self._job_reservation_bytes(messages)
        self.storage.ensure_job_reservation(reservation_bytes)

        job_dir = self.config.downloads.active_dir / f"job-{job.id}"
        job_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[tuple[Message, Path]] = []
        success = False
        force_delete = False
        completed_bytes = 0

        try:
            await on_phase("downloading")
            for message in messages:
                path = await self._download_one(
                    message,
                    job_dir,
                    reservation_bytes=reservation_bytes,
                    completed_bytes=completed_bytes,
                )
                downloaded.append((message, path))
                completed_bytes += path.stat().st_size

            if stop_event.is_set():
                raise RetryableJobError("Stop requested after download; leaving job for retry")

            await on_phase("uploading")
            result = await self._upload_downloaded(job, downloaded, writer=local_writer)
            success = True
            return self._with_writer_capabilities(result, local_capabilities)
        except DiskFullError:
            force_delete = True
            raise
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
                force_delete = True
                raise DiskFullError("The filesystem reported that it is out of space") from exc
            raise
        finally:
            self._cleanup_job_dir(job_dir, success, force_delete=force_delete)

    async def _offload_oversized(
        self,
        job: MessageJob,
        messages: list[Message],
        stop_event: asyncio.Event,
        on_phase: PhaseCallback,
    ) -> UploadResult:
        assert self.offloader is not None
        existing_uri = self.offloader.existing_uri(job)
        if existing_uri:
            return UploadResult(
                status="skipped",
                reason="Oversized source media was already offloaded to the configured remote",
                reason_code="oversized",
                transfer_route="remote",
                remote_uri=existing_uri,
            )

        remote_directory = self.offloader.directory_for(job)
        known_size = self._known_media_bytes(messages)
        try:
            if known_size is None:
                raise DiskLowError("An unknown-size oversized album will be streamed to the remote")
            self.storage.ensure_job_reservation(known_size)
        except (DiskLowError, JobSizeLimitError):
            await self._stream_oversized_to_remote(
                job,
                messages,
                remote_directory,
                stop_event,
                on_phase,
            )
        else:
            await self._spool_oversized_to_remote(
                job,
                messages,
                remote_directory,
                known_size,
                stop_event,
                on_phase,
            )

        self.offloader.record_completed(job, remote_directory)
        return UploadResult(
            status="skipped",
            reason="Oversized source media was offloaded to the configured remote",
            reason_code="oversized",
            transfer_route="remote",
            remote_uri=remote_directory,
        )

    async def _spool_oversized_to_remote(
        self,
        job: MessageJob,
        messages: list[Message],
        remote_directory: str,
        reservation_bytes: int,
        stop_event: asyncio.Event,
        on_phase: PhaseCallback,
    ) -> None:
        assert self.offloader is not None
        job_dir = self.config.downloads.active_dir / f"job-{job.id}-remote"
        job_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[tuple[Message, Path]] = []
        completed_bytes = 0
        success = False
        try:
            await on_phase("downloading")
            for message in messages:
                path = await self._download_one(
                    message,
                    job_dir,
                    reservation_bytes=reservation_bytes,
                    completed_bytes=completed_bytes,
                )
                downloaded.append((message, path))
                completed_bytes += path.stat().st_size

            if stop_event.is_set():
                raise RetryableJobError("Stop requested before remote upload", reason_code="remote_error")

            await on_phase("uploading")
            for message, path in downloaded:
                await self.offloader.upload_file(
                    path,
                    self.offloader.file_uri(remote_directory, self._file_name_for(message)),
                )
            success = True
        finally:
            self._cleanup_remote_spool(job_dir, success)

    async def _stream_oversized_to_remote(
        self,
        job: MessageJob,
        messages: list[Message],
        remote_directory: str,
        stop_event: asyncio.Event,
        on_phase: PhaseCallback,
    ) -> None:
        assert self.offloader is not None
        await on_phase("downloading")
        for message in messages:
            if stop_event.is_set():
                raise RetryableJobError("Stop requested before remote stream", reason_code="remote_error")
            await self.limiter.wait("download")
            await on_phase("uploading")
            await self.offloader.upload_stream(
                self.reader.stream_media(message),
                self.offloader.file_uri(remote_directory, self._file_name_for(message)),
                size=message_file_size(message),
            )

    async def _download_one(
        self,
        message: Message,
        job_dir: Path,
        *,
        reservation_bytes: int,
        completed_bytes: int,
    ) -> Path:
        path = job_dir / self._file_name_for(message)
        progress_error: DiskFullError | None = None

        async def check_progress(current: int, _total: int) -> None:
            nonlocal progress_error
            remaining = reservation_bytes - completed_bytes - max(0, int(current))
            try:
                self.storage.ensure_progress_reservation(remaining)
            except DiskFullError as exc:
                progress_error = exc
                stop_transmission = getattr(self.reader, "stop_transmission", None)
                if callable(stop_transmission):
                    stop_transmission()
                raise

        result = await self.limiter.call(
            "download",
            message.download,
            file_name=str(path),
            progress=check_progress,
        )
        if progress_error:
            raise progress_error

        downloaded_path = Path(result or path)
        if not downloaded_path.exists():
            if self.storage.free_bytes() < self.config.downloads.min_free_bytes:
                raise DiskFullError("Download stopped after the filesystem fell below its reserved free space")
            raise RetryableJobError("Telegram download did not produce a local file", reason_code="network_error")
        return downloaded_path

    async def _upload_downloaded(
        self,
        job: MessageJob,
        downloaded: list[tuple[Message, Path]],
        *,
        writer: Client,
    ) -> UploadResult:
        kwargs = self._destination_kwargs(job)

        if len(downloaded) > 1:
            media_group = []
            caption_used = False
            for message, path in downloaded:
                caption = self._caption_for(message) if not caption_used else None
                caption_entities = message.caption_entities if caption else None
                caption_used = caption_used or bool(caption)
                media_type = message_media_type(message)

                if media_type == "photo":
                    media_group.append(InputMediaPhoto(str(path), caption=caption, caption_entities=caption_entities))
                elif media_type == "video":
                    media_group.append(
                        InputMediaVideo(
                            str(path),
                            caption=caption,
                            caption_entities=caption_entities,
                            supports_streaming=True,
                        )
                    )
                else:
                    media_group.append(InputMediaDocument(str(path), caption=caption, caption_entities=caption_entities))

            result = await self.limiter.call(
                "upload",
                writer.send_media_group,
                chat_id=job.dest_chat_id,
                media=media_group,
                **kwargs,
            )
            await self._ensure_topic_delivery(job, result, writer=writer)
            return UploadResult(
                status="copied",
                dest_message_ids=self._result_message_ids(result),
                transfer_route="telegram",
            )

        message, path = downloaded[0]
        caption = self._caption_for(message)
        media_type = message_media_type(message)

        if media_type == "photo":
            result = await self.limiter.call(
                "upload",
                writer.send_photo,
                chat_id=job.dest_chat_id,
                photo=str(path),
                caption=caption,
                caption_entities=message.caption_entities if caption else None,
                **kwargs,
            )
        elif media_type == "video":
            result = await self.limiter.call(
                "upload",
                writer.send_video,
                chat_id=job.dest_chat_id,
                video=str(path),
                caption=caption,
                caption_entities=message.caption_entities if caption else None,
                supports_streaming=True,
                **kwargs,
            )
        elif media_type == "text":
            result = await self.limiter.call(
                "upload",
                writer.send_message,
                chat_id=job.dest_chat_id,
                text=message.text or message.caption or "",
                entities=message.entities or message.caption_entities,
                **kwargs,
            )
        else:
            result = await self.limiter.call(
                "upload",
                writer.send_document,
                chat_id=job.dest_chat_id,
                document=str(path),
                caption=caption,
                caption_entities=message.caption_entities if caption else None,
                **kwargs,
            )

        await self._ensure_topic_delivery(job, result, writer=writer)
        return UploadResult(
            status="copied",
            dest_message_ids=self._result_message_ids(result),
            transfer_route="telegram",
        )

    def _destination_kwargs(self, job: MessageJob) -> dict[str, Any]:
        if job.dest_topic_id:
            return {"reply_to_message_id": job.dest_topic_id}
        return {}

    async def _ensure_topic_delivery(
        self,
        job: MessageJob,
        result: Any,
        *,
        writer: Client | None = None,
    ) -> None:
        if not job.dest_topic_id:
            return
        messages = result if isinstance(result, list) else [result]
        message_ids = self._result_message_ids(result)
        if messages and all(self._message_is_in_topic(message, job.dest_topic_id) for message in messages):
            return
        await self._cleanup_misplaced_messages(job, message_ids, writer=writer)
        raise TopicDeliveryError("Destination message did not arrive in the configured forum topic")

    async def _cleanup_misplaced_messages(
        self,
        job: MessageJob,
        message_ids: list[int],
        *,
        writer: Client | None = None,
    ) -> None:
        if not message_ids:
            return
        target_writer = writer if writer is not None else self.writer
        delete_messages = getattr(target_writer, "delete_messages", None)
        if not callable(delete_messages):
            return
        try:
            await self.limiter.call("copy", delete_messages, job.dest_chat_id, message_ids)
        except Exception as exc:
            if self.logger:
                self.logger.warning(
                    "Could not remove misplaced topic messages for job %s: %s",
                    job.id,
                    exc,
                )

    @staticmethod
    def _message_is_in_topic(message: Any, topic_id: int) -> bool:
        reply_to = getattr(message, "reply_to", None)
        topic_values = {
            getattr(message, "reply_to_top_message_id", None),
            getattr(message, "reply_to_message_id", None),
            getattr(reply_to, "reply_to_top_id", None),
            getattr(reply_to, "reply_to_msg_id", None),
        }
        return topic_id in topic_values

    @classmethod
    def _raw_message_is_in_topic(cls, message: Any, topic_id: int) -> bool:
        return cls._message_is_in_topic(message, topic_id)

    def _random_ids(self, count: int) -> list[int]:
        random_id = getattr(self.writer, "rnd_id", None)
        if callable(random_id):
            return [int(random_id()) for _ in range(count)]
        return [random.getrandbits(63) for _ in range(count)]

    @staticmethod
    def _peer_id(value: str) -> int | str:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    def _caption_for(self, message: Message) -> str | None:
        if self.config.transfer.drop_caption:
            return None
        return message.caption or message.text or None

    def _message_should_process(self, message: Message) -> bool:
        media_type = message_media_type(message)
        if media_type == "video":
            return self.config.transfer.include_videos
        if media_type == "photo":
            return self.config.transfer.include_photos
        if media_type == "document":
            return self.config.transfer.include_documents
        if media_type == "text":
            return self.config.transfer.include_text
        return False

    def _filter_match(self, messages: list[Message]) -> FilterMatch | None:
        return self.content_filter.match_texts(message_caption(message) for message in messages)

    async def _select_local_writer(
        self,
        job: MessageJob,
        messages: list[Message],
    ) -> tuple[Client, WriterCapabilities | None, UploadResult | None]:
        """Choose the account that will send locally downloaded media.

        A Premium reader is never selected implicitly.  It may only replace a
        bot after the user enabled the setting, the bot limit rejected the
        source, and the reader was confirmed to be able to post to the target.
        """

        primary_identity, primary_limit = self._writer_upload_limit()
        primary_validation = self._validate_downloadable_messages(
            job,
            messages,
            identity=primary_identity,
            limit=primary_limit,
        )
        if primary_validation is None:
            return self.writer, self.writer_capabilities, None

        if (
            primary_validation.reason_code != "oversized"
            or not self._premium_fallback_is_available()
        ):
            return self.writer, self.writer_capabilities, primary_validation

        assert self.fallback_writer is not None
        assert self.fallback_writer_capabilities is not None
        fallback_capabilities = self.fallback_writer_capabilities
        fallback_validation = self._validate_downloadable_messages(
            job,
            messages,
            identity=fallback_capabilities.identity,
            limit=fallback_capabilities.max_upload_bytes,
        )
        if fallback_validation is not None:
            return self.writer, self.writer_capabilities, primary_validation

        if not await self._fallback_writer_can_post(job):
            if self.logger:
                self.logger.info(
                    "Premium writer fallback is unavailable for job %s because the reader cannot post to the destination",
                    job.id,
                )
            return self.writer, self.writer_capabilities, primary_validation

        if self.logger:
            self.logger.info(
                "Using the explicitly enabled Premium user writer fallback for job %s",
                job.id,
            )
        return self.fallback_writer, fallback_capabilities, None

    def _premium_fallback_is_available(self) -> bool:
        primary = self.writer_capabilities
        fallback = self.fallback_writer_capabilities
        return bool(
            getattr(self.config.transfer, "allow_premium_user_fallback", False)
            and self.fallback_writer is not None
            and primary is not None
            and fallback is not None
            and primary.account_type == "bot"
            and fallback.account_type == "premium_user"
            and fallback.max_upload_bytes > primary.max_upload_bytes
            and isinstance(fallback.account_id, int)
        )

    async def _fallback_writer_can_post(self, job: MessageJob) -> bool:
        """Preflight the explicitly chosen reader account without downloading."""

        assert self.fallback_writer is not None
        assert self.fallback_writer_capabilities is not None
        get_chat = getattr(self.fallback_writer, "get_chat", None)
        get_chat_member = getattr(self.fallback_writer, "get_chat_member", None)
        if not callable(get_chat) or not callable(get_chat_member):
            return False

        try:
            chat = await self.limiter.call("resolve", get_chat, job.dest_chat_id)
            member = await self.limiter.call(
                "resolve",
                get_chat_member,
                job.dest_chat_id,
                self.fallback_writer_capabilities.account_id,
            )
        except (BadRequest, ChannelInvalid, ChannelPrivate, ChatWriteForbidden):
            return False

        status = str(getattr(member, "status", "")).lower()
        if any(value in status for value in ("banned", "left", "restricted")):
            return False

        permissions = getattr(member, "permissions", None)
        if getattr(permissions, "can_send_messages", None) is False:
            return False
        privileges = getattr(member, "privileges", None)
        if getattr(privileges, "can_post_messages", None) is False:
            return False

        chat_type = str(getattr(chat, "type", "")).lower()
        if "channel" in chat_type:
            return any(value in status for value in ("owner", "creator", "administrator", "admin"))
        return True

    @staticmethod
    def _with_writer_capabilities(
        result: UploadResult,
        capabilities: WriterCapabilities | None,
    ) -> UploadResult:
        if capabilities is None:
            return result
        return replace(
            result,
            writer_identity=capabilities.identity,
            upload_limit_bytes=capabilities.max_upload_bytes,
        )

    def _validate_downloadable_messages(
        self,
        job: MessageJob,
        messages: list[Message],
        *,
        identity: str | None = None,
        limit: int | None = None,
    ) -> UploadResult | None:
        if identity is None or limit is None:
            identity, limit = self._writer_upload_limit()
        unknown_messages: list[Message] = []
        for message in messages:
            if message_media_type(message) == "text":
                continue

            size = message_file_size(message)
            source = f"source chat {job.source_chat_id}, message {message.id}"
            if size is None:
                unknown_messages.append(message)
                continue

            if size > limit:
                return UploadResult(
                    status="skipped",
                    reason=(
                        f"File for {source} is {size} bytes, above the {limit}-byte "
                        f"upload limit for {identity}"
                    ),
                    reason_code="oversized",
                    transfer_route="record",
                )

        if unknown_messages and not self.config.transfer.allow_download_unknown_size:
            message = unknown_messages[0]
            source = f"source chat {job.source_chat_id}, message {message.id}"
            return UploadResult(
                status="skipped",
                reason=(
                    f"File size is unknown for {source}; local download is disabled "
                    "by transfer.allow_download_unknown_size"
                ),
                reason_code="unknown_size",
                transfer_route="record",
            )
        return None

    def _writer_upload_limit(self) -> tuple[str, int]:
        if self.writer_capabilities:
            return self.writer_capabilities.identity, self.writer_capabilities.max_upload_bytes

        # This fallback is intentionally conservative for callers using the
        # Uploader directly instead of main.py, where capabilities are known.
        override = self.config.transfer.max_upload_bytes
        return "unknown_writer", override or self.config.transfer.max_bot_upload_bytes

    def _job_reservation_bytes(self, messages: list[Message]) -> int:
        sizes = [
            message_file_size(message)
            for message in messages
            if message_media_type(message) != "text"
        ]
        if any(size is None for size in sizes):
            if self.config.downloads.max_job_bytes == 0:
                raise JobSizeLimitError(
                    "A media item has no known size; set downloads.max_job_bytes before allowing "
                    "unknown-size downloads"
                )
            return self.config.downloads.max_job_bytes
        return sum(size for size in sizes if size is not None)

    def _known_media_bytes(self, messages: list[Message]) -> int | None:
        sizes = [
            message_file_size(message)
            for message in messages
            if message_media_type(message) != "text"
        ]
        if not sizes or any(size is None for size in sizes):
            return None
        return sum(size for size in sizes if size is not None)

    def _cleanup_job_dir(self, job_dir: Path, success: bool, *, force_delete: bool = False) -> None:
        if not job_dir.exists():
            return

        if force_delete:
            shutil.rmtree(job_dir, ignore_errors=True)
            return

        keep_completed = self.config.downloads.keep_completed or self.config.transfer.save_to_local
        if success and keep_completed:
            self._move_directory(job_dir, self.config.downloads.completed_dir / job_dir.name)
            return
        if (
            not success
            and self.config.downloads.keep_failed
            and self.config.downloads.max_failed_bytes > 0
        ):
            self._move_directory(job_dir, self.config.downloads.failed_dir / job_dir.name)
            self.storage.prune_failed_jobs()
            return
        shutil.rmtree(job_dir, ignore_errors=True)

    def _cleanup_remote_spool(self, job_dir: Path, success: bool) -> None:
        if not job_dir.exists():
            return
        if success and not self.config.transfer.oversized.remote.delete_local_after:
            self._move_directory(job_dir, self.config.downloads.completed_dir / job_dir.name)
            return
        # Remote failures never use failed/ because an oversized file could
        # otherwise fill a small server while waiting for retries.
        shutil.rmtree(job_dir, ignore_errors=True)

    def _move_directory(self, source: Path, dest: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))

    def _file_name_for(self, message: Message) -> str:
        media = None
        for attr in ("video", "photo", "document", "animation", "audio", "voice", "video_note"):
            media = getattr(message, attr, None)
            if media:
                break

        original = getattr(media, "file_name", None) if media else None
        if not original:
            extension = {
                "photo": ".jpg",
                "video": ".mp4",
                "animation": ".mp4",
                "audio": ".mp3",
                "voice": ".ogg",
                "video_note": ".mp4",
                "document": ".bin",
            }.get(message_media_type(message), ".bin")
            original = f"{message.id}{extension}"

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._")
        return f"{message.id}_{safe or 'media.bin'}"

    def _result_message_ids(self, result: Any) -> list[int]:
        if isinstance(result, list):
            return [int(item.id) for item in result if getattr(item, "id", None)]
        if getattr(result, "id", None):
            return [int(result.id)]
        return []
