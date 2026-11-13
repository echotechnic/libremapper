from __future__ import annotations

import json
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from libremapper import cli
from libremapper.inventory import build_inventory
from libremapper.parser_definitions import BUILTIN_PARSERS
from libremapper.providers.oxidized import OxidizedGitProvider


class FakeState:
    def __init__(self, initialized: set[str]) -> None:
        self.initialized = initialized

    def map_record(self, map_key: str) -> dict[str, Any] | None:
        return {"map_key": map_key} if map_key in self.initialized else None


class FakeService:
    def __init__(self, initialized: set[str], plans: dict[str, Any]) -> None:
        self.state = FakeState(initialized)
        self.plans = plans
        self.drafts: set[str] = set()
        self.planned: list[str] = []
        self.applied: list[str] = []
        self.validations: list[tuple[str | None, bool]] = []

    def validate(self, map_key: str | None = None, *, structural: bool = False) -> list[str]:
        self.validations.append((map_key, structural))
        return []

    def create_plan(self, map_key: str, *, relayout: bool = False, adopt: bool = False) -> Any:
        self.planned.append(map_key)
        return self.plans[map_key]

    def has_draft(self, map_key: str) -> bool:
        return map_key in self.drafts

    def apply(self, plan: Any) -> None:
        self.applied.append(plan.map_key)


def test_reconcile_applies_clean_maps_and_aggregates_warnings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    maps = [
        SimpleNamespace(key="warning-map", scheduled=True),
        SimpleNamespace(key="clean-map", scheduled=True),
        SimpleNamespace(key="manual-map", scheduled=False),
    ]
    plans = {
        "warning-map": SimpleNamespace(
            map_key="warning-map", changes=1, warnings=["review"], conflicts=[]
        ),
        "clean-map": SimpleNamespace(map_key="clean-map", changes=1, warnings=[], conflicts=[]),
    }
    service = FakeService({"warning-map", "clean-map", "manual-map"}, plans)

    @contextmanager
    def locked_service(_: str) -> Any:
        yield SimpleNamespace(maps=maps), service

    monkeypatch.setattr(cli, "_locked_service", locked_service)
    with pytest.raises(typer.Exit) as result:
        cli.reconcile(None, "unused.yaml")

    assert result.value.exit_code == 1
    assert service.applied == ["clean-map"]
    assert service.validations == [(None, True)]
    output = capsys.readouterr()
    assert "warning-map: Plan has warnings" in output.err
    assert "clean-map reconciled (1 change(s))" in output.out


def test_reconcile_requires_a_first_manual_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(set(), {})

    @contextmanager
    def locked_service(_: str) -> Any:
        yield SimpleNamespace(maps=[SimpleNamespace(key="scheduled", scheduled=True)]), service

    monkeypatch.setattr(cli, "_locked_service", locked_service)
    with pytest.raises(typer.Exit) as result:
        cli.reconcile("scheduled", "unused.yaml")
    assert result.value.exit_code == 1


@pytest.mark.parametrize("target", [None, "draft-map"])
def test_reconcile_never_plans_or_publishes_an_initialized_map_with_a_draft(
    target: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = FakeService({"draft-map"}, {})
    service.drafts.add("draft-map")

    @contextmanager
    def locked_service(_: str) -> Any:
        yield SimpleNamespace(maps=[SimpleNamespace(key="draft-map", scheduled=True)]), service

    monkeypatch.setattr(cli, "_locked_service", locked_service)

    cli.reconcile(target, "unused.yaml")

    assert service.planned == service.applied == []
    assert "draft-map: unpublished draft" in capsys.readouterr().out


@pytest.mark.parametrize("stale", [False, True])
def test_manual_noop_publish_calls_apply_and_reports_freshness_errors(
    stale: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = SimpleNamespace(map_key="draft-map", changes=0, warnings=[], conflicts=[], operations=[])
    service = FakeService({"draft-map"}, {"draft-map": plan})

    @contextmanager
    def locked_service(_: str) -> Any:
        yield SimpleNamespace(), service

    monkeypatch.setattr(cli, "_locked_service", locked_service)
    if stale:

        def reject_stale(_: Any) -> None:
            raise RuntimeError("configuration changed after this plan was generated")

        monkeypatch.setattr(service, "apply", reject_stale)
        with pytest.raises(typer.Exit) as result:
            cli.apply("draft-map", "unused.yaml", False, False, False, True)
        assert result.value.exit_code == 1
        assert "configuration changed" in capsys.readouterr().err
    else:
        cli.apply("draft-map", "unused.yaml", False, False, False, True)
        assert service.applied == ["draft-map"]


def test_locked_service_rejects_a_concurrent_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "libremapper.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "state_path": str(tmp_path / "state.sqlite3"),
                "providers": {},
                "maps": [],
            }
        )
    )
    lock_path = tmp_path / "action.lock"
    monkeypatch.setenv("LIBREMAPPER_LOCK_PATH", str(lock_path))
    with lock_path.open("a+b") as lock:
        flock(lock.fileno(), LOCK_EX | LOCK_NB)
        try:
            with pytest.raises(RuntimeError, match="already running"):
                with cli._locked_service(str(config)):
                    pass
        finally:
            flock(lock.fileno(), LOCK_UN)


