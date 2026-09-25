"""Channel-independent alert content built from immutable outbox snapshots.

Rendered as compact, Polish-language Telegram-style text (the project owner is
the sole recipient). HTML is used only for the offer link (`parse_mode: "HTML"`,
set in `notifications.UrllibTelegramTransport`) -- every piece of free text is
escaped with `html.escape` first, so a hotel name or destination containing
`&`/`<`/`>` can never break the message or the link markup.
"""

import html
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from pydantic import TypeAdapter

from .attractiveness import Attractiveness, classify_offer
from .climate import typical_daytime_temperature
from .config_types import AttractivenessConfig, RatingRule
from .models import Offer
from .storage import Notification

# The Polish names observed on real provider cards, keyed by ISO code so this
# module never depends on provider internals.
COUNTRY_NAMES_PL = {
    "TR": "Turcja",
    "GR": "Grecja",
    "TN": "Tunezja",
    "AL": "Albania",
    "EG": "Egipt",
    "ES": "Hiszpania",
    "BG": "Bułgaria",
    "CY": "Cypr",
    "IT": "Włochy",
    "PT": "Portugalia",
    "MT": "Malta",
}

# Canonical board codes only; any other value is shown as-is, never guessed.
BOARD_LABELS_PL = {
    "UAI": "Ultra All Inclusive (UAI)",
    "AI": "All Inclusive (AI)",
    "HB": "Śniadania i obiadokolacje (HB)",
    "FB": "Pełne wyżywienie (FB)",
    "BB": "Śniadania (BB)",
    "ZO": "Według programu (ZO)",
    "RO": "Bez wyżywienia (RO)",
}

WEEKDAYS_PL = (
    "poniedziałek",
    "wtorek",
    "środa",
    "czwartek",
    "piątek",
    "sobota",
    "niedziela",
)

# Deliberately separate from rainbow_config.AIRPORT_LABELS, which mirrors the
# labels observed on Rainbow's own site.
AIRPORT_DISPLAY_LABELS_PL = {
    "LCJ": "Łódź",
    "WAW": "Warszawa",
    "WMI": "Warszawa Modlin",
    "KTW": "Katowice",
    "WRO": "Wrocław",
}

# `new_offer` borrows the attractiveness emoji; price-history events keep their
# own glyph so they stay recognizable in a feed regardless of category.
EVENT_LABELS_PL: dict[str, tuple[str | None, str]] = {
    "new_offer": (None, "NOWA"),
    "returned": ("↩️", "WRÓCIŁA"),
    "price_drop": ("📉", "SPADEK CENY"),
    "new_low": ("🏆", "NAJNIŻSZA CENA"),
}

# Locative case ("w maju") for the climate line; see climate.py for the data.
MONTHS_PL_LOCATIVE = (
    "styczniu",
    "lutym",
    "marcu",
    "kwietniu",
    "maju",
    "czerwcu",
    "lipcu",
    "sierpniu",
    "wrześniu",
    "październiku",
    "listopadzie",
    "grudniu",
)

ATTRACTIVENESS_LABELS_PL: dict[Attractiveness, tuple[str, str]] = {
    "HOT": ("🔥", "Szczególnie ciekawa"),
    "GOOD": ("👍", "Dobra oferta"),
    "MATCH": ("✓", "Spełnia kryteria"),
}


def _pl_number(value: float) -> str:
    """A plain integer when whole, one Polish-comma decimal otherwise."""
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}".replace(".", ",")


def _pl_decimal(value: Decimal) -> str:
    """A plain integer when whole, two Polish-comma decimals otherwise."""
    whole = value.to_integral_value()
    if value == whole:
        return str(whole)
    return f"{value:.2f}".replace(".", ",")


def _pl_people(count: int) -> str:
    """Standard Polish plural for "osoba": 1 / 2-4 / 5+."""
    if count == 1:
        return "osobę"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "osoby"
    return "osób"


def _pl_nights(count: int) -> str:
    """Standard Polish plural for "noc": 1 / 2-4 / 5+."""
    if count == 1:
        return "noc"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "noce"
    return "nocy"


def _pl_days(count: int) -> str:
    """Standard Polish plural for "dzień": only 1 vs. everything else."""
    return "dzień" if count == 1 else "dni"


def _stay_length(offer: Offer) -> str | None:
    """Renders as "<days> dni / <nights> nocy" -- the phrasing Wakacje.pl itself
    shows a shopper on an offer's own page (e.g. "8 dni / 7 nocy" for a 7-night
    stay).

    Derived from `departure_date`/`return_date` -- the one stay-length signal
    whose meaning (a calendar span) is the same for every provider -- rather
    than from `Offer.number_of_days`, whose native meaning is NOT consistent
    across providers (ITAKA and Rainbow already store their own touroperator
    "dni" count, nights + 1, while Wakacje.pl and TUI store a plain night
    count). Falls back to the source's own `number_of_days` value only when a
    date is missing, and never relabels it in that case (its "dni"/"nights"
    meaning is provider-specific and not resolved here).
    """
    if offer.departure_date is not None and offer.return_date is not None:
        nights = (offer.return_date - offer.departure_date).days
        if nights >= 1:
            days = nights + 1
            return f"{days} {_pl_days(days)} / {nights} {_pl_nights(nights)}"
    if offer.number_of_days is not None:
        return f"{offer.number_of_days} dni"
    return None


