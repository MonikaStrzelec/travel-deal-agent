"""Run an offline demonstration or the local scheduler."""

import argparse
import logging
import signal
from logging.handlers import RotatingFileHandler
from types import FrameType

from .config import ROOT, load_settings, load_telegram_config
from .config_types import ProviderConfig
from .notifications import ConsoleNotifier, Notifier, TelegramNotifier
from .presentation import format_ratings
from .providers.registry import build_providers
from .scheduler import Scheduler
from .storage import Store


def _mock_data_warning_applies(providers: dict[str, ProviderConfig]) -> bool:
    """True only when mock is enabled and every other (real) provider is not.

    Generic over provider names -- adding a new real provider never requires
    touching this function, unlike a check hardcoded to specific providers.
    """
    mock_config = providers.get("mock")
    mock_enabled = mock_config is not None and mock_config["enabled"]
    real_provider_enabled = any(
        name != "mock" and cfg["enabled"] for name, cfg in providers.items()
    )
    return mock_enabled and not real_provider_enabled


def _raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    """Route SIGTERM (e.g. `docker stop`) through the existing KeyboardInterrupt cleanup path."""
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Travel Deal Agent (mock or manual ITAKA booking-price checks)"
    )
    parser.add_argument("--watch", action="store_true", help="Run continuously; Ctrl+C stops")
    parser.add_argument(
        "--force", action="store_true", help="Ignore due times for one manual check"
    )
    args = parser.parse_args()
    if args.watch and args.force:
        parser.error("--force is only available for a single check")
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        settings = load_settings()
        telegram_config = load_telegram_config()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Configuration error: {exc}\n")
    rainbow_config = settings.providers.get("rainbow")
    rainbow_enabled = rainbow_config is not None and rainbow_config["enabled"]
    if args.watch and rainbow_enabled:
        parser.error("Rainbow is manual-only; disable it before using --watch")
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                ROOT / "logs" / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            ),
        ],
    )
    notifier: Notifier = TelegramNotifier(telegram_config) if telegram_config else ConsoleNotifier()
    logging.info("Notifications: %s", "telegram" if telegram_config else "console")
    store = Store(settings.database)
    try:
        scheduler = Scheduler(
            settings,
            build_providers(settings.providers, filters=settings.filters),
            store,
            notifier,
        )
        if args.watch:
            scheduler.run_forever()
        else:
            offers = scheduler.run_once(force=args.force)
            if _mock_data_warning_applies(settings.providers):
                logging.info("MOCK DATA - not real travel offers")
            for index, offer in enumerate(offers, 1):
                logging.info(
                    "%s. %s | %s | %s | %s %s/person | %s days",
                    index,
                    offer.hotel_name,
                    offer.country,
                    offer.departure_airport,
                    offer.price_per_person,
                    offer.currency,
                    offer.number_of_days,
                )
                logging.info("%s", format_ratings(offer, settings.ranking))
    except KeyboardInterrupt:
        logging.info("Stopped by user")
    finally:
        store.close()


if __name__ == "__main__":
    main()