def test_inventory_includes_device_attributes_groups_and_active_links(
    custom_map_engine: Engine,
) -> None:
    with custom_map_engine.begin() as connection:
        for statement in (
            "CREATE TABLE locations (id INTEGER PRIMARY KEY, location TEXT)",
            "CREATE TABLE devices (device_id INTEGER PRIMARY KEY, hostname TEXT, sysName TEXT, "
            "location_id INTEGER, os TEXT, hardware TEXT, type TEXT)",
            "CREATE TABLE device_groups (id INTEGER PRIMARY KEY, name TEXT)",
            "CREATE TABLE device_group_device (device_id INTEGER, device_group_id INTEGER)",
            "CREATE TABLE ports (port_id INTEGER PRIMARY KEY)",
            "CREATE TABLE links "
            "(local_device_id INTEGER, remote_device_id INTEGER, active INTEGER)",
            "INSERT INTO locations VALUES (1, 'London'), (2, 'Empty')",
            "INSERT INTO devices VALUES "
            "(2, 'edge.example.test', 'Edge', NULL, NULL, NULL, 'network'), "
            "(1, 'core.example.test', 'Core', 1, 'ios', 'ASR', 'network')",
            "INSERT INTO device_groups VALUES (3, 'Core'), (4, 'Empty'), (5, 'All')",
            "INSERT INTO device_group_device VALUES (1, 3), (1, 5), (2, 5)",
            "INSERT INTO links VALUES (1, 2, 1), (1, 2, 0), (2, 2, 1), (1, NULL, 1)",
        ):
            connection.exec_driver_sql(statement)

    report = build_inventory(custom_map_engine)

    assert report["devices"] == [
        {
            "device_id": 1,
            "hostname": "core.example.test",
            "sys_name": "Core",
            "location": "London",
            "groups": ["5", "All", "3", "Core"],
            "os": "ios",
            "hardware": "ASR",
            "type": "network",
            "discovered_link_count": 2,
        },
        {
            "device_id": 2,
            "hostname": "edge.example.test",
            "sys_name": "Edge",
            "location": None,
            "groups": ["5", "All"],
            "os": None,
            "hardware": None,
            "type": "network",
            "discovered_link_count": 2,
        },
    ]
    assert report["counts"] == {"devices": 2, "ports": 0, "links": 4}
    assert report["groups"] == [
        {"id": 5, "name": "All", "device_count": 2},
        {"id": 3, "name": "Core", "device_count": 1},
        {"id": 4, "name": "Empty", "device_count": 0},
    ]
    assert report["locations"] == [
        {"id": 2, "location": "Empty", "device_count": 0},
        {"id": 1, "location": "London", "device_count": 1},
    ]
    assert all(report["schema"].values())


