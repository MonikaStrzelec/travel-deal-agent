"""Notification interface with local logging and Telegram implementations."""

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import TelegramConfig
from .config_types import AttractivenessConfig, RatingRule
from .notification_content import NotificationMessage
from .storage import Notification, Store

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


class Notifier(ABC):
    @abstractmethod
    def send(self, notification: Notification) -> None:
        """Deliver a notification; use its ID as an idempotency key when supported."""


class ConsoleNotifier(Notifier):
    """Local logging transport; never contacts any external messaging service."""

    def __init__(
        self,
        attractiveness_config: AttractivenessConfig | None = None,
        provider_ratings: Mapping[str, RatingRule] | None = None,
    ) -> None:
        self._attractiveness_config = attractiveness_config
        self._provider_ratings = provider_ratings

    def send(self, notification: Notification) -> None:
        message = NotificationMessage.from_notification(notification)
        logger.info("%s", message.render(self._attractiveness_config, self._provider_ratings))


# Preserve the public name used by existing integrations and tests.
LogNotifier = ConsoleNotifier


class TelegramDeliveryError(Exception):
    """A Telegram delivery failure; the message never includes the bot token."""


class TelegramTransport(Protocol):
    def send_message(self, chat_id: str, text: str, timeout: float) -> None:
        """Deliver text to chat_id or raise TelegramDeliveryError.

        Implementations must never include the bot token in a raised message.
        """
        ...


class UrllibTelegramTransport:
    """Minimal stdlib HTTP client for the Telegram Bot API sendMessage call."""

    def __init__(self, bot_token: str) -> None:
        # The token lives only in this closed-over URL; never logged or re-exposed.
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send_message(self, chat_id: str, text: str, timeout: float) -> None:
        # HTML parse mode: NotificationMessage.render() already HTML-escapes
        # every piece of free text and only ever emits a single, well-formed
        # <a href="..."> tag for the offer link -- never raw, unescaped markup.
        body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
        request = Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                status = response.status
                payload = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise TelegramDeliveryError(
                f"Telegram API returned HTTP {exc.code}: {payload}"
            ) from None
        except URLError as exc:
            raise TelegramDeliveryError(f"Telegram request failed: {exc.reason}") from None
        if status != 200:
            raise TelegramDeliveryError(f"Telegram API returned HTTP {status}: {payload}")
        try:
            parsed = json.loads(payload)
        except ValueError:
            raise TelegramDeliveryError("Telegram API returned an unreadable response") from None
        if not parsed.get("ok", False):
            description = parsed.get("description", "unknown error")
            raise TelegramDeliveryError(f"Telegram API rejected the message: {description}")


class TelegramNotifier(Notifier):
    """Delivers the shared alert content to a Telegram chat via the Bot API."""

    def __init__(
        self,
        config: TelegramConfig,
        transport: TelegramTransport | None = None,
        timeout: float = 10.0,
        attractiveness_config: AttractivenessConfig | None = None,
        provider_ratings: Mapping[str, RatingRule] | None = None,
    ) -> None:
        if not config.bot_token or not config.chat_id:
            raise ValueError("Telegram notifier requires a bot token and chat id")
        self._chat_id = config.chat_id
        self._transport = transport or UrllibTelegramTransport(config.bot_token)
        self._timeout = timeout
        self._attractiveness_config = attractiveness_config
        self._provider_ratings = provider_ratings

    def send(self, notification: Notification) -> None:
        message = NotificationMessage.from_notification(notification)
        text = message.render(self._attractiveness_config, self._provider_ratings)
        if len(text) > TELEGRAM_MESSAGE_LIMIT:
            text = text[: TELEGRAM_MESSAGE_LIMIT - 1] + "…"
        self._transport.send_message(self._chat_id, text, self._timeout)


def deliver_pending(store: Store, notifier: Notifier) -> None:
    for notification in store.pending():
        try:
            notifier.send(notification)
        except Exception:
            logger.exception("Notification %s failed; will retry later", notification["id"])
            store.mark_retry(notification["id"])
        else:
            store.mark_delivered(notification["id"])
