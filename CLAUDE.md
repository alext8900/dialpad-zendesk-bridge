# CLAUDE.md — Project Operational Rules

> Drop this file in the root of any repo. Claude reads it automatically at session start.
> Customize the stack-specific sections (marked with `# TODO`) for your project.

---

## Start Here (New Session Orientation)

Before touching any code:

1. Read any `docs/` files relevant to the task — architecture decisions, status docs, changelogs.
2. Run the existing test suite. Know the baseline **before** making changes.
3. Identify what files the task actually touches. Read them. Don't guess at structure.
4. Define success criteria out loud before writing a single line.

---

## Tech Stack

<!-- # TODO: Fill this in for your project -->

- **Language / runtime:**
- **Framework:**
- **Database:**
- **Test runner:**
- **CI/CD:**

---

# Operational Rules

## Rule 1 — Think Before Coding

State assumptions explicitly. Ask rather than guess.
Push back when a simpler approach exists. Stop when confused.
For any stateful, async, or networked work: define the exact test scenario
and pass criteria BEFORE writing a single line.

## Rule 2 — Simplicity First

Minimum code that solves the problem. Nothing speculative.
No abstractions for single-use code.

**Exception:** state machines, reconnection logic, and security-critical paths —
prefer explicit and complete over brief. Edge cases matter there.

## Rule 3 — Surgical Changes

Touch only what you must. Don't improve adjacent code.
Match existing style. Don't refactor what isn't broken.
If a refactor is warranted, call it out separately — don't slip it into a fix.

## Rule 4 — Goal-Driven Execution

Define success criteria before starting. Loop until verified.
Strong, measurable success criteria let Claude work autonomously.

## Rule 5 — Read Before You Write

Before adding code, read exports, immediate callers, and shared utilities.
If unsure why existing code is structured a certain way, ask.
Always read the full file before modifying any stateful module.

## Rule 6 — Checkpoint After Every Significant Step

Summarize what was done, what's verified, what's left.
Don't continue from a state you can't describe back.
If a fix attempt fails, document WHY before trying the next approach.

## Rule 7 — Surface Conflicts, Don't Average Them

If two patterns contradict, pick one (more recent / more tested).
Explain why. Flag the other for cleanup.
Never silently blend two conflicting approaches.

## Rule 8 — No Silent Regressions

After any change, explicitly state which test suite areas could be affected
and need re-verification. Surprising coupling is common — don't assume
unrelated code is safe.

## Rule 9 — Fail Loud

"Completed" is wrong if anything was skipped silently.
"Tests pass" is wrong if any were skipped or excluded.
If a fix cannot be verified without a specific environment or device, say so.
Default to surfacing uncertainty, not hiding it.

## Rule 10 — Match the Codebase Conventions

Conformance over taste. Match the logging style, naming style, and module
structure already in use. If you think a convention is harmful, surface it.
Don't fork silently.

## Rule 11 — Advisor Escalation

If the same bug has had 3+ failed fix attempts, stop.
Document the failure chain, then escalate to a stronger model or load a
debugging-focused system prompt before attempt 4.
Don't run the same approach a fourth time hoping for a different result.

## Rule 12 — Token Budget Awareness

If a task is running significantly longer than expected, checkpoint,
summarize progress, and re-evaluate before continuing.
Don't silently overrun. Surface the breach and ask whether to continue.

---

# CI/CD & Testing Rules

These rules are ALWAYS active — even when CI is broken, unavailable, or
not yet set up. Claude is expected to write and maintain tests as a core
part of every task, not as an optional follow-up.

## T1 — Tests Are Not Optional

Every bug fix ships with a regression test that would have caught it.
Every new feature ships with tests covering the happy path and the
primary failure modes.
"I'll add tests later" is not an acceptable deliverable state.

## T2 — Own the Test Suite — Don't Wait for CI

