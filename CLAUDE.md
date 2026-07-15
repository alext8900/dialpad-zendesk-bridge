# CLAUDE.md — Dialpad → Zendesk Bridge

A small FastAPI webhook bridge that creates **Zendesk** tickets for **internal**
Dialpad calls. Dialpad's native Zendesk integration logs *external* calls only,
so internal Dialpad-to-Dialpad calls (BPI's internal IT help desk) never get
tickets. This bridge subscribes to Dialpad's raw Call Events (which fire for
internal calls too) and creates the tickets itself — supplementing, not
replacing, the native integration.

> Treat as production-supporting tooling. It writes real tickets into a live
> Zendesk instance. Be surgical.

---

## Start Here (new session)

1. Read this file and `README.md`.
2. Make a venv and run the tests — know the baseline before changing anything:
   ```bash
   python -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt
   ./.venv/bin/python -m pytest -q          # expect: 41 passed
   ```
3. Read the file you're about to change. The whole request-handling flow lives in
   `app/main.py:dialpad_webhook`.
4. Dialpad field names have bitten us repeatedly — **verify against a real payload
   (set `DEBUG_PAYLOAD=true`) before trusting any field**, don't assume from docs.

---

## Tech Stack

- **Language / runtime:** Python 3.12 (FastAPI + uvicorn)
- **HTTP client:** httpx
- **Auth:** Dialpad webhook JWT (HS256, PyJWT); Zendesk basic auth (email/token)
- **State:** SQLite (stdlib `sqlite3`), volume-mounted at `/data/state.db`
- **Test runner:** pytest (HTTP fully mocked — no network/creds needed)
- **Packaging / deploy:** Docker Compose (bridge + cloudflared); Cloudflare Tunnel
  for the public URL
- **CI/CD:** none yet — tests are run locally / pre-push. Wire CI when convenient.

---

## How it works (the flow)

`POST /dialpad/webhook` (`app/main.py`) decodes the event (JWT if a secret is set,
else plain JSON), then:

1. **Union all the call-graph ids into one canonical key** (`store.resolve_and_link`
   over `call_id` / `master_call_id` / `entry_point_call_id` / `operator_call_id`).
   One real call rings/transfers through many legs that cross-reference each other
   by **different** fields (no single id is on all of them), so an alias map unions
   them → **one ticket per call**. (A single key like `master_call_id` is NOT
   enough — the operator/agent leg can have `master_call_id: null`.)
2. **Attach-only signals** (recap, voicemail recording, transcription) are applied
   first; they no-op if no ticket exists yet, so they're safe for any leg.
3. **Filters:** `TICKET_ON` (direction) and `INTERNAL_ONLY` (only `contact.type`
   in `INTERNAL_CONTACT_TYPES`, default `user`) — external callers are left to the
   native integration so we never double-ticket.
4. **Two-phase creation:**
   - **Phase 1 — `connected`:** if an agent truly answered (`date_connected` set
     AND (`operator_call_id` set OR `target.type == user`)) → create the ticket,
     resolve + assign the answering agent. Re-assigns on every answered connected
     (last-answerer-owns).
   - **Phase 2 — `hangup`:** append the call length to the subject. If no ticket
     exists yet (connected was missed), create as a **fallback** using the same
     answered guard. The two phases are independently recoverable.
   - **Voicemail — `voicemail_uploaded`:** always create a `voicemail` ticket.

---

## Cases covered / what works (all unit-tested, HTTP mocked)

**Ticket creation**
- ✅ Agent-answered call → one ticket, on answer (`connected`).
- ✅ Contact-center / **transferred** call (rings through multiple call centers) →
  still **one** ticket, deduped on `master_call_id`.
- ✅ `hangup` **fallback** create if the `connected` event was missed.
- ✅ Voicemail (incl. after-hours department voicemail) → ticket.
- ✅ Menu-disconnect / IVR-abandon / ring-and-hangup → **no ticket** (they never
  set the answer fields). This was real noise we deliberately filter.
- ✅ Reached the voicemail greeting and hung up **without leaving a message** →
  **no ticket** (only `voicemail_uploaded` proves a recording exists).
- ✅ Outbound calls and external callers → skipped.

**Assignment**
- ✅ Direct answer: agent is `target` (`type==user`) → assigned by email match.
- ✅ Contact-center answer: `target` is the call center; the agent is a separate
  operator leg fetched via `operator_call_id` (`GET /api/v2/call/{id}`) → assigned.
- ✅ Transfers: re-assign to whoever last answered.
- ✅ No agent resolvable → ticket left unassigned in the default (Support) group.

**Requester (caller)**
- ✅ Matched to an existing Zendesk user by phone (exact, last-10-digit match),
  then by **email** (exact). Existing users — including **support agents/admins
  calling the help desk** — are reused as the requester, never modified, so an
  agent is never downgraded to end-user.
