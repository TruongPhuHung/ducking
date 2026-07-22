from __future__ import annotations

import json
import os
import secrets
import uuid
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .config import (
    PROJECT_CONFIG_NAME,
    default_state_home,
    default_user_config_path,
    load_project_config,
    load_user_config,
)
from .contracts import load_plan, validate_semantic_action
from .errors import AgentCtlError
from .gitops import (
    ensure_commit,
    file_exists_at_commit,
    find_git_root,
    head_sha,
    is_clean,
    read_regular_file_at_commit,
)
from .store import find_store_for_project, project_state_key
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    ensure_within,
    json_hash,
    read_json,
    require_identifier,
    sha256_bytes,
    sha256_file,
)
from .worker import probe_worker


DUCK_COUNT = 6
SNAPSHOT_MAX_BYTES = 16 * 1024
FLOCK_TERMINAL_STATES = {"reviewed", "escalated", "cancelled"}
TASK_TERMINAL_STATES = {"succeeded", "dead_lettered", "cancelled", "escalated"}
SEMANTIC_MAX_ATTEMPTS = 3
SEMANTIC_RETRY_DELAYS_SECONDS = (5, 30)
SEMANTIC_CLAIM_MAX_SECONDS = 24 * 60 * 60
AUTO_RETRY_REASONS = {
    "controller_lost",
    "lease_hard_expired",
    "progress_hard_expired",
    "spawn_failed",
    "transient_tool_error",
    "worker_crash",
}


def _parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AgentCtlError("Corrupt flock timestamp", code="corrupt_state") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _time_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bounded_text(value: Any, limit: int = 512) -> str:
    text = str(value or "").strip().replace("\x00", "")
    return text[:limit]


def _new_flock_id(plan_id: str, now: datetime) -> str:
    timestamp = now.strftime("%Y%m%dt%H%M%Sz").lower()
    suffix = secrets.token_hex(2)
    available = 128 - len(timestamp) - len(suffix) - 2
    return f"{timestamp}-{plan_id[:available]}-{suffix}"


