"""
Dialpad -> Zendesk bridge for INTERNAL calls.

Dialpad's native Zendesk integration logs *external* calls only. Internal
Dialpad-to-Dialpad calls never create tickets, which is useless for an internal
IT help desk. The raw Call Events API fires on every call regardless, so we
catch those events and create the ticket via the Zendesk API ourselves.

Ticket timing mirrors the native integration:
  - 'connected'         -> create the ticket the moment the call is answered
  - 'hangup'/'voicemail'-> safety net: create a ticket for calls that never
                           connected (missed / voicemail), and enrich the
                           answered-call ticket with final duration
  - 'recording'         -> attach the recording link once it's ready

A tiny SQLite store maps call_id -> ticket_id so dedup and the later updates
survive container restarts.
"""

import os
import json
import logging

import jwt
import httpx
from fastapi import FastAPI, Request, HTTPException

from . import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# ---- Config (see .env.example) ---------------------------------------------
DIALPAD_WEBHOOK_SECRET = os.environ.get("DIALPAD_WEBHOOK_SECRET", "")
ZENDESK_SUBDOMAIN = os.environ["ZENDESK_SUBDOMAIN"]
ZENDESK_EMAIL = os.environ["ZENDESK_EMAIL"]
ZENDESK_API_TOKEN = os.environ["ZENDESK_API_TOKEN"]
TICKET_ON = os.environ.get("TICKET_ON", "inbound")      # inbound | outbound | both
DEFAULT_GROUP_ID = os.environ.get("ZENDESK_GROUP_ID")
# Dialpad API token (needs the 'recordings'/'recordings_export' scope) so the
# bridge can DOWNLOAD the voicemail audio and re-upload it to Zendesk as a real
# file attachment, like the native integration does. Without it, the bridge falls
# back to dropping the voicemail_link as a comment.
DIALPAD_API_TOKEN = os.environ.get("DIALPAD_API_TOKEN", "")
ATTACH_VOICEMAIL_AUDIO = os.environ.get("ATTACH_VOICEMAIL_AUDIO", "true").lower() == "true"
# States that should result in a ticket. 'connected' = answered (instant ticket).
# 'hangup'/'voicemail' = safety net so missed calls still get a ticket. Drop
# 'hangup' here if you DON'T want tickets for abandoned calls with no voicemail.
VOICEMAIL_STATES = {"voicemail", "voicemail_uploaded"}
CREATE_STATES = {"connected", "hangup", "voicemail", "voicemail_uploaded"}
TERMINAL_STATES = {"hangup", "voicemail", "voicemail_uploaded"}
# Only ticket INTERNAL (Dialpad-to-Dialpad) calls. External callers are already
# ticketed by Dialpad's native Zendesk integration, so handling them here too
# would double up. An internal caller has contact.type == "user"; external
# callers are local/google/nylas/microsoft. Override the type set if your tenant
# differs (e.g. include 'room'); set INTERNAL_ONLY=false to ticket everything.
INTERNAL_ONLY = os.environ.get("INTERNAL_ONLY", "true").lower() == "true"
INTERNAL_CONTACT_TYPES = {
    t.strip().lower()
    for t in os.environ.get("INTERNAL_CONTACT_TYPES", "user").split(",")
    if t.strip()
}

ZBASE = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
ZAUTH = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)

app = FastAPI(title="Dialpad-Zendesk Internal Bridge")


@app.on_event("startup")
def _startup():
    store.init()
    log.info("bridge up; ticketing on '%s' calls", TICKET_ON)


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _decode(raw: bytes) -> dict:
    """Dialpad sends a JWT (HS256) when a secret is set, else plain JSON."""
    body = raw.decode("utf-8").strip()
    if DIALPAD_WEBHOOK_SECRET:
        try:
            return jwt.decode(body, DIALPAD_WEBHOOK_SECRET, algorithms=["HS256"])
        except jwt.InvalidTokenError as e:
            raise HTTPException(status_code=401, detail=f"bad signature: {e}")
    return json.loads(body)


