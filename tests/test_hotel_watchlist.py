"""Hotel watchlist: independent search criteria, name matching, no duplicates.

Covers `watchlist.py` directly, its own config file (`hotel_watchlist.json`,
loaded by `config.load_hotel_watchlist`), its wiring into
`OfferPipeline`/`Scheduler`, and the notification rendering rule that a
watched-hotel alert never gets a HOT/GOOD/MATCH attractiveness category.
"""

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import WriteHotelWatchlist
from travel_deal_agent.config import Settings, load_settings
from travel_deal_agent.config_types import HotelWatchlistEntry
from travel_deal_agent.models import Offer
from travel_deal_agent.notification_content import NotificationMessage
from travel_deal_agent.notifications import Notifier
from travel_deal_agent.pipeline import OfferPipeline
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Notification, Store
from travel_deal_agent.watchlist import hotel_identity_matches, matches, matches_any

NEVERLAND_ENTRY: HotelWatchlistEntry = {
    "name": "Pickalbatros Jungle Aqua Park Resort Neverland",
    "aliases": [
        "Pickalbatros Jungle Aqua Park - Neverland",
        "Jungle Aqua Park Resort Neverland",
        "Jungle Aqua Park - Neverland",
        "Pick Albatros Jungle Aqua Park Resort Neverland",
    ],
    "country": "EG",
    "people": 2,
    "max_price_per_person": "2300",
    "min_nights": 7,
    "airports": ["LCJ", "WAW", "WMI", "KTW", "WRO"],
}


def _neverland_offer(offer: Offer, **overrides: object) -> Offer:
    """A base offer that satisfies every NEVERLAND_ENTRY rule; tests override
    exactly the one thing they mean to test."""
    base = replace(
        offer,
        provider="wakacje.pl",
        offer_id="neverland-1",
        hotel_name="Pickalbatros Jungle Aqua Park Resort Neverland",
        country="EG",
        destination="Hurghada",
        departure_airport="LCJ",
        number_of_people=2,
        price_per_person=Decimal("2300"),
        total_price=Decimal("2300") * 2,
        return_date=offer.departure_date + timedelta(days=7),  # type: ignore
    )
    return replace(base, **overrides)  # type: ignore


# --- Required scenarios from the feature request -----------------------------


@pytest.mark.parametrize("price,expected", [("2299", True), ("2300", True), ("2301", False)])
def test_price_boundary(offer: Offer, price: str, expected: bool) -> None:
    candidate = _neverland_offer(offer, price_per_person=Decimal(price))
    assert matches(candidate, NEVERLAND_ENTRY, []) is expected


def test_stay_shorter_than_seven_nights_is_rejected(offer: Offer) -> None:
    candidate = _neverland_offer(
        offer,
        return_date=offer.departure_date + timedelta(days=6),  # type: ignore
    )
    assert matches(candidate, NEVERLAND_ENTRY, []) is False


def test_stay_of_exactly_seven_nights_passes(offer: Offer) -> None:
    candidate = _neverland_offer(
        offer,
        return_date=offer.departure_date + timedelta(days=7),  # type: ignore
    )
    assert matches(candidate, NEVERLAND_ENTRY, []) is True


@pytest.mark.parametrize(
    "name",
    [
        "Pickalbatros Jungle Aqua Park Resort Neverland",
        "pickalbatros jungle aqua park resort neverland",  # case
        "Pickalbatros Jungle Aqua Park - Neverland",  # dash variant
        "PICKALBATROS  JUNGLE   AQUA PARK - NEVERLAND",  # extra whitespace
        "Jungle Aqua Park Resort Neverland",  # brand prefix dropped
        "Jungle Aqua Park - Neverland",
        "Pick Albatros Jungle Aqua Park Resort Neverland",  # two-word brand spelling
    ],
)
def test_accepted_name_variant_is_recognized(offer: Offer, name: str) -> None:
    candidate = _neverland_offer(offer, hotel_name=name)
    assert hotel_identity_matches(candidate, NEVERLAND_ENTRY) is True
    assert matches(candidate, NEVERLAND_ENTRY, []) is True


@pytest.mark.parametrize(
    "name",
    [
        "Pickalbatros Jungle Aqua Park",  # the wider complex, a different sub-resort
        "Pickalbatros Jungle Aqua Park Resort Aloha",  # a real sibling sub-resort
        "Pickalbatros Palm Beach Resort",  # unrelated Pickalbatros property
        "Jungle Water Park Hurghada",  # superficially similar, different hotel
    ],
)
def test_similar_but_different_hotel_name_is_not_recognized(offer: Offer, name: str) -> None:
    candidate = _neverland_offer(offer, hotel_name=name)
    assert hotel_identity_matches(candidate, NEVERLAND_ENTRY) is False
    assert matches(candidate, NEVERLAND_ENTRY, []) is False