class FlockStore:
    def __init__(self, project_key: str, flock_id: str) -> None:
        if (
            not isinstance(project_key, str)
            or not project_key
            or project_key in {".", ".."}
            or "/" in project_key
            or "\\" in project_key
            or "\x00" in project_key
        ):
            raise AgentCtlError(
                "Invalid project state namespace", code="invalid_state_namespace"
            )
        self.project_key = project_key
        self.flock_id = require_identifier(flock_id, "flock_id")
        self.root = default_state_home() / project_key / "flocks" / self.flock_id
        self.state_path = self.root / "state.json"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / ".lock"

    def load(self) -> dict[str, Any]:
        state = read_json(self.state_path)
        if not isinstance(state, dict) or state.get("flock_id") != self.flock_id:
            raise AgentCtlError("Corrupt flock state", code="corrupt_state")
        if state.get("schema_version") != 1 or state.get("protocol_version") != 1:
            raise AgentCtlError(
                "Flock state uses an unsupported schema or protocol",
                code="unsupported_schema",
            )
        state_version = str(state.get("controller_version", "0"))
        if state_version.split(".", 1)[0] != __version__.split(".", 1)[0]:
            raise AgentCtlError(
                "Flock was created by an incompatible controller version",
                code="controller_version_mismatch",
                details={"flock": state_version, "current": __version__},
            )
        if state.get("project_key") != self.project_key:
            raise AgentCtlError("Flock state namespace mismatch", code="corrupt_state")
        return state

    def save(
        self,
        state: dict[str, Any],
        *,
        actor: str,
        reason: str,
        artifacts: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = now or datetime.now(UTC)
        revision = int(state.get("revision", 0)) + 1
        event = {
            "protocol_version": 1,
            "seq": revision,
            "ts": _time_text(timestamp),
            "flock_id": self.flock_id,
            "actor": actor,
            "reason": reason,
            "state": state["state"],
            "artifacts": artifacts or {},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self.events_path, 0o600)
        state["revision"] = revision
        state["updated_at"] = _time_text(timestamp)
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
                self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError as exc:
            raise AgentCtlError(
                f"Flock is already being modified: {self.flock_id}",
                code="flock_locked",
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


def _unit_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit["task_id"]: unit for unit in plan["units"]}


def _load_frozen_plan(store: FlockStore, state: dict[str, Any]) -> dict[str, Any]:
    plan = load_plan(store.root / "plan.json")
    if json_hash(plan) != state.get("plan_sha256"):
        raise AgentCtlError(
            "Frozen flock plan failed its integrity check", code="artifact_tampered"
        )
    return plan


def _profile_value(profile: Any) -> dict[str, Any]:
    return asdict(profile)


def _task_summary(state: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for task in state["tasks"].values():
        name = task["state"]
        result[name] = result.get(name, 0) + 1
    return dict(sorted(result.items()))


def _active_leases(state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    leases: list[dict[str, Any]] = []
    for task_id, task in state["tasks"].items():
        lease = task.get("current_lease")
        if not lease:
            continue
        leases.append(
            {
                "task_id": task_id,
                "attempt": lease["attempt"],
                "lease_id": lease["lease_id"],
                "slot_id": lease["slot_id"],
                "duck_incarnation": lease["duck_incarnation"],
                "state": task["state"],
                "heartbeat_age_ms": max(
                    0,
                    int((now - _parse_time(lease["last_heartbeat_at"])).total_seconds() * 1000),
                ),
                "progress_age_ms": max(
                    0,
                    int((now - _parse_time(lease["last_progress_at"])).total_seconds() * 1000),
                ),
                "progress_seq": lease["progress_seq"],
                "phase": lease.get("phase", "starting"),
            }
        )
    return leases[:DUCK_COUNT]


def _subject_revision(state: dict[str, Any], kind: str, subject_id: str | None) -> int:
    if kind == "task" and subject_id:
        return int(state["tasks"][subject_id]["revision"])
    return int(state.get("aggregate_revision", 0))


def _command_id(snapshot_id: str, name: str) -> str:
    return sha256_bytes(f"{snapshot_id}:{name}".encode())[:24]


def _enqueue_snapshot(
    store: FlockStore,
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    role: str,
    trigger: str,
    commands: list[str],
    subject_kind: str,
    subject_id: str | None,
    now: datetime,
    alert_ids: list[str] | None = None,
) -> dict[str, Any]:
    subject_revision = _subject_revision(state, subject_kind, subject_id)
    stable_name = (
        f"{state['flock_id']}:{role}:{trigger}:{subject_kind}:"
        f"{subject_id or 'flock'}:{subject_revision}:"
        f"{state['coordinator_epoch']}:{state.get('aggregate_revision', 0)}:"
        f"{state['state']}"
    )
    snapshot_id = uuid.uuid5(uuid.NAMESPACE_URL, stable_name).hex
    for item in state["outbox"]:
        if item["snapshot_id"] == snapshot_id:
            return item

    task_value: dict[str, Any] | None = None
    attempt_value: dict[str, Any] | None = None
    progress_value: dict[str, Any] | None = None
    retry_value: dict[str, Any] | None = None
    budgets_value: dict[str, Any] | None = None
    slot_value: dict[str, Any] | None = None
    artifact_refs: list[dict[str, str]] = []
    if subject_kind == "task" and subject_id:
        unit = _unit_map(plan)[subject_id]
        task = state["tasks"][subject_id]
        lease = task.get("current_lease")
        task_value = {
            "id": subject_id,
            "goal": _bounded_text(unit["objective"]),
            "contract_sha256": task["task_sha256"],
            "state": task["state"],
        }
        retry_value = {
            "attempt": task["attempts"],
            "max": task["max_attempts"],
            "next_after": task.get("available_at"),
        }
        budgets_value = {
            "wall_seconds": {
                "used": task["wall_used_seconds"],
                "limit": task["wall_budget_seconds"],
                "remaining": max(
                    0, task["wall_budget_seconds"] - task["wall_used_seconds"]
                ),
            },
            "tokens": None,
            "cost": None,
        }
        if lease:
            attempt_value = {
                "number": lease["attempt"],
                "lease_id": lease["lease_id"],
                "state": task["state"],
                "branch_ref": lease["branch_ref"],
            }
            progress_value = {
                "seq": lease["progress_seq"],
                "phase": lease.get("phase", "starting"),
                "heartbeat_age_ms": max(
                    0,
                    int((now - _parse_time(lease["last_heartbeat_at"])).total_seconds() * 1000),
                ),
                "progress_age_ms": max(
                    0,
                    int((now - _parse_time(lease["last_progress_at"])).total_seconds() * 1000),
                ),
            }
            slot = state["ducks"][lease["slot_id"]]
            slot_value = {
                "id": slot["slot_id"],
                "incarnation": slot["incarnation"],
            }
        elif task["attempt_history"]:
            latest_eof = task["attempt_history"][-1]
            attempt_value = {
                "number": latest_eof["attempt"],
                "lease_id": latest_eof["lease_id"],
                "state": task["state"],
                "branch_ref": latest_eof["branch_ref"],
                "eof": {
                    "outcome": latest_eof["outcome"],
                    "reason": latest_eof["reason"],
                    "worker_eof_seen": latest_eof["worker_eof_seen"],
                },
            }
            slot_value = {
                "id": latest_eof["slot_id"],
                "incarnation": latest_eof["duck_incarnation"],
            }
            artifact_refs = [
                {"kind": name, "sha256": digest}
                for name, digest in latest_eof["artifacts"].items()
            ]
    elif subject_kind == "flock":
        manifest_path = store.root / "aggregate-manifest.json"
        if manifest_path.exists():
            artifact_refs = [
                {
                    "kind": "aggregate_manifest",
                    "sha256": sha256_file(manifest_path),
                    "ref": "aggregate-manifest.json",
                }
            ]

    selected_alerts = [
        {
            "id": item["id"],
            "severity": item["severity"],
            "code": item["code"],
            "facts": item["facts"],
        }
        for item in state["alerts"]
        if not alert_ids or item["id"] in alert_ids
    ][-6:]
    allowed_commands = [
        {
            "command_id": _command_id(snapshot_id, name),
            "name": name,
            "if_revision": subject_revision,
            "if_flock_revision": int(state.get("aggregate_revision", 0)),
            "if_coordinator_epoch": int(state["coordinator_epoch"]),
            "if_flock_state": state["state"],
        }
        for name in commands
    ]
    snapshot = {
        "schema": "ducking.semantic-snapshot/v1",
        "contract_version": 1,
        "snapshot_id": snapshot_id,
        "role": role,
        "model_profile": state["semantic_profiles"].get(role),
        "trigger": trigger,
        "generated_at": _time_text(now),
        "flock": {
            "id": state["flock_id"],
            "revision": int(state.get("aggregate_revision", 0)),
            "state": state["state"],
            "coordinator_epoch": state["coordinator_epoch"],
            "task_summary": _task_summary(state),
            "active_leases": _active_leases(state, now),
        },
        "task": task_value,
        "slot": slot_value,
        "attempt": attempt_value,
        "progress": progress_value,
        "retry": retry_value,
        "budgets": budgets_value,
        "alerts": selected_alerts,
        "artifacts": artifact_refs,
        "allowed_commands": allowed_commands,
        "context_policy": {
            "worker_transcript_included": False,
            "worker_prose_included": False,
            "prior_conversation_included": False,
            "raw_logs_included": False,
        },
    }
    payload = canonical_json(snapshot)
    if len(payload) > SNAPSHOT_MAX_BYTES:
        raise AgentCtlError(
            "Semantic snapshot exceeds the 16 KiB context budget",
            code="snapshot_too_large",
        )
    digest = sha256_bytes(payload)
    path = store.root / "outbox" / f"{snapshot_id}.json"
    atomic_write_bytes(path, payload, mode=0o600)
    item = {
        "snapshot_id": snapshot_id,
        "snapshot_sha256": digest,
        "role": role,
        "trigger": trigger,
        "state": "pending",
        "path": os.fspath(path.relative_to(store.root)),
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "subject_revision": subject_revision,
        "flock_revision": int(state.get("aggregate_revision", 0)),
        "coordinator_epoch": int(state["coordinator_epoch"]),
        "flock_state": state["state"],
        "commands": allowed_commands,
        "delivery_attempts": 0,
        "created_at": _time_text(now),
    }
    state["outbox"].append(item)
    return item


def _add_alert(
    state: dict[str, Any],
    *,
    code: str,
    severity: str,
    facts: dict[str, Any],
    task_id: str | None,
    episode: int,
    now: datetime,
) -> dict[str, Any]:
    dedupe_key = f"{state['flock_id']}:{task_id or 'flock'}:{code}:{episode}"
    for alert in state["alerts"]:
        if alert["dedupe_key"] == dedupe_key:
            return alert
    alert_id = sha256_bytes(dedupe_key.encode())[:24]
    bounded_facts = {
        key: (_bounded_text(value, 256) if isinstance(value, str) else value)
        for key, value in facts.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    alert = {
        "schema": "ducking.alert/v1",
        "id": alert_id,
        "dedupe_key": dedupe_key,
        "scope": {"flock_id": state["flock_id"], "task_id": task_id},
        "source": "runtime",
        "severity": severity,
        "code": code,
        "facts": bounded_facts,
        "episode": episode,
        "occurred_at": _time_text(now),
    }
    state["alerts"].append(alert)
    state["alerts"] = state["alerts"][-256:]
    return alert


def _retry_policy(plan: dict[str, Any]) -> tuple[int, list[int]]:
    retry = plan.get("retry", {})
    return int(retry.get("max_attempts", 3)), [
        int(value) for value in retry.get("delays_seconds", [5, 30])
    ]


def _lease_policy(plan: dict[str, Any]) -> dict[str, int]:
    lease = plan.get("lease", {})
    return {
        "liveness_soft_seconds": int(lease.get("liveness_soft_seconds", 45)),
        "liveness_hard_seconds": int(lease.get("liveness_hard_seconds", 90)),
        "progress_soft_seconds": int(lease.get("progress_soft_seconds", 120)),
        "progress_hard_seconds": int(lease.get("progress_hard_seconds", 600)),
    }


def init_flock(
    project_path: Path,
    plan_path: Path,
    *,
    user_config_path: Path | None,
    allow_unsafe_worker: bool = False,
    allow_unsafe_supervisor: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    project_root = find_git_root(project_path)
    project = load_project_config(project_root)
    if not is_clean(project_root):
        raise AgentCtlError(
            "Project worktree must be clean before a flock is created",
            code="dirty_worktree",
        )
    plan = dict(load_plan(plan_path))
    full_base_sha = ensure_commit(project_root, plan["base_sha"])
    if full_base_sha != head_sha(project_root):
        raise AgentCtlError(
            "Plan base_sha must match the current project HEAD",
            code="stale_plan",
        )
    committed_config = read_regular_file_at_commit(
        project_root, full_base_sha, PROJECT_CONFIG_NAME
    )
    if committed_config is None:
        raise AgentCtlError(
            "Project config must be committed in the frozen base",
            code="project_config_not_in_base",
        )
    if committed_config != project.path.read_bytes():
        raise AgentCtlError(
            "Loaded project config does not match the frozen base",
            code="config_drift",
        )
    plan["base_sha"] = full_base_sha
    known_profiles = {
        profile
        for command in project.validation_commands
        for profile in command.profiles
        if profile != "always"
    }
    for unit in plan["units"]:
        unit["base_sha"] = full_base_sha
        unknown_profiles = sorted(
            set(unit["validation_profiles"]) - known_profiles
        )
        if unknown_profiles:
            raise AgentCtlError(
                f"Task requests unknown validation profile: {unknown_profiles[0]}",
                code="unknown_validation_profile",
            )
        required_context = list(
            dict.fromkeys(
                [
                    *project.instruction_files,
                    *project.context_files,
                    *unit.get("context_files", []),
                ]
            )
        )
        missing_context = [
            path
            for path in required_context
            if not file_exists_at_commit(project_root, full_base_sha, path)
        ]
        if missing_context:
            raise AgentCtlError(
                "Required context must be committed in the frozen base",
                code="context_not_in_base",
                details={"task_id": unit["task_id"], "paths": missing_context},
            )
        unit["context_files"] = required_context

    user = load_user_config(user_config_path or default_user_config_path())
    implementation_profile = user.worker_profiles.get(project.worker_profile)
    if implementation_profile is None:
        raise AgentCtlError(
            f"Worker profile does not exist: {project.worker_profile}",
            code="worker_profile_not_found",
        )
    if implementation_profile.isolation == "unsafe-host" and not allow_unsafe_worker:
        raise AgentCtlError(
            "The child worker profile has unrestricted host access; flock init "
            "requires explicit --allow-unsafe-worker authority",
            code="unsafe_worker_requires_opt_in",
        )
    implementation_probe = probe_worker(implementation_profile)
    if not implementation_probe.get("ok"):
        raise AgentCtlError(
            "Child worker runtime probe failed",
            code="worker_unavailable",
            details=implementation_probe,
        )
    missing_roles = sorted({"mother", "top"} - set(user.semantic_roles))
    if missing_roles:
        raise AgentCtlError(
            f"User config is missing semantic role: {missing_roles[0]}",
            code="semantic_role_not_found",
        )
    semantic_profiles = {
        role: user.semantic_roles[role] for role in ("mother", "top")
    }
    semantic_probes: dict[str, dict[str, Any]] = {}
    for role, profile_name in semantic_profiles.items():
        profile = user.worker_profiles[profile_name]
        if profile.timeout_seconds + 30 > SEMANTIC_CLAIM_MAX_SECONDS:
            raise AgentCtlError(
                f"Semantic supervisor timeout is too large for role {role}",
                code="invalid_config",
            )
        if profile.isolation == "unsafe-host" and not allow_unsafe_supervisor:
            raise AgentCtlError(
                "Semantic supervisor profiles have unrestricted host access; "
                "flock init requires explicit --allow-unsafe-supervisor authority",
                code="unsafe_supervisor_requires_opt_in",
            )
        probe = probe_worker(profile)
        if not probe.get("ok"):
            raise AgentCtlError(
                f"Semantic supervisor runtime probe failed for role {role}",
                code="semantic_supervisor_unavailable",
                details=probe,
            )
        semantic_probes[role] = probe

    flock_id = _new_flock_id(plan["plan_id"], now)
    project_key = project_state_key(project_root, project.project_id)
    store = FlockStore(project_key, flock_id)
    try:
        store.root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise AgentCtlError(f"Flock already exists: {flock_id}", code="flock_exists") from exc
    os.chmod(store.root, 0o700)

    plan_hash = json_hash(plan)
    atomic_write_json(store.root / "plan.json", plan)
    profile_hashes: dict[str, str] = {}
    for role, profile_name in semantic_profiles.items():
        profile_value = _profile_value(user.worker_profiles[profile_name])
        path = store.root / f"semantic-profile-{role}.json"
        atomic_write_json(path, profile_value)
        profile_hashes[role] = json_hash(profile_value)

    max_attempts, retry_delays = _retry_policy(plan)
    tasks: dict[str, dict[str, Any]] = {}
    for unit in plan["units"]:
        tasks[unit["task_id"]] = {
            "task_id": unit["task_id"],
            "task_sha256": json_hash(unit),
            "state": "queued",
            "revision": 0,
            "depends_on": list(unit.get("depends_on", [])),
            "attempts": 0,
            "max_attempts": max_attempts,
            "wall_budget_seconds": int(unit["budget"]["wall_seconds"]),
            "wall_used_seconds": 0,
            "available_at": _time_text(now),
            "retry_authorized": True,
            "reported_to_top": False,
            "current_lease": None,
            "attempt_history": [],
            "task_eof": None,
        }
    state = {
        "schema_version": 1,
        "protocol_version": 1,
        "controller_version": __version__,
        "flock_id": flock_id,
        "plan_id": plan["plan_id"],
        "project_id": project.project_id,
        "project_key": project_key,
        "project_root": os.fspath(project_root),
        "base_sha": full_base_sha,
        "config_sha256": sha256_file(project.path),
        "plan_sha256": plan_hash,
        "semantic_profiles": semantic_profiles,
        "semantic_profile_sha256": profile_hashes,
        "semantic_probes": semantic_probes,
        "implementation_profile": implementation_profile.name,
        "implementation_profile_sha256": json_hash(
            _profile_value(implementation_profile)
        ),
        "implementation_probe": implementation_probe,
        "state": "running",
        "revision": 0,
        "aggregate_revision": 0,
        "coordinator_epoch": 1,
        "duck_count": DUCK_COUNT,
        "ducks": [
            {
                "slot_id": index,
                "incarnation": 1,
                "state": "idle",
                "lease_id": None,
                "branch_ref": None,
            }
            for index in range(DUCK_COUNT)
        ],
        "tasks": tasks,
        "retry_delays_seconds": retry_delays,
        "lease_policy": _lease_policy(plan),
        "restart_policy": {"max_restarts": 6, "window_seconds": 60},
        "restart_events": [],
        "root_restart_policy": {"max_restarts": 3, "window_seconds": 60},
        "root_restart_events": [],
        "recovery_operations": {},
        "outbox": [],
        "alerts": [],
        "created_at": _time_text(now),
        "updated_at": _time_text(now),
    }
    store.save(
        state,
        actor="runtime_supervisor",
        reason="flock_created",
        artifacts={"plan_sha256": plan_hash, "duck_count": DUCK_COUNT},
        now=now,
    )
    return {
        "ok": True,
        "flock_id": flock_id,
        "state": state["state"],
        "duck_count": DUCK_COUNT,
        "task_count": len(tasks),
        "flock_root": os.fspath(store.root),
        "plan_sha256": plan_hash,
    }


def _stores_for_project(project_root: Path) -> list[FlockStore]:
    resolved = project_root.resolve()
    stores: list[FlockStore] = []
    for state_path in default_state_home().glob("*/flocks/*/state.json"):
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
        try:
            stores.append(FlockStore(state_path.parents[2].name, state_path.parent.name))
        except AgentCtlError:
            continue
    return stores


def _store_for(project_path: Path, flock_id: str) -> tuple[Path, FlockStore, dict[str, Any]]:
    project_root = find_git_root(project_path)
    matches = [
        store for store in _stores_for_project(project_root) if store.flock_id == flock_id
    ]
    if len(matches) != 1:
        raise AgentCtlError(
            f"Flock not found for this project: {flock_id}", code="flock_not_found"
        )
    store = matches[0]
    state = store.load()
    config = load_project_config(project_root)
    if sha256_file(config.path) != state.get("config_sha256"):
        raise AgentCtlError(
            "Project config changed after this flock started", code="config_drift"
        )
    return project_root, store, state


def _task_ready(state: dict[str, Any], task: dict[str, Any], now: datetime) -> bool:
    if task["state"] not in {"queued", "retry_wait"}:
        return False
    if not task.get("retry_authorized", True):
        return False
    if _parse_time(task["available_at"]) > now:
        return False
    return all(state["tasks"][item]["state"] == "succeeded" for item in task["depends_on"])


def _assignment_value(
    state: dict[str, Any],
    units: dict[str, dict[str, Any]],
    task: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    lease = task["current_lease"]
    duck = state["ducks"][lease["slot_id"]]
    unit = deepcopy(units[task["task_id"]])
    unit["budget"]["wall_seconds"] = max(
        1, task["wall_budget_seconds"] - task["wall_used_seconds"]
    )
    unit["flock_attempt"] = {
        "flock_id": state["flock_id"],
        "lease_id": lease["lease_id"],
        "coordinator_epoch": state["coordinator_epoch"],
        "slot_id": duck["slot_id"],
        "duck_incarnation": duck["incarnation"],
        "contract_sha256": task["task_sha256"],
        "branch_ref": lease["branch_ref"],
    }
    return {
        "slot_id": duck["slot_id"],
        "duck_incarnation": duck["incarnation"],
        "lease": deepcopy(lease),
        "task": unit,
        "replayed": replayed,
    }


def flock_tick(
    project_path: Path, flock_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    _, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        if state["state"] != "running":
            return {
                "ok": state["state"] in FLOCK_TERMINAL_STATES,
                "flock_id": flock_id,
                "state": state["state"],
                "assignments": [],
            }
        plan = _load_frozen_plan(store, state)
        units = _unit_map(plan)
        assignments = [
            _assignment_value(state, units, task, replayed=True)
            for task in state["tasks"].values()
            if task.get("current_lease")
            and not task["current_lease"].get("assignment_acknowledged", False)
        ]
        idle = [duck for duck in state["ducks"] if duck["state"] == "idle"]
        ready = [
            task
            for task in state["tasks"].values()
            if _task_ready(state, task, current)
        ]
        new_assignments: list[dict[str, Any]] = []
        for duck, task in zip(idle, ready):
            task["attempts"] += 1
            task["revision"] += 1
            lease_id = secrets.token_hex(16)
            attempt = task["attempts"]
            branch_ref = (
                f"ducking/{flock_id}/{task['task_id']}/"
                f"a{attempt}-i{duck['incarnation']}"
            )
            lease = {
                "lease_id": lease_id,
                "coordinator_epoch": state["coordinator_epoch"],
                "task_id": task["task_id"],
                "attempt": attempt,
                "slot_id": duck["slot_id"],
                "duck_incarnation": duck["incarnation"],
                "contract_sha256": task["task_sha256"],
                "branch_ref": branch_ref,
                "issued_at": _time_text(current),
                "last_heartbeat_at": _time_text(current),
                "last_progress_at": _time_text(current),
                "progress_seq": 0,
                "phase": "starting",
                "summary": "",
                "stall_episode": 0,
                "assignment_acknowledged": False,
            }
            task["state"] = "leased"
            task["current_lease"] = lease
            task["retry_authorized"] = False
            duck.update(
                {
                    "state": "busy",
                    "lease_id": lease_id,
                    "branch_ref": branch_ref,
                }
            )
            new_assignments.append(
                _assignment_value(state, units, task, replayed=False)
            )
        assignments.extend(new_assignments)
        if new_assignments:
            store.save(
                state,
                actor="mother_runtime",
                reason="tasks_leased",
                artifacts={
                    "assignments": [
                        {
                            "task_id": item["task"]["task_id"],
                            "slot_id": item["slot_id"],
                            "attempt": item["lease"]["attempt"],
                        }
                        for item in new_assignments
                    ]
                },
                now=current,
            )
        return {
            "ok": True,
            "flock_id": flock_id,
            "state": state["state"],
            "assignments": assignments,
            "idle_ducks": sum(1 for duck in state["ducks"] if duck["state"] == "idle"),
        }


def _active_task(
    state: dict[str, Any], *, slot_id: int, lease_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if slot_id < 0 or slot_id >= DUCK_COUNT:
        raise AgentCtlError("slot_id must be between 0 and 5", code="invalid_contract")
    duck = state["ducks"][slot_id]
    if duck.get("lease_id") != lease_id:
        raise AgentCtlError("Lease is stale or belongs to another duck", code="stale_lease")
    for task in state["tasks"].values():
        lease = task.get("current_lease")
        if lease and lease.get("lease_id") == lease_id:
            if (
                lease.get("coordinator_epoch") != state["coordinator_epoch"]
                or lease.get("duck_incarnation") != duck["incarnation"]
            ):
                raise AgentCtlError("Lease epoch or incarnation is stale", code="stale_lease")
            return duck, task, lease
    raise AgentCtlError("Active lease was not found", code="stale_lease")


def flock_heartbeat(
    project_path: Path,
    flock_id: str,
    *,
    slot_id: int,
    lease_id: str,
    progress_seq: int,
    phase: str,
    summary: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    require_identifier(phase, "phase")
    if progress_seq < 0:
        raise AgentCtlError("progress_seq must be non-negative", code="invalid_contract")
    if len(summary) > 512:
        raise AgentCtlError("summary must be <= 512 characters", code="invalid_contract")
    _, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        duck, task, lease = _active_task(state, slot_id=slot_id, lease_id=lease_id)
        previous_seq = int(lease["progress_seq"])
        if progress_seq < previous_seq:
            raise AgentCtlError("Progress sequence moved backwards", code="stale_progress")
        if progress_seq == previous_seq and (
            phase != lease.get("phase") or summary != lease.get("summary")
        ):
            raise AgentCtlError(
                "The same progress sequence cannot carry different content",
                code="progress_seq_collision",
            )
        lease["last_heartbeat_at"] = _time_text(current)
        lease["assignment_acknowledged"] = True
        progressed = progress_seq > previous_seq
        if progressed:
            lease.update(
                {
                    "progress_seq": progress_seq,
                    "phase": phase,
                    "summary": summary,
                    "last_progress_at": _time_text(current),
                }
            )
            task["revision"] += 1
        policy = state["lease_policy"]
        progress_age = (current - _parse_time(lease["last_progress_at"])).total_seconds()
        if task["state"] == "soft_stalled" and (
            progressed or progress_age < policy["progress_soft_seconds"]
        ):
            task["state"] = "leased"
            task["revision"] += 1
        store.save(
            state,
            actor=f"duck_{duck['slot_id']}",
            reason="progress" if progressed else "heartbeat",
            artifacts={"task_id": task["task_id"], "progress_seq": progress_seq},
            now=current,
        )
        return {
            "ok": True,
            "flock_id": flock_id,
            "task_id": task["task_id"],
            "state": task["state"],
            "progressed": progressed,
            "progress_seq": progress_seq,
        }


def _release_duck(duck: dict[str, Any], *, replace: bool) -> None:
    if replace:
        duck["incarnation"] += 1
    duck.update({"state": "idle", "lease_id": None, "branch_ref": None})


def _record_restart(
    store: FlockStore,
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    task_id: str,
    reason: str,
    now: datetime,
) -> None:
    window = int(state["restart_policy"]["window_seconds"])
    cutoff = now - timedelta(seconds=window)
    events = [
        value
        for value in state["restart_events"]
        if _parse_time(value["at"]) >= cutoff
    ]
    events.append({"at": _time_text(now), "task_id": task_id, "reason": reason})
    state["restart_events"] = events
    if (
        len(events) > int(state["restart_policy"]["max_restarts"])
        and state["state"] != "escalated"
    ):
        _escalate_flock(state, reason="pool_restart_intensity", now=now)
        alert = _add_alert(
            state,
            code="supervision.restart_intensity",
            severity="critical",
            facts={"count": len(events), "window_seconds": window},
            task_id=None,
            episode=state["aggregate_revision"],
            now=now,
        )
        _enqueue_snapshot(
            store,
            state,
            plan,
            role="top",
            trigger="pool_circuit_open",
            commands=["escalate_sol"],
            subject_kind="flock",
            subject_id=None,
            now=now,
            alert_ids=[alert["id"]],
        )


def _attempt_eof(
    task: dict[str, Any],
    lease: dict[str, Any],
    *,
    outcome: str,
    reason: str,
    worker_eof_seen: bool,
    artifacts: dict[str, str],
    now: datetime,
) -> dict[str, Any]:
    duration_ms = max(
        0, int((now - _parse_time(lease["issued_at"])).total_seconds() * 1000)
    )
    task["wall_used_seconds"] = min(
        task["wall_budget_seconds"],
        task["wall_used_seconds"] + ((duration_ms + 999) // 1000),
    )
    eof = {
        "schema": "ducking.attempt-eof/v1",
        "attempt": lease["attempt"],
        "lease_id": lease["lease_id"],
        "slot_id": lease["slot_id"],
        "duck_incarnation": lease["duck_incarnation"],
        "branch_ref": lease["branch_ref"],
        "reported_outcome": outcome,
        "reported_reason": reason,
        "outcome": outcome,
        "reason": reason,
        "worker_eof_seen": worker_eof_seen,
        "duration_ms": duration_ms,
        "artifacts": dict(sorted(artifacts.items())),
        "finished_at": _time_text(now),
    }
    task["attempt_history"].append(eof)
    return eof


def _set_task_eof(
    task: dict[str, Any], *, outcome: str, reason: str, now: datetime
) -> None:
    if task.get("task_eof") is not None:
        raise AgentCtlError("Task already has a terminal EOF", code="duplicate_eof")
    task["task_eof"] = {
        "schema": "ducking.task-eof/v1",
        "outcome": outcome,
        "reason": reason,
        "finished_at": _time_text(now),
    }


def _supersede_pending_snapshots(
    state: dict[str, Any], *, reason: str, now: datetime
) -> None:
    for item in state["outbox"]:
        if item.get("state") != "pending":
            continue
        item["state"] = "superseded"
        item["superseded_at"] = _time_text(now)
        item["superseded_reason"] = reason
        item.pop("claim_id", None)
        item.pop("claimed_at", None)
        item.pop("claimed_until", None)


def _escalate_flock(
    state: dict[str, Any], *, reason: str, now: datetime
) -> int:
    """Fence every live duck and give every unfinished task one terminal EOF."""

    terminated = 0
    for task in state["tasks"].values():
        if task["state"] in TASK_TERMINAL_STATES:
            continue
        lease = task.get("current_lease")
        if lease:
            _attempt_eof(
                task,
                lease,
                outcome="escalated",
                reason=reason,
                worker_eof_seen=False,
                artifacts={},
                now=now,
            )
            _release_duck(state["ducks"][lease["slot_id"]], replace=True)
            task["current_lease"] = None
        task["state"] = "escalated"
        task["revision"] += 1
        _set_task_eof(task, outcome="escalated", reason=reason, now=now)
        terminated += 1
    if state["state"] != "escalated":
        state["state"] = "escalated"
        state["aggregate_revision"] += 1
    _supersede_pending_snapshots(state, reason=reason, now=now)
    return terminated


def _finish_in_state(
    store: FlockStore,
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    slot_id: int,
    lease_id: str,
    outcome: str,
    reason: str,
    artifacts: dict[str, str],
    worker_eof_seen: bool,
    verified_child: dict[str, Any] | None = None,
    global_cancel: bool = False,
    now: datetime,
) -> dict[str, Any]:
    duck, task, lease = _active_task(state, slot_id=slot_id, lease_id=lease_id)
    eof = _attempt_eof(
        task,
        lease,
        outcome=outcome,
        reason=reason,
        worker_eof_seen=worker_eof_seen,
        artifacts=artifacts,
        now=now,
    )
    task["current_lease"] = None
    task["revision"] += 1

    budget_exhausted = task["wall_used_seconds"] >= task["wall_budget_seconds"]
    if outcome == "succeeded" and budget_exhausted:
        outcome = "fatal_failure"
        reason = "task_wall_budget_exhausted"
        eof["outcome"] = outcome
        eof["reason"] = reason
        verified_child = None

    if outcome == "succeeded":
        if verified_child is None:
            raise AgentCtlError(
                "Success requires a verified child run",
                code="unverified_child_result",
            )
        task["state"] = "succeeded"
        task["reported_to_top"] = False
        task["verified_child"] = verified_child
        _set_task_eof(task, outcome="succeeded", reason=reason, now=now)
        _release_duck(duck, replace=False)
        _enqueue_snapshot(
            store,
            state,
            plan,
            role="mother",
            trigger="task_terminal",
            commands=["notify_top"],
            subject_kind="task",
            subject_id=task["task_id"],
            now=now,
        )
    elif outcome == "cancelled":
        task["state"] = "cancelled"
        _set_task_eof(task, outcome="cancelled", reason=reason, now=now)
        _release_duck(duck, replace=True)
        if not global_cancel:
            _escalate_flock(state, reason=reason, now=now)
            alert = _add_alert(
                state,
                code="task.cancelled",
                severity="error",
                facts={"attempts": task["attempts"], "reason": reason},
                task_id=task["task_id"],
                episode=task["attempts"],
                now=now,
            )
            _enqueue_snapshot(
                store,
                state,
                plan,
                role="top",
                trigger="task_cancelled",
                commands=["escalate_sol"],
                subject_kind="task",
                subject_id=task["task_id"],
                now=now,
                alert_ids=[alert["id"]],
            )
    else:
        replace = True
        _release_duck(duck, replace=replace)
        if reason in AUTO_RETRY_REASONS:
            _record_restart(
                store, state, plan, task_id=task["task_id"], reason=reason, now=now
            )
            if state["state"] == "escalated":
                return {"task": task, "duck": duck, "attempt_eof": eof}
        if (
            outcome == "retryable_failure"
            and task["attempts"] < task["max_attempts"]
            and not budget_exhausted
        ):
            delay_index = min(
                task["attempts"] - 1, len(state["retry_delays_seconds"]) - 1
            )
            delay = int(state["retry_delays_seconds"][delay_index])
            task["state"] = "retry_wait"
            task["available_at"] = _time_text(now + timedelta(seconds=delay))
            task["retry_authorized"] = reason in AUTO_RETRY_REASONS
            alert = _add_alert(
                state,
                code=f"attempt.{reason}",
                severity="warning",
                facts={"attempt": lease["attempt"], "retry_after_seconds": delay},
                task_id=task["task_id"],
                episode=task["attempts"],
                now=now,
            )
            _enqueue_snapshot(
                store,
                state,
                plan,
                role="mother",
                trigger="ambiguous_attempt_failure",
                commands=["retry_task", "dead_letter", "notify_top"],
                subject_kind="task",
                subject_id=task["task_id"],
                now=now,
                alert_ids=[alert["id"]],
            )
        else:
            if budget_exhausted:
                reason = "task_wall_budget_exhausted"
                eof["reason"] = reason
            task["state"] = "dead_lettered"
            _set_task_eof(task, outcome="dead_lettered", reason=reason, now=now)
            _escalate_flock(state, reason=reason, now=now)
            alert = _add_alert(
                state,
                code="task.dead_lettered",
                severity="error",
                facts={"attempts": task["attempts"], "reason": reason},
                task_id=task["task_id"],
                episode=task["attempts"],
                now=now,
            )
            _enqueue_snapshot(
                store,
                state,
                plan,
                role="top",
                trigger="dead_letter",
                commands=["escalate_sol"],
                subject_kind="task",
                subject_id=task["task_id"],
                now=now,
                alert_ids=[alert["id"]],
            )
    return {"task": task, "duck": duck, "attempt_eof": eof}


def _verified_child_result(
    project_root: Path,
    flock_store: FlockStore,
    state: dict[str, Any],
    plan: dict[str, Any],
    task: dict[str, Any],
    lease: dict[str, Any],
    child_run_id: str,
) -> dict[str, Any]:
    """Re-hash and retain the exact child artifacts accepted by the flock."""

    child_run_id = require_identifier(child_run_id, "child_run_id")
    child_store = find_store_for_project(project_root, child_run_id)
    with child_store.lock():
        child_state = child_store.load()
        if child_state.get("state") not in {"ready_for_review", "accepted"}:
            raise AgentCtlError(
                "Child run must have green evidence and be reviewable or accepted",
                code="child_not_ready_for_review",
                details={"run_id": child_run_id, "state": child_state.get("state")},
            )
        try:
            child_project_root = Path(child_state.get("project_root", "")).resolve()
        except (TypeError, OSError) as exc:
            raise AgentCtlError("Child run state is corrupt", code="corrupt_state") from exc
        if child_project_root != project_root:
            raise AgentCtlError("Child run belongs to another project", code="project_mismatch")
        if child_state.get("base_sha") != state["base_sha"]:
            raise AgentCtlError(
                "Child run base does not match the flock base", code="review_mismatch"
            )
        if child_state.get("config_sha256") != state["config_sha256"]:
            raise AgentCtlError(
                "Child run config does not match the flock config", code="config_drift"
            )

        child_task_path = child_store.root / "task.json"
        child_task = read_json(child_task_path)
        if not isinstance(child_task, dict) or json_hash(child_task) != child_state.get(
            "task_sha256"
        ):
            raise AgentCtlError(
                "Frozen child task failed its integrity check", code="artifact_tampered"
            )
        expected_attempt = {
            "flock_id": state["flock_id"],
            "lease_id": lease["lease_id"],
            "coordinator_epoch": lease["coordinator_epoch"],
            "slot_id": lease["slot_id"],
            "duck_incarnation": lease["duck_incarnation"],
            "contract_sha256": lease["contract_sha256"],
            "branch_ref": lease["branch_ref"],
        }
        expected_child_task = deepcopy(_unit_map(plan)[task["task_id"]])
        expected_child_task["budget"]["wall_seconds"] = max(
            1, task["wall_budget_seconds"] - task["wall_used_seconds"]
        )
        expected_child_task["flock_attempt"] = expected_attempt
        if child_task != expected_child_task:
            raise AgentCtlError(
                "Child task differs from the exact active flock assignment",
                code="review_mismatch",
            )

        patch_path = child_store.root / "patch.diff"
        evidence_path = child_store.root / "evidence.json"
        try:
            patch_bytes = patch_path.read_bytes()
            evidence_bytes = evidence_path.read_bytes()
        except FileNotFoundError as exc:
            raise AgentCtlError(
                "Verified child artifacts are missing", code="artifact_tampered"
            ) from exc
        patch_sha256 = sha256_bytes(patch_bytes)
        evidence_sha256 = sha256_bytes(evidence_bytes)
        if patch_sha256 != child_state.get("patch_sha256"):
            raise AgentCtlError(
                "Frozen child patch failed its integrity check", code="artifact_tampered"
            )
        if evidence_sha256 != child_state.get("evidence_sha256"):
            raise AgentCtlError(
                "Frozen child evidence failed its integrity check",
                code="artifact_tampered",
            )
        try:
            evidence = json.loads(evidence_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentCtlError("Child evidence is corrupt", code="corrupt_state") from exc
        if (
            not isinstance(evidence, dict)
            or evidence.get("passed") is not True
            or evidence.get("run_id") != child_run_id
            or evidence.get("patch_sha256") != patch_sha256
        ):
            raise AgentCtlError(
                "Child evidence is not green and bound to the frozen patch",
                code="evidence_failed",
            )
        high_risk = bool(
            child_task.get("risk_flags")
            or evidence.get("high_risk_files")
            or evidence.get("high_risk_flags")
        )
        extra_payloads: dict[str, tuple[str, bytes]] = {}
        if high_risk and child_state["state"] != "accepted":
            raise AgentCtlError(
                "High-risk child work requires its hash-bound human gate before flock success",
                code="human_gate_required",
            )
        if child_state["state"] == "accepted":
            review_path = child_store.root / "review.json"
            try:
                review_bytes = review_path.read_bytes()
            except FileNotFoundError as exc:
                raise AgentCtlError(
                    "Accepted child review is missing", code="artifact_tampered"
                ) from exc
            if sha256_bytes(review_bytes) != child_state.get("review_sha256"):
                raise AgentCtlError(
                    "Accepted child review failed its integrity check",
                    code="artifact_tampered",
                )
            review = read_json(review_path)
            if (
                not isinstance(review, dict)
                or review.get("run_id") != child_run_id
                or review.get("task_sha256") != child_state["task_sha256"]
                or review.get("patch_sha256") != patch_sha256
                or review.get("evidence_sha256") != evidence_sha256
                or review.get("decision")
                != ("human_required" if high_risk else "accept")
            ):
                raise AgentCtlError(
                    "Accepted child review is not bound to the retained artifacts",
                    code="review_mismatch",
                )
            extra_payloads["review"] = ("review.json", review_bytes)
            if high_risk:
                approval_path = child_store.root / "human-approval.json"
                try:
                    approval_bytes = approval_path.read_bytes()
                except FileNotFoundError as exc:
                    raise AgentCtlError(
                        "High-risk child approval is missing",
                        code="artifact_tampered",
                    ) from exc
                if sha256_bytes(approval_bytes) != child_state.get(
                    "human_approval_sha256"
                ):
                    raise AgentCtlError(
                        "High-risk child approval failed its integrity check",
                        code="artifact_tampered",
                    )
                approval = read_json(approval_path)
                if (
                    not isinstance(approval, dict)
                    or approval.get("run_id") != child_run_id
                    or any(
                        approval.get(field) != child_state.get(field)
                        for field in (
                            "patch_sha256",
                            "task_sha256",
                            "evidence_sha256",
                            "review_sha256",
                        )
                    )
                ):
                    raise AgentCtlError(
                        "High-risk child approval is not hash-bound",
                        code="approval_mismatch",
                    )
                extra_payloads["human_approval"] = (
                    "human-approval.json",
                    approval_bytes,
                )

        review_pack = {
            "contract_version": 1,
            "run_id": child_run_id,
            "created_at": _time_text(datetime.now(UTC)),
            "task_sha256": child_state["task_sha256"],
            "evidence_sha256": evidence_sha256,
            "task": child_task,
            "patch_sha256": patch_sha256,
            "patch": patch_bytes.decode("utf-8", errors="replace"),
            "evidence": evidence,
            "worker_self_report_trusted": False,
        }
        review_pack_bytes = canonical_json(review_pack)
        review_pack_sha256 = sha256_bytes(review_pack_bytes)

        retained_root = flock_store.root / "children" / task["task_id"] / child_run_id
        retained_root.mkdir(parents=True, exist_ok=True)
        os.chmod(retained_root, 0o700)
        retained = {
            "task": ("task.json", child_task_path.read_bytes()),
            "patch": ("patch.diff", patch_bytes),
            "evidence": ("evidence.json", evidence_bytes),
            "review_pack": ("review-pack.json", review_pack_bytes),
            **extra_payloads,
        }
        refs: dict[str, str] = {}
        for name, (filename, payload) in retained.items():
            destination = retained_root / filename
            atomic_write_bytes(destination, payload, mode=0o600)
            refs[f"{name}_ref"] = os.fspath(destination.relative_to(flock_store.root))
        extra_hashes = {
            f"{name}_sha256": sha256_bytes(payload)
            for name, (_, payload) in extra_payloads.items()
        }

    return {
        "run_id": child_run_id,
        "base_sha": state["base_sha"],
        "unit_contract_sha256": task["task_sha256"],
        "child_task_sha256": child_state["task_sha256"],
        "patch_sha256": patch_sha256,
        "evidence_sha256": evidence_sha256,
        "review_pack_sha256": review_pack_sha256,
        "child_state": child_state["state"],
        **extra_hashes,
        **refs,
    }


def _existing_finish(
    state: dict[str, Any],
    *,
    slot_id: int,
    lease_id: str,
    outcome: str,
    reason: str,
    child_run_id: str | None,
    artifacts: dict[str, str],
) -> dict[str, Any] | None:
    for task in state["tasks"].values():
        for eof in task["attempt_history"]:
            if eof.get("lease_id") != lease_id or eof.get("slot_id") != slot_id:
                continue
            if (
                eof.get("reported_outcome", eof.get("outcome")) != outcome
                or eof.get("reported_reason", eof.get("reason")) != reason
            ):
                raise AgentCtlError(
                    "Lease already has a different terminal EOF", code="duplicate_eof"
                )
            if outcome == "succeeded":
                verified = task.get("verified_child") or {}
                if verified.get("run_id") != child_run_id:
                    raise AgentCtlError(
                        "Lease success is bound to a different child run",
                        code="duplicate_eof",
                    )
            elif eof.get("artifacts", {}) != dict(sorted(artifacts.items())):
                raise AgentCtlError(
                    "Lease EOF is bound to different artifacts", code="duplicate_eof"
                )
            return {
                "ok": eof.get("outcome") == "succeeded",
                "flock_id": state["flock_id"],
                "flock_state": state["state"],
                "task_id": task["task_id"],
                "task_state": task["state"],
                "duck": state["ducks"][slot_id],
                "attempt_eof": eof,
                "idempotent": True,
            }
    return None


def flock_finish(
    project_path: Path,
    flock_id: str,
    *,
    slot_id: int,
    lease_id: str,
    outcome: str,
    reason: str,
    child_run_id: str | None = None,
    artifacts: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if outcome not in {"succeeded", "retryable_failure", "fatal_failure", "cancelled"}:
        raise AgentCtlError("Invalid duck outcome", code="invalid_contract")
    require_identifier(reason, "reason")
    if outcome == "succeeded" and not child_run_id:
        raise AgentCtlError(
            "Successful EOF requires --run CHILD_RUN_ID",
            code="unverified_child_result",
        )
    if outcome != "succeeded" and child_run_id is not None:
        raise AgentCtlError(
            "--run is valid only for succeeded EOF", code="invalid_contract"
        )
    normalized_artifacts = artifacts or {}
    if outcome == "succeeded" and normalized_artifacts:
        raise AgentCtlError(
            "Success artifacts are derived from the verified child run",
            code="invalid_contract",
        )
    for name, digest in normalized_artifacts.items():
        require_identifier(name, "artifact name")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise AgentCtlError(
                "Artifact values must be lowercase SHA-256 digests",
                code="invalid_contract",
            )
    current = now or datetime.now(UTC)
    project_root, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        existing = _existing_finish(
            state,
            slot_id=slot_id,
            lease_id=lease_id,
            outcome=outcome,
            reason=reason,
            child_run_id=child_run_id,
            artifacts=normalized_artifacts,
        )
        if existing is not None:
            return existing
        plan = _load_frozen_plan(store, state)
        _, task, lease = _active_task(state, slot_id=slot_id, lease_id=lease_id)
        elapsed_seconds = max(
            0, int((current - _parse_time(lease["issued_at"])).total_seconds())
        )
        budget_will_expire = (
            task["wall_used_seconds"] + elapsed_seconds
            >= task["wall_budget_seconds"]
        )
        verified_child = None
        authoritative_artifacts = normalized_artifacts
        if outcome == "succeeded" and not budget_will_expire:
            verified_child = _verified_child_result(
                project_root,
                store,
                state,
                plan,
                task,
                lease,
                child_run_id or "",
            )
            authoritative_artifacts = {
                "child_task_sha256": verified_child["child_task_sha256"],
                "patch_sha256": verified_child["patch_sha256"],
                "evidence_sha256": verified_child["evidence_sha256"],
                "review_pack_sha256": verified_child["review_pack_sha256"],
            }
        result = _finish_in_state(
            store,
            state,
            plan,
            slot_id=slot_id,
            lease_id=lease_id,
            outcome=outcome,
            reason=reason,
            artifacts=authoritative_artifacts,
            worker_eof_seen=True,
            verified_child=verified_child,
            now=current,
        )
        store.save(
            state,
            actor=f"duck_{slot_id}",
            reason="attempt_eof",
            artifacts={
                "task_id": result["task"]["task_id"],
                "outcome": outcome,
                "attempt": result["attempt_eof"]["attempt"],
            },
            now=current,
        )
        return {
            "ok": result["attempt_eof"]["outcome"] == "succeeded",
            "flock_id": flock_id,
            "flock_state": state["state"],
            "task_id": result["task"]["task_id"],
            "task_state": result["task"]["state"],
            "duck": result["duck"],
            "attempt_eof": result["attempt_eof"],
        }


def flock_sweep(
    project_path: Path, flock_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    _, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        if state["state"] in FLOCK_TERMINAL_STATES:
            return {"ok": True, "flock_id": flock_id, "state": state["state"], "events": []}
        plan = _load_frozen_plan(store, state)
        policy = state["lease_policy"]
        events: list[dict[str, Any]] = []
        active = [
            (task, task.get("current_lease"))
            for task in state["tasks"].values()
            if task.get("current_lease")
        ]
        for task, lease in active:
            if task.get("current_lease") is not lease:
                continue
            liveness_age = (current - _parse_time(lease["last_heartbeat_at"])).total_seconds()
            progress_age = (current - _parse_time(lease["last_progress_at"])).total_seconds()
            attempt_age = (current - _parse_time(lease["issued_at"])).total_seconds()
            hard_reason: str | None = None
            if task["wall_used_seconds"] + attempt_age >= task["wall_budget_seconds"]:
                hard_reason = "task_wall_budget_exhausted"
            elif liveness_age >= policy["liveness_hard_seconds"]:
                hard_reason = "lease_hard_expired"
            elif progress_age >= policy["progress_hard_seconds"]:
                hard_reason = "progress_hard_expired"
            if hard_reason:
                result = _finish_in_state(
                    store,
                    state,
                    plan,
                    slot_id=lease["slot_id"],
                    lease_id=lease["lease_id"],
                    outcome="retryable_failure",
                    reason=hard_reason,
                    artifacts={},
                    worker_eof_seen=False,
                    now=current,
                )
                events.append(
                    {
                        "task_id": task["task_id"],
                        "event": "hard_expiry",
                        "reason": hard_reason,
                        "task_state": result["task"]["state"],
                    }
                )
                continue
            soft_reason: str | None = None
            if liveness_age >= policy["liveness_soft_seconds"]:
                soft_reason = "lease_soft_expired"
            elif progress_age >= policy["progress_soft_seconds"]:
                soft_reason = "progress_soft_expired"
            if soft_reason and task["state"] != "soft_stalled":
                task["state"] = "soft_stalled"
                task["revision"] += 1
                lease["stall_episode"] += 1
                alert = _add_alert(
                    state,
                    code=f"lease.{soft_reason}",
                    severity="warning",
                    facts={
                        "liveness_age_seconds": int(liveness_age),
                        "progress_age_seconds": int(progress_age),
                    },
                    task_id=task["task_id"],
                    episode=lease["stall_episode"],
                    now=current,
                )
                _enqueue_snapshot(
                    store,
                    state,
                    plan,
                    role="mother",
                    trigger="soft_stall",
                    commands=["wait", "recycle", "notify_top"],
                    subject_kind="task",
                    subject_id=task["task_id"],
                    now=current,
                    alert_ids=[alert["id"]],
                )
                events.append(
                    {"task_id": task["task_id"], "event": "soft_stall", "reason": soft_reason}
                )
        if events:
            store.save(
                state,
                actor="runtime_supervisor",
                reason="lease_sweep",
                artifacts={"events": events},
                now=current,
            )
        return {"ok": True, "flock_id": flock_id, "state": state["state"], "events": events}


def _snapshot_item(state: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    for item in state["outbox"]:
        if item["snapshot_id"] == snapshot_id:
            return item
    raise AgentCtlError("Semantic snapshot was not found", code="snapshot_not_found")


def _snapshot_is_current(state: dict[str, Any], item: dict[str, Any]) -> bool:
    if item.get("state") != "pending":
        return False
    if int(item.get("coordinator_epoch", -1)) != int(state["coordinator_epoch"]):
        return False
    if int(item.get("flock_revision", -1)) != int(
        state.get("aggregate_revision", 0)
    ):
        return False
    if item.get("flock_state") != state.get("state"):
        return False
    try:
        actual = _subject_revision(
            state, item["subject_kind"], item.get("subject_id")
        )
    except (KeyError, TypeError):
        return False
    return int(item.get("subject_revision", -1)) == actual


def next_semantic_snapshot(
    project_path: Path, flock_id: str, *, role: str
) -> dict[str, Any]:
    if role not in {"mother", "top", "sol"}:
        raise AgentCtlError("Invalid semantic role", code="invalid_contract")
    _, store, state = _store_for(project_path, flock_id)
    for item in state["outbox"]:
        if (
            item["role"] == role
            and item["state"] == "pending"
            and _snapshot_is_current(state, item)
        ):
            return {
                "ok": True,
                "flock_id": flock_id,
                "pending": True,
                **item,
                "snapshot_path": os.fspath(store.root / item["path"]),
            }
    return {"ok": True, "flock_id": flock_id, "pending": False, "role": role}


def claim_semantic_snapshot(
    project_path: Path,
    flock_id: str,
    *,
    role: str,
    claim_seconds: int = 300,
    now: datetime | None = None,
) -> dict[str, Any]:
    if role not in {"mother", "top"}:
        raise AgentCtlError("Invalid external semantic role", code="invalid_contract")
    if claim_seconds < 1 or claim_seconds > SEMANTIC_CLAIM_MAX_SECONDS:
        raise AgentCtlError("Invalid semantic claim duration", code="invalid_contract")
    current = now or datetime.now(UTC)
    _, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        for item in state["outbox"]:
            if (
                item["role"] != role
                or item["state"] != "pending"
                or not _snapshot_is_current(state, item)
            ):
                continue
            if int(item.get("delivery_attempts", 0)) >= SEMANTIC_MAX_ATTEMPTS:
                continue
            next_delivery_at = item.get("next_delivery_at")
            if next_delivery_at and _parse_time(next_delivery_at) > current:
                continue
            claimed_until = item.get("claimed_until")
            if claimed_until and _parse_time(claimed_until) > current:
                continue
            item["claim_id"] = secrets.token_hex(16)
            item["claimed_at"] = _time_text(current)
            item["claimed_until"] = _time_text(
                current + timedelta(seconds=claim_seconds)
            )
            store.save(
                state,
                actor="semantic_dispatcher",
                reason="semantic_snapshot_claimed",
                artifacts={
                    "snapshot_id": item["snapshot_id"],
                    "role": role,
                },
                now=current,
            )
            return {
                "ok": True,
                "flock_id": flock_id,
                "pending": True,
                **item,
                "snapshot_path": os.fspath(store.root / item["path"]),
            }
    return {"ok": True, "flock_id": flock_id, "pending": False, "role": role}


def _enqueue_top_for_task(
    store: FlockStore,
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    task_id: str,
    now: datetime,
) -> None:
    task = state["tasks"][task_id]
    if task["state"] == "succeeded":
        task["reported_to_top"] = True
        task["revision"] += 1
    _enqueue_snapshot(
        store,
        state,
        plan,
        role="top",
        trigger="task_terminal",
        commands=["ack", "escalate_sol"],
        subject_kind="task",
        subject_id=task_id,
        now=now,
    )


def _aggregate_manifest_value(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "ducking.aggregate-manifest/v1",
        "flock_id": state["flock_id"],
        "base_sha": state["base_sha"],
        "plan_sha256": state["plan_sha256"],
        "tasks": [
            {
                "task_id": task["task_id"],
                "task_sha256": task["task_sha256"],
                "verified_child": task["verified_child"],
                "attempt_eof": task["attempt_history"][-1],
            }
            for task in sorted(
                state["tasks"].values(), key=lambda value: value["task_id"]
            )
        ],
        "integration": {
            "combined_patch_created": False,
            "combined_verification_run": False,
        },
    }


def _open_aggregate_review(
    store: FlockStore,
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    now: datetime,
) -> None:
    if state["state"] != "running" or not all(
        task["state"] == "succeeded"
        and task["reported_to_top"]
        and isinstance(task.get("verified_child"), dict)
        for task in state["tasks"].values()
    ):
        return
    manifest = _aggregate_manifest_value(state)
    manifest_path = store.root / "aggregate-manifest.json"
    atomic_write_bytes(manifest_path, canonical_json(manifest), mode=0o600)
    state["aggregate_manifest_sha256"] = sha256_file(manifest_path)
    state["state"] = "final_review"
    state["aggregate_revision"] += 1
    _enqueue_snapshot(
        store,
        state,
        plan,
        role="top",
        trigger="aggregate_ready",
        commands=["open_final_review"],
        subject_kind="flock",
        subject_id=None,
        now=now,
    )


def _verify_aggregate_artifacts(store: FlockStore, state: dict[str, Any]) -> None:
    manifest_path = store.root / "aggregate-manifest.json"
    expected_manifest = state.get("aggregate_manifest_sha256")
    if not expected_manifest or sha256_file(manifest_path) != expected_manifest:
        raise AgentCtlError(
            "Aggregate manifest failed its integrity check", code="artifact_tampered"
        )
    if read_json(manifest_path) != _aggregate_manifest_value(state):
        raise AgentCtlError(
            "Aggregate manifest no longer matches flock state",
            code="artifact_tampered",
        )
    for task in state["tasks"].values():
        child = task.get("verified_child")
        if not isinstance(child, dict):
            raise AgentCtlError(
                "Aggregate task has no verified child result",
                code="unverified_child_result",
            )
        required = {
            "child_task_sha256",
            "patch_sha256",
            "evidence_sha256",
            "review_pack_sha256",
            "task_ref",
            "patch_ref",
            "evidence_ref",
            "review_pack_ref",
        }
        if not required.issubset(child) or not all(
            isinstance(child[name], str) for name in required
        ):
            raise AgentCtlError(
                "Verified child artifact binding is corrupt", code="corrupt_state"
            )
        task_path = ensure_within(store.root, child["task_ref"], "child task ref")
        checks = {
            "child_task_sha256": json_hash(read_json(task_path)),
            "patch_sha256": sha256_file(
                ensure_within(store.root, child["patch_ref"], "child patch ref")
            ),
            "evidence_sha256": sha256_file(
                ensure_within(
                    store.root, child["evidence_ref"], "child evidence ref"
                )
            ),
            "review_pack_sha256": sha256_file(
                ensure_within(
                    store.root,
                    child["review_pack_ref"],
                    "child review pack ref",
                )
            ),
        }
        for name, actual in checks.items():
            if actual != child.get(name):
                raise AgentCtlError(
                    f"Retained child {name} failed its integrity check",
                    code="artifact_tampered",
                )
        for name in ("review", "human_approval"):
            ref_name = f"{name}_ref"
            hash_name = f"{name}_sha256"
            if ref_name not in child and hash_name not in child:
                continue
            if not isinstance(child.get(ref_name), str) or not isinstance(
                child.get(hash_name), str
            ):
                raise AgentCtlError(
                    f"Retained child {name} binding is corrupt",
                    code="corrupt_state",
                )
            actual = sha256_file(
                ensure_within(store.root, child[ref_name], f"child {name} ref")
            )
            if actual != child[hash_name]:
                raise AgentCtlError(
                    f"Retained child {name} failed its integrity check",
                    code="artifact_tampered",
                )


def submit_semantic_action(
    project_path: Path,
    flock_id: str,
    action_value: dict[str, Any],
    *,
    claim_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    action = validate_semantic_action(action_value)
    current = now or datetime.now(UTC)
    _, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        plan = _load_frozen_plan(store, state)
        item = _snapshot_item(state, action["snapshot_id"])
        if item["snapshot_sha256"] != action["snapshot_sha256"]:
            raise AgentCtlError(
                "Semantic action has the wrong snapshot hash",
                code="stale_decision",
            )
        if item["state"] == "done":
            previous = item.get("decision", {})
            if previous.get("selected_command_id") == action["selected_command_id"]:
                return {"ok": True, "flock_id": flock_id, "idempotent": True}
            raise AgentCtlError("Snapshot already has a different decision", code="stale_decision")
        if item["state"] != "pending":
            raise AgentCtlError("Semantic snapshot is no longer pending", code="stale_decision")
        current_claim = item.get("claim_id")
        if current_claim is not None and claim_id != current_claim:
            raise AgentCtlError("Semantic delivery claim is stale", code="stale_claim")
        if claim_id is not None and current_claim != claim_id:
            raise AgentCtlError("Semantic delivery claim is stale", code="stale_claim")
        if current_claim is not None and (
            not item.get("claimed_until")
            or _parse_time(item["claimed_until"]) <= current
        ):
            raise AgentCtlError("Semantic delivery claim expired", code="stale_claim")
        command = next(
            (
                candidate
                for candidate in item["commands"]
                if candidate["command_id"] == action["selected_command_id"]
            ),
            None,
        )
        if command is None:
            raise AgentCtlError(
                "Command was not offered by this snapshot",
                code="invalid_semantic_command",
            )
        actual_revision = _subject_revision(
            state, item["subject_kind"], item.get("subject_id")
        )
        if (
            int(command["if_revision"]) != actual_revision
            or int(command.get("if_flock_revision", -1))
            != int(state.get("aggregate_revision", 0))
            or int(command.get("if_coordinator_epoch", -1))
            != int(state["coordinator_epoch"])
            or command.get("if_flock_state") != state["state"]
        ):
            raise AgentCtlError("Semantic action is stale", code="stale_decision")

        name = command["name"]
        task_id = item.get("subject_id")
        if name == "notify_top":
            if state["state"] != "running":
                raise AgentCtlError("Flock is no longer running", code="stale_decision")
            if not task_id:
                raise AgentCtlError("notify_top requires a task", code="invalid_semantic_command")
            _enqueue_top_for_task(store, state, plan, task_id=task_id, now=current)
        elif name == "retry_task":
            if state["state"] != "running":
                raise AgentCtlError("Flock is no longer running", code="stale_decision")
            task = state["tasks"][task_id]
            if task["state"] != "retry_wait":
                raise AgentCtlError(
                    "Task is not waiting for retry",
                    code="invalid_semantic_command",
                )
            task["retry_authorized"] = True
            task["revision"] += 1
        elif name == "dead_letter":
            if state["state"] != "running":
                raise AgentCtlError("Flock is no longer running", code="stale_decision")
            task = state["tasks"][task_id]
            if task["state"] != "retry_wait":
                raise AgentCtlError(
                    "Task cannot be dead-lettered now",
                    code="invalid_semantic_command",
                )
            task["state"] = "dead_lettered"
            task["revision"] += 1
            _set_task_eof(task, outcome="dead_lettered", reason=action["reason_code"], now=current)
            _escalate_flock(state, reason=action["reason_code"], now=current)
            _enqueue_snapshot(
                store,
                state,
                plan,
                role="top",
                trigger="dead_letter",
                commands=["escalate_sol"],
                subject_kind="task",
                subject_id=task_id,
                now=current,
            )
        elif name == "recycle":
            if state["state"] != "running":
                raise AgentCtlError("Flock is no longer running", code="stale_decision")
            task = state["tasks"][task_id]
            lease = task.get("current_lease")
            if not lease:
                raise AgentCtlError("Task no longer has an active lease", code="stale_decision")
            _finish_in_state(
                store,
                state,
                plan,
                slot_id=lease["slot_id"],
                lease_id=lease["lease_id"],
                outcome="retryable_failure",
                reason="transient_tool_error",
                artifacts={},
                worker_eof_seen=False,
                now=current,
            )
        elif name == "escalate_sol":
            _enqueue_snapshot(
                store,
                state,
                plan,
                role="sol",
                trigger="senior_escalation",
                commands=["ack", "abort_flock"],
                subject_kind=item["subject_kind"],
                subject_id=task_id,
                now=current,
            )
        elif name == "open_final_review":
            if state["state"] != "final_review":
                raise AgentCtlError("Flock is not ready for final review", code="stale_decision")
            _enqueue_snapshot(
                store,
                state,
                plan,
                role="sol",
                trigger="aggregate_final_review",
                commands=["approve_flock", "rework", "abort_flock"],
                subject_kind="flock",
                subject_id=None,
                now=current,
            )
        elif name == "abort_flock":
            if item["role"] != "sol":
                raise AgentCtlError(
                    "Only Sol may abort a flock", code="invalid_semantic_command"
                )
            _escalate_flock(state, reason=action["reason_code"], now=current)
        elif name == "approve_flock":
            if item["role"] != "sol":
                raise AgentCtlError(
                    "Only Sol may approve final review",
                    code="invalid_semantic_command",
                )
            if state["state"] != "final_review" or not all(
                task["state"] == "succeeded"
                and task["reported_to_top"]
                and isinstance(task.get("verified_child"), dict)
                for task in state["tasks"].values()
            ):
                raise AgentCtlError(
                    "Flock is not eligible for completion", code="stale_decision"
                )
            _verify_aggregate_artifacts(store, state)
            _supersede_pending_snapshots(
                state, reason="aggregate_review_approved", now=current
            )
            state["state"] = "reviewed"
            state["aggregate_revision"] += 1
        elif name == "rework":
            if item["role"] != "sol":
                raise AgentCtlError(
                    "Only Sol may request aggregate rework",
                    code="invalid_semantic_command",
                )
            if state["state"] != "final_review":
                raise AgentCtlError("Flock is not in final review", code="stale_decision")
            _escalate_flock(state, reason=action["reason_code"], now=current)
        elif name not in {"ack", "hold", "wait"}:
            raise AgentCtlError("Unsupported semantic command", code="invalid_semantic_command")

        if name == "ack" and task_id:
            task = state["tasks"].get(task_id)
            if task and task["state"] == "succeeded" and task["reported_to_top"]:
                _open_aggregate_review(store, state, plan, now=current)
            elif task and task["state"] == "retry_wait":
                task["retry_authorized"] = True
                task["revision"] += 1

        item["state"] = "done"
        item.pop("claim_id", None)
        item.pop("claimed_at", None)
        item.pop("claimed_until", None)
        item["completed_at"] = _time_text(current)
        item["decision"] = {
            "selected_command_id": action["selected_command_id"],
            "reason_code": action["reason_code"],
        }
        action_path = store.root / "actions" / f"{action['snapshot_id']}.json"
        atomic_write_json(action_path, action)
        store.save(
            state,
            actor=f"semantic_{item['role']}",
            reason="semantic_command_applied",
            artifacts={"snapshot_id": item["snapshot_id"], "command": name},
            now=current,
        )
        return {
            "ok": True,
            "flock_id": flock_id,
            "snapshot_id": item["snapshot_id"],
            "command": name,
            "state": state["state"],
        }


def record_semantic_delivery_failure(
    project_path: Path,
    flock_id: str,
    snapshot_id: str,
    *,
    claim_id: str,
    error_code: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    _, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        item = _snapshot_item(state, snapshot_id)
        if item["state"] != "pending":
            return {"ok": True, "flock_id": flock_id, "pending": False}
        if item.get("claim_id") != claim_id:
            raise AgentCtlError(
                "Semantic delivery claim is stale", code="stale_claim"
            )
        if (
            not item.get("claimed_until")
            or _parse_time(item["claimed_until"]) <= current
        ):
            raise AgentCtlError(
                "Semantic delivery claim expired", code="stale_claim"
            )
        if not _snapshot_is_current(state, item):
            raise AgentCtlError(
                "Semantic snapshot no longer matches current state",
                code="stale_decision",
            )
        item["delivery_attempts"] += 1
        item.pop("claim_id", None)
        item.pop("claimed_at", None)
        item.pop("claimed_until", None)
        item["last_error_code"] = require_identifier(error_code, "error_code")
        item["last_attempt_at"] = _time_text(current)
        exhausted = item["delivery_attempts"] >= SEMANTIC_MAX_ATTEMPTS
        alert = _add_alert(
            state,
            code=(
                "semantic.delivery_exhausted"
                if exhausted
                else "semantic.delivery_failed"
            ),
            severity="critical" if exhausted else "warning",
            facts={"snapshot_id": snapshot_id, "error_code": error_code},
            task_id=item.get("subject_id") if item["subject_kind"] == "task" else None,
            episode=item["delivery_attempts"],
            now=current,
        )
        if exhausted:
            item["state"] = "exhausted"
            plan = _load_frozen_plan(store, state)
            _escalate_flock(
                state, reason="semantic_delivery_exhausted", now=current
            )
            _enqueue_snapshot(
                store,
                state,
                plan,
                role="sol",
                trigger="semantic_delivery_exhausted",
                commands=["ack", "abort_flock"],
                subject_kind="flock",
                subject_id=None,
                now=current,
                alert_ids=[alert["id"]],
            )
        else:
            delay_index = min(
                item["delivery_attempts"] - 1,
                len(SEMANTIC_RETRY_DELAYS_SECONDS) - 1,
            )
            item["next_delivery_at"] = _time_text(
                current
                + timedelta(seconds=SEMANTIC_RETRY_DELAYS_SECONDS[delay_index])
            )
        store.save(
            state,
            actor="semantic_dispatcher",
            reason="semantic_delivery_failed",
            artifacts={"snapshot_id": snapshot_id, "error_code": error_code},
            now=current,
        )
        return {
            "ok": False,
            "flock_id": flock_id,
            "pending": not exhausted,
            "delivery_attempts": item["delivery_attempts"],
            "state": state["state"],
            "next_delivery_at": item.get("next_delivery_at"),
        }


def cancel_flock(
    project_path: Path, flock_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    _, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        if state["state"] in FLOCK_TERMINAL_STATES:
            return {"ok": True, "flock_id": flock_id, "state": state["state"]}
        plan = _load_frozen_plan(store, state)
        for task in state["tasks"].values():
            lease = task.get("current_lease")
            if lease:
                _finish_in_state(
                    store,
                    state,
                    plan,
                    slot_id=lease["slot_id"],
                    lease_id=lease["lease_id"],
                    outcome="cancelled",
                    reason="flock_cancelled",
                    artifacts={},
                    worker_eof_seen=False,
                    global_cancel=True,
                    now=current,
                )
            elif task["state"] not in TASK_TERMINAL_STATES:
                task["state"] = "cancelled"
                task["revision"] += 1
                _set_task_eof(task, outcome="cancelled", reason="flock_cancelled", now=current)
        _supersede_pending_snapshots(
            state, reason="flock_cancelled", now=current
        )
        state["state"] = "cancelled"
        state["aggregate_revision"] += 1
        store.save(
            state,
            actor="runtime_supervisor",
            reason="flock_cancelled",
            now=current,
        )
        return {"ok": True, "flock_id": flock_id, "state": "cancelled"}


def recover_flock(
    project_path: Path,
    flock_id: str,
    *,
    expected_epoch: int,
    operation_id: str,
    reason: str = "coordinator_lost",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fence a lost mother generation and rebuild its six-slot subtree.

    A real runtime root calls this after it has stopped the old child-controller
    processes. The durable epoch, not a recycled PID, rejects late events.
    """

    require_identifier(reason, "reason")
    operation_id = require_identifier(operation_id, "operation_id")
    if expected_epoch < 1:
        raise AgentCtlError("expected_epoch must be positive", code="invalid_contract")
    current = now or datetime.now(UTC)
    _, store, _ = _store_for(project_path, flock_id)
    with store.lock():
        state = store.load()
        previous = state.setdefault("recovery_operations", {}).get(operation_id)
        if previous is not None:
            return {**previous, "idempotent": True}
        if int(state["coordinator_epoch"]) != expected_epoch:
            raise AgentCtlError(
                "Coordinator recovery expected a different epoch",
                code="stale_recovery",
                details={
                    "expected": expected_epoch,
                    "actual": state["coordinator_epoch"],
                },
            )
        if state["state"] in FLOCK_TERMINAL_STATES:
            return {
                "ok": True,
                "flock_id": flock_id,
                "state": state["state"],
                "recovered": False,
            }
        if state["state"] != "running":
            result = {
                "ok": True,
                "flock_id": flock_id,
                "state": state["state"],
                "recovered": False,
                "coordinator_epoch": state["coordinator_epoch"],
                "operation_id": operation_id,
            }
            state["recovery_operations"][operation_id] = result
            store.save(
                state,
                actor="runtime_supervisor",
                reason="coordinator_recovery_not_needed",
                artifacts={"state": state["state"]},
                now=current,
            )
            return result
        plan = _load_frozen_plan(store, state)
        fenced: list[dict[str, Any]] = []
        active = [
            task["current_lease"]
            for task in state["tasks"].values()
            if task.get("current_lease")
        ]
        active_task_ids = {lease["task_id"] for lease in active}
        semantic_replay = [
            {
                "role": item["role"],
                "trigger": item["trigger"],
                "commands": [command["name"] for command in item["commands"]],
                "subject_kind": item["subject_kind"],
                "subject_id": item.get("subject_id"),
            }
            for item in state["outbox"]
            if _snapshot_is_current(state, item)
            and not (
                item["subject_kind"] == "task"
                and item.get("subject_id") in active_task_ids
            )
        ]
        for lease in active:
            if not any(
                task.get("current_lease", {}).get("lease_id") == lease["lease_id"]
                for task in state["tasks"].values()
                if task.get("current_lease")
            ):
                continue
            result = _finish_in_state(
                store,
                state,
                plan,
                slot_id=lease["slot_id"],
                lease_id=lease["lease_id"],
                outcome="retryable_failure",
                reason="controller_lost",
                artifacts={},
                worker_eof_seen=False,
                now=current,
            )
            fenced.append(
                {
                    "task_id": result["task"]["task_id"],
                    "lease_id": lease["lease_id"],
                    "task_state": result["task"]["state"],
                }
            )
        state["coordinator_epoch"] += 1
        _supersede_pending_snapshots(
            state, reason="coordinator_epoch_changed", now=current
        )
        policy = state["root_restart_policy"]
        cutoff = current - timedelta(seconds=int(policy["window_seconds"]))
        events = [
            item
            for item in state["root_restart_events"]
            if _parse_time(item["at"]) >= cutoff
        ]
        events.append({"at": _time_text(current), "reason": reason})
        state["root_restart_events"] = events
        if len(events) > int(policy["max_restarts"]):
            _escalate_flock(
                state, reason="root_restart_intensity", now=current
            )
            alert = _add_alert(
                state,
                code="supervision.root_restart_intensity",
                severity="critical",
                facts={
                    "count": len(events),
                    "window_seconds": policy["window_seconds"],
                },
                task_id=None,
                episode=state["aggregate_revision"],
                now=current,
            )
            _enqueue_snapshot(
                store,
                state,
                plan,
                role="top",
                trigger="root_circuit_open",
                commands=["escalate_sol"],
                subject_kind="flock",
                subject_id=None,
                now=current,
                alert_ids=[alert["id"]],
            )
        if state["state"] == "running":
            for obligation in semantic_replay:
                _enqueue_snapshot(
                    store,
                    state,
                    plan,
                    role=obligation["role"],
                    trigger=obligation["trigger"],
                    commands=obligation["commands"],
                    subject_kind=obligation["subject_kind"],
                    subject_id=obligation["subject_id"],
                    now=current,
                )
        result = {
            "ok": state["state"] != "escalated",
            "flock_id": flock_id,
            "state": state["state"],
            "recovered": True,
            "coordinator_epoch": state["coordinator_epoch"],
            "fenced_leases": fenced,
            "replayed_semantic_obligations": len(semantic_replay),
            "operation_id": operation_id,
        }
        state["recovery_operations"][operation_id] = result
        store.save(
            state,
            actor="runtime_supervisor",
            reason="coordinator_recovered",
            artifacts={
                "coordinator_epoch": state["coordinator_epoch"],
                "fenced_leases": fenced,
            },
            now=current,
        )
        return result


def _status_value(state: dict[str, Any]) -> dict[str, Any]:
    active = sum(1 for duck in state["ducks"] if duck["state"] == "busy")
    terminal = all(
        task["state"] in TASK_TERMINAL_STATES for task in state["tasks"].values()
    )
    eof = terminal and active == 0 and state["state"] in FLOCK_TERMINAL_STATES
    return {
        "flock_id": state["flock_id"],
        "plan_id": state["plan_id"],
        "state": state["state"],
        "revision": state["revision"],
        "coordinator_epoch": state["coordinator_epoch"],
        "duck_count": state["duck_count"],
        "active_ducks": active,
        "task_summary": _task_summary(state),
        "tasks": [
            {
                "task_id": task["task_id"],
                "state": task["state"],
                "attempts": task["attempts"],
                "max_attempts": task["max_attempts"],
                "wall_used_seconds": task["wall_used_seconds"],
                "wall_budget_seconds": task["wall_budget_seconds"],
                "reported_to_top": task["reported_to_top"],
                "task_eof": task["task_eof"],
            }
            for task in state["tasks"].values()
        ],
        "ducks": state["ducks"],
        "pending_semantic": {
            role: sum(
                1
                for item in state["outbox"]
                if item["role"] == role
                and item["state"] == "pending"
                and _snapshot_is_current(state, item)
            )
            for role in ("mother", "top", "sol")
        },
        "eof": eof,
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
    }


def flock_status(project_path: Path, flock_id: str | None) -> dict[str, Any]:
    project_root = find_git_root(project_path)
    if flock_id:
        _, store, state = _store_for(project_root, flock_id)
        return {"ok": True, "flock_root": os.fspath(store.root), **_status_value(state)}
    values = [_status_value(store.load()) for store in _stores_for_project(project_root)]
    values.sort(key=lambda item: item["created_at"], reverse=True)
    return {"ok": True, "flocks": values}
