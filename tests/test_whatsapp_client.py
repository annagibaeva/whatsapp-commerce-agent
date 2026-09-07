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


def test_the_token_never_appears_in_the_repr():
    t = WhatsAppTransport(phone_number_id="PNID", access_token="SECRET", http=StubHTTP())
    assert "SECRET" not in repr(t)
