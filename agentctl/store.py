from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .config import default_state_home
from .errors import AgentCtlError
from .util import (
    atomic_write_json,
    atomic_write_text,
    read_json,
    require_identifier,
    sha256_bytes,
    utc_now,
)


TERMINAL_STATES = {
    "failed",
    "cancelled",
    "replan_required",
    "integrated",
}

TRANSITIONS: dict[str, set[str]] = {
    "initialized": {"dispatching", "cancelled"},
    "dispatching": {"candidate", "failed", "cancelled"},
    "candidate": {"verifying", "cancelled"},
    "verifying": {"ready_for_review", "verify_failed", "failed", "cancelled"},
    "verify_failed": {"dispatching", "cancelled"},
    "ready_for_review": {"reviewing", "cancelled"},
    "reviewing": {
        "accepted",
        "repair_queued",
        "replan_required",
        "human_required",
        "failed",
    },
    "repair_queued": {"dispatching", "cancelled"},
    "human_required": {"accepted", "cancelled"},
    "accepted": {"integrated", "cancelled"},
}


def new_run_id(task_id: str) -> str:
    validated = require_identifier(task_id, "task_id")
    timestamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz").lower()
    suffix = secrets.token_hex(2)
    available = 128 - len(timestamp) - len(suffix) - 2
    return f"{timestamp}-{validated[:available]}-{suffix}"


def project_state_key(project_root: Path, project_id: str) -> str:
    root_hash = sha256_bytes(os.fspath(project_root.resolve()).encode())[:12]
    return f"{project_id}-{root_hash}"


class RunStore:
    def __init__(self, project_key: str, run_id: str) -> None:
        if (
            not isinstance(project_key, str)
            or not project_key
            or project_key in {".", ".."}
            or "/" in project_key
            or "\\" in project_key
            or "\x00" in project_key
        ):
            raise AgentCtlError("Invalid project state namespace", code="invalid_state_namespace")
        self.project_key = project_key
        self.run_id = require_identifier(run_id, "run_id")
        self.root = default_state_home() / project_key / "runs" / self.run_id
        self.state_path = self.root / "state.json"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / ".lock"
        self.cancel_path = self.root / "cancel.request"

    @classmethod
    def create(
        cls,
        *,
        project_key: str,
        project_id: str,
        run_id: str,
        project_root: Path,
        base_sha: str,
        config_sha256: str,
        task_sha256: str,
    ) -> "RunStore":
        store = cls(project_key, run_id)
        try:
            store.root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise AgentCtlError(
                f"Run already exists: {run_id}", code="run_exists"
            ) from exc
        os.chmod(store.root, 0o700)
        state = {
            "schema_version": 1,
            "protocol_version": 1,
            "controller_version": __version__,
            "run_id": run_id,
            "project_id": project_id,
            "project_key": project_key,
            "project_root": os.fspath(project_root),
            "base_sha": base_sha,
            "config_sha256": config_sha256,
            "task_sha256": task_sha256,
            "state": "initialized",
            "attempt": 0,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "event_seq": 0,
        }
        atomic_write_json(store.state_path, state)
        store._append_event(
            seq=1,
            actor="controller",
            previous=None,
            current="initialized",
            reason="run_created",
            artifacts={"config_sha256": config_sha256, "task_sha256": task_sha256},
        )
        state["event_seq"] = 1
        atomic_write_json(store.state_path, state)
        return store

    def load(self) -> dict[str, Any]:
        state = read_json(self.state_path)
        if not isinstance(state, dict) or state.get("run_id") != self.run_id:
            raise AgentCtlError("Corrupt run state", code="corrupt_state")
        if state.get("schema_version") != 1 or state.get("protocol_version") != 1:
            raise AgentCtlError(
                "Run state uses an unsupported schema or protocol",
                code="unsupported_schema",
            )
        state_version = str(state.get("controller_version", "0"))
        if state_version.split(".", 1)[0] != __version__.split(".", 1)[0]:
            raise AgentCtlError(
                "Run was created by an incompatible controller version",
                code="controller_version_mismatch",
                details={"run": state_version, "current": __version__},
            )
        if state.get("project_key") != self.project_key:
            raise AgentCtlError("Run state namespace mismatch", code="corrupt_state")
        return state

    def _append_event(
        self,
        *,
        seq: int,
        actor: str,
        previous: str | None,
        current: str,
        reason: str,
        artifacts: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "protocol_version": 1,
            "seq": seq,
            "ts": utc_now(),
            "run_id": self.run_id,
            "actor": actor,
            "from": previous,
            "to": current,
            "reason": reason,
            "artifacts": artifacts or {},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self.events_path, 0o600)

    def transition(
        self,
        current: str,
        *,
        actor: str,
        reason: str,
        fields: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        previous = state["state"]
        if current not in TRANSITIONS.get(previous, set()):
            raise AgentCtlError(
                f"Invalid run transition: {previous} -> {current}",
                code="invalid_transition",
            )
        seq = int(state.get("event_seq", 0)) + 1
        self._append_event(
            seq=seq,
            actor=actor,
            previous=previous,
            current=current,
            reason=reason,
            artifacts=artifacts,
        )
        state.update(fields or {})
        state.update({"state": current, "updated_at": utc_now(), "event_seq": seq})
        atomic_write_json(self.state_path, state)
        return state

    def update_fields(self, **fields: Any) -> dict[str, Any]:
        state = self.load()
        state.update(fields)
        state["updated_at"] = utc_now()
        atomic_write_json(self.state_path, state)
        return state

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                owner_pid = int(self.lock_path.read_text(encoding="utf-8").strip())
                os.kill(owner_pid, 0)
            except (ValueError, ProcessLookupError):
                self.lock_path.unlink(missing_ok=True)
            except PermissionError:
                pass
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise AgentCtlError(
                f"Run is already being modified: {self.run_id}", code="run_locked"
            ) from exc
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode())
            os.close(descriptor)
            yield
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self.lock_path.unlink(missing_ok=True)

    def request_cancel(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.cancel_path, f"requested_at={utc_now()}\n", mode=0o600
        )


