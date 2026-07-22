from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import ProjectConfig, ValidationCommand
from .errors import AgentCtlError
from .gitops import DiffSnapshot, scope_violations
from .util import (
    atomic_write_json,
    atomic_write_text,
    ensure_within,
    matches_any,
    sha256_file,
    utc_now,
)


VERIFIER_ENV_ALLOW = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "CI",
)


def _selected_commands(
    config: ProjectConfig,
    profiles: list[str],
    changed_files: tuple[str, ...],
) -> list[ValidationCommand]:
    requested = set(profiles)
    selected: list[ValidationCommand] = []
    for command in config.validation_commands:
        profile_match = "always" in command.profiles or bool(
            requested.intersection(command.profiles)
        )
        path_match = any(
            matches_any(path, list(command.match)) for path in changed_files
        )
        if profile_match and path_match:
            selected.append(command)
    return selected


def evaluate_policy(
    *,
    task: dict[str, Any],
    config: ProjectConfig,
    workspace: Path,
    snapshot: DiffSnapshot,
) -> dict[str, Any]:
    forbidden = [*config.protected_paths, *task.get("forbidden_paths", [])]
    violations = scope_violations(
        snapshot.changed_files,
        allowed_paths=task["allowed_paths"],
        forbidden_paths=forbidden,
    )
    if len(snapshot.changed_files) > config.max_files_per_unit:
        violations.append(
            {
                "path": "*",
                "reason": f"changed_file_budget_exceeded:{len(snapshot.changed_files)}>{config.max_files_per_unit}",
            }
        )
    if snapshot.changed_lines > config.max_changed_lines_per_unit:
        violations.append(
            {
                "path": "*",
                "reason": f"changed_line_budget_exceeded:{snapshot.changed_lines}>{config.max_changed_lines_per_unit}",
            }
        )
    for path in snapshot.binary_files:
        violations.append({"path": path, "reason": "binary_patch_not_allowed"})
    for path in snapshot.symlink_files:
        violations.append({"path": path, "reason": "symlink_patch_not_allowed"})

    requested = set(task["validation_profiles"])
    for path in snapshot.changed_files:
        covered = any(
            requested.intersection(command.profiles)
            and matches_any(path, list(command.match))
            for command in config.validation_commands
        )
        if not covered:
            violations.append(
                {"path": path, "reason": "no_profiled_validation_for_path"}
            )

    return {
        "violations": violations,
        "high_risk_files": [
            path
            for path in snapshot.changed_files
            if matches_any(path, list(config.high_risk_paths))
        ],
        "high_risk_flags": list(task.get("risk_flags", [])),
    }


def _run_command(
    command: ValidationCommand,
    workspace: Path,
    log_dir: Path,
    cancel_file: Path | None = None,
) -> dict[str, Any]:
    cwd = ensure_within(workspace, command.cwd, f"validation command {command.name} cwd")
    if not cwd.is_dir():
        raise AgentCtlError(
            f"Validation cwd does not exist: {command.cwd}",
            code="validation_failed",
        )
    env = {name: os.environ[name] for name in VERIFIER_ENV_ALLOW if name in os.environ}
    env.update(command.env)
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            entry
            for entry in env["PATH"].split(os.pathsep)
            if entry and os.path.isabs(entry)
        )
    configured_executable = command.argv[0]
    if os.path.isabs(configured_executable):
        executable = (
            configured_executable
            if os.access(configured_executable, os.X_OK)
            else None
        )
    elif os.sep in configured_executable or (
        os.altsep and os.altsep in configured_executable
    ):
        executable = None
    else:
        search_path = env.get("PATH", "")
        executable = (
            shutil.which(configured_executable, path=search_path)
            if search_path
            else None
        )
    argv = [executable, *command.argv[1:]] if executable else list(command.argv)
    started = time.monotonic()
    timed_out = False
    cancelled = False
    output_limited = False
    raw_stdout = log_dir / f".{command.name}.stdout.raw"
    raw_stderr = log_dir / f".{command.name}.stderr.raw"
    try:
        with raw_stdout.open("wb") as stdout_handle, raw_stderr.open("wb") as stderr_handle:
            if not executable:
                raise OSError(f"executable not found: {configured_executable}")
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            deadline = started + command.timeout_seconds
            while process.poll() is None:
                if cancel_file and cancel_file.exists():
                    cancelled = True
                if time.monotonic() >= deadline:
                    timed_out = True
                stdout_handle.flush()
                stderr_handle.flush()
                if raw_stdout.stat().st_size + raw_stderr.stat().st_size > 4_000_000:
                    output_limited = True
                if not (cancelled or timed_out or output_limited):
                    time.sleep(0.25)
                    continue
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                    process.wait()
                break
        exit_code = process.returncode
        output_limited = (
            raw_stdout.stat().st_size + raw_stderr.stat().st_size > 4_000_000
        )
        stdout = raw_stdout.read_bytes()[:2_000_000]
        stderr = raw_stderr.read_bytes()[:2_000_000]
    except OSError as exc:
        timed_out = False
        exit_code = 127
        stdout = b""
        stderr = f"agentctl: could not execute validation command: {exc}\n".encode()
    finally:
        raw_stdout.unlink(missing_ok=True)
        raw_stderr.unlink(missing_ok=True)
    if timed_out:
        exit_code = 124
        stderr += b"\nagentctl: validation timed out\n"
    elif cancelled:
        exit_code = 130
        stderr += b"\nagentctl: validation cancelled\n"
    elif output_limited:
        exit_code = 125
        stderr += b"\nagentctl: validation output limit exceeded\n"
    duration_ms = int((time.monotonic() - started) * 1000)
    log_path = log_dir / f"{command.name}.log"
    log = b"STDOUT\n" + stdout + b"\nSTDERR\n" + stderr
    atomic_write_text(log_path, log.decode("utf-8", errors="replace"), mode=0o600)
    return {
        "name": command.name,
        "cwd": command.cwd,
        "argv": argv,
        "env_names": sorted(command.env),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "output_limited": output_limited,
        "duration_ms": duration_ms,
        "log": os.fspath(log_path),
        "log_sha256": sha256_file(log_path),
    }


