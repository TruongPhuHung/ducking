from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import AgentCtlError
from .util import matches_any, sha256_bytes


GIT_EXECUTABLE = shutil.which("git") or "git"


def _git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


@dataclass(frozen=True)
class DiffSnapshot:
    patch: bytes
    patch_sha256: str
    changed_files: tuple[str, ...]
    changed_lines: int
    binary_files: tuple[str, ...]
    symlink_files: tuple[str, ...]


def _git(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        GIT_EXECUTABLE,
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *args,
    ]
    process = subprocess.run(
        command,
        cwd=cwd,
        env=_git_env(),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and process.returncode != 0:
        stderr = process.stderr.decode("utf-8", errors="replace").strip()
        raise AgentCtlError(
            f"Git command failed: {' '.join(command)}: {stderr}",
            code="git_failed",
            details={"argv": command, "exit_code": process.returncode},
        )
    return process


def find_git_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    process = _git(["rev-parse", "--show-toplevel"], cwd=resolved, check=False)
    if process.returncode != 0:
        raise AgentCtlError(
            f"Not inside a Git repository: {resolved}", code="not_git_repository"
        )
    return Path(process.stdout.decode("utf-8").strip()).resolve()


def head_sha(repo_root: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=repo_root).stdout.decode().strip()


def branch_name(repo_root: Path) -> str | None:
    process = _git(
        ["symbolic-ref", "--short", "-q", "HEAD"], cwd=repo_root, check=False
    )
    if process.returncode != 0:
        return None
    return process.stdout.decode().strip()


def is_clean(repo_root: Path) -> bool:
    output = _git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=repo_root
    ).stdout
    return not output.strip()


