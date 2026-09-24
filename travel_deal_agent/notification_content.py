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

# Polish display names for the ISO codes this project's providers can produce.
# Not guessed: the same evidence-backed names already used for display in
# rainbow_data.COUNTRIES/tui_data.COUNTRIES (real Polish names observed on
# real cards), just keyed by ISO code here instead of by source slug/label --
# presentation only, this module never depends on provider internals.
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

# Canonical board codes only (`boards.CANONICAL_BOARDS`); any other value
# (e.g. a provider-specific label that was never normalized) falls back to
# being shown as-is, never guessed into one of these.
BOARD_LABELS_PL = {
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

# Compact display labels for this message, deliberately separate from
# rainbow_config.AIRPORT_LABELS (which mirrors labels actually observed on
# Rainbow's own site, e.g. "Warszawa Chopin", and must not be repurposed for
# unrelated presentation elsewhere).
AIRPORT_DISPLAY_LABELS_PL = {
    "LCJ": "Łódź",
    "WAW": "Warszawa",
    "WMI": "Warszawa Modlin",
    "KTW": "Katowice",
    "WRO": "Wrocław",
}

EVENT_LABELS_PL = {
    "new_offer": "NOWA",
    "price_drop": "SPADEK CENY",
}

# Locative case ("w maju"), indexed by `date.month - 1`. Used only for the
# Climate V0 "sun" line -- see climate.py for what the temperature itself means.
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

# (emoji, Polish label) for each attractiveness.Attractiveness category. The
# emoji here is the header's ONLY emoji -- it replaces the old fixed
# event emoji entirely; the event itself (new offer vs. price drop) is
# conveyed by EVENT_LABELS_PL alone.
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
        """Render one compact message, in Polish, for the console or Telegram.

        The header's category (HOT/GOOD/MATCH) is computed fresh from the
        current `offer` via `attractiveness.classify_offer` every time this
        is called -- it is never read from a stored field. Callers that do
        not care about calibrated classification (most existing tests) may
        omit both config arguments and get the built-in V0 defaults; the
        production entry point (`__main__.py`) always passes the real,
        configured values through `Notifier`.

        `final_score` and Tripadvisor rating are deliberately never shown here
        (they still drive ranking/eligibility elsewhere -- only the message
        content is affected). An incomplete price (`price_is_complete=False`)
        still gets a short disclaimer, moved under the link rather than mixed
        into the main body: by the time a notification exists in the outbox,
        `filtering.matches()` has already decided this provider's listing
        price is acceptable to alert on (see
        `filters["accept_incomplete_price_from"]`); this method never
        re-checks that decision, only discloses it.
        """
        offer = self.offer
        e = html.escape
        ratings = provider_ratings if provider_ratings is not None else {}
        breakdown = classify_offer(offer, ratings, attractiveness_config)
        emoji, category_label = ATTRACTIVENESS_LABELS_PL[breakdown.category]
        event_label = EVENT_LABELS_PL.get(self.kind, self.kind)

        lines = [f"{emoji} {event_label} • {category_label}"]

        hotel = [e(offer.hotel_name) if offer.hotel_name else "Hotel nieznany"]
        if offer.hotel_stars is not None:
            hotel[0] += f" {'★' * max(0, round(offer.hotel_stars))}"
        country_name = COUNTRY_NAMES_PL.get(offer.country, offer.country) if offer.country else None
        if country_name:
            hotel.append(e(country_name))
        destination = _compact_destination(offer.destination)
        if destination:
            hotel.append(e(destination))
        lines.append(f"🏨 {' • '.join(hotel)}")

        rating_part = None
        if offer.rating is not None:
            rating_part = f"⭐ {f'{offer.rating:.1f}'.replace('.', ',')}"
            if offer.provider_rating_max is not None:
                rating_part += f"/{_pl_number(offer.provider_rating_max)}"
            for source in dict.fromkeys(["google", "tripadvisor", *offer.hotel_ratings]):
                verified = _verified_external_rating(offer, source)
                if verified is None:
                    continue
                v_rating, v_max = verified
                label = EXTERNAL_SOURCE_LABELS.get(source, source.capitalize())
                rating_part += (
                    f" ({label}: {f'{v_rating:.1f}'.replace('.', ',')}/{_pl_number(v_max)})"
                )
        board_part = None
        if offer.board_type:
            board_label = BOARD_LABELS_PL.get(offer.board_type, offer.board_type)
            board_part = f"🍽 {e(board_label)}"
        if rating_part or board_part:
            lines.append(" ".join(part for part in (rating_part, board_part) if part))

        nights = (
            (offer.return_date - offer.departure_date).days
            if offer.departure_date is not None and offer.return_date is not None
            else None
        )

        if offer.price_per_person is not None:
            price_line = f"💰 {_pl_decimal(offer.price_per_person)} zł/os."
            people = offer.number_of_people
            total = offer.total_price
            if total is None and people is not None:
                total = offer.price_per_person * people
            if total is not None and people is not None:
                price_line += f" ({_pl_decimal(total)} zł / {people} {_pl_people(people)})"
            per_night = _price_per_person_per_night(offer.price_per_person, nights)
            if per_night is not None:
                price_line += f" • {per_night}"
            lines.append(price_line)
            if self.kind == "price_drop" and self.previous_price is not None:
                lines.append(f"📉 Poprzednio: {_pl_decimal(self.previous_price)} zł/os.")

        if offer.departure_airport:
            airport_name = AIRPORT_DISPLAY_LABELS_PL.get(
                offer.departure_airport, offer.departure_airport
            )
            departure_line = f"🛫 {e(airport_name)}"
            stay_length = _stay_length(offer)
            if stay_length is not None:
                departure_line += f" • {stay_length}"
            lines.append(departure_line)

        if offer.departure_date is not None and offer.return_date is not None:
            lines.append(f"📅 {_pl_date_range(offer.departure_date, offer.return_date)}")
        elif offer.departure_date is not None:
            lines.append(f"📅 {_pl_date(offer.departure_date)}")
        elif offer.return_date is not None:
            lines.append(f"🛬 Powrót: {_pl_date(offer.return_date)}")

        if offer.departure_date is not None:
            temperature = typical_daytime_temperature(
                offer.country, offer.destination, offer.departure_date.month
            )
            if temperature is not None:
                month_name = MONTHS_PL_LOCATIVE[offer.departure_date.month - 1]
                lines.append(f"☀️ Typowo w {month_name}: ok. {temperature}°C")

        lines.append("")

        if offer.url:
            lines.append(f'🔗 <a href="{e(offer.url)}">Zobacz ofertę</a>')

        if not offer.price_is_complete:
            lines.append("ℹ️ Cena z listingu — niepotwierdzona.")

        if offer.booking_total_price is not None:
            lines.append(
                f"🧾 Cena całkowita rezerwacji: {_pl_decimal(offer.booking_total_price)} zł "
                f"(zawiera opłaty obowiązkowe)"
            )
        for cost in offer.local_mandatory_costs:
            detail = e(cost.description)
            if cost.amount is not None:
                detail += f" ({_pl_decimal(cost.amount)} {e(cost.currency or '')})"
            lines.append(f"🧾 {detail}")

        return "\n".join(lines).strip()