def test_inventory_handles_missing_optional_device_columns(custom_map_engine: Engine) -> None:
    assert build_inventory(custom_map_engine)["devices"] == []
    with custom_map_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE devices (device_id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO devices VALUES (7)")
        connection.exec_driver_sql("CREATE TABLE locations (id INTEGER PRIMARY KEY, location TEXT)")
        connection.exec_driver_sql("CREATE TABLE links (id INTEGER PRIMARY KEY)")

    report = build_inventory(custom_map_engine)

    assert report["devices"] == [
        {
            "device_id": 7,
            "hostname": None,
            "sys_name": None,
            "location": None,
            "groups": [],
            "os": None,
            "hardware": None,
            "type": None,
            "discovered_link_count": 0,
        }
    ]
    assert report["counts"] == {"devices": 1, "links": 0}
    assert report["locations"] == []


def preview_configuration(tmp_path: Path) -> Path:
    path = tmp_path / "libremapper.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "state_path": str(tmp_path / "state.sqlite3"),
                "providers": {},
                "maps": [],
            }
        )
    )
    return path


@pytest.mark.parametrize("list_files", [False, True])
def test_cli_preview_reads_stdin_without_state_locks_or_draft_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    list_files: bool,
) -> None:
    configuration = preview_configuration(tmp_path)
    draft = tmp_path / "existing.pending"
    draft.write_text("unpublished")
    disposed: list[bool] = []
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    captured: list[tuple[Any, dict[str, Any], bool]] = []
    preview = {
        "parser_id": "draft",
        "definition": {"label": "definition-secret"},
        "filename": "configs/device.cfg",
        "sample": "sample-secret",
    }

    def forbidden(*_args: Any, **_kwargs: Any):
        pytest.fail("preview must not construct map services, state, or locks")

    def execute(config: Any, observed_engine: Any, payload: dict[str, Any], *, list_files: bool):
        assert observed_engine is engine
        captured.append((config.config_path, payload, list_files))
        return {"preview": "ok"}

    monkeypatch.setattr(cli, "StateStore", forbidden)
    monkeypatch.setattr(cli, "LibremapperService", forbidden)
    monkeypatch.setattr(cli, "_service", forbidden)
    monkeypatch.setattr(cli, "_locked_service", forbidden)
    monkeypatch.setattr(cli, "create_librenms_engine", lambda _config: engine)
    monkeypatch.setattr(cli, "parser_preview", execute)
    command = ["provider", "preview", "--config", str(configuration)]
    if list_files:
        command.append("--list-files")
    result = CliRunner().invoke(cli.app, command, input=json.dumps(preview))
    assert result.exit_code == 0 and json.loads(result.stdout) == {"preview": "ok"}
    assert captured == [(configuration.resolve(), preview, list_files)]
    assert disposed == [True]
    assert draft.read_text() == "unpublished"
    assert not (tmp_path / "state.sqlite3").exists() and not (tmp_path / "action.lock").exists()
    assert "secret" not in result.stdout


@pytest.mark.parametrize(
    "bad_input",
    ["malformed-json", "non-object", "invalid-regex", "unsafe-path", "oversized-unicode"],
)
def test_cli_preview_errors_never_echo_pasted_samples_or_definition_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_input: str,
) -> None:
    configuration = preview_configuration(tmp_path)
    disposed: list[bool] = []
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    monkeypatch.setattr(cli, "create_librenms_engine", lambda _config: engine)
    preview = {
        "parser_id": "draft",
        "definition": BUILTIN_PARSERS["saos10-lldp-neighbors"].model_dump(mode="json"),
        "filename": "topology/local--lldp-neighbors",
        "sample": "sample-secret-do-not-expose",
    }
    if bad_input == "malformed-json":
        raw = '{"sample":"sample-secret-do-not-expose",'
    elif bad_input == "non-object":
        raw = '["sample-secret-do-not-expose"]'
    elif bad_input == "invalid-regex":
        preview["definition"]["rules"][0]["regex"] = "[definition-secret-do-not-expose"
        raw = json.dumps(preview)
    elif bad_input == "unsafe-path":
        preview["filename"] = "../escaped"
        raw = json.dumps(preview)
    else:
        raw = json.dumps(
            {"sample": "sample-secret-do-not-expose" + "é" * 600000}, ensure_ascii=False
        )
    result = CliRunner().invoke(
        cli.app, ["provider", "preview", "--config", str(configuration)], input=raw
    )
    assert result.exit_code == 1
    assert "Invalid preview input or parser settings" in result.stderr
    assert "secret-do-not-expose" not in result.stdout + result.stderr
    assert not (tmp_path / "state.sqlite3").exists()
    assert disposed == ([True] if bad_input in {"invalid-regex", "unsafe-path"} else [])


