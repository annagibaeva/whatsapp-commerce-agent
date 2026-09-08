# Live thread runbook

Steps to run one real WhatsApp conversation through this codebase: receive a message, print it, send one back, and confirm a forged webhook is rejected.

This runbook assumes a working WhatsApp test number, a permanent System User access token, and an app secret, already set up in Meta's console. It does not cover creating those.

## 1. `.env`

Create a `.env` file in the project root with five values:

```
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_APP_SECRET=...
WHATSAPP_WEBHOOK_VERIFY_TOKEN=...
ANTHROPIC_API_KEY=...
```

`WHATSAPP_ACCESS_TOKEN` should be the permanent System User token, not the 24-hour token shown on Meta's API Setup page.

Save the file as UTF-8 **without a byte-order mark**. Several editors on Windows save plain text files with a BOM by default. If the file has one, it attaches itself to the first key name in the file. The line that reads `WHATSAPP_PHONE_NUMBER_ID=...` on screen is actually stored as `﻿WHATSAPP_PHONE_NUMBER_ID=...`, and `os.environ.get("WHATSAPP_PHONE_NUMBER_ID")` then returns `None`, even though the file plainly sets it. If the first value in `.env` behaves as if it were never set while the others work, this is the first thing to check.

## 2. Start a tunnel

```bash
cloudflared tunnel --url http://localhost:8000
```

Note the `https://...trycloudflare.com` URL it prints. It is needed in step 4.

## 3. Start the receiver

```bash
uv run wca serve
```

This reads `WHATSAPP_APP_SECRET` and `WHATSAPP_WEBHOOK_VERIFY_TOKEN` from `.env` and starts the webhook receiver on port 8000.

## 4. Configure the webhook in Meta's console

In Meta's console: **WhatsApp → Configuration → Webhooks → Edit**.

- Paste the tunnel URL from step 2 with `/webhook` appended, for example `https://your-tunnel.trycloudflare.com/webhook`.
- Paste the verify token, the same value as `WHATSAPP_WEBHOOK_VERIFY_TOKEN` in `.env`.
- Save.

**Then subscribe to the `messages` field.** This is a separate click from Save, in the field subscription list below the webhook URL. It is easy to save the URL and stop there; if no messages arrive at the receiver despite a saved, verified webhook, this is the step to check first.

## 5. Message the test number

Send a WhatsApp message to the test number from a phone. The receiver process prints the thread ID and the text of the message it received.

## 6. Send one back

```bash
uv run wca send --to <your number, digits only, full international form> --text "hello"
```

This reads `WHATSAPP_PHONE_NUMBER_ID` and `WHATSAPP_ACCESS_TOKEN` from `.env` and sends one text message through the Cloud API.

## 7. Confirm a forged webhook is rejected

With the receiver still running, post a request with a bad signature:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$TUNNEL_URL/webhook" \
  -H "X-Hub-Signature-256: sha256=deadbeef" \
  -H "Content-Type: application/json" \
  -d '{"object":"whatsapp_business_account","entry":[]}'
```

Set `TUNNEL_URL` to the tunnel URL from step 2 first, or substitute it directly. Expected result: `403`. The receiver never parses the body of a request whose signature does not match.

## 8. Two things that expire

- The free `cloudflared` tunnel URL changes every time the tunnel is restarted. After a restart, step 4 has to be repeated: the new URL has to be re-saved in Meta's console.
- The free WhatsApp test number lasts 90 days from when it was created. After that it stops working and a new one has to be requested.

## 9. Point n8n at the reminder hop

n8n runs in the cloud, on a schedule, and polls this process for what's due -- **it never gets called by us**. There is no outbound webhook to n8n anywhere in this codebase; only two endpoints for it to poll.

### 9.1 `.env`

Add a sixth value, alongside the five in step 1:

```
N8N_REMINDER_SECRET=...
```

Any value you choose, the same way `WHATSAPP_WEBHOOK_VERIFY_TOKEN` is any value you choose. `uv run wca serve` prints a warning and leaves `/reminders/*` refusing every request if this is unset -- a missing secret is closed, not open, so there is no way to run the reminder hop without it.

### 9.2 The two endpoints

Both live on the same host and port as `/webhook`, behind the tunnel from step 2. Both require the header `X-N8N-Secret: <N8N_REMINDER_SECRET>` on every request -- missing, empty, or wrong all get `401`, checked before anything else runs.

**`GET /reminders/due?within_hours=26`** -- bookings starting within `within_hours` (default 26, capped at 168) that have not yet been marked reminded. `within_hours` is optional. Each result carries `booking_id`, `customer_phone`, `service_name`, `starts_at` (ISO 8601) and `starts_at_label` (e.g. `"Tuesday 25 August, 2:00pm"`, the same label `check_availability` already produces) so an n8n template can use either without reformatting a time itself. This call changes nothing -- poll it as often as needed.

**`POST /reminders/sent`** -- body `{"booking_id": "..."}`. Marks that booking reminded, so it stops appearing in `/reminders/due`. Idempotent: call it twice for the same booking and the second call still succeeds, and does not move the first call's timestamp. `404` for an unknown booking id.

In n8n: a Schedule node polling `GET /reminders/due` on an interval shorter than the window (so a missed poll is caught by the next one), a node per due booking sending the WhatsApp template, then `POST /reminders/sent` for each one actually sent. A booking that was never marked keeps appearing until it is; one already marked never appears again -- that property is what makes the loop safe against both a missed poll and a double poll.

### 9.3 A note on deliverability

A reminder sent roughly 24 hours ahead falls outside WhatsApp's 24-hour customer service window, so it must go out as an approved message template, not free-form text -- Meta will reject anything else at send time, a failure this codebase cannot see or prevent from here. Getting a reminder template approved in Meta's console has its own lead time, separate from and unblocked by this endpoint existing.

## 10. Check the Graph API version before going live

`GRAPH_VERSION` in `src/wca/transport/whatsapp.py` is currently `v23.0`. Before running any of this, compare it against the version shown in the curl example on Meta's WhatsApp API Setup page for this app. Meta moves this version over time. A stale version does not fail with an obvious "wrong version" error; it fails in ways that look like a bug in this code (a 400, or a field Meta says does not exist). If a call to `send` or a webhook fails for no clear reason, check this first.
