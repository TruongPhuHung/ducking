from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AgentCtlError
from .util import (
    ensure_within,
    require_env_name,
    require_identifier,
    require_relative_path,
    require_relative_pattern,
)


PROJECT_CONFIG_NAME = ".agentctl.toml"
SUPPORTED_CONFIG_SCHEMA = 1
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
ALLOWED_WORKER_PLACEHOLDERS = {
    "workspace",
    "request_file",
    "events_file",
    "run_id",
    "prompt",
}


@dataclass(frozen=True)
class ValidationCommand:
    name: str
    profiles: tuple[str, ...]
    match: tuple[str, ...]
    cwd: str
    argv: tuple[str, ...]
    env: dict[str, str]
    timeout_seconds: int


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    schema_version: int
    project_id: str
    worker_profile: str
    instruction_files: tuple[str, ...]
    context_files: tuple[str, ...]
    protected_paths: tuple[str, ...]
    high_risk_paths: tuple[str, ...]
    max_parallel: int
    max_fix_rounds: int
    max_files_per_unit: int
    max_changed_lines_per_unit: int
    max_patch_bytes: int
    validation_isolation: str
    validation_commands: tuple[ValidationCommand, ...]


@dataclass(frozen=True)
class WorkerProfile:
    name: str
    adapter: str
    argv: tuple[str, ...]
    probe_argv: tuple[str, ...]
    probe_contains: str
    env_allow: tuple[str, ...]
    timeout_seconds: int
    max_output_bytes: int
    prompt: str
    isolation: str
    runtime_id: str


@dataclass(frozen=True)
class UserConfig:
    path: Path
    schema_version: int
    worker_profiles: dict[str, WorkerProfile]
    semantic_roles: dict[str, str]


