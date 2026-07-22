# Agent Operating Guide

This guide is the normative operating protocol for a Sol-class planner/reviewer
using `agentctl` to supervise lower-cost CLI coding workers. It covers both the
single-task evidence pipeline and the OTP-inspired Flock MVP that coordinates
multiple child runs. The deterministic controller, not a worker or semantic
supervisor transcript, owns run state and evidence.

In the Ducking model, Sol is the lead duck: it chooses the plan and performs
required escalation/final review. CLI workers follow one frozen task contract
at a time and never choose their own scope or approve their own patch. In Flock,
stateless DeepSeek mother/top roles handle bounded routine semantic routing so
normal orchestration does not keep a Sol conversation in the loop.

## 1. Authority and trust boundaries

Keep these roles separate:

- **Sol planner/reviewer:** reads project policy and freezes bounded task
  contracts. In a single run it reviews and submits the semantic decision. In a
  Flock it stays out of routine unit handling, then reviews the retained child
  artifacts together at an escalation or aggregate final-review boundary. Its
  Flock decision marks the aggregate `reviewed`, requests rework, or aborts; it
  does not implicitly accept or integrate the child runs.
- **DeepSeek mother/top supervisors (Flock only):** receive fresh, bounded
  snapshots and select one controller-offered command. They have no memory,
  patch authority, scope authority, or release authority.
- **CLI worker:** may edit only its isolated run workspace to satisfy the
  frozen task. Treat its prose, claimed tests, and exit message as untrusted.
- **Deterministic controller/verifier:** freezes hashes, derives the Git diff,
  enforces path and size policy, runs project-owned validation argv, and
  controls state transitions.
- **Human owner:** authorizes unsafe-host execution and high-risk work, and is
  the only role that commits, pushes, merges, or deploys.

`agentctl` never commits, pushes, merges, or deploys. `integrate --apply` only
applies the accepted patch to a clean project worktree. The human must inspect,
stage, commit, and publish it through the project's normal process.

OpenCode permissions are defense-in-depth controls. They are **not an OS
sandbox**. A direct OpenCode process can inherit the host access available to
its operating-system user. For unattended work, place the entire worker argv
inside a real container, VM, or OS sandbox and declare `external-sandbox` only
after that boundary exists.

## 2. Resolve the CLI, configuration, and state paths

The repository requires Python 3.11 or newer and has no runtime Python
dependencies. Prefer the repository launcher because it resolves the module
relative to its own location:

```sh
ACP_ROOT=/absolute/path/to/ducking
ACP="$ACP_ROOT/scripts/agentctl"
"$ACP" version --json
"$ACP" paths --json
```

`scripts/agentctl` searches for Python 3.13, 3.12, 3.11, the bundled Codex
Python, and then `python3`. Set `AGENTCTL_PYTHON` to an absolute Python 3.11+
interpreter when automatic resolution is unsuitable. An editable package
installation (`python3.11 -m pip install -e "$ACP_ROOT"`) also exposes the
`agentctl` console command.

Path resolution is deterministic:

- user config: `AGENTCTL_CONFIG`, otherwise
  `$XDG_CONFIG_HOME/ducking/config.toml`, otherwise
  `~/.config/ducking/config.toml`;
- state: `AGENTCTL_STATE_HOME`, otherwise
  `$XDG_STATE_HOME/ducking`, otherwise
  `~/.local/state/ducking`.

Use `agentctl paths --json` instead of assuming either default. Run state,
patches, logs, receipts, verifier clones, and review artifacts live outside the
attached project. Do not add the state directory to project Git.

## 3. Attach a project and establish its committed base

Attachment writes one project adapter, `.agentctl.toml`; it does not commit it.
Start with read-only inspection and a dry run:

```sh
"$ACP" project inspect --project /absolute/path/to/project --json
"$ACP" project attach --project /absolute/path/to/project --dry-run --json
```

For a real project, copy and tailor `templates/project.toml`, then validate that
exact candidate before attaching:

```sh
"$ACP" project attach \
  --project /absolute/path/to/project \
  --template /absolute/path/to/project-adapter.toml \
  --dry-run --json
"$ACP" project attach \
  --project /absolute/path/to/project \
  --template /absolute/path/to/project-adapter.toml --json
```

Review every field, especially `protected_paths`, `high_risk_paths`, validation
profiles, argv arrays, budgets, and both isolation declarations. Never put a
credential value, provider endpoint, absolute machine path, or shell command
string in `.agentctl.toml`; validator commands are argv arrays.

The human owner must then commit `.agentctl.toml` together with every referenced
instruction or context file. This is a protocol requirement, not housekeeping:
`run init` requires a clean worktree, requires the task `base_sha` to equal the
current `HEAD`, and verifies that required context exists in that committed
base. Agents must not perform the commit unless the user separately and
explicitly authorizes it.