@app.post("/dialpad/webhook")
async def dialpad_webhook(request: Request):
    event = _decode(await request.body())

    call_id = str(event.get("call_id") or "")
    state = event.get("state")
    direction = event.get("direction")  # 'inbound' | 'outbound'
    if not call_id or not state:
        return {"ignored": "no call_id/state"}

    log.info("event call_id=%s state=%s direction=%s", call_id, state, direction)

    # Attach-only signals can arrive on their own event after the call ends
    # (AI recap, voicemail recording, voicemail transcription). They no-op
    # unless we already created a ticket for this call, so they're safe to run
    # before the internal-only gate — external calls have no ticket to touch.
    _attach_extras(call_id, event)

    if state in CREATE_STATES:
        if TICKET_ON != "both" and direction != TICKET_ON:
            return {"ignored": f"direction {direction} not ticketed"}

        # Only ticket INTERNAL callers; the native integration handles external
        # ones, so ticketing them here would double up.
        contact_type = ((event.get("contact") or {}).get("type") or "").lower()
        if INTERNAL_ONLY and contact_type not in INTERNAL_CONTACT_TYPES:
            return {"ignored": f"external caller (contact.type={contact_type or 'n/a'}); "
                               "handled by native integration"}

        if store.get_ticket(call_id) is None:
            answered = state == "connected"
            voicemail = state in VOICEMAIL_STATES
            ticket_id = _create_ticket(event, answered=answered, voicemail=voicemail)
            store.save_ticket(call_id, ticket_id)
            # Attach anything already present on the creating event.
            _attach_extras(call_id, event)
            # A ticket born from a terminal state already has final duration.
            if state in TERMINAL_STATES:
                store.mark_enriched(call_id)
            return {"created": ticket_id, "answered": answered, "voicemail": voicemail}

        # Ticket already exists (created on 'connected'); fill in final details.
        if state in TERMINAL_STATES:
            _maybe_enrich(call_id, event)

    return {"ok": state}


def _attach_extras(call_id: str, event: dict):
    """Run all the after-the-fact attachers that no-op without an existing ticket."""
    if event.get("recap_summary"):
        _maybe_attach_recap(call_id, event)
    if event.get("voicemail_link") or event.get("transcription_text"):
        _maybe_attach_voicemail(call_id, event)


def _requester_id(event: dict):
    """Best-effort: match the internal caller to an existing Zendesk end-user
    by phone. For an internal desk the caller is almost always already a user."""
    contact = event.get("contact") or {}
    phone = contact.get("phone")
    if not phone:
        return None
    try:
        r = httpx.get(f"{ZBASE}/users/search.json", params={"query": phone},
                      auth=ZAUTH, timeout=10)
        r.raise_for_status()
        users = r.json().get("users", [])
        return users[0]["id"] if users else None
    except Exception as e:
        log.warning("requester lookup failed for %s: %s", phone, e)
        return None


def _assignee_id(event: dict):
    """For an ANSWERED call, the target is the agent who picked up (target.type
    == 'user'). Match them to a Zendesk agent by email so the ticket is assigned
    to whoever took the call. Returns None for non-user targets / no match, in
    which case the ticket just lands in the default group for someone to grab."""
    target = event.get("target") or {}
    if (target.get("type") or "").lower() != "user":
        return None
    email = target.get("email")
    if not email:
        return None
    try:
        r = httpx.get(f"{ZBASE}/users/search.json", params={"query": email},
                      auth=ZAUTH, timeout=10)
        r.raise_for_status()
        for u in r.json().get("users", []):
            if u.get("role") in ("agent", "admin"):
                return u["id"]
    except Exception as e:
        log.warning("assignee lookup failed for %s: %s", email, e)
    return None


def _comment(ticket_id: int, body: str, uploads=None):
    """Add a private comment to an existing ticket, optionally with file uploads
    (Zendesk upload tokens from _zendesk_upload)."""
    comment = {"body": body, "public": False}
    if uploads:
        comment["uploads"] = uploads
    r = httpx.put(f"{ZBASE}/tickets/{ticket_id}.json",
                  json={"ticket": {"comment": comment}}, auth=ZAUTH, timeout=15)
    r.raise_for_status()


