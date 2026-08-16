from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path

from app.config import AppConfig, load_config
from app.db import Database
from app.logging import setup_logging
from app.offload import RemoteOffloader
from app.queue import MessageQueue
from app.report import build_oversized_report
from app.scanner import Scanner
from app.storage import DownloadStorage, format_bytes
from app.telegram_client import (
    TelegramLimiter,
    get_writer_capabilities,
    install_stop_handlers,
    interactive_login,
    make_bot_client,
    make_user_client,
    update_account_cache,
    warmup_bot_destinations,
    warmup_user_dialogs,
)
from app.upload import Uploader
from app.worker import Verifier, Worker


def log_telegram_proxy(config: AppConfig, logger: logging.Logger) -> None:
    proxy = config.telegram.proxy
    if proxy.enabled:
        logger.info(
            "Telegram proxy enabled: scheme=%s hostname=%s port=%s",
            proxy.scheme,
            proxy.hostname,
            proxy.port,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queue-based Telegram migration tool")
    parser.add_argument(
        "command",
        choices=(
            "login",
            "warmup-bot",
            "scan",
            "process",
            "verify",
            "run",
            "stats",
            "recover",
            "report-oversized",
        ),
        help="Phase to run",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config")
    parser.add_argument("--session", help="Session name for login command")
    parser.add_argument(
        "--oversized-only",
        action="store_true",
        help="Process only known oversized jobs (valid with the process command)",
    )
    parser.add_argument("--csv", help="Write report-oversized rows to this CSV file")
    parser.add_argument(
        "--warmup-timeout",
        type=int,
        default=120,
        help="Seconds warmup-bot waits for a Bot update in each unresolved destination",
    )
    return parser.parse_args()


async def run_with_clients(config: AppConfig, command: str, *, oversized_only: bool = False) -> None:
    logger = setup_logging(config.logging)
    limiter = TelegramLimiter(config, logger)
    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)

    db = Database(config.queue.db_path)
    db.initialize()
    queue = MessageQueue(db, config)
    offloader = RemoteOffloader(config.transfer.oversized, db, logger=logger)

    try:
        if command == "stats":
            print_counts(queue.counts_by_status())
            return
        if command == "recover":
            recovered = queue.recover_in_progress()
            print(f"Recovered {recovered} in-progress jobs to pending")
            return
        if command == "report-oversized":
            report = build_oversized_report(db)
            report.print()
            return

        storage = DownloadStorage(config.downloads)
        removed_active = storage.cleanup_active_jobs()
        removed_failed = storage.prune_failed_jobs()
        disk = storage.summary()
        logger.info(
            "Disk summary: free=%s min_free=%s failed=%s",
            format_bytes(disk.free_bytes),
            format_bytes(disk.min_free_bytes),
            format_bytes(disk.failed_bytes),
        )
        if removed_active or removed_failed:
            logger.info(
                "Removed stale managed download directories: active=%s failed=%s",
                removed_active,
                removed_failed,
            )

        log_telegram_proxy(config, logger)
        async with AsyncExitStack() as stack:
            reader = make_user_client(config)
            await stack.enter_async_context(reader)
            me = await limiter.call("read", reader.get_me)
            update_account_cache(config, config.telegram.user_session, me)
            logger.info("Reader session: %s (%s)", me.first_name, me.id)
            reader_capabilities = get_writer_capabilities(config, me)

            if config.telegram.load_dialogs_on_start:
                await warmup_user_dialogs(reader, logger)

            bot = make_bot_client(config)
            writer = reader
            writer_me = me
            if bot and config.telegram.use_bot_for_uploads:
                writer = bot
                await stack.enter_async_context(writer)
                bot_me = await limiter.call("read", writer.get_me)
                writer_me = bot_me
                logger.info("Writer bot: %s (%s)", bot_me.first_name, bot_me.id)

            writer_capabilities = get_writer_capabilities(config, writer_me)
            logger.info(
                "Writer %s has a %s MiB local upload limit",
                writer_capabilities.identity,
                writer_capabilities.max_upload_bytes // (1024 * 1024),
            )

            fallback_writer = None
            fallback_writer_capabilities = None
            if (
                config.transfer.allow_premium_user_fallback
                and writer is not reader
                and writer_capabilities.account_type == "bot"
                and reader_capabilities.account_type == "premium_user"
                and reader_capabilities.max_upload_bytes > writer_capabilities.max_upload_bytes
            ):
                fallback_writer = reader
                fallback_writer_capabilities = reader_capabilities
                logger.info(
                    "Premium reader fallback is enabled; it will be used only after a destination permission check"
                )
            elif config.transfer.allow_premium_user_fallback:
                logger.warning(
                    "Premium reader fallback is enabled but no eligible Premium reader/bot combination is active"
                )

            if command in {"scan", "run"}:
                scanner = Scanner(
                    config,
                    queue,
                    reader,
                    limiter,
                    writer=writer,
                    writer_capabilities=writer_capabilities,
                    logger=logger,
                )
                await scanner.scan(stop_event)

            if command in {"process", "run"} and not stop_event.is_set():
                uploader = Uploader(
                    config,
                    reader,
                    writer,
                    limiter,
                    logger=logger,
                    writer_capabilities=writer_capabilities,
                    fallback_writer=fallback_writer,
                    fallback_writer_capabilities=fallback_writer_capabilities,
                    storage=storage,
                    offloader=offloader,
                )
                worker = Worker(config, queue, uploader, logger=logger)
                await worker.run(stop_event, only_reason_code="oversized" if oversized_only else None)

            if command == "verify" and not stop_event.is_set():
                verifier = Verifier(config, queue, writer, limiter, logger=logger)
                await verifier.run(stop_event)
    finally:
        db.close()


def print_counts(counts: dict[str, int]) -> None:
    if not counts:
        print("Queue is empty")
        return
    for status in ("pending", "downloading", "uploading", "copied", "failed", "skipped"):
        print(f"{status}: {counts.get(status, 0)}")


async def async_main() -> None:
    args = parse_args()
    if args.oversized_only and args.command != "process":
        raise ValueError("--oversized-only can only be used with the process command")
    if args.csv and args.command != "report-oversized":
        raise ValueError("--csv can only be used with the report-oversized command")
    if args.warmup_timeout <= 0:
        raise ValueError("--warmup-timeout must be greater than zero")
    config = load_config(args.config)
    config.ensure_directories()

    if args.command == "login":
        log_telegram_proxy(config, setup_logging(config.logging))
        await interactive_login(config, args.session)
        return

    if args.command == "warmup-bot":
        await run_bot_warmup(config, args.warmup_timeout)
        return

    if args.command == "report-oversized" and args.csv:
        await write_oversized_csv(config, args.csv)
        return

    await run_with_clients(config, args.command, oversized_only=args.oversized_only)


async def run_bot_warmup(config: AppConfig, timeout_seconds: int) -> None:
    if not config.telegram.bot_enabled or not config.telegram.use_bot_for_uploads:
        raise ValueError(
            "warmup-bot requires telegram.bot.enabled: true and telegram.bot.use_for_uploads: true"
        )

    logger = setup_logging(config.logging)
    log_telegram_proxy(config, logger)
    limiter = TelegramLimiter(config, logger)
    bot = make_bot_client(config)
    if bot is None:
        raise ValueError("warmup-bot requires a configured Bot token")

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(bot)
        bot_me = await limiter.call("read", bot.get_me)
        logger.info("Warming Bot session: %s (%s)", bot_me.first_name, bot_me.id)
        resolved = await warmup_bot_destinations(
            bot,
            config.destinations,
            limiter,
            timeout_seconds=timeout_seconds,
            bot_username=getattr(bot_me, "username", None),
            logger=logger,
        )
        logger.info("Bot peer warmup complete: %s destination(s) ready", len(resolved))


async def write_oversized_csv(config: AppConfig, csv_path: str) -> None:
    db = Database(config.queue.db_path)
    db.initialize()
    try:
        report = build_oversized_report(db)
        report.print()
        destination = (config.base_dir / csv_path).resolve() if not Path(csv_path).is_absolute() else Path(csv_path)
        report.write_csv(destination)
        print(f"Wrote {len(report.rows)} oversized report rows to {destination}")
    finally:
        db.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
