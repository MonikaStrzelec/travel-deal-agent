"""Display native and optional external ratings without changing eligibility."""

from .config_types import RankingConfig
from .models import Offer
from .ratings import normalize_rating


def format_ratings(offer: Offer, ranking: RankingConfig) -> str:
    """Show the provider scale alongside optional, verified external ratings."""
    rule = ranking["provider_ratings"].get(offer.provider)
    scale = rule["scale"] if rule else None
    native = "brak danych"
    if (
        offer.rating is not None
        and scale is not None
        and normalize_rating(offer.rating, scale) is not None
    ):
        native = f"{offer.rating:g}/{scale['max']:g}"
    parts = [f"{offer.provider.upper()}: {native}"]
    sources = dict.fromkeys(
        ["google", "tripadvisor", *ranking.get("external_sources", {}), *offer.hotel_ratings]
    )
    for source in sources:
        result_value = offer.hotel_ratings.get(source)
        value = "brak danych"
        if (
            result_value is not None
            and offer.external_verification_statuses.get(source) == "verified"
        ):
            value = f"{result_value.rating:g}/{result_value.scale_max:g}"
        label = {"google": "Google", "tripadvisor": "Tripadvisor"}.get(source, source)
        parts.append(f"{label}: {value}")
    return " | ".join(parts)