def _pl_date(value: date) -> str:
    return f"{value.strftime('%d.%m.%Y')} ({WEEKDAYS_PL[value.weekday()]})"


def _pl_date_short(value: date, *, with_year: bool) -> str:
    pattern = "%d.%m.%Y" if with_year else "%d.%m"
    return f"{value.strftime(pattern)} ({WEEKDAYS_PL[value.weekday()]})"


def _pl_date_range(departure: date, return_date: date) -> str:
    """One line for both dates. The year is shown on the departure date too
    only when it differs from the return year, so a same-year trip (the
    common case) is not cluttered with a repeated year."""
    same_year = departure.year == return_date.year
    return (
        f"{_pl_date_short(departure, with_year=not same_year)} – "
        f"{_pl_date_short(return_date, with_year=True)}"
    )


def _price_per_person_per_night(price_per_person: Decimal, nights: int | None) -> str | None:
    """Canonical nights only (`return_date - departure_date`); fails safe (no
    division) when nights is unknown or non-positive rather than guessing."""
    if nights is None or nights <= 0:
        return None
    per_night = (price_per_person / nights).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{_pl_decimal(per_night)} zł/os./noc"


def _compact_destination(destination: str | None) -> str | None:
    """Collapse "Region / Region" (region == city) to one part; keep distinct
    parts, comma-joined -- never invents or drops real place information."""
    if not destination:
        return None
    parts = [p.strip() for p in destination.split("/") if p.strip()]
    deduped: list[str] = []
    for part in parts:
        if not deduped or deduped[-1].casefold() != part.casefold():
            deduped.append(part)
    return ", ".join(deduped) if deduped else None


EXTERNAL_SOURCE_LABELS = {"google": "Google", "tripadvisor": "TripAdvisor"}


def _verified_external_rating(offer: Offer, source: str) -> tuple[float, float] | None:
    """(rating, scale_max) only when a source has actually confirmed it."""
    verified = offer.hotel_ratings.get(source)
    if verified is not None and offer.external_verification_statuses.get(source) == "verified":
        return verified.rating, verified.scale_max
    if (
        source == "google"
        and offer.google_rating is not None
        and offer.external_rating_status == "verified"
    ):
        rating = offer.google_rating.get("rating")
        if rating is not None and offer.google_rating_max is not None:
            return rating, offer.google_rating_max
    return None


@dataclass(frozen=True)
class NotificationMessage:
    """A portable message; future transports use notification_id for idempotency."""

    notification_id: int
    kind: str
    offer: Offer
    previous_price: Decimal | None

    @classmethod
    def from_notification(cls, notification: Notification) -> "NotificationMessage":
        """Read persisted alert-time values, never current configuration or live data."""
        return cls(
            notification["id"],
            notification["kind"],
            TypeAdapter(Offer).validate_json(notification["payload"]),
            Decimal(notification["previous_price"])
            if notification["previous_price"] is not None
            else None,
        )

    def render(
        self,
        attractiveness_config: AttractivenessConfig | None = None,
        provider_ratings: Mapping[str, RatingRule] | None = None,
    ) -> str:
        """Render one compact Polish message for the console or Telegram.

        The HOT/GOOD/MATCH category is classified fresh from the snapshot, never
        stored. `final_score` is deliberately not shown. An incomplete price only
        gets a disclaimer: `filtering.matches()` already decided it may alert.
        """
        offer = self.offer
        ratings = provider_ratings if provider_ratings is not None else {}
        category = classify_offer(offer, ratings, attractiveness_config).category
        body = [
            _header_line(self.kind, category),
            _hotel_line(offer),
            _rating_and_board_line(offer),
            *_price_lines(offer, self.kind, self.previous_price),
            _departure_line(offer),
            _dates_line(offer),
            _climate_line(offer),
        ]
        footer = [
            _link_line(offer),
            None if offer.price_is_complete else "ℹ️ Cena z listingu — niepotwierdzona.",
            *_booking_cost_lines(offer),
        ]
        lines = [line for line in body if line is not None]
        lines.append("")
        lines.extend(line for line in footer if line is not None)
        return "\n".join(lines).strip()


def _nights(offer: Offer) -> int | None:
    if offer.departure_date is None or offer.return_date is None:
        return None
    return (offer.return_date - offer.departure_date).days


