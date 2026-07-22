from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import WorkerProfile
from .errors import AgentCtlError
from .util import atomic_write_bytes


BASE_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP")


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    duration_ms: int
    cancelled: bool
    timed_out: bool
    output_limited: bool
    stdout_path: Path
    stderr_path: Path


def _safe_env(profile: WorkerProfile) -> dict[str, str]:
    names = set(BASE_ENV_ALLOW) | set(profile.env_allow)
    env = {name: os.environ[name] for name in names if name in os.environ}
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            entry
            for entry in env["PATH"].split(os.pathsep)
            if entry and os.path.isabs(entry)
        )
    return env


def _redact(payload: bytes, env: dict[str, str], explicit_names: tuple[str, ...]) -> bytes:
    redacted = payload
    for name in explicit_names:
        value = env.get(name)
        if value and len(value) >= 4:
            redacted = redacted.replace(value.encode(), b"***REDACTED***")
    return redacted


def _render(template: str, values: dict[str, str]) -> str:
    try:
        return template.format_map(values)
    except KeyError as exc:
        raise AgentCtlError(
            f"Unknown worker template placeholder: {exc.args[0]}",
            code="invalid_config",
        ) from exc


def _resolve_executable(argv: tuple[str, ...], env: dict[str, str]) -> str | None:
    executable = argv[0]
    if os.path.isabs(executable):
        return executable if os.access(executable, os.X_OK) else None
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return None
    search_path = env.get("PATH", "")
    if not search_path:
        return None
    return shutil.which(executable, path=search_path)


def probe_worker(profile: WorkerProfile) -> dict[str, object]:
    env = _safe_env(profile)
    executable = _resolve_executable(profile.probe_argv, env)
    if not executable:
        return {
            "ok": False,
            "profile": profile.name,
            "runtime_id": profile.runtime_id,
            "isolation": profile.isolation,
            "reason": "executable_not_found",
            "executable": profile.probe_argv[0],
        }
    try:
        process = subprocess.run(
            [executable, *profile.probe_argv[1:]],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "profile": profile.name,
            "runtime_id": profile.runtime_id,
            "isolation": profile.isolation,
            "reason": "probe_failed",
            "error": str(exc),
        }
    output = _redact(
        process.stdout + process.stderr, env, profile.env_allow
    ).decode("utf-8", errors="replace")
    identity_match = profile.probe_contains in output
    ok = process.returncode == 0 and identity_match
    return {
        "ok": ok,
        "profile": profile.name,
        "runtime_id": profile.runtime_id,
        "isolation": profile.isolation,
        "executable": executable,
        "exit_code": process.returncode,
        "identity_match": identity_match,
        "expected_output_fragment": profile.probe_contains,
        "reason": None if ok else "runtime_identity_mismatch",
        "output": output.strip()[:1000],
    }


def run_worker(
    profile: WorkerProfile,
    *,
    workspace: Path,
    request_file: Path,
    events_file: Path,
    stderr_file: Path,
    run_id: str,
    cancel_file: Path,
    timeout_seconds: int,
    on_start: Callable[[int], None] | None = None,
) -> ProcessResult:
    values = {
        "workspace": os.fspath(workspace),
        "request_file": os.fspath(request_file),
        # Never reveal the controller state directory to a worker profile.
        "events_file": os.fspath(workspace / ".agentctl-runtime" / "events.jsonl"),
        "run_id": run_id,
    }
    values["prompt"] = _render(profile.prompt, {**values, "prompt": ""})
    argv = tuple(_render(part, values) for part in profile.argv)
    env = _safe_env(profile)
    executable = _resolve_executable(argv, env)
    if not executable:
        raise AgentCtlError(
            f"Worker executable not found: {argv[0]}", code="worker_unavailable"
        )
    argv = (executable, *argv[1:])
    try:
        return _run_worker_bounded(
            profile,
            argv=argv,
            env=env,
            workspace=workspace,
            events_file=events_file,
            stderr_file=stderr_file,
            cancel_file=cancel_file,
            timeout_seconds=timeout_seconds,
            on_start=on_start,
        )
    except OSError as exc:
        raise AgentCtlError(
            f"Could not start worker process: {exc}", code="worker_start_failed"
        ) from exc


