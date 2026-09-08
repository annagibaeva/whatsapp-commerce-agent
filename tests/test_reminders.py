"""The n8n reminder hop: `GET /reminders/due` and `POST /reminders/sent`.

n8n polls us on a schedule and sends the 24-hour-ahead confirmation
itself. We are only ever the source of truth about what is due, so the
property worth testing is not the mechanics of either route but the one
they exist to guarantee together:

    a booking that was never marked keeps appearing until it is,
    and one already marked never appears again.

That is what survives a missed poll and a double poll, which are the two
things a cloud scheduler on a public URL will eventually do.
"""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from wca.calendar.mock import MockCalendar, Slot
from wca.catalogue import load_catalogue
from wca.cli import reminder_secret_from_env
from wca.clock import utc
from wca.transport.webhook import (
    REMINDER_SECRET_HEADER,
    WebhookSettings,
    create_app,
    verify_reminder_secret,
)

SECRET = "n8n-shared-secret"
APP_SECRET = "app-secret-value"
VERIFY_TOKEN = "verify-me"

NOW = utc(2026, 8, 24, 14)
DUE_SLOT = "s_2026_08_25_1400"          # 24 hours out -- inside the window
FAR_SLOT = "s_2026_08_28_1400"          # 96 hours out -- outside it
SERVICE = "svc_colour_full"

CATALOGUE = load_catalogue("policy/salon.catalogue.json")


def _calendar() -> MockCalendar:
    return MockCalendar(slots=[
        Slot(DUE_SLOT, utc(2026, 8, 25, 14)),
        Slot(FAR_SLOT, utc(2026, 8, 28, 14)),
    ])


def _book(cal: MockCalendar, slot_id: str, thread_id: str, service_id: str = SERVICE):
    hold = cal.hold(slot_id, thread_id=thread_id, now=NOW)
    return cal.commit(
        hold.hold_id, idempotency_key=f"key-{slot_id}", now=NOW, service_id=service_id
    )


def _settings(reminder_secret: str | None = SECRET) -> WebhookSettings:
    return WebhookSettings(
        app_secret=APP_SECRET, verify_token=VERIFY_TOKEN, reminder_secret=reminder_secret
    )


def _client(cal: MockCalendar, reminder_secret: str | None = SECRET) -> TestClient:
    return TestClient(create_app(
        _settings(reminder_secret),
        on_message=lambda m: None,
        calendar=cal,
        catalogue=CATALOGUE,
        now=lambda: NOW,
    ))


def _auth() -> dict[str, str]:
    return {REMINDER_SECRET_HEADER: SECRET}


def _advancing_client(cal: MockCalendar, step_hours: float = 1.0) -> TestClient:
    """A client whose clock moves forward on every read.

    A fixed clock cannot test idempotency. With `now` pinned to NOW, a
    `mark_reminded` that overwrites on every call writes the identical
    timestamp the guarded one does, and "the timestamp did not move"
    holds for both. Advancing the clock is what makes the second write
    observable, so the assertion has something to catch.
    """
    state = {"t": NOW}

    def read():
        current = state["t"]
        state["t"] = current + timedelta(hours=step_hours)
        return current

    return TestClient(create_app(
        _settings(), on_message=lambda m: None,
        calendar=cal, catalogue=CATALOGUE, now=read,
    ))


# --- what is due -------------------------------------------------------

def test_a_booking_inside_the_window_is_due_and_one_outside_it_is_not():
    cal = _calendar()
    near = _book(cal, DUE_SLOT, "447700900123")
    _book(cal, FAR_SLOT, "447700900456")

    body = _client(cal).get("/reminders/due", headers=_auth()).json()

    assert [b["booking_id"] for b in body] == [near.booking_id]


