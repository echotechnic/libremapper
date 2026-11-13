from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from libremapper import __version__
from libremapper.runtime import drop_privileges

MAX_REQUEST_BYTES = 64 * 1024
MAX_PREVIEW_REQUEST_BYTES = 1024 * 1024
SAFE_MAP_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SAFE_PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
CANDIDATE_DIRECTORY = re.compile(r"candidate-[0-9a-f]{16}")
REVIEWED_PLAN = re.compile(r"[0-9a-f]{64}\.json")
TEMPORARY_PLAN = re.compile(r"[0-9a-f]{64}\.json\.[0-9a-f]{16}\.tmp")


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _contained_path(value: str, root: Path) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path is outside {root}")
    return path


def _config_path(value: str, config_root: Path, data_root: Path) -> Path:
    path = Path(value).resolve()
    if path == (config_root / "libremapper.yaml").resolve():
        return path
    runtime = (data_root / "runtime").resolve()
    if (
        path.name == "libremapper.yaml"
        and path.parent.parent == runtime
        and CANDIDATE_DIRECTORY.fullmatch(path.parent.name)
    ):
        return path
    raise ValueError("config must be the active file or a managed validation candidate")


def _plan_path(value: str, data_root: Path, temporary: bool) -> Path:
    path = _contained_path(value, data_root / "plans")
    pattern = TEMPORARY_PLAN if temporary else REVIEWED_PLAN
    if path.parent != (data_root / "plans").resolve() or pattern.fullmatch(path.name) is None:
        raise ValueError("plan path is not managed by Libremapper")
    return path


def build_command(
    payload: dict[str, Any],
    *,
    config_root: Path,
    data_root: Path,
    binary: str = "libremapper",
) -> list[str]:
    """Translate the authenticated API shape into the fixed CLI grammar."""
    operation = _string(payload, "operation")
    allowed = {
        "validate": {"operation", "config", "map", "structural"},
        "inventory": {"operation", "config"},
        "inspect": {"operation", "config", "provider", "map"},
        "catalog": {"operation", "config"},
        "test-source": {"operation", "config", "provider"},
        "preview": {
            "operation",
            "config",
            "provider",
            "parser_id",
            "definition",
            "filename",
            "sample",
        },
        "parser-files": {
            "operation",
            "config",
            "provider",
            "parser_id",
            "definition",
            "filename",
            "query",
        },
        "plan": {"operation", "config", "map", "output", "adopt", "relayout"},
        "apply": {"operation", "config", "plan"},
    }.get(operation)
    if allowed is None:
        raise ValueError("unsupported operation")
    unexpected = set(payload) - allowed
    if unexpected:
        raise ValueError(f"unexpected fields: {', '.join(sorted(unexpected))}")

    config = _config_path(_string(payload, "config"), config_root, data_root)
    if operation == "validate":
        command = [binary, "validate", "--config", str(config)]
        if "structural" in payload:
            if not isinstance(payload["structural"], bool):
                raise ValueError("structural must be a boolean")
            if payload["structural"]:
                command.append("--structural-only")
        if "map" in payload:
            key = _string(payload, "map")
            if SAFE_MAP_KEY.fullmatch(key) is None:
                raise ValueError("invalid map key")
            command.extend(["--map", key])
        return command
    if operation == "catalog":
        return [binary, "provider", "catalog", "--config", str(config)]
    if operation in {"preview", "parser-files"}:
        if not isinstance(payload.get("definition"), dict):
            raise ValueError("definition must be an object")
        command = [binary, "provider", "preview", "--config", str(config)]
        if operation == "parser-files":
            command.append("--list-files")
        return command
    if operation == "inventory":
        return [binary, "inventory", "--config", str(config), "--json"]
    if operation in {"inspect", "test-source"}:
        provider = _string(payload, "provider")
        if SAFE_PROVIDER.fullmatch(provider) is None:
            raise ValueError("provider must be a safe name")
        command = [
            binary,
            "provider",
            "inspect" if operation == "inspect" else "test-source",
            provider,
            "--config",
            str(config),
        ]
        if operation == "inspect":
            command.append("--json")
            if "map" in payload:
                key = _string(payload, "map")
                if SAFE_MAP_KEY.fullmatch(key) is None:
                    raise ValueError("invalid map key")
                command.extend(["--map", key])
        return command
    if operation == "apply":
        plan = _plan_path(_string(payload, "plan"), data_root, False)
        return [binary, "apply", str(plan), "--config", str(config), "--yes"]

    map_key = _string(payload, "map")
    if SAFE_MAP_KEY.fullmatch(map_key) is None:
        raise ValueError("map must be a safe lowercase key")
    output = _plan_path(_string(payload, "output"), data_root, True)
    command = [binary, "plan", map_key, "--config", str(config), "--output", str(output)]
    for field, flag in (("adopt", "--adopt"), ("relayout", "--relayout")):
        value = payload.get(field, False)
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be a boolean")
        if value:
            command.append(flag)
    return command


class SidecarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        config_root: Path,
        data_root: Path,
        binary: str,
        timeout: int,
    ) -> None:
        super().__init__(address, SidecarHandler)
        self.token = token
        self.config_root = config_root
        self.data_root = data_root
        self.binary = binary
        self.timeout = timeout


class SidecarHandler(BaseHTTPRequestHandler):
    server_version = "Libremapper"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._json(HTTPStatus.OK, {"status": "ok", "version": __version__})

    def do_POST(self) -> None:
        server = cast(SidecarServer, self.server)
        if self.path != "/v1/run":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {server.token}"
        if not hmac.compare_digest(authorization, expected):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_PREVIEW_REQUEST_BYTES:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid request size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            if length > MAX_REQUEST_BYTES and payload.get("operation") not in {
                "preview",
                "parser-files",
            }:
                raise ValueError("invalid request size")
            command = build_command(
                payload,
                config_root=server.config_root,
                data_root=server.data_root,
                binary=server.binary,
            )
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                input=json.dumps(payload)
                if payload.get("operation") in {"preview", "parser-files"}
                else None,
                timeout=server.timeout,
                check=False,
                env={
                    **os.environ,
                    "LIBREMAPPER_LOCK_HELD": "1",
                    "LIBREMAPPER_DATA_ROOT": str(server.data_root),
                },
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except subprocess.TimeoutExpired:
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "Libremapper operation timed out"})
            return
        except OSError as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        self._json(
            HTTPStatus.OK,
            {
                "exit_code": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    drop_privileges()
    token = os.getenv("LIBREMAPPER_API_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("LIBREMAPPER_API_TOKEN must contain at least 32 characters")
    host = os.getenv("LIBREMAPPER_API_HOST", "127.0.0.1")
    port = int(os.getenv("LIBREMAPPER_API_PORT", "8001"))
    data_root = Path(os.getenv("LIBREMAPPER_DATA_ROOT", "/data/libremapper"))
    token_path = data_root / "api-token"
    temporary = token_path.with_name(f".{token_path.name}.{os.getpid()}.tmp")
    temporary.write_text(token)
    temporary.chmod(0o600)
    temporary.replace(token_path)
    server = SidecarServer(
        (host, port),
        token,
        Path(os.getenv("LIBREMAPPER_CONFIG_ROOT", "/config")),
        data_root,
        os.getenv("LIBREMAPPER_BINARY", "libremapper"),
        int(os.getenv("LIBREMAPPER_OPERATION_TIMEOUT", "300")),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