def _run_worker_bounded(
    profile: WorkerProfile,
    *,
    argv: tuple[str, ...],
    env: dict[str, str],
    workspace: Path,
    events_file: Path,
    stderr_file: Path,
    cancel_file: Path,
    timeout_seconds: int,
    on_start: Callable[[int], None] | None,
) -> ProcessResult:
    started = time.monotonic()
    events_file.parent.mkdir(parents=True, exist_ok=True)
    stderr_file.parent.mkdir(parents=True, exist_ok=True)
    raw_stdout = events_file.with_name(f".{events_file.name}.raw")
    raw_stderr = stderr_file.with_name(f".{stderr_file.name}.raw")
    cancelled = False
    timed_out = False
    output_limited = False
    watchdog: subprocess.Popen[bytes] | None = None
    watchdog_write: int | None = None
    try:
        with raw_stdout.open("wb") as stdout_handle, raw_stderr.open("wb") as stderr_handle:
            process = subprocess.Popen(
                list(argv),
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            watchdog_read, watchdog_write = os.pipe()
            try:
                try:
                    watchdog = subprocess.Popen(
                        [
                            sys.executable,
                            os.fspath(Path(__file__).with_name("watchdog.py")),
                            str(watchdog_read),
                            str(process.pid),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        pass_fds=(watchdog_read,),
                        start_new_session=True,
                    )
                except OSError:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
            finally:
                os.close(watchdog_read)
            if on_start:
                on_start(process.pid)
            deadline = started + min(profile.timeout_seconds, timeout_seconds)

            def terminate_group(force: bool = False) -> None:
                selected_signal = signal.SIGKILL if force else signal.SIGTERM
                try:
                    os.killpg(process.pid, selected_signal)
                except (ProcessLookupError, PermissionError):
                    if force:
                        process.kill()
                    else:
                        process.terminate()

            while process.poll() is None:
                if cancel_file.exists():
                    cancelled = True
                if time.monotonic() >= deadline:
                    timed_out = True
                stdout_handle.flush()
                stderr_handle.flush()
                if (
                    raw_stdout.stat().st_size + raw_stderr.stat().st_size
                    > profile.max_output_bytes
                ):
                    output_limited = True
                if not cancelled and not timed_out and not output_limited:
                    time.sleep(0.25)
                    continue
                terminate_group()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    terminate_group(force=True)
                    process.wait()
                break
    except KeyboardInterrupt:
        cancelled = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            process.wait()
    finally:
        if watchdog_write is not None:
            os.close(watchdog_write)
        if watchdog is not None:
            try:
                watchdog.wait(timeout=7)
            except subprocess.TimeoutExpired:
                watchdog.kill()
                watchdog.wait()
        max_bytes = profile.max_output_bytes
        total_size = sum(
            path.stat().st_size for path in (raw_stdout, raw_stderr) if path.exists()
        )
        output_limited = output_limited or total_size > max_bytes
        stdout = raw_stdout.read_bytes()[:max_bytes] if raw_stdout.exists() else b""
        remaining = max(0, max_bytes - len(stdout))
        stderr = raw_stderr.read_bytes()[:remaining] if raw_stderr.exists() else b""
        if output_limited:
            marker = b"\n*** OUTPUT LIMIT EXCEEDED; PROCESS TERMINATED ***\n"
            stderr = (stderr + marker)[:max_bytes]
        atomic_write_bytes(events_file, _redact(stdout, env, profile.env_allow), mode=0o600)
        atomic_write_bytes(stderr_file, _redact(stderr, env, profile.env_allow), mode=0o600)
        raw_stdout.unlink(missing_ok=True)
        raw_stderr.unlink(missing_ok=True)

    return ProcessResult(
        exit_code=process.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
        cancelled=cancelled,
        timed_out=timed_out,
        output_limited=output_limited,
        stdout_path=events_file,
        stderr_path=stderr_file,
    )
