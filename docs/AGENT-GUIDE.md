# Agent Operating Guide

This guide is the normative operating protocol for a Sol-class planner/reviewer
using `agentctl` to supervise a lower-cost CLI coding worker. The controller,
not the worker transcript, owns run state and evidence.

## 1. Authority and trust boundaries

Keep these roles separate:

- **Sol planner/reviewer:** reads project policy, writes the bounded task
  contract, invokes `agentctl`, reviews the frozen patch and evidence, and
  submits one semantic decision.
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
ACP_ROOT=/absolute/path/to/agent-control-plane
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
  `$XDG_CONFIG_HOME/agent-control-plane/config.toml`, otherwise
  `~/.config/agent-control-plane/config.toml`;
- state: `AGENTCTL_STATE_HOME`, otherwise
  `$XDG_STATE_HOME/agent-control-plane`, otherwise
  `~/.local/state/agent-control-plane`.

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

The optional `schemas/plan.schema.json` can structure a multi-unit plan, but the
current CLI initializes one task JSON at a time; there is no `plan` command.

## 7. Execute the state protocol

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

## 8. Review only frozen artifacts

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

## 9. Integrate, then hand control to the human

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

## 10. `social_match` example

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

## 11. Troubleshooting map

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
  flag.
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
