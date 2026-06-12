# Architecture & Status

Companion to `README.md` (how to run) and `CLAUDE.md` (rules). This is the
**design, data flow, and decision log**. Status: live in production as of
2026-06-12.

---

## What problem this solves

Dialpad's native Zendesk integration logs **external** calls only. BPI's internal
IT help desk is reached via internal Dialpad calls, which the native integration
ignores → no tickets. This bridge listens to Dialpad's raw Call Events (which fire
for internal calls too) and creates the tickets itself. It is **supplemental**:
external calls still flow through the native integration; an `INTERNAL_ONLY` filter
keeps the bridge from double-ticketing them.

```
 Caller ──► Dialpad IT Department (IVR menu)
              │  1 = AS400      ─► IT - AS400 (Don't Call)            [contact center]
              │  2 = IT issue   ─► IT - Technical Support (Don't call)[contact center]
              │  3 = other      ─► IT Department - All Agents (...)   [contact center]
              └  after hours    ─► department voicemail
                         │
                         ▼  (Call Events, scoped to those 4 targets)
                 ┌─────────────────────┐
                 │  bridge (FastAPI)    │  POST /dialpad/webhook
                 │  app/main.py         │
                 └─────────┬───────────┘
                           ▼
                   Zendesk API  ─►  ticket (requester, assignee, tags, comments)
```

Public path: Dialpad → `https://dialpad.bpiteam.com` (Cloudflare Tunnel) →
`cloudflared` container → `bridge:8080` (internal Docker network).

---

## Request flow (one event)

```mermaid
flowchart TD
  A[POST /dialpad/webhook] --> B[decode JWT/JSON]
  B --> C[key = master_call_id or entry_point_call_id or call_id]
  C --> D[attach extras: recap / voicemail audio / transcription
          (no-op if no ticket yet)]
  D --> E{direction & INTERNAL_ONLY ok?}
  E -- no --> X[ignore]
  E -- yes --> F{state?}
  F -- voicemail_uploaded --> G[create voicemail ticket]
  F -- connected --> H{answered?\n date_connected & (operator_call_id or target=user)}
  H -- no --> X
  H -- yes, no ticket --> I[create + resolve & assign agent]
  H -- yes, ticket exists --> J[reassign to this answerer]
  F -- hangup --> K{ticket exists?}
  K -- yes --> L[append call length to subject]
  K -- no & answered --> M[fallback create + assign]
  K -- no & not answered --> X
```

**Two-phase, independently recoverable:** Phase 1 creates on `connected`; Phase 2
finalizes (length) on `hangup`. If `connected` is missed, `hangup` creates as a
fallback. If `hangup` is missed, the ticket still exists from `connected` (just no
length appended). Neither phase depends on the other landing.

---

## Event state → action

| Dialpad state        | Action |
|----------------------|--------|
| `connected` (answered) | create ticket (or reassign if it exists), assign answerer |
| `connected` (no answer / menu) | ignore |
| `hangup` (ticket exists) | append call length to subject |
| `hangup` (answered, no ticket) | fallback create + assign |
| `hangup` (unanswered) | ignore |
| `voicemail_uploaded` | create voicemail ticket; attach audio file |
| `transcription` | attach voicemail transcription |
| `recap_summary` | attach AI recap to the answered-call ticket |

"Answered" = `date_connected` set AND (`operator_call_id` set OR
`target.type == "user"`). Menu-disconnects / ring-outs set none of these.

---

## The call-graph problem (why dedup matters)

A single real call fans out into **many legs**, each with its own `call_id`:
the entry-point/queue leg, one operator leg per agent it rings, and a new set on
every transfer. Keying tickets on `call_id` made a ticket *per leg* (e.g. one to
the queue→Support, one to the agent). The fix: key on **`master_call_id`**, which
is identical across all legs and transfers → exactly one ticket per call. On each
answered leg we re-assign (last-answerer-owns), so a transferred call ends up on
whoever finally handled it.

---

## Decision log

- **Supplemental, internal-only** — match an internal caller via `contact.type ==
  "user"`; external calls are the native integration's job (avoids double tickets).
- **Dedup on `master_call_id`** — only reliable cross-leg/transfer key
  (`entry_point_call_id` was null in real payloads).
- **Create on answer (two-phase)** — the user wants the ticket the moment an agent
  picks up. Length is appended at hangup since `talk_time` isn't known until then.
  (We briefly moved creation to hangup to enable a talk-time short-call filter,
  then reverted at the user's request — see CLAUDE.md operating rules.)
- **Answered = `date_connected` + `operator_call_id`/`user` target** — robust
  across contact-center calls where `target.type` is `call_center`, not the agent.
- **Assign via operator fetch** — the agent leg isn't always delivered, so for CC
  calls we fetch `GET /api/v2/call/{operator_call_id}` to get the agent's email.
- **No `talk_time` short-call filter** — it only caught answered-then-dropped
  calls, which are real interactions; dropped on purpose.
- **AI recap for answered calls; recording for voicemails** — a voicemail has no
  recap; an answered call's recap is more useful than the raw recording.
- **Voicemail audio as a real file** — download from `voicemail_link`, re-upload to
  Zendesk Uploads API, matching the native experience.
- **Requester: match-or-create, no `external_id`** — an `external_id` blocks
  Zendesk merges, which breaks when Dialpad has stale caller info.
- **Call center as a slugged tag, `(Don't Call)` stripped** — matches native.

---

## Status

- ✅ Deployed and live; 30 unit tests passing (HTTP mocked).
- ✅ All cases in CLAUDE.md "Cases covered" are implemented + tested.
- ⏳ Open: clear `external_id` on the one pre-fix duplicate contact then merge;
  optional scope/queue rule (payloads are logged on ticketing events for this);
  CI not wired; FastAPI `on_event` → lifespan (cosmetic).
