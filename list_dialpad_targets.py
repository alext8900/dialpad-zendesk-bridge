"""
Discovery helper: list every Dialpad office, department, and call center with its
ID, so you can pick the targets to scope the bridge to.

    DIALPAD_API_TOKEN=... python list_dialpad_targets.py
    DIALPAD_API_TOKEN=... python list_dialpad_targets.py "IT"   # filter by name substr

Our topology (IT Department menu -> 3 contact centers) needs the IDs of:
  - the "IT Department" department         (department)   -> after-hours voicemail
  - "IT - AS400 (Don't Call)"              (callcenter)
  - "IT - Technical Support (Don't Call)"  (callcenter)
  - "IT Department All Agents (Don't Call)"(callcenter)

Feed those into setup_dialpad.py via IT_TARGETS (see that file).
"""

import os
import sys

import httpx

import envfile

envfile.load()  # pick up DIALPAD_API_TOKEN etc. from .env if present

API = "https://dialpad.com/api/v2"
TOKEN = os.environ["DIALPAD_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}
NEEDLE = (sys.argv[1].lower() if len(sys.argv) > 1 else None)


def _paged(path):
    """Yield items across Dialpad's cursor pagination."""
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        r = httpx.get(f"{API}{path}", headers=H, params=params, timeout=20)
        r.raise_for_status()
        body = r.json()
        for item in body.get("items", []):
            yield item
        cursor = body.get("cursor")
        if not cursor:
            return


def _show(kind, item, office_name):
    name = item.get("name", "?")
    if NEEDLE and NEEDLE not in name.lower():
        return
    print(f"{kind:<11} id={str(item.get('id')):<14} office={office_name!r:<22} name={name!r}")


def main():
    offices = list(_paged("/offices"))
    if not offices:
        print("No offices returned — check the token's scopes.")
        return
    for office in offices:
        oid, oname = office.get("id"), office.get("name", "?")
        _show("OFFICE", office, oname)
        for dept in _paged(f"/offices/{oid}/departments"):
            _show("DEPARTMENT", dept, oname)
        for cc in _paged(f"/offices/{oid}/callcenters"):
            _show("CALLCENTER", cc, oname)
    print("\nUse the CALLCENTER ids for the 3 IT queues and the DEPARTMENT id for "
          "the IT Department voicemail. Pass them to setup_dialpad.py as IT_TARGETS.")


if __name__ == "__main__":
    main()