Attachment refuses to overwrite an existing config. Detachment removes only an
unchanged config that has an attachment receipt and has no active runs; run
evidence is preserved:

```sh
"$ACP" project detach --project /absolute/path/to/project --dry-run --json
"$ACP" project detach --project /absolute/path/to/project --json
```

## 4. Configure the OpenCode/DeepSeek worker outside the project

Install and authenticate OpenCode separately. Before writing a model argument,
query the installed runtime:

```sh
opencode --version
opencode auth list
opencode models --refresh
opencode models
```

Model availability and IDs are runtime/provider dependent. Copy the exact
`provider/model` value printed by `opencode models`; do not assume that an ID in
an example or `templates/user-config.toml` is available. In particular,
"DeepSeek V4 Pro" is a desired capability label, not a guaranteed OpenCode
model identifier.

Copy `templates/user-config.toml` to the path returned by `agentctl paths`, then
use a profile like this after replacing the model placeholder:

```toml
schema_version = 1

[worker_profiles.default-implementer]
adapter = "generic-cli"
runtime_id = "opencode-deepseek-worker"
isolation = "unsafe-host"
argv = [
  "opencode", "run",
  "--format", "json",
  "--model", "provider/model-confirmed-by-opencode-models",
  "--agent", "agentctl-worker",
  "--dir", "{workspace}",
  "{prompt}"
]
probe_argv = ["opencode", "models"]
probe_contains = "provider/model-confirmed-by-opencode-models"
env_allow = ["HOME", "XDG_CONFIG_HOME", "DEEPSEEK_API_KEY"]
timeout_seconds = 1500
max_output_bytes = 8000000
prompt = "Read the request contract at {request_file}, then read every task.context_files entry. Implement only its bounded objective. Do not commit, push, access external paths, or change the contract. Leave the workspace ready for controller verification."
```

Install `templates/opencode-agent.md` as the dedicated OpenCode agent named
`agentctl-worker`. With OpenCode's default global configuration path:

```sh
mkdir -p "$HOME/.config/opencode/agents"
cp "$ACP_ROOT/templates/opencode-agent.md" \
  "$HOME/.config/opencode/agents/agentctl-worker.md"
```

The Markdown filename becomes the agent name. A per-project alternative is
`.opencode/agents/agentctl-worker.md`, but the global copy keeps runtime policy
outside an attached project's reviewed patch surface. If you replace the
template, preserve an equivalent deny-first policy. Keep `probe_contains`
identical to the exact model ID used by `--model`; `run init` refuses a probe
whose output does not contain that identity.

The controller passes only a small base environment plus names explicitly
listed in `env_allow`; it redacts listed environment values from captured
worker stdout/stderr. Keep credentials in OpenCode's auth store or the process
environment, never in TOML, JSON, prompts, or project files. Redaction is a
last-resort log control, not permission to expose secrets to the worker.

For direct OpenCode execution, keep `isolation = "unsafe-host"`. That forces an
explicit `--allow-unsafe-worker` at run initialization. A profile may say
`external-sandbox` only when its argv actually enters a container, VM, or OS
sandbox before OpenCode starts. `agentctl` trusts this declaration; `doctor`
does not prove the sandbox exists.

Configure OpenCode permissions to deny external directories, commit/push, web
access, and other unneeded actions. This reduces accidental capability but does
not replace the external sandbox. Do not add OpenCode `--auto` to an unsafe-host
profile: it auto-approves permission requests that are not explicitly denied.
Check the effective permissions of the agent named by `--agent`; OpenCode
agent-specific rules are merged with, and can override, global rules. Prefer a
dedicated worker agent with an explicit deny policy.

## 5. Preflight and interpret `doctor`

Run before every new task:

```sh
"$ACP" doctor --project /absolute/path/to/project --json
```

Treat missing/invalid project config, dirty worktree, uncommitted config or
context, missing worker profile, failed worker probe, and missing validator
executables as blockers. `doctor` deliberately reports unsafe-host worker or
validator isolation as non-green, so its top-level `ok` remains false in an
unsafe setup. Proceed in that mode only when the human explicitly authorizes
the corresponding unsafe flags; the flags acknowledge risk and do not create
isolation.

The supplied worker probe runs unfiltered `opencode models` and requires the
configured `probe_contains` model ID. This checks runtime visibility, but does
not prove authentication will succeed during generation, OpenCode permission
quality, network availability, or the truth of an `external-sandbox`
declaration. Test the sandbox independently. An external-sandbox profile must
probe through the same wrapper executable used for dispatch.

