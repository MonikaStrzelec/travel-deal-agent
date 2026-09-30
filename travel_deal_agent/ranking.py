"""Weighted ranking and conservative cross-provider result grouping."""

from math import log1p

from .boards import normalize_board
from .config_types import RankingConfig
from .models import Offer, duplicate_key
from .ratings import normalize_rating


def meal_score(offer: Offer, ranking: RankingConfig) -> float:
    """Reward meal quality independently of the price-dependent eligibility minimum."""
    return ranking["board_scores"].get(normalize_board(offer.provider, offer.board_type) or "", 0)


def score(offer: Offer, ranking: RankingConfig, max_price: str) -> float:
    """Weight native provider signals plus, if verified, each external source.

    A verified external source's contribution (`external_score` below) is
    computed directly from `Offer.hotel_ratings`/`external_verification_statuses`
    -- the single, source-generic mechanism (see `providers/external_rating.py`)
    -- via each source's own `ranking.external_sources[source]` weights, never
    a single hardcoded "google" component.
    """
    airports = ranking["airport_priority"]
    rule = ranking["provider_ratings"].get(offer.provider)
    normalized = normalize_rating(offer.rating, rule["scale"] if rule else None)
    airport = (
        (len(airports) - airports.index(offer.departure_airport)) / len(airports)
        if offer.departure_airport is not None and offer.departure_airport in airports
        else 0
    )
    components = {
        "board": meal_score(offer, ranking),
        "price": max(0, 1 - float(offer.price_per_person) / float(max_price))
        if offer.price_per_person is not None
        else 0,
        "airport": airport,
        "rating": (normalized or 0) / 100,
        # Superseded by the per-source `external_score` below (see the
        # docstring); kept at zero rather than removed so a `ranking.weights`
        # config still declaring these two keys stays valid and inert.
        "google_rating": 0.0,
        "google_reviews": 0.0,
        "reviews": min(1, log1p(offer.number_of_reviews or 0) / log1p(ranking["review_count_cap"])),
        "stars": (offer.hotel_stars or 0) / ranking["star_scale_max"],
    }
    if "airport_groups" in ranking:
        groups = ranking["airport_groups"]
        components["airport"] = next(
            (
                (len(groups) - index) / len(groups)
                for index, group in enumerate(groups)
                if offer.departure_airport in group
            ),
            0.0,
        )
    external_score = 0.0
    for source, result in offer.hotel_ratings.items():
        policy = ranking.get("external_sources", {}).get(source)
        if (
            policy is None
            or not policy["enabled"]
            or offer.external_verification_statuses.get(source) != "verified"
        ):
            continue
        value = (result.rating - result.scale_min) / (result.scale_max - result.scale_min)
        reviews = min(1, log1p(result.number_of_reviews or 0) / log1p(ranking["review_count_cap"]))
        external_score += policy["rating_weight"] * value + policy["reviews_weight"] * reviews
    return external_score + sum(
        ranking["weights"].get(key, 0) * value for key, value in components.items()
    )


def rank_offers(offers: list[Offer], ranking: RankingConfig, max_price: str) -> list[Offer]:
    return sorted(offers, key=lambda o: (-score(o, ranking, max_price), o.provider, o.offer_id))


def deduplicate(offers: list[Offer]) -> list[Offer]:
    groups: dict[str, Offer] = {}
    for offer in offers:
        key = duplicate_key(offer)
        current = groups.get(key)
        if current is None or (
            offer.price_per_person is not None
            and (
                current.price_per_person is None
                or offer.price_per_person < current.price_per_person
            )
        ):
            groups[key] = offer
    return list(groups.values())
