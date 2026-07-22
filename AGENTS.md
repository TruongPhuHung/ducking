# Agent Instructions

- Keep `agentctl` provider-neutral. Model names, credentials, CLI flags, and
  provider quirks belong in user worker profiles or adapters, never core state
  or task contracts.
- Use Python 3.11+ standard-library code in V0. Do not add runtime dependencies
  without an explicit design decision.
- Spawn commands as argv arrays with `shell=False`. Never interpolate secrets
  into command strings or persist secret values in run artifacts.
- Treat worker output as untrusted. Git-derived diffs and verifier exit codes
  are authoritative.
- Keep state outside attached project repositories. Do not modify a project's
  main worktree except through the explicit reviewed integration command.
- Preserve schema compatibility. Bump protocol or config schema versions when
  making incompatible changes.

Validate changes with:

```text
python3 -m unittest discover -s tests -v
python3 -m compileall -q agentctl tests
python3 <plugin-creator>/scripts/validate_plugin.py .
python3 <skill-creator>/scripts/quick_validate.py skills/supervise-workers
git diff --check
```