If CI is broken, unavailable, or not configured:
- Run tests locally and report results explicitly.
- Write tests anyway. The test file is the artifact.
- Note that CI needs to be wired up and flag it — don't silently skip.

Claude should proactively write or update tests without being asked
whenever it modifies behavior, fixes a bug, or adds a feature.
This is the default behavior, not a special mode.

## T3 — Know the Baseline Before Touching Anything

Before any code change:
```
<run the test command here — e.g., npm test / pytest / go test ./...>
```
Record the result: `X passing, Y failing, Z skipped`.
Any regression introduced by your changes is your responsibility to fix
before declaring the work done.

## T4 — Regression Tests Must Be Specific

A regression test must:
- Reproduce the exact scenario that caused the bug
- Assert the specific output or state that was wrong
- Pass only after the fix, not before

Vague "smoke tests" don't count as regression coverage.

## T5 — Test Near the Behavior, Not the Implementation

Test public interfaces and observable outcomes.
Don't assert internal state or private method calls unless the whole
point is to guard an internal contract.
Tests that break on every refactor aren't protecting you — they're
slowing you down.

## T6 — Write Tests That Can Run Without a Human Present

Tests must not require manual steps, human confirmation, or a
specific developer machine state to pass.
If a test needs an external service, mock it or mark it as integration-only
and skip it in the standard suite.

## T7 — Test File Naming and Location

<!-- # TODO: Customize for your project's conventions -->

Default conventions (adjust to match the existing project structure):
- Unit tests: co-located with the module (`foo.test.ts`, `test_foo.py`)
- Integration tests: `tests/integration/`
- End-to-end tests: `tests/e2e/`
- Fixtures / mocks: `tests/fixtures/`

If the project has an existing pattern, match it exactly.

## T8 — CI Failures Block Merges

If CI is configured and a pipeline is failing:
- Do not ask the human to merge anyway.
- Fix the pipeline or explicitly state what needs to be fixed and why
  it's out of scope for this task.
- A failing CI pipeline is a first-class bug, not background noise.

## T9 — Flag Untestable Code

If a piece of code genuinely can't be tested without a physical device,
third-party hardware, or a live external service:
- Say so explicitly.
- Write tests for everything around it that CAN be tested.
- Stub or mock the boundary so the surrounding logic is covered.

## T10 — Keep the Test Suite Fast

Tests that take >5s each slow down the feedback loop for everyone.
Heavy tests (DB, network, E2E) belong in a separate suite that runs
on a slower cadence (pre-push or CI-only), not in the fast unit suite
that runs on every save.

---

# Failure Modes to Avoid

Document project-specific failure modes here as they're discovered.
Format: what happened, why it happened, how to not do it again.

<!-- # TODO: Add entries as you discover them -->

Example format:
```
- **[Short name]:** What went wrong. Root cause. The rule it implies going forward.
```

---

# Files — Do Not Touch Without Discussion

List files or patterns here that are load-bearing, cross-cutting, or
easy to break in non-obvious ways.

<!-- # TODO: Add project-specific entries -->

Examples of things that belong here:
- Wire protocol definitions shared across multiple processes/services
- Auth middleware or session handling
- Database migration files (once applied, don't edit — add a new migration)
- Build tool config files where small changes have large blast radius

---

# Deployment Notes

<!-- # TODO: Fill in for your project -->

- **How to deploy:**
- **Branch conventions:**
- **Environment variables:**
- **Database migrations:**

---

# Autonomous / Long-Running Session Rules

When running without a human in the loop (overnight runs, multi-step
implementations, subagent-driven tasks):

- Compact context at ~50% usage, not at 90%. Late compacts are slow and lossy.
- Before compacting: finish or document in-flight work, commit pending changes,
  write a brief status note so the post-compact session can re-orient.
- Never compact with a hanging background process.
- Treat a brief in `docs/plans/<task>-status.md` as a handoff note to your
  future self. Write it like the next session won't remember anything.

---

*Last updated: <!-- date --> | Maintainer: <!-- name -->*
