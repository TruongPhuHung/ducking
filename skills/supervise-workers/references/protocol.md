# Artifact Protocol

## Task

A task fixes `contract_version`, `task_id`, `base_sha`, `objective`, acceptance
criteria, context files, allowed and forbidden paths, validation profiles, and
budgets. Paths are repository-relative. Unknown fields may be retained for
forward compatibility, but required fields must validate before dispatch.

## Evidence

The controller derives changed files, diff statistics, patch hash, scope
violations, verifier commands, exit codes, durations, and log hashes. Worker
self-reports are informative only. Green evidence requires every selected
command to exit zero and every controller policy check to pass.

## Review

A review names the run, exact patch hash, frozen task hash, and deterministic
evidence hash. Its decision is one of:

- `accept`: all criteria pass and no blocking finding remains.
- `repair`: the frozen objective remains valid and findings are bounded.
- `replan`: acceptance, scope, or dependency shape must change.
- `human_required`: product authority, credentials, network access, destructive
  data operations, production changes, or another protected action is needed.

Any patch change invalidates the review. The controller, not prose, owns state
transitions.

For `human_required`, a separate human approval artifact must bind the run,
patch, task, evidence, and review hashes. It records the human identifier and
rationale, then transitions the same frozen candidate to `accepted`. Agents
must not synthesize this approval without explicit human authority.

## Events

Events are append-only JSON Lines with sequence, timestamp, actor, previous
state, next state, reason, and artifact hashes. Raw worker output is stored
separately and must not drive core transitions without adapter normalization.
Frozen task, profile, patch, evidence, review, and human-approval artifacts are
rehashed before trust transitions and integration.

## Flock plan v2

A flock plan v2 adds `plan_id`, one committed `base_sha`, an aggregate goal,
finite retry/lease policy, and atomic task-v1 `units`. Unit `task_id` values are
unique, every unit uses the plan base, and `depends_on` must form an acyclic
graph. Provider/model names and process settings are forbidden from the plan.

The runtime creates six fixed logical slots. `tick` atomically changes a ready
unit from queued/retry-wait to leased and returns a fencing tuple containing:

```text
task_id + attempt + lease_id + coordinator_epoch
+ slot_id + duck_incarnation + contract_sha256 + branch_ref
```

Every duck event must match that tuple. Increasing `progress_seq` renews the
progress lease; a heartbeat only renews liveness. Reusing a sequence with new
content, reporting after EOF, or completing an old lease is rejected.
Until that first heartbeat, `tick` may replay the same durable assignment after
a lost response. The runner deduplicates by lease ID and launches it once.

## EOF and retry

An attempt always ends with one `ducking.attempt-eof/v1`, including whether the
worker itself supplied EOF. A pipe close, controller loss, or hard lease expiry
is abnormal and the MotherRuntime synthesizes the EOF before replacing the duck
incarnation. A terminal task gets one `ducking.task-eof/v1`. The flock has EOF
only when no lease is active, every task is terminal, and the aggregate state is
`reviewed`, `escalated`, or `cancelled`. Entering escalation fences every sibling
lease and emits terminal EOFs; it cannot strand a live duck.

Infrastructure failures may retry at deterministic 5s then 30s delays, with
three total attempts by default. Ambiguous failures require a mother command.
If Mother routes one upward, Top/Sol `ack` authorizes the next bounded attempt.
Exhaustion creates a dead-letter record and top-supervisor event. Restart
intensity is a separate pool circuit breaker: more than six infrastructure
restarts inside 60 seconds escalates the flock.

A worker-reported success is not trusted. `flock finish --run CHILD_RUN_ID`
allows low-risk `ready_for_review`/`accepted`; high-risk work requires its
hash-bound human gate and `accepted`. The controller re-hashes the lease-bound
task, patch, and green evidence, creates its own review pack, then retains exact
copies under the flock store. Caller-supplied digests cannot satisfy success.

Root recovery re-issues pending semantic obligations for unaffected tasks under
the new coordinator epoch. In `final_review` it is an idempotent no-op that
preserves the review handoff.

## Semantic snapshots and commands

Mother and top are semantic roles mapped in user config to DeepSeek V4 Pro.
They are temporary, stateless invocations, not lifecycle supervisors. The
outbox stores exact canonical snapshot bytes and SHA-256. A snapshot is bounded
to 16 KiB and includes current state, at most six leases, one triggering
subject, bounded alerts/hashes, and a list of commands. It excludes transcripts,
worker prose, old prompts, raw logs, environment values, and hidden reasoning.

The response contract is:

```json
{
  "contract_version": 1,
  "snapshot_id": "...",
  "snapshot_sha256": "...",
  "selected_command_id": "...",
  "reason_code": "bounded_code"
}
```

The runtime validates the hash, command capability, subject revision, flock
revision/state, coordinator epoch, delivery claim, lease, and budget before
applying it. Delivery retry reuses the same snapshot bytes, uses 5/30-second
backoff, and stops after three failed model calls; exhaustion fences the flock
and wakes Sol. Invalid or stale output performs no task/lease mutation. Top may
escalate but cannot abort. Sol receives only a `senior_escalation` or
`aggregate_final_review` snapshot and must review from a fresh context. Its
`approve_flock` action ends at `reviewed`; it does not combine, apply, commit,
push, merge, or deploy child patches.
