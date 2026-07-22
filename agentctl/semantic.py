from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import WorkerProfile
from .errors import AgentCtlError
from .flock import (
    SEMANTIC_CLAIM_MAX_SECONDS,
    _store_for,
    claim_semantic_snapshot,
    record_semantic_delivery_failure,
    submit_semantic_action,
)
from .util import (
    atomic_write_bytes,
    canonical_json,
    json_hash,
    read_json,
    require_identifier,
    sha256_bytes,
)
from .worker import run_worker


def _profile_from_value(value: dict[str, Any]) -> WorkerProfile:
    try:
        return WorkerProfile(
            name=value["name"],
            adapter=value["adapter"],
            argv=tuple(value["argv"]),
            probe_argv=tuple(value["probe_argv"]),
            probe_contains=value["probe_contains"],
            env_allow=tuple(value["env_allow"]),
            timeout_seconds=int(value["timeout_seconds"]),
            max_output_bytes=int(value["max_output_bytes"]),
            prompt=value["prompt"],
            isolation=value["isolation"],
            runtime_id=value["runtime_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentCtlError(
            "Frozen semantic profile is corrupt", code="corrupt_state"
        ) from exc


def _prepare_workspace(
    root: Path,
    claim_id: str,
    snapshot: dict[str, Any],
    snapshot_sha256: str,
) -> tuple[Path, Path]:
    workspace = root / f"workspace-{claim_id}"
    workspace.mkdir(parents=True, mode=0o700)
    runtime = workspace / ".agentctl-runtime"
    runtime.mkdir(mode=0o700)
    request = runtime / "request.json"
    value = {"snapshot_sha256": snapshot_sha256, "snapshot": snapshot}
    envelope = canonical_json(value)
    readable_request = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(root / "delivery-envelope.json", envelope, mode=0o600)
    atomic_write_bytes(request, readable_request, mode=0o444)
    return workspace, request


def _exact_json_object(value: str) -> dict[str, Any] | None:
    stripped = value.strip()
    if stripped.startswith("```json\n") and stripped.endswith("\n```"):
        if stripped.count("```") != 2:
            return None
        stripped = stripped[len("```json\n") : -len("\n```")]
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def extract_semantic_action(payload: bytes) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentCtlError(
            "Semantic supervisor did not return one JSON action",
            code="semantic_invalid_response",
        ) from exc
    direct = _exact_json_object(text)
    if direct and "selected_command_id" in direct:
        return direct

    chunks: list[str] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
        elif event.get("type") == "text" and isinstance(event.get("text"), str):
            chunks.append(event["text"])
    result = _exact_json_object("".join(chunks))
    if result is None:
        raise AgentCtlError(
            "Semantic supervisor did not return one JSON action",
            code="semantic_invalid_response",
        )
    return result


def dispatch_semantic_supervisor(
    project_path: Path,
    flock_id: str,
    *,
    role: str,
    allow_unsafe_supervisor: bool = False,
) -> dict[str, Any]:
    if role not in {"mother", "top"}:
        raise AgentCtlError(
            "Only mother and top are external semantic supervisors",
            code="invalid_contract",
        )
    _, store, state = _store_for(project_path, flock_id)
    profile_value = read_json(store.root / f"semantic-profile-{role}.json")
    if not isinstance(profile_value, dict):
        raise AgentCtlError("Frozen semantic profile is corrupt", code="corrupt_state")
    if json_hash(profile_value) != state["semantic_profile_sha256"][role]:
        raise AgentCtlError(
            "Frozen semantic profile failed its integrity check",
            code="artifact_tampered",
        )
    profile = _profile_from_value(profile_value)
    expected_name = state["semantic_profiles"][role]
    if profile.name != expected_name:
        raise AgentCtlError(
            "Semantic role resolved to a different frozen profile",
            code="semantic_profile_mismatch",
        )
    if profile.isolation == "unsafe-host" and not allow_unsafe_supervisor:
        raise AgentCtlError(
            "Semantic supervisor has unrestricted host access; rerun only with "
            "explicit --allow-unsafe-supervisor authority",
            code="unsafe_supervisor_requires_opt_in",
        )
    claim_seconds = profile.timeout_seconds + 30
    if claim_seconds > SEMANTIC_CLAIM_MAX_SECONDS:
        raise AgentCtlError(
            "Semantic supervisor timeout exceeds the durable claim limit",
            code="invalid_config",
        )

    pending = claim_semantic_snapshot(
        project_path,
        flock_id,
        role=role,
        claim_seconds=claim_seconds,
    )
    if not pending["pending"]:
        return {"ok": True, "flock_id": flock_id, "role": role, "pending": False}
    claim_id = require_identifier(pending.get("claim_id"), "claim_id")
    try:
        snapshot_payload = Path(pending["snapshot_path"]).read_bytes()
        snapshot = json.loads(snapshot_payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        record_semantic_delivery_failure(
            project_path,
            flock_id,
            pending["snapshot_id"],
            claim_id=claim_id,
            error_code="snapshot_tampered",
        )
        raise AgentCtlError(
            "Semantic snapshot failed its integrity check",
            code="artifact_tampered",
        ) from exc
    if (
        sha256_bytes(snapshot_payload) != pending.get("snapshot_sha256")
        or not isinstance(snapshot, dict)
        or snapshot.get("snapshot_id") != pending.get("snapshot_id")
        or snapshot.get("role") != role
        or pending.get("role") != role
    ):
        record_semantic_delivery_failure(
            project_path,
            flock_id,
            pending["snapshot_id"],
            claim_id=claim_id,
            error_code="snapshot_tampered",
        )
        raise AgentCtlError(
            "Semantic snapshot identity or hash does not match its claim",
            code="artifact_tampered",
        )

    item_root = store.root / "semantic" / pending["snapshot_id"]
    attempt = int(pending.get("delivery_attempts", 0)) + 1
    workspace, request = _prepare_workspace(
        item_root,
        claim_id,
        snapshot,
        pending["snapshot_sha256"],
    )
    events_path = item_root / f"events-{attempt}-{claim_id}.jsonl"
    stderr_path = item_root / f"stderr-{attempt}-{claim_id}.log"
    (item_root / "cancel.request").unlink(missing_ok=True)
    cancel_path = workspace / "cancel.request"
    try:
        result = run_worker(
            profile,
            workspace=workspace,
            request_file=request,
            events_file=events_path,
            stderr_file=stderr_path,
            run_id=flock_id,
            cancel_file=cancel_path,
            timeout_seconds=profile.timeout_seconds,
        )
    except AgentCtlError as exc:
        record_semantic_delivery_failure(
            project_path,
            flock_id,
            pending["snapshot_id"],
            claim_id=claim_id,
            error_code="adapter_error",
        )
        raise AgentCtlError(
            "Semantic supervisor adapter failed",
            code="semantic_adapter_error",
            details={"cause": exc.code},
        ) from exc
    if result.cancelled or result.timed_out or result.output_limited or result.exit_code != 0:
        if result.cancelled:
            error_code = "cancelled"
        elif result.timed_out:
            error_code = "timeout"
        elif result.output_limited:
            error_code = "output_limited"
        else:
            error_code = "nonzero_exit"
        failure = record_semantic_delivery_failure(
            project_path,
            flock_id,
            pending["snapshot_id"],
            claim_id=claim_id,
            error_code=error_code,
        )
        return {
            **failure,
            "role": role,
            "snapshot_id": pending["snapshot_id"],
            "exit_code": result.exit_code,
        }
    try:
        action = extract_semantic_action(events_path.read_bytes())
        return submit_semantic_action(
            project_path, flock_id, action, claim_id=claim_id
        )
    except AgentCtlError as exc:
        record_semantic_delivery_failure(
            project_path,
            flock_id,
            pending["snapshot_id"],
            claim_id=claim_id,
            error_code="invalid_response",
        )
        if exc.code in {
            "invalid_contract",
            "unsupported_schema",
            "semantic_invalid_response",
        }:
            raise AgentCtlError(
                "Semantic supervisor returned an invalid or unbound action",
                code="semantic_invalid_response",
            ) from exc
        raise


def semantic_profile_summary(project_path: Path, flock_id: str) -> dict[str, Any]:
    _, _, state = _store_for(project_path, flock_id)
    return {
        "ok": True,
        "flock_id": flock_id,
        "profiles": state["semantic_profiles"],
        "profile_sha256": state["semantic_profile_sha256"],
        "provider_details_in_snapshot": False,
        "prior_conversation_reused": False,
        "model_fallback": False,
    }
