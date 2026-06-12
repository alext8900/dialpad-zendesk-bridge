"""
Run ONCE to register the webhook + call-event subscriptions with Dialpad.

    python setup_dialpad.py

Reads from env:
  DIALPAD_API_TOKEN        - admin/company API key
  PUBLIC_WEBHOOK_URL       - public https URL pointing at /dialpad/webhook
  DIALPAD_WEBHOOK_SECRET   - shared secret used to sign/verify JWTs (set the same
                             value in the bridge's env)
  IT_TARGETS               - comma-separated "type:id" pairs to scope to, e.g.
                             "department:111,callcenter:222,callcenter:333,callcenter:444"
                             Run list_dialpad_targets.py first to find the ids.
                             Leave blank to subscribe company-wide (NOT recommended
                             — you'd get every internal call in the company).

Our topology: the IT Department's menu routes to 3 contact centers, and
after-hours calls hit the department's voicemail. So we scope to FOUR targets:
the department (for voicemail) + the 3 call centers (for routed calls). One
subscription is created per target, all pointing at the same webhook.

Dialpad target types: office, department, callcenter, user, room.
"""

import os
import httpx

API = "https://dialpad.com/api/v2"
TOKEN = os.environ["DIALPAD_API_TOKEN"]
HOOK_URL = os.environ["PUBLIC_WEBHOOK_URL"]
SECRET = os.environ["DIALPAD_WEBHOOK_SECRET"]
IT_TARGETS = os.environ.get("IT_TARGETS", "").strip()

H = {"Authorization": f"Bearer {TOKEN}"}

# States the bridge needs:
#   connected/hangup            -> create + enrich answered-call tickets
#   voicemail/voicemail_uploaded-> create voicemail tickets + attach voicemail_link
#   transcription               -> attach voicemail transcription
#   recap_summary               -> attach the AI recap for answered calls
CALL_STATES = ["connected", "hangup", "voicemail", "voicemail_uploaded",
               "transcription", "recap_summary"]


def _parse_targets(raw: str):
    """'department:111,callcenter:222' -> [('department', 111), ('callcenter', 222)]"""
    targets = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        ttype, _, tid = chunk.partition(":")
        targets.append((ttype.strip(), int(tid.strip())))
    return targets


def main():
    # 1) create the webhook
    wh = httpx.post(f"{API}/webhooks", headers=H,
                    json={"hook_url": HOOK_URL, "secret": SECRET}, timeout=15)
    wh.raise_for_status()
    webhook_id = wh.json()["id"]
    print("webhook_id:", webhook_id)

    # 2) one call-event subscription per scoped target (all -> same webhook)
    targets = _parse_targets(IT_TARGETS)
    if not targets:
        print("WARNING: no IT_TARGETS set — subscribing company-wide.")
        targets = [(None, None)]

    for ttype, tid in targets:
        sub = {"webhook_id": webhook_id, "enabled": True, "call_states": CALL_STATES}
        if ttype and tid:
            sub["target_type"] = ttype
            sub["target_id"] = tid
        s = httpx.post(f"{API}/subscriptions/call", headers=H, json=sub, timeout=15)
        s.raise_for_status()
        print(f"subscription_id: {s.json().get('id')}  (target={ttype}:{tid})")

    print("done. point Dialpad-side config at:", HOOK_URL)


if __name__ == "__main__":
    main()
