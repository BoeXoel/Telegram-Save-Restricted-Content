from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import queue as thread_queue
import ssl
from pathlib import Path
from typing import Any, AsyncIterable
from urllib.parse import quote, urlsplit, urlunsplit

from app.config import OversizedConfig
from app.db import Database
from app.errors import RemoteConfigurationError, RemotePermissionError, RemoteTransferError
from app.queue import MessageJob


class RemoteOffloader:
    """Move oversized source media to a preconfigured remote without cloud SDKs."""

    def __init__(self, config: OversizedConfig, database: Database, logger: Any | None = None) -> None:
        self.config = config
        self.database = database
        self.logger = logger

    @property
    def enabled(self) -> bool:
        return self.config.remote_enabled

    def existing_uri(self, job: MessageJob) -> str | None:
        return self.database.remote_uri_for_source(job.source_chat_id, job.file_unique_key)

    def directory_for(self, job: MessageJob) -> str:
        digest = hashlib.sha256(
            f"{job.source_chat_id}\0{job.file_unique_key}".encode("utf-8")
        ).hexdigest()[:16]
        relative = f"source-{_safe_component(job.source_chat_id)}/object-{digest}"
        return self._join_destination(relative)

    def file_uri(self, directory: str, file_name: str) -> str:
        if self.config.remote.method == "rclone":
            return f"{directory.rstrip('/')}/{file_name}"

        parsed = urlsplit(directory)
        path = parsed.path.rstrip("/") + "/" + quote(file_name, safe="._-")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def record_completed(self, job: MessageJob, remote_uri: str) -> None:
        self.database.record_remote_object(job.source_chat_id, job.file_unique_key, remote_uri)

    async def upload_file(self, local_path: Path, remote_uri: str) -> None:
        self._require_enabled()
        if self.config.remote.method == "rclone":
            await self._run_rclone("copyto", str(local_path), remote_uri)
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _webdav_put_file,
            remote_uri,
            local_path,
            self.config.remote.webdav_username,
            self.config.remote.webdav_password,
            self.config.remote.timeout_seconds,
        )

    async def upload_stream(
        self,
        chunks: AsyncIterable[bytes],
        remote_uri: str,
        *,
        size: int | None,
    ) -> None:
        self._require_enabled()
        if self.config.remote.method == "rclone":
            await self._stream_to_rclone(chunks, remote_uri, size=size)
            return
        await self._stream_to_webdav(chunks, remote_uri)

    def _join_destination(self, relative: str) -> str:
        destination = self.config.remote.dest.rstrip("/")
        if self.config.remote.method == "rclone":
            return f"{destination}/{relative}"

        parsed = urlsplit(destination)
        path = parsed.path.rstrip("/") + "/" + quote(relative, safe="/_-")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RemoteConfigurationError("Remote fallback is not enabled")

    async def _run_rclone(self, operation: str, *arguments: str) -> None:
        command = ["rclone", operation, *arguments, *self.config.remote.extra_args]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RemoteConfigurationError("rclone was not found in PATH") from exc

        _stdout, stderr = await process.communicate()
        if process.returncode:
            raise RemoteTransferError(_rclone_failure_message(stderr))

    async def _stream_to_rclone(
        self,
        chunks: AsyncIterable[bytes],
        remote_uri: str,
        *,
        size: int | None,
    ) -> None:
        command = ["rclone", "rcat", remote_uri]
        if size is not None:
            command.extend(["--size", str(size)])
        command.extend(self.config.remote.extra_args)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RemoteConfigurationError("rclone was not found in PATH") from exc

        try:
            assert process.stdin is not None
            async for chunk in chunks:
                if not chunk:
                    continue
                process.stdin.write(chunk)
                await process.stdin.drain()
            process.stdin.close()
            _stdout, stderr = await process.communicate()
        except (BrokenPipeError, ConnectionResetError) as exc:
            _stdout, stderr = await process.communicate()
            raise RemoteTransferError(_rclone_failure_message(stderr)) from exc
        except BaseException:
            if process.stdin:
                process.stdin.close()
            await process.communicate()
            raise

        if process.returncode:
            raise RemoteTransferError(_rclone_failure_message(stderr))

    async def _stream_to_webdav(self, chunks: AsyncIterable[bytes], remote_uri: str) -> None:
        channel: thread_queue.Queue[bytes | None] = thread_queue.Queue(maxsize=4)
        loop = asyncio.get_running_loop()
        consumer = loop.run_in_executor(
            None,
            _webdav_put_chunks,
            remote_uri,
            channel,
            self.config.remote.webdav_username,
            self.config.remote.webdav_password,
            self.config.remote.timeout_seconds,
        )
        try:
            async for chunk in chunks:
                if chunk:
                    await _put_stream_chunk(channel, consumer, chunk)
            await _put_stream_chunk(channel, consumer, None)
            await consumer
        except BaseException:
            await _close_stream_channel(channel, consumer)
            raise


