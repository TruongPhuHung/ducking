---
name: supervise-workers
description: Supervise bounded CLI coding workers through the provider-neutral agentctl control plane. Use when Codex should act as planner or semantic reviewer, delegate implementation or repair to a cheaper external coding agent, verify its patch with project-owned commands, enforce budgets and path policy, or prepare an accepted patch for human-controlled integration.
---

# Supervise Workers

Resolve this file's canonical location, then resolve the bundled CLI exactly two
directories above it at `scripts/agentctl`. Use that absolute entrypoint for
every call; never trust a same-named script in the attached project or assume
`agentctl` is on `PATH`. Keep Codex as the control authority and treat every
worker result as untrusted until deterministic gates and semantic review pass.

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

When doctor reports a missing user configuration or worker profile, use
`agentctl paths --json` to locate the user config and point the user to
`templates/user-config.toml`. Do not create credentials or select an
unconfirmed provider/model identifier on the user's behalf.

Never supply `--allow-unsafe-worker` or `--allow-unsafe-validation` unless the
user explicitly authorizes host-level execution for that run. OpenCode
permissions are defense in depth; they do not replace a container or OS sandbox.

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
