from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .config import (
    PROJECT_CONFIG_NAME,
    default_state_home,
    default_user_config_path,
    load_project_config,
    load_user_config,
)
from .errors import AgentCtlError
from .gitops import (
    branch_name,
    file_exists_at_commit,
    find_git_root,
    head_sha,
    is_clean,
)
from .store import TERMINAL_STATES, project_state_key, stores_for_project_root
from .util import (
    atomic_write_json,
    atomic_write_text,
    ensure_within,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .worker import probe_worker


def _project_slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return value[:64] or "project"


def _attachment_receipt_path(project_root: Path) -> Path:
    digest = sha256_bytes(os.fspath(project_root.resolve()).encode())
    return default_state_home() / "attachments" / f"{digest}.json"


def inspect_project(path: Path) -> dict[str, Any]:
    root = find_git_root(path)
    markers = {
        "node": any((root / item).exists() for item in ("package.json", "pnpm-lock.yaml"))
        or any(root.glob("*/**/package.json")),
        "python": any(
            (root / item).exists() for item in ("pyproject.toml", "requirements.txt")
        )
        or any(root.glob("*/**/pyproject.toml")),
        "elixir": (root / "mix.exs").exists() or any(root.glob("*/**/mix.exs")),
        "docker_compose": any(
            (root / item).exists()
            for item in ("compose.yaml", "compose.yml", "docker-compose.yml")
        ),
    }
    instructions = [
        name for name in ("AGENTS.md", "CLAUDE.md") if (root / name).is_file()
    ]
    return {
        "ok": True,
        "project_root": os.fspath(root),
        "project_id_suggestion": _project_slug(root.name),
        "head_sha": head_sha(root),
        "branch": branch_name(root),
        "clean": is_clean(root),
        "instruction_files": instructions,
        "stack_markers": markers,
        "config_exists": (root / PROJECT_CONFIG_NAME).exists(),
    }


def default_project_config(project_root: Path) -> str:
    info = inspect_project(project_root)
    instruction_files = info["instruction_files"]
    quoted_instructions = ", ".join(f'"{item}"' for item in instruction_files)
    return f'''schema_version = 1
project_id = "{info["project_id_suggestion"]}"
worker_profile = "default-implementer"
instruction_files = [{quoted_instructions}]
context_files = []
protected_paths = ["{PROJECT_CONFIG_NAME}", ".git/**", ".env*"]
high_risk_paths = [".github/**", "**/migrations/**", "**/package-lock.json", "**/mix.lock"]
max_parallel = 1
max_fix_rounds = 2
max_files_per_unit = 6
max_changed_lines_per_unit = 300
max_patch_bytes = 5000000
validation_isolation = "unsafe-host"

[[validation.commands]]
name = "git-diff-check"
profiles = ["always", "default"]
match = ["**"]
cwd = "."
argv = ["git", "diff", "--check"]
timeout_seconds = 60
'''


def attach_project(
    path: Path, *, dry_run: bool, template_path: Path | None = None
) -> dict[str, Any]:
    root = find_git_root(path)
    config_path = root / PROJECT_CONFIG_NAME
    receipt_path = _attachment_receipt_path(root)
    if config_path.exists():
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            if receipt.get("config_sha256") == sha256_file(config_path):
                return {
                    "ok": True,
                    "action": "already_attached",
                    "project_root": os.fspath(root),
                    "config": os.fspath(config_path),
                }
        raise AgentCtlError(
            f"Refusing to overwrite existing {PROJECT_CONFIG_NAME}",
            code="config_exists",
        )

    if template_path:
        try:
            content = template_path.expanduser().read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise AgentCtlError(
                f"Attachment template not found: {template_path}",
                code="file_not_found",
            ) from exc
    else:
        content = default_project_config(root)
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise AgentCtlError(
            f"Attachment template is invalid TOML: {exc}", code="invalid_config"
        ) from exc
    if parsed.get("schema_version") != 1:
        raise AgentCtlError(
            "Attachment template must use schema_version = 1",
            code="unsupported_schema",
        )
    # Validate the complete shape before touching the project. The parser only
    # needs a directory containing the candidate file; paths remain relative.
    with tempfile.TemporaryDirectory() as temporary:
        candidate_root = Path(temporary)
        (candidate_root / PROJECT_CONFIG_NAME).write_text(content, encoding="utf-8")
        load_project_config(candidate_root)
    result = {
        "ok": True,
        "action": "would_attach" if dry_run else "attached",
        "project_root": os.fspath(root),
        "config": os.fspath(config_path),
        "config_sha256": sha256_bytes(content.encode()),
        "content": content if dry_run else None,
    }
    if dry_run:
        return result
    atomic_write_text(config_path, content, mode=0o644)
    atomic_write_json(
        receipt_path,
        {
            "schema_version": 1,
            "project_root": os.fspath(root),
            "config_path": os.fspath(config_path),
            "config_sha256": sha256_file(config_path),
            "attached_at": utc_now(),
        },
    )
    return result


def detach_project(path: Path, *, dry_run: bool) -> dict[str, Any]:
    root = find_git_root(path)
    config_path = root / PROJECT_CONFIG_NAME
    receipt_path = _attachment_receipt_path(root)
    if not receipt_path.exists():
        raise AgentCtlError(
            "No agentctl attachment receipt exists for this project",
            code="not_attached",
        )
    receipt = read_json(receipt_path)
    if not config_path.exists():
        raise AgentCtlError(
            f"Attachment config is already missing: {config_path}",
            code="config_not_found",
        )
    current_hash = sha256_file(config_path)
    if current_hash != receipt.get("config_sha256"):
        raise AgentCtlError(
            "Refusing to remove a project config modified after attachment",
            code="config_modified",
        )
    action = "would_detach" if dry_run else "detached"
    if not dry_run:
        active = []
        for store in stores_for_project_root(root):
            state = store.load()
            if state.get("state") not in TERMINAL_STATES:
                active.append(state)
        if active:
            raise AgentCtlError(
                "Refusing to detach while this project has active runs",
                code="active_runs",
                details={"run_ids": [item["run_id"] for item in active]},
            )
        config_path.unlink()
        receipt_path.unlink()
    return {
        "ok": True,
        "action": action,
        "project_root": os.fspath(root),
        "config": os.fspath(config_path),
        "run_state_preserved": True,
    }


def doctor_project(path: Path, *, user_config_path: Path | None) -> dict[str, Any]:
    root = find_git_root(path)
    checks: list[dict[str, Any]] = []
    try:
        project = load_project_config(root)
        checks.append({"name": "project_config", "ok": True, "path": os.fspath(project.path)})
    except AgentCtlError as exc:
        return {"ok": False, "project_root": os.fspath(root), "checks": [exc.as_dict()["error"]]}

    current_head = head_sha(root)
    checks.append(
        {
            "name": "clean_worktree",
            "ok": is_clean(root),
            "reason": "workers use committed HEAD only",
        }
    )
    checks.append(
        {
            "name": "config_in_base",
            "ok": file_exists_at_commit(root, current_head, PROJECT_CONFIG_NAME),
            "path": os.fspath(project.path),
        }
    )
    for relative in (*project.instruction_files, *project.context_files):
        candidate = ensure_within(root, relative, "context file")
        checks.append(
            {
                "name": f"context:{relative}",
                "ok": candidate.is_file()
                and file_exists_at_commit(root, current_head, relative),
                "path": os.fspath(candidate),
            }
        )
    checks.append(
        {
            "name": "validation_isolation",
            "ok": project.validation_isolation == "external-sandbox",
            "declared": project.validation_isolation,
            "reason": "unsafe-host validators require explicit per-run authority",
        }
    )
    for command in project.validation_commands:
        cwd = ensure_within(root, command.cwd, f"validation:{command.name}")
        executable = shutil.which(command.argv[0])
        checks.append(
            {
                "name": f"validation:{command.name}",
                "ok": cwd.is_dir() and executable is not None,
                "cwd": os.fspath(cwd),
                "executable": executable,
            }
        )

    resolved_user_config = user_config_path or default_user_config_path()
    try:
        user = load_user_config(resolved_user_config)
        profile = user.worker_profiles.get(project.worker_profile)
        if not profile:
            checks.append(
                {
                    "name": "worker_profile",
                    "ok": False,
                    "reason": f"missing profile {project.worker_profile}",
                }
            )
        else:
            checks.append({"name": "worker_profile", **probe_worker(profile)})
            checks.append(
                {
                    "name": "worker_isolation",
                    "ok": profile.isolation == "external-sandbox",
                    "declared": profile.isolation,
                    "reason": "unsafe-host workers require explicit per-run authority",
                }
            )
    except AgentCtlError as exc:
        checks.append(
            {
                "name": "user_config",
                "ok": False,
                "path": os.fspath(resolved_user_config),
                "reason": exc.message,
            }
        )
    return {
        "ok": all(bool(check.get("ok")) for check in checks),
        "project_root": os.fspath(root),
        "project_id": project.project_id,
        "project_key": project_state_key(root, project.project_id),
        "config_sha256": sha256_file(project.path),
        "checks": checks,
    }


def paths() -> dict[str, Any]:
    return {
        "ok": True,
        "user_config": os.fspath(default_user_config_path()),
        "state_home": os.fspath(default_state_home()),
    }
