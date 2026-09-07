from wca.clock import utc
from wca.transport.fake import FakeTransport
from wca.transport.base import InboundMessage


def test_sent_messages_are_recorded_in_order():
    t = FakeTransport()
    t.send_text("+441234", "first")
    t.send_text("+441234", "second")
    assert [m.body for m in t.sent()] == ["first", "second"]


def test_buttons_are_recorded_with_their_labels():
    t = FakeTransport()
    t.send_buttons("+441234", "Pick one", ["Tuesday", "Friday"])
    sent = t.sent()[0]
    assert sent.buttons == ("Tuesday", "Friday")


def test_more_than_three_buttons_is_refused():
    import pytest

    t = FakeTransport()
    with pytest.raises(ValueError, match="three"):
        t.send_buttons("+441234", "Pick", ["a", "b", "c", "d"])


def test_received_messages_come_back_in_order():
    t = FakeTransport()
    t.receive(InboundMessage(
        message_id="m1", thread_id="+441234", text="hello", sent_at=utc(2026, 8, 21, 9)
    ))
    assert [m.message_id for m in t.inbox()] == ["m1"]
