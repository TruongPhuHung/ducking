# Ducking

`ducking` lets a high-reasoning planner/reviewer supervise bounded, lower-cost
CLI coding workers without coupling project policy to one model or one agent
runtime. The name comes from a flock following its lead duck: Sol chooses the
route and performs senior exception/final review, while cheaper worker and
semantic-supervisor agents follow frozen, event-scoped contracts.

The repository is both:

- a standalone Python 3.11+ CLI named `agentctl`; and
- a Codex plugin whose `supervise-workers` skill keeps planning and semantic
  review in Codex while deterministic orchestration stays in the CLI.

## Boundaries

The core owns run/flock state, budgets, leases, EOF, shallow single-commit workspaces, patch
hashes, validation, and review decisions. A user-owned worker profile owns
executable arguments, environment-variable names, runtime identity, and its
declared isolation boundary. Attached projects own context files, path policy,
risk classification, and validation commands.

The OTP-inspired Flock MVP splits deterministic lifecycle control from model
judgment:

```text
RuntimeSupervisor
├── MotherRuntime (durable queue, leases, retry, EOF)
├── DuckPool (six fixed logical slots; bounded leaf replacement)
└── Semantic outbox
    ├── mother -> DeepSeek V4 Pro, fresh event snapshot
    ├── top    -> DeepSeek V4 Pro, fresh event snapshot
    └── sol    -> senior escalation or aggregate final review only
```

DeepSeek supervisors never poll or retain a conversation. Each invocation gets
one canonical snapshot of at most 16 KiB and may select only a pre-issued,
revision-fenced command. Heartbeat summaries and all other worker prose stay
out of semantic snapshots. The deterministic runtime remains authoritative.

Path policy and Git clones are detective controls, not an OS sandbox. Safe
unattended operation requires worker and validator argv that enter a real
container/OS sandbox. `unsafe-host` profiles require explicit per-run flags and
`doctor` reports them as not isolated.

## Quick start

Inspect and attach a repository:

```text
scripts/agentctl project inspect --project /path/to/project --json
scripts/agentctl project attach --project /path/to/project --dry-run
scripts/agentctl project attach --project /path/to/project
```

Review and commit the generated `.agentctl.toml` before starting a run. Runs
freeze committed `HEAD` and refuse a dirty project worktree.

Copy `templates/user-config.toml` to the path reported by `agentctl paths`, then
configure a real worker profile outside the project. No credential value should
appear in either configuration file.

Create and execute a bounded task:

```text
scripts/agentctl run init --project /path/to/project --task task.json
scripts/agentctl unit dispatch --project /path/to/project --run RUN_ID
scripts/agentctl unit verify --project /path/to/project --run RUN_ID
scripts/agentctl review pack --project /path/to/project --run RUN_ID
scripts/agentctl decision submit --project /path/to/project --run RUN_ID --file review.json
scripts/agentctl integrate --project /path/to/project --run RUN_ID --dry-run
```

If a deliberately local prototype uses `unsafe-host`, the human must explicitly
authorize `run init --allow-unsafe-worker` and
`unit verify --allow-unsafe-validation`. Do not add these flags to unattended
automation.

For high-risk patches, Sol submits `human_required`; a human then supplies the
hash-bound artifact from `templates/human-approval.json` through
`decision approve-human` before integration is eligible.

`integrate` defaults to a dry run and never commits, pushes, merges, or deploys.

## Six-duck Flock MVP

Start from `templates/plan.json`. A plan v2 contains atomic task v1 units, a
shared committed base, dependencies, finite attempts, and lease thresholds.
Configure both semantic roles outside the project:

```toml
[semantic_roles]
mother = "deepseek-supervisor"
top = "deepseek-supervisor"
```

Install `templates/opencode-supervisor-agent.md` as the read-only OpenCode agent
`agentctl-supervisor`, then create and drive the durable flock:

```text
scripts/agentctl flock init --project /path/to/project --plan plan.json \
  --allow-unsafe-worker --allow-unsafe-supervisor
scripts/agentctl flock tick --project /path/to/project --flock FLOCK_ID
scripts/agentctl flock heartbeat --project /path/to/project --flock FLOCK_ID \
  --slot 0 --lease LEASE_ID --progress-seq 1 --phase implementing \
  --summary "bounded progress"
scripts/agentctl flock finish --project /path/to/project --flock FLOCK_ID \
  --slot 0 --lease LEASE_ID --outcome succeeded --reason verified_candidate \
  --run CHILD_RUN_ID
scripts/agentctl supervisor dispatch --project /path/to/project \
  --flock FLOCK_ID --role mother --allow-unsafe-supervisor
scripts/agentctl supervisor dispatch --project /path/to/project \
  --flock FLOCK_ID --role top --allow-unsafe-supervisor
scripts/agentctl supervisor next --project /path/to/project \
  --flock FLOCK_ID --role sol
```

