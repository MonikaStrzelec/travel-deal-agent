"""Channel-independent alert content built from immutable outbox snapshots."""

from dataclasses import dataclass
from decimal import Decimal

from pydantic import TypeAdapter

from .models import Offer
from .storage import Notification

MISSING = "not available"


def format_money(value: Decimal | None, currency: str | None) -> str:
    """Keep missing prices explicit and preserve the source currency."""
    return f"{value:.2f} {currency or MISSING}" if value is not None else MISSING


def format_rating(value: float | None, maximum: float | None) -> str:
    """Avoid assuming a source scale when an older snapshot has no scale metadata."""
    if value is None:
        return MISSING
    return f"{value:g}/{maximum:g}" if maximum is not None else f"{value:g} (scale unknown)"


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

    def render(self) -> str:
        """Render the same complete content for the console, Telegram or a future
        Discord/email adapter.

        An incomplete price (`price_is_complete=False`) is rendered with an
        explicit disclaimer instead of being rejected: by the time a
        notification exists in the outbox, `filtering.matches()` has already
        decided this provider's listing price is acceptable to alert on (see
        `filters["accept_incomplete_price_from"]`). This method never
        re-checks that business decision -- it only reflects the persisted
        `price_is_complete` value in the rendered text.
        """
        offer = self.offer
        google = offer.google_rating if offer.external_rating_status == "verified" else None
        google_rating = (
            format_rating(google.get("rating"), offer.google_rating_max) if google else MISSING
        )
        if offer.hotel_ratings or offer.external_verification_statuses:
            verified_google = offer.hotel_ratings.get("google")
            google_rating = (
                format_rating(verified_google.rating, verified_google.scale_max)
                if verified_google is not None
                and offer.external_verification_statuses.get("google") == "verified"
                else MISSING
            )
        total = offer.total_price
        calculated = (
            total is None
            and offer.price_per_person is not None
            and offer.number_of_people is not None
        )
        if calculated and offer.price_per_person is not None and offer.number_of_people is not None:
            total = offer.price_per_person * offer.number_of_people
        lines = [
            f"Travel Deal Agent | {self.kind} | #{self.notification_id}",
            f"Hotel: {offer.hotel_name or MISSING}",
            f"Country / region: {offer.country or MISSING} / {offer.destination or MISSING}",
            f"Travel agency: {offer.provider.upper()}",
            f"Price per person: {format_money(offer.price_per_person, offer.currency)}",
            f"Total for {offer.number_of_people if offer.number_of_people is not None else '?'} travelers: "
            f"{format_money(total, offer.currency)}{' (calculated)' if calculated else ''}",
        ]
        if not offer.price_is_complete:
            lines.append("Price is from the listing — not yet confirmed at checkout/booking.")
        lines += [
            f"Duration: {offer.number_of_days if offer.number_of_days is not None else MISSING} days",
            f"Departure airport: {offer.departure_airport or MISSING}",
            f"Hotel stars: {offer.hotel_stars if offer.hotel_stars is not None else MISSING}",
            f"Agency rating: {format_rating(offer.rating, offer.provider_rating_max)}",
            f"Google rating: {google_rating}",
            f"Board: {offer.board_type or MISSING}",
            f"Offer URL: {offer.url or MISSING}",
            f"Final score: {offer.final_score:.3f}"
            if offer.final_score is not None
            else f"Final score: {MISSING}",
        ]
        for source in dict.fromkeys(["google", "tripadvisor", *offer.hotel_ratings]):
            result = offer.hotel_ratings.get(source)
            label = {"google": "Google", "tripadvisor": "Tripadvisor"}.get(source, source)
            value = (
                format_rating(result.rating, result.scale_max)
                if result is not None
                and offer.external_verification_statuses.get(source) == "verified"
                else MISSING
            )
            if source != "google":
                lines.append(f"{label} rating: {value}")
        if offer.booking_total_price is not None:
            lines.append(
                "Booking total includes mandatory operator fees; local costs are separate."
            )
            lines.append(f"Package price: {format_money(offer.package_price, offer.currency)}")
            for fee in offer.operator_mandatory_fees:
                lines.append(
                    f"Operator fee {fee.kind}: {format_money(fee.amount, fee.currency)} (party total)"
                )
        if not offer.local_mandatory_costs:
            lines.append("Local mandatory costs: no data (does not mean no costs)")
        for cost in offer.local_mandatory_costs:
            detail = cost.description
            if cost.amount is not None:
                detail += f" | {format_money(cost.amount, cost.currency)}"
            if cost.unit:
                detail += f" / {cost.unit}"
            if cost.conditions:
                detail += f" | {cost.conditions}"
            lines.append(f"Local mandatory cost ({cost.certainty}): {detail}")
        if (
            self.kind == "price_drop"
            and self.previous_price is not None
            and offer.price_per_person is not None
        ):
            drop = self.previous_price - offer.price_per_person
            lines.append(
                f"Price drop per person: {format_money(drop, offer.currency)} "
                f"(previous alert: {format_money(self.previous_price, offer.currency)})"
            )
        return "\n".join(lines)
