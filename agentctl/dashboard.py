from __future__ import annotations

import json
import secrets
import sys
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .errors import AgentCtlError
from .flock import (
    _parse_time,
    _snapshot_is_current,
    _status_value,
    _store_for,
    cancel_flock,
    flock_sweep,
    flock_tick,
)
from .semantic import dispatch_semantic_supervisor


MAX_REQUEST_BYTES = 4096
EVENT_TAIL_BYTES = 128 * 1024
SSE_INTERVAL_SECONDS = 1.0


def _age_seconds(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    return max(0, int((now - _parse_time(value)).total_seconds()))


def _recent_events(path: Path, *, limit: int = 24) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - EVENT_TAIL_BYTES))
            payload = handle.read()
    except FileNotFoundError:
        return []
    lines = payload.splitlines()
    if size > EVENT_TAIL_BYTES and lines:
        lines = lines[1:]
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def dashboard_snapshot(project_path: Path, flock_id: str) -> dict[str, Any]:
    _, store, _ = _store_for(project_path, flock_id)
    now = datetime.now(UTC)
    with store.lock():
        state = store.load()
        status = _status_value(state)
        lease_by_slot: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        for task in state["tasks"].values():
            lease = task.get("current_lease")
            if lease:
                lease_by_slot[int(lease["slot_id"])] = (task, lease)

        ducks: list[dict[str, Any]] = []
        for duck in state["ducks"]:
            value = {
                "slot_id": duck["slot_id"],
                "incarnation": duck["incarnation"],
                "state": duck["state"],
                "branch_ref": duck.get("branch_ref"),
                "task_id": None,
                "lease_id": duck.get("lease_id"),
                "attempt": None,
                "phase": None,
                "progress_seq": None,
                "heartbeat_age_seconds": None,
                "progress_age_seconds": None,
            }
            active = lease_by_slot.get(int(duck["slot_id"]))
            if active:
                task, lease = active
                value.update(
                    {
                        "task_id": task["task_id"],
                        "attempt": lease["attempt"],
                        "phase": lease.get("phase", "starting"),
                        "progress_seq": lease.get("progress_seq", 0),
                        "heartbeat_age_seconds": _age_seconds(
                            lease.get("last_heartbeat_at"), now
                        ),
                        "progress_age_seconds": _age_seconds(
                            lease.get("last_progress_at"), now
                        ),
                    }
                )
            ducks.append(value)

        tasks = []
        for task in state["tasks"].values():
            lease = task.get("current_lease")
            tasks.append(
                {
                    "task_id": task["task_id"],
                    "state": task["state"],
                    "depends_on": task["depends_on"],
                    "attempts": task["attempts"],
                    "max_attempts": task["max_attempts"],
                    "wall_used_seconds": task["wall_used_seconds"],
                    "wall_budget_seconds": task["wall_budget_seconds"],
                    "retry_authorized": task["retry_authorized"],
                    "reported_to_top": task["reported_to_top"],
                    "available_at": task["available_at"],
                    "slot_id": lease.get("slot_id") if lease else None,
                    "task_eof": task.get("task_eof"),
                }
            )

        pending = [
            {
                "snapshot_id": item["snapshot_id"],
                "role": item["role"],
                "trigger": item["trigger"],
                "subject_kind": item["subject_kind"],
                "subject_id": item.get("subject_id"),
                "delivery_attempts": item["delivery_attempts"],
                "next_delivery_at": item.get("next_delivery_at"),
                "commands": [command["name"] for command in item["commands"]],
            }
            for item in state["outbox"]
            if _snapshot_is_current(state, item)
        ]
        alerts = [
            {
                "id": item["id"],
                "severity": item["severity"],
                "code": item["code"],
                "task_id": item["scope"].get("task_id"),
                "facts": item["facts"],
                "occurred_at": item["occurred_at"],
            }
            for item in state["alerts"][-20:]
        ]
        events = _recent_events(store.events_path)

    return {
        "ok": True,
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "flock": {
            "flock_id": status["flock_id"],
            "plan_id": status["plan_id"],
            "state": status["state"],
            "revision": status["revision"],
            "coordinator_epoch": status["coordinator_epoch"],
            "active_ducks": status["active_ducks"],
            "duck_count": status["duck_count"],
            "task_summary": status["task_summary"],
            "pending_semantic": status["pending_semantic"],
            "eof": status["eof"],
            "created_at": status["created_at"],
            "updated_at": status["updated_at"],
        },
        "ducks": ducks,
        "tasks": tasks,
        "pending": pending,
        "alerts": alerts,
        "events": events,
    }


class DuckingDashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        project_path: Path,
        flock_id: str,
        allow_unsafe_supervisor: bool,
    ) -> None:
        self.project_path = project_path
        self.flock_id = flock_id
        self.allow_unsafe_supervisor = allow_unsafe_supervisor
        self.action_token = secrets.token_urlsafe(32)
        super().__init__(address, DuckingDashboardHandler)


