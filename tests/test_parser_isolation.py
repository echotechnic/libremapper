from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
import typer
from sqlalchemy import Engine
from test_reconciliation import service

from libremapper import cli
from libremapper.config import AppConfig, SourceConfig
from libremapper.models import Plan
from libremapper.parser_definitions import BUILTIN_PARSERS, RegexParserDefinition
from libremapper.providers.file import FileProvider
from libremapper.service import LibremapperService
from libremapper.state import StateStore

LLDP = "saos10-lldp-neighbors"
NNI = "saos10-nni-config"


def config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "providers": {
                "configs": {"type": "oxidized", "settings": {"repository": "/unused.git"}}
            },
            "oxidized_parsers": {
                "unused": BUILTIN_PARSERS[LLDP].model_dump(mode="json"),
            },
            "maps": [
                {"key": "a", "name": "A", "sources": [{"provider": "configs"}]},
                {"key": "b", "name": "B", "sources": [{"provider": "configs", "parsers": [NNI]}]},
                {
                    "key": "disabled",
                    "name": "Disabled",
                    "sources": [{"provider": "configs", "parsers": []}],
                },
            ],
        }
    )


def test_plan_fingerprints_depend_only_on_selected_effective_parser_definitions() -> None:
    settings = config()
    before = {key: settings.plan_fingerprint(key) for key in ("a", "b", "disabled")}
    full_before = settings.fingerprint()
    settings.oxidized_parsers["unused"].label = "Unused definition draft"
    assert settings.fingerprint() != full_before
    assert {key: settings.plan_fingerprint(key) for key in before} == before

    settings.oxidized_parsers[LLDP] = BUILTIN_PARSERS[LLDP].model_copy(deep=True)
    assert settings.plan_fingerprint("a") == before["a"]
    settings.oxidized_parsers[LLDP].label = "Changed enabled parser draft"
    assert settings.plan_fingerprint("a") != before["a"]
    assert settings.plan_fingerprint("b") == before["b"]
    assert settings.plan_fingerprint("disabled") == before["disabled"]

    settings.oxidized_parsers.pop(LLDP)
    assert settings.plan_fingerprint("a") == before["a"]
    settings.oxidized_parsers[NNI] = BUILTIN_PARSERS[NNI].model_copy(
        update={"label": "Changed NNI"}
    )
    assert settings.plan_fingerprint("a") == before["a"]
    assert settings.plan_fingerprint("b") != before["b"]
    assert settings.plan_fingerprint("disabled") == before["disabled"]


@pytest.mark.parametrize("field", ["provider", "other-map-layout", "librenms", "state"])
def test_parser_fingerprint_isolation_preserves_other_configuration_freshness(field: str) -> None:
    settings = config()
    before = settings.plan_fingerprint("a")
    if field == "provider":
        settings.providers["configs"].settings["ref"] = "another-branch"
    elif field == "other-map-layout":
        settings.map_by_key("b").layout.width += 1
    elif field == "librenms":
        settings.librenms.url = "sqlite:///another.sqlite3"
    else:
        settings.state_path = Path("another-state.sqlite3")
    assert settings.plan_fingerprint("a") != before