## 6. Author a bounded task contract

Start from `templates/task.json`. The task is immutable after `run init` and
must contain:

- `contract_version: 1` and a stable lowercase `task_id`;
- `base_sha` equal to the current committed project `HEAD`;
- one externally observable `objective`;
- at least one acceptance criterion with a unique ID and proof type `test`,
  `diff`, or `manual`;
- repository-relative `context_files`, `allowed_paths`, and
  `forbidden_paths`;
- one or more validation profiles defined by `.agentctl.toml`;
- explicit non-goals and risk flags;
- finite `wall_seconds` and `max_fix_rounds`.

Use one write owner and the smallest useful path surface. Never put CLI flags,
provider/model names, secrets, hidden reasoning, or mutable product decisions
in the task. If ambiguity changes behavior, security, privacy, data shape,
dependencies, or release scope, stop and obtain human direction before
freezing the contract.

All path globs are anchored at the repository root. `*` never crosses `/`,
while `**` does: `.env*` protects only root-level env files, so include
`**/.env*` when nested env files must also be forbidden.

For one unit, continue with the single-run protocol below. For a dependency DAG,
wrap complete task contracts in a Flock plan and use `flock init`; the Flock
control layer does not weaken any child task boundary.

## 7. Operate the OTP-inspired Flock MVP

Use Flock only when several already-bounded units benefit from dependency-aware
coordination. It is an OTP-inspired controller, not Erlang/OTP and not a daemon.
The implementation divides runtime responsibility as follows:

- `RuntimeSupervisor` deterministically owns flock state, coordinator epoch,
  lease sweeps, alerts, restart intensity, and cancellation/escalation;
- `MotherRuntime` deterministically assigns dependency-ready units to idle
  logical slots when `flock tick` is called;
- exactly six duck slots, IDs 0 through 5, exist for every flock; plans cannot
  resize the pool, but external-runner capacity and the child project's
  `max_parallel` policy may make physical concurrency lower than six;
- the semantic `mother` and `top` are fresh DeepSeek invocations that select
  from commands pre-authorized by the controller; neither is the deterministic
  `MotherRuntime` scheduler;
- Sol is outside that normal loop and is opened only for senior escalation or
  aggregate final review.

### 7.1 Freeze semantic profiles and install the read-only supervisor

The user config must map both external semantic roles to existing profiles. The
provided template intentionally maps both roles to the same DeepSeek profile:

```toml
[semantic_roles]
mother = "deepseek-supervisor"
top = "deepseek-supervisor"
```

The referenced profile uses the same provider-neutral `generic-cli` adapter as
a worker, but its prompt requires one bound JSON action and its OpenCode agent is
read-only. Install the supplied policy under the agent name used by the profile:

```sh
mkdir -p "$HOME/.config/opencode/agents"
cp "$ACP_ROOT/templates/opencode-supervisor-agent.md" \
  "$HOME/.config/opencode/agents/agentctl-supervisor.md"
```

Keep the exact provider/model ID, CLI argv, credentials, and isolation outside
the plan. If the child implementation profile is `unsafe-host`, `flock init`
requires `--allow-unsafe-worker`. If either semantic profile is `unsafe-host`,
initialization requires `--allow-unsafe-supervisor`, and every later
`supervisor dispatch` requires that flag again. These flags are separate,
invocation-scoped acknowledgements. For unattended use, wrap the whole argv in
a real sandbox before declaring `external-sandbox`.

At `flock init`, the controller probes and freezes the child implementation
profile and both semantic roles. Each semantic dispatch creates a fresh
snapshot-only workspace. The snapshot is at most 16 KiB, excludes prior
conversation, worker transcripts, heartbeat summaries, all other worker prose,
and raw logs, and contains bounded controller facts plus an allowlist of command
IDs. There is no model fallback and Sol is deliberately absent from
`[semantic_roles]`.

### 7.2 Author the multi-unit plan

Start from `templates/plan.json` and validate against
`schemas/plan.schema.json`. A Flock plan uses `contract_version: 2`, a stable
`plan_id`, optional retry/lease policy, and `depends_on` on each complete unit.
The nested unit contracts remain task contract version 1. Every unit must
independently pass normal task validation and use the same `base_sha` as the
plan. Dependencies must exist and form an acyclic graph.

