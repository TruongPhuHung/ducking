from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import AgentCtlError
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
    return parser


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
