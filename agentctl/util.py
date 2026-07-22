from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from functools import lru_cache
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import AgentCtlError


IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def json_hash(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise AgentCtlError(
            f"JSON file does not exist: {path}", code="file_not_found"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AgentCtlError(
            f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}",
            code="invalid_json",
        ) from exc


def atomic_write_bytes(path: Path, value: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload, mode=mode)


def atomic_write_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, value.encode("utf-8"), mode=mode)


def require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise AgentCtlError(
            f"{field} must use lowercase letters, digits, dots, underscores, or hyphens",
            code="invalid_contract",
            details={"field": field},
        )
    return value


def require_env_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ENV_NAME_RE.fullmatch(value):
        raise AgentCtlError(
            f"{field} contains an invalid environment-variable name",
            code="invalid_config",
            details={"field": field, "value": value},
        )
    return value


def require_relative_pattern(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AgentCtlError(
            f"{field} must be a non-empty repository-relative path pattern",
            code="invalid_config",
        )
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise AgentCtlError(
            f"{field} must not be absolute or traverse outside the repository",
            code="invalid_config",
            details={"value": value},
        )
    return normalized


def require_relative_path(value: Any, field: str) -> str:
    normalized = require_relative_pattern(value, field)
    if any(character in normalized for character in "*?["):
        raise AgentCtlError(
            f"{field} must be a literal repository-relative file path",
            code="invalid_config",
            details={"value": value},
        )
    if normalized in {".", ""} or normalized.endswith("/"):
        raise AgentCtlError(
            f"{field} must name a file",
            code="invalid_config",
            details={"value": value},
        )
    return normalized


def ensure_within(root: Path, relative: str, field: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise AgentCtlError(
            f"{field} resolves outside the project root",
            code="path_escape",
            details={"value": relative},
        ) from exc
    return candidate


@lru_cache(maxsize=512)
def _root_glob(pattern: str) -> re.Pattern[str]:
    """Compile a repository-root glob.

    `*` never crosses `/`; `**` does. Patterns are root anchored, so `README.md`
    cannot match `docs/README.md`. This intentionally differs from
    `PurePosixPath.match`, whose basename matching is unsafe for path policy.
    """

    expression: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    expression.append("(?:[^/]+/)*")
                    index += 1
                else:
                    expression.append(".*")
                continue
            expression.append("[^/]*")
        elif character == "?":
            expression.append("[^/]")
        elif character == "[":
            closing = pattern.find("]", index + 1)
            if closing == -1:
                expression.append(r"\[")
            else:
                content = pattern[index + 1 : closing]
                if not content or "/" in content:
                    expression.append(re.escape(pattern[index : closing + 1]))
                else:
                    if content[0] == "!":
                        content = "^" + content[1:]
                    elif content[0] == "^":
                        content = "\\" + content
                    expression.append("[" + content.replace("\\", "\\\\") + "]")
                index = closing
        else:
            expression.append(re.escape(character))
        index += 1
    expression.append("$")
    return re.compile("".join(expression))


def matches_pattern(path: str, pattern: str) -> bool:
    normalized_path = path.replace("\\", "/").lstrip("/")
    normalized_pattern = pattern.replace("\\", "/").lstrip("/")
    return _root_glob(normalized_pattern).fullmatch(normalized_path) is not None


def matches_any(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(matches_pattern(path, pattern) for pattern in patterns)
