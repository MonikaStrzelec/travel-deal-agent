"""Implementations must return normalized offers and use bounded network timeouts."""

from abc import ABC, abstractmethod

from ..models import Offer


class Provider(ABC):
    name: str

    @abstractmethod
    def fetch(self) -> list[Offer]:
        """Fetch one batch without bypassing access controls or CAPTCHAs."""
