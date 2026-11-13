from __future__ import annotations

import importlib
import json
import multiprocessing
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from test_oxidized_git import git, repository
from test_oxidized_git import identity_engine as identity_engine

from libremapper.config import AppConfig
from libremapper.models import Device, Interface, Link, LinkEndpoint, ProviderInspection, Topology
from libremapper.parser_definitions import BUILTIN_PARSERS, MAX_PREVIEW_BYTES
from libremapper.parser_execution import execute_parser
from libremapper.parser_preview import FIELD_LIMIT, _result, parser_preview
from libremapper.providers.oxidized import GitTopologySnapshot, OxidizedGitProvider

preview_module = importlib.import_module("libremapper.parser_preview")
PARSER = "saos10-lldp-neighbors"
FILENAME = "topology/local.example.net--lldp-neighbors"
SAMPLE = "\n".join(
    [
        "| interface | Ethernet1 |",
        "| chassis-id | 00aabbccddee |",
        "| port-id | Ethernet9 |",
        "| system-name | known |",
    ]
)


def payload(**changes: Any) -> dict[str, Any]:
    return {
        "parser_id": PARSER,
        "definition": BUILTIN_PARSERS[PARSER].model_dump(mode="json"),
        "filename": FILENAME,
        **changes,
    }


def config(repository_path: Path | None = None) -> AppConfig:
    providers = (
        {"oxidized": {"type": "oxidized", "settings": {"repository": str(repository_path)}}}
        if repository_path
        else {}
    )
    return AppConfig.model_validate({"providers": providers, "maps": []})


def test_pasted_unsaved_preview_works_without_git_and_preserves_configuration_and_drafts(
    tmp_path: Path,
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(cache))
    selected = config()
    configuration = tmp_path / "config.yaml"
    configuration.write_text("unchanged configuration")
    selected.config_path = configuration
    draft = tmp_path / "draft.pending"
    draft.write_text("unpublished draft")
    plan = tmp_path / "reviewed.json"
    plan.write_text("saved plan")
    before = selected.model_dump()
    artifacts = {path: path.read_bytes() for path in (configuration, draft, plan)}
    sample_payload = payload(sample=SAMPLE + "\ncredential secret-do-not-expose")
    sample_payload["definition"]["label"] = "Unsaved label"
    sample_payload["definition"]["protocol"] = "site-neighbor"
    roots: list[Path] = []

    def execute(*args: Any, **kwargs: Any):
        root = args[3]
        roots.append(root)
        assert (root / FILENAME).stat().st_mode & 0o777 == 0o600
        assert kwargs == {"preview": True}
        return execute_parser(*args, **kwargs)

    monkeypatch.setattr(preview_module, "execute_parser", execute)
    result = parser_preview(selected, identity_engine, sample_payload)

    assert result["revision"] is None and not result["blocked"]
    assert result["artifact_count"] == 1 and result["connection_count"] == 1
    assert result["device_count"] == 2 and not result["truncated"]
    assert result["links"] == [
        {
            "a": {
                "hostname": "local.example.net",
                "device_id": 1,
                "interface": "Ethernet1",
                "port_id": 11,
            },
            "b": {
                "hostname": "known.example.net",
                "device_id": 2,
                "interface": "Ethernet9",
                "port_id": 21,
            },
            "protocol": "site-neighbor",
        }
    ]
    assert result["captures"][0]["captures"]["remote_hostname"] == "known"
    assert "secret-do-not-expose" not in json.dumps(result)
    assert roots and all(not root.exists() for root in roots)
    assert not cache.exists()
    assert selected.model_dump() == before
    assert {path: path.read_bytes() for path in artifacts} == artifacts


