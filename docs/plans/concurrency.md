# Concurrency & Durable Delivery — Dialpad↔Zendesk Bridge

**Status:** Plan only. Not scheduled. Pick up when call volume justifies it.
**Current state:** Fine today — helpdesk is slow, the single worker serializes
correctly, and the (narrow) length backfill covers the common dropped-hangup case.
See Q2 for exactly what "fine" does and does not cover.

---

## Gate before building (verify, don't assume)

These decide whether this work is urgent, a nice-to-have, or unnecessary. **Answer
Q1 first.** Q2's answer is already known (it's recorded below). Do not schedule the
inbox queue until Q1 is resolved.

### Q1 — What is Dialpad's actual webhook timeout, and does it retry failures?

This sets the entire urgency.

- **If Dialpad retries** failed deliveries with reasonable backoff → the inbox
  drops to a durability nicety. Lower priority; may not build it at all.
- **If delivery is at-most-once with no retry** → a slow ack means the event is
  gone for good. The inbox becomes the real safety net. High priority.

Dialpad Call Events are believed to be fire-and-forget (likely why Gabby's `hangup`
vanished), but **confirm against Dialpad's docs/support — do not infer from one
incident.** Note: Q1's answer also drives the inbox idempotency key (below) — if
Dialpad *does* retry, we'll receive duplicate deliveries and must dedup on insert.

### Q2 — What does the backfill actually guarantee? (answer known)

"Fine today" rests on the length backfill (commit 9c982e9). It is **narrow**, and
this is the recorded answer — no investigation needed:

- It is **event-driven, not scheduled.** It fires only when a *later* event for the
  same call arrives carrying `talk_time` (the recap, or a late hangup) **and a
  ticket already exists**. There is no periodic reconciler / gap-filler.
- It covers **only the call length** appended to the subject. It does **nothing**
  for: ticket *creation*, the AI recap comment, voicemail audio/transcript, or
  agent assignment — if the event that would have done those is the one dropped,
  it is simply lost.

**The real unprotected case:** a call where **both `connected` and `hangup` time
out → no ticket is ever created.** The backfill can't help because it no-ops
without an existing ticket. This is the strongest argument for the inbox: its value
is **"stops lost tickets,"** not just "duration on the subject."

### Bonus measurement (cheap, do alongside Q1)

Measure real per-event latency with `DEBUG_PAYLOAD` timing under a couple of
concurrent calls. Confirms the 2–4s/event estimate that drives the drain-time math.

---

## The problem (what actually fails under load)

Not CPU. Not races. It's slow acks → upstream timeouts → silently dropped events:

1. Each webhook holds the single event loop for ~2–4s — it does 2–4 sequential
   Zendesk/Dialpad HTTP round-trips (~0.3–1s each, visible in logs).
2. They process serially, so a burst of ~30 webhooks (a few simultaneous calls ×
   connected/hangup/recap legs) takes ~60–120s to drain.
3. Dialpad waits for a `200` per delivery. If it times out before we answer, it
   treats that delivery as failed — and Call Events give us no reliable retry. A
   dropped delivery is exactly a missed `hangup` like Gabby's, just caused by **us
   being slow** instead of Dialpad hiccuping. (And per Q2, if the *connected* is
   what we're slow to ack, that's a wholly lost ticket.)

**Design against:** slow acks causing upstream timeouts.

---

## The fix: ack fast, process durably

Canonical webhook inbox pattern. Fits current scale and infra with **no new
infrastructure**.

1. **On POST:** decode + **verify the JWT first** (never persist an unauthenticated
   payload), then write the raw event into a new SQLite `inbox` table
   (`status=pending`), commit, return `200` immediately. A local SQLite insert is
   ~1ms, so we can't time out Dialpad regardless of burst size.
2. **Background drainer** pulls `pending` events FIFO, runs the existing processing
   logic, marks each `done` / `failed` with a retry count.
