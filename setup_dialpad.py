"""
Run ONCE to register the webhook + call-event subscription with Dialpad.

    python setup_dialpad.py

Reads from env:
  DIALPAD_API_TOKEN        - admin/company API key WITH the 'recordings_export' scope
  PUBLIC_WEBHOOK_URL       - public https URL pointing at /dialpad/webhook
  DIALPAD_WEBHOOK_SECRET   - shared secret used to sign/verify JWTs (set the same
                             value in the bridge's env)
  IT_TARGET_TYPE           - e.g. 'callcenter' (confirm against your IT queue)
  IT_TARGET_ID             - the id of your IT help-desk call center / user

Scoping to the IT target is what keeps you from receiving every internal call at
BPI. Confirm IT_TARGET_TYPE against your queue: Dialpad targets include office,
department, callcenter, user, room, etc.
"""

import os
import httpx

API = "https://dialpad.com/api/v2"
TOKEN = os.environ["DIALPAD_API_TOKEN"]
HOOK_URL = os.environ["PUBLIC_WEBHOOK_URL"]
SECRET = os.environ["DIALPAD_WEBHOOK_SECRET"]
TARGET_TYPE = os.environ.get("IT_TARGET_TYPE")     # e.g. "callcenter"
TARGET_ID = os.environ.get("IT_TARGET_ID")

H = {"Authorization": f"Bearer {TOKEN}"}


def main():
    # 1) create the webhook
    wh = httpx.post(f"{API}/webhooks", headers=H,
                    json={"hook_url": HOOK_URL, "secret": SECRET}, timeout=15)
    wh.raise_for_status()
    webhook_id = wh.json()["id"]
    print("webhook_id:", webhook_id)

    # 2) create the call-event subscription, scoped to the IT queue.
    #    We only need hangup (create) and recording (attach) states.
    sub = {
        "webhook_id": webhook_id,
        "enabled": True,
        "call_states": ["connected", "hangup", "voicemail", "recording"],
    }
    if TARGET_TYPE and TARGET_ID:
        sub["target_type"] = TARGET_TYPE
        sub["target_id"] = int(TARGET_ID)

    s = httpx.post(f"{API}/subscriptions/call", headers=H, json=sub, timeout=15)
    s.raise_for_status()
    print("subscription_id:", s.json().get("id"))
    print("done. point Dialpad-side config at:", HOOK_URL)


if __name__ == "__main__":
    main()