class DuckingDashboardHandler(BaseHTTPRequestHandler):
    server: DuckingDashboardServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *,
        cookie: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"ducking_session={self.server.action_token}; "
                "HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        self._write(
            status,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
        )

    def _has_session_cookie(self) -> bool:
        for part in self.headers.get("Cookie", "").split(";"):
            name, separator, value = part.strip().partition("=")
            if (
                separator
                and name == "ducking_session"
                and secrets.compare_digest(value, self.server.action_token)
            ):
                return True
        return False

    def _valid_host(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return secrets.compare_digest(self.headers.get("Host", ""), expected)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin == f"http://{self.headers.get('Host', '')}"

    def do_GET(self) -> None:
        if not self._valid_host():
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return
        if self.path == "/":
            template = Path(__file__).with_name("dashboard.html").read_text(
                encoding="utf-8"
            )
            page = template.replace(
                "__DUCKING_BOOTSTRAP__",
                json.dumps(
                    {
                        "flockId": self.server.flock_id,
                        "actionToken": self.server.action_token,
                        "allowSupervisor": self.server.allow_unsafe_supervisor,
                    },
                    ensure_ascii=False,
                ).replace("</", "<\\/"),
            ).encode()
            self._write(
                HTTPStatus.OK,
                page,
                "text/html; charset=utf-8",
                cookie=True,
            )
            return
        if self.path == "/api/state":
            self._serve_state()
            return
        if self.path == "/events":
            self._serve_events()
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def _serve_state(self) -> None:
        try:
            snapshot = dashboard_snapshot(
                self.server.project_path, self.server.flock_id
            )
        except AgentCtlError as exc:
            self._json(HTTPStatus.CONFLICT, exc.as_dict())
            return
        self._json(HTTPStatus.OK, snapshot)

    def _serve_events(self) -> None:
        if not self._has_session_cookie():
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for _ in range(300):
                snapshot = dashboard_snapshot(
                    self.server.project_path, self.server.flock_id
                )
                data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(f"event: state\ndata: {data}\n\n".encode())
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, AgentCtlError, OSError):
            return
        finally:
            self.close_connection = True

    def do_POST(self) -> None:
        if not self.path.startswith("/api/actions/"):
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if (
            not self._valid_host()
            or not self._same_origin()
            or self.headers.get("X-Ducking-Token") != self.server.action_token
            or self.headers.get_content_type() != "application/json"
        ):
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "request_too_large"},
            )
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return
        if not isinstance(body, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return

        action = self.path.rsplit("/", 1)[-1]
        try:
            result = self._perform_action(action, body)
            snapshot = dashboard_snapshot(
                self.server.project_path, self.server.flock_id
            )
        except AgentCtlError as exc:
            self._json(HTTPStatus.CONFLICT, exc.as_dict())
            return
        self._json(HTTPStatus.OK, {"ok": True, "result": result, "state": snapshot})

    def _perform_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        project = self.server.project_path
        flock_id = self.server.flock_id
        if action == "tick":
            return flock_tick(project, flock_id)
        if action == "sweep":
            return flock_sweep(project, flock_id)
        if action == "cancel":
            if body.get("confirm") != flock_id:
                raise AgentCtlError(
                    "Cancellation confirmation does not match the flock ID",
                    code="confirmation_required",
                )
            return cancel_flock(project, flock_id)
        if action in {"dispatch-mother", "dispatch-top"}:
            if body.get("confirm_cost") is not True:
                raise AgentCtlError(
                    "Semantic dispatch requires explicit cost confirmation",
                    code="confirmation_required",
                )
            role = action.removeprefix("dispatch-")
            return dispatch_semantic_supervisor(
                project,
                flock_id,
                role=role,
                allow_unsafe_supervisor=self.server.allow_unsafe_supervisor,
            )
        raise AgentCtlError("Unknown dashboard action", code="invalid_command")


def create_dashboard_server(
    project_path: Path,
    flock_id: str,
    *,
    port: int,
    allow_unsafe_supervisor: bool,
) -> DuckingDashboardServer:
    if port < 0 or port > 65535:
        raise AgentCtlError("Dashboard port must be between 0 and 65535", code="invalid_argument")
    dashboard_snapshot(project_path, flock_id)
    try:
        return DuckingDashboardServer(
            ("127.0.0.1", port),
            project_path=project_path.resolve(),
            flock_id=flock_id,
            allow_unsafe_supervisor=allow_unsafe_supervisor,
        )
    except OSError as exc:
        raise AgentCtlError(
            "Dashboard could not bind to the requested local port",
            code="dashboard_bind_failed",
            details={"port": port},
        ) from exc


def serve_flock_dashboard(
    project_path: Path,
    flock_id: str,
    *,
    port: int = 8765,
    allow_unsafe_supervisor: bool = False,
) -> dict[str, Any]:
    server = create_dashboard_server(
        project_path,
        flock_id,
        port=port,
        allow_unsafe_supervisor=allow_unsafe_supervisor,
    )
    actual_port = int(server.server_address[1])
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"Ducking dashboard: {url}", file=sys.stderr, flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"ok": True, "flock_id": flock_id, "url": url, "stopped": True}