def mapper_with_file_backed_sources(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> LibremapperService:
    existing, topology = service(tmp_path, engine)
    payload = existing.config.model_dump(mode="json", exclude={"config_path"})
    payload["providers"] = config().model_dump(mode="json")["providers"]
    payload["oxidized_parsers"] = {
        "unused": BUILTIN_PARSERS[LLDP].model_dump(mode="json"),
    }
    first = payload["maps"][0]
    first.update(key="a", name="Parser A", sources=[{"provider": "configs", "parsers": [LLDP]}])
    second = {
        **first,
        "key": "b",
        "name": "Parser B",
        "sources": [{"provider": "configs", "parsers": [NNI]}],
    }
    payload["maps"] = [first, second]
    selected = AppConfig.model_validate(payload)
    mapper = LibremapperService(selected, engine, StateStore(selected.state_path))
    # Replace only source I/O; planning, federation, hashes and native writes remain real.
    monkeypatch.setattr(
        mapper, "_provider", lambda name: FileProvider(name, {"path": str(topology)})
    )
    return mapper


def test_real_saved_plan_remains_publishable_after_unused_parser_edit(
    tmp_path: Path,
    custom_map_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapper = mapper_with_file_backed_sources(tmp_path, custom_map_engine, monkeypatch)
    reviewed = mapper.create_plan("a")
    plan_path = tmp_path / "reviewed.json"
    plan_path.write_text(reviewed.model_dump_json())
    mapper.config.oxidized_parsers["unused"].label = "Edited while A was under review"

    mapper.apply(Plan.model_validate_json(plan_path.read_text()))

    assert mapper.state.map_record("a") is not None
    assert mapper.create_plan("a").changes == 0


def test_real_saved_plan_rejects_enabled_definition_edit_even_with_identical_topology(
    tmp_path: Path,
    custom_map_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapper = mapper_with_file_backed_sources(tmp_path, custom_map_engine, monkeypatch)
    reviewed = Plan.model_validate_json(mapper.create_plan("a").model_dump_json())
    mapper.config.oxidized_parsers[LLDP] = BUILTIN_PARSERS[LLDP].model_copy(
        update={"label": "Changed label, unchanged extracted links"},
        deep=True,
    )
    unchanged = mapper.create_plan("a")
    assert unchanged.source_hash == reviewed.source_hash
    assert unchanged.operations == reviewed.operations
    assert unchanged.config_hash != reviewed.config_hash

    with pytest.raises(RuntimeError, match="configuration changed"):
        mapper.apply(reviewed)

    assert mapper.state.map_record("a") is None


def test_two_maps_can_publish_independently_after_one_parser_definition_changes(
    tmp_path: Path,
    custom_map_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapper = mapper_with_file_backed_sources(tmp_path, custom_map_engine, monkeypatch)
    reviewed_a = Plan.model_validate_json(mapper.create_plan("a").model_dump_json())
    reviewed_b = Plan.model_validate_json(mapper.create_plan("b").model_dump_json())
    mapper.config.oxidized_parsers[LLDP] = BUILTIN_PARSERS[LLDP].model_copy(
        update={"label": "Parser A draft"},
        deep=True,
    )

    mapper.apply(reviewed_b)
    with pytest.raises(RuntimeError, match="configuration changed"):
        mapper.apply(reviewed_a)

    assert mapper.state.map_record("a") is None
    assert mapper.state.map_record("b") is not None
    assert mapper.create_plan("b").changes == 0


def test_new_global_parsers_are_not_enabled_on_existing_maps() -> None:
    settings = config()
    selected_before = {
        str(item.key): settings.effective_parsers(item.sources[0]) for item in settings.maps
    }
    settings.oxidized_parsers["new-custom"] = RegexParserDefinition.model_validate(
        BUILTIN_PARSERS[LLDP].model_dump(),
    )
    assert {
        str(item.key): settings.effective_parsers(item.sources[0]) for item in settings.maps
    } == selected_before
    disabled = settings.map_by_key("disabled").sources[0]
    assert disabled == SourceConfig(provider="configs", parsers=[])


@pytest.mark.parametrize("draft", [False, True])
def test_scheduler_skips_drafts_first_and_isolates_runtime_parser_failures(
    tmp_path: Path,
    custom_map_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    draft: bool,
) -> None:
    mapper = mapper_with_file_backed_sources(tmp_path, custom_map_engine, monkeypatch)
    for map_config in mapper.config.maps:
        map_config.scheduled = True
        mapper.apply(mapper.create_plan(str(map_config.key)))
    if draft:
        marker = mapper._draft_path("a")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("unpublished parser draft")
    original_load = FileProvider.load
    attempts: list[str] = []

    def source_load(provider: FileProvider, source: SourceConfig):
        attempts.extend(source.parsers or [])
        if source.parsers == [LLDP]:
            raise RuntimeError("parser A runtime failure")
        return original_load(provider, source)

    @contextmanager
    def locked_service(_config_path: str):
        yield mapper.config, mapper

    monkeypatch.setattr(FileProvider, "load", source_load)
    monkeypatch.setattr(cli, "_locked_service", locked_service)
    if draft:
        cli.reconcile(None, "unused.yaml")
    else:
        with pytest.raises(typer.Exit):
            cli.reconcile(None, "unused.yaml")
    captured = capsys.readouterr()
    assert "b reconciled (0 change(s))" in captured.out
    if draft:
        assert "a: unpublished draft" in captured.out
        assert LLDP not in attempts
    else:
        assert "a: parser A runtime failure" in captured.err
    assert NNI in attempts