3. **Idempotency already exists** — alias-map dedup, `is_enriched`, and the
   `*_done` flags make reprocessing and out-of-order events safe (the "goblin
   order" test proves this). The inbox converts Dialpad's unreliable at-most-once
   delivery into our durable at-least-once.

**Why it fits:**
- Survives container restarts mid-burst (inbox lives on the persisted volume).
- Decouples *receiving* from the slow Zendesk work.
- Keeps processing serialized → no reintroduced race.
- A single FIFO drainer is plenty at this volume; no parallelism needed.

### The drainer MUST be a thread, not an asyncio task (load-bearing)

The existing processing logic uses **blocking** `httpx`/`sqlite3`. A blocking call
inside an asyncio task **freezes the event loop for the whole 2–4s**, so a new POST
can't be acked during that window — which quietly defeats the fast-ack this whole
plan depends on.

→ Run the drainer in a **dedicated worker thread**. This is also *less* work: the
thread reuses all current synchronous code unchanged, and `store.py` already opens
a fresh connection per call (`_conn()`), so there is no cross-thread SQLite handle
problem (`check_same_thread`). The async POST handler then only does
`await request.body()` + verify + a ~1ms insert + return, and the loop stays free.

(An asyncio-task drainer would require porting everything to `httpx.AsyncClient` +
`aiosqlite` first — which we explicitly do not want to do. So: thread.)

### Inbox table requirements

- **Idempotency key:** `UNIQUE(call_id, state, event_timestamp)` with
  `INSERT OR IGNORE`, so a Dialpad redelivery (if Q1 says they retry) collapses on
  arrival instead of duplicating work.
- **WAL + busy_timeout (first-class, not optional):** once the POST writer and the
  drainer thread both touch SQLite, you'll hit `database is locked` without it.
  Enable WAL mode and a few-second `busy_timeout` in `store.init()`.

---

## Liveness — don't let the new component fail silently

The inbox fixes durability but introduces a drainer that is now a single point of
failure with a **silent** failure mode: a wedged or crashed drainer produces a
growing backlog while Dialpad still sees healthy `200`s on every delivery.
Idempotency covers correctness, not liveness. So:

- **Dead-letter path:** after N retries, mark an event `dead` and alert. Don't let
  one poison event block the FIFO or retry forever.
- **Depth/age metric:** expose `pending` count and oldest-pending age (e.g. via
  `/healthz`) so a stalled drainer is visible before the backlog matters. Because a
  single FIFO drainer serializes across calls, **backlog age — not ack latency — is
  the SLO.** Expect ticket creation to lag a slow burst by up to a minute; that's
  acceptable at this scale.
- **Crash recovery on boot:** on startup, reset orphaned `processing → pending`
  (events in-flight when the container died), then resume draining. Otherwise
  mid-burst crashes strand in-flight events.

---

## Explicitly NOT doing

- **Multiple uvicorn workers / replicas** — breaks dedup serialization, forces a
  real lock + Postgres. Overkill and a regression risk.
- **Redis / Celery / RabbitMQ** — a broker for an internal helpdesk is
  over-engineering. SQLite-as-queue is the right weight.
- **Full async `httpx` + `aiosqlite` rewrite now** — real concurrency reintroduces
  the TOCTOU race for benefit we don't need yet. (The thread drainer above sidesteps
  this entirely.)

---

## Cheap stopgap (only if breathing room is needed before the queue)

Making the handler a plain `def` instead of `async def` makes Starlette run it in a
~40-thread pool → instant concurrency, ~one-line change. **But** it reintroduces the
check-then-create race and concurrent SQLite writes, so it'd need a per-call lock +
`busy_timeout`. Recommendation: skip it, go straight to the inbox queue when the
time comes — cleaner and actually durable.

---

## Net

You're fine today, with the precise caveat in Q2 (length backfill only; a fully
dropped call still loses its ticket). The right long-term move is the durable inbox
+ fast-ack + **thread** drainer + idempotent processing — modest, no new infra,
swallows whatever burst you throw at it. Gate on Q1 first; build with the inbox
idempotency key, WAL/busy_timeout, a dead-letter path, and a drainer-liveness
metric so the new component doesn't fail silently.
