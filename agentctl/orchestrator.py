from __future__ import annotations

import os
import shutil
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import (
    PROJECT_CONFIG_NAME,
    ProjectConfig,
    WorkerProfile,
    default_user_config_path,
    load_project_config,
    load_user_config,
)
from .contracts import load_human_approval, load_review, load_task
from .errors import AgentCtlError
from .gitops import (
    apply_patch,
    capture_diff,
    capture_external_worktree,
    create_independent_clone,
    ensure_commit,
    file_exists_at_commit,
    find_git_root,
    head_sha,
    is_clean,
    read_regular_file_at_commit,
)
from .store import (
    RunStore,
    TERMINAL_STATES,
    find_store_for_project,
    new_run_id,
    project_dispatch_slot,
    project_state_key,
    stores_for_project_root,
)
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    json_hash,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .verifier import build_evidence, evaluate_policy
from .worker import probe_worker, run_worker


def _load_project_and_check_hash(
    project_root: Path, state: dict[str, Any]
) -> ProjectConfig:
    config = load_project_config(project_root)
    actual = sha256_file(config.path)
    if actual != state.get("config_sha256"):
        raise AgentCtlError(
            "Project config changed after this run started",
            code="config_drift",
            details={"expected": state.get("config_sha256"), "actual": actual},
        )
    return config


def _profile_snapshot(profile: WorkerProfile) -> dict[str, Any]:
    return asdict(profile)


def _profile_from_snapshot(value: dict[str, Any]) -> WorkerProfile:
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


def _load_hashed_json(
    path: Path, expected: str | None, *, label: str, canonical: bool
) -> dict[str, Any]:
    value = read_json(path)
    actual = json_hash(value) if canonical else sha256_file(path)
    if not expected or actual != expected:
        raise AgentCtlError(
            f"Frozen {label} artifact failed its integrity check",
            code="artifact_tampered",
            details={"artifact": label, "expected": expected, "actual": actual},
        )
    if not isinstance(value, dict):
        raise AgentCtlError(f"Frozen {label} is not an object", code="corrupt_state")
    return value


def _load_frozen_task(store: RunStore, state: dict[str, Any]) -> dict[str, Any]:
    return _load_hashed_json(
        store.root / "task.json",
        state.get("task_sha256"),
        label="task",
        canonical=True,
    )


def _load_frozen_profile(store: RunStore, state: dict[str, Any]) -> WorkerProfile:
    value = _load_hashed_json(
        store.root / "worker-profile.json",
        state.get("worker_profile_sha256"),
        label="worker profile",
        canonical=True,
    )
    return _profile_from_snapshot(value)


def _load_frozen_patch(store: RunStore, state: dict[str, Any]) -> bytes:
    try:
        patch = (store.root / "patch.diff").read_bytes()
    except FileNotFoundError as exc:
        raise AgentCtlError("Frozen patch is missing", code="artifact_tampered") from exc
    actual = sha256_bytes(patch)
    if actual != state.get("patch_sha256"):
        raise AgentCtlError(
            "Frozen patch artifact failed its integrity check",
            code="artifact_tampered",
            details={"artifact": "patch", "expected": state.get("patch_sha256"), "actual": actual},
        )
    return patch


def _public_repair_context(value: Any) -> Any:
    if isinstance(value, dict):
        blocked = {"log", "stdout", "stderr", "workspace", "project_root"}
        return {
            key: _public_repair_context(item)
            for key, item in value.items()
            if key not in blocked
        }
    if isinstance(value, list):
        return [_public_repair_context(item) for item in value]
    return value


def _prepare_worker_runtime(workspace: Path, request: dict[str, Any]) -> Path:
    runtime_dir = workspace / ".agentctl-runtime"
    try:
        mode = runtime_dir.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            shutil.rmtree(runtime_dir)
        else:
            runtime_dir.unlink()
    runtime_dir.mkdir(mode=0o700)
    worker_request_path = runtime_dir / "request.json"
    atomic_write_json(worker_request_path, request, mode=0o444)
    return worker_request_path