- ✅ Otherwise a new customer is created from the caller ID / phone — never falls
  back to the API account. The create sends **no `role`** (defaults to end-user),
  so a stray email match can't downgrade an account.
- ✅ Created customers carry **no `external_id`** so they stay mergeable (Dialpad
  can have stale info, e.g. a changed email → a duplicate that must be mergeable).

**Content**
- ✅ Subject: `Dialpad call with {caller} — answered by {agent} · {length}`
  (length from `talk_time`, appended at hangup — or **backfilled from the recap**
  or any later event carrying `talk_time` if Dialpad drops the hangup). Voicemail:
  `Dialpad voicemail from {caller}`.
- ✅ Call center added as a **slugged tag** (`it_technical_support`), like native.
- ✅ `(Don't Call)` marker stripped from subject, body, and tag.
- ✅ First comment: clean Caller / Receiver / Call center breakdown (private note).
- ✅ Answered calls get the Dialpad **AI recap** (`recap_summary`) as a comment.
- ✅ Voicemails get the **audio file** (downloaded from `voicemail_link`, re-
  uploaded to Zendesk Uploads API) + the **transcription** (`transcription_text`).
  Falls back to a link if no `DIALPAD_API_TOKEN`.

---

## Verified Dialpad payload facts (don't re-learn these the hard way)

- **No single id links all legs.** A call fans into the department/entry leg, the
  call-center leg, and the operator/agent leg. They cross-reference each other:
  call-center leg has `master_call_id`=root + `operator_call_id`=agent-leg's id;
  the operator leg has `master_call_id: null` and `entry_point_call_id`=call-center
  leg's id. → must union `call_id`/`master_call_id`/`entry_point_call_id`/
  `operator_call_id` (the alias map), not key on one field.
- Answered contact-center leg has `target.type == "call_center"` (underscore!),
  **not** `user`. The agent is a separate leg in `operator_call_id`; sometimes
  it's delivered as its own event (target == the agent), sometimes not → fetch it
  via `GET /api/v2/call/{operator_call_id}` when needed.
- `date_connected` is set when an agent picks up; `talk_time` (ms) is real agent
  talk time (excludes IVR/queue/ring) and is only populated at `hangup`.
- `duration` / `total_duration` **include IVR menu time** — misleading for "how
  long was the call." Use `talk_time`.
- Voicemail: `voicemail_link` (secureblob URL, needs the API token to download),
  `transcription_text`, on `voicemail_uploaded` / `transcription` states.
- Recording links are `recording_details[].url` (NOT `recording_url`). Need the
  `recordings_export` scope on the API key.
- The internal-vs-external signal is `contact.type` (`user` = internal).

---

## Run / Deploy

- **Repo:** github.com/alext8900/dialpad-zendesk-bridge (public; **no secrets** in
  it — `.env` is gitignored).
- **Server:** Windows Docker host (`C:\docker\dialpad-zendesk-bridge`). Workflow:
  push here → `git pull` on the server → `docker compose up -d --build`.
- **Public URL:** Cloudflare Tunnel (`cloudflared` service in compose, token in
  `.env`) → `https://dialpad.bpiteam.com/dialpad/webhook`.
- **One-time setup:** `list_dialpad_targets.py` to find IDs →
  `setup_dialpad.py` to register the webhook + per-target subscriptions
  (`IT_TARGETS`). Scope = IT Department (after-hours voicemail) + 3 IT contact
  centers. See README for the exact commands.
- **Config:** all via `.env` (see `.env.example`). `ZENDESK_GROUP_ID` stays blank
  (Support is the default group). `DEBUG_PAYLOAD=true` logs full event payloads.

---

## Files — handle with care

- `app/main.py` — the whole webhook state machine. Read it fully before editing.
- `app/store.py` — SQLite dedup/flags. `init()` migrates older DBs via
  `ALTER TABLE ADD COLUMN`; keep it idempotent. The server's `state.db` persists.
- `docker-compose.yml` — bridge has **no host port** (Windows reserves 8080); the
  tunnel reaches it at `bridge:8080` over the internal network. cloudflared is
  forced to `--protocol http2` (QUIC drops packets on WSL2). Don't re-add a host
  port mapping without a reason.
- `.env` — never commit it. Don't add inline `# comments` after a value in a real
  `.env`: Docker Compose `env_file` keeps them as part of the value (caused a 500).
  `_cfg()` in `main.py` defends against this for scalar config.

---

## Project gotchas / failure modes (and the rule each implies)

- **Unverified Dialpad fields:** every wrong assumption (`target.type==user` for
  answered, `entry_point_call_id` for dedup, `recording_url`) caused a real miss.
  → Verify with `DEBUG_PAYLOAD` before trusting a field.
- **Windows host port 8080:** WinNAT reserves it; binding fails. → No host port;
  tunnel only.
