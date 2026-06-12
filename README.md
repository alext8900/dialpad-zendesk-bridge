# Dialpad → Zendesk bridge (internal calls)

Dialpad's native Zendesk integration logs **external** calls only — internal
Dialpad-to-Dialpad calls never create tickets, which defeats the purpose for an
internal IT help desk. This bridge subscribes to Dialpad's raw **Call Events**
(which fire for internal calls too) and creates Zendesk tickets itself.

## What it does
- Listens for Dialpad call events on `POST /dialpad/webhook`
- **Only tickets calls an agent actually answered, plus voicemails.** Two-phase:
  the ticket is created on **connected** the moment an agent picks up (a real
  answer = `date_connected` set with `operator_call_id`, or a direct `target.type
  == user`), and finalized on **hangup** by appending the call length. Hangup also
  creates as a fallback if the connected event was missed. Menu-disconnects,
  abandons, and transfer hops never set those answer fields, so they don't ticket.
- **One ticket per call.** A call rings/transfers through many legs, each with its
  own `call_id`, but all share `master_call_id` — the bridge dedupes on that, so
  transfers and contact-center fan-out collapse to a single ticket. On each answer
  it re-assigns to whoever just picked up (last-answerer-owns).
- **Assigned to whoever answered.** For a direct call the answering agent is the
  `target`; for a contact-center call the target is the call center and the agent
  is a separate operator leg, fetched via `operator_call_id` (Dialpad `GET
  /api/v2/call/{id}`). No match → unassigned in the default (Support) group.
- **Subject**: `Dialpad call with {caller} — answered by {agent} · {length}` for
  answered calls (just the agent, not the queue), `Dialpad voicemail from {caller}`
  for voicemails. The **call center is added as a tag** (slugged, e.g.
  `it_technical_support`), like the native integration. The `(Don't Call)` marker
  on the IT call-center names is stripped from the subject, body, and tag.
- On **voicemail / voicemail_uploaded**: creates a `voicemail` ticket and attaches
  the **voicemail recording as an actual audio file** (downloaded from Dialpad and
  re-uploaded to Zendesk, like the native integration) — this is the case the
  native integration skips for internal calls. Falls back to a link if no Dialpad
  API token is configured. The voicemail **transcription** is attached too when
  the `transcription` event fires (a beat later)
- On **recap_summary**: for *answered* calls, attaches Dialpad's **AI call recap**
  (summary + outcome + action items) as a private comment once it's ready (a
  voicemail has no recap, so it gets the recording/transcript instead)
- **Requester** = the caller, like the native integration: matches an existing
  Zendesk customer by phone, otherwise creates a new customer from the caller ID
  (or the phone number). Never silently falls back to the API account. Created
  customers carry **no `external_id`**, so they stay mergeable if Dialpad's caller
  info was stale and a duplicate gets made.

## Prereqs
- A Dialpad **admin/company API key** (the bridge uses it to register the webhook
  + subscription via `setup_dialpad.py`)
- **Dialpad Ai (recaps) enabled** on the account — that's what produces the
  `recap_summary` the bridge attaches. (No `recordings_export` scope needed,
  since we attach the AI summary rather than the audio.)
- The **IDs** of the Dialpad targets to scope to. Our topology is the IT
  Department's menu routing to 3 contact centers, so that's 4 targets: the
  department (after-hours voicemail) + the 3 contact centers (routed calls).
  Find the IDs with `list_dialpad_targets.py` (below).
- A Zendesk API token + an agent email

## Run it
1. `cp .env.example .env` and fill in the Zendesk values, a long random
   `DIALPAD_WEBHOOK_SECRET`, and `DIALPAD_API_TOKEN` (needed to attach voicemail
   audio files).
2. Set up the public URL (Cloudflare Tunnel) — see **Public URL** below — and put
   its `TUNNEL_TOKEN` + `PUBLIC_WEBHOOK_URL` in `.env`.
3. `docker compose up -d --build` (starts both the bridge and the tunnel).
4. Find the target IDs to scope to (one time). The helper scripts auto-read
   `.env`, so as long as `DIALPAD_API_TOKEN` is in it:
   ```bash
   docker compose exec bridge python list_dialpad_targets.py IT
   ```
   Note the DEPARTMENT id for the IT Department + the CALLCENTER ids for the 3
   IT queues, and put them in `.env` as `IT_TARGETS` plus set `PUBLIC_WEBHOOK_URL`:
   ```
   PUBLIC_WEBHOOK_URL=https://<your-host>/dialpad/webhook
   IT_TARGETS=department:<deptId>,callcenter:<as400>,callcenter:<tech>,callcenter:<allAgents>
   ```
