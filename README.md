# Dialpad → Zendesk bridge (internal calls)

Dialpad's native Zendesk integration logs **external** calls only — internal
Dialpad-to-Dialpad calls never create tickets, which defeats the purpose for an
internal IT help desk. This bridge subscribes to Dialpad's raw **Call Events**
(which fire for internal calls too) and creates Zendesk tickets itself.

## What it does
- Listens for Dialpad call events on `POST /dialpad/webhook`
- On **connected** (answered): creates the ticket the moment the agent picks up,
  matching the native integration's behavior (deduped by `call_id`)
- On **hangup / voicemail**: safety net — creates a ticket for calls that never
  connected (missed / voicemail), tagged `missed-call`, and adds final call
  duration to the answered-call ticket
- On **recap_summary**: attaches Dialpad's **AI call recap** (summary + outcome +
  action items) as a private comment once it's ready (lands *after* the call ends
  and can lag) — used instead of the call recording
- Tries to match the caller to an existing Zendesk end-user by phone

## Prereqs
- A Dialpad **admin/company API key** (the bridge uses it to register the webhook
  + subscription via `setup_dialpad.py`)
- **Dialpad Ai (recaps) enabled** on the account — that's what produces the
  `recap_summary` the bridge attaches. (No `recordings_export` scope needed,
  since we attach the AI summary rather than the audio.)
- The **target_id** of your IT help-desk call center (so you only get IT calls,
  not every internal call at the company)
- A Zendesk API token + an agent email

## Run it
1. `cp .env.example .env` and fill in the Zendesk values + a long random
   `DIALPAD_WEBHOOK_SECRET`.
2. `docker compose up -d --build`
3. Expose `:8080` to the internet over HTTPS. Easiest: put it behind the same
   reverse proxy / tunnel you already use, or a Cloudflare Tunnel. Dialpad must
   be able to reach `https://<your-host>/dialpad/webhook`.
4. Register the webhook + subscription with Dialpad (one time):
   ```bash
   docker compose exec bridge \
     env DIALPAD_API_TOKEN=... \
         PUBLIC_WEBHOOK_URL=https://<your-host>/dialpad/webhook \
         DIALPAD_WEBHOOK_SECRET=<same as .env> \
         IT_TARGET_TYPE=callcenter \
         IT_TARGET_ID=<your IT queue id> \
     python setup_dialpad.py
   ```
   Confirm `IT_TARGET_TYPE` against your queue — Dialpad targets include
   `office`, `department`, `callcenter`, `user`, `room`, etc.

## Verify
- `curl https://<your-host>/healthz` → `{"ok": true}`
- Place a test internal call into the IT queue, hang up, watch the logs:
  `docker compose logs -f bridge`. A ticket should appear; the AI recap comment
  follows once Dialpad finishes generating it.

## Knobs
- `TICKET_ON` = `inbound` (default) | `outbound` | `both`
- `ZENDESK_GROUP_ID` — route auto-tickets straight to the IT group
- `CREATE_STATES` in `app/main.py` controls ticket timing. Default creates on
  `connected` (answer) with `hangup`/`voicemail` as the missed-call safety net.
  Remove `hangup` if you DON'T want tickets for abandoned calls that left no
  voicemail; keep only `hangup` if you preferred the original answer-agnostic
  "ticket when the call ends" behavior.
- State lives in `./data/state.db` (SQLite). Delete it to reset dedup memory.

## Tests
The state machine (ticket-on-answer, dedup, missed-call safety net, enrichment,
AI recap attach) is covered with mocked HTTP — no network or real creds needed:
```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest -q
```

## If the AI recap doesn't attach
The `recap_summary` event lands after the call ends and can lag (or won't fire if
Dialpad Ai isn't enabled / the call wasn't long enough to summarize). The ticket
is still created on connect/hangup — only the recap comment is missing. If it's
chronic, switch from event-driven recap to fetching it by `call_id` on a short
delay after hangup.

## Want the recording too (or instead)?
The bridge attaches the AI recap, not the audio. To add the recording: the call
event carries `recording_details[]` (objects with a `url`) plus `was_recorded`
when the API key has the `recordings_export` scope. Add a `recording` state to
the subscription and a handler that drops those URLs in a comment — or download
each `url` with your Dialpad bearer token and push it through Zendesk's Uploads
API for a real audio attachment. (Note: the field is `recording_details[].url`,
not `recording_url`.)