```json
{
  "contract_version": 2,
  "plan_id": "profile-screen-flock",
  "base_sha": "CURRENT_CLEAN_HEAD_SHA",
  "goal": "Implement and document the approved profile screen slice.",
  "assumptions": [],
  "non_goals": ["No deployment", "No schema migration"],
  "retry": {"max_attempts": 3, "delays_seconds": [5, 30]},
  "lease": {
    "liveness_soft_seconds": 45,
    "liveness_hard_seconds": 90,
    "progress_soft_seconds": 120,
    "progress_hard_seconds": 600
  },
  "units": [
    {
      "contract_version": 1,
      "task_id": "profile-api",
      "base_sha": "CURRENT_CLEAN_HEAD_SHA",
      "objective": "Implement the approved profile read endpoint.",
      "acceptance": [
        {"id": "api-1", "claim": "The endpoint passes its focused tests.", "proof": "test"}
      ],
      "non_goals": ["No client changes"],
      "context_files": ["AGENTS.md", "docs/ARCHITECTURE.md"],
      "allowed_paths": ["server/**"],
      "forbidden_paths": [".agentctl.toml", ".env*", "**/.env*"],
      "validation_profiles": ["server"],
      "risk_flags": [],
      "budget": {"wall_seconds": 1200, "max_fix_rounds": 1},
      "depends_on": []
    }
  ]
}
```

The pool size is not plan data. Defaults are three attempts, delays of 5 and 30
seconds, liveness soft/hard thresholds of 45/90 seconds, and progress soft/hard
thresholds of 120/600 seconds. `max_attempts` may not exceed 10. A unit's
`budget.wall_seconds` is cumulative across its Flock attempts; exhausting it
dead-letters the task even when an attempt count remains.

### 7.3 Initialize and drive the deterministic control loop

Initialization requires a clean attached project and a plan base equal to the
current `HEAD`. It also requires the committed project adapter to match the
loaded file, validates every unit's profiles/context, and probes the child plus
both semantic runtimes before creating state:

```sh
"$ACP" flock init \
  --project /absolute/path/to/project \
  --plan /absolute/path/to/plan.json --json

# Save the returned flock_id exactly as FLOCK_ID.
"$ACP" flock status --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --json
"$ACP" supervisor profiles --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --json
```

For a deliberately authorized all-`unsafe-host` prototype, initialization is:

```sh
"$ACP" flock init \
  --project /absolute/path/to/project \
  --plan /absolute/path/to/plan.json \
  --allow-unsafe-worker --allow-unsafe-supervisor --json
```

Omit each unsafe flag only when the corresponding frozen argv enters a real
external sandbox. Initialization authority does not carry into child
`run init`, validation, or semantic dispatch; those commands retain their own
gates.

Call `tick` to lease currently ready units:

```sh
"$ACP" flock tick --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --json
```

**Important MVP boundary:** `tick` leases logical slots and returns assignment
records. It does not spawn six processes or invoke a worker. An external runner
must persist the assignment's `slot_id`, `duck_incarnation`, `lease_id`, and
task contract; serialize that returned task unchanged outside the project; then
drive its child pipeline:

```text
run init -> unit dispatch -> unit verify -> ready_for_review
```

A low-risk child may be handed to `flock finish` at `ready_for_review` or after
its normal review reaches `accepted`. A high-risk child may not stop at green
verification: submit its per-child `human_required` review and hash-bound human
approval through the single-run protocol until its state is `accepted`, then
report Flock success.

Before the first heartbeat, `flock tick` replays the same unacknowledged
assignment with `replayed: true`; it does not mint another lease. Persist the
lease-to-child-run mapping first, deduplicate tick responses by lease ID, and
only then heartbeat. Otherwise a runner crash can start a duplicate child or
acknowledge delivery before it has saved the mapping.

Use the execution commands and unsafe gates in section 8 for each child run.
The runner must never report success merely because a worker exited zero. A
normal Flock unit reaches success only through green deterministic evidence and
the bound `flock finish --run CHILD_RUN_ID` check described below.

The assignment task contains a controller-owned `flock_attempt` envelope. On
the first `unit dispatch`, the child controller materializes its `branch_ref` as
a local `ducking/...` branch inside that run's independent shallow clone. It
does not create a branch in the source repository or share Git metadata with
another duck. `depends_on` only gates when `tick` may issue the assignment; it
does not apply an upstream patch to the downstream unit's frozen base. Until an
external assembly/rebase layer exists, use dependencies for ordering and keep
units patch-independent or have the operator explicitly manage that handoff.

While a child pipeline runs, refresh liveness and report monotonic progress:

```sh
"$ACP" flock heartbeat --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --slot 0 --lease "$LEASE_ID" \
  --progress-seq 1 --phase verifying --summary "focused validators running" --json
```

Repeating an identical sequence, phase, and summary is only a heartbeat;
increasing `progress-seq` refreshes the progress clock. A lower sequence or the
same sequence with different content is rejected. Summaries are bounded and
must not contain secrets or raw logs. They are stored only as untrusted runtime
diagnostics and are never copied into mother, top, or Sol semantic snapshots.

