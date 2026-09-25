"""Strict validation primitives for untrusted source payloads, shared by all providers."""

from pydantic import BaseModel, ConfigDict


class Boundary(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("Expected a JSON object")
    return {str(k): v for k, v in value.items()}
