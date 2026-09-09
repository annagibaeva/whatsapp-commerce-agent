"""Unit coverage for wca.cli._interactive_offer and _send_reply -- the
structural decision of whether a reply goes out as a list, as buttons, or
as plain text, and the fallback that keeps a bad interactive send from
ever costing the customer their answer.

No network, no model: these two functions take plain Python values
(the same shape `Agent.tool_calls` and `_facts_still_needed` produce)
and a transport double, so they are tested directly rather than through
the full webhook pipeline -- tests/test_serve_wiring.py already covers
the end-to-end wiring.
"""

from __future__ import annotations

from wca.cli import _interactive_offer, _send_reply
from wca.transport.fake import FakeTransport


# --- _interactive_offer: slots ------------------------------------------------

def test_no_tool_calls_and_no_missing_facts_is_plain_text():
    assert _interactive_offer([], set()) is None


def test_check_availability_with_slots_offers_a_list_of_their_labels():
    result = [{"slot_id": "s1", "label": "Tuesday 2:00pm"}, {"slot_id": "s2", "label": "Thursday 10:00am"}]
    offer = _interactive_offer([("check_availability", result)], set())
    assert offer == ("list", ["Tuesday 2:00pm", "Thursday 10:00am"])


def test_check_availability_with_zero_slots_is_plain_text():
    offer = _interactive_offer([("check_availability", [])], set())
    assert offer is None


def test_check_availability_with_eleven_slots_is_plain_text():
    result = [{"slot_id": f"s{i}", "label": f"Slot {i}"} for i in range(11)]
    offer = _interactive_offer([("check_availability", result)], set())
    assert offer is None


def test_check_availability_with_exactly_ten_slots_offers_a_list():
    result = [{"slot_id": f"s{i}", "label": f"Slot {i}"} for i in range(10)]
    offer = _interactive_offer([("check_availability", result)], set())
    assert offer is not None
    assert offer[0] == "list"
    assert len(offer[1]) == 10


def test_a_committed_booking_this_turn_is_always_plain_text_even_with_slots_earlier():
    """The customer already got a real answer -- there is nothing left to
    offer a choice about, no matter what an earlier check_availability
    call in the same turn returned."""
    tool_calls = [
        ("check_availability", [{"slot_id": "s1", "label": "Tuesday 2:00pm"}]),
        ("request_booking", {"ok": True, "booking_id": "b1", "slot_id": "s1"}),
    ]
    assert _interactive_offer(tool_calls, set()) is None


def test_a_refused_booking_does_not_suppress_a_later_availability_offer():
    tool_calls = [
        ("request_booking", {"ok": False, "reason": "needs 48 hours notice"}),
        ("check_availability", [{"slot_id": "s2", "label": "Friday 9:00am"}]),
    ]
    assert _interactive_offer(tool_calls, set()) == ("list", ["Friday 9:00am"])


def test_a_successful_escalation_is_always_plain_text():
    tool_calls = [("escalate", {"ok": True, "message": "a human has been notified"})]
    assert _interactive_offer(tool_calls, set()) is None


def test_a_refused_escalation_falls_through_to_the_missing_facts_check():
    tool_calls = [("escalate", {"ok": False, "reason": "cannot reach a human"})]
    offer = _interactive_offer(tool_calls, {"customer_is_over_16"})
    assert offer == ("buttons", ["Yes", "No"])


# --- _interactive_offer: yes/no buttons --------------------------------------

def test_exactly_one_missing_conversational_fact_offers_yes_no_buttons():
    offer = _interactive_offer([], {"customer_is_over_16"})
    assert offer == ("buttons", ["Yes", "No"])


def test_two_missing_conversational_facts_is_plain_text_not_buttons():
    """A compound question ("is this your first visit, and are you over
    16?") is not a clean yes/no -- it must not become two-plus buttons
    guessed at from nothing."""
    offer = _interactive_offer([], {"is_first_colour_visit", "customer_is_over_16"})
    assert offer is None


def test_slots_take_priority_over_a_missing_fact_in_the_same_turn():
    tool_calls = [("check_availability", [{"slot_id": "s1", "label": "Tuesday 2:00pm"}])]
    offer = _interactive_offer(tool_calls, {"customer_is_over_16"})
    assert offer == ("list", ["Tuesday 2:00pm"])


# --- _send_reply: the interactive path is never a second channel ------------

def test_send_reply_with_no_offer_sends_plain_text():
    t = FakeTransport()
    _send_reply(t, "+441234", "hello", None)
    sent = t.sent()[0]
    assert sent.body == "hello"
    assert sent.list_rows == () and sent.buttons == ()


def test_send_reply_with_a_list_offer_sends_the_same_body_as_a_list():
    t = FakeTransport()
    _send_reply(t, "+441234", "Pick a time", ("list", ["Tuesday 2:00pm", "Thursday 10:00am"]))
    sent = t.sent()[0]
    assert sent.body == "Pick a time"
    assert sent.list_rows == ("Tuesday 2:00pm", "Thursday 10:00am")


def test_send_reply_with_a_buttons_offer_sends_the_same_body_as_buttons():
    t = FakeTransport()
    _send_reply(t, "+441234", "Are you over 16?", ("buttons", ["Yes", "No"]))
    sent = t.sent()[0]
    assert sent.body == "Are you over 16?"
    assert sent.buttons == ("Yes", "No")


def test_a_failed_interactive_send_falls_back_to_plain_text_not_a_raise():
    class _AlwaysFailsList:
        def send_text(self, to, body):
            self.sent = ("text", body)

        def send_list(self, to, body, labels):
            raise ValueError("WhatsApp allows at most ten list rows, got 11")

        def send_buttons(self, to, body, labels):
            raise AssertionError("not exercised by this test")

    t = _AlwaysFailsList()
    _send_reply(t, "+441234", "too many options", ("list", [f"row {i}" for i in range(11)]))

    assert t.sent == ("text", "too many options")


def test_a_transport_level_failure_on_buttons_also_falls_back_to_text():
    class _AlwaysFailsButtons:
        def send_text(self, to, body):
            self.sent = ("text", body)

        def send_buttons(self, to, body, labels):
            raise RuntimeError("simulated network failure")

    t = _AlwaysFailsButtons()
    _send_reply(t, "+441234", "Are you over 16?", ("buttons", ["Yes", "No"]))

    assert t.sent == ("text", "Are you over 16?")
