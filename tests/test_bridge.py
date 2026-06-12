"""Regression tests for the Dialpad -> Zendesk bridge state machine.

These exercise the observable behavior of POST /dialpad/webhook end to end:
ticket-on-answer, dedup, the missed-call safety net, hangup enrichment, and
recording attachment. All Zendesk/Dialpad HTTP traffic is mocked, so the suite
runs with no network and no real credentials.

Run:  python -m pytest -q   (after pip install -r requirements-dev.txt)
"""

import importlib
import os
import re

import pytest

# Required env must exist BEFORE app.main is imported (module-level os.environ[...]).
os.environ.setdefault("ZENDESK_SUBDOMAIN", "demo")
os.environ.setdefault("ZENDESK_EMAIL", "it@demo.com")
os.environ.setdefault("ZENDESK_API_TOKEN", "tok")
# No DIALPAD_WEBHOOK_SECRET -> the webhook accepts plain JSON (no JWT needed).
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
    creates; user-creates and uploads are tracked separately so ticket-count
    assertions stay clean.

    - GET  .../users/search.json   -> agent (by email) / customer (by phone) / none
    - GET  dialpad secureblob link -> fake audio bytes
    - POST .../tickets.json        -> a new ticket with an incrementing id
    - POST .../users/create_or_update.json -> a new customer
    - POST .../uploads.json        -> a fake upload token
    - PUT  .../tickets/{id}.json   -> the enrich / recap / voicemail updates
    """

    def __init__(self):
        self.posts = []          # ticket creates only
        self.puts = []
        self.gets = []
        self.uploads = []
        self.created_users = []   # [{"id":..., "payload":...}]
        self.agents = {}          # email -> zendesk agent id (assignee lookup)
        self.users_by_phone = {}  # normalized phone -> {"id":..., "phone":...}
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
        # voicemail audio download
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
        # tickets.json
        self.posts.append((url, kwargs))
        self._next_id += 1
        return FakeResp({"ticket": {"id": self._next_id}})

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return FakeResp({"ticket": {}})


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Fresh SQLite state per test so dedup memory never leaks across tests.
    db = tmp_path / "state.db"
    monkeypatch.setattr(store, "DB_PATH", str(db))
    store.init()

    fake = FakeHttpx()
    monkeypatch.setattr(main, "httpx", fake)

    c = TestClient(main.app)
    c.fake = fake  # stash for assertions
    return c


def _event(state, call_id="call-1", direction="inbound", **extra):
    ev = {
        "call_id": call_id,
        "state": state,
        "direction": direction,
        "contact": {"name": "Jane Tech", "phone": "+15551112222", "type": "user"},
        "target": {"name": "IT Help Desk"},
    }
    ev.update(extra)
    return ev


def post(client, event):
    return client.post("/dialpad/webhook", json=event)


def test_answered_call_creates_one_ticket_on_connected(client):
    r = post(client, _event("connected"))
    assert r.json()["answered"] is True
    assert len(client.fake.posts) == 1  # exactly one ticket created
    # Answered tickets are NOT tagged missed-call.
    ticket = client.fake.posts[0][1]["json"]["ticket"]
    assert "missed-call" not in ticket["tags"]


def test_answered_call_assigned_to_agent_who_picked_up(client):
    # On answer, target is the agent (target.type=user); assign by email match.
    client.fake.agents = {"agent@demo.com": 555}
    ev = _event("connected")
    ev["target"] = {"type": "user", "name": "Agent Smith", "email": "agent@demo.com"}
    post(client, ev)
    ticket = client.fake.posts[0][1]["json"]["ticket"]
    assert ticket["assignee_id"] == 555


def test_voicemail_left_unassigned_for_the_group(client):
    # Voicemails get no assignee -> default (Support) group.
    r = post(client, _event("voicemail_uploaded", voicemail_link="https://x/vm"))
    assert r.json()["voicemail"] is True
    ticket = client.fake.posts[0][1]["json"]["ticket"]
    assert "assignee_id" not in ticket


def test_duplicate_connected_does_not_create_second_ticket(client):
    post(client, _event("connected"))
    post(client, _event("connected"))  # e.g. a transfer / hold re-connect
    assert len(client.fake.posts) == 1  # still just one ticket


def test_hangup_after_connected_enriches_not_recreates(client):
    post(client, _event("connected"))
    post(client, _event("hangup", duration=42000))
    assert len(client.fake.posts) == 1          # no new ticket
    assert len(client.fake.puts) == 1           # one enrichment update
    assert "42s" in client.fake.puts[0][1]["json"]["ticket"]["comment"]["body"]


def test_missed_call_creates_ticket_tagged_missed(client):
    # Rings out / voicemail: hangup arrives with no prior 'connected'.
    r = post(client, _event("hangup", duration=8000))
    body = r.json()
    assert "created" in body and body["answered"] is False
    ticket = client.fake.posts[0][1]["json"]["ticket"]
    assert "missed-call" in ticket["tags"]


def test_recap_event_attaches_summary(client):
    post(client, _event("connected"))
    post(client, _event("recap_summary",
                         recap_summary="Caller couldn't reach the VPN; reset their token.",
                         recap_action_items=["Follow up on VPN cert rotation"]))
    bodies = [p[1]["json"]["ticket"]["comment"]["body"] for p in client.fake.puts]
    assert any("couldn't reach the VPN" in b for b in bodies)
    assert any("Follow up on VPN cert rotation" in b for b in bodies)


def test_recap_attached_only_once(client):
    post(client, _event("connected"))
    post(client, _event("recap_summary", recap_summary="summary text"))
    puts_after_first = len(client.fake.puts)
    post(client, _event("recap_summary", recap_summary="summary text"))
    assert len(client.fake.puts) == puts_after_first  # no duplicate attach


def test_outbound_call_not_ticketed_by_default(client):
    r = post(client, _event("connected", direction="outbound"))
    assert "ignored" in r.json()
    assert len(client.fake.posts) == 0


def test_voicemail_creates_ticket_with_recording_and_transcript(client):
    # voicemail_uploaded carries the recording link...
    r = post(client, _event("voicemail_uploaded",
                            voicemail_link="https://dialpad.com/vm/xyz"))
    body = r.json()
    assert body["voicemail"] is True and "created" in body
    ticket = client.fake.posts[0][1]["json"]["ticket"]
    assert "voicemail" in ticket["tags"] and "missed-call" in ticket["tags"]
    assert any("dialpad.com/vm/xyz" in p[1]["json"]["ticket"]["comment"]["body"]
               for p in client.fake.puts)
    # ...the transcription lands later on its own event.
    post(client, _event("transcription",
                        transcription_text="My laptop won't boot, please call back."))
    assert any("won't boot" in p[1]["json"]["ticket"]["comment"]["body"]
               for p in client.fake.puts)


def test_voicemail_audio_downloaded_and_attached_as_file(client, monkeypatch):
    # With a Dialpad token present, the bridge downloads the audio and uploads it
    # to Zendesk, attaching the file (not just the link) — like the native one.
    monkeypatch.setattr(main, "DIALPAD_API_TOKEN", "dp-token")
    monkeypatch.setattr(main, "ATTACH_VOICEMAIL_AUDIO", True)
    post(client, _event("voicemail_uploaded",
                        voicemail_link="https://dialpad.com/secureblob/voicemail/abc"))
    # audio fetched from the dialpad link...
    assert any("secureblob" in g[0] for g in client.fake.gets)
    # ...uploaded to zendesk...
    assert len(client.fake.uploads) == 1
    assert client.fake.uploads[0][1]["content"] == b"FAKE-AUDIO-BYTES"
    # ...and the upload token attached to a ticket comment.
    assert any(p[1]["json"]["ticket"]["comment"].get("uploads") == ["uptoken-1"]
               for p in client.fake.puts)


def test_voicemail_recording_attached_only_once(client):
    post(client, _event("voicemail_uploaded", voicemail_link="https://dialpad.com/vm/xyz"))
    puts = len(client.fake.puts)
    post(client, _event("voicemail_uploaded", voicemail_link="https://dialpad.com/vm/xyz"))
    assert len(client.fake.puts) == puts  # no duplicate recording comment


def test_cfg_strips_inline_comment_left_in_env(monkeypatch):
    # docker env_file keeps inline comments; _cfg must drop them.
    monkeypatch.setenv("SOME_FLAG", "true   # only ticket internal")
    assert main._cfg("SOME_FLAG", "x") == "true"
    monkeypatch.setenv("SOME_FLAG", "")
    assert main._cfg("SOME_FLAG", "x") == ""


def test_polluted_group_id_does_not_crash_ticket_creation(client, monkeypatch):
    # The real-world bug: ZENDESK_GROUP_ID held the example's comment text.
    monkeypatch.setattr(main, "DEFAULT_GROUP_ID", "# optional: route auto-tickets")
    r = post(client, _event("connected"))
    assert "created" in r.json()                      # no 500
    assert "group_id" not in client.fake.posts[0][1]["json"]["ticket"]


def test_numeric_group_id_is_applied(client, monkeypatch):
    monkeypatch.setattr(main, "DEFAULT_GROUP_ID", "12345")
    post(client, _event("connected"))
    assert client.fake.posts[0][1]["json"]["ticket"]["group_id"] == 12345


def test_unknown_caller_creates_customer_as_requester(client):
    # No existing user for the caller -> create a customer (NOT default to the
    # API account), using the caller ID + phone, and make them the requester.
    r = post(client, _event("connected"))
    assert "created" in r.json()
    assert len(client.fake.created_users) == 1
    created = client.fake.created_users[0]
    assert created["payload"]["name"] == "Jane Tech"
    assert created["payload"]["phone"] == "+15551112222"
    assert client.fake.posts[0][1]["json"]["ticket"]["requester_id"] == created["id"]


def test_existing_customer_matched_by_phone_is_requester(client):
    client.fake.users_by_phone["5551112222"] = {"id": 777, "phone": "+1 (555) 111-2222"}
    post(client, _event("connected"))
    assert client.fake.posts[0][1]["json"]["ticket"]["requester_id"] == 777
    assert client.fake.created_users == []   # reused existing, didn't create


def test_unknown_caller_without_name_falls_back_to_phone(client):
    ev = _event("voicemail_uploaded", voicemail_link="https://x/vm")
    ev["contact"] = {"phone": "+15553334444", "type": "user"}   # no caller-ID name
    post(client, ev)
    assert client.fake.created_users[0]["payload"]["name"] == "+15553334444"


def test_external_caller_is_skipped(client):
    # contact.type != "user" => external; native integration handles it.
    ev = _event("connected")
    ev["contact"] = {"name": "Acme Corp", "phone": "+18005550000", "type": "google"}
    r = post(client, ev)
    assert "ignored" in r.json() and "external" in r.json()["ignored"]
    assert len(client.fake.posts) == 0


def test_event_without_call_id_is_ignored(client):
    r = client.post("/dialpad/webhook", json={"state": "connected"})
    assert r.json() == {"ignored": "no call_id/state"}
    assert len(client.fake.posts) == 0