After the eligible child has green evidence and is either low-risk
`ready_for_review`/`accepted` or high-risk `accepted`, close the lease with an
explicit attempt EOF bound to that child run:

```sh
"$ACP" flock finish --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --slot 0 --lease "$LEASE_ID" \
  --outcome succeeded --reason child_run_verified \
  --run "$CHILD_RUN_ID" --json
```

For success, the controller verifies the child project/base/config, exact
`flock_attempt` lease envelope, task hash, eligible child state, patch hash, and
green evidence. It re-hashes task/patch/evidence, creates a new Flock-owned
review pack, and retains those exact bytes under the flock store. For an
accepted child it also validates and retains the child review and any required
high-risk human approval. The EOF receives controller-derived hashes. Do not
pass `--artifact` on success; caller-supplied success digests are rejected.

Other outcomes are `retryable_failure`, `fatal_failure`, and `cancelled`; they
must not pass `--run` and may carry optional hash-only diagnostic artifacts.
Every attempt gets an attempt EOF, and a terminal task gets one task EOF. A
worker-reported cancellation escalates and drains the flock; use `flock cancel`
for an intentional aggregate cancellation.

### 7.4 Sweep leases, handle leaf replacement, and drain aggregate failures

Run lease sweeps on an external timer or control loop:

```sh
"$ACP" flock sweep --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --json
```

Soft liveness/progress expiry marks one task `soft_stalled` and queues a mother
snapshot. Hard expiry writes a synthetic attempt EOF with
`worker_eof_seen: false`, releases that one slot, increments only that slot's
incarnation, and enters retry handling. While a leaf failure remains retryable
and below the restart circuit, replacement is one-for-one: other slots and
valid leases continue. Cancellation terminates only that task and releases its
slot; it does not schedule a replacement attempt or escalate the aggregate.

Fatal failure, exhausted attempts or wall budget, semantic `dead_letter`, and
an open pool/root circuit are aggregate escalation boundaries. The controller
records the triggering terminal condition, fences and drains every active
sibling lease, and gives every unfinished sibling a terminal escalated EOF. The
DLQ is the durable terminal record plus its alert/top outbox item; the MVP does
not run a separate message broker. More than six supervised restarts in the
default 60-second window opens the pool circuit. A late heartbeat or EOF is
rejected because a lease binds its ID, coordinator epoch, slot, and incarnation.

Coordinator/root recovery is deliberately separate. Only after the external
runtime has stopped the old controller processes, read `coordinator_epoch` from
`flock status` and fence that exact generation with a stable operation ID:

```sh
"$ACP" flock recover --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --expected-epoch "$EXPECTED_EPOCH" \
  --operation-id recover-controller-1 --reason coordinator_lost --json
```

This command does not kill anything. It closes all active leases as
`controller_lost`, increments the coordinator epoch, and lets the six-slot
subtree retry under the new generation. It re-issues pending semantic
obligations for unaffected tasks against that epoch. During `final_review`,
recovery is a recorded no-op and preserves the pending review. Retrying the same operation ID returns
the recorded result; a new operation with a stale expected epoch fails with
`stale_recovery`. More than three recoveries in the default 60-second window
opens the root circuit and escalates. Never use `recover` as a substitute for
proving that the old processes are gone, and never invent a new operation ID
merely to bypass an epoch mismatch.

For live observation and bounded operator controls, start the loopback-only
dashboard:

```sh
"$ACP" flock dashboard --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --port 8765 --allow-unsafe-supervisor
```

It streams a credential-free live graph of Sol, Top, Mother, the six slots,
tasks, lease ages, semantic outbox, alerts, and durable events over SSE. Each
duck shows a short state-derived quack; it is bounded display text, not model
conversation or shared context. Mutating requests require
the page's per-process action token; cancellation additionally requires the
exact flock ID, and Mother/Top dispatch requires an explicit cost confirmation.
Without `--allow-unsafe-supervisor`, those dispatch buttons remain disabled.
The dashboard does not spawn child processes or replace the external runner.
Sol should remain asleep while its pending count is zero. A resident runner or
notification adapter owns heartbeat/sweep polling and wakes Sol only for a
durable replan, senior anomaly, or final-review event; do not put
`supervisor next --role sol` in a Sol conversation loop.

Drain one pending mother/top snapshot per dispatch call:

```sh
"$ACP" supervisor next --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --role mother --json
"$ACP" supervisor dispatch --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --role mother --allow-unsafe-supervisor --json
"$ACP" supervisor dispatch --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --role top --allow-unsafe-supervisor --json
```

