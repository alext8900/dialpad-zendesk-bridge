"""Regression tests for the Dialpad -> Zendesk bridge.

Exercises POST /dialpad/webhook end to end: tickets are created at hangup for
calls an agent actually TALKED on (talk_time), or on voicemail; all legs of one
call dedupe on master_call_id; the answering agent is assigned (resolving the
operator leg when needed); requester is matched/created from the caller. All
Zendesk/Dialpad HTTP is mocked — no network, no creds.

Run:  python -m pytest -q   (after pip install -r requirements-dev.txt)
"""

import os
import re

import pytest

# Required env must exist BEFORE app.main is imported (module-level os.environ[...]).
os.environ.setdefault("ZENDESK_SUBDOMAIN", "demo")
os.environ.setdefault("ZENDESK_EMAIL", "it@demo.com")
os.environ.setdefault("ZENDESK_API_TOKEN", "tok")
os.environ.pop("DIALPAD_WEBHOOK_SECRET", None)

from fastapi.testclient import TestClient

from app import main, store


class FakeResp:
    def __init__(self, payload=None, content=b"", headers=None):
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeHttpx:
    """Records calls and returns canned responses. `posts` holds ONLY ticket
    creates; user-creates and uploads are tracked separately."""

    def __init__(self):
        self.posts = []           # ticket creates only
        self.puts = []
        self.gets = []
        self.uploads = []
        self.created_users = []    # [{"id":..., "payload":...}]
        self.agents = {}           # email -> zendesk agent id
        self.users_by_phone = {}   # normalized phone -> {"id":..., "phone":...}
        self.operator_legs = {}    # dialpad call_id -> call json (for operator fetch)
        self._next_id = 100

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if "users/search" in url:
            q = (kwargs.get("params") or {}).get("query", "")
            if q in self.agents:
                return FakeResp({"users": [{"id": self.agents[q], "role": "agent"}]})
            norm = re.sub(r"\D", "", q)[-10:]
            if norm and norm in self.users_by_phone:
                return FakeResp({"users": [self.users_by_phone[norm]]})
            return FakeResp({"users": []})
        if "/api/v2/call/" in url:                       # operator-leg fetch
            cid = url.rsplit("/", 1)[-1]
            return FakeResp(self.operator_legs.get(cid, {}))
        return FakeResp(content=b"FAKE-AUDIO-BYTES",      # voicemail audio download
                        headers={"content-type": "audio/mpeg"})

    def post(self, url, **kwargs):
        if "uploads.json" in url:
            self.uploads.append((url, kwargs))
            return FakeResp({"upload": {"token": f"uptoken-{len(self.uploads)}"}})
        if "users/create_or_update.json" in url:
            self._next_id += 1
            self.created_users.append({"id": self._next_id,
                                       "payload": kwargs["json"]["user"]})
            return FakeResp({"user": {"id": self._next_id}})
        self.posts.append((url, kwargs))                 # tickets.json
        self._next_id += 1
        return FakeResp({"ticket": {"id": self._next_id}})

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return FakeResp({"ticket": {}})


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr(store, "DB_PATH", str(db))
    store.init()
    fake = FakeHttpx()
    monkeypatch.setattr(main, "httpx", fake)
    c = TestClient(main.app)
    c.fake = fake
    return c


def _event(state, call_id="call-1", direction="inbound", **extra):
    ev = {
        "call_id": call_id,
        "state": state,
        "direction": direction,
        "contact": {"name": "Jane Tech", "phone": "+15551112222", "type": "user"},
        "target": {"type": "user", "name": "IT Agent"},
    }
    ev.update(extra)
    return ev


def _answered(call_id="call-1", talk_time=120000, **extra):
    """A hangup event for a call an agent talked on (talk_time ms)."""
    return _event("hangup", call_id=call_id, talk_time=talk_time, **extra)


def post(client, event):
    return client.post("/dialpad/webhook", json=event)


def _assignee_in_puts(fake):
    for _url, kw in fake.puts:
        aid = kw["json"]["ticket"].get("assignee_id")
        if aid is not None:
            return aid
    return None


def _subject_of(fake, i=0):
    return fake.posts[i][1]["json"]["ticket"]["subject"]


# ---- creation gating -------------------------------------------------------

def test_answered_call_creates_one_ticket(client):
    r = post(client, _answered())
    assert r.json()["answered"] is True
    assert len(client.fake.posts) == 1
    assert "missed-call" not in client.fake.posts[0][1]["json"]["ticket"]["tags"]


def test_connected_event_does_not_create_ticket(client):
    # We create at hangup (talk_time known then), not on connect.
    r = post(client, _event("connected"))
    assert "created" not in r.json()
    assert len(client.fake.posts) == 0


def test_unanswered_hangup_creates_no_ticket(client):
    # Rang/menu-disconnect: talk_time 0 -> no agent talked -> no ticket.
    r = post(client, _event("hangup", talk_time=0, duration=8000))
    assert "ignored" in r.json()
    assert len(client.fake.posts) == 0