def default_user_config_path() -> Path:
    explicit = os.environ.get("AGENTCTL_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ).expanduser()
    return config_home / "ducking" / "config.toml"


def default_state_home() -> Path:
    explicit = os.environ.get("AGENTCTL_STATE_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    ).expanduser()
    return state_home / "ducking"


def _read_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise AgentCtlError(
            f"{label} does not exist: {path}", code="config_not_found"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise AgentCtlError(
            f"Invalid TOML in {path}: {exc}", code="invalid_config"
        ) from exc
    if not isinstance(value, dict):
        raise AgentCtlError(f"{label} must be a TOML table", code="invalid_config")
    return value


def _positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise AgentCtlError(
            f"{field} must be an integer >= {minimum}", code="invalid_config"
        )
    return value


def _string_list(
    value: Any, field: str, *, relative_patterns: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgentCtlError(f"{field} must be an array of strings", code="invalid_config")
    if relative_patterns:
        return tuple(
            require_relative_pattern(item, f"{field}[{index}]")
            for index, item in enumerate(value)
        )
    return tuple(value)


def load_project_config(project_root: Path) -> ProjectConfig:
    path = project_root.resolve() / PROJECT_CONFIG_NAME
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(mode):
            raise AgentCtlError(
                "Project config must be a regular file",
                code="invalid_config",
                details={"path": os.fspath(path)},
            )
    raw = _read_toml(path, "Project config")
    schema_version = raw.get("schema_version")
    if schema_version != SUPPORTED_CONFIG_SCHEMA:
        raise AgentCtlError(
            f"Unsupported project config schema: {schema_version}",
            code="unsupported_schema",
            details={"supported": SUPPORTED_CONFIG_SCHEMA},
        )

    validation = raw.get("validation", {})
    if not isinstance(validation, dict):
        raise AgentCtlError("validation must be a table", code="invalid_config")
    command_rows = validation.get("commands", [])
    if not isinstance(command_rows, list):
        raise AgentCtlError(
            "validation.commands must be an array of tables", code="invalid_config"
        )

    commands: list[ValidationCommand] = []
    names: set[str] = set()
    for index, row in enumerate(command_rows):
        field = f"validation.commands[{index}]"
        if not isinstance(row, dict):
            raise AgentCtlError(f"{field} must be a table", code="invalid_config")
        name = require_identifier(row.get("name"), f"{field}.name")
        if name in names:
            raise AgentCtlError(
                f"Duplicate validation command: {name}", code="invalid_config"
            )
        names.add(name)
        profiles = _string_list(row.get("profiles", []), f"{field}.profiles")
        if not profiles:
            raise AgentCtlError(
                f"{field}.profiles must not be empty", code="invalid_config"
            )
        profiles = tuple(require_identifier(item, f"{field}.profiles") for item in profiles)
        match = _string_list(
            row.get("match", ["**"]), f"{field}.match", relative_patterns=True
        )
        cwd = require_relative_pattern(row.get("cwd", "."), f"{field}.cwd")
        argv = _string_list(row.get("argv"), f"{field}.argv")
        if not argv or any(not part or "\x00" in part for part in argv):
            raise AgentCtlError(
                f"{field}.argv must be a non-empty argv array", code="invalid_config"
            )
        env = row.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in env.items()
        ):
            raise AgentCtlError(
                f"{field}.env must map names to strings", code="invalid_config"
            )
        normalized_env = {
            require_env_name(key, f"{field}.env"): value for key, value in env.items()
        }
        timeout_seconds = _positive_int(
            row.get("timeout_seconds", 900), f"{field}.timeout_seconds"
        )
        commands.append(
            ValidationCommand(
                name=name,
                profiles=profiles,
                match=match,
                cwd=cwd,
                argv=argv,
                env=normalized_env,
                timeout_seconds=timeout_seconds,
            )
        )

    instruction_files = tuple(
        require_relative_path(item, f"instruction_files[{index}]")
        for index, item in enumerate(
            _string_list(raw.get("instruction_files", []), "instruction_files")
        )
    )
    context_files = tuple(
        require_relative_path(item, f"context_files[{index}]")
        for index, item in enumerate(
            _string_list(raw.get("context_files", []), "context_files")
        )
    )
    for field_name, values in (
        ("instruction_files", instruction_files),
        ("context_files", context_files),
    ):
        for value in values:
            ensure_within(project_root, value, field_name)

    validation_isolation = raw.get("validation_isolation", "unsafe-host")
    if validation_isolation not in {"external-sandbox", "unsafe-host"}:
        raise AgentCtlError(
            "validation_isolation must be external-sandbox or unsafe-host",
            code="invalid_config",
        )

    return ProjectConfig(
        path=path,
        schema_version=schema_version,
        project_id=require_identifier(raw.get("project_id"), "project_id"),
        worker_profile=require_identifier(raw.get("worker_profile"), "worker_profile"),
        instruction_files=instruction_files,
        context_files=context_files,
        protected_paths=_string_list(
            raw.get("protected_paths", [PROJECT_CONFIG_NAME, ".git/**", ".env*"]),
            "protected_paths",
            relative_patterns=True,
        ),
        high_risk_paths=_string_list(
            raw.get("high_risk_paths", []),
            "high_risk_paths",
            relative_patterns=True,
        ),
        max_parallel=_positive_int(raw.get("max_parallel", 1), "max_parallel"),
        max_fix_rounds=_positive_int(
            raw.get("max_fix_rounds", 2), "max_fix_rounds", minimum=0
        ),
        max_files_per_unit=_positive_int(
            raw.get("max_files_per_unit", 6), "max_files_per_unit"
        ),
        max_changed_lines_per_unit=_positive_int(
            raw.get("max_changed_lines_per_unit", 300),
            "max_changed_lines_per_unit",
        ),
        max_patch_bytes=_positive_int(
            raw.get("max_patch_bytes", 5_000_000), "max_patch_bytes"
        ),
        validation_isolation=validation_isolation,
        validation_commands=tuple(commands),
    )


def load_user_config(path: Path | None = None) -> UserConfig:
    resolved = (path or default_user_config_path()).expanduser().resolve()
    raw = _read_toml(resolved, "User config")
    schema_version = raw.get("schema_version")
    if schema_version != SUPPORTED_CONFIG_SCHEMA:
        raise AgentCtlError(
            f"Unsupported user config schema: {schema_version}",
            code="unsupported_schema",
            details={"supported": SUPPORTED_CONFIG_SCHEMA},
        )
    rows = raw.get("worker_profiles", {})
    if not isinstance(rows, dict) or not rows:
        raise AgentCtlError(
            "worker_profiles must contain at least one profile", code="invalid_config"
        )

    profiles: dict[str, WorkerProfile] = {}
    for name, row in rows.items():
        profile_name = require_identifier(name, "worker_profiles key")
        if not isinstance(row, dict):
            raise AgentCtlError(
                f"worker_profiles.{profile_name} must be a table",
                code="invalid_config",
            )
        argv = _string_list(row.get("argv"), f"worker_profiles.{profile_name}.argv")
        if not argv:
            raise AgentCtlError(
                f"worker_profiles.{profile_name}.argv must not be empty",
                code="invalid_config",
            )
        probe_argv = _string_list(
            row.get("probe_argv", [argv[0], "--version"]),
            f"worker_profiles.{profile_name}.probe_argv",
        )
        for part in argv:
            unknown = set(PLACEHOLDER_RE.findall(part)) - ALLOWED_WORKER_PLACEHOLDERS
            if unknown:
                raise AgentCtlError(
                    f"Unknown worker argv placeholder: {sorted(unknown)[0]}",
                    code="invalid_config",
                )
        if any(PLACEHOLDER_RE.search(part) for part in probe_argv):
            raise AgentCtlError(
                f"worker_profiles.{profile_name}.probe_argv cannot use placeholders",
                code="invalid_config",
            )
        probe_contains = row.get("probe_contains")
        if not isinstance(probe_contains, str) or not probe_contains:
            raise AgentCtlError(
                f"worker_profiles.{profile_name}.probe_contains must be a non-empty string",
                code="invalid_config",
            )
        prompt = str(
            row.get(
                "prompt",
                "Read the request contract at {request_file}, implement only its bounded objective, and leave the workspace ready for controller verification.",
            )
        )
        unknown_prompt = set(PLACEHOLDER_RE.findall(prompt)) - (
            ALLOWED_WORKER_PLACEHOLDERS - {"prompt"}
        )
        if unknown_prompt:
            raise AgentCtlError(
                f"Unknown worker prompt placeholder: {sorted(unknown_prompt)[0]}",
                code="invalid_config",
            )
        env_allow = _string_list(
            row.get("env_allow", []), f"worker_profiles.{profile_name}.env_allow"
        )
        env_allow = tuple(
            require_env_name(item, f"worker_profiles.{profile_name}.env_allow")
            for item in env_allow
        )
        adapter = row.get("adapter", "generic-cli")
        if adapter != "generic-cli":
            raise AgentCtlError(
                f"Unsupported worker adapter: {adapter}",
                code="unsupported_adapter",
            )
        isolation = row.get("isolation")
        if isolation not in {"external-sandbox", "unsafe-host"}:
            raise AgentCtlError(
                f"worker_profiles.{profile_name}.isolation must be external-sandbox or unsafe-host",
                code="invalid_config",
            )
        if isolation == "external-sandbox" and argv[0] != probe_argv[0]:
            raise AgentCtlError(
                f"worker_profiles.{profile_name} must probe through the same sandbox wrapper used for dispatch",
                code="invalid_config",
            )
        if not any("{workspace}" in part for part in argv):
            raise AgentCtlError(
                f"worker_profiles.{profile_name}.argv must include {{workspace}}",
                code="invalid_config",
            )
        if not (
            any("{request_file}" in part or "{prompt}" in part for part in argv)
            and ("{request_file}" in prompt or any("{request_file}" in part for part in argv))
        ):
            raise AgentCtlError(
                f"worker_profiles.{profile_name} must pass the request file to the worker",
                code="invalid_config",
            )
        runtime_id = require_identifier(
            row.get("runtime_id"), f"worker_profiles.{profile_name}.runtime_id"
        )
        profiles[profile_name] = WorkerProfile(
            name=profile_name,
            adapter=adapter,
            argv=argv,
            probe_argv=probe_argv,
            probe_contains=probe_contains,
            env_allow=env_allow,
            timeout_seconds=_positive_int(
                row.get("timeout_seconds", 1500),
                f"worker_profiles.{profile_name}.timeout_seconds",
            ),
            max_output_bytes=_positive_int(
                row.get("max_output_bytes", 8_000_000),
                f"worker_profiles.{profile_name}.max_output_bytes",
            ),
            prompt=prompt,
            isolation=isolation,
            runtime_id=runtime_id,
        )
    semantic_rows = raw.get("semantic_roles", {})
    if not isinstance(semantic_rows, dict):
        raise AgentCtlError(
            "semantic_roles must be a table", code="invalid_config"
        )
    semantic_roles: dict[str, str] = {}
    for role, profile_value in semantic_rows.items():
        role_name = require_identifier(role, "semantic_roles key")
        if role_name not in {"mother", "top"}:
            raise AgentCtlError(
                f"Unsupported semantic role: {role_name}", code="invalid_config"
            )
        profile_name = require_identifier(
            profile_value, f"semantic_roles.{role_name}"
        )
        if profile_name not in profiles:
            raise AgentCtlError(
                f"semantic_roles.{role_name} references an unknown worker profile",
                code="invalid_config",
            )
        semantic_roles[role_name] = profile_name
    return UserConfig(
        path=resolved,
        schema_version=schema_version,
        worker_profiles=profiles,
        semantic_roles=semantic_roles,
    )