def test_a_due_booking_carries_what_n8n_needs_to_fill_the_template():
    cal = _calendar()
    _book(cal, DUE_SLOT, "447700900123")

    entry = _client(cal).get("/reminders/due", headers=_auth()).json()[0]

    assert entry["customer_phone"] == "447700900123"
    assert entry["service_name"] == "full head colour"
    assert entry["starts_at"] == "2026-08-25T14:00:00+00:00"
    # The same label check_availability already produces, so n8n never
    # reformats a time and never drifts from what the agent said.
    assert entry["starts_at_label"] == "Tuesday 25 August, 2:00pm"


def test_the_window_is_capped_so_a_stray_query_param_cannot_return_everything():
    cal = _calendar()
    _book(cal, DUE_SLOT, "447700900123")
    _book(cal, FAR_SLOT, "447700900456")

    huge = _client(cal).get(
        "/reminders/due", params={"within_hours": 100000}, headers=_auth()
    ).json()

    # 168h caps it: the 96-hour booking is in reach, but the cap is what
    # decides that, not the caller.
    assert len(huge) == 2


# --- the loop property -------------------------------------------------

def test_a_marked_booking_stops_appearing():
    cal = _calendar()
    booking = _book(cal, DUE_SLOT, "447700900123")
    client = _client(cal)

    assert len(client.get("/reminders/due", headers=_auth()).json()) == 1

    sent = client.post(
        "/reminders/sent", json={"booking_id": booking.booking_id}, headers=_auth()
    )
    assert sent.status_code == 200

    assert client.get("/reminders/due", headers=_auth()).json() == []


def test_an_unmarked_booking_keeps_appearing_across_polls():
    """A missed send must not be lost. Polling twice without marking in
    between returns it both times."""
    cal = _calendar()
    booking = _book(cal, DUE_SLOT, "447700900123")
    client = _client(cal)

    first = client.get("/reminders/due", headers=_auth()).json()
    second = client.get("/reminders/due", headers=_auth()).json()

    assert [b["booking_id"] for b in first] == [booking.booking_id]
    assert [b["booking_id"] for b in second] == [booking.booking_id]


def test_marking_twice_succeeds_and_does_not_move_the_timestamp():
    """n8n retries. A redelivered `sent` must be a success, not a 4xx,
    and must not rewrite when the reminder actually went out."""
    cal = _calendar()
    booking = _book(cal, DUE_SLOT, "447700900123")
    # Advancing, not fixed: the second call reads a later time, so an
    # unguarded overwrite would show up as a moved timestamp.
    client = _advancing_client(cal)

    first = client.post(
        "/reminders/sent", json={"booking_id": booking.booking_id}, headers=_auth()
    )
    second = client.post(
        "/reminders/sent", json={"booking_id": booking.booking_id}, headers=_auth()
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["reminder_sent_at"] == second.json()["reminder_sent_at"]
    assert cal._bookings[booking.booking_id].reminder_sent_at == NOW


def test_marking_an_unknown_booking_is_a_404_not_a_silent_success():
    cal = _calendar()
    _book(cal, DUE_SLOT, "447700900123")

    r = _client(cal).post(
        "/reminders/sent", json={"booking_id": "bkg_does_not_exist"}, headers=_auth()
    )

    assert r.status_code == 404


@pytest.mark.parametrize("body", [b"not json at all", b'"a string"', b"{}"])
def test_a_malformed_sent_body_is_refused(body: bytes):
    cal = _calendar()
    r = _client(cal).post("/reminders/sent", content=body, headers=_auth())
    assert r.status_code == 400


# --- auth --------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    pytest.param({}, id="header-missing"),
    pytest.param({REMINDER_SECRET_HEADER: ""}, id="header-empty"),
    pytest.param({REMINDER_SECRET_HEADER: "wrong-secret"}, id="header-wrong"),
    pytest.param({REMINDER_SECRET_HEADER: SECRET + "x"}, id="header-prefix-of-real"),
])
def test_both_endpoints_refuse_a_bad_secret(headers: dict[str, str]):
    cal = _calendar()
    booking = _book(cal, DUE_SLOT, "447700900123")
    client = _client(cal)

    assert client.get("/reminders/due", headers=headers).status_code == 401
    assert client.post(
        "/reminders/sent", json={"booking_id": booking.booking_id}, headers=headers
    ).status_code == 401


