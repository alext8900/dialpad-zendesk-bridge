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
import re
import json
import logging

import jwt
import httpx
from fastapi import FastAPI, Request, HTTPException

from . import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# ---- Config (see .env.example) ---------------------------------------------
def _cfg(name: str, default: str = "") -> str:
    """Read an env var, tolerating an inline '# comment' accidentally left in a
    .env value. Docker Compose's env_file does NOT strip inline comments, so
    `KEY=value   # note` arrives as 'value   # note' — guard against that."""
    v = os.environ.get(name, default)
    if v is None:
        return default
    i = v.find(" #")
    if i != -1:
        v = v[:i]
    return v.strip()


DIALPAD_WEBHOOK_SECRET = _cfg("DIALPAD_WEBHOOK_SECRET")
ZENDESK_SUBDOMAIN = _cfg("ZENDESK_SUBDOMAIN") or os.environ["ZENDESK_SUBDOMAIN"]
ZENDESK_EMAIL = _cfg("ZENDESK_EMAIL") or os.environ["ZENDESK_EMAIL"]
ZENDESK_API_TOKEN = _cfg("ZENDESK_API_TOKEN") or os.environ["ZENDESK_API_TOKEN"]
TICKET_ON = _cfg("TICKET_ON", "inbound") or "inbound"   # inbound | outbound | both
DEFAULT_GROUP_ID = _cfg("ZENDESK_GROUP_ID")             # numeric string or "" (ignored)
# Dialpad API token (needs the 'recordings'/'recordings_export' scope) so the
# bridge can DOWNLOAD the voicemail audio and re-upload it to Zendesk as a real
# file attachment, like the native integration does. Without it, the bridge falls
# back to dropping the voicemail_link as a comment.
DIALPAD_API_TOKEN = _cfg("DIALPAD_API_TOKEN")
ATTACH_VOICEMAIL_AUDIO = _cfg("ATTACH_VOICEMAIL_AUDIO", "true").lower() == "true"
# Set true to log the full Dialpad event payload (for diagnosing field names).
DEBUG_PAYLOAD = _cfg("DEBUG_PAYLOAD", "false").lower() == "true"
# Minimum agent TALK time (seconds) for an answered call to make a ticket. We use
# talk_time (real agent conversation, excluding IVR/queue/ring) so menu-disconnects
# (talk_time 0) and pocket-dials are filtered. 0 = ticket any call with real talk.
MIN_TALK_SECONDS = int(_cfg("MIN_TALK_SECONDS", "0") or 0)
# States the bridge acts on for ticket CREATION. We create at hangup (talk_time is
# only known then) for answered calls, and on voicemail_uploaded for voicemails.
VOICEMAIL_STATES = {"voicemail", "voicemail_uploaded"}
CREATE_STATES = {"hangup", "voicemail", "voicemail_uploaded"}
# Only ticket INTERNAL (Dialpad-to-Dialpad) calls. External callers are already
# ticketed by Dialpad's native Zendesk integration, so handling them here too
# would double up. An internal caller has contact.type == "user"; external
# callers are local/google/nylas/microsoft. Override the type set if your tenant
# differs (e.g. include 'room'); set INTERNAL_ONLY=false to ticket everything.
INTERNAL_ONLY = _cfg("INTERNAL_ONLY", "true").lower() == "true"
INTERNAL_CONTACT_TYPES = {
    t.strip().lower()
    for t in _cfg("INTERNAL_CONTACT_TYPES", "user").split(",")
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

    contact = event.get("contact") or {}
    target = event.get("target") or {}
    # One real call rings/transfers through many legs, each with its own call_id,
    # but they ALL share master_call_id (present even across transfers). Key on
    # that so the whole call is ONE ticket. Fall back to entry_point_call_id, then
    # this leg's own id for simple direct calls.
    key = str(event.get("master_call_id") or event.get("entry_point_call_id") or call_id)
    talk_secs = round((event.get("talk_time") or 0) / 1000)
    log.info("event call_id=%s key=%s state=%s dir=%s contact=%s:%s target=%s:%s talk=%ss",
             call_id, key, state, direction, contact.get("type"), contact.get("name"),
             target.get("type"), target.get("name"), talk_secs)
    if DEBUG_PAYLOAD:
        log.info("payload call_id=%s %s", call_id, json.dumps(event, default=str))

    # Attach-only signals can arrive on their own event after the call ends
    # (AI recap, voicemail recording, voicemail transcription). They no-op
    # unless we already created a ticket for this call, so they're safe to run
    # before the internal-only gate — external calls have no ticket to touch.
    _attach_extras(key, event)

    if state in CREATE_STATES:
        if TICKET_ON != "both" and direction != TICKET_ON:
            return {"ignored": f"direction {direction} not ticketed"}

        # Only ticket INTERNAL callers; the native integration handles external
        # ones, so ticketing them here would double up.
        contact_type = (contact.get("type") or "").lower()
        if INTERNAL_ONLY and contact_type not in INTERNAL_CONTACT_TYPES:
            log.info("skip: contact.type=%r not internal (internal=%s) call_id=%s",
                     contact_type, sorted(INTERNAL_CONTACT_TYPES), call_id)
            return {"ignored": f"external caller (contact.type={contact_type or 'n/a'}); "
                               "handled by native integration"}

        # Ticket when an agent actually TALKED (talk_time, set at hangup, excludes
        # IVR/queue/ring time) or a VOICEMAIL was left. Menu-disconnects, abandons,
        # and transfer hops have talk_time 0 -> filtered out.
        voicemail = state in VOICEMAIL_STATES
        answered = state == "hangup" and talk_secs > 0 and talk_secs >= MIN_TALK_SECONDS

        if store.get_ticket(key) is not None:
            return {"ok": "deduped"}  # another leg of a call we already ticketed
        if not (answered or voicemail):
            return {"ignored": f"not ticketed (talk={talk_secs}s, state={state})"}

        agent = _resolve_agent(event) if answered else None
        ticket_id, subject = _create_ticket(event, answered=answered, voicemail=voicemail,
                                            agent=agent, talk_secs=talk_secs)
        store.save_ticket(key, ticket_id, subject)
        _attach_extras(key, event)
        if agent:
            _assign(key, ticket_id, agent)
        return {"created": ticket_id, "answered": answered, "voicemail": voicemail}

    return {"ok": state}


def _attach_extras(call_id: str, event: dict):
    """Run all the after-the-fact attachers that no-op without an existing ticket."""
    if event.get("recap_summary"):
        _maybe_attach_recap(call_id, event)
    if event.get("voicemail_link") or event.get("transcription_text"):
        _maybe_attach_voicemail(call_id, event)


def _norm_phone(p: str) -> str:
    """Digits only, last 10 (US), for tolerant phone comparison across formats."""
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d


def _find_user_by_phone(phone: str):
    """Return the id of an existing Zendesk user whose PHONE actually matches,
    else None. We require a real phone-field match (not just a fuzzy search hit)
    so the call doesn't get attached to the wrong person."""
    want = _norm_phone(phone)
    if not want:
        return None
    try:
        r = httpx.get(f"{ZBASE}/users/search.json", params={"query": phone},
                      auth=ZAUTH, timeout=10)
        r.raise_for_status()
        for u in r.json().get("users", []):
            if _norm_phone(u.get("phone")) == want:
                return u["id"]
    except Exception as e:
        log.warning("user phone lookup failed for %s: %s", phone, e)
    return None


def _create_end_user(name: str, phone: str = None, email: str = None):
    """Create (or update) a Zendesk end-user 'customer' for the caller, mirroring
    the native integration when no match exists. Deliberately does NOT set an
    external_id: an external_id makes the contact impossible to merge later, which
    is a problem when Dialpad has stale info (e.g. someone's email changed) and a
    duplicate gets made. Repeat callers are deduped by the phone search instead."""
    user = {"name": name, "role": "end-user", "verified": True}
    if phone:
        user["phone"] = phone
    if email:
        user["email"] = email
    try:
        r = httpx.post(f"{ZBASE}/users/create_or_update.json",
                       json={"user": user}, auth=ZAUTH, timeout=15)
        r.raise_for_status()
        return r.json()["user"]["id"]
    except Exception as e:
        log.warning("end-user create failed for %s / %s: %s", name, phone, e)
        return None


def _requester_id(event: dict):
    """Resolve the ticket requester from the caller, like the native integration:
    match an existing customer by phone, else create a new customer from the
    caller ID (name) or the phone number. Returns None only when we have nothing
    to go on — and only then does Zendesk fall back to the API account."""
    contact = event.get("contact") or {}
    phone = contact.get("phone")
    name = contact.get("name")
    email = contact.get("email")
    uid = _find_user_by_phone(phone) if phone else None
    if uid:
        return uid
    if not (name or phone or email):
        return None
    return _create_end_user(name or phone, phone, email)


def _fetch_call(call_id):
    """GET a single Dialpad call's details (used to resolve the operator leg)."""
    if not DIALPAD_API_TOKEN:
        return None
    try:
        r = httpx.get(f"https://dialpad.com/api/v2/call/{call_id}",
                      headers={"Authorization": f"Bearer {DIALPAD_API_TOKEN}"},
                      timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("fetch call %s failed: %s", call_id, e)
        return None


def _resolve_agent(event: dict):
    """Find the agent who answered. For a direct call the target IS the agent
    (target.type == user). For a contact-center call the target is the call center
    and the agent is a separate operator leg (operator_call_id) we must fetch.
    Returns {'name','email'} or None."""
    t = event.get("target") or {}
    if (t.get("type") or "").lower() == "user" and t.get("email"):
        return {"name": t.get("name"), "email": t.get("email")}
    op = event.get("operator_call_id")
    if op:
        leg = _fetch_call(op)
        lt = (leg or {}).get("target") or {}
        if (lt.get("type") or "").lower() == "user" and lt.get("email"):
            return {"name": lt.get("name"), "email": lt.get("email")}
    return None


def _zendesk_agent_id(email: str):
    """Match an email to a Zendesk agent/admin user id."""
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
        log.warning("agent lookup failed for %s: %s", email, e)
    return None


def _update_ticket(ticket_id: int, fields: dict):
    """PUT arbitrary ticket fields (e.g. assignee_id)."""
    r = httpx.put(f"{ZBASE}/tickets/{ticket_id}.json",
                  json={"ticket": fields}, auth=ZAUTH, timeout=15)
    r.raise_for_status()


def _assign(key: str, ticket_id: int, agent: dict):
    """Assign the ticket to the resolved answering agent (matched in Zendesk by
    email). No match -> leave unassigned in the default (Support) group."""
    aid = _zendesk_agent_id(agent.get("email"))
    if not aid:
        return
    _update_ticket(ticket_id, {"assignee_id": aid})
    store.mark_assigned(key)
    log.info("assigned ticket %s to agent %s <%s> (call %s)",
             ticket_id, aid, agent.get("email"), key)


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


def _fmt_duration(ms) -> str:
    secs = round((ms or 0) / 1000)
    if secs < 60:
        return f"{secs} sec"
    m, s = divmod(secs, 60)
    return f"{m} min" if s == 0 else f"{m} min {s} sec"


def _caller_name(event: dict) -> str:
    c = event.get("contact") or {}
    return c.get("name") or c.get("phone") or "Unknown caller"


def _receiver_name(event: dict, agent: dict = None) -> str:
    """Who took it: the resolved agent if we have one, else the queue/target name,
    prefixed with the queue when both are known ('IT - AS400 / Alex Thompson')."""
    queue = (event.get("target") or {}).get("name")
    agent_name = (agent or {}).get("name")
    if agent_name and queue and queue != agent_name:
        return f"{queue} / {agent_name}"
    return agent_name or queue or "IT"


def _subject(event: dict, answered: bool, voicemail: bool,
             agent: dict = None, talk_secs: int = 0) -> str:
    caller = _caller_name(event)
    if voicemail:
        return f"Dialpad voicemail from {caller}"
    base = f"Dialpad call with {caller} — answered by {_receiver_name(event, agent)}"
    return f"{base} · {_fmt_duration(talk_secs * 1000)}" if talk_secs else base


def _render_body(event: dict, answered: bool, voicemail: bool,
                 agent: dict = None, talk_secs: int = 0) -> str:
    contact = event.get("contact") or {}
    target = event.get("target") or {}
    lines = [_subject(event, answered, voicemail, agent, talk_secs), "",
             f"Direction: {(event.get('direction') or 'n/a').title()}"]
    if answered and talk_secs:
        lines.append(f"Talk time: {_fmt_duration(talk_secs * 1000)}")
    lines += ["", "— Caller —", f"Name: {contact.get('name') or '—'}"]
    if contact.get("email"):
        lines.append(f"Email: {contact['email']}")
    if contact.get("phone"):
        lines.append(f"Phone: {contact['phone']}")
    lines += ["", "— Receiver —", f"Name: {_receiver_name(event, agent)}"]
    recv_email = (agent or {}).get("email") or target.get("email")
    if recv_email:
        lines.append(f"Email: {recv_email}")
    if target.get("phone"):
        lines.append(f"Phone: {target['phone']}")
    if voicemail:
        lines += ["", "(voicemail recording & transcription attached below)"]
    lines += ["", f"Dialpad call id: {event.get('call_id')}"]
    return "\n".join(lines)


def _create_ticket(event: dict, answered: bool, voicemail: bool = False,
                   agent: dict = None, talk_secs: int = 0):
    """Returns (ticket_id, subject). Created at hangup, so the call length
    (talk_secs) is known and baked into the subject right away."""
    subject = _subject(event, answered, voicemail, agent, talk_secs)
    tags = ["dialpad", "internal-call"]
    if voicemail:
        tags += ["voicemail", "missed-call"]

    ticket = {
        "subject": subject,
        "comment": {"body": _render_body(event, answered, voicemail, agent, talk_secs),
                    "public": False},
        "tags": tags,
    }
    rid = _requester_id(event)
    if rid:
        ticket["requester_id"] = rid
    if DEFAULT_GROUP_ID.isdigit():
        ticket["group_id"] = int(DEFAULT_GROUP_ID)

    r = httpx.post(f"{ZBASE}/tickets.json", json={"ticket": ticket},
                   auth=ZAUTH, timeout=15)
    r.raise_for_status()
    tid = r.json()["ticket"]["id"]
    log.info("created ticket %s (answered=%s voicemail=%s talk=%ss) for call %s",
             tid, answered, voicemail, talk_secs, event.get("call_id"))
    return tid, subject


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