def test_min_talk_threshold_filters_short_calls(client, monkeypatch):
    monkeypatch.setattr(main, "MIN_TALK_SECONDS", 10)
    post(client, _answered(talk_time=5000))             # 5s < 10 -> filtered
    assert len(client.fake.posts) == 0
    post(client, _answered(call_id="c2", talk_time=15000))   # 15s -> ticket
    assert len(client.fake.posts) == 1


def test_outbound_call_not_ticketed(client):
    r = post(client, _answered(direction="outbound"))
    assert "ignored" in r.json()
    assert len(client.fake.posts) == 0


def test_external_caller_is_skipped(client):
    ev = _answered()
    ev["contact"] = {"name": "Acme Corp", "phone": "+18005550000", "type": "google"}
    r = post(client, ev)
    assert "ignored" in r.json() and "external" in r.json()["ignored"]
    assert len(client.fake.posts) == 0


def test_event_without_call_id_is_ignored(client):
    r = client.post("/dialpad/webhook", json={"state": "hangup"})
    assert r.json() == {"ignored": "no call_id/state"}
    assert len(client.fake.posts) == 0


# ---- dedup across legs / transfers ----------------------------------------

def test_call_legs_dedup_on_master_call_id(client):
    post(client, _answered(call_id="LEG1", master_call_id="M"))
    post(client, _answered(call_id="LEG2", master_call_id="M"))
    assert len(client.fake.posts) == 1


def test_transfer_hop_does_not_make_extra_ticket(client):
    hop = _event("hangup", call_id="HOP", talk_time=0, master_call_id="M")
    hop["target"] = {"type": "call_center", "name": "IT - AS400"}
    post(client, hop)                                   # talk 0 -> nothing
    assert len(client.fake.posts) == 0
    post(client, _answered(call_id="ANS", master_call_id="M"))  # answered -> 1
    assert len(client.fake.posts) == 1


# ---- assignment ------------------------------------------------------------

def test_answered_call_assigned_to_agent(client):
    # Direct call: target IS the agent (type user + email).
    client.fake.agents = {"agent@demo.com": 555}
    ev = _answered()
    ev["target"] = {"type": "user", "name": "Agent Smith", "email": "agent@demo.com"}
    post(client, ev)
    assert _assignee_in_puts(client.fake) == 555


def test_contact_center_assigns_via_operator_fetch(client, monkeypatch):
    # CC call: target is the call center; the agent is a separate operator leg we
    # fetch via operator_call_id. One ticket, assigned to the fetched agent.
    monkeypatch.setattr(main, "DIALPAD_API_TOKEN", "dp")
    client.fake.agents = {"alex@bpiteam.com": 99}
    client.fake.operator_legs["OP1"] = {
        "target": {"type": "user", "name": "Alex Thompson", "email": "alex@bpiteam.com"}}
    ev = _answered(call_id="LEG", talk_time=88000, master_call_id="M",
                   operator_call_id="OP1")
    ev["target"] = {"type": "call_center", "name": "IT - AS400 (Don't Call)"}
    post(client, ev)
    assert len(client.fake.posts) == 1
    assert _assignee_in_puts(client.fake) == 99
    assert "Alex Thompson" in _subject_of(client.fake)   # queue / agent in subject


def test_unresolvable_agent_leaves_ticket_unassigned(client):
    # CC call but no token/operator info -> ticket created, just unassigned.
    ev = _answered(master_call_id="M")
    ev["target"] = {"type": "call_center", "name": "IT - AS400"}
    post(client, ev)
    assert len(client.fake.posts) == 1
    assert _assignee_in_puts(client.fake) is None


# ---- subject / length ------------------------------------------------------

def test_subject_includes_caller_agent_and_length(client):
    ev = _answered(talk_time=1140000)                   # 19 min
    ev["contact"] = {"name": "Walet Jan", "phone": "+12256036216", "type": "user"}
    ev["target"] = {"type": "user", "name": "Alex Thompson", "email": "a@bpiteam.com"}
    post(client, ev)
    assert _subject_of(client.fake) == \
        "Dialpad call with Walet Jan — answered by Alex Thompson · 19 min"


def test_short_answered_length_in_seconds(client):
    post(client, _answered(talk_time=42000))
    assert _subject_of(client.fake).endswith("· 42 sec")


def test_voicemail_subject_and_unassigned(client):
    r = post(client, _event("voicemail_uploaded", voicemail_link="https://x/vm"))
    assert r.json()["voicemail"] is True
    t = client.fake.posts[0][1]["json"]["ticket"]
    assert t["subject"] == "Dialpad voicemail from Jane Tech"
    assert "assignee_id" not in t


# ---- recap / voicemail attachments ----------------------------------------