def _header_line(kind: str, category: Attractiveness) -> str:
    attractiveness_emoji, category_label = ATTRACTIVENESS_LABELS_PL[category]
    event_emoji, event_label = EVENT_LABELS_PL.get(kind, (None, kind))
    return f"{event_emoji or attractiveness_emoji} {event_label} • {category_label}"


def _hotel_line(offer: Offer) -> str:
    name = html.escape(offer.hotel_name) if offer.hotel_name else "Hotel nieznany"
    if offer.hotel_stars is not None:
        name += f" {'★' * max(0, round(offer.hotel_stars))}"
    parts = [name]
    country_name = COUNTRY_NAMES_PL.get(offer.country, offer.country) if offer.country else None
    if country_name:
        parts.append(html.escape(country_name))
    destination = _compact_destination(offer.destination)
    if destination:
        parts.append(html.escape(destination))
    return f"🏨 {' • '.join(parts)}"


def _pl_rating(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def _rating_and_board_line(offer: Offer) -> str | None:
    rating_part = None
    if offer.rating is not None:
        rating_part = f"⭐ {_pl_rating(offer.rating)}"
        if offer.provider_rating_max is not None:
            rating_part += f"/{_pl_number(offer.provider_rating_max)}"
        for source in dict.fromkeys(["google", "tripadvisor", *offer.hotel_ratings]):
            verified = _verified_external_rating(offer, source)
            if verified is None:
                continue
            external_rating, external_max = verified
            label = EXTERNAL_SOURCE_LABELS.get(source, source.capitalize())
            rating_part += f" ({label}: {_pl_rating(external_rating)}/{_pl_number(external_max)})"
    board_part = None
    if offer.board_type:
        board_label = BOARD_LABELS_PL.get(offer.board_type, offer.board_type)
        board_part = f"🍽 {html.escape(board_label)}"
    if not (rating_part or board_part):
        return None
    return " ".join(part for part in (rating_part, board_part) if part)


def _price_lines(offer: Offer, kind: str, previous_price: Decimal | None) -> list[str]:
    if offer.price_per_person is None:
        return []
    price_line = f"💰 {_pl_decimal(offer.price_per_person)} zł/os."
    people = offer.number_of_people
    total = offer.total_price
    if total is None and people is not None:
        total = offer.price_per_person * people
    if total is not None and people is not None:
        price_line += f" ({_pl_decimal(total)} zł / {people} {_pl_people(people)})"
    per_night = _price_per_person_per_night(offer.price_per_person, _nights(offer))
    if per_night is not None:
        price_line += f" • {per_night}"
    lines = [price_line]
    if kind in ("price_drop", "new_low") and previous_price is not None:
        drop = previous_price - offer.price_per_person
        lines.append(
            f"📉 Było {_pl_decimal(previous_price)} zł/os. • spadek {_pl_decimal(drop)} zł"
        )
    return lines


def _departure_line(offer: Offer) -> str | None:
    if not offer.departure_airport:
        return None
    airport_name = AIRPORT_DISPLAY_LABELS_PL.get(offer.departure_airport, offer.departure_airport)
    line = f"🛫 {html.escape(airport_name)}"
    stay_length = _stay_length(offer)
    if stay_length is not None:
        line += f" • {stay_length}"
    return line


def _dates_line(offer: Offer) -> str | None:
    if offer.departure_date is not None and offer.return_date is not None:
        return f"📅 {_pl_date_range(offer.departure_date, offer.return_date)}"
    if offer.departure_date is not None:
        return f"📅 {_pl_date(offer.departure_date)}"
    if offer.return_date is not None:
        return f"🛬 Powrót: {_pl_date(offer.return_date)}"
    return None


def _climate_line(offer: Offer) -> str | None:
    if offer.departure_date is None:
        return None
    month = offer.departure_date.month
    temperature = typical_daytime_temperature(offer.country, offer.destination, month)
    if temperature is None:
        return None
    return f"☀️ Typowo w {MONTHS_PL_LOCATIVE[month - 1]}: ok. {temperature}°C"


def _link_line(offer: Offer) -> str | None:
    if not offer.url:
        return None
    return f'🔗 <a href="{html.escape(offer.url)}">Zobacz ofertę</a>'


def _booking_cost_lines(offer: Offer) -> list[str]:
    lines: list[str] = []
    if offer.booking_total_price is not None:
        lines.append(
            f"🧾 Cena całkowita rezerwacji: {_pl_decimal(offer.booking_total_price)} zł "
            f"(zawiera opłaty obowiązkowe)"
        )
    for cost in offer.local_mandatory_costs:
        detail = html.escape(cost.description)
        if cost.amount is not None:
            detail += f" ({_pl_decimal(cost.amount)} {html.escape(cost.currency or '')})"
        lines.append(f"🧾 {detail}")
    return lines
