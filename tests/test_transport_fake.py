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


def test_list_rows_are_recorded_with_their_titles():
    t = FakeTransport()
    t.send_list("+441234", "Pick a time", ["Tuesday 2:00pm", "Thursday 10:00am"])
    sent = t.sent()[0]
    assert sent.list_rows == ("Tuesday 2:00pm", "Thursday 10:00am")


def test_more_than_ten_list_rows_is_refused():
    import pytest

    t = FakeTransport()
    labels = [f"row {i}" for i in range(11)]
    with pytest.raises(ValueError, match="ten"):
        t.send_list("+441234", "Pick", labels)


def test_exactly_ten_list_rows_is_accepted():
    t = FakeTransport()
    labels = [f"row {i}" for i in range(10)]
    sent = t.send_list("+441234", "Pick", labels)
    assert len(sent.list_rows) == 10


def test_received_messages_come_back_in_order():
    t = FakeTransport()
    t.receive(InboundMessage(
        message_id="m1", thread_id="+441234", text="hello", sent_at=utc(2026, 8, 21, 9)
    ))
    assert [m.message_id for m in t.inbox()] == ["m1"]
