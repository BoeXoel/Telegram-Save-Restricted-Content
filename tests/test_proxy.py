from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import AppConfig, load_config
from app.telegram_client import interactive_login, make_bot_client, make_user_client
from main import log_telegram_proxy


class TelegramProxyConfigTests(unittest.TestCase):
    def test_proxy_defaults_to_direct_connections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _load_config(Path(temp_dir))

            self.assertFalse(config.telegram.proxy.enabled)
            self.assertIsNone(config.telegram.proxy.as_pyrogram_proxy())

    def test_enabled_socks5_proxy_expands_environment_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "TEST_PROXY_HOST": "sg-relay.example.test",
                    "TEST_PROXY_USER": "relay-user",
                    "TEST_PROXY_PASSWORD": "relay-password",
                },
                clear=False,
            ):
                config = _load_config(
                    Path(temp_dir),
                    """
                    proxy:
                      enabled: true
                      scheme: SOCKS5
                      hostname: "${TEST_PROXY_HOST}"
                      port: 1080
                      username: "${TEST_PROXY_USER}"
                      password: "${TEST_PROXY_PASSWORD}"
                    """,
                )

            self.assertEqual(
                config.telegram.proxy.as_pyrogram_proxy(),
                {
                    "scheme": "socks5",
                    "hostname": "sg-relay.example.test",
                    "port": 1080,
                    "username": "relay-user",
                    "password": "relay-password",
                },
            )

    def test_enabled_http_proxy_allows_no_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _load_config(
                Path(temp_dir),
                """
                proxy:
                  enabled: true
                  scheme: http
                  hostname: relay.example.test
                  port: 8080
                """,
            )

            self.assertEqual(
                config.telegram.proxy.as_pyrogram_proxy(),
                {
                    "scheme": "http",
                    "hostname": "relay.example.test",
                    "port": 8080,
                },
            )

    def test_enabled_proxy_rejects_invalid_settings(self) -> None:
        cases = (
            (
                "proxy:\n  enabled: true\n  scheme: socks4\n  hostname: relay.example.test\n  port: 1080",
                "scheme must be either",
            ),
            (
                "proxy:\n  enabled: true\n  scheme: socks5\n  port: 1080",
                "hostname is required",
            ),
            (
                "proxy:\n  enabled: true\n  scheme: socks5\n  hostname: relay.example.test\n  port: 0",
                "port must be between",
            ),
            (
                "proxy:\n  enabled: true\n  scheme: socks5\n  hostname: relay.example.test\n  port: 65536",
                "port must be between",
            ),
        )
        for settings, message in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as temp_dir:
                with self.assertRaisesRegex(ValueError, message):
                    _load_config(
                        Path(temp_dir),
                        settings,
                    )


class TelegramProxyClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_bot_and_login_use_the_same_enabled_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _load_config(
                Path(temp_dir),
                """
                proxy:
                  enabled: true
                  scheme: socks5
                  hostname: relay.example.test
                  port: 1080
                  username: user
                  password: password
                bot:
                  enabled: true
                  token: test-token
                """,
            )
            expected_proxy = config.telegram.proxy.as_pyrogram_proxy()

            with patch("app.telegram_client.Client") as client_factory:
                make_user_client(config)
                make_bot_client(config)

            self.assertEqual(client_factory.call_count, 2)
            self.assertEqual(client_factory.call_args_list[0].kwargs["proxy"], expected_proxy)
            self.assertEqual(client_factory.call_args_list[1].kwargs["proxy"], expected_proxy)

            config.ensure_directories()
            login_client = _LoginClient()
            with (
                patch("app.telegram_client.Client", return_value=login_client) as client_factory,
                patch("builtins.input", side_effect=["+15551234567", "12345"]),
            ):
                await interactive_login(config, "login_user")

            self.assertEqual(client_factory.call_args.kwargs["proxy"], expected_proxy)
            self.assertTrue(login_client.connected)
            self.assertTrue(login_client.disconnected)

    async def test_direct_mode_does_not_pass_a_proxy_to_pyrogram(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _load_config(Path(temp_dir))

            with patch("app.telegram_client.Client") as client_factory:
                make_user_client(config)

            self.assertNotIn("proxy", client_factory.call_args.kwargs)

    async def test_proxy_log_does_not_include_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _load_config(
                Path(temp_dir),
                """
                proxy:
                  enabled: true
                  scheme: socks5
                  hostname: relay.example.test
                  port: 1080
                  username: relay-user
                  password: relay-password
                """,
            )
            logger = _RecordingLogger()

            log_telegram_proxy(config, logger)  # type: ignore[arg-type]

            self.assertEqual(len(logger.messages), 1)
            self.assertIn("relay.example.test", logger.messages[0])
            self.assertNotIn("relay-user", logger.messages[0])
            self.assertNotIn("relay-password", logger.messages[0])


class _LoginClient:
    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_code(self, _phone: str) -> object:
        return SimpleNamespace(phone_code_hash="code-hash")

    async def sign_in(self, _phone: str, _phone_code_hash: str, _code: str) -> object:
        return SimpleNamespace()

    async def get_me(self) -> object:
        return SimpleNamespace(first_name="Reader", last_name=None, username=None, id=123)


class _RecordingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str, *args: object) -> None:
        self.messages.append(message % args)


def _load_config(directory: Path, telegram_extra: str = "") -> AppConfig:
    path = directory / "config.yaml"
    path.write_text(
        "telegram:\n"
        "  api_id: 1\n"
        "  api_hash: 0123456789abcdef0123456789abcdef\n"
        + _indent(telegram_extra)
        + "\n",
        encoding="utf-8",
    )
    return load_config(path)


def _indent(value: str) -> str:
    normalized = textwrap.dedent(value).strip()
    return "\n".join(f"  {line}" if line.strip() else "" for line in normalized.splitlines()) + "\n"


if __name__ == "__main__":
    unittest.main()
