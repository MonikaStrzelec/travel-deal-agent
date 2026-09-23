"""Deterministic offline examples with future travel dates."""

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from ..models import Offer
from .base import Provider


class MockProvider(Provider):
    name = "mock"

    def __init__(self, today: Callable[[], date] = date.today) -> None:
        self.today = today

    def fetch(self) -> list[Offer]:
        departure = self.today() + timedelta(days=30)
        examples = [
            ("Sunny Demo", "GR", "Crete", "LCJ", "1299", 3, 8.4),
            ("Desert Demo", "EG", "Hurghada", "WAW", "1399", 4, 8.7),
            ("Budget Demo", "EG", "Hurghada", "KTW", "999", 3, 7.5),
            ("Luxury Demo", "ES", "Mallorca", "WRO", "1799", 5, 9.0),
        ]
        return [
            Offer(
                provider=self.name,
                offer_id=f"demo-{index}-{departure.isoformat()}",
                hotel_name=hotel,
                country=country,
                destination=destination,
                departure_airport=airport,
                departure_date=departure,
                return_date=departure + timedelta(days=6),
                number_of_days=7,
                number_of_people=2,
                price_per_person=Decimal(price),
                total_price=Decimal(price) * 2,
                currency="PLN",
                hotel_stars=stars,
                rating=rating,
                number_of_reviews=150,
                board_type="all_inclusive",
                url=f"https://example.invalid/offers/{index}",
            )
            for index, (hotel, country, destination, airport, price, stars, rating) in enumerate(
                examples
            )
        ]
