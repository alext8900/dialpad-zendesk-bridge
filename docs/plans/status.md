# Status / Handoff (pre-compact)

Last updated: 2026-06-22. Read `CLAUDE.md` (rules + cases), `docs/ARCHITECTURE.md`
(design + decision log), `README.md` (run/deploy). This is the short "where we are."

## State

- **Live in production.** Repo `github.com/alext8900/dialpad-zendesk-bridge` (public).
  Deploy = push to `main` → `git pull` on the Windows Docker host → `docker compose
  up -d --build`. Public URL `https://dialpad.bpiteam.com` via Cloudflare Tunnel.
- **34 unit tests passing** (`./.venv/bin/python -m pytest -q`, HTTP mocked).
- All cases in CLAUDE.md "Cases covered" are implemented + tested.

## What works (high level)

- Internal calls → Zendesk tickets; external calls left to the native integration.
- **One ticket per call** even across transfers / contact-center fan-out, via an
  **alias map** unioning `call_id`/`master_call_id`/`entry_point_call_id`/
  `operator_call_id` (`store.resolve_and_link`). Single-field keying was NOT enough
  (operator leg has `master_call_id: null`).
- Two-phase create: on `connected` (real answer), finalize length on `hangup`
  (hangup fallback-creates if connected was missed). Voicemails → ticket + audio +
  transcription. Menu-disconnects/ring-outs filtered (no answer fields).
- Assign to the answerer (fetch the operator leg for CC calls); last-answerer-owns.
- Requester matched/created from caller, no `external_id` (mergeable).
- Subject `Dialpad call with {caller} — answered by {agent} · {length}`; call center
  as a slugged tag; `(Don't Call)` stripped.
- Dedup hardening: junk ids (`null`/`0`/`""`) can't union unrelated calls; if two
  already-ticketed groups unify late, it logs loudly + notes the canonical ticket
  (no silent orphan).

## Open items (not blocking)

- **Historical duplicate tickets** (196/197) + the **Yvonne** contact: merge/close
  by hand (clear `external_id` first for Yvonne). No auto-heal.
- **Scope/queue rule** not built; full payloads are logged on every ticketing event
  (`_log_ticketing`) so the routing field can be chosen later.
- `aliases` table is never pruned (fine at this volume; add cleanup if `state.db`
  bloats). CI not wired. FastAPI `on_event` → lifespan (cosmetic warning).

## Last thing verified

A real contact-center call (Trey↔Alex) was making 2 tickets; fixed with the alias
map + conflict handling. Worth confirming on the next live CC call that it's a
single ticket. If it ever splits again, the `ticketing[...]` payload logs show the
id relationships.
</content>
