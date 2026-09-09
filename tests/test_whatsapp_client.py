import json

import pytest

from wca.transport.whatsapp import WhatsAppTransport, parse_inbound

PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "441234", "phone_number_id": "PNID"},
        "contacts": [{"profile": {"name": "Ann"}, "wa_id": "447700900123"}],
        "messages": [{
            "from": "447700900123", "id": "wamid.ABC", "timestamp": "1755772800",
            "text": {"body": "colour on tuesday please"}, "type": "text",
        }],
    }}]}],
}


class StubHTTP:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"messages": [{"id": "wamid.SENT"}]}

            @staticmethod
            def raise_for_status():
                return None

        return R()


def test_parse_inbound_pulls_out_the_message():
    messages = parse_inbound(PAYLOAD)
    assert len(messages) == 1
    assert messages[0].message_id == "wamid.ABC"
    assert messages[0].thread_id == "447700900123"
    assert messages[0].text == "colour on tuesday please"
    assert messages[0].sent_at.tzinfo is not None


def test_parse_inbound_ignores_a_status_only_payload():
    statuses = {"object": "whatsapp_business_account", "entry": [{"changes": [
        {"field": "messages", "value": {"statuses": [{"id": "wamid.X", "status": "delivered"}]}}
    ]}]}
    assert parse_inbound(statuses) == []


def test_parse_inbound_ignores_a_non_text_message():
    image = json.loads(json.dumps(PAYLOAD))
    image["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": "447700900123", "id": "wamid.IMG", "timestamp": "1755772800",
        "type": "image", "image": {"id": "media123"},
    }
    assert parse_inbound(image) == []


def _with_message(message: dict) -> dict:
    body = json.loads(json.dumps(PAYLOAD))
    body["entry"][0]["changes"][0]["value"]["messages"][0] = message
    return body


def test_parse_inbound_reads_a_template_quick_reply_button():
    button = {
        "from": "447700900123", "id": "wamid.BTN", "timestamp": "1755772800",
        "type": "button", "button": {"text": "Confirm", "payload": "confirm_bkg_123"},
    }
    messages = parse_inbound(_with_message(button))
    assert len(messages) == 1
    assert messages[0].text == "Confirm"
    assert messages[0].message_id == "wamid.BTN"
    assert messages[0].thread_id == "447700900123"


def test_parse_inbound_reads_an_interactive_button_reply():
    button_reply = {
        "from": "447700900123", "id": "wamid.IBTN", "timestamp": "1755772800",
        "type": "interactive", "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "confirm", "title": "Confirm"},
        },
    }
    messages = parse_inbound(_with_message(button_reply))
    assert len(messages) == 1
    assert messages[0].text == "Confirm"


def test_parse_inbound_reads_an_interactive_list_reply():
    list_reply = {
        "from": "447700900123", "id": "wamid.LST", "timestamp": "1755772800",
        "type": "interactive", "interactive": {
            "type": "list_reply",
            "list_reply": {
                "id": "slot_a", "title": "Tuesday 2:00pm", "description": "",
            },
        },
    }
    messages = parse_inbound(_with_message(list_reply))
    assert len(messages) == 1
    assert messages[0].text == "Tuesday 2:00pm"


def test_parse_inbound_still_reads_plain_text():
    messages = parse_inbound(PAYLOAD)
    assert len(messages) == 1
    assert messages[0].text == "colour on tuesday please"


@pytest.mark.parametrize("message", [
    {"from": "447700900123", "id": "wamid.B1", "timestamp": "1755772800", "type": "button"},
    {
        "from": "447700900123", "id": "wamid.B2", "timestamp": "1755772800",
        "type": "button", "button": {},
    },
    {"from": "447700900123", "id": "wamid.I1", "timestamp": "1755772800", "type": "interactive"},
    {
        "from": "447700900123", "id": "wamid.I2", "timestamp": "1755772800",
        "type": "interactive", "interactive": {},
    },
    {
        "from": "447700900123", "id": "wamid.I3", "timestamp": "1755772800",
        "type": "interactive", "interactive": {"type": "button_reply"},
    },
    {
        "from": "447700900123", "id": "wamid.I4", "timestamp": "1755772800",
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {}},
    },
    {
        "from": "447700900123", "id": "wamid.I5", "timestamp": "1755772800",
        "type": "interactive", "interactive": {"type": "list_reply"},
    },
    {
        "from": "447700900123", "id": "wamid.I6", "timestamp": "1755772800",
        "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {}},
    },
])
def test_parse_inbound_skips_a_button_or_interactive_shape_missing_its_inner_object(message):
    assert parse_inbound(_with_message(message)) == []