def test_standard_search_still_uses_its_own_1500_limit(offer: Offer, settings: Settings) -> None:
    """The watchlist's higher ceiling never leaks into the standard search."""
    from travel_deal_agent.filtering import matches as standard_matches

    candidate = _neverland_offer(offer, price_per_person=Decimal("2000"))
    # Fails the standard 1500 PLN/person cap...
    assert standard_matches(candidate, settings.filters) is False
    # ...yet is a perfectly valid watchlist match at the same price.
    assert matches(candidate, NEVERLAND_ENTRY, []) is True


def test_watchlist_file_never_changes_other_filters(
    settings: Settings, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    write_hotel_watchlist([dict(NEVERLAND_ENTRY)])
    with_watchlist = load_settings()
    assert with_watchlist.filters == settings.filters
    assert with_watchlist.hotel_watchlist == [NEVERLAND_ENTRY]


# --- Safeguards beyond the required list -------------------------------------


def test_country_mismatch_rejects_a_same_named_hotel_elsewhere(offer: Offer) -> None:
    candidate = _neverland_offer(offer, country="TR")
    assert hotel_identity_matches(candidate, NEVERLAND_ENTRY) is False


def test_missing_country_data_does_not_block_a_match(offer: Offer) -> None:
    candidate = _neverland_offer(offer, country=None)
    assert hotel_identity_matches(candidate, NEVERLAND_ENTRY) is True


def test_airport_outside_the_watchlist_entry_is_rejected(offer: Offer) -> None:
    candidate = _neverland_offer(offer, departure_airport="KRK")
    assert matches(candidate, NEVERLAND_ENTRY, []) is False


def test_incomplete_price_follows_the_shared_whitelist(offer: Offer) -> None:
    candidate = _neverland_offer(offer, price_is_complete=False)
    assert matches(candidate, NEVERLAND_ENTRY, []) is False
    assert matches(candidate, NEVERLAND_ENTRY, ["wakacje.pl"]) is True


def test_matches_any_returns_none_without_a_hit(offer: Offer) -> None:
    candidate = replace(offer, hotel_name="Some Other Hotel")
    assert matches_any(candidate, [NEVERLAND_ENTRY], []) is None


# --- hotel_watchlist.json loading: the one place to add/remove/replace a hotel


def test_load_single_hotel_entry(write_hotel_watchlist: WriteHotelWatchlist) -> None:
    write_hotel_watchlist([dict(NEVERLAND_ENTRY)])
    assert load_settings().hotel_watchlist == [NEVERLAND_ENTRY]


def test_load_multiple_hotel_entries(write_hotel_watchlist: WriteHotelWatchlist) -> None:
    second = dict(
        NEVERLAND_ENTRY,
        name="Second Demo Hotel",
        aliases=["Second Demo Hotel"],
        max_price_per_person="1800",
    )
    write_hotel_watchlist([dict(NEVERLAND_ENTRY), second])
    loaded = load_settings().hotel_watchlist
    assert [entry["name"] for entry in loaded] == [NEVERLAND_ENTRY["name"], "Second Demo Hotel"]


def test_empty_list_means_no_active_watchlist(write_hotel_watchlist: WriteHotelWatchlist) -> None:
    write_hotel_watchlist([])
    assert load_settings().hotel_watchlist == []


def test_missing_file_means_no_active_watchlist(settings: Settings) -> None:
    # The `settings`/`write_config` fixtures point TDA_HOTEL_WATCHLIST at a
    # path that is never created unless a test calls `write_hotel_watchlist`.
    assert settings.hotel_watchlist == []


def test_malformed_json_disables_the_watchlist_without_crashing(
    write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture
) -> None:
    write_hotel_watchlist("{ not valid json")
    with caplog.at_level(logging.ERROR):
        loaded = load_settings()
    assert loaded.hotel_watchlist == []
    assert "hotel watchlist" in caplog.text.lower()


def test_invalid_entry_disables_the_watchlist_without_crashing(
    write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture
) -> None:
    # Structurally valid JSON, but fails business validation (empty alias) --
    # the whole application must still start, with the standard search intact.
    entry = dict(NEVERLAND_ENTRY, aliases=[""])
    write_hotel_watchlist([entry])
    with caplog.at_level(logging.ERROR):
        loaded = load_settings()
    assert loaded.hotel_watchlist == []
    assert "empty alias" in caplog.text


def test_unknown_field_disables_the_watchlist_without_crashing(
    write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture
) -> None:
    entry = dict(NEVERLAND_ENTRY, min_days=7)  # a typo'd field name
    write_hotel_watchlist([entry])
    with caplog.at_level(logging.ERROR):
        loaded = load_settings()
    assert loaded.hotel_watchlist == []
    assert "min_days" in caplog.text


def test_standard_search_runs_normally_without_any_watchlist_file(
    settings: Settings, store: Store
) -> None:
    """The app's normal behavior (config validation, standard search) never
    depends on hotel_watchlist.json existing at all."""
    from travel_deal_agent.providers.mock import MockProvider

    assert settings.hotel_watchlist == []
    settings = replace(
        settings, providers={"mock": {**settings.providers["mock"], "enabled": True}}
    )
    scheduler = Scheduler(settings, [MockProvider()], store, _RecordingNotifier())

    result = scheduler.run_once()

    assert len(result) > 0  # the standard search still finds the usual mock matches


# --- Config validation (still enforced, just non-fatal -- see load_hotel_watchlist)


def test_config_rejects_duplicate_hotel_names(
    write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture
) -> None:
    entry = dict(NEVERLAND_ENTRY)
    write_hotel_watchlist([entry, dict(entry)])
    with caplog.at_level(logging.ERROR):
        loaded = load_settings()
    assert loaded.hotel_watchlist == []
    assert "Duplicate hotel_watchlist name" in caplog.text


def test_provider_listings_accepts_providers_without_a_pinned_host(
    write_hotel_watchlist: WriteHotelWatchlist,
) -> None:
    """The mapping is provider-name -> https URL: a future ITAKA/TUI listing URL
    needs no model or schema change (only an optional host pin in config.py)."""
    listings = {"itaka": "https://www.itaka.pl/x/", "tui": "https://www.tui.pl/x/"}
    write_hotel_watchlist([dict(NEVERLAND_ENTRY, provider_listings=listings)])

    loaded = load_settings()

    assert loaded.hotel_watchlist[0]["provider_listings"] == listings


def test_config_rejects_max_nights_below_min_nights(
    write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture
) -> None:
    entry = dict(NEVERLAND_ENTRY, min_nights=7, max_nights=6)
    write_hotel_watchlist([entry])
    with caplog.at_level(logging.ERROR):
        loaded = load_settings()
    assert loaded.hotel_watchlist == []
    assert "max_nights must be at least min_nights" in caplog.text


def test_config_rejects_invalid_country_code(
    write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture
) -> None:
    entry = dict(NEVERLAND_ENTRY, country="Egypt")
    write_hotel_watchlist([entry])
    with caplog.at_level(logging.ERROR):
        loaded = load_settings()
    assert loaded.hotel_watchlist == []
    assert "2-letter uppercase code" in caplog.text


# --- Pipeline wiring: no duplicate offers, watchlist independent of filters --


def test_filter_watchlist_batch_excludes_already_accepted_offers(
    offer: Offer, store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    """An offer matching both the standard search and the watchlist is
    reported once, through the standard path -- never twice."""
    write_hotel_watchlist([dict(NEVERLAND_ENTRY)])
    settings = load_settings()
    # A candidate that satisfies every standard filter (see test_config.json:
    # min_nights 6-8, min_stars 3, itaka 1000-1500 PLN band needs rating>=5.0,
    # allowed board AI) *and* the watchlist's own rules (price <=2300, >=7
    # nights, LCJ, EG, 2 people).
    candidate = replace(
        offer,
        provider="itaka",
        offer_id="overlap-1",
        hotel_name="Pickalbatros Jungle Aqua Park Resort Neverland",
        country="EG",
        departure_airport="LCJ",
        hotel_stars=4,
        rating=5.5,
        board_type="all_inclusive",
        price_per_person=Decimal("1400"),
        total_price=Decimal("1400") * 2,
        return_date=offer.departure_date + timedelta(days=7),  # type: ignore
    )
    pipeline = OfferPipeline(settings, store, external_providers=[])

    standard_matches = pipeline.filter_batch("itaka", [candidate])
    watchlist_matches = pipeline.filter_watchlist_batch("itaka", [candidate], standard_matches)

    assert standard_matches == [candidate]
    assert watchlist_matches == []


def test_itaka_offer_confirmed_above_1500_is_kept_by_filter_watchlist_batch(
    offer: Offer, store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    """The itaka.py fix (widening `_detail_shortlist`) exists so a confirmed
    ITAKA offer above the standard 1500 cap can reach exactly this point:
    once `price_is_complete=True`, `filter_watchlist_batch` accepts it under
    the watchlist's own (higher) ceiling, the same as any other provider."""
    write_hotel_watchlist([dict(NEVERLAND_ENTRY)])
    settings = load_settings()
    candidate = _neverland_offer(
        offer,
        provider="itaka",
        price_per_person=Decimal("2200"),
        total_price=Decimal("2200") * 2,
        price_is_complete=True,
    )
    pipeline = OfferPipeline(settings, store, external_providers=[])

    standard_matches = pipeline.filter_batch("itaka", [candidate])
    watchlist_matches = pipeline.filter_watchlist_batch("itaka", [candidate], standard_matches)

    assert standard_matches == []  # still fails the standard search's own 1500 cap
    assert watchlist_matches == [candidate]


def test_watchlist_only_offer_is_kept_by_filter_watchlist_batch(
    offer: Offer, store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    write_hotel_watchlist([dict(NEVERLAND_ENTRY)])
    settings = load_settings()
    candidate = _neverland_offer(offer, provider="wakacje.pl", price_is_complete=False)
    pipeline = OfferPipeline(settings, store, external_providers=[])

    standard_matches = pipeline.filter_batch("wakacje.pl", [candidate])
    watchlist_matches = pipeline.filter_watchlist_batch("wakacje.pl", [candidate], standard_matches)

    assert watchlist_matches == [candidate]


# --- End-to-end: scheduler + storage + notification rendering ---------------


class _RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


def test_scheduler_raises_a_watchlist_alert_without_an_attractiveness_badge(
    offer: Offer, store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    write_hotel_watchlist([dict(NEVERLAND_ENTRY)])
    settings = load_settings()
    watchlist_offer = _neverland_offer(offer, provider="wakacje.pl", price_is_complete=False)

    from travel_deal_agent.providers.mock import MockProvider

    class SingleOfferProvider(MockProvider):
        name = "wakacje.pl"

        def fetch(self) -> list[Offer]:
            return [watchlist_offer]

    settings = replace(
        settings, providers={"wakacje.pl": {"enabled": True, "interval_seconds": 3600}}
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [SingleOfferProvider()], store, notifier)

    result = scheduler.run_once()

    assert [(o.provider, o.offer_id) for o in result] == [("wakacje.pl", "neverland-1")]
    # deliver_pending (run as part of run_once) already delivered it -- assert
    # on what was actually sent, not on the now-empty outbox.
    assert [n["kind"] for n in notifier.sent] == ["watchlist_new_offer"]
    message = NotificationMessage.from_notification(notifier.sent[0]).render(
        settings.attractiveness
    )
    assert "Obserwowany hotel" in message
    assert "NOWA" in message
    for badge in ("Szczególnie ciekawa", "Dobra oferta", "Spełnia kryteria"):
        assert badge not in message
    assert "2300" in message.replace(",", "")


def test_watchlist_reuses_the_existing_price_history_for_price_drop(
    offer: Offer, store: Store
) -> None:
    """No second history system: the same Store.observe/price_history the
    standard search uses records the watched hotel's price too."""
    first = _neverland_offer(offer, price_per_person=Decimal("2300"))
    assert store.observe(first, True, kind_prefix="watchlist_") == ["watchlist_new_offer"]

    lower = replace(first, price_per_person=Decimal("2100"))
    events = store.observe(lower, True, kind_prefix="watchlist_")

    assert events == ["price_changed", "watchlist_new_low"]
    assert store.price_history(first.provider, first.offer_id) == [
        Decimal("2300"),
        Decimal("2100"),
    ]
    kinds = [n["kind"] for n in store.pending()]
    assert kinds == ["watchlist_new_offer", "watchlist_new_low"]


def test_watchlist_returned_offer_keeps_its_prefix(offer: Offer, settings: Settings) -> None:
    """`kind_prefix` composes with every `classify_alert` outcome, not just
    new_offer/new_low -- including "returned" (the same shared alert_state
    re-arm mechanism the standard search uses, see `test_storage_scheduler.py`),
    so a watched hotel that drops out and comes back is still reported as a
    watchlist alert, not a plain one."""
    now = [datetime(2026, 9, 24, 7, tzinfo=timezone.utc)]
    rearm = timedelta(hours=24)
    watched = _neverland_offer(offer)
    with Store(settings.database, alert_rearm_after=rearm, clock=lambda: now[0]) as store:
        assert store.observe(watched, True, kind_prefix="watchlist_") == ["watchlist_new_offer"]
        now[0] += timedelta(hours=25)  # past the re-arm window, no observation in between
        assert store.observe(watched, True, kind_prefix="watchlist_") == ["watchlist_returned"]
        assert [n["kind"] for n in store.pending()] == ["watchlist_new_offer", "watchlist_returned"]
