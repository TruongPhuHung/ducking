from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import AgentCtlError
from .util import (
    read_json,
    require_identifier,
    require_relative_path,
    require_relative_pattern,
)


SUPPORTED_CONTRACT_VERSION = 1
SUPPORTED_PLAN_VERSION = 2
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
PATCH_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DECISIONS = {"accept", "repair", "replan", "human_required"}
SEMANTIC_ROLES = {"mother", "top", "sol"}


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentCtlError(f"{field} must be an object", code="invalid_contract")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentCtlError(
            f"{field} must be a non-empty string", code="invalid_contract"
        )
    return value.strip()


def _require_string_array(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise AgentCtlError(
            f"{field} must be an array of non-empty strings",
            code="invalid_contract",
        )
    return value


def _require_non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AgentCtlError(
            f"{field} must be a non-negative integer", code="invalid_contract"
        )
    return value


def _require_positive_int(value: Any, field: str) -> int:
    result = _require_non_negative_int(value, field)
    if result == 0:
        raise AgentCtlError(
            f"{field} must be greater than zero", code="invalid_contract"
        )
    return result


def _require_sha256(value: Any, field: str) -> str:
    digest = _require_string(value, field)
    if not PATCH_SHA_RE.fullmatch(digest):
        raise AgentCtlError(
            f"{field} must be a lowercase SHA-256 digest",
            code="invalid_contract",
        )
    return digest


def validate_task(value: Any) -> dict[str, Any]:
    task = _require_object(value, "task")
    if task.get("contract_version") != SUPPORTED_CONTRACT_VERSION:
        raise AgentCtlError(
            f"Unsupported task contract version: {task.get('contract_version')}",
            code="unsupported_schema",
        )
    require_identifier(task.get("task_id"), "task_id")
    base_sha = _require_string(task.get("base_sha"), "base_sha")
    if not SHA_RE.fullmatch(base_sha):
        raise AgentCtlError(
            "base_sha must be a 7-64 character hexadecimal Git object ID",
            code="invalid_contract",
        )
    _require_string(task.get("objective"), "objective")

    acceptance = task.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise AgentCtlError(
            "acceptance must contain at least one criterion", code="invalid_contract"
        )
    criterion_ids: set[str] = set()
    for index, criterion_value in enumerate(acceptance):
        criterion = _require_object(criterion_value, f"acceptance[{index}]")
        criterion_id = require_identifier(
            criterion.get("id"), f"acceptance[{index}].id"
        )
        if criterion_id in criterion_ids:
            raise AgentCtlError(
                f"Duplicate acceptance criterion: {criterion_id}",
                code="invalid_contract",
            )
        criterion_ids.add(criterion_id)
        _require_string(criterion.get("claim"), f"acceptance[{index}].claim")
        if criterion.get("proof") not in {"test", "diff", "manual"}:
            raise AgentCtlError(
                f"acceptance[{index}].proof must be test, diff, or manual",
                code="invalid_contract",
            )

    for field in ("context_files", "allowed_paths", "forbidden_paths"):
        items = _require_string_array(task.get(field, []), field)
        if field == "allowed_paths" and not items:
            raise AgentCtlError(
                "allowed_paths must not be empty", code="invalid_contract"
            )
        for index, item in enumerate(items):
            if field == "context_files":
                require_relative_path(item, f"{field}[{index}]")
            else:
                require_relative_pattern(item, f"{field}[{index}]")

    profiles = _require_string_array(
        task.get("validation_profiles"), "validation_profiles"
    )
    if not profiles:
        raise AgentCtlError(
            "validation_profiles must not be empty", code="invalid_contract"
        )
    for profile in profiles:
        require_identifier(profile, "validation_profiles entry")

    budget = _require_object(task.get("budget"), "budget")
    _require_non_negative_int(budget.get("max_fix_rounds"), "budget.max_fix_rounds")
    wall_seconds = _require_non_negative_int(
        budget.get("wall_seconds"), "budget.wall_seconds"
    )
    if wall_seconds == 0:
        raise AgentCtlError(
            "budget.wall_seconds must be greater than zero", code="invalid_contract"
        )
    for optional_field in ("non_goals", "risk_flags"):
        _require_string_array(task.get(optional_field, []), optional_field)
    if "flock_attempt" in task:
        attempt = _require_object(task["flock_attempt"], "flock_attempt")
        fields = {
            "flock_id",
            "lease_id",
            "coordinator_epoch",
            "slot_id",
            "duck_incarnation",
            "contract_sha256",
            "branch_ref",
        }
        if set(attempt) != fields:
            raise AgentCtlError(
                "flock_attempt must contain exactly the lease envelope fields",
                code="invalid_contract",
            )
        require_identifier(attempt["flock_id"], "flock_attempt.flock_id")
        require_identifier(attempt["lease_id"], "flock_attempt.lease_id")
        _require_positive_int(
            attempt["coordinator_epoch"], "flock_attempt.coordinator_epoch"
        )
        slot_id = _require_non_negative_int(
            attempt["slot_id"], "flock_attempt.slot_id"
        )
        if slot_id > 5:
            raise AgentCtlError(
                "flock_attempt.slot_id must be between 0 and 5",
                code="invalid_contract",
            )
        _require_positive_int(
            attempt["duck_incarnation"], "flock_attempt.duck_incarnation"
        )
        _require_sha256(
            attempt["contract_sha256"], "flock_attempt.contract_sha256"
        )
        branch_ref = _require_string(
            attempt["branch_ref"], "flock_attempt.branch_ref"
        )
        if not branch_ref.startswith("ducking/") or any(
            character.isspace() or ord(character) < 32 for character in branch_ref
        ):
            raise AgentCtlError(
                "flock_attempt.branch_ref must be a ducking/ Git ref without whitespace",
                code="invalid_contract",
            )
    return task


def load_task(path: Path) -> dict[str, Any]:
    return validate_task(read_json(path))


def validate_plan(value: Any) -> dict[str, Any]:
    """Validate a frozen multi-unit flock plan.

    The six-duck pool is a runtime invariant. Plans describe atomic work and
    dependencies; they never name providers, model IDs, or process settings.
    """

    plan = _require_object(value, "plan")
    if plan.get("contract_version") != SUPPORTED_PLAN_VERSION:
        raise AgentCtlError(
            f"Unsupported plan contract version: {plan.get('contract_version')}",
            code="unsupported_schema",
        )
    require_identifier(plan.get("plan_id"), "plan_id")
    base_sha = _require_string(plan.get("base_sha"), "base_sha")
    if not SHA_RE.fullmatch(base_sha):
        raise AgentCtlError(
            "base_sha must be a 7-64 character hexadecimal Git object ID",
            code="invalid_contract",
        )
    _require_string(plan.get("goal"), "goal")
    for field in ("assumptions", "non_goals"):
        _require_string_array(plan.get(field, []), field)

    units = plan.get("units")
    if not isinstance(units, list) or not units:
        raise AgentCtlError(
            "units must contain at least one atomic task", code="invalid_contract"
        )
    task_ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, unit_value in enumerate(units):
        unit = validate_task(unit_value)
        task_id = unit["task_id"]
        if task_id in task_ids:
            raise AgentCtlError(
                f"Duplicate plan task: {task_id}", code="invalid_contract"
            )
        task_ids.add(task_id)
        if unit["base_sha"].lower() != base_sha.lower():
            raise AgentCtlError(
                f"units[{index}].base_sha must match plan.base_sha",
                code="invalid_contract",
            )
        depends_on = _require_string_array(
            unit.get("depends_on", []), f"units[{index}].depends_on"
        )
        normalized: list[str] = []
        for dependency in depends_on:
            normalized.append(
                require_identifier(dependency, f"units[{index}].depends_on")
            )
        if len(normalized) != len(set(normalized)):
            raise AgentCtlError(
                f"units[{index}].depends_on contains duplicates",
                code="invalid_contract",
            )
        dependencies[task_id] = normalized

    for task_id, values in dependencies.items():
        for dependency in values:
            if dependency == task_id:
                raise AgentCtlError(
                    f"Task {task_id} cannot depend on itself",
                    code="invalid_contract",
                )
            if dependency not in task_ids:
                raise AgentCtlError(
                    f"Task {task_id} has unknown dependency: {dependency}",
                    code="invalid_contract",
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise AgentCtlError(
                f"Plan dependencies contain a cycle at {task_id}",
                code="invalid_contract",
            )
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in dependencies:
        visit(task_id)

    retry = _require_object(plan.get("retry", {}), "retry")
    max_attempts = _require_positive_int(
        retry.get("max_attempts", 3), "retry.max_attempts"
    )
    if max_attempts > 10:
        raise AgentCtlError(
            "retry.max_attempts must be <= 10", code="invalid_contract"
        )
    delays = retry.get("delays_seconds", [5, 30])
    if not isinstance(delays, list) or not delays:
        raise AgentCtlError(
            "retry.delays_seconds must be a non-empty array",
            code="invalid_contract",
        )
    for index, delay in enumerate(delays):
        _require_non_negative_int(delay, f"retry.delays_seconds[{index}]")

    lease = _require_object(plan.get("lease", {}), "lease")
    liveness_soft = _require_positive_int(
        lease.get("liveness_soft_seconds", 45),
        "lease.liveness_soft_seconds",
    )
    liveness_hard = _require_positive_int(
        lease.get("liveness_hard_seconds", 90),
        "lease.liveness_hard_seconds",
    )
    progress_soft = _require_positive_int(
        lease.get("progress_soft_seconds", 120),
        "lease.progress_soft_seconds",
    )
    progress_hard = _require_positive_int(
        lease.get("progress_hard_seconds", 600),
        "lease.progress_hard_seconds",
    )
    if liveness_soft >= liveness_hard or progress_soft >= progress_hard:
        raise AgentCtlError(
            "Each soft lease threshold must be lower than its hard threshold",
            code="invalid_contract",
        )
    return plan


def load_plan(path: Path) -> dict[str, Any]:
    return validate_plan(read_json(path))


def validate_semantic_action(value: Any) -> dict[str, Any]:
    action = _require_object(value, "semantic action")
    required_fields = {
        "contract_version",
        "snapshot_id",
        "snapshot_sha256",
        "selected_command_id",
        "reason_code",
    }
    if set(action) != required_fields:
        raise AgentCtlError(
            "semantic action must contain exactly the schema-defined fields",
            code="invalid_contract",
        )
    if action.get("contract_version") != SUPPORTED_CONTRACT_VERSION:
        raise AgentCtlError(
            f"Unsupported semantic action version: {action.get('contract_version')}",
            code="unsupported_schema",
        )
    require_identifier(action.get("snapshot_id"), "snapshot_id")
    _require_sha256(action.get("snapshot_sha256"), "snapshot_sha256")
    require_identifier(action.get("selected_command_id"), "selected_command_id")
    require_identifier(action.get("reason_code"), "reason_code")
    return action


def load_semantic_action(path: Path) -> dict[str, Any]:
    return validate_semantic_action(read_json(path))


def validate_review(value: Any) -> dict[str, Any]:
    review = _require_object(value, "review")
    if review.get("contract_version") != SUPPORTED_CONTRACT_VERSION:
        raise AgentCtlError(
            f"Unsupported review contract version: {review.get('contract_version')}",
            code="unsupported_schema",
        )
    require_identifier(review.get("run_id"), "run_id")
    _require_sha256(review.get("patch_sha256"), "patch_sha256")
    _require_sha256(review.get("task_sha256"), "task_sha256")
    _require_sha256(review.get("evidence_sha256"), "evidence_sha256")
    decision = review.get("decision")
    if decision not in DECISIONS:
        raise AgentCtlError(
            f"decision must be one of {sorted(DECISIONS)}", code="invalid_contract"
        )
    criteria = review.get("criteria")
    if not isinstance(criteria, list):
        raise AgentCtlError("criteria must be an array", code="invalid_contract")
    criterion_ids: set[str] = set()
    for index, item_value in enumerate(criteria):
        item = _require_object(item_value, f"criteria[{index}]")
        criterion_id = require_identifier(item.get("id"), f"criteria[{index}].id")
        if criterion_id in criterion_ids:
            raise AgentCtlError(
                f"Duplicate review criterion: {criterion_id}",
                code="invalid_contract",
            )
        criterion_ids.add(criterion_id)
        if item.get("status") not in {"pass", "fail", "unknown"}:
            raise AgentCtlError(
                f"criteria[{index}].status must be pass, fail, or unknown",
                code="invalid_contract",
            )
        _require_string(item.get("evidence"), f"criteria[{index}].evidence")
    findings = review.get("findings", [])
    if not isinstance(findings, list):
        raise AgentCtlError("findings must be an array", code="invalid_contract")
    finding_ids: set[str] = set()
    for index, finding_value in enumerate(findings):
        finding = _require_object(finding_value, f"findings[{index}]")
        finding_id = require_identifier(finding.get("id"), f"findings[{index}].id")
        if finding_id in finding_ids:
            raise AgentCtlError(
                f"Duplicate review finding: {finding_id}",
                code="invalid_contract",
            )
        finding_ids.add(finding_id)
        if finding.get("severity") not in {"blocker", "major", "minor"}:
            raise AgentCtlError(
                f"findings[{index}].severity is invalid", code="invalid_contract"
            )
        _require_string(
            finding.get("failure_mode"), f"findings[{index}].failure_mode"
        )
        _require_string(
            finding.get("required_change"), f"findings[{index}].required_change"
        )
    if decision == "accept":
        if any(item.get("status") != "pass" for item in criteria):
            raise AgentCtlError(
                "accept requires every supplied criterion to pass",
                code="invalid_contract",
            )
        if any(
            item.get("severity") in {"blocker", "major"} for item in findings
        ):
            raise AgentCtlError(
                "accept cannot contain blocker or major findings",
                code="invalid_contract",
            )
    elif not findings:
        raise AgentCtlError(
            f"{decision} requires at least one actionable finding",
            code="invalid_contract",
        )
    if decision == "repair" and not any(
        item.get("severity") in {"blocker", "major"} for item in findings
    ):
        raise AgentCtlError(
            "repair requires at least one blocker or major finding",
            code="invalid_contract",
        )
    return review


def load_review(path: Path) -> dict[str, Any]:
    return validate_review(read_json(path))


def validate_human_approval(value: Any) -> dict[str, Any]:
    approval = _require_object(value, "human approval")
    if approval.get("contract_version") != SUPPORTED_CONTRACT_VERSION:
        raise AgentCtlError(
            f"Unsupported human approval version: {approval.get('contract_version')}",
            code="unsupported_schema",
        )
    require_identifier(approval.get("run_id"), "run_id")
    for field in (
        "patch_sha256",
        "task_sha256",
        "evidence_sha256",
        "review_sha256",
    ):
        _require_sha256(approval.get(field), field)
    require_identifier(approval.get("approved_by"), "approved_by")
    _require_string(approval.get("rationale"), "rationale")
    return approval


def load_human_approval(path: Path) -> dict[str, Any]:
    return validate_human_approval(read_json(path))
