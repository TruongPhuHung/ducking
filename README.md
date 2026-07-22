# Ducking

`ducking` lets a high-reasoning planner/reviewer supervise bounded, lower-cost
CLI coding workers without coupling project policy to one model or one agent
runtime. The name comes from a flock following its lead duck: Sol stays in
front to choose the route and inspect the result, while worker agents follow
the frozen task contract and execute small units of work.

The repository is both:

- a standalone Python 3.11+ CLI named `agentctl`; and
- a Codex plugin whose `supervise-workers` skill keeps planning and semantic
  review in Codex while deterministic orchestration stays in the CLI.

## Boundaries

The core owns run state, budgets, shallow single-commit workspaces, patch
hashes, validation, and review decisions. A user-owned worker profile owns
executable arguments, environment-variable names, runtime identity, and its
declared isolation boundary. Attached projects own context files, path policy,
risk classification, and validation commands.

Sol or another control authority calls `agentctl`; it never calls OpenCode,
Claude Code, or another worker runtime directly.

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

Detailed guides:

- `docs/AGENT-GUIDE.md` for planner/reviewer agents.
- `docs/USER-GUIDE.html` for human installation and operation.

## Development

```text
python3 -m unittest discover -s tests -v
python3 -m compileall -q agentctl tests
```
