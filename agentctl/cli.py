from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import AgentCtlError
from .flock import (
    cancel_flock,
    flock_finish,
    flock_heartbeat,
    recover_flock,
    flock_status,
    flock_sweep,
    flock_tick,
    init_flock,
    next_semantic_snapshot,
    submit_semantic_action,
)
from .orchestrator import (
    approve_human_decision,
    cancel_run,
    create_review_pack,
    dispatch_unit,
    init_run,
    integrate_run,
    run_status,
    submit_decision,
    verify_unit,
)
from .project import (
    attach_project,
    detach_project,
    doctor_project,
    inspect_project,
    paths,
)
from .semantic import dispatch_semantic_supervisor, semantic_profile_summary
from .util import read_json


def _add_output_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit compact JSON")


def _add_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, default=Path.cwd())


def _add_user_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-config", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentctl")
    subcommands = parser.add_subparsers(dest="command", required=True)

    version = subcommands.add_parser("version")
    _add_output_flag(version)

    path_command = subcommands.add_parser("paths")
    _add_output_flag(path_command)

    project = subcommands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    inspect = project_commands.add_parser("inspect")
    _add_project(inspect)
    _add_output_flag(inspect)
    attach = project_commands.add_parser("attach")
    _add_project(attach)
    attach.add_argument("--dry-run", action="store_true")
    attach.add_argument("--template", type=Path)
    _add_output_flag(attach)
    detach = project_commands.add_parser("detach")
    _add_project(detach)
    detach.add_argument("--dry-run", action="store_true")
    _add_output_flag(detach)

    doctor = subcommands.add_parser("doctor")
    _add_project(doctor)
    _add_user_config(doctor)
    _add_output_flag(doctor)

    run = subcommands.add_parser("run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    run_init = run_commands.add_parser("init")
    _add_project(run_init)
    _add_user_config(run_init)
    run_init.add_argument("--task", type=Path, required=True)
    run_init.add_argument(
        "--allow-unsafe-worker",
        action="store_true",
        help="Explicitly authorize an unsafe-host worker for this run",
    )
    _add_output_flag(run_init)
    status = run_commands.add_parser("status")
    _add_project(status)
    status.add_argument("--run")
    _add_output_flag(status)
    cancel = run_commands.add_parser("cancel")
    _add_project(cancel)
    cancel.add_argument("--run", required=True)
    _add_output_flag(cancel)

    unit = subcommands.add_parser("unit")
    unit_commands = unit.add_subparsers(dest="unit_command", required=True)
    dispatch = unit_commands.add_parser("dispatch")
    _add_project(dispatch)
    dispatch.add_argument("--run", required=True)
    _add_output_flag(dispatch)
    verify = unit_commands.add_parser("verify")
    _add_project(verify)
    verify.add_argument("--run", required=True)
    verify.add_argument(
        "--allow-unsafe-validation",
        action="store_true",
        help="Explicitly authorize project validators to execute on the host",
    )
    _add_output_flag(verify)

    review = subcommands.add_parser("review")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    pack = review_commands.add_parser("pack")
    _add_project(pack)
    pack.add_argument("--run", required=True)
    _add_output_flag(pack)

    decision = subcommands.add_parser("decision")
    decision_commands = decision.add_subparsers(
        dest="decision_command", required=True
    )
    submit = decision_commands.add_parser("submit")
    _add_project(submit)
    submit.add_argument("--run", required=True)
    submit.add_argument("--file", type=Path, required=True)
    _add_output_flag(submit)
    approve = decision_commands.add_parser("approve-human")
    _add_project(approve)
    approve.add_argument("--run", required=True)
    approve.add_argument("--file", type=Path, required=True)
    _add_output_flag(approve)

    integrate = subcommands.add_parser("integrate")
    _add_project(integrate)
    integrate.add_argument("--run", required=True)
    mode = integrate.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    _add_output_flag(integrate)

    flock = subcommands.add_parser("flock")
    flock_commands = flock.add_subparsers(dest="flock_command", required=True)
    flock_init = flock_commands.add_parser("init")
    _add_project(flock_init)
    _add_user_config(flock_init)
    flock_init.add_argument("--plan", type=Path, required=True)
    flock_init.add_argument(
        "--allow-unsafe-worker",
        action="store_true",
        help="Explicitly authorize the probed child worker profile on the host",
    )
    flock_init.add_argument(
        "--allow-unsafe-supervisor",
        action="store_true",
        help="Explicitly authorize the probed semantic profiles on the host",
    )
    _add_output_flag(flock_init)
    flock_status_parser = flock_commands.add_parser("status")
    _add_project(flock_status_parser)
    flock_status_parser.add_argument("--flock")
    _add_output_flag(flock_status_parser)
    tick = flock_commands.add_parser("tick")
    _add_project(tick)
    tick.add_argument("--flock", required=True)
    _add_output_flag(tick)
    heartbeat = flock_commands.add_parser("heartbeat")
    _add_project(heartbeat)
    heartbeat.add_argument("--flock", required=True)
    heartbeat.add_argument("--slot", type=int, required=True)
    heartbeat.add_argument("--lease", required=True)
    heartbeat.add_argument("--progress-seq", type=int, required=True)
    heartbeat.add_argument("--phase", required=True)
    heartbeat.add_argument("--summary", default="")
    _add_output_flag(heartbeat)
    finish = flock_commands.add_parser("finish")
    _add_project(finish)
    finish.add_argument("--flock", required=True)
    finish.add_argument("--slot", type=int, required=True)
    finish.add_argument("--lease", required=True)
    finish.add_argument(
        "--outcome",
        required=True,
        choices=["succeeded", "retryable_failure", "fatal_failure", "cancelled"],
    )
    finish.add_argument("--reason", required=True)
    finish.add_argument(
        "--run",
        help="Verified child run ID; required only when outcome is succeeded",
    )
    finish.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME=SHA256",
        help="Attach a hash-only failure diagnostic; invalid for succeeded EOF",
    )
    _add_output_flag(finish)
    sweep = flock_commands.add_parser("sweep")
    _add_project(sweep)
    sweep.add_argument("--flock", required=True)
    _add_output_flag(sweep)
    flock_cancel = flock_commands.add_parser("cancel")
    _add_project(flock_cancel)
    flock_cancel.add_argument("--flock", required=True)
    _add_output_flag(flock_cancel)
    recover = flock_commands.add_parser("recover")
    _add_project(recover)
    recover.add_argument("--flock", required=True)
    recover.add_argument("--expected-epoch", type=int, required=True)
    recover.add_argument("--operation-id", required=True)
    recover.add_argument("--reason", default="coordinator_lost")
    _add_output_flag(recover)

    supervisor = subcommands.add_parser("supervisor")
    supervisor_commands = supervisor.add_subparsers(
        dest="supervisor_command", required=True
    )
    supervisor_next = supervisor_commands.add_parser("next")
    _add_project(supervisor_next)
    supervisor_next.add_argument("--flock", required=True)
    supervisor_next.add_argument("--role", required=True, choices=["mother", "top", "sol"])
    _add_output_flag(supervisor_next)
    supervisor_apply = supervisor_commands.add_parser("apply")
    _add_project(supervisor_apply)
    supervisor_apply.add_argument("--flock", required=True)
    supervisor_apply.add_argument("--file", type=Path, required=True)
    _add_output_flag(supervisor_apply)
    supervisor_dispatch = supervisor_commands.add_parser("dispatch")
    _add_project(supervisor_dispatch)
    supervisor_dispatch.add_argument("--flock", required=True)
    supervisor_dispatch.add_argument("--role", required=True, choices=["mother", "top"])
    supervisor_dispatch.add_argument(
        "--allow-unsafe-supervisor",
        action="store_true",
        help="Explicitly authorize a semantic supervisor on the host",
    )
    _add_output_flag(supervisor_dispatch)
    supervisor_profiles = supervisor_commands.add_parser("profiles")
    _add_project(supervisor_profiles)
    supervisor_profiles.add_argument("--flock", required=True)
    _add_output_flag(supervisor_profiles)
    return parser


