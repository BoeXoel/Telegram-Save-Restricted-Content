from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pyrogram.errors import MessageIdInvalid, PeerFlood

from app.errors import FloodWaitDeferred
from app.telegram_client import TelegramLimiter
from app.worker import Worker


class FloodWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_floodwait_is_deferred_instead_of_sleeping_inline(self) -> None:
        limiter = TelegramLimiter(_limiter_config())

        async def flood() -> None:
            from pyrogram.errors import FloodWait

            raise FloodWait(11)

        with patch("app.telegram_client.random.randint", return_value=0):
            with self.assertRaises(FloodWaitDeferred) as context:
                await limiter.call("upload", flood)

        self.assertEqual(context.exception.reason_code, "flood_wait")
        self.assertEqual(context.exception.retry_after_seconds, 11)


class WorkerFailureClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_restriction_defers_job_and_stops_the_current_run(self) -> None:
        queue = _RecordingQueue()
        worker = Worker(_worker_config(), queue, _FailingUploader(PeerFlood("PEER_FLOOD")))
        stop_event = _StopEvent()

        await worker._process_one(_job(), stop_event)

        self.assertTrue(stop_event.is_set())
        self.assertEqual(queue.deferred["reason_code"], "account_restricted")
        self.assertEqual(queue.deferred["retry_after_seconds"], 3600)
        self.assertEqual(queue.skipped, [])

    async def test_missing_source_message_is_skipped_with_the_right_reason(self) -> None:
        queue = _RecordingQueue()
        worker = Worker(_worker_config(), queue, _FailingUploader(MessageIdInvalid("MESSAGE_ID_INVALID")))

        await worker._process_one(_job(), _StopEvent())

        self.assertEqual(queue.skipped[0]["reason_code"], "source_missing")


class _RecordingQueue:
    def __init__(self) -> None:
        self.deferred: dict[str, object] = {}
        self.skipped: list[dict[str, object]] = []

    def start_attempt(self, _job: object) -> int:
        return 1

    def defer(self, _job: object, _error: str, **kwargs: object) -> None:
        self.deferred = kwargs

    def mark_skipped(self, _job_id: int, _error: str, **kwargs: object) -> None:
        self.skipped.append(kwargs)


class _FailingUploader:
    writer_capabilities = None

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def process(self, _job: object, _stop_event: object, _on_phase: object) -> object:
        raise self.error


class _StopEvent:
    def __init__(self) -> None:
        self.stopped = False

    def set(self) -> None:
        self.stopped = True

    def is_set(self) -> bool:
        return self.stopped


def _limiter_config() -> object:
    return SimpleNamespace(
        limits=SimpleNamespace(
            global_min_delay_seconds=0,
            floodwait_extra_min_seconds=0,
            floodwait_extra_max_seconds=0,
            floodwait_defer_after_seconds=10,
            delay_for=lambda _operation: 0,
        )
    )


def _worker_config() -> object:
    return SimpleNamespace(
        limits=SimpleNamespace(account_restricted_retry_seconds=3600),
        queue=SimpleNamespace(max_attempts=4, retry_backoff_seconds=[1]),
    )


def _job() -> object:
    return SimpleNamespace(id=12)


if __name__ == "__main__":
    unittest.main()
