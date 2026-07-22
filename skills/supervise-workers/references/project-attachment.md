# Project Attachment

Keep generic orchestration outside the project. A project config may define:

- semantic worker profile name;
- instruction and context files;
- protected, allowed, and high-risk path patterns;
- validation profiles expressed as cwd plus argv arrays;
- file, line, time, and repair budgets.
- a patch-byte ceiling and declared validator isolation mode.

It must not contain credential values, absolute machine paths, provider API
endpoints, raw shell strings, or model-specific prompt syntax.

Use `project attach --dry-run` before attachment and `doctor` afterward. Attach
is idempotent and must not overwrite an existing config. Review and commit the
config before dispatch: workers receive committed `HEAD`, and run initialization
refuses dirty worktrees or context absent from the base commit. Detach may remove only
an unchanged config that the tool previously created; it must preserve run
evidence by default and refuse while a run remains active.

Validation profile names are contracts, not labels. Define at least one
requested non-`always` command that covers every path a task may change. Mark
validator argv as `external-sandbox` only when those commands really enter a
container or OS sandbox; otherwise use `unsafe-host` and require explicit human
authorization for each verification.