Omit the unsafe flag only when the frozen profile enters a real external
sandbox. Before starting DeepSeek, dispatch atomically claims the snapshot.
Another dispatcher cannot claim it until the claim expires; an older claimant
cannot submit after a newer claim takes over. A failed delivery clears its
claim, retries at most three times with 5- then 30-second backoff, and on the
third failure marks the item exhausted, escalates the flock, and queues Sol.

DeepSeek can select only the offered, hash-bound command at the current subject
and flock revisions, coordinator epoch, and flock state. Mother commands include
`wait`, `recycle`, `retry_task`, `dead_letter`, `notify_top`, and bounded no-ops.
Top commands include only `ack`, `escalate_sol`, and `open_final_review` when
the aggregate is ready. For an ambiguous `retry_wait`, Top or Sol `ack`
authorizes the next bounded attempt. Top DeepSeek is never authorized to select
`abort_flock`. For manual or alternate semantic delivery, inspect `supervisor
next`, construct the exact bound action, and apply it with:

```sh
"$ACP" supervisor apply --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --file /absolute/path/to/action.json --json
```

Do not manufacture or broaden an allowed command. A stale snapshot hash,
delivery claim, command ID, subject/flock revision, coordinator epoch, or flock
state must fail closed.

Poll Sol only after top routing opens that path:

```sh
"$ACP" supervisor next --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --role sol --json
```

Sol handles only senior escalation and aggregate final review. It does not
replace normal `tick`, `heartbeat`, `sweep`, retry, mother, or top processing.
At final review, the offered commands are `approve_flock`, `rework`, and
`abort_flock`; an escalation offers `ack` or `abort_flock`. Apply the selected
bound action through `supervisor apply`. Review the retained child task, patch,
evidence, and Flock-owned review pack plus the aggregate manifest before
choosing.
`approve_flock` re-hashes the retained set and marks the terminal aggregate
state `reviewed`, not `complete`. It does not accept or integrate any child run,
and it creates no combined patch or aggregate verification. `rework` and
`abort_flock` take the flock to escalation.

Continue `tick`, child execution, sweep, and semantic dispatch until the
operator reaches the appropriate review/escalation boundary. Cancel explicitly
when required:

```sh
"$ACP" flock cancel --project /absolute/path/to/project \
  --flock "$FLOCK_ID" --json
```

### 7.5 Current non-goals and the cost illustration

The Flock MVP retains verified task/patch/evidence bytes and creates a
Flock-owned review pack for each successful child. It additionally retains the
validated child review and required human approval when a child is accepted,
but it does **not** assemble those patches in
dependency order, rebase them, resolve conflicts, run an aggregate verifier, or
produce a combined patch/review pack. State `reviewed` is not integration
eligibility. There is no automatic child decision, apply, integration, commit,
push, merge, release, or deploy; Sol and the human receive retained evidence,
not a release artifact.

One recorded cached-context comparison contained 42,650 cache-miss input
tokens, 1,133,696 cache-read tokens, and 32,216 output tokens. At that trace's
assumed rates, DeepSeek cost **$0.050690**. The same token mix priced as a Sol
counterfactual cost **$1.746578**: $0.213250 miss input, $0.566848 cached input,
and $0.966480 output. The illustrative saving is **97.10%**.

The trace's input cache-hit ratio is **96.37%**:
`1,133,696 / (1,133,696 + 42,650)`. Cache-read tokens are **93.805%** of all
counted tokens including output:
`1,133,696 / (42,650 + 1,133,696 + 32,216)`.