async def _put_stream_chunk(
    channel: thread_queue.Queue[bytes | None],
    consumer: asyncio.Future[Any],
    chunk: bytes | None,
) -> None:
    while True:
        if consumer.done():
            await consumer
        try:
            channel.put_nowait(chunk)
            return
        except thread_queue.Full:
            await asyncio.sleep(0.05)


async def _close_stream_channel(
    channel: thread_queue.Queue[bytes | None],
    consumer: asyncio.Future[Any],
) -> None:
    if consumer.done():
        return
    try:
        await _put_stream_chunk(channel, consumer, None)
    except BaseException:
        return


def _safe_component(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value).strip("_") or "unknown"


def _rclone_failure_message(stderr: bytes) -> str:
    detail = " ".join(stderr.decode("utf-8", errors="replace").split())[:500]
    return f"rclone remote upload failed{': ' + detail if detail else ''}"


def _webdav_put_file(
    remote_uri: str,
    local_path: Path,
    username: str,
    password: str,
    timeout_seconds: int,
) -> None:
    connection = _webdav_connection(remote_uri, timeout_seconds)
    try:
        _start_webdav_put(
            connection,
            remote_uri,
            username,
            password,
            content_length=local_path.stat().st_size,
        )
        with local_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                connection.send(chunk)
        _validate_webdav_response(connection)
    except OSError as exc:
        raise RemoteTransferError("WebDAV remote upload failed due to a network error") from exc
    finally:
        connection.close()


def _webdav_put_chunks(
    remote_uri: str,
    channel: thread_queue.Queue[bytes | None],
    username: str,
    password: str,
    timeout_seconds: int,
) -> None:
    connection = _webdav_connection(remote_uri, timeout_seconds)
    try:
        _start_webdav_put(connection, remote_uri, username, password, content_length=None)
        while True:
            chunk = channel.get()
            if chunk is None:
                break
            connection.send(f"{len(chunk):X}\r\n".encode("ascii"))
            connection.send(chunk)
            connection.send(b"\r\n")
        connection.send(b"0\r\n\r\n")
        _validate_webdav_response(connection)
    except OSError as exc:
        raise RemoteTransferError("WebDAV remote upload failed due to a network error") from exc
    finally:
        connection.close()


def _webdav_connection(remote_uri: str, timeout_seconds: int) -> http.client.HTTPSConnection:
    parsed = urlsplit(remote_uri)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RemoteConfigurationError("WebDAV remote destination must use HTTPS")
    return http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port,
        timeout=timeout_seconds,
        context=ssl.create_default_context(),
    )


def _start_webdav_put(
    connection: http.client.HTTPSConnection,
    remote_uri: str,
    username: str,
    password: str,
    *,
    content_length: int | None,
) -> None:
    parsed = urlsplit(remote_uri)
    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query
    credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    connection.putrequest("PUT", request_path)
    connection.putheader("Authorization", f"Basic {credentials}")
    connection.putheader("Content-Type", "application/octet-stream")
    if content_length is None:
        connection.putheader("Transfer-Encoding", "chunked")
    else:
        connection.putheader("Content-Length", str(content_length))
    connection.endheaders()


def _validate_webdav_response(connection: http.client.HTTPSConnection) -> None:
    response = connection.getresponse()
    status = response.status
    response.read(1024)
    if 200 <= status < 300:
        return
    if status in {401, 403}:
        raise RemotePermissionError(f"WebDAV rejected the upload with HTTP {status}")
    if 400 <= status < 500:
        raise RemoteConfigurationError(f"WebDAV rejected the upload with HTTP {status}")
    raise RemoteTransferError(f"WebDAV remote upload failed with HTTP {status}")
