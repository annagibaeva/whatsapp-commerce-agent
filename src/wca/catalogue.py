"""The service catalogue.

Two facts the rules read, `service_category` and `quoted_price_minor`,
used to arrive from the model with no source behind them. The model
could say anything. This module gives both facts a source it cannot
invent: a service looked up from a file on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Service(_Strict):
    id: str
    name: str
    category: str
    duration_minutes: int = Field(gt=0)
    price_minor: int = Field(ge=0)


class Catalogue(_Strict):
    services: tuple[Service, ...]

    @model_validator(mode="after")
    def _no_duplicate_ids(self) -> Catalogue:
        seen: set[str] = set()
        for service in self.services:
            if service.id in seen:
                raise ValueError(f"duplicate service id {service.id!r}")
            seen.add(service.id)
        return self

    def get(self, service_id: str) -> Service | None:
        for service in self.services:
            if service.id == service_id:
                return service
        return None

    def search(self, query: str) -> tuple[Service, ...]:
        """Case-insensitive substring match over name and category.

        No fuzzy matching. A query that matches nothing returns an empty
        tuple rather than guessing at what the customer meant — the same
        posture the rest of this codebase takes about not guessing.
        """
        needle = query.lower()
        return tuple(
            service for service in self.services
            if needle in service.name.lower() or needle in service.category.lower()
        )


def load_catalogue(path: str | Path) -> Catalogue:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Catalogue.model_validate(raw)


def facts_for(service: Service) -> dict[str, Any]:
    """The one place a service turns into the rule facts that read it."""
    return {
        "service_category": service.category,
        "quoted_price_minor": service.price_minor,
    }