The two unsafe flags on `flock init` are required only when the frozen child
worker or mother/top profiles declare `unsafe-host`; they acknowledge host
execution but do not create isolation. They do not replace the separate unsafe
gate on each child `run init` or later `supervisor dispatch`. Omit them only
when those argv enter a real external sandbox.

`flock tick` atomically leases ready units to the six logical slots and embeds
the fenced attempt metadata in each returned task. When that task enters the
leaf pipeline, `unit dispatch` materializes its `ducking/...` branch inside an
independent shallow clone; siblings never share Git metadata. The MVP leaves
child process launch and the existing `run init` → `unit dispatch` →
`unit verify` leaf pipeline to an external runner; it does not yet assemble six
patches automatically. Until the first heartbeat acknowledges an assignment,
another `flock tick` returns the same lease with `replayed: true`; the external
runner must persist and deduplicate by lease before heartbeating so a replay
does not launch a second child run. A retryable leaf failure below the retry and
circuit bounds gets a fresh lease, duck incarnation, isolated branch, and
worktree while healthy siblings continue. Fatal failure, exhausted attempts or
budget, explicit dead-letter, or an open pool/root circuit instead escalates the
aggregate, fences every live sibling lease, and gives every unfinished task a
terminal EOF. Old branches must be sealed or quarantined by the runner.

A successful `flock finish` must name the bound child with
`--run CHILD_RUN_ID`. A low-risk child may be `ready_for_review` or `accepted`;
a high-risk child must first pass its per-child hash-bound human gate and reach
`accepted`. The controller rechecks project, base, config, lease envelope,
task, patch, and green evidence, then creates a Flock-owned review pack and
retains exact bytes with controller-derived digests under the flock store. For
an accepted child it also validates and retains its review and, when high-risk,
human approval. Do not pass `--artifact` for success; caller-supplied success
digests are rejected.

`supervisor dispatch` atomically claims one snapshot before starting DeepSeek,
so concurrent dispatchers cannot duplicate the same model call. Failed delivery
is limited to three attempts with 5- and 30-second backoff; exhaustion fences
the item, escalates the flock, and opens a Sol event. Top DeepSeek may
acknowledge or escalate routine events and may open final review, but it cannot
abort the flock. On an ambiguous `retry_wait`, Top/Sol `ack` explicitly
authorizes the next bounded attempt. Sol alone may choose `abort_flock`; at final review it chooses
`approve_flock`, `rework`, or `abort_flock`. Approval re-hashes the retained
artifacts and ends in state `reviewed`, not `complete`.

`reviewed` is an aggregate review marker only. The MVP still creates no
combined patch, aggregate verifier result, child acceptance decisions, or
integration. It never automatically applies, commits, pushes, merges, or
deploys any patch.

Use `flock sweep` from a local timer to emit soft-stall events and enforce hard
lease/progress EOF. The timer calls deterministic code only; it never wakes a
model unless a durable semantic event exists. Use `flock status` to inspect the
derived `eof` flag. After a MotherRuntime/controller crash, stop the old child
controllers and call `flock recover`; this increments the coordinator epoch,
fences every old lease, synthesizes abnormal EOF, and rebuilds only the affected
slot incarnations. Pending semantic obligations for unaffected tasks are
re-issued against the new epoch; recovery during `final_review` is an
idempotent no-op that preserves the review handoff. Recovery is fenced and idempotent: read the current epoch
from `flock status`, stop the old controller tree, then call, for example,
`flock recover --expected-epoch 1 --operation-id recover-controller-1`.
Retry the same recovery with the same operation ID; a new operation with a stale
epoch is rejected. Literal Erlang-style atoms must never be derived from task
or branch IDs; Ducking keeps those values as bounded strings.

Start the live local control dashboard for an existing flock:

```text
scripts/agentctl flock dashboard --project /path/to/project \
  --flock FLOCK_ID --port 8765 --allow-unsafe-supervisor
```

Open `http://127.0.0.1:8765/`. The SSE view shows all six slots, active leases,
heartbeat/progress age, task budgets, semantic outbox, alerts, and durable
events. It can run `tick` and `sweep`, perform a typed-confirmation aggregate
cancel, and dispatch one pending Mother/Top event after a cost confirmation.
The server binds loopback only and protects mutations with a per-process token.
Omit `--allow-unsafe-supervisor` to keep DeepSeek buttons disabled. The dashboard
visualizes logical assignments; the external runner still owns child launch.

Detailed guides:

- `docs/AGENT-GUIDE.md` for planner/reviewer agents.
- `docs/USER-GUIDE.html` for human installation and operation.

## Development

```text
python3 -m unittest discover -s tests -v
python3 -m compileall -q agentctl tests
```