def _audio_ext(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "wav" in ct:
        return "wav"
    if "ogg" in ct:
        return "ogg"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return "m4a"
    return "mp3"


def _download_voicemail(link: str):
    """Fetch the voicemail audio from Dialpad. The link is a secureblob URL that
    requires the API token via bearer auth. Returns (bytes, content_type)."""
    r = httpx.get(link, headers={"Authorization": f"Bearer {DIALPAD_API_TOKEN}"},
                  timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "audio/mpeg")


def _zendesk_upload(filename: str, data: bytes, content_type: str) -> str:
    """Upload bytes to Zendesk and return the upload token to attach to a comment."""
    r = httpx.post(f"{ZBASE}/uploads.json", params={"filename": filename},
                   content=data,
                   headers={"Content-Type": content_type or "application/binary"},
                   auth=ZAUTH, timeout=60)
    r.raise_for_status()
    return r.json()["upload"]["token"]


def _create_ticket(event: dict, answered: bool, voicemail: bool = False) -> int:
    contact = event.get("contact") or {}
    caller = contact.get("name") or contact.get("phone") or "Unknown caller"
    target = (event.get("target") or {}).get("name") or "IT"
    if voicemail:
        status_word, status_desc = "Voicemail", "voicemail"
    elif answered:
        status_word, status_desc = "Call", "answered"
    else:
        status_word, status_desc = "Missed call", "missed / no voicemail"

    body = (
        f"Auto-created from a Dialpad internal call.\n\n"
        f"Caller: {caller} ({contact.get('phone', 'n/a')})\n"
        f"Direction: {event.get('direction')}\n"
        f"Status: {status_desc}\n"
        f"Dialpad call_id: {event.get('call_id')}"
    )
    if not answered and event.get("duration"):
        body += f"\nDuration: {round(event['duration'] / 1000)}s"

    tags = ["dialpad", "internal-call"]
    if voicemail:
        tags += ["voicemail", "missed-call"]
    elif not answered:
        tags += ["missed-call"]

    ticket = {
        "subject": f"{status_word} from {caller} -> {target}",
        "comment": {"body": body, "public": False},
        "tags": tags,
    }
    rid = _requester_id(event)
    if rid:
        ticket["requester_id"] = rid
    # Answered calls go to whoever picked up; voicemails/missed stay unassigned
    # so they fall into the default (Support) group for someone to grab.
    if answered:
        aid = _assignee_id(event)
        if aid:
            ticket["assignee_id"] = aid
    if DEFAULT_GROUP_ID:
        ticket["group_id"] = int(DEFAULT_GROUP_ID)

    r = httpx.post(f"{ZBASE}/tickets.json", json={"ticket": ticket},
                   auth=ZAUTH, timeout=15)
    r.raise_for_status()
    tid = r.json()["ticket"]["id"]
    log.info("created ticket %s (answered=%s) for call %s", tid, answered,
             event.get("call_id"))
    return tid


def _maybe_enrich(call_id: str, event: dict):
    """Once the call ends, add final duration/disposition to a ticket that was
    created at answer time (when those weren't known yet)."""
    ticket_id = store.get_ticket(call_id)
    if not ticket_id or store.is_enriched(call_id):
        return
    secs = round((event.get("duration") or 0) / 1000)
    dispo = event.get("call_dispositions")
    body = f"Call ended. Duration: {secs}s"
    if dispo:
        body += f"\nDisposition: {dispo}"
    _comment(ticket_id, body)
    store.mark_enriched(call_id)
    log.info("enriched ticket %s (call %s)", ticket_id, call_id)


def _maybe_attach_voicemail(call_id: str, event: dict):
    """Attach the voicemail recording link and/or transcription to the ticket.
    These arrive on later events (voicemail_uploaded carries voicemail_link; the
    transcription state carries transcription_text), so each is attached once,
    independently, when it shows up. No recording for an internal voicemail =
    the whole reason this exists, since the native integration skips them."""
    ticket_id = store.get_ticket(call_id)
    if not ticket_id:
        return
    link = event.get("voicemail_link")
    if link and not store.vm_link_done(call_id):
        token = None
        if ATTACH_VOICEMAIL_AUDIO and DIALPAD_API_TOKEN:
            try:
                data, ctype = _download_voicemail(link)
                token = _zendesk_upload(
                    f"voicemail-{call_id}.{_audio_ext(ctype)}", data, ctype)
            except Exception as e:
                log.warning("voicemail audio fetch/upload failed (call %s), "
                            "falling back to link: %s", call_id, e)
        if token:
            _comment(ticket_id, "Voicemail recording attached.", uploads=[token])
        else:
            _comment(ticket_id, f"Voicemail recording:\n{link}")
        store.mark_vm_link(call_id)
        log.info("attached voicemail recording to ticket %s (call %s)", ticket_id, call_id)
    text = (event.get("transcription_text") or "").strip()
    if text and not store.vm_transcript_done(call_id):
        _comment(ticket_id, f"Voicemail transcription:\n{text}")
        store.mark_vm_transcript(call_id)
        log.info("attached voicemail transcription to ticket %s (call %s)", ticket_id, call_id)


def _maybe_attach_recap(call_id: str, event: dict):
    """Attach Dialpad's AI recap (summary + outcome + action items) to the ticket.
    The recap lands on its own event after the call ends and can lag, so it's a
    second-phase update keyed on call_id — same pattern the recording used, but
    recaps need no recordings_export scope. Requires Dialpad Ai to be enabled."""
    ticket_id = store.get_ticket(call_id)
    if not ticket_id or store.recap_done(call_id):
        return
    summary = (event.get("recap_summary") or "").strip()
    if not summary:
        return
    lines = ["AI call recap (Dialpad):", "", summary]
    outcome = event.get("recap_outcome")
    if outcome:
        lines += ["", f"Outcome: {outcome}"]
    actions = event.get("recap_action_items") or []
    if isinstance(actions, str):
        actions = [actions]
    if actions:
        lines += ["", "Action items:"] + [f"- {a}" for a in actions]
    _comment(ticket_id, "\n".join(lines))
    store.mark_recap(call_id)
    log.info("attached AI recap to ticket %s (call %s)", ticket_id, call_id)
