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