def _artifacts(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, digest = value.partition("=")
        if not separator or not name or not digest:
            raise AgentCtlError(
                "--artifact must use NAME=SHA256", code="invalid_argument"
            )
        if name in result:
            raise AgentCtlError(
                f"Duplicate artifact name: {name}", code="invalid_argument"
            )
        result[name] = digest
    return result


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "version":
        return {"ok": True, "version": __version__}
    if args.command == "paths":
        return paths()
    if args.command == "project":
        if args.project_command == "inspect":
            return inspect_project(args.project)
        if args.project_command == "attach":
            return attach_project(
                args.project, dry_run=args.dry_run, template_path=args.template
            )
        return detach_project(args.project, dry_run=args.dry_run)
    if args.command == "doctor":
        return doctor_project(args.project, user_config_path=args.user_config)
    if args.command == "run":
        if args.run_command == "init":
            return init_run(
                args.project,
                args.task,
                user_config_path=args.user_config,
                allow_unsafe_worker=args.allow_unsafe_worker,
            )
        if args.run_command == "status":
            return run_status(args.project, args.run)
        return cancel_run(args.project, args.run)
    if args.command == "unit":
        if args.unit_command == "dispatch":
            return dispatch_unit(args.project, args.run)
        return verify_unit(
            args.project,
            args.run,
            allow_unsafe_validation=args.allow_unsafe_validation,
        )
    if args.command == "review":
        return create_review_pack(args.project, args.run)
    if args.command == "decision":
        if args.decision_command == "submit":
            return submit_decision(args.project, args.run, args.file)
        return approve_human_decision(args.project, args.run, args.file)
    if args.command == "integrate":
        return integrate_run(args.project, args.run, dry_run=not args.apply)
    if args.command == "flock":
        if args.flock_command == "init":
            return init_flock(
                args.project,
                args.plan,
                user_config_path=args.user_config,
                allow_unsafe_worker=args.allow_unsafe_worker,
                allow_unsafe_supervisor=args.allow_unsafe_supervisor,
            )
        if args.flock_command == "status":
            return flock_status(args.project, args.flock)
        if args.flock_command == "tick":
            return flock_tick(args.project, args.flock)
        if args.flock_command == "heartbeat":
            return flock_heartbeat(
                args.project,
                args.flock,
                slot_id=args.slot,
                lease_id=args.lease,
                progress_seq=args.progress_seq,
                phase=args.phase,
                summary=args.summary,
            )
        if args.flock_command == "finish":
            return flock_finish(
                args.project,
                args.flock,
                slot_id=args.slot,
                lease_id=args.lease,
                outcome=args.outcome,
                reason=args.reason,
                child_run_id=args.run,
                artifacts=_artifacts(args.artifact),
            )
        if args.flock_command == "sweep":
            return flock_sweep(args.project, args.flock)
        if args.flock_command == "recover":
            return recover_flock(
                args.project,
                args.flock,
                expected_epoch=args.expected_epoch,
                operation_id=args.operation_id,
                reason=args.reason,
            )
        return cancel_flock(args.project, args.flock)
    if args.command == "supervisor":
        if args.supervisor_command == "next":
            return next_semantic_snapshot(
                args.project, args.flock, role=args.role
            )
        if args.supervisor_command == "dispatch":
            return dispatch_semantic_supervisor(
                args.project,
                args.flock,
                role=args.role,
                allow_unsafe_supervisor=args.allow_unsafe_supervisor,
            )
        if args.supervisor_command == "profiles":
            return semantic_profile_summary(args.project, args.flock)
        value = read_json(args.file)
        if not isinstance(value, dict):
            raise AgentCtlError(
                "Semantic action file must contain an object",
                code="invalid_contract",
            )
        return submit_semantic_action(args.project, args.flock, value)
    raise AgentCtlError("Unknown command", code="invalid_command")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    compact = bool(getattr(args, "json", False))
    try:
        result = _dispatch(args)
    except AgentCtlError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, indent=None if compact else 2),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=None if compact else 2))
    return 0 if result.get("ok", False) else 1