def test_cli_catalog_does_not_create_state_database_or_fetch_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = preview_configuration(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any):
        pytest.fail("catalog must not access map state, inventory, or Git")

    monkeypatch.setattr(cli, "create_librenms_engine", forbidden)
    monkeypatch.setattr(cli, "StateStore", forbidden)
    monkeypatch.setattr(cli, "_service", forbidden)
    result = CliRunner().invoke(cli.app, ["provider", "catalog", "--config", str(configuration)])
    assert result.exit_code == 0
    catalog = json.loads(result.stdout)
    assert {item["id"] for item in catalog["items"]} == set(BUILTIN_PARSERS)
    assert catalog["selections"] == {}
    assert not (tmp_path / "state.sqlite3").exists()


def test_cli_source_connection_test_runs_with_all_parsers_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = preview_configuration(tmp_path)
    data = yaml.safe_load(configuration.read_text())
    data["providers"] = {
        "oxidized": {
            "type": "oxidized",
            "settings": {"repository": str(tmp_path / "unused.git")},
        }
    }
    data["maps"] = [
        {
            "key": "disabled",
            "name": "Disabled",
            "sources": [{"provider": "oxidized", "parsers": []}],
        }
    ]
    configuration.write_text(yaml.safe_dump(data))
    checks: list[list[str]] = []
    disposed: list[bool] = []
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))

    def inspect_forbidden(*_args: Any, **_kwargs: Any):
        pytest.fail("connection test must not parse artifacts or construct state")

    def test_connection(provider: OxidizedGitProvider):
        checks.append(provider.settings.parsers)
        return {"status": "ok", "revision": "a" * 40}

    monkeypatch.setattr(cli, "create_librenms_engine", lambda _config: engine)
    monkeypatch.setattr("libremapper.database.create_librenms_engine", lambda _config: engine)
    monkeypatch.setattr(cli, "StateStore", inspect_forbidden)
    monkeypatch.setattr(OxidizedGitProvider, "inspect", inspect_forbidden)
    monkeypatch.setattr(OxidizedGitProvider, "test_connection", test_connection)
    result = CliRunner().invoke(
        cli.app, ["provider", "test-source", "oxidized", "--config", str(configuration)]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "ok"
    assert checks == [["saos10-lldp-neighbors"]]
    assert disposed == [True] and not (tmp_path / "state.sqlite3").exists()


def test_cli_inspect_passes_optional_map_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from libremapper.models import ProviderInspection, Topology

    observed: list[tuple[str, str | None]] = []

    def inspect(name: str, map_key: str | None):
        observed.append((name, map_key))
        return ProviderInspection(
            provider=name, provider_type="oxidized", topology=Topology(provider=name)
        )

    monkeypatch.setattr(
        cli, "_service", lambda _config: (None, SimpleNamespace(inspect_provider=inspect))
    )
    for options, expected in (([], None), (["--map", "campus-core"], "campus-core")):
        result = CliRunner().invoke(
            cli.app, ["provider", "inspect", "oxidized", "--json", *options]
        )
        assert result.exit_code == 0 and json.loads(result.stdout)["provider"] == "oxidized"
        assert observed[-1] == ("oxidized", expected)


@pytest.mark.parametrize(
    "options,expected",
    [
        ([], (None, False)),
        (["--map", "campus-core", "--structural-only"], ("campus-core", True)),
    ],
)
def test_cli_validate_passes_map_and_structural_mode(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    expected: tuple[str | None, bool],
) -> None:
    service = FakeService(set(), {})

    @contextmanager
    def locked_service(_path: str):
        yield None, service

    monkeypatch.setattr(cli, "_locked_service", locked_service)
    result = CliRunner().invoke(cli.app, ["validate", "--config", "unused.yaml", *options])
    assert result.exit_code == 0
    assert service.validations == [expected]
