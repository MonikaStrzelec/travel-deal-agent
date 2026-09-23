"""Provider-neutral models. Providers normalize countries to ISO alpha-2 codes."""

import hashlib
import json
import math
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from typing_extensions import TypedDict


class GoogleRatingData(TypedDict, total=False):
    """Serializable external result; absent fields represent unavailable data."""

    rating: float
    number_of_reviews: int | None
    matched_hotel_name: str
    location: str | None
    place_id: str | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ExternalHotelRating:
    """Independent source evidence, with native scale and explicit match confidence."""

    source: str
    rating: float
    scale_min: float
    scale_max: float
    number_of_reviews: int | None
    matched_hotel_name: str
    country: str | None
    confidence: float
    location: str | None = None
    external_id: str | None = None
    ambiguous: bool = False

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.matched_hotel_name.strip():
            raise ValueError("Source and matched hotel name are required")
        if not all(
            math.isfinite(v) for v in (self.rating, self.scale_min, self.scale_max, self.confidence)
        ):
            raise ValueError("External rating values must be finite")
        if (
            not self.scale_min < self.scale_max
            or not self.scale_min <= self.rating <= self.scale_max
        ):
            raise ValueError("External rating outside its native scale")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Confidence must be in [0, 1]")
        if self.number_of_reviews is not None and (
            type(self.number_of_reviews) is not int or self.number_of_reviews < 0
        ):
            raise ValueError("Invalid external review count")


@dataclass(frozen=True)
class OperatorFee:
    """One mandatory operator fee for the entire party, never per person."""

    kind: str
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        amount = Decimal(str(self.amount))
        if not self.kind or not self.currency or not amount.is_finite() or amount < 0:
            raise ValueError("Invalid operator fee")
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True)
class LocalMandatoryCost:
    """Information outside the booking total; absence never proves zero cost."""

    description: str
    certainty: Literal["exact", "approximate", "unknown"] = "unknown"
    amount: Decimal | None = None
    currency: str | None = None
    unit: str | None = None
    conditions: str | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        if not self.description.strip() or self.certainty not in {
            "exact",
            "approximate",
            "unknown",
        }:
            raise ValueError("Invalid local cost")
        if self.amount is not None:
            amount = Decimal(str(self.amount))
            if not amount.is_finite() or amount < 0 or not self.currency:
                raise ValueError("Invalid local cost amount")
            object.__setattr__(self, "amount", amount)


@dataclass(frozen=True)
class Offer:
    """A provider trip variant with native ratings and exact monetary amounts."""

    provider: str
    offer_id: str
    hotel_name: str | None = None
    country: str | None = None
    destination: str | None = None
    departure_airport: str | None = None
    departure_date: date | None = None
    return_date: date | None = None
    number_of_days: int | None = None
    number_of_people: int | None = None
    price_per_person: Decimal | None = None
    total_price: Decimal | None = None
    currency: str | None = None
    hotel_stars: float | None = None
    rating: float | None = None
    number_of_reviews: int | None = None
    board_type: str | None = None
    url: str | None = None
    found_at: datetime = field(default_factory=utc_now)
    last_seen: datetime = field(default_factory=utc_now)
    hotel_ratings: dict[str, ExternalHotelRating] = field(default_factory=dict)
    external_verification_statuses: dict[str, str] = field(default_factory=dict)
    external_rating_status: str = "external rating not verified"
    google_rating: GoogleRatingData | None = None
    final_score: float | None = None
    provider_rating_max: float | None = None
    google_rating_max: float | None = None
    price_is_complete: bool = True
    price_notes: str | None = None
    variant_identity: str | None = None
    package_price: Decimal | None = None
    operator_mandatory_fees: list[OperatorFee] = field(default_factory=list)
    booking_total_price: Decimal | None = None
    local_mandatory_costs: list[LocalMandatoryCost] = field(default_factory=list)
    sale_status: str | None = None
    variant_verified: bool = False
    price_verification_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.offer_id:
            raise ValueError("Provider and offer ID are required")
        for key in ("price_per_person", "total_price", "package_price", "booking_total_price"):
            value = getattr(self, key)
            if value is not None:
                value = Decimal(str(value))
                if not value.is_finite() or value < 0:
                    raise ValueError(f"Invalid {key}")
                object.__setattr__(self, key, value)
        if (
            self.booking_total_price is not None
            and self.price_is_complete
            and (
                self.package_price is None
                or any(fee.currency != self.currency for fee in self.operator_mandatory_fees)
                or self.package_price
                + sum((fee.amount for fee in self.operator_mandatory_fees), Decimal(0))
                != self.booking_total_price
                or self.total_price != self.booking_total_price
                or self.number_of_people != 2
                or self.price_per_person != self.booking_total_price / 2
                or not self.variant_verified
                or self.sale_status != "available"
            )
        ):
            raise ValueError("Inconsistent confirmed booking price")
        if self.rating is not None and (not math.isfinite(self.rating) or self.rating < 0):
            raise ValueError("Invalid rating")
        if self.final_score is not None and (
            not math.isfinite(self.final_score) or self.final_score < 0
        ):
            raise ValueError("Invalid final score")
        for maximum in (self.provider_rating_max, self.google_rating_max):
            if maximum is not None and (not math.isfinite(maximum) or maximum <= 0):
                raise ValueError("Invalid rating scale maximum")
        for key, maximum in (("hotel_stars", 5),):
            value = getattr(self, key)
            if value is not None and not 0 <= value <= maximum:
                raise ValueError(f"Invalid {key}")
        for key in ("number_of_days", "number_of_people", "number_of_reviews"):
            value = getattr(self, key)
            minimum = 0 if key == "number_of_reviews" else 1
            if value is not None and (type(value) is not int or value < minimum):
                raise ValueError(f"Invalid {key}")
        if self.departure_date and self.return_date and self.return_date < self.departure_date:
            raise ValueError("Return date precedes departure")

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, ensure_ascii=False)


def duplicate_key(offer: Offer) -> str:
    """Conservative grouping; incomplete records remain provider-specific."""
    fields: tuple[object, ...] = (
        offer.hotel_name,
        offer.country,
        offer.destination,
        offer.departure_airport,
        offer.departure_date,
        offer.return_date,
        offer.number_of_days,
        offer.number_of_people,
        offer.board_type,
        offer.currency,
    )
    if offer.variant_identity:
        fields = (offer.provider, offer.variant_identity)
    elif any(value is None or value == "" for value in fields):
        fields = (offer.provider, offer.offer_id)
    normalized = [
        " ".join(unicodedata.normalize("NFKC", str(v)).casefold().split()) for v in fields
    ]
    return hashlib.sha256(json.dumps(normalized).encode()).hexdigest()
