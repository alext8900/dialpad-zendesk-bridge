"""Regression tests for the Dialpad -> Zendesk bridge state machine.

These exercise the observable behavior of POST /dialpad/webhook end to end:
ticket-on-answer, dedup, the missed-call safety net, hangup enrichment, and
recording attachment. All Zendesk/Dialpad HTTP traffic is mocked, so the suite
runs with no network and no real credentials.

Run:  python -m pytest -q   (after pip install -r requirements-dev.txt)
"""

import importlib
import os

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
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeHttpx:
    """Records every Zendesk call and returns canned responses.

    - GET  .../users/search.json  -> no matching end-user (skip requester)
    - POST .../tickets.json       -> a new ticket with an incrementing id
    - PUT  .../tickets/{id}.json  -> the enrich / recording updates
    """

    def __init__(self):
        self.posts = []
        self.puts = []
        self.gets = []
        self._next_id = 100

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResp({"users": []})

    def post(self, url, **kwargs):
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
        "contact": {"name": "Jane Tech", "phone": "+15551112222"},
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


def test_event_without_call_id_is_ignored(client):
    r = client.post("/dialpad/webhook", json={"state": "connected"})
    assert r.json() == {"ignored": "no call_id/state"}
    assert len(client.fake.posts) == 0