@contextmanager
def project_dispatch_slot(project_key: str, max_parallel: int) -> Iterator[None]:
    root = default_state_home() / project_key / "dispatch-slots"
    root.mkdir(parents=True, exist_ok=True)
    acquired: Path | None = None
    for index in range(max_parallel):
        candidate = root / f"slot-{index}.lock"
        if candidate.exists():
            try:
                owner_pid = int(candidate.read_text(encoding="utf-8").strip())
                os.kill(owner_pid, 0)
            except (ValueError, ProcessLookupError):
                candidate.unlink(missing_ok=True)
            except PermissionError:
                pass
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode())
        finally:
            os.close(descriptor)
        acquired = candidate
        break
    if acquired is None:
        raise AgentCtlError(
            f"Project already has {max_parallel} active worker dispatch(es)",
            code="parallel_limit_reached",
        )
    try:
        yield
    finally:
        acquired.unlink(missing_ok=True)


def list_runs(project_key: str) -> list[dict[str, Any]]:
    root = default_state_home() / project_key / "runs"
    if not root.exists():
        return []
    states: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/state.json"), reverse=True):
        try:
            value = read_json(path)
        except AgentCtlError:
            continue
        if isinstance(value, dict):
            states.append(value)
    return states


def stores_for_project_root(project_root: Path) -> list[RunStore]:
    resolved = project_root.resolve()
    stores: list[RunStore] = []
    for state_path in default_state_home().glob("*/runs/*/state.json"):
        try:
            value = read_json(state_path)
        except AgentCtlError:
            continue
        if not isinstance(value, dict):
            continue
        try:
            state_root = Path(value.get("project_root", "")).resolve()
        except (OSError, TypeError):
            continue
        if state_root != resolved:
            continue
        project_key = state_path.parents[2].name
        try:
            stores.append(RunStore(project_key, state_path.parent.name))
        except AgentCtlError:
            continue
    return stores


def find_store_for_project(project_root: Path, run_id: str) -> RunStore:
    matches = [
        store
        for store in stores_for_project_root(project_root)
        if store.run_id == run_id
    ]
    if len(matches) != 1:
        raise AgentCtlError(
            f"Run not found for this project: {run_id}", code="run_not_found"
        )
    return matches[0]
