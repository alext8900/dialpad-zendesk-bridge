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
- On **recording**: attaches the recording link once it's ready (lands *after*
  the call ends and sometimes lags)
- Tries to match the caller to an existing Zendesk end-user by phone

## Prereqs
- A Dialpad **admin/company API key** with the `recordings_export` scope
  (needed for `recording_url` in events)
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
  `docker compose logs -f bridge`. A ticket should appear; the recording comment
  follows a beat later.

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
recording attach) is covered with mocked HTTP — no network or real creds needed:
```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest -q
```

## If recordings don't attach
Dialpad's recording event is known to occasionally lag or no-show even with the
scope set. The ticket still gets created on hangup — only the recording comment
is missing. If it's chronic, switch from event-driven recording to fetching the
recording by `call_id` on a short delay after hangup.

## Going from link → real attachment
`_maybe_attach_recording` drops the Dialpad recording URL as a private comment.
To attach the actual audio: download each `recording_url` with your Dialpad
bearer token, push the bytes through Zendesk's Uploads API, then reference the
returned upload token in the ticket comment.