def test_repository_picker_and_preview_do_not_mark_prune_or_replace_successful_snapshots(
    tmp_path: Path,
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, bare = repository(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(cache))
    selected = config(bare)
    provider = OxidizedGitProvider(
        "oxidized", identity_engine, selected.providers["oxidized"].settings
    )
    successful = provider.inspect()
    old = provider.repository.cache / "snapshots" / str(successful.revision)
    old_content = {
        path.relative_to(old): path.read_bytes() for path in old.rglob("*") if path.is_file()
    }
    (worktree / "topology/other.example.net--lldp-neighbors").write_text(SAMPLE)
    git("add", "topology", cwd=worktree)
    git("commit", "-m", "add another artifact", cwd=worktree)
    git("push", str(bare), "main", cwd=worktree)
    revision = git("rev-parse", "main", cwd=bare)
    before = selected.model_dump()
    temporary_roots: list[Path] = []

    def never_mark(*_args: Any) -> None:
        pytest.fail("preview must not mark or prune snapshots")

    def execute(*args: Any, **kwargs: Any):
        temporary = args[3]
        temporary_roots.append(temporary)
        assert not temporary.is_relative_to(cache)
        assert [
            path.relative_to(temporary).as_posix()
            for path in temporary.rglob("*")
            if path.is_file()
        ] == [FILENAME]
        assert (temporary / FILENAME).stat().st_mode & 0o777 == 0o600
        return execute_parser(*args, **kwargs)

    monkeypatch.setattr(GitTopologySnapshot, "mark_successful", never_mark)
    monkeypatch.setattr(GitTopologySnapshot, "discard_failed", never_mark)
    monkeypatch.setattr(preview_module, "execute_parser", execute)
    picker = parser_preview(selected, identity_engine, payload(query="LOCAL"), list_files=True)
    assert picker["files"] == [FILENAME] and picker["total"] == 1
    assert picker["revision"] == revision and not temporary_roots
    result = parser_preview(selected, identity_engine, payload())
    assert result["revision"] == revision and result["connection_count"] == 1
    assert temporary_roots and all(not path.exists() for path in temporary_roots)
    assert selected.model_dump() == before
    assert {
        path.relative_to(old): path.read_bytes() for path in old.rglob("*") if path.is_file()
    } == old_content
    assert (provider.repository.cache / "snapshots" / revision / ".complete").is_file()

    def fail(*_args: Any, **_kwargs: Any):
        raise RuntimeError("credential secret-do-not-expose")

    monkeypatch.setattr(preview_module, "execute_parser", fail)
    with pytest.raises(RuntimeError, match="check the definition"):
        parser_preview(selected, identity_engine, payload())
    assert (old / ".complete").is_file()
    assert (provider.repository.cache / "snapshots" / revision / ".complete").is_file()


@pytest.mark.parametrize(
    "filename",
    ["../sample", "/sample", "configs/../sample", "configs\\sample", ".git/config", "configs/*"],
)
def test_preview_rejects_unsafe_paths_before_execution(
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    def never_execute(*_args: Any, **_kwargs: Any):
        pytest.fail("unsafe preview path reached parser execution")

    monkeypatch.setattr(preview_module, "execute_parser", never_execute)
    with pytest.raises(ValueError):
        parser_preview(config(), identity_engine, payload(filename=filename, sample=SAMPLE))


def test_picker_ignores_snapshot_markers_and_symlinks_including_parent_directories(
    tmp_path: Path,
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "snapshot"
    (root / "configs").mkdir(parents=True)
    (root / "configs/real.cfg").write_text(SAMPLE)
    (root / ".complete").write_text("complete")
    (root / "alias").symlink_to(root / "configs", target_is_directory=True)
    (root / "configs/link.cfg").symlink_to(root / "configs/real.cfg")
    (tmp_path / "outside.cfg").write_text(SAMPLE)
    (root / "outside.cfg").symlink_to(tmp_path / "outside.cfg")

    @contextmanager
    def snapshot(_self: GitTopologySnapshot):
        yield root

    monkeypatch.setattr(GitTopologySnapshot, "open", snapshot)
    preview_payload = payload()
    preview_payload["definition"]["file_globs"] = ["**/*", "alias/*"]
    result = parser_preview(
        config(tmp_path / "unused.git"), identity_engine, preview_payload, list_files=True
    )
    assert result["files"] == ["configs/real.cfg"] and result["total"] == 1
    for filename in ("alias/real.cfg", "configs/link.cfg", "outside.cfg"):
        with pytest.raises(ValueError, match="matching this parser"):
            parser_preview(
                config(tmp_path / "unused.git"),
                identity_engine,
                {**preview_payload, "filename": filename},
            )


@pytest.mark.parametrize(
    "error",
    [
        ValueError("credential secret-do-not-expose"),
        RuntimeError("credential secret-do-not-expose"),
    ],
)
def test_preview_sanitizes_parser_errors_and_cleans_temporary_samples(
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    roots: list[Path] = []

    def fail(*args: Any, **_kwargs: Any):
        roots.append(args[3])
        raise error

    monkeypatch.setattr(preview_module, "execute_parser", fail)
    with pytest.raises(RuntimeError, match="check the definition") as caught:
        parser_preview(config(), identity_engine, payload(sample=SAMPLE))
    assert "secret-do-not-expose" not in str(caught.value)
    assert roots and all(not path.exists() for path in roots)


def test_preview_timeout_is_clear_and_terminates_worker(
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = {child.pid for child in multiprocessing.active_children()}

    def timeout(*args: Any, **kwargs: Any):
        return execute_parser(*args, **kwargs, timeout=0.001)

    monkeypatch.setattr(preview_module, "execute_parser", timeout)
    with pytest.raises(RuntimeError, match="timed out; no changes were published"):
        parser_preview(config(), identity_engine, payload(sample=SAMPLE))
    assert {child.pid for child in multiprocessing.active_children()} <= before


def test_preview_bounds_fields_rows_and_serialized_bytes_without_changing_totals() -> None:
    large = "界" * 2048
    devices = [
        Device(
            source_id=name,
            hostname=large,
            interfaces=[Interface(source_id=name + "-port", device_source_id=name, name=large)],
        )
        for name in ("a", "b")
    ]
    links = [
        Link(
            source_id=str(index),
            a=LinkEndpoint(device="a", interface="a-port"),
            b=LinkEndpoint(device="b", interface="b-port"),
            protocol=large,
        )
        for index in range(120)
    ]
    report = ProviderInspection(
        provider="oxidized",
        provider_type="oxidized",
        matched_files=[str(index) for index in range(120)],
        warnings=[large] * 120,
        resolution_failures=[{"path": large, "reason": large}] * 120,
        skipped_files=[{"path": large, "reason": large}] * 120,
        topology=Topology(provider="oxidized", devices=devices, links=links),
    )
    captures = [
        {
            "path": large,
            "rule": large,
            "block": index,
            "captures": {f"field-{field}": large for field in range(120)},
        }
        for index in range(120)
    ]
    original = report.model_dump()
    result = _result(PARSER, report, captures, "revision")
    assert len(json.dumps(result).encode()) <= MAX_PREVIEW_BYTES
    assert result["truncated"] and "truncated" in result["truncation_notice"]
    assert result["artifact_count"] == result["connection_count"] == 120
    assert (
        result["warning_count"]
        == result["resolution_failure_count"]
        == result["skipped_file_count"]
        == 120
    )
    assert result["device_count"] == 2
    assert result["captures"] and len(result["captures"][0]["captures"]) == 100
    assert all(
        len(result[key]) <= 100
        for key in ("captures", "links", "warnings", "resolution_failures", "skipped_files")
    )

    def check_fields(value: Any) -> None:
        if isinstance(value, str):
            assert len(value) <= FIELD_LIMIT
        elif isinstance(value, dict):
            for key, item in value.items():
                check_fields(key)
                check_fields(item)
        elif isinstance(value, list):
            for item in value:
                check_fields(item)

    check_fields(result)
    assert report.model_dump() == original
    assert captures[0]["captures"]["field-0"] == large


def test_repository_picker_output_is_byte_bounded_and_retains_total_count(
    tmp_path: Path,
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "snapshot"
    folder = root.joinpath(*["界" * 80 for _ in range(5)])
    folder.mkdir(parents=True)
    for index in range(502):
        (folder / f"device-{index:03}.cfg").write_text("")

    @contextmanager
    def snapshot(_self: GitTopologySnapshot):
        yield root

    monkeypatch.setattr(GitTopologySnapshot, "open", snapshot)
    preview_payload = payload()
    preview_payload["definition"]["file_globs"] = ["**/*.cfg"]
    result = parser_preview(
        config(tmp_path / "unused.git"), identity_engine, preview_payload, list_files=True
    )
    assert result["total"] == 502 and 0 < len(result["files"]) < 500
    assert result["truncated"] and len(json.dumps(result).encode()) <= MAX_PREVIEW_BYTES
