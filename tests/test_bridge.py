"""Regression tests for the Dialpad -> Zendesk bridge.

Two-phase model: a ticket is created on `connected` when an agent actually answers
(date_connected + operator_call_id, or a direct target.type==user), and finalized
on `hangup` by appending the call length. Hangup also creates as a fallback if the
connected was missed. All legs of a call dedupe on master_call_id; the answerer is
assigned (resolving the operator leg for contact-center calls); requester is
matched/created from the caller. All Zendesk/Dialpad HTTP is mocked.

Run:  python -m pytest -q   (after pip install -r requirements-dev.txt)
"""

import os
import re

import pytest

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
    def __init__(self):
        self.posts = []           # ticket creates only
        self.puts = []
        self.gets = []
        self.uploads = []
        self.created_users = []
        self.agents = {}           # email -> zendesk agent id
        self.users_by_phone = {}   # normalized phone -> {"id","phone"}
        self.operator_legs = {}    # dialpad call_id -> call json
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
        if "/api/v2/call/" in url:
            cid = url.rsplit("/", 1)[-1]
            return FakeResp(self.operator_legs.get(cid, {}))
        return FakeResp(content=b"FAKE-AUDIO-BYTES",
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
        self.posts.append((url, kwargs))
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


def _answer(call_id="call-1", **extra):
    """A 'connected' event where an agent actually answered (direct: target=user)."""
    return _event("connected", call_id=call_id, date_connected=1, **extra)


def _cc_answer(call_id="LEG", master="M", op="OP1", **extra):
    """A contact-center 'connected': target is the call center, agent on op leg."""
    ev = _event("connected", call_id=call_id, date_connected=1,
                master_call_id=master, operator_call_id=op, **extra)
    ev["target"] = {"type": "call_center", "name": "IT - AS400 (Don't Call)"}
    return ev


def post(client, event):
    return client.post("/dialpad/webhook", json=event)


def _assignees(fake):
    return [kw["json"]["ticket"]["assignee_id"] for _u, kw in fake.puts
            if "assignee_id" in kw["json"]["ticket"]]


def _subject_of(fake, i=0):
    return fake.posts[i][1]["json"]["ticket"]["subject"]


# ---- creation gating -------------------------------------------------------

def test_agent_answer_creates_ticket_on_connected(client):
    r = post(client, _answer())
    assert "created" in r.json() and r.json()["answered"] is True
    assert len(client.fake.posts) == 1
    assert "missed-call" not in client.fake.posts[0][1]["json"]["ticket"]["tags"]


def test_connected_without_answer_creates_no_ticket(client):
    # No date_connected => caller hit the menu/queue but no agent picked up.
    ev = _event("connected")
    ev["target"] = {"type": "call_center", "name": "IT - All Agents"}
    r = post(client, ev)
    assert "ignored" in r.json()
    assert len(client.fake.posts) == 0


def test_unanswered_hangup_creates_no_ticket(client):
    r = post(client, _event("hangup", talk_time=0, duration=8000))
    assert "ignored" in r.json()
    assert len(client.fake.posts) == 0


def test_outbound_not_ticketed(client):
    r = post(client, _answer(direction="outbound"))
    assert "ignored" in r.json()
    assert len(client.fake.posts) == 0


def test_external_caller_skipped(client):
    ev = _answer()
    ev["contact"] = {"name": "Acme", "phone": "+18005550000", "type": "google"}
    r = post(client, ev)
    assert "ignored" in r.json() and "external" in r.json()["ignored"]
    assert len(client.fake.posts) == 0


def test_event_without_call_id_ignored(client):
    r = client.post("/dialpad/webhook", json={"state": "connected"})
    assert r.json() == {"ignored": "no call_id/state"}


# ---- two-phase + fallback --------------------------------------------------

def test_length_appended_on_hangup(client):
    post(client, _answer(master_call_id="M"))
    assert "·" not in _subject_of(client.fake)          # no length yet at answer
    post(client, _event("hangup", master_call_id="M", talk_time=1140000))  # 19 min
    subj_puts = [p for p in client.fake.puts if "subject" in p[1]["json"]["ticket"]]
    assert subj_puts and subj_puts[-1][1]["json"]["ticket"]["subject"].endswith("· 19 min")


def test_hangup_fallback_creates_if_connected_missed(client):
    # No connected received; the answered hangup must still make the ticket.
    r = post(client, _event("hangup", master_call_id="M", talk_time=88000,
                            date_connected=1, operator_call_id="OP"))
    assert "created" in r.json()
    assert len(client.fake.posts) == 1
    assert _subject_of(client.fake).endswith("· 1 min 28 sec")


def test_duplicate_connected_does_not_double_create(client):
    post(client, _answer(call_id="L1", master_call_id="M"))
    post(client, _answer(call_id="L2", master_call_id="M"))
    assert len(client.fake.posts) == 1


def test_transfer_hop_makes_no_extra_ticket(client):
    hop = _event("hangup", call_id="HOP", talk_time=0, master_call_id="M")
    hop["target"] = {"type": "call_center", "name": "IT - AS400"}
    post(client, hop)                                   # talk 0, no answer -> nothing
    assert len(client.fake.posts) == 0
    post(client, _answer(call_id="ANS", master_call_id="M"))
    assert len(client.fake.posts) == 1


# ---- assignment ------------------------------------------------------------

def test_direct_answer_assigned_to_agent(client):
    client.fake.agents = {"agent@demo.com": 555}
    ev = _answer()
    ev["target"] = {"type": "user", "name": "Agent Smith", "email": "agent@demo.com"}
    post(client, ev)
    assert _assignees(client.fake)[-1] == 555


def test_contact_center_assigns_via_operator_fetch(client, monkeypatch):
    monkeypatch.setattr(main, "DIALPAD_API_TOKEN", "dp")
    client.fake.agents = {"alex@bpiteam.com": 99}
    client.fake.operator_legs["OP1"] = {
        "target": {"type": "user", "name": "Alex Thompson", "email": "alex@bpiteam.com"}}
    post(client, _cc_answer())
    assert len(client.fake.posts) == 1
    assert _assignees(client.fake)[-1] == 99
    assert "Alex Thompson" in _subject_of(client.fake)


def test_last_answerer_owns_on_transfer(client, monkeypatch):
    monkeypatch.setattr(main, "DIALPAD_API_TOKEN", "dp")
    client.fake.agents = {"a@x.com": 1, "b@x.com": 2}
    client.fake.operator_legs = {
        "OPA": {"target": {"type": "user", "name": "A", "email": "a@x.com"}},
        "OPB": {"target": {"type": "user", "name": "B", "email": "b@x.com"}}}
    post(client, _cc_answer(call_id="L1", master="M", op="OPA"))
    post(client, _cc_answer(call_id="L2", master="M", op="OPB"))
    assert len(client.fake.posts) == 1            # one ticket
    assert _assignees(client.fake)[-1] == 2       # reassigned to the last answerer


def test_unresolvable_agent_leaves_unassigned(client):
    ev = _cc_answer()                              # no DIALPAD_API_TOKEN -> no fetch
    post(client, ev)
    assert len(client.fake.posts) == 1
    assert _assignees(client.fake) == []


# ---- subject ---------------------------------------------------------------

def test_subject_caller_and_agent(client):
    ev = _answer()
    ev["contact"] = {"name": "Walet Jan", "phone": "+12256036216", "type": "user"}
    ev["target"] = {"type": "user", "name": "Alex Thompson", "email": "a@bpiteam.com"}
    post(client, ev)
    assert _subject_of(client.fake) == "Dialpad call with Walet Jan — answered by Alex Thompson"


def test_voicemail_subject_and_unassigned(client):
    r = post(client, _event("voicemail_uploaded", voicemail_link="https://x/vm"))
    assert r.json()["voicemail"] is True
    t = client.fake.posts[0][1]["json"]["ticket"]
    assert t["subject"] == "Dialpad voicemail from Jane Tech"
    assert "assignee_id" not in t


# ---- recap / voicemail attachments ----------------------------------------

def test_recap_attaches(client):
    post(client, _answer(master_call_id="M"))
    post(client, _event("recap_summary", call_id="L2", master_call_id="M",
                        recap_summary="Reset their VPN token.",
                        recap_action_items=["Follow up on cert rotation"]))
    bodies = [p[1]["json"]["ticket"]["comment"]["body"] for p in client.fake.puts]
    assert any("Reset their VPN token" in b for b in bodies)
    assert any("Follow up on cert rotation" in b for b in bodies)


def test_recap_attached_only_once(client):
    post(client, _answer(master_call_id="M"))
    post(client, _event("recap_summary", master_call_id="M", recap_summary="s"))
    n = len(client.fake.puts)
    post(client, _event("recap_summary", master_call_id="M", recap_summary="s"))
    assert len(client.fake.puts) == n


def test_voicemail_recording_and_transcript(client):
    r = post(client, _event("voicemail_uploaded", voicemail_link="https://dialpad.com/vm/xyz"))
    assert r.json()["voicemail"] is True
    t = client.fake.posts[0][1]["json"]["ticket"]
    assert "voicemail" in t["tags"] and "missed-call" in t["tags"]
    assert any("dialpad.com/vm/xyz" in p[1]["json"]["ticket"]["comment"]["body"]
               for p in client.fake.puts)
    post(client, _event("transcription", transcription_text="Laptop won't boot."))
    assert any("won't boot" in p[1]["json"]["ticket"]["comment"]["body"]
               for p in client.fake.puts)


def test_voicemail_audio_attached_as_file(client, monkeypatch):
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
    post(client, _event("voicemail_uploaded", voicemail_link="https://dialpad.com/vm/x"))
    n = len(client.fake.puts)
    post(client, _event("voicemail_uploaded", voicemail_link="https://dialpad.com/vm/x"))
    assert len(client.fake.puts) == n


# ---- requester (caller) ----------------------------------------------------

def test_unknown_caller_creates_customer(client):
    r = post(client, _answer())
    assert "created" in r.json()
    created = client.fake.created_users[0]
    assert created["payload"]["name"] == "Jane Tech"
    assert created["payload"]["phone"] == "+15551112222"
    assert "external_id" not in created["payload"]      # mergeable
    assert client.fake.posts[0][1]["json"]["ticket"]["requester_id"] == created["id"]


def test_existing_customer_matched_by_phone(client):
    client.fake.users_by_phone["5551112222"] = {"id": 777, "phone": "+1 (555) 111-2222"}
    post(client, _answer())
    assert client.fake.posts[0][1]["json"]["ticket"]["requester_id"] == 777
    assert client.fake.created_users == []


def test_unknown_caller_without_name_uses_phone(client):
    ev = _event("voicemail_uploaded", voicemail_link="https://x/vm")
    ev["contact"] = {"phone": "+15553334444", "type": "user"}
    post(client, ev)
    assert client.fake.created_users[0]["payload"]["name"] == "+15553334444"


# ---- config robustness -----------------------------------------------------

def test_cfg_strips_inline_comment(monkeypatch):
    monkeypatch.setenv("SOME_FLAG", "true   # comment")
    assert main._cfg("SOME_FLAG", "x") == "true"


def test_polluted_group_id_does_not_crash(client, monkeypatch):
    monkeypatch.setattr(main, "DEFAULT_GROUP_ID", "# optional: route auto-tickets")
    r = post(client, _answer())
    assert "created" in r.json()
    assert "group_id" not in client.fake.posts[0][1]["json"]["ticket"]


def test_numeric_group_id_applied(client, monkeypatch):
    monkeypatch.setattr(main, "DEFAULT_GROUP_ID", "12345")
    post(client, _answer())
    assert client.fake.posts[0][1]["json"]["ticket"]["group_id"] == 12345