5. Register the webhook + subscriptions with Dialpad (one time, reads `.env`):
   ```bash
   docker compose exec bridge python setup_dialpad.py
   ```
   One subscription is created per target. Dialpad target types: `office`,
   `department`, `callcenter`, `user`, `room`.

> Running the scripts **locally** instead of in the container? Use the venv —
> `./.venv/bin/python list_dialpad_targets.py IT` — they read the same `.env`.

## Public URL (Cloudflare Tunnel)
The `cloudflared` service in `docker-compose.yml` publishes the bridge at a stable
public hostname with no inbound firewall ports. One-time dashboard setup:

1. Cloudflare **Zero Trust** → **Networks → Tunnels → Create a tunnel** →
   type **Cloudflared** → name it (e.g. `dialpad-bridge`) → **Save**.
2. On the "Install connector" screen, copy the **token** (the long string after
   `--token` in the shown command). Put it in `.env` as `TUNNEL_TOKEN=...`.
   (You do NOT run the install command — the compose `cloudflared` service does.)
3. **Public Hostname** tab → **Add a public hostname**:
   - Subdomain `dialpad`, Domain `bpiteam.com`  →  `https://dialpad.bpiteam.com`
   - Service: **Type** `HTTP`, **URL** `bridge:8080`
     (cloudflared reaches the bridge by its compose service name on the internal
     network — not `localhost`).
4. `docker compose up -d` — the tunnel connects and `dialpad.bpiteam.com` goes live.

`bpiteam.com` must be a zone in this Cloudflare account for the hostname to route.

## Verify
- `docker compose ps` → the `bridge` container should be **healthy** (internal
  healthcheck; no host port is published — the tunnel is the access path).
- `curl https://dialpad.bpiteam.com/healthz` → `{"ok": true}`. To check from the
  box without the tunnel: `docker compose exec bridge python -c "import
  urllib.request; print(urllib.request.urlopen('http://localhost:8080/healthz').read())"`.
- `docker compose logs -f cloudflared` should show a registered connection.
- Place a test internal call into the IT queue, hang up, watch the logs:
  `docker compose logs -f bridge`. A ticket should appear; the AI recap comment
  follows once Dialpad finishes generating it.

## Knobs
- `INTERNAL_ONLY` = `true` (default) — only tickets **internal** calls so it stays
  purely supplemental to the native integration (which already tickets external
  calls). Internal callers have `contact.type == "user"`; external callers are
  `local`/`google`/`nylas`/`microsoft`. Set `false` to ticket every call.
- `INTERNAL_CONTACT_TYPES` = `user` (default) — comma-list of `contact.type`
  values treated as internal (e.g. `user,room`) if your tenant differs.
- `TICKET_ON` = `inbound` (default) | `outbound` | `both`
- `ZENDESK_GROUP_ID` — **optional**; leave blank if your support group is the
  Zendesk *default* group (unassigned tickets land there automatically). Only set
  it to force a specific non-default group.
- Tickets are created only for **agent-answered calls + voicemails** (see "What it
  does"). Unanswered/menu-disconnect calls are intentionally filtered out.
- State lives in `./data/state.db` (SQLite). Delete it to reset dedup memory.

## Tests
The state machine (answered-only ticketing, dedup, enrichment, AI recap +
voicemail attach) is covered with mocked HTTP — no network or real creds needed:
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

## Recordings
- **Voicemails**: the audio is downloaded from Dialpad and attached to the ticket
  as a real file (needs `DIALPAD_API_TOKEN` with the recordings scope; set
  `ATTACH_VOICEMAIL_AUDIO=false` to attach the link instead).
- **Answered calls**: get the AI recap, not the audio. If you also want the
  *answered-call* recording, the event carries `recording_details[]` (objects with
  a `url`) plus `was_recorded` when the key has the `recordings_export` scope —
  subscribe to the `recording` state and reuse `_download_*`/`_zendesk_upload` to
  attach it the same way voicemails are handled. (Field is `recording_details[].url`,
  not `recording_url`.)
