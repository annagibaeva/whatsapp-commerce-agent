"""The 24-hour window, and whether a human could still reply.

A customer message opens a 24-hour window. Inside it the salon can reply
freely. Outside it, only an approved template can be sent. An escalation
raised at hour 23 produces a human reply that never arrives.

Meta does not tell us whether a send will actually work. This check
guesses using two things we can see: time left, and whether a template
for this reason is approved. It leaves out per-user frequency caps,
because nobody outside Meta knows how they behave and inventing them
would make this check look careful while asserting things we made up.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from wca.clock import hours_between

WINDOW_HOURS = 24
ESCALATION_MARGIN_HOURS = 2


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: str
    name: str
    approved: bool


class TemplateRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    templates: tuple[Template, ...] = ()
    #: Hard-coded in v0. Present so switching it on later is a config
    #: change, not a rewrite.
    quality_rating: str = "green"

    def find(self, reason: str) -> Template | None:
        for template in self.templates:
            if template.reason == reason:
                return template
        return None


def window_closes_at(last_inbound_at: datetime) -> datetime:
    return last_inbound_at + timedelta(hours=WINDOW_HOURS)


def window_view(
    last_inbound_at: datetime,
    now: datetime,
    reason: str,
    registry: TemplateRegistry,
) -> dict[str, Any]:
    """A read-only snapshot for the gate. Never mutates anything."""
    closes = window_closes_at(last_inbound_at)
    hours_left = round(hours_between(now, closes), 3)
    template = registry.find(reason)

    if template is None:
        why_not = f"no template registered for {reason}"
    elif not template.approved:
        why_not = f"template {template.name} is not approved"
    elif hours_left < ESCALATION_MARGIN_HOURS:
        why_not = (
            f"{hours_left}h left, margin is {ESCALATION_MARGIN_HOURS}h"
        )
    else:
        why_not = ""

    return {
        "window_closes_at": closes.isoformat(),
        "hours_left": hours_left,
        "template_name": template.name if template else None,
        "template_approved": bool(template and template.approved),
        "quality_rating": registry.quality_rating,
        "quality_rating_consulted": False,
        "deliverable": why_not == "",
        "why_not": why_not,
    }