- **cloudflared QUIC on WSL2:** intermittent 502s. → `--protocol http2`.
- **Docker DNS after partial recreate:** `lookup bridge: no such host`. → full
  `docker compose down && up`; cloudflared `depends_on: condition: service_healthy`.
- **env_file inline comments** → polluted values / 500. → comments on their own
  lines; `_cfg()` strips them defensively.
- **Contact `external_id`** makes Zendesk contacts unmergeable. → don't set it.
- **Dropped `hangup` event:** on overlapping calls Dialpad sometimes never delivers
  the `hangup`, so the call length was never appended (the recap fired with the
  final `talk_time`, but only `hangup` appended length). → `_append_length` now
  backfills from any event carrying `talk_time` (via `_attach_extras`), idempotent
  on `is_enriched`; `mark_enriched` only fires once a real length is appended.
- **Phantom voicemail ticket (#753):** `state=voicemail` only means "this call ended
  up at voicemail" — Dialpad emits it even when the caller hung up during the
  greeting, and populates `voicemail_link` anyway (fetching it 404s;
  `voicemail_recording_id`/`was_recorded`/`transcription_text` are the honest
  signals and are null/false). `"voicemail"` had been a create state since the first
  commit, before `voicemail_uploaded` existed; 658eb21 added the upload state and the
  comments, but left the legacy one in `CREATE_STATES` — so the code contradicted its
  own docs for a month. Invisible on real voicemails (the upload event just deduped),
  so only an empty one exposed it. → **create voicemails ONLY on `voicemail_uploaded`**;
  it is the only event that proves a message exists.
- **Agent downgraded to end-user:** a support agent calling the help desk got their
  Zendesk role flipped to end-user, because `users/create_or_update.json` matches by
  email and we sent `role: end-user`. → resolve the requester by reusing any existing
  user (phone **and** email lookup) and never send `role` on create. Re-promote any
  already-downgraded agent in Zendesk by hand.

---

## Open items / not yet done

- **Yvonne duplicate (and any like it):** a contact created before the
  `external_id` fix can't be merged until its `external_id` is cleared
  (`PUT /users/{id}.json {"external_id":null}`), then merge. New contacts are fine.
- **Scope/queue rule:** we don't yet branch behavior by which call center took the
  call. Full event payloads are logged on every ticketing event (`_log_ticketing`)
  so the right routing field (`routing_breadcrumbs`, `transferred_from`, etc.) can
  be chosen later.
- **"close out" on hangup** appends the length only — it does **not** set the
  Zendesk ticket to Solved/Closed (intentional). Revisit if auto-status is wanted.
- **Create path is check-then-act, and only accidentally race-free.** `dialpad_webhook`
  reads `store.get_ticket(key)` and only later calls `store.save_ticket()`, with a
  Zendesk POST in between — a classic TOCTOU window. It is currently **unreachable**:
  the handler is `async def` but its only `await` is `request.body()` (main.py:121),
  *before* the window, and every HTTP call inside is **sync httpx** on uvicorn's
  default single worker, so the event loop cannot interleave two webhooks inside the
  critical section. `save_ticket`'s `INSERT OR IGNORE` would then *mask* a double
  create by silently dropping the second row, orphaning a real Zendesk ticket.
  This safety is load-bearing accident: switching to `httpx.AsyncClient`, adding
  `await` in the window, or running `--workers 2` opens the race immediately.
  → If that ever changes, make creation an atomic claim-then-create (`INSERT OR
  IGNORE ... ticket_id NULL` as the lock) on the **existing `calls` table, keyed on
  the canonical alias key** — NOT a new table and NOT keyed on `master_call_id` (the
  operator leg has `master_call_id: null`; single-id keying is the duplicate-ticket
  bug 47efa02 fixed). Note `get_ticket` returns `None` for both "no row" and
  "ticket_id IS NULL", so a claim row is invisible to the current dedup check and
  would need handling, along with releasing the claim if the Zendesk create throws.
- **CI** is not wired up. Tests are local/pre-push for now.
- The deprecated FastAPI `@app.on_event("startup")` could move to a lifespan
  handler (cosmetic warning only).

---

## Operating rules

The full operating rules (Rules 1–12, CI/CD & Testing rules T1–T10, autonomous
session rules) live in **[docs/OPERATING_RULES.md](docs/OPERATING_RULES.md)** —
read and follow them. Project-specific additions on top of those:

- **Separate necessary bug fixes from behavior changes, and ASK before changing
  agreed behavior** (learned the hard way on create-on-answer vs create-at-hangup).
- Dialpad field names have burned us — verify against a real payload
  (`DEBUG_PAYLOAD=true`) before trusting any field; don't assume from docs.
- Pushing to `main` is the deploy path (server pulls). Don't touch Cloudflare /
  Railway / Fly infra directly; give the user the steps.