def test_recap_event_attaches_summary(client):
    post(client, _answered(master_call_id="M"))
    post(client, _event("recap_summary", call_id="LEG2", master_call_id="M",
                         recap_summary="Caller couldn't reach the VPN; reset token.",
                         recap_action_items=["Follow up on VPN cert rotation"]))
    bodies = [p[1]["json"]["ticket"]["comment"]["body"] for p in client.fake.puts]
    assert any("couldn't reach the VPN" in b for b in bodies)
    assert any("Follow up on VPN cert rotation" in b for b in bodies)


def test_recap_attached_only_once(client):
    post(client, _answered(master_call_id="M"))
    post(client, _event("recap_summary", master_call_id="M", recap_summary="s"))
    n = len(client.fake.puts)
    post(client, _event("recap_summary", master_call_id="M", recap_summary="s"))
    assert len(client.fake.puts) == n


def test_voicemail_creates_ticket_with_recording_and_transcript(client):
    r = post(client, _event("voicemail_uploaded",
                            voicemail_link="https://dialpad.com/vm/xyz"))
    body = r.json()
    assert body["voicemail"] is True and "created" in body
    ticket = client.fake.posts[0][1]["json"]["ticket"]
    assert "voicemail" in ticket["tags"] and "missed-call" in ticket["tags"]
    assert any("dialpad.com/vm/xyz" in p[1]["json"]["ticket"]["comment"]["body"]
               for p in client.fake.puts)
    post(client, _event("transcription",
                        transcription_text="My laptop won't boot, please call back."))
    assert any("won't boot" in p[1]["json"]["ticket"]["comment"]["body"]
               for p in client.fake.puts)


def test_voicemail_audio_downloaded_and_attached_as_file(client, monkeypatch):
    monkeypatch.setattr(main, "DIALPAD_API_TOKEN", "dp-token")
    monkeypatch.setattr(main, "ATTACH_VOICEMAIL_AUDIO", True)
    post(client, _event("voicemail_uploaded",
                        voicemail_link="https://dialpad.com/secureblob/voicemail/abc"))
    assert any("secureblob" in g[0] for g in client.fake.gets)
    assert len(client.fake.uploads) == 1
    assert client.fake.uploads[0][1]["content"] == b"FAKE-AUDIO-BYTES"
    assert any(p[1]["json"]["ticket"]["comment"].get("uploads") == ["uptoken-1"]
               for p in client.fake.puts)


def test_voicemail_recording_attached_only_once(client):
    post(client, _event("voicemail_uploaded", voicemail_link="https://dialpad.com/vm/xyz"))
    n = len(client.fake.puts)
    post(client, _event("voicemail_uploaded", voicemail_link="https://dialpad.com/vm/xyz"))
    assert len(client.fake.puts) == n


# ---- requester (caller) ----------------------------------------------------

def test_unknown_caller_creates_customer_as_requester(client):
    r = post(client, _answered())
    assert "created" in r.json()
    assert len(client.fake.created_users) == 1
    created = client.fake.created_users[0]
    assert created["payload"]["name"] == "Jane Tech"
    assert created["payload"]["phone"] == "+15551112222"
    assert "external_id" not in created["payload"]      # mergeable (no external_id)
    assert client.fake.posts[0][1]["json"]["ticket"]["requester_id"] == created["id"]


def test_existing_customer_matched_by_phone_is_requester(client):
    client.fake.users_by_phone["5551112222"] = {"id": 777, "phone": "+1 (555) 111-2222"}
    post(client, _answered())
    assert client.fake.posts[0][1]["json"]["ticket"]["requester_id"] == 777
    assert client.fake.created_users == []


def test_unknown_caller_without_name_falls_back_to_phone(client):
    ev = _event("voicemail_uploaded", voicemail_link="https://x/vm")
    ev["contact"] = {"phone": "+15553334444", "type": "user"}
    post(client, ev)
    assert client.fake.created_users[0]["payload"]["name"] == "+15553334444"


# ---- config robustness -----------------------------------------------------

def test_cfg_strips_inline_comment_left_in_env(monkeypatch):
    monkeypatch.setenv("SOME_FLAG", "true   # only ticket internal")
    assert main._cfg("SOME_FLAG", "x") == "true"
    monkeypatch.setenv("SOME_FLAG", "")
    assert main._cfg("SOME_FLAG", "x") == ""


def test_polluted_group_id_does_not_crash(client, monkeypatch):
    monkeypatch.setattr(main, "DEFAULT_GROUP_ID", "# optional: route auto-tickets")
    r = post(client, _answered())
    assert "created" in r.json()
    assert "group_id" not in client.fake.posts[0][1]["json"]["ticket"]


def test_numeric_group_id_is_applied(client, monkeypatch):
    monkeypatch.setattr(main, "DEFAULT_GROUP_ID", "12345")
    post(client, _answered())
    assert client.fake.posts[0][1]["json"]["ticket"]["group_id"] == 12345