def ensure_commit(repo_root: Path, revision: str) -> str:
    process = _git(
        ["rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=repo_root,
        check=False,
    )
    if process.returncode != 0:
        raise AgentCtlError(
            f"Base revision is not a local commit: {revision}",
            code="invalid_base_sha",
        )
    return process.stdout.decode().strip()


def create_independent_clone(repo_root: Path, base_sha: str, workspace: Path) -> None:
    if workspace.exists():
        raise AgentCtlError(
            f"Workspace already exists: {workspace}", code="workspace_exists"
        )
    workspace.mkdir(parents=True)
    try:
        _git(["init", "--quiet"], cwd=workspace)
        # Fetch only the exact frozen commit. This preserves its object ID while
        # withholding unrelated refs, tags, reflogs, and repository history.
        _git(
            [
                "fetch",
                "--quiet",
                "--depth=1",
                "--no-tags",
                repo_root.resolve().as_uri(),
                base_sha,
            ],
            cwd=workspace,
        )
        _git(["checkout", "--detach", "--quiet", "FETCH_HEAD"], cwd=workspace)
        (workspace / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    except AgentCtlError:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def _intent_to_add_untracked(workspace: Path, prefix: Sequence[str] = ()) -> None:
    process = _git(
        [
            *prefix,
            "add",
            "-N",
            "-f",
            "--all",
            "--",
            ".",
            ":(exclude).agentctl-runtime/**",
            ":(exclude).git/**",
        ],
        cwd=workspace,
        check=False,
    )
    if process.returncode != 0:
        raise AgentCtlError(
            "Could not prepare untracked files for patch capture",
            code="diff_failed",
            details={"stderr": process.stderr.decode(errors="replace").strip()},
        )


def _bounded_git_output(
    args: Sequence[str], *, cwd: Path, max_bytes: int
) -> bytes:
    command = [
        GIT_EXECUTABLE,
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *args,
    ]
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    chunks: list[bytes] = []
    total = 0
    exceeded = False
    while True:
        chunk = process.stdout.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            exceeded = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            break
        chunks.append(chunk)
    _, stderr = process.communicate()
    if exceeded:
        raise AgentCtlError(
            f"Patch exceeds the {max_bytes}-byte controller limit",
            code="patch_too_large",
        )
    if process.returncode != 0:
        raise AgentCtlError(
            f"Git patch capture failed: {stderr.decode(errors='replace').strip()}",
            code="diff_failed",
        )
    return b"".join(chunks)


def _capture_diff(
    workspace: Path,
    base_sha: str,
    *,
    max_patch_bytes: int,
    prefix: Sequence[str] = (),
) -> DiffSnapshot:
    _intent_to_add_untracked(workspace, prefix)
    names_raw = _git(
        [
            *prefix,
            "diff",
            base_sha,
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
        ],
        cwd=workspace,
    ).stdout
    changed_files = tuple(
        sorted(
            item.decode("utf-8", errors="surrogateescape")
            for item in names_raw.split(b"\0")
            if item
        )
    )
    numstat_raw = _git(
        [
            *prefix,
            "diff",
            base_sha,
            "--numstat",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
        ],
        cwd=workspace,
    ).stdout
    changed_lines = 0
    binary_files: list[str] = []
    for record in numstat_raw.split(b"\0"):
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, raw_path = parts
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if added == b"-" or deleted == b"-":
            binary_files.append(path)
            continue
        changed_lines += int(added) + int(deleted)
    raw_metadata = _git(
        [
            *prefix,
            "diff",
            base_sha,
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
        ],
        cwd=workspace,
    ).stdout
    raw_records = raw_metadata.split(b"\0")
    symlink_files: list[str] = []
    index = 0
    while index < len(raw_records) and raw_records[index]:
        header = raw_records[index]
        index += 1
        if index >= len(raw_records) or not header.startswith(b":"):
            raise AgentCtlError("Could not parse raw Git diff metadata", code="diff_failed")
        fields = header[1:].split()
        if len(fields) != 5:
            raise AgentCtlError("Could not parse raw Git diff metadata", code="diff_failed")
        path = raw_records[index].decode("utf-8", errors="surrogateescape")
        index += 1
        if fields[0] == b"120000" or fields[1] == b"120000":
            symlink_files.append(path)
    patch = _bounded_git_output(
        [
            *prefix,
            "diff",
            base_sha,
            "--binary",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
        ],
        cwd=workspace,
        max_bytes=max_patch_bytes,
    )
    if b"\0" in patch:
        binary_files.extend(
            path for path in changed_files if path not in binary_files
        )
    return DiffSnapshot(
        patch=patch,
        patch_sha256=sha256_bytes(patch),
        changed_files=changed_files,
        changed_lines=changed_lines,
        binary_files=tuple(sorted(binary_files)),
        symlink_files=tuple(sorted(symlink_files)),
    )


def capture_diff(
    workspace: Path, base_sha: str, *, max_patch_bytes: int = 5_000_000
) -> DiffSnapshot:
    return _capture_diff(
        workspace, base_sha, max_patch_bytes=max_patch_bytes
    )


def capture_external_worktree(
    *,
    worker_workspace: Path,
    metadata_workspace: Path,
    base_sha: str,
    max_patch_bytes: int = 5_000_000,
) -> DiffSnapshot:
    if not (metadata_workspace / ".git").is_dir():
        raise AgentCtlError(
            "Controller metadata workspace is invalid", code="diff_failed"
        )
    for root, directories, files in os.walk(worker_workspace, followlinks=False):
        relative_root = Path(root).relative_to(worker_workspace)
        if relative_root == Path("."):
            directories[:] = [
                name
                for name in directories
                if name not in {".git", ".agentctl-runtime"}
            ]
        for name in [*directories, *files]:
            candidate = Path(root) / name
            mode = candidate.lstat().st_mode
            if not (
                os.path.isdir(candidate)
                or os.path.isfile(candidate)
                or os.path.islink(candidate)
            ):
                raise AgentCtlError(
                    f"Unsupported special file in worker workspace: {candidate.relative_to(worker_workspace)}",
                    code="unsupported_candidate_file",
                )
    prefix = (
        f"--git-dir={metadata_workspace / '.git'}",
        f"--work-tree={worker_workspace}",
    )
    return _capture_diff(
        metadata_workspace,
        base_sha,
        max_patch_bytes=max_patch_bytes,
        prefix=prefix,
    )


def scope_violations(
    changed_files: Sequence[str],
    *,
    allowed_paths: Sequence[str],
    forbidden_paths: Sequence[str],
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for path in changed_files:
        if not matches_any(path, list(allowed_paths)):
            violations.append({"path": path, "reason": "outside_allowed_paths"})
        if matches_any(path, list(forbidden_paths)):
            violations.append({"path": path, "reason": "matches_forbidden_path"})
    return violations


def apply_patch(
    project_root: Path, patch: bytes, *, base_sha: str, dry_run: bool
) -> None:
    current = head_sha(project_root)
    if current != base_sha:
        raise AgentCtlError(
            "Project HEAD no longer matches the reviewed base SHA",
            code="base_sha_drift",
            details={"expected": base_sha, "actual": current},
        )
    if not is_clean(project_root):
        raise AgentCtlError(
            "Project worktree must be clean before integration",
            code="dirty_worktree",
        )
    args = ["apply", "--check", "--whitespace=error-all", "-"]
    _git(args, cwd=project_root, input_bytes=patch)
    if not dry_run:
        _git(["apply", "--whitespace=error-all", "-"], cwd=project_root, input_bytes=patch)


def file_exists_at_commit(repo_root: Path, revision: str, relative_path: str) -> bool:
    process = _git(
        ["cat-file", "-e", f"{revision}:{relative_path}"],
        cwd=repo_root,
        check=False,
    )
    return process.returncode == 0


def read_regular_file_at_commit(
    repo_root: Path, revision: str, relative_path: str
) -> bytes | None:
    tree = _git(
        ["ls-tree", "-z", revision, "--", relative_path],
        cwd=repo_root,
        check=False,
    )
    if tree.returncode != 0 or not tree.stdout:
        return None
    record = tree.stdout.rstrip(b"\0")
    metadata, separator, raw_path = record.partition(b"\t")
    fields = metadata.split()
    decoded_path = raw_path.decode("utf-8", errors="surrogateescape")
    if (
        separator != b"\t"
        or len(fields) != 3
        or fields[1] != b"blob"
        or not fields[0].startswith(b"100")
        or decoded_path != relative_path
    ):
        return None
    return _git(["cat-file", "blob", fields[2].decode("ascii")], cwd=repo_root).stdout