def test_with_no_secret_configured_every_request_is_refused():
    """A missing secret is closed, not open. The failure mode of
    forgetting to set N8N_REMINDER_SECRET must not be a public endpoint
    that lists customer phone numbers."""
    cal = _calendar()
    booking = _book(cal, DUE_SLOT, "447700900123")
    client = _client(cal, reminder_secret=None)

    for headers in ({}, {REMINDER_SECRET_HEADER: SECRET}, {REMINDER_SECRET_HEADER: ""}):
        assert client.get("/reminders/due", headers=headers).status_code == 401
        assert client.post(
            "/reminders/sent", json={"booking_id": booking.booking_id}, headers=headers
        ).status_code == 401


def test_an_unauthenticated_call_cannot_mark_a_booking():
    """401 has to mean nothing happened, not 'refused after writing'."""
    cal = _calendar()
    booking = _book(cal, DUE_SLOT, "447700900123")
    client = _client(cal)

    client.post(
        "/reminders/sent",
        json={"booking_id": booking.booking_id},
        headers={REMINDER_SECRET_HEADER: "wrong"},
    )

    assert cal._bookings[booking.booking_id].reminder_sent_at is None


def test_verify_reminder_secret_refuses_the_empty_pair():
    """compare_digest("", "") is True. Reaching it with no secret and no
    header would turn 'nothing sent' into 'authenticated'."""
    assert verify_reminder_secret(None, "") is False
    assert verify_reminder_secret(None, None) is False


def test_an_unset_or_empty_env_var_both_read_as_no_secret(monkeypatch):
    """Asserted through the environment rather than around it: an earlier
    credential test in this repo passed only when no credential existed,
    because .env quietly repopulated it."""
    monkeypatch.delenv("N8N_REMINDER_SECRET", raising=False)
    assert reminder_secret_from_env() is None

    monkeypatch.setenv("N8N_REMINDER_SECRET", "")
    assert reminder_secret_from_env() is None

    monkeypatch.setenv("N8N_REMINDER_SECRET", "a-real-secret")
    assert reminder_secret_from_env() == "a-real-secret"


# --- the read is a read ------------------------------------------------

def test_polling_due_mutates_nothing():
    cal = _calendar()
    booking = _book(cal, DUE_SLOT, "447700900123")
    client = _client(cal)

    before = (
        len(cal._bookings),
        len(cal._holds),
        cal._bookings[booking.booking_id].reminder_sent_at,
        cal.view(DUE_SLOT, NOW),
        cal.view(FAR_SLOT, NOW),
    )

    client.get("/reminders/due", headers=_auth())
    client.get("/reminders/due", params={"within_hours": 168}, headers=_auth())

    after = (
        len(cal._bookings),
        len(cal._holds),
        cal._bookings[booking.booking_id].reminder_sent_at,
        cal.view(DUE_SLOT, NOW),
        cal.view(FAR_SLOT, NOW),
    )
    assert before == after


def test_the_reminder_routes_do_not_disturb_the_whatsapp_webhook():
    """create_app grew two routes and three optional arguments. The
    handshake it already had has to answer exactly as before."""
    cal = _calendar()
    r = _client(cal).get("/webhook", params={
        "hub.mode": "subscribe", "hub.challenge": "12345", "hub.verify_token": VERIFY_TOKEN
    })
    assert r.status_code == 200
    assert r.text == "12345"


def test_a_webhook_only_app_still_builds_without_a_calendar():
    """Nothing that only wants the WhatsApp webhook should have to know
    the reminder hop exists."""
    app = create_app(_settings(), on_message=lambda m: None)
    client = TestClient(app)

    r = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.challenge": "9", "hub.verify_token": VERIFY_TOKEN
    })
    assert r.status_code == 200
    # Authenticated, but there is no calendar behind it: 500, not a crash
    # and not an empty list that would read as "nothing due".
    assert client.get("/reminders/due", headers=_auth()).status_code == 500