Treat this as one counterfactual, not a benchmark or promise. Prices, cache
eligibility and hit rate, token accounting, prompts, routing, and quality may
change, and this unusually high cache share is not guaranteed. Cost never
overrides deterministic evidence, security gates, or Sol's required
escalation/final review. Recalculate with current
[GPT-5.6 Sol pricing](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
and [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)
before using the example as a budget forecast.

## 8. Execute the single-run state protocol

Initialize from a clean, committed base:

```sh
"$ACP" run init \
  --project /absolute/path/to/project \
  --task /absolute/path/to/task.json --json
```

For a deliberately authorized direct host worker, add
`--allow-unsafe-worker`. Save the returned `run_id` as `RUN_ID`; do not infer it
from directory names.

Dispatch and verify:

```sh
"$ACP" unit dispatch --project /absolute/path/to/project --run "$RUN_ID" --json
"$ACP" unit verify --project /absolute/path/to/project --run "$RUN_ID" --json
```

For deliberately authorized host validators, add
`--allow-unsafe-validation` to each `unit verify` invocation. Validation runs
against a fresh clone with the frozen patch applied. Project validators can
execute candidate code and may perform network or dependency operations if the
project argv says so; sandbox them accordingly.

The normal path is:

```text
initialized -> dispatching -> candidate -> verifying -> ready_for_review
            -> reviewing -> accepted -> integrated
```

Recovery and authority branches are:

```text
verify_failed  -> dispatching                 (mechanical repair attempt)
repair_queued  -> dispatching                 (bounded semantic repair)
human_required -> accepted                    (hash-bound human approval)
reviewing      -> replan_required             (new task/base required)
active state   -> cancelled                   (where transition permits)
dispatch/verify errors -> failed
```

`verify_failed` evidence is automatically included in the next worker request.
Do not spend semantic review on a mechanically failing candidate. Repair
rounds are capped by the smaller of the project and task repair limits.

Inspect status or request cancellation with:

```sh
"$ACP" run status --project /absolute/path/to/project --run "$RUN_ID" --json
"$ACP" run status --project /absolute/path/to/project --json
"$ACP" run cancel --project /absolute/path/to/project --run "$RUN_ID" --json
```

During dispatch or verification, cancellation is cooperative and returns
`cancel_requested`; poll status until the process exits.

## 9. Review only frozen artifacts

After `ready_for_review`, build the pack:

```sh
"$ACP" review pack --project /absolute/path/to/project --run "$RUN_ID" --json
```

Open the returned `review_pack` path. Review only its frozen task, Git-derived
patch, deterministic evidence, project invariants, and explicit uncertainty.
The pack states `worker_self_report_trusted: false`. Never accept based on the
worker transcript.

Start from `templates/review.json` and copy the exact `run_id`, `patch_sha256`,
`task_sha256`, and `evidence_sha256`. Cover every acceptance ID exactly once for
`accept`. Each finding must include a unique ID, severity (`blocker`, `major`,
or `minor`), concrete failure mode, and required change.

Choose exactly one decision:

- `accept`: every acceptance criterion passes, deterministic evidence is
  green, no blocker/major finding remains, and no high-risk file or flag is
  present;
- `repair`: the task remains valid and at least one bounded blocker/major
  finding is repairable within budget;
- `replan`: the objective, scope, acceptance, dependency shape, or base must
  change;
- `human_required`: the patch is mechanically green but touches a high-risk
  path/flag or needs product/security authority.

Every non-`accept` decision must include at least one actionable finding;
`repair` specifically requires a blocker or major finding.

Submit the decision:

```sh
"$ACP" decision submit \
  --project /absolute/path/to/project \
  --run "$RUN_ID" --file /absolute/path/to/review.json --json
```

Any candidate change alters the patch hash and invalidates the decision. A
high-risk patch cannot be submitted as `accept`.

For `human_required`, the human reviews the exact frozen artifacts and fills
`templates/human-approval.json` with all four matching hashes plus an identity
and rationale:

`approved_by` is a machine identifier matching
`[a-z0-9][a-z0-9._-]{0,127}` (for example `truongphuhung`), not a display name
or email address.

```sh
"$ACP" decision approve-human \
  --project /absolute/path/to/project \
  --run "$RUN_ID" --file /absolute/path/to/human-approval.json --json
```

Agents must not manufacture human identity, rationale, or approval.

## 10. Integrate, then hand control to the human

Always check first; omitting both mode flags is also a dry run:

```sh
"$ACP" integrate --project /absolute/path/to/project --run "$RUN_ID" --dry-run --json
```

Integration requires the project worktree to be clean and `HEAD` to equal the
reviewed base SHA. With explicit human authority, apply the patch:

```sh
"$ACP" integrate --project /absolute/path/to/project --run "$RUN_ID" --apply --json
```

The returned result explicitly reports `committed: false` and `pushed: false`.
Stop after presenting the worktree diff and validation evidence. The human owns
all subsequent Git and release operations.

## 11. `social_match` example

The prepared `social_match/.agentctl.toml` uses `default-implementer`, committed
project instructions/context, narrow protected/high-risk paths, and profiles
such as `docs`, `client`, `server`, `vertical-slice`, and `containers`. Before
its first run, the human must inspect and commit `.agentctl.toml` and the updated
`AGENTS.md`; otherwise `run init` correctly rejects the dirty/uncommitted base.

For a docs-only task, use a task outside the repository with:

```json
{
  "contract_version": 1,
  "task_id": "clarify-match-rule",
  "base_sha": "CURRENT_SOCIAL_MATCH_HEAD_SHA",
  "objective": "Clarify one already-approved match rule in the product requirements without changing behavior.",
  "acceptance": [
    {"id": "ac1", "claim": "The existing rule is stated unambiguously and introduces no new behavior.", "proof": "diff"}
  ],
  "non_goals": ["No runtime code changes", "No new product policy"],
  "context_files": ["docs/product/requirements.md"],
  "allowed_paths": ["docs/product/requirements.md"],
  "forbidden_paths": ["AGENTS.md", ".agentctl.toml", ".github/**", ".env*", "**/.env*"],
  "validation_profiles": ["docs"],
  "risk_flags": [],
  "budget": {"wall_seconds": 900, "max_fix_rounds": 1}
}
```

Replace the SHA with the exact clean `HEAD`. Run `doctor`; local machines
without `mix` will report the server validator probes as unavailable even for
this docs example, and both configured isolation checks are non-green because
the prepared adapter declares `unsafe-host`. Do not reinterpret those warnings
as sandboxing. Install the required project tools or use a real external
sandbox, and obtain explicit human authority before either unsafe flag.

## 12. Troubleshooting map

- `dirty_worktree`: commit or otherwise resolve all intended human changes;
  never discard unrelated work automatically.
- `context_not_in_base` or `config_in_base: false`: commit the adapter and every
  referenced context/instruction file, then regenerate the task with the new
  `HEAD`.
- `worker_profile_not_found`: make `.agentctl.toml`'s semantic profile name
  match a user-config table.
- `worker_unavailable`: fix `PATH`, `probe_argv`, Python/OpenCode installation,
  or use `--user-config`/`AGENTCTL_CONFIG` to select the intended config.
- model/provider failure after a successful probe: authenticate separately and
  copy the exact ID from `opencode models`; `doctor` does not validate it.
- `unsafe_worker_requires_opt_in` or `unsafe_validation_requires_opt_in`: use a
  real sandbox, or stop for explicit human authority before adding the named
  flag. Flock checks the child worker at `flock init`, and each child `run init`
  remains a separate unsafe gate.
- `unsafe_supervisor_requires_opt_in`: the frozen mother/top profile executes
  directly on the host. `flock init` and every later `supervisor dispatch`
  require explicit authority. Use a real sandbox for unattended automation.
- `semantic_supervisor_unavailable`: `flock init` could not probe a configured
  mother/top runtime. Fix the referenced profile, executable, model identity,
  authentication, or sandbox wrapper before creating the flock.
- `stale_lease`, `stale_progress`, or `progress_seq_collision`: the external
  runner used an old slot/lease/incarnation tuple, moved the progress sequence
  backwards, or changed content without incrementing it. Reload `flock status`
  and never let an old child process close a replacement lease.
- repeated assignment with `replayed: true`: no heartbeat has acknowledged that
  lease yet. Reuse the persisted lease-to-run mapping; do not start a duplicate
  child. Persist first and heartbeat second.
- `unverified_child_result`, `child_not_ready_for_review`, `human_gate_required`,
  `review_mismatch`, or `evidence_failed` on successful `flock finish`: do not
  replace `--run` with hand-written digests. Complete the exact lease-bound
  low-risk child through green verification (`ready_for_review` or `accepted`),
  or the high-risk child through its per-child hash-bound gate to `accepted`.
  Preserve that child store and retry with the same child run ID.
- `stale_recovery`: the current coordinator epoch differs from
  `--expected-epoch`. Do not bypass the fence with a new operation ID; inspect
  status and establish which controller generation is alive. Retry an already
  completed recovery only with its original operation ID.
- `semantic_invalid_response`, `stale_decision`, or
  `invalid_semantic_command`: preserve the pending snapshot and retry with a
  fresh stateless invocation bound to its exact ID, claim, hash, offered command
  ID, revisions, epoch, and state. Delivery uses at most three attempts with
  5/30-second backoff; exhaustion escalates to Sol. Never broaden the command,
  let top abort, or copy hashes around the gate.
- Flock has ready work but no execution: `flock tick` only returned logical
  assignments. Check the external runner, its lease-to-child-run mapping,
  heartbeat timer, `flock sweep`, and pending mother/top outbox; there is no
  resident process launcher in the MVP.
- `unknown_validation_profile` or `no_profiled_validation_for_path`: align the
  task profiles and changed-path match rules with `.agentctl.toml`.
- `verify_failed`: inspect `evidence.json` and verifier log hashes; dispatch a
  bounded mechanical repair while budget remains.
- `config_drift`, `patch_drift`, `artifact_tampered`, or `review_mismatch`: stop;
  do not copy hashes around the gate. Restore the frozen inputs or create a new
  run from a clean current base.
- `base_sha_drift` at integration: the reviewed base is no longer current;
  replan/re-run rather than applying an old decision to a new base.
- timeout/output-limit/exit 127: inspect the run root returned by `run status`,
  fix the runtime or bounded command, and do not treat missing logs as success.

When uncertain, preserve the state directory and report the exact error code,
run ID, base SHA, patch SHA, and failed check without exposing credential
values.