def test_parse_inbound_on_a_mixed_batch_yields_only_the_real_messages_in_order():
    status = {"id": "wamid.STATUS", "status": "delivered"}
    text = {
        "from": "447700900123", "id": "wamid.TXT", "timestamp": "1755772800",
        "text": {"body": "hi"}, "type": "text",
    }
    button_reply = {
        "from": "447700900123", "id": "wamid.BR", "timestamp": "1755772900",
        "type": "interactive", "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "confirm", "title": "Confirm"},
        },
    }
    mixed = {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "statuses": [status],
            "messages": [text, button_reply],
        },
    }]}]}
    messages = parse_inbound(mixed)
    assert [m.message_id for m in messages] == ["wamid.TXT", "wamid.BR"]
    assert [m.text for m in messages] == ["hi", "Confirm"]


def test_send_text_posts_the_right_shape():
    http = StubHTTP()
    t = WhatsAppTransport(phone_number_id="PNID", access_token="TOKEN", http=http)
    t.send_text("447700900123", "hello")
    call = http.calls[0]
    assert "/PNID/messages" in call["url"]
    assert call["headers"]["Authorization"] == "Bearer TOKEN"
    assert call["json"]["messaging_product"] == "whatsapp"
    assert call["json"]["to"] == "447700900123"
    assert call["json"]["text"]["body"] == "hello"


def test_send_buttons_builds_an_interactive_payload():
    http = StubHTTP()
    t = WhatsAppTransport(phone_number_id="PNID", access_token="TOKEN", http=http)
    t.send_buttons("447700900123", "Pick a day", ["Tuesday", "Friday"])
    body = http.calls[0]["json"]
    assert body["type"] == "interactive"
    titles = [b["reply"]["title"] for b in body["interactive"]["action"]["buttons"]]
    assert titles == ["Tuesday", "Friday"]


def test_more_than_three_buttons_is_refused_before_any_request():
    http = StubHTTP()
    t = WhatsAppTransport(phone_number_id="PNID", access_token="TOKEN", http=http)
    with pytest.raises(ValueError, match="three"):
        t.send_buttons("447700900123", "Pick", ["a", "b", "c", "d"])
    assert http.calls == []


def test_send_list_builds_an_interactive_list_payload():
    http = StubHTTP()
    t = WhatsAppTransport(phone_number_id="PNID", access_token="TOKEN", http=http)
    t.send_list("447700900123", "Pick a time", ["Tuesday 2:00pm", "Thursday 10:00am"])
    body = http.calls[0]["json"]
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "447700900123"
    assert body["type"] == "interactive"
    interactive = body["interactive"]
    assert interactive["type"] == "list"
    assert interactive["body"]["text"] == "Pick a time"
    action = interactive["action"]
    assert "button" in action and action["button"]
    sections = action["sections"]
    assert len(sections) == 1
    titles = [row["title"] for row in sections[0]["rows"]]
    assert titles == ["Tuesday 2:00pm", "Thursday 10:00am"]
    # Row ids are present and unique, mirroring send_buttons' btn_{i}.
    ids = [row["id"] for row in sections[0]["rows"]]
    assert len(set(ids)) == len(ids)


def test_more_than_ten_list_rows_is_refused_before_any_request():
    http = StubHTTP()
    t = WhatsAppTransport(phone_number_id="PNID", access_token="TOKEN", http=http)
    labels = [f"row {i}" for i in range(11)]
    with pytest.raises(ValueError, match="ten"):
        t.send_list("447700900123", "Pick", labels)
    assert http.calls == []


def test_the_token_never_appears_in_the_repr():
    t = WhatsAppTransport(phone_number_id="PNID", access_token="SECRET", http=StubHTTP())
    assert "SECRET" not in repr(t)
