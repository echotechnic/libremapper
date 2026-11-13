from __future__ import annotations

import json
import subprocess
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from libremapper.api import (
    MAX_PREVIEW_REQUEST_BYTES,
    MAX_REQUEST_BYTES,
    SidecarServer,
    build_command,
)


def request(url: str, path: str, token: str | None, payload: dict[str, Any]) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    call = urllib.request.Request(
        url + path,
        json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(call) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_build_command_accepts_only_managed_paths(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    candidate = data_root / "runtime" / "candidate-0123456789abcdef" / "libremapper.yaml"
    output = data_root / "plans" / ("a" * 64 + ".json.0123456789abcdef.tmp")

    command = build_command(
        {
            "operation": "plan",
            "config": str(candidate),
            "map": "campus-core",
            "output": str(output),
            "adopt": True,
            "relayout": False,
        },
        config_root=config_root,
        data_root=data_root,
    )

    assert command == [
        "libremapper",
        "plan",
        "campus-core",
        "--config",
        str(candidate),
        "--output",
        str(output),
        "--adopt",
    ]
    with pytest.raises(ValueError, match="outside"):
        build_command(
            {
                "operation": "apply",
                "config": str(config_root / "libremapper.yaml"),
                "plan": str(tmp_path / "escaped.json"),
            },
            config_root=config_root,
            data_root=data_root,
        )
    with pytest.raises(ValueError, match="safe lowercase"):
        build_command(
            {
                "operation": "plan",
                "config": str(config_root / "libremapper.yaml"),
                "map": "Unsafe Key",
                "output": str(output),
            },
            config_root=config_root,
            data_root=data_root,
        )


def test_build_command_inventory_accepts_no_extra_fields(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    config = config_root / "libremapper.yaml"

    assert build_command(
        {"operation": "inventory", "config": str(config)},
        config_root=config_root,
        data_root=data_root,
    ) == ["libremapper", "inventory", "--config", str(config), "--json"]
    with pytest.raises(ValueError, match="unexpected fields: map"):
        build_command(
            {"operation": "inventory", "config": str(config), "map": "campus-core"},
            config_root=config_root,
            data_root=data_root,
        )


def test_build_command_inspect_accepts_provider_and_optional_map(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    config = config_root / "libremapper.yaml"

    assert build_command(
        {"operation": "inspect", "config": str(config), "provider": "oxidized"},
        config_root=config_root,
        data_root=data_root,
    ) == [
        "libremapper",
        "provider",
        "inspect",
        "oxidized",
        "--config",
        str(config),
        "--json",
    ]
    assert build_command(
        {
            "operation": "inspect",
            "config": str(config),
            "provider": "oxidized",
            "map": "campus-core",
        },
        config_root=config_root,
        data_root=data_root,
    ) == [
        "libremapper",
        "provider",
        "inspect",
        "oxidized",
        "--config",
        str(config),
        "--json",
        "--map",
        "campus-core",
    ]
    with pytest.raises(ValueError, match="safe name"):
        build_command(
            {"operation": "inspect", "config": str(config), "provider": "../oxidized"},
            config_root=config_root,
            data_root=data_root,
        )


def test_sidecar_health_auth_and_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observed_roots = []
    original_run = subprocess.run

    def record_run(*args: Any, **kwargs: Any) -> Any:
        observed_roots.append(kwargs["env"].get("LIBREMAPPER_DATA_ROOT"))
        return original_run(*args, **kwargs)

    monkeypatch.setattr("libremapper.api.subprocess.run", record_run)
    token = "t" * 32
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    config_root.mkdir()
    data_root.mkdir()
    server = SidecarServer(("127.0.0.1", 0), token, config_root, data_root, "/bin/echo", 5)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(url + "/health") as response:
            metadata = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
            assert json.load(response) == {
                "status": "ok", "version": metadata["project"]["version"]
            }

        status, body = request(
            url,
            "/v1/run",
            None,
            {"operation": "validate", "config": str(config_root / "libremapper.yaml")},
        )
        assert status == 401 and body == {"error": "unauthorized"}

        status, body = request(
            url,
            "/v1/run",
            token,
            {"operation": "validate", "config": str(config_root / "libremapper.yaml")},
        )
        assert status == 200
        assert body["exit_code"] == 0
        assert body["stdout"] == f"validate --config {config_root}/libremapper.yaml"
        assert observed_roots == [str(data_root)]

        status, body = request(
            url,
            "/v1/run",
            token,
            {"operation": "inventory", "config": str(config_root / "libremapper.yaml")},
        )
        assert status == 200
        assert body["exit_code"] == 0
        assert body["stdout"] == (f"inventory --config {config_root}/libremapper.yaml --json")

        status, body = request(
            url,
            "/v1/run",
            token,
            {
                "operation": "inspect",
                "config": str(config_root / "libremapper.yaml"),
                "provider": "oxidized",
            },
        )
        assert status == 200
        assert body["exit_code"] == 0
        assert body["stdout"] == (
            f"provider inspect oxidized --config {config_root}/libremapper.yaml --json"
        )

        status, body = request(
            url,
            "/v1/run",
            token,
            {
                "operation": "inventory",
                "config": str(config_root / "libremapper.yaml"),
                "map": "campus-core",
            },
        )
        assert status == 400
        assert body["error"] == "unexpected fields: map"

        status, body = request(url, "/v1/run", token, {"operation": "shell"})
        assert status == 400
        assert body["error"] == "unsupported operation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "operation,suffix",
    [
        ("catalog", ["catalog"]),
        ("test-source", ["test-source", "oxidized"]),
        ("preview", ["preview"]),
        ("parser-files", ["preview"]),
    ],
)
def test_parser_operations_use_fixed_cli_grammar_and_keep_definition_off_argv(
    tmp_path: Path,
    operation: str,
    suffix: list[str],
) -> None:
    config_root, data_root = tmp_path / "config", tmp_path / "data"
    configuration = config_root / "libremapper.yaml"
    payload: dict[str, Any] = {"operation": operation, "config": str(configuration)}
    if operation == "test-source":
        payload["provider"] = "oxidized"
    elif operation in {"preview", "parser-files"}:
        payload.update(
            provider="oxidized",
            parser_id="unsaved",
            definition={"label": "definition-secret"},
            filename="topology/device.cfg",
        )
        payload.update(
            {"sample": "sample-secret --config /etc/passwd"}
            if operation == "preview"
            else {"query": "device"}
        )
    command = build_command(payload, config_root=config_root, data_root=data_root)
    expected = ["libremapper", "provider", *suffix, "--config", str(configuration)]
    if operation == "parser-files":
        expected.append("--list-files")
    assert command == expected
    assert "definition-secret" not in " ".join(command) and "sample-secret" not in " ".join(command)
    with pytest.raises(ValueError, match="unexpected fields: args"):
        build_command({**payload, "args": ["--help"]}, config_root=config_root, data_root=data_root)
    with pytest.raises(ValueError, match="managed validation candidate"):
        build_command(
            {**payload, "config": str(tmp_path / "outside.yaml")},
            config_root=config_root,
            data_root=data_root,
        )


@pytest.mark.parametrize("operation", ["inspect", "test-source"])
@pytest.mark.parametrize("provider", ["../oxidized", "--help", "oxidized;id", "oxidized\n--help"])
def test_parser_diagnostics_reject_provider_option_injection(
    tmp_path: Path,
    operation: str,
    provider: str,
) -> None:
    root = tmp_path / "config"
    with pytest.raises(ValueError, match="safe name"):
        build_command(
            {
                "operation": operation,
                "config": str(root / "libremapper.yaml"),
                "provider": provider,
            },
            config_root=root,
            data_root=tmp_path / "data",
        )


@pytest.mark.parametrize("operation", ["inspect", "validate"])
def test_map_scoped_operations_reject_map_options_and_preserve_structural_boolean(
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "config"
    payload = {"operation": operation, "config": str(root / "libremapper.yaml")}
    if operation == "inspect":
        payload["provider"] = "oxidized"
    with pytest.raises(ValueError, match="invalid map key"):
        build_command({**payload, "map": "--help"}, config_root=root, data_root=tmp_path / "data")
    if operation == "validate":
        command = build_command(
            {**payload, "structural": True, "map": "campus-core"},
            config_root=root,
            data_root=tmp_path / "data",
        )
        assert command[-3:] == ["--structural-only", "--map", "campus-core"]
        with pytest.raises(ValueError, match="boolean"):
            build_command(
                {**payload, "structural": "true"}, config_root=root, data_root=tmp_path / "data"
            )


@pytest.mark.parametrize("operation", ["preview", "parser-files"])
@pytest.mark.parametrize("definition", [None, [], "--help"])
def test_preview_transport_requires_definition_object(
    tmp_path: Path,
    operation: str,
    definition: Any,
) -> None:
    root = tmp_path / "config"
    with pytest.raises(ValueError, match="definition must be an object"):
        build_command(
            {
                "operation": operation,
                "config": str(root / "libremapper.yaml"),
                "definition": definition,
            },
            config_root=root,
            data_root=tmp_path / "data",
        )


def test_sidecar_preview_size_limits_and_stdin_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='{"preview":"ok"}', stderr="")

    monkeypatch.setattr("libremapper.api.subprocess.run", run)
    config_root, data_root = tmp_path / "config", tmp_path / "data"
    config_root.mkdir()
    data_root.mkdir()
    token = "t" * 32
    server = SidecarServer(("127.0.0.1", 0), token, config_root, data_root, "/fixed/libremapper", 5)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    configuration = str(config_root / "libremapper.yaml")
    try:
        preview = {
            "operation": "preview",
            "config": configuration,
            "provider": "oxidized",
            "parser_id": "draft",
            "definition": {"label": "definition-secret"},
            "filename": "topology/local.cfg",
            "sample": "sample-secret --config /etc/passwd\n$(not-executed)"
            + "x" * MAX_REQUEST_BYTES,
        }
        assert MAX_REQUEST_BYTES < len(json.dumps(preview).encode()) < MAX_PREVIEW_REQUEST_BYTES
        status, body = request(url, "/v1/run", token, preview)
        assert status == 200 and body["exit_code"] == 0
        command, kwargs = calls[-1]
        assert command == ["/fixed/libremapper", "provider", "preview", "--config", configuration]
        assert "secret" not in " ".join(command)
        assert json.loads(kwargs["input"]) == preview
        assert kwargs["env"]["LIBREMAPPER_DATA_ROOT"] == str(data_root)
        assert kwargs.get("shell", False) is False

        files = {key: value for key, value in preview.items() if key != "sample"}
        files.update(operation="parser-files", query="device")
        files["definition"] = {"label": "x" * MAX_REQUEST_BYTES}
        status, _ = request(url, "/v1/run", token, files)
        assert status == 200 and calls[-1][0][-1] == "--list-files"
        assert json.loads(calls[-1][1]["input"]) == files
        count = len(calls)
        status, body = request(
            url,
            "/v1/run",
            token,
            {"operation": "catalog", "config": configuration, "padding": "x" * MAX_REQUEST_BYTES},
        )
        assert status == 400 and body == {"error": "invalid request size"}
        oversized = urllib.request.Request(
            url + "/v1/run",
            b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(MAX_PREVIEW_REQUEST_BYTES + 1),
            },
        )
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(oversized)
        assert rejected.value.code == 400
        assert json.load(rejected.value) == {"error": "invalid request size"}
        assert len(calls) == count
        for operation in ("catalog", "test-source"):
            payload = {"operation": operation, "config": configuration}
            if operation == "test-source":
                payload["provider"] = "oxidized"
            status, _ = request(url, "/v1/run", token, payload)
            assert status == 200 and calls[-1][1]["input"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
