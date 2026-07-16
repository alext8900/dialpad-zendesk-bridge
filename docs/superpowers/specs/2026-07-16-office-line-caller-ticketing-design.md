# Office-line caller ticketing

**Date:** 2026-07-16
**Status:** Approved (design), pending implementation plan
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

- Code default, not a server-only `.env` override, so the behavior travels with
  the repo and a fresh clone cannot silently lose it.
- `local` and every other external type stay skipped, so native-handled calls
  are untouched. Only `office` is added; no other internal types (`room`,
  `department`, etc.) until there is evidence they are needed (YAGNI).

### 2. Requester (no code change to resolution)

Office calls already fall through `_requester_id` to
`_create_end_user("BPI Jackson", phone)`:

- Reuses/creates a mergeable end-user named after the office (no `external_id`,
  per existing rule). Honest: it records which **location** called.
- Multiple callers from one office share that placeholder contact until a tech
  corrects each ticket's requester. Acceptable and expected.
- The requester never gets mis-set to a real named person, so there is no
  misattribution or cross-user ticket-visibility risk.

### 3. Placeholder-requester note (new)

When `contact.type == "office"`, add one **internal** (non-public) line to the
ticket's first comment, e.g.:

> Shared office line: requester is a placeholder. Confirm the actual caller (see
> the call recap / recording) and update the requester.

- Internal so the placeholder contact is never emailed.
- "see the recap / recording" rather than only "recap", because short office
  calls may get Dialpad's "Summaries are currently not generated for short calls
  or voicemails" placeholder instead of a real recap.
- Wording avoids em dashes, per project style.

### 4. Suppress Dialpad's "no recap" sentinel (folded in)

Dialpad returns a human-readable sentinel in `recap_summary` when it has nothing
to summarize (voicemails and short calls): `"Summaries are currently not generated
for short calls or voicemails."` `_maybe_attach_recap` only checks the field is
non-empty, so it posts that apology string as if it were the AI's recap, cluttering
exactly the low-context tickets (voicemails, short calls) this feature also creates.

Fix: after stripping `recap_summary`, bail if it matches a known sentinel. Use
**exact match against a small list** of known sentinel strings, not a substring
check. A false negative (a new sentinel variant slips through once) is harmless
noise; a false positive (suppressing a real recap because it happened to contain
similar words) loses real content. Add variants to the list if Dialpad changes the
wording.

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

## Touch points

- `INTERNAL_CONTACT_TYPES` default (`app/main.py`, config block ~L74).
- The internal-only gate (`app/main.py` ~L172) needs no logic change; it reads
  the widened set.
- First-comment/body rendering (`_render_body` / `_create_ticket`) for the
  office note, conditional on `contact.type == "office"`.
- `_maybe_attach_recap` (`app/main.py` ~L651) for the sentinel suppression.

## Tests

1. **`local` external caller-ID call is still skipped** (anti-double-ticket
   regression guard; protects the native flow). Highest priority.
2. **`office` answered call** creates exactly one ticket, requester is the office
   placeholder, assigned to the answering tech, and carries the internal note.
3. **`office` voicemail** creates a voicemail ticket (confirms composition with
   the 2026-07-16 `voicemail_uploaded` fix).
4. **Sentinel recap suppression:** a `recap_summary` equal to Dialpad's
   "Summaries are currently not generated..." sentinel attaches **no** recap
   comment; a real summary still attaches as before.