def init_run(
    project_path: Path,
    task_path: Path,
    *,
    user_config_path: Path | None,
    allow_unsafe_worker: bool = False,
) -> dict[str, Any]:
    project_root = find_git_root(project_path)
    project = load_project_config(project_root)
    if not is_clean(project_root):
        raise AgentCtlError(
            "Project worktree must be clean so the frozen base contains all policy and context files",
            code="dirty_worktree",
        )
    task = dict(load_task(task_path))
    full_base_sha = ensure_commit(project_root, task["base_sha"])
    current_head = head_sha(project_root)
    if full_base_sha != current_head:
        raise AgentCtlError(
            "Task base_sha must match the current project HEAD when the run is created",
            code="stale_task",
            details={"task_base_sha": full_base_sha, "current_head": current_head},
        )
    committed_config = read_regular_file_at_commit(
        project_root, full_base_sha, PROJECT_CONFIG_NAME
    )
    if committed_config is None:
        raise AgentCtlError(
            "Project config must be committed in the frozen base",
            code="project_config_not_in_base",
            details={"path": PROJECT_CONFIG_NAME, "base_sha": full_base_sha},
        )
    if committed_config != project.path.read_bytes():
        raise AgentCtlError(
            "Loaded project config does not match the frozen base",
            code="config_drift",
            details={"path": PROJECT_CONFIG_NAME, "base_sha": full_base_sha},
        )
    known_profiles = {
        profile
        for command in project.validation_commands
        for profile in command.profiles
        if profile != "always"
    }
    unknown_profiles = sorted(set(task["validation_profiles"]) - known_profiles)
    if unknown_profiles:
        raise AgentCtlError(
            f"Task requests unknown validation profile: {unknown_profiles[0]}",
            code="unknown_validation_profile",
            details={"known": sorted(known_profiles), "unknown": unknown_profiles},
        )
    required_context = list(
        dict.fromkeys(
            [
                *project.instruction_files,
                *project.context_files,
                *task.get("context_files", []),
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
            details={"paths": missing_context},
        )
    task["base_sha"] = full_base_sha
    task["context_files"] = required_context
    user = load_user_config(user_config_path or default_user_config_path())
    profile = user.worker_profiles.get(project.worker_profile)
    if not profile:
        raise AgentCtlError(
            f"Worker profile does not exist: {project.worker_profile}",
            code="worker_profile_not_found",
        )
    if profile.isolation == "unsafe-host" and not allow_unsafe_worker:
        raise AgentCtlError(
            "This worker profile has unrestricted host access; rerun only with explicit --allow-unsafe-worker authority",
            code="unsafe_worker_requires_opt_in",
        )
    worker_probe = probe_worker(profile)
    if not worker_probe.get("ok"):
        raise AgentCtlError(
            "Worker runtime probe failed",
            code="worker_unavailable",
            details=worker_probe,
        )

    run_id = new_run_id(task["task_id"])
    task_hash = json_hash(task)
    config_hash = sha256_file(project.path)
    profile_value = _profile_snapshot(profile)
    project_key = project_state_key(project_root, project.project_id)
    store = RunStore.create(
        project_key=project_key,
        project_id=project.project_id,
        run_id=run_id,
        project_root=project_root,
        base_sha=full_base_sha,
        config_sha256=config_hash,
        task_sha256=task_hash,
    )
    atomic_write_json(store.root / "task.json", task)
    atomic_write_json(store.root / "worker-profile.json", profile_value)
    store.update_fields(
        worker_profile=profile.name,
        worker_profile_sha256=json_hash(profile_value),
        worker_probe=worker_probe,
        workspace=os.fspath(store.root / "workspace"),
    )
    return {
        "ok": True,
        "run_id": run_id,
        "state": "initialized",
        "run_root": os.fspath(store.root),
        "project_key": project_key,
        "base_sha": full_base_sha,
        "task_sha256": task_hash,
    }


def _store_for(project_path: Path, run_id: str) -> tuple[Path, ProjectConfig, RunStore, dict[str, Any]]:
    project_root = find_git_root(project_path)
    initial_config = load_project_config(project_root)
    project_key = project_state_key(project_root, initial_config.project_id)
    store = RunStore(project_key, run_id)
    state = store.load()
    if Path(state["project_root"]).resolve() != project_root:
        raise AgentCtlError(
            "Run belongs to a different project root", code="project_mismatch"
        )
    config = _load_project_and_check_hash(project_root, state)
    return project_root, config, store, state


def dispatch_unit(project_path: Path, run_id: str) -> dict[str, Any]:
    project_root, config, store, _ = _store_for(project_path, run_id)
    with store.lock(), project_dispatch_slot(store.project_key, config.max_parallel):
        state = store.load()
        if state["state"] not in {"initialized", "verify_failed", "repair_queued"}:
            raise AgentCtlError(
                f"Run is not dispatchable from state {state['state']}",
                code="invalid_state",
            )
        task = _load_frozen_task(store, state)
        profile = _load_frozen_profile(store, state)
        max_repairs = min(
            int(task["budget"]["max_fix_rounds"]), config.max_fix_rounds
        )
        next_attempt = int(state.get("attempt", 0)) + 1
        if next_attempt > 1 + max_repairs:
            raise AgentCtlError(
                "Repair budget is exhausted", code="budget_exhausted"
            )
        workspace = Path(state["workspace"])
        if next_attempt == 1:
            create_independent_clone(project_root, state["base_sha"], workspace)
        elif not workspace.is_dir() or workspace.is_symlink():
            raise AgentCtlError(
                "Repair workspace is missing", code="workspace_missing"
            )
        store.cancel_path.unlink(missing_ok=True)

        repair_context: dict[str, Any] = {}
        for name in ("evidence.json", "review.json"):
            path = store.root / name
            if path.exists():
                repair_context[name.removesuffix(".json")] = _public_repair_context(
                    read_json(path)
                )
        request = {
            "contract_version": 1,
            "run_id": run_id,
            "attempt": next_attempt,
            "task": task,
            "repair_context": repair_context,
            "controller_rules": {
                "worker_may_commit": False,
                "worker_may_push": False,
                "worker_may_merge": False,
                "worker_may_change_task": False,
                "worker_may_access_external_paths": False,
                "required_context_files": task["context_files"],
            },
        }
        request_path = store.root / f"worker-request-{next_attempt}.json"
        atomic_write_json(request_path, request)
        worker_request_path = _prepare_worker_runtime(workspace, request)
        state = store.transition(
            "dispatching",
            actor="controller",
            reason="worker_dispatch_started",
            fields={"attempt": next_attempt, "worker_pid": None},
            artifacts={"request_sha256": json_hash(request)},
        )
        try:
            result = run_worker(
                profile,
                workspace=workspace,
                request_file=worker_request_path,
                events_file=store.root / f"worker-events-{next_attempt}.jsonl",
                stderr_file=store.root / f"worker-stderr-{next_attempt}.log",
                run_id=run_id,
                cancel_file=store.cancel_path,
                timeout_seconds=int(task["budget"]["wall_seconds"]),
                on_start=lambda pid: store.update_fields(worker_pid=pid),
            )
        except AgentCtlError as exc:
            store.update_fields(worker_pid=None)
            store.transition(
                "failed",
                actor="controller",
                reason="worker_adapter_error",
                artifacts={"error_code": exc.code},
            )
            raise
        process_summary = {
            "attempt": next_attempt,
            "runtime_id": profile.runtime_id,
            "isolation": profile.isolation,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "cancelled": result.cancelled,
            "timed_out": result.timed_out,
            "output_limited": result.output_limited,
            "stdout_artifact": result.stdout_path.name,
            "stderr_artifact": result.stderr_path.name,
        }
        atomic_write_json(store.root / f"worker-result-{next_attempt}.json", process_summary)
        store.update_fields(worker_pid=None)
        if result.cancelled:
            store.transition(
                "cancelled", actor="controller", reason="worker_cancelled"
            )
            return {"ok": False, "run_id": run_id, "state": "cancelled"}
        if result.output_limited or result.timed_out or result.exit_code != 0:
            if result.output_limited:
                reason = "worker_output_limit_exceeded"
            else:
                reason = "worker_timed_out" if result.timed_out else "worker_failed"
            store.transition(
                "failed",
                actor="controller",
                reason=reason,
                artifacts={"worker_result": process_summary},
            )
            return {
                "ok": False,
                "run_id": run_id,
                "state": "failed",
                "worker": process_summary,
            }
        if store.cancel_path.exists():
            store.transition(
                "cancelled", actor="controller", reason="cancelled_before_capture"
            )
            return {"ok": False, "run_id": run_id, "state": "cancelled"}
        try:
            metadata_workspace = store.root / f"capture-metadata-{next_attempt}"
            create_independent_clone(
                project_root, state["base_sha"], metadata_workspace
            )
            try:
                snapshot = capture_external_worktree(
                    worker_workspace=workspace,
                    metadata_workspace=metadata_workspace,
                    base_sha=state["base_sha"],
                    max_patch_bytes=config.max_patch_bytes,
                )
            finally:
                shutil.rmtree(metadata_workspace, ignore_errors=True)
        except AgentCtlError as exc:
            store.transition(
                "failed",
                actor="controller",
                reason="candidate_capture_failed",
                artifacts={"error_code": exc.code},
            )
            raise
        if not snapshot.changed_files:
            store.transition(
                "failed", actor="controller", reason="worker_produced_no_patch"
            )
            return {
                "ok": False,
                "run_id": run_id,
                "state": "failed",
                "reason": "worker_produced_no_patch",
            }
        patch_path = store.root / "patch.diff"
        atomic_write_bytes(patch_path, snapshot.patch)
        snapshot_value = {
            "patch_sha256": snapshot.patch_sha256,
            "changed_files": list(snapshot.changed_files),
            "changed_lines": snapshot.changed_lines,
            "binary_files": list(snapshot.binary_files),
            "symlink_files": list(snapshot.symlink_files),
        }
        atomic_write_json(store.root / "diff-snapshot.json", snapshot_value)
        store.transition(
            "candidate",
            actor="controller",
            reason="worker_candidate_captured",
            fields={"patch_sha256": snapshot.patch_sha256},
            artifacts=snapshot_value,
        )
        return {
            "ok": True,
            "run_id": run_id,
            "state": "candidate",
            **snapshot_value,
        }


def verify_unit(
    project_path: Path,
    run_id: str,
    *,
    allow_unsafe_validation: bool = False,
) -> dict[str, Any]:
    project_root, config, store, _ = _store_for(project_path, run_id)
    if config.validation_isolation == "unsafe-host" and not allow_unsafe_validation:
        raise AgentCtlError(
            "Project validators can execute untrusted candidate code on the host; rerun only with explicit --allow-unsafe-validation authority",
            code="unsafe_validation_requires_opt_in",
        )
    with store.lock():
        state = store.load()
        if state["state"] != "candidate":
            raise AgentCtlError(
                f"Run is not verifiable from state {state['state']}",
                code="invalid_state",
            )
        workspace = Path(state["workspace"])
        task = _load_frozen_task(store, state)
        patch = _load_frozen_patch(store, state)
        recapture_metadata = store.root / f"recapture-metadata-{state['attempt']}"
        create_independent_clone(
            project_root, state["base_sha"], recapture_metadata
        )
        try:
            snapshot = capture_external_worktree(
                worker_workspace=workspace,
                metadata_workspace=recapture_metadata,
                base_sha=state["base_sha"],
                max_patch_bytes=config.max_patch_bytes,
            )
        finally:
            shutil.rmtree(recapture_metadata, ignore_errors=True)
        if snapshot.patch_sha256 != state.get("patch_sha256"):
            store.transition(
                "failed", actor="controller", reason="candidate_patch_changed"
            )
            raise AgentCtlError(
                "Candidate workspace changed after patch capture",
                code="patch_drift",
            )
        if snapshot.patch != patch:
            raise AgentCtlError(
                "Candidate patch bytes differ from the frozen artifact",
                code="patch_drift",
            )
        store.transition("verifying", actor="verifier", reason="verification_started")
        evidence_path = store.root / "evidence.json"
        try:
            verifier_workspace = store.root / f"verifier-workspace-{state['attempt']}"
            if verifier_workspace.exists():
                raise AgentCtlError(
                    "Fresh verifier workspace already exists",
                    code="verifier_workspace_exists",
                )
            create_independent_clone(
                project_root, state["base_sha"], verifier_workspace
            )
            try:
                apply_patch(
                    verifier_workspace,
                    patch,
                    base_sha=state["base_sha"],
                    dry_run=False,
                )
            except AgentCtlError:
                evidence = build_evidence(
                    run_id=run_id,
                    task=task,
                    config=config,
                    workspace=workspace,
                    snapshot=snapshot,
                    evidence_path=evidence_path,
                    log_dir=store.root / "verification-logs",
                    execute_commands=False,
                    run_controller_check=False,
                    cancel_file=store.cancel_path,
                )
                evidence["scope_violations"].append(
                    {"path": "*", "reason": "frozen_patch_apply_failed"}
                )
                evidence["passed"] = False
                atomic_write_json(evidence_path, evidence)
            else:
                verifier_snapshot = capture_diff(
                    verifier_workspace,
                    state["base_sha"],
                    max_patch_bytes=config.max_patch_bytes,
                )
                if verifier_snapshot.patch_sha256 != state["patch_sha256"]:
                    raise AgentCtlError(
                        "Fresh verifier workspace did not reproduce the frozen patch",
                        code="patch_drift",
                    )
                policy = evaluate_policy(
                    task=task,
                    config=config,
                    workspace=verifier_workspace,
                    snapshot=verifier_snapshot,
                )
                evidence = build_evidence(
                    run_id=run_id,
                    task=task,
                    config=config,
                    workspace=verifier_workspace,
                    snapshot=verifier_snapshot,
                    evidence_path=evidence_path,
                    log_dir=store.root / "verification-logs",
                    execute_commands=not policy["violations"],
                    cancel_file=store.cancel_path,
                )
                validation_metadata = (
                    store.root / f"validation-metadata-{state['attempt']}"
                )
                create_independent_clone(
                    project_root, state["base_sha"], validation_metadata
                )
                try:
                    after_validation = capture_external_worktree(
                        worker_workspace=verifier_workspace,
                        metadata_workspace=validation_metadata,
                        base_sha=state["base_sha"],
                        max_patch_bytes=config.max_patch_bytes,
                    )
                finally:
                    shutil.rmtree(validation_metadata, ignore_errors=True)
                if after_validation.patch_sha256 != state["patch_sha256"]:
                    evidence["scope_violations"].append(
                        {
                            "path": "*",
                            "reason": "validators_modified_reviewed_files",
                        }
                    )
                    evidence["passed"] = False
                    atomic_write_json(evidence_path, evidence)
        except AgentCtlError as exc:
            store.transition(
                "failed",
                actor="verifier",
                reason="verification_error",
                artifacts={"error_code": exc.code},
            )
            raise
        shutil.rmtree(verifier_workspace, ignore_errors=True)
        if store.cancel_path.exists():
            store.transition(
                "cancelled", actor="verifier", reason="verification_cancelled"
            )
            return {"ok": False, "state": "cancelled", "evidence": evidence}
        target = "ready_for_review" if evidence["passed"] else "verify_failed"
        store.transition(
            target,
            actor="verifier",
            reason="verification_passed" if evidence["passed"] else "verification_failed",
            fields={"evidence_sha256": sha256_file(evidence_path)},
            artifacts={
                "evidence_sha256": sha256_file(evidence_path),
                "patch_sha256": evidence["patch_sha256"],
            },
        )
        return {"ok": evidence["passed"], "state": target, "evidence": evidence}


def create_review_pack(project_path: Path, run_id: str) -> dict[str, Any]:
    _, _, store, state = _store_for(project_path, run_id)
    if state["state"] != "ready_for_review":
        raise AgentCtlError(
            f"Review pack requires ready_for_review, got {state['state']}",
            code="invalid_state",
        )
    task = _load_frozen_task(store, state)
    patch = _load_frozen_patch(store, state)
    evidence = _load_hashed_json(
        store.root / "evidence.json",
        state.get("evidence_sha256"),
        label="evidence",
        canonical=False,
    )
    if evidence.get("patch_sha256") != state["patch_sha256"]:
        raise AgentCtlError(
            "Evidence is not bound to the frozen patch", code="artifact_tampered"
        )
    pack = {
        "contract_version": 1,
        "run_id": run_id,
        "created_at": utc_now(),
        "task_sha256": state["task_sha256"],
        "evidence_sha256": state["evidence_sha256"],
        "task": task,
        "patch_sha256": state["patch_sha256"],
        "patch": patch.decode("utf-8", errors="replace"),
        "evidence": evidence,
        "worker_self_report_trusted": False,
    }
    pack_path = store.root / "review-pack.json"
    atomic_write_json(pack_path, pack)
    return {
        "ok": True,
        "run_id": run_id,
        "review_pack": os.fspath(pack_path),
        "review_pack_sha256": sha256_file(pack_path),
        "patch_sha256": state["patch_sha256"],
    }


def submit_decision(project_path: Path, run_id: str, review_path: Path) -> dict[str, Any]:
    _, config, store, _ = _store_for(project_path, run_id)
    review = load_review(review_path)
    with store.lock():
        state = store.load()
        if state["state"] != "ready_for_review":
            raise AgentCtlError(
                f"Run is not reviewable from state {state['state']}",
                code="invalid_state",
            )
        if review["run_id"] != run_id:
            raise AgentCtlError("Review run_id does not match", code="review_mismatch")
        if review["patch_sha256"] != state.get("patch_sha256"):
            raise AgentCtlError(
                "Review patch hash does not match the candidate",
                code="review_mismatch",
            )
        _load_frozen_patch(store, state)
        task = _load_frozen_task(store, state)
        evidence = _load_hashed_json(
            store.root / "evidence.json",
            state.get("evidence_sha256"),
            label="evidence",
            canonical=False,
        )
        if review["task_sha256"] != state["task_sha256"]:
            raise AgentCtlError("Review task hash does not match", code="review_mismatch")
        if review["evidence_sha256"] != state.get("evidence_sha256"):
            raise AgentCtlError(
                "Review evidence hash does not match", code="review_mismatch"
            )
        if evidence.get("patch_sha256") != state["patch_sha256"]:
            raise AgentCtlError(
                "Evidence patch hash does not match", code="artifact_tampered"
            )
        if not evidence.get("passed"):
            raise AgentCtlError(
                "A review decision requires green deterministic evidence",
                code="evidence_failed",
            )
        expected_criteria = {item["id"] for item in task["acceptance"]}
        actual_criteria = {item["id"] for item in review["criteria"]}
        if review["decision"] == "accept" and actual_criteria != expected_criteria:
            raise AgentCtlError(
                "Accept review must cover every task acceptance criterion exactly once",
                code="review_incomplete",
                details={
                    "expected": sorted(expected_criteria),
                    "actual": sorted(actual_criteria),
                },
            )
        if review["decision"] == "accept" and (
            evidence.get("high_risk_files") or evidence.get("high_risk_flags")
        ):
            raise AgentCtlError(
                "High-risk changes require a human_required decision",
                code="human_gate_required",
                details={
                    "paths": evidence.get("high_risk_files", []),
                    "flags": evidence.get("high_risk_flags", []),
                },
            )
        if review["decision"] == "repair":
            max_repairs = min(
                int(task["budget"]["max_fix_rounds"]), config.max_fix_rounds
            )
            if int(state.get("attempt", 0)) >= 1 + max_repairs:
                raise AgentCtlError(
                    "Review requested repair after the repair budget was exhausted",
                    code="budget_exhausted",
                )
        atomic_write_json(store.root / "review.json", review)
        store.transition("reviewing", actor="sol-reviewer", reason="review_submitted")
        target_by_decision = {
            "accept": "accepted",
            "repair": "repair_queued",
            "replan": "replan_required",
            "human_required": "human_required",
        }
        target = target_by_decision[review["decision"]]
        store.transition(
            target,
            actor="sol-reviewer",
            reason=f"review_{review['decision']}",
            fields={"review_sha256": sha256_file(store.root / "review.json")},
            artifacts={
                "review_sha256": sha256_file(store.root / "review.json"),
                "patch_sha256": review["patch_sha256"],
            },
        )
        if target != "repair_queued":
            shutil.rmtree(Path(state["workspace"]), ignore_errors=True)
        return {"ok": True, "run_id": run_id, "state": target}


def approve_human_decision(
    project_path: Path, run_id: str, approval_path: Path
) -> dict[str, Any]:
    _, _, store, _ = _store_for(project_path, run_id)
    approval = load_human_approval(approval_path)
    with store.lock():
        state = store.load()
        if state["state"] != "human_required":
            raise AgentCtlError(
                f"Human approval requires human_required, got {state['state']}",
                code="invalid_state",
            )
        if approval["run_id"] != run_id:
            raise AgentCtlError("Approval run_id does not match", code="approval_mismatch")
        for field in (
            "patch_sha256",
            "task_sha256",
            "evidence_sha256",
            "review_sha256",
        ):
            if approval[field] != state.get(field):
                raise AgentCtlError(
                    f"Approval {field} does not match the frozen run",
                    code="approval_mismatch",
                )
        _load_frozen_patch(store, state)
        _load_frozen_task(store, state)
        evidence = _load_hashed_json(
            store.root / "evidence.json",
            state.get("evidence_sha256"),
            label="evidence",
            canonical=False,
        )
        review = _load_hashed_json(
            store.root / "review.json",
            state.get("review_sha256"),
            label="review",
            canonical=False,
        )
        if not evidence.get("passed") or review.get("decision") != "human_required":
            raise AgentCtlError(
                "Human approval artifacts are not eligible for acceptance",
                code="approval_mismatch",
            )
        atomic_write_json(store.root / "human-approval.json", approval)
        approval_hash = sha256_file(store.root / "human-approval.json")
        store.transition(
            "accepted",
            actor=f"human:{approval['approved_by']}",
            reason="hash_bound_human_approval",
            fields={"human_approval_sha256": approval_hash},
            artifacts={
                "human_approval_sha256": approval_hash,
                "patch_sha256": approval["patch_sha256"],
            },
        )
        return {"ok": True, "run_id": run_id, "state": "accepted"}


def integrate_run(
    project_path: Path, run_id: str, *, dry_run: bool
) -> dict[str, Any]:
    project_root, _, store, state = _store_for(project_path, run_id)
    with store.lock():
        state = store.load()
        if state["state"] != "accepted":
            raise AgentCtlError(
                f"Only accepted runs can integrate, got {state['state']}",
                code="invalid_state",
            )
        patch = _load_frozen_patch(store, state)
        _load_frozen_task(store, state)
        evidence = _load_hashed_json(
            store.root / "evidence.json",
            state.get("evidence_sha256"),
            label="evidence",
            canonical=False,
        )
        review = _load_hashed_json(
            store.root / "review.json",
            state.get("review_sha256"),
            label="review",
            canonical=False,
        )
        if not evidence.get("passed"):
            raise AgentCtlError("Frozen evidence is no longer green", code="artifact_tampered")
        if not (
            evidence.get("patch_sha256")
            == review.get("patch_sha256")
            == state["patch_sha256"]
        ):
            raise AgentCtlError("Review/evidence patch binding failed", code="artifact_tampered")
        if review.get("decision") == "human_required":
            _load_hashed_json(
                store.root / "human-approval.json",
                state.get("human_approval_sha256"),
                label="human approval",
                canonical=False,
            )
        elif review.get("decision") != "accept":
            raise AgentCtlError("Frozen review is not accepted", code="artifact_tampered")
        apply_patch(project_root, patch, base_sha=state["base_sha"], dry_run=dry_run)
        if not dry_run:
            store.transition(
                "integrated",
                actor="controller",
                reason="patch_applied_to_project_worktree",
            )
    return {
        "ok": True,
        "run_id": run_id,
        "action": "integration_check_passed" if dry_run else "integrated",
        "committed": False,
        "pushed": False,
    }


def cancel_run(project_path: Path, run_id: str) -> dict[str, Any]:
    project_root = find_git_root(project_path)
    store = find_store_for_project(project_root, run_id)
    state = store.load()
    if state["state"] in TERMINAL_STATES:
        raise AgentCtlError(
            f"Run cannot be cancelled from state {state['state']}",
            code="invalid_state",
        )
    if state["state"] in {"dispatching", "verifying"}:
        store.request_cancel()
        return {"ok": True, "run_id": run_id, "action": "cancel_requested"}
    with store.lock():
        state = store.load()
        store.transition("cancelled", actor="user", reason="run_cancelled")
    return {"ok": True, "run_id": run_id, "state": "cancelled"}


def run_status(project_path: Path, run_id: str | None) -> dict[str, Any]:
    project_root = find_git_root(project_path)
    if run_id:
        store = find_store_for_project(project_root, run_id)
        state = store.load()
        return {"ok": True, "run": state, "run_root": os.fspath(store.root)}
    runs = [store.load() for store in stores_for_project_root(project_root)]
    runs.sort(key=lambda value: value.get("created_at", ""), reverse=True)
    return {"ok": True, "runs": runs}
