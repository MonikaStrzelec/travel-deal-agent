"""Google Places API (New) Text Search: minimal fields, no scraping, no browser.

Only the official `https://places.googleapis.com/v1/places:searchText`
endpoint is used, with the smallest `X-Goog-FieldMask` that still lets
callers identify a place, read its rating/review count and make a
conservative country check -- no photos, review text or opening hours.

The API key is read from `GOOGLE_PLACES_API_KEY` by the caller (see
`providers.external_rating.GoogleRatingProvider`) and is passed in here only
as a header value; this module never logs it and never includes it in an
exception message.
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

API_KEY_ENV = "GOOGLE_PLACES_API_KEY"

# Only what `GoogleRatingProvider` needs: an identity (id, name), the two
# rating figures, and an address string for a conservative country check.
# Deliberately excludes photos, reviews, opening hours and anything else
# billed as part of the Places API (New) "Enterprise + Atmosphere" tiers.
FIELD_MASK = (
    "places.id,places.displayName,places.rating,places.userRatingCount,places.formattedAddress"
)

# English names for the fixed set of countries this project's providers
# observe (see `notification_content.COUNTRY_NAMES_PL` for the same set in
# Polish) -- used to build the Google query text and to conservatively read
# back the country from Google's own `formattedAddress`.
COUNTRY_NAMES_EN = {
    "TR": "Turkey",
    "GR": "Greece",
    "TN": "Tunisia",
    "AL": "Albania",
    "EG": "Egypt",
    "ES": "Spain",
    "BG": "Bulgaria",
    "CY": "Cyprus",
    "IT": "Italy",
    "PT": "Portugal",
    "MT": "Malta",
}
_COUNTRY_CODES_BY_NAME = {name.casefold(): code for code, name in COUNTRY_NAMES_EN.items()}


class GooglePlacesError(Exception):
    """A Google Places request failed; the message never includes the API key."""


@dataclass(frozen=True)
class GooglePlace:
    """One Text Search candidate, holding only the fields this project reads."""

    place_id: str
    display_name: str
    rating: float | None
    user_rating_count: int | None
    formatted_address: str | None


def country_from_address(formatted_address: str | None) -> str | None:
    """The ISO alpha-2 code for the address's trailing country name, if recognized.

    Deliberately simple (see module docstring): only the fixed
    `COUNTRY_NAMES_EN` list is recognized; an unrecognized or missing address
    resolves to `None` rather than guessing.
    """
    if not formatted_address:
        return None
    last_part = formatted_address.rsplit(",", 1)[-1].strip()
    return _COUNTRY_CODES_BY_NAME.get(last_part.casefold())


class GooglePlacesTransport(Protocol):
    def search_text(
        self, query: str, api_key: str, field_mask: str, timeout: float
    ) -> list[GooglePlace]:
        """Return Text Search candidates in Google's own relevance order."""
        ...


class UrllibGooglePlacesTransport:
    """Minimal stdlib POST client for the Places API (New) Text Search call."""

    def search_text(
        self, query: str, api_key: str, field_mask: str, timeout: float
    ) -> list[GooglePlace]:
        body = json.dumps({"textQuery": query}).encode("utf-8")
        request = Request(
            PLACES_SEARCH_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": field_mask,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            # Never read or surface the error body: Google may echo request
            # details back in it, and the caller only needs the status code.
            exc.close()
            raise GooglePlacesError(f"Google Places API returned HTTP {exc.code}") from None
        except (URLError, TimeoutError) as exc:
            raise GooglePlacesError(f"Google Places request failed: {exc}") from None
        try:
            parsed = json.loads(payload)
        except ValueError:
            raise GooglePlacesError("Google Places API returned an unreadable response") from None
        return [
            GooglePlace(
                place_id=place["id"],
                display_name=place["displayName"]["text"],
                rating=place.get("rating"),
                user_rating_count=place.get("userRatingCount"),
                formatted_address=place.get("formattedAddress"),
            )
            for place in parsed.get("places", [])
            if place.get("id") and (place.get("displayName") or {}).get("text")
        ]
