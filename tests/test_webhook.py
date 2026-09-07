import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from wca.transport.webhook import WebhookSettings, create_app, verify_signature

SECRET = "app-secret-value"
VERIFY_TOKEN = "verify-me"
SETTINGS = WebhookSettings(app_secret=SECRET, verify_token=VERIFY_TOKEN)

PAYLOAD = {"object": "whatsapp_business_account", "entry": [{"changes": [
    {"field": "messages", "value": {"messages": [{
        "from": "447700900123", "id": "wamid.ABC", "timestamp": "1755772800",
        "type": "text", "text": {"body": "hello"},
    }]}}
]}]}


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _client(seen: list):
    return TestClient(create_app(SETTINGS, on_message=lambda m: seen.append(m)))


def test_the_handshake_echoes_the_challenge_when_the_token_matches():
    r = _client([]).get("/webhook", params={
        "hub.mode": "subscribe", "hub.challenge": "12345", "hub.verify_token": VERIFY_TOKEN
    })
    assert r.status_code == 200
    assert r.text == "12345"


def test_the_handshake_refuses_a_wrong_token():
    r = _client([]).get("/webhook", params={
        "hub.mode": "subscribe", "hub.challenge": "12345", "hub.verify_token": "wrong"
    })
    assert r.status_code == 403


def test_a_correctly_signed_message_is_accepted_and_delivered():
    seen = []
    body = json.dumps(PAYLOAD).encode()
    r = _client(seen).post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert [m.message_id for m in seen] == ["wamid.ABC"]


def test_a_wrongly_signed_message_is_rejected_and_never_delivered():
    seen = []
    body = json.dumps(PAYLOAD).encode()
    r = _client(seen).post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": _sign(body, "wrong-secret")},
    )
    assert r.status_code == 403
    assert seen == []


def test_a_message_with_no_signature_at_all_is_rejected():
    seen = []
    body = json.dumps(PAYLOAD).encode()
    r = _client(seen).post("/webhook", content=body)
    assert r.status_code == 403
    assert seen == []


def test_the_signature_is_computed_over_the_raw_body_not_the_reparsed_json():
    # Same JSON, different bytes. Signing the reserialised form would
    # reject this, and every real message with it.
    seen = []
    body = b'{"object":"whatsapp_business_account",  "entry":[{"changes":[{"field":"messages","value":{"messages":[{"from":"447700900123","id":"wamid.SPACED","timestamp":"1755772800","type":"text","text":{"body":"hi"}}]}}]}]}'
    r = _client(seen).post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert [m.message_id for m in seen] == ["wamid.SPACED"]


def test_verify_signature_uses_a_constant_time_comparison():
    import inspect

    from wca.transport import webhook

    assert "compare_digest" in inspect.getsource(webhook.verify_signature)


def test_a_status_only_callback_is_accepted_but_delivers_nothing():
    seen = []
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [
        {"field": "messages", "value": {"statuses": [{"id": "x", "status": "sent"}]}}
    ]}]}).encode()
    r = _client(seen).post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert seen == []


def test_the_app_secret_never_appears_in_the_repr():
    # pydantic's default repr prints every field in full, and reprs end
    # up in logs. WhatsAppTransport already guards its repr; this model
    # did not.
    assert SECRET not in repr(SETTINGS)
    assert VERIFY_TOKEN not in repr(SETTINGS)
