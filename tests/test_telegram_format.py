"""Regression tests for the compact Telegram-style message format, including
the attractiveness-driven header (HOT/GOOD/MATCH)."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from travel_deal_agent.config import Settings
from travel_deal_agent.models import Offer
from travel_deal_agent.notification_content import NotificationMessage
from travel_deal_agent.storage import Store


def _at_nights(offer: Offer, nights: int) -> Offer:
    assert offer.departure_date is not None
    return replace(offer, return_date=offer.departure_date + timedelta(days=nights))


def _render(store: Store, settings: Settings) -> str:
    return NotificationMessage.from_notification(store.pending()[0]).render(
        settings.attractiveness, settings.filters["provider_ratings"]
    )


def test_hot_new_offer_header(offer: Offer, store: Store, settings: Settings) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=9.2,
            hotel_stars=4,
            departure_airport="WAW",
            board_type="HB",
            price_per_person=Decimal("1000"),
        ),
        6,
    )
    store.observe(candidate, True)

    assert _render(store, settings).startswith("🔥 NOWA • Szczególnie ciekawa")


def test_good_new_offer_header(offer: Offer, store: Store, settings: Settings) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=9.2,
            hotel_stars=4,
            departure_airport="KTW",
            board_type="HB",
            price_per_person=Decimal("1200"),
        ),
        6,
    )
    store.observe(candidate, True)

    assert _render(store, settings).startswith("👍 NOWA • Dobra oferta")


def test_match_new_offer_header(offer: Offer, store: Store, settings: Settings) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=8.0,
            hotel_stars=3,
            departure_airport="KTW",
            board_type="HB",
            price_per_person=Decimal("1200"),
        ),
        6,
    )
    store.observe(candidate, True)

    assert _render(store, settings).startswith("✓ NOWA • Spełnia kryteria")


def test_price_drop_keeps_the_same_category_header_shape(
    offer: Offer, store: Store, settings: Settings
) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=9.2,
            hotel_stars=4,
            departure_airport="WAW",
            board_type="HB",
            price_per_person=Decimal("1000"),
        ),
        6,
    )
    store.observe(candidate, True)
    store.observe(replace(candidate, price_per_person=Decimal("950")), True)

    message = NotificationMessage.from_notification(store.pending()[-1]).render(
        settings.attractiveness, settings.filters["provider_ratings"]
    )

    assert message.startswith("🔥 SPADEK CENY • Szczególnie ciekawa")
    assert "📉 Poprzednio: 1000 zł/os." in message


def test_wakacje_incomplete_price_shows_disclaimer(
    offer: Offer, store: Store, settings: Settings
) -> None:
    candidate = replace(offer, provider="wakacje.pl", price_is_complete=False)
    store.observe(candidate, True)

    assert "ℹ️ Cena z listingu — niepotwierdzona." in _render(store, settings)


def test_complete_price_has_no_disclaimer(offer: Offer, store: Store, settings: Settings) -> None:
    store.observe(offer, True)

    assert "niepotwierdzona" not in _render(store, settings)


def test_full_compact_format_matches_the_agreed_layout(
    offer: Offer, store: Store, settings: Settings
) -> None:
    """One end-to-end shape check against the format agreed with the project
    owner: hotel/rating+board/price+price-per-night/airport+duration/date-range/
    climate, in that order, with the attractiveness header on top and the
    link+disclaimer block at the bottom.

    "Słoneczny Brzeg" in May resolves through Climate V0's region alias
    (climate.py), giving the "sun" line its 22C -- see test_climate.py for
    the region-alias/country-fallback rules this depends on.

    This offer's own real classification under the accepted V0 rule is MATCH
    (VALUE/HOTEL QUALITY/AIRPORT/BOARD all land on "normal", no strong area) --
    the illustrative mockup that established this layout used a HOT header as
    a generic placeholder, not a claim about this specific data.

    `price_is_complete=False` here on purpose: every real Wakacje.pl listing
    offer in the local database has it (Wakacje.pl is listing-only and is the
    sole entry in `accept_incomplete_price_from`), so a representative
    Wakacje.pl example must show the disclaimer, not omit it.
    """
    candidate = replace(
        offer,
        provider="wakacje.pl",
        hotel_name="Meridian",
        hotel_stars=4,
        country="BG",
        destination="Słoneczny Brzeg / Słoneczny Brzeg",
        rating=8.0,
        provider_rating_max=10,
        board_type="HB",
        departure_airport="WAW",
        departure_date=date(2027, 5, 18),
        return_date=date(2027, 5, 25),
        number_of_days=8,
        price_per_person=Decimal("1393"),
        total_price=Decimal("2786"),
        number_of_people=2,
        url="https://example.invalid/oferty/meridian",
        price_is_complete=False,
    )
    store.observe(candidate, True)

    message = _render(store, settings)
    lines = message.split("\n")

    assert lines[0] == "✓ NOWA • Spełnia kryteria"
    assert lines[1] == "🏨 Meridian ★★★★ • Bułgaria • Słoneczny Brzeg"
    assert lines[2] == "⭐ 8,0/10 🍽 Śniadania i obiadokolacje (HB)"
    assert lines[3] == "💰 1393 zł/os. (2786 zł / 2 osoby) • 199 zł/os./noc"
    assert lines[4] == "🛫 Warszawa • 8 dni / 7 nocy"
    assert lines[5] == "📅 18.05 (wtorek) – 25.05.2027 (wtorek)"
    assert lines[6] == "☀️ Typowo w maju: ok. 22°C"
    assert lines[7] == ""
    assert lines[8] == '🔗 <a href="https://example.invalid/oferty/meridian">Zobacz ofertę</a>'
    assert lines[9] == "ℹ️ Cena z listingu — niepotwierdzona."


def test_date_range_shows_year_on_both_sides_when_they_differ(
    offer: Offer, store: Store, settings: Settings
) -> None:
    candidate = replace(offer, departure_date=date(2026, 12, 28), return_date=date(2027, 1, 4))
    store.observe(candidate, True)

    assert "📅 28.12.2026 (poniedziałek) – 04.01.2027 (poniedziałek)" in _render(store, settings)


def test_html_special_characters_are_escaped(
    offer: Offer, store: Store, settings: Settings
) -> None:
    candidate = replace(
        offer,
        hotel_name='Sunny "Beach" <Resort> & Spa',
        country=None,
        destination="A & B <script>alert(1)</script>",
        url="https://example.invalid/o?a=1&b=2",
    )
    store.observe(candidate, True)

    message = _render(store, settings)

    assert "<script>" not in message
    assert "<Resort>" not in message
    assert "&amp;" in message
    assert 'href="https://example.invalid/o?a=1&amp;b=2"' in message
    assert "Zobacz ofertę</a>" in message


def test_climate_line_shown_for_a_recognized_region(
    offer: Offer, store: Store, settings: Settings
) -> None:
    candidate = replace(
        offer,
        country="EG",
        destination="Hurghada / Hurghada",
        departure_date=date(2026, 8, 15),
        return_date=date(2026, 8, 22),
    )
    store.observe(candidate, True)

    message = _render(store, settings)
    lines = message.split("\n")

    assert "☀️ Typowo w sierpniu: ok. 38°C" in message
    # Exactly between the date line and the blank line before the link.
    date_index = next(i for i, line in enumerate(lines) if line.startswith("📅"))
    assert lines[date_index + 1] == "☀️ Typowo w sierpniu: ok. 38°C"
    assert lines[date_index + 2] == ""


def test_climate_line_omitted_for_an_unrecognized_destination(
    offer: Offer, store: Store, settings: Settings
) -> None:
    # The default fixture offer (GR / "Crete", in English) matches no alias,
    # and GR has no safe country fallback -- the line must simply not appear,
    # never a placeholder like "brak danych" or "n/a".
    store.observe(offer, True)

    message = _render(store, settings)

    assert "☀️" not in message
    assert "brak danych" not in message.lower()
    assert "n/a" not in message.lower()