def build_evidence(
    *,
    run_id: str,
    task: dict[str, Any],
    config: ProjectConfig,
    workspace: Path,
    snapshot: DiffSnapshot,
    evidence_path: Path,
    log_dir: Path,
    execute_commands: bool = True,
    run_controller_check: bool = True,
    cancel_file: Path | None = None,
) -> dict[str, Any]:
    policy = evaluate_policy(
        task=task, config=config, workspace=workspace, snapshot=snapshot
    )
    violations = list(policy["violations"])
    commands = _selected_commands(
        config, task["validation_profiles"], snapshot.changed_files
    )
    if not commands:
        violations.append({"path": "*", "reason": "no_validation_command_selected"})

    log_dir.mkdir(parents=True, exist_ok=True)
    command_results: list[dict[str, Any]] = []
    controller_checks: list[dict[str, Any]] = []
    if run_controller_check:
        controller_check = _run_command(
            ValidationCommand(
                name="controller-patch-check",
                profiles=("always",),
                match=("**",),
                cwd=".",
                argv=(
                    "git",
                    "diff",
                    task["base_sha"],
                    "--check",
                    "--no-ext-diff",
                    "--no-textconv",
                ),
                env={},
                timeout_seconds=60,
            ),
            workspace,
            log_dir,
            cancel_file,
        )
        controller_checks.append(controller_check)
        if controller_check["exit_code"] != 0:
            violations.append(
                {"path": "*", "reason": "controller_patch_check_failed"}
            )

    skipped_reason: str | None = None
    if not execute_commands:
        skipped_reason = "controller_requested_structural_check_only"
    elif violations:
        skipped_reason = "structural_policy_failed"
    else:
        for command in commands:
            command_results.append(
                _run_command(command, workspace, log_dir, cancel_file)
            )
            if cancel_file and cancel_file.exists():
                break

    passed = execute_commands and not violations and all(
        result["exit_code"] == 0 for result in command_results
    )
    evidence = {
        "contract_version": 1,
        "run_id": run_id,
        "generated_at": utc_now(),
        "passed": passed,
        "patch_sha256": snapshot.patch_sha256,
        "changed_files": list(snapshot.changed_files),
        "changed_lines": snapshot.changed_lines,
        "binary_files": list(snapshot.binary_files),
        "symlink_files": list(snapshot.symlink_files),
        "scope_violations": violations,
        "high_risk_files": policy["high_risk_files"],
        "high_risk_flags": policy["high_risk_flags"],
        "controller_checks": controller_checks,
        "commands": command_results,
        "commands_skipped_reason": skipped_reason,
    }
    atomic_write_json(evidence_path, evidence)
    return evidence
