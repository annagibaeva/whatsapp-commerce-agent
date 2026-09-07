from datetime import timedelta

from wca.clock import utc
from wca.escalation import (
    ESCALATION_MARGIN_HOURS,
    WINDOW_HOURS,
    Template,
    TemplateRegistry,
    window_closes_at,
    window_view,
)

LAST_INBOUND = utc(2026, 8, 21, 9)
REGISTRY = TemplateRegistry(templates=(
    Template(reason="patch_test_required", name="patch_test_notice", approved=True),
    Template(reason="under_16", name="guardian_notice", approved=False),
))


def test_the_window_closes_24_hours_after_the_last_inbound_message():
    assert window_closes_at(LAST_INBOUND) == LAST_INBOUND + timedelta(hours=WINDOW_HOURS)


def test_early_in_the_window_an_escalation_is_deliverable():
    view = window_view(LAST_INBOUND, utc(2026, 8, 21, 11), "patch_test_required", REGISTRY)
    assert view["deliverable"] is True
    assert view["hours_left"] == 22.0
    assert view["template_approved"] is True


def test_at_hour_23_it_is_not_deliverable():
    view = window_view(LAST_INBOUND, utc(2026, 8, 22, 8), "patch_test_required", REGISTRY)
    assert view["deliverable"] is False
    assert view["hours_left"] == 1.0
    assert "margin" in view["why_not"]


def test_the_boundary_is_exactly_the_margin():
    at_margin = LAST_INBOUND + timedelta(hours=WINDOW_HOURS - ESCALATION_MARGIN_HOURS)
    assert window_view(LAST_INBOUND, at_margin, "patch_test_required", REGISTRY)["deliverable"] is True
    just_after = at_margin + timedelta(minutes=1)
    assert window_view(LAST_INBOUND, just_after, "patch_test_required", REGISTRY)["deliverable"] is False


def test_an_unapproved_template_is_not_deliverable_however_much_time_is_left():
    view = window_view(LAST_INBOUND, utc(2026, 8, 21, 10), "under_16", REGISTRY)
    assert view["deliverable"] is False
    assert view["hours_left"] == 23.0
    assert "approved" in view["why_not"]


def test_an_unknown_reason_has_no_template_and_is_not_deliverable():
    view = window_view(LAST_INBOUND, utc(2026, 8, 21, 10), "never_registered", REGISTRY)
    assert view["deliverable"] is False
    assert "no template" in view["why_not"]


def test_quality_rating_is_recorded_but_not_used_in_v0():
    view = window_view(LAST_INBOUND, utc(2026, 8, 21, 10), "patch_test_required", REGISTRY)
    assert view["quality_rating"] == "green"
    assert view["quality_rating_consulted"] is False
