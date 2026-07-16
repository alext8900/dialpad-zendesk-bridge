# Office-line caller ticketing

**Date:** 2026-07-16
**Status:** Implemented + tested (48 passing); pending deploy (server `.env` edit + push)
**Area:** `app/main.py` webhook gate + requester resolution

## Problem

Calls that arrive from a Dialpad **office** (a shared BPI location line, e.g.
"BPI Jackson") are dropped by both integrations and never become tickets:

- The **native** Dialpad-Zendesk integration only tickets external caller-ID
  calls (Dialpad `contact.type == "local"`), e.g. someone on a BPI AT&T phone
  whose caller ID reads "Building Plastics, Inc". It ignores calls that originate
  inside Dialpad.
- **This bridge** only tickets `contact.type == "user"` (a real Dialpad user).
  Office-line callers are neither, so they fall through the internal-only gate
  and get `skip: contact.type='office' not internal`.

The result: a real, answered, recorded IT support call gets no ticket anywhere.
This has happened repeatedly and is the trigger for the change.

The individual on an office line is often not a Dialpad user (example: "Courtney
Ellis" calling from the Jackson office), so the call carries only the office
identity, not the person's.

## Evidence (verified, not assumed)

Direct read-only `GET /api/v2/call/{id}` queries were run against real calls:

| Call | `contact.type` | `contact.name` | Who tickets it |
| --- | --- | --- | --- |
| Courtney / BPI Jackson (call `4895451305943040` + legs) | `office` | "BPI Jackson" | nobody today (this feature) |
| Willem / Building Plastics AT&T (calls `6550789315829760`, `5987024526581760`) | `local` | "Building Plasti" | native integration |

Two independent facts came out of this:

1. **The individual is not retrievable via API.** All three legs of the office
   call return the same `contact` block: `{name: "BPI Jackson", type: "office",
   phone: "+16019816060", email: ""}`. No participants/contacts array, no person
   object. "Courtney Ellis" exists only in Dialpad's UI enrichment and as a
   first-name mention inside the AI recap free-text. There is no reliable
   structured field to auto-identify the caller from. Auto-ID is out of scope
   (see Non-goals).
2. **`contact.type` cleanly separates the two cases.** `office` (internal Dialpad
   office, native ignores) vs `local` (external caller-ID, native tickets) are
   distinct enum values on the same field. Gating on `office` catches exactly the
   calls we want and never overlaps the native-handled `local` calls, so there is
   no double-ticket path.

## Design

Approach: ticket office-line calls with an honest placeholder requester and let
the technician who took the call correct it. The person's name reaches the tech
through the AI recap we already attach; a human disambiguates (in the example
recap, "Courtney" the caller vs "Blake Parker" a third party) far more reliably
than any name-extraction could.

### 1. Gate

Change the default of `INTERNAL_CONTACT_TYPES` from `"user"` to `"user,office"`.

- Code default, so the behavior travels with the repo and a fresh clone cannot
  silently lose it.
- `local` and every other external type stay skipped, so native-handled calls
  are untouched. Only `office` is added; no other internal types (`room`,
  `department`, etc.) until there is evidence they are needed (YAGNI).

**Deploy step (required, not optional).** `_cfg` reads the env var *before* the
code default, and both `.env` and `.env.example` currently pin
`INTERNAL_CONTACT_TYPES=user`. So the code-default change alone is **inert on the
running server** and the tests still pass (they use the module default), which
would read as "shipped and working" while prod keeps skipping office calls. The
change is only live once the env is updated:

- Update `.env.example` to `INTERNAL_CONTACT_TYPES=user,office` (committed).
- Update the server's `.env` to `INTERNAL_CONTACT_TYPES=user,office` (operator
  does this by hand; `.env` is gitignored) before/at deploy.
- Verify after deploy:
  `docker compose exec bridge python -c "import app.main as m; print(m.INTERNAL_CONTACT_TYPES)"`
  should include `office`.

### 2. Requester (no code change to resolution)

Office calls already fall through `_requester_id` to
`_create_end_user("BPI Jackson", phone)`:

- Reuses/creates a mergeable end-user named after the office (no `external_id`,
  per existing rule). Honest: it records which **location** called.
- Multiple callers from one office share that placeholder contact until a tech
  corrects each ticket's requester. Acceptable and expected.
- The requester never gets mis-set to a real named person, so there is no
  misattribution or cross-user ticket-visibility risk.
- Edge case (accepted): `_requester_id` tries `_find_user_by_phone` first, so if a
  Zendesk user happens to have the office main line as their phone, the requester
  resolves to that user instead of the office placeholder. Low likelihood, still a
  reasonable requester, not worth guarding against.

### 3. Placeholder-requester note + tags (new)

When `contact.type == "office"`, do two things at ticket creation:

**a. Add a line to the first comment**, e.g.:

> Shared office line: requester is a placeholder. Confirm the actual caller (see
> the call recap / recording) and update the requester.

- The whole first comment is **already** created `"public": False` in
  `_create_ticket`, so this line is private by construction. No extra mechanism
  needed; just append it to `_render_body`'s lines.
- "see the recap / recording" rather than only "recap", because short office
  calls may get Dialpad's "Summaries are currently not generated..." sentinel
  instead of a real recap (see section 4). The recording still exists.

**b. Add filterable tags** `office-line` and `verify-requester` to the ticket.
Body text is not filterable in Zendesk; the whole point is that a tech corrects
the requester, which is exactly what a saved view / trigger keys off. The tags let
BPI build a "office-line tickets needing requester confirmation" view and a
trigger. `office-line` is the durable category; `verify-requester` is the
actionable flag.

**Applies to office voicemails too.** Gate the note and tags purely on
`contact.type == "office"`, independent of `voicemail` — an office caller who
leaves a voicemail also gets a placeholder requester and also needs confirming.

### 4. Suppress Dialpad's "no recap" sentinel (folded in)

Dialpad returns a human-readable sentinel in `recap_summary` when it has nothing
to summarize (voicemails and short calls): `"Summaries are currently not generated
for short calls or voicemails."` `_maybe_attach_recap` only checks the field is
non-empty, so it posts that apology string as if it were the AI's recap, cluttering
exactly the low-context tickets (voicemails, short calls) this feature also creates.

Fix: after stripping `recap_summary`, bail if it matches a known sentinel. Define
the sentinel(s) as a module-level `frozenset` constant (e.g. `RECAP_SENTINELS`)
and check `summary in RECAP_SENTINELS`. Use **exact match**, not a substring
check. A false negative (a new sentinel variant slips through once) is harmless
noise; a false positive (suppressing a real recap because it happened to contain
similar words) loses real content. Add variants to the constant if Dialpad changes
the wording.

### 5. Everything else: unchanged

Office calls ride the existing call-center to operator path (the same path the
Courtney call took: `target.type == call_center`, agent on the operator leg).
So create-on-`connected`, last-answerer assignment, alias-map dedup, recording
attach, hangup length-append, and voicemail (`voicemail_uploaded`, per the
2026-07-16 fix) all apply with no change. Recap attach applies too, with the one
change in section 4 (sentinel suppression).

## Non-goals

- **No recap name-extraction / auto-ID.** Verified impossible from structured
  data and unsafe from free-text (the example recap names three people: the
  agent, the caller, and a discussed third party). A human corrects the
  requester instead.
- **No new contact types beyond `office`.**

## Risks

- **Double-ticketing native-handled calls** is the only material risk, and it is
  closed by the disjoint `office` vs `local` split above. A regression test locks
  it in.
- **Post-deploy canary.** Because this widens what gets ticketed, watch the first
  few office calls after deploy: confirm each tickets exactly once and native does
  NOT also ticket it (no duplicate appears). The disjoint-set reasoning is
  API-verified, but this is cheap insurance and the change is reversible (revert
  the `.env` value).

## Touch points

- `INTERNAL_CONTACT_TYPES` default (`app/main.py`, config block ~L74) + the
  `.env.example` / server `.env` value (see Deploy step in section 1).
- The internal-only gate (`app/main.py` ~L172) needs no logic change; it reads
  the widened set.
- `_render_body` (office note) and `_create_ticket` (office tags), conditional on
  `contact.type == "office"`, independent of `voicemail`.
- `_maybe_attach_recap` (`app/main.py` ~L651) + a new `RECAP_SENTINELS` constant
  for the sentinel suppression.

## Tests

1. **`local` external caller-ID call is still skipped** (anti-double-ticket
   regression guard; protects the native flow). Highest priority. Assert
   `len(posts) == 0`, not just the skip return value.
2. **Gate logic, not just the default.** Monkeypatch `main.INTERNAL_CONTACT_TYPES`
   to prove: `office` in the set to ticketed; `office` absent to skipped.
   (`INTERNAL_CONTACT_TYPES` is frozen at import, so a config-behavior test must
   patch the set directly rather than the env.)
3. **`office` answered call** creates exactly one ticket, requester is the office
   placeholder, assigned to the answering tech, carries the note **and** both
   tags (`office-line`, `verify-requester`).
4. **A normal `user` call does NOT** get the office note or tags (guard against
   the note/tags leaking onto every ticket).
5. **`office` voicemail** creates a voicemail ticket that ALSO carries the note
   and tags (confirms composition with the 2026-07-16 `voicemail_uploaded` fix).
6. **Sentinel recap suppression:** a `recap_summary` equal to the sentinel
   attaches **no** recap comment; the same sentinel with surrounding whitespace is
   still suppressed (locks the strip+match); a real summary still attaches.
