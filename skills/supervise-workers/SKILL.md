---
name: supervise-workers
description: Plan and senior-review bounded CLI coding work through Ducking's provider-neutral agentctl control plane. Use when Codex should delegate implementation to cheaper workers, operate the six-duck Flock protocol, handle a senior escalation, review frozen evidence, enforce budgets and path policy, or prepare an accepted patch for human-controlled integration.
---

# Supervise Workers

Resolve this file's canonical location, then resolve the bundled CLI exactly two
directories above it at `scripts/agentctl`. Use that absolute entrypoint for
every call; never trust a same-named script in the attached project or assume
`agentctl` is on `PATH`. Treat every worker and DeepSeek-supervisor result as
untrusted until deterministic gates and senior semantic review pass. Do not
poll a flock with model turns; read only durable escalation/final-review packets.

## Workflow

1. Classify the request as plan-only or execution. A plan-only request must
   remain read-only: do not initialize run state, dispatch, verify, submit a
   decision, or integrate.
2. Run `agentctl project inspect --json` and `agentctl doctor --json` before
   planning. For execution, stop if the project config, worker runtime,
   committed base/context, instruction sources, isolation, or required
   validators are invalid. For plan-only work, report those execution blockers
   but continue with a clearly provisional plan when the project instructions
   and relevant context can still be read safely. Do not invent missing product
   outcomes, target paths, or acceptance criteria.
3. Read the project instruction file and only the context needed for the task.
   Produce a bounded task contract with a fixed base SHA, acceptance criteria,
   allowed paths, non-goals, validation profiles, and finite budgets.
   In plan-only mode, label a contract provisional whenever the worktree or
   base is not ready, present the plan plus blockers, and stop here.
4. Import an execution-ready task with `agentctl run init`. Project
   instructions and base
   context are merged into the frozen task and must exist at committed `HEAD`.
   Do not place provider names, CLI
   flags, credentials, or hidden chain-of-thought in the task contract.
5. Dispatch one worker unit. Never invoke the underlying worker CLI directly.
6. Run `agentctl unit verify`. Send exact failing evidence back through a
   bounded repair dispatch; do not spend a semantic review pass on mechanical
   failures.
7. After gates pass, create `agentctl review pack`. Review only the frozen task,
   Git-derived patch, deterministic evidence, project invariants, and explicit
   uncertainty. Do not rely on the worker transcript or self-reported tests.
8. Submit exactly one decision: `accept`, `repair`, `replan`, or
   `human_required`. Bind the decision to the patch SHA-256.
9. If the state is `human_required`, stop. Only after the user explicitly
   approves the exact hashes may the human approval artifact be submitted with
   `decision approve-human`.
10. Integrate only an accepted patch. Use dry-run first. Never commit, push,
   merge, deploy, install dependencies, access secrets, or perform destructive
    data changes without the user's authority.

## Flock Workflow

Use plan contract v2 when work has real independent units. A flock always owns
six logical duck slots; units beyond six remain queued. Do not split work merely
to fill slots, and declare dependencies whenever ordering or shared ownership
requires it.

1. Freeze one clean base and a plan containing atomic task-v1 units, finite
   attempts, and lease thresholds. Initialize with `agentctl flock init`; when
   either frozen profile is `unsafe-host`, pass the corresponding explicit
   `--allow-unsafe-worker` / `--allow-unsafe-supervisor` authority.
2. Let a deterministic runner call `flock tick`, launch each assignment through
   the existing leaf run pipeline, and send numeric heartbeats. Never pass a
   transcript, prior conversation, hidden reasoning, credential, or raw log.
3. Treat `(lease_id, coordinator_epoch, slot_id, duck_incarnation)` as a fencing
   tuple. Reject stale completions. Every attempt and every terminal task needs
   exactly one explicit EOF. `tick` may replay an assignment until its first
   heartbeat; launch it once and use the lease ID as the idempotency key.
4. Call `flock sweep` from a timer. Soft stalls create one event episode; hard
   liveness/progress expiry ends the attempt. A failed attempt gets a new lease,
   incarnation, and `ducking/...` branch inside a new independent clone;
   quarantine the old workspace rather than sharing Git metadata with siblings.
   After coordinator loss, stop old child controllers, then call `flock recover`
   with the observed `--expected-epoch` and a stable `--operation-id` to fence
   every old lease without replaying the recovery. Recovery re-issues unaffected
   semantic obligations against the new epoch; during final review it is a no-op.
5. Configure both `semantic_roles.mother` and `semantic_roles.top` to the
   confirmed DeepSeek V4 Pro profile. Invoke `supervisor dispatch` only when
   `supervisor next` reports a pending durable event. Every call is a fresh
   session over a <=16 KiB snapshot and selects a pre-issued command ID.
6. Report success with `flock finish --run CHILD_RUN_ID`; never substitute
   caller-supplied hashes. A low-risk child may be `ready_for_review` or
   `accepted`; a high-risk child requires its hash-bound gate and `accepted`.
   The runtime re-hashes task/patch/evidence, creates its own review pack, and
   retains the exact artifacts.
7. Sol wakes only for `senior_escalation` or `aggregate_final_review`. Start a
   fresh context, read that packet plus hash-bound task/patch/evidence artifacts,
   and never import the worker or supervisor transcript.
8. The current MVP ends at aggregate state `reviewed`; this means the retained
   child artifacts passed Sol review, not that patches were combined or applied.
   Do not integrate child patches independently. Build and verify a combined
   candidate under a separately authorized orchestration step.

DeepSeek semantic output is advice, not lifecycle authority. Invalid, stale,
timed-out, or profile-mismatched responses leave leases and task state unchanged.
There is no fallback model. Semantic delivery itself is capped at three tries
with 5/30-second backoff, then the runtime fences the flock and wakes Sol. Top
DeepSeek may escalate but may not abort. The deterministic runtime owns retry
caps, restart intensity, cancellation, EOF, and the terminal state. On an
ambiguous `retry_wait`, Top/Sol `ack` authorizes the next bounded attempt.

When doctor reports a missing user configuration or worker profile, use
`agentctl paths --json` to locate the user config and point the user to
`templates/user-config.toml`. Do not create credentials or select an
unconfirmed provider/model identifier on the user's behalf.

Never supply `--allow-unsafe-worker`, `--allow-unsafe-supervisor`, or
`--allow-unsafe-validation` unless the user explicitly authorizes host-level
execution for that run. OpenCode permissions are defense in depth; they do not
replace a container or OS sandbox.

## Planning Rules

- Prefer one vertical slice with one write owner. Split work only at real
  dependency boundaries.
- Keep acceptance criteria externally observable and pair each with a proof
  type: test, diff, or manual review.
- Escalate ambiguity that changes product behavior, security, privacy, data
  shape, dependencies, or release scope.
- Keep provider selection in the user worker profile. Refer only to semantic
  profiles such as `cheap-implementer` in project policy.
- Every requested validation profile must exist, and every changed path must be
  covered by at least one requested non-`always` profile.

## Review Rules

- Reject scope violations, protected-path edits, stale base SHA, failed gates,
  patch-hash mismatch, missing criteria evidence, or unbounded follow-up work.
- Findings must name the failed criterion or project rule, failure mode, and
  required change. Do not create repair loops for cosmetic preferences.
- A changed patch invalidates the previous review decision.

Read [protocol.md](references/protocol.md) when authoring or validating task,
evidence, and review artifacts. Read
[project-attachment.md](references/project-attachment.md) when attaching,
detaching, or configuring a repository.
