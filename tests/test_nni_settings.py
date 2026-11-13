from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from test_oxidized_git import NNI_CONFIG
from test_oxidized_git import identity_engine as identity_engine
from test_oxidized_git import nni_engine as nni_engine

from libremapper.models import stable_hash
from libremapper.parser_definitions import NniParserDefinition
from libremapper.providers.ciena_nni import (
    DEFAULT_DEFINITION,
    PARSER_NAME,
    _interfaces,
    inspect_ciena_nni,
)
from libremapper.providers.regex_artifacts import LibreNMSIdentityResolver


def definition(**changes: Any) -> NniParserDefinition:
    return NniParserDefinition.model_validate({**DEFAULT_DEFINITION.model_dump(), **changes})


def test_nni_defaults_preserve_identity_and_custom_parser_namespaces(
    tmp_path: Path,
    nni_engine: Engine,
) -> None:
    (tmp_path / "local.example.net").write_text(NNI_CONFIG)
    resolver = LibreNMSIdentityResolver(nni_engine)
    default = inspect_ciena_nni("oxidized", nni_engine, tmp_path, resolver)
    explicit = inspect_ciena_nni("oxidized", nni_engine, tmp_path, resolver, definition())
    assert default == explicit
    link = default.topology.links[0]
    assert (
        link.source_id
        == stable_hash(
            {
                "parser": PARSER_NAME,
                "local": "device:1",
                "interface": "IF_ACCESS",
                "remote": "device:3",
                "remote_port": 31,
            }
        )[:24]
    )
    custom = inspect_ciena_nni(
        "oxidized", nni_engine, tmp_path, resolver, definition(), "campus-nni"
    )
    assert custom.topology.links[0].source_id != link.source_id
    assert custom.topology.links[0].attributes["rule"] == "campus-nni"
    assert custom.topology.devices == default.topology.devices


def test_nni_filename_extraction_glob_dedup_and_symlink_exclusion(
    tmp_path: Path,
    nni_engine: Engine,
) -> None:
    root = tmp_path / "snapshot"
    (root / "routers").mkdir(parents=True)
    (root / "routers/local.example.net.cfg").write_text(NNI_CONFIG)
    (tmp_path / "outside.cfg").write_text(NNI_CONFIG)
    (root / "routers/outside.cfg").symlink_to(tmp_path / "outside.cfg")
    (root / "alias").symlink_to(root / "routers", target_is_directory=True)
    selected = definition(
        file_globs=["routers/*", "routers/*.cfg", "alias/*"],
        path_regex=r"^(?:routers|alias)/(?P<device_name>.+)\.cfg$",
    )
    result = inspect_ciena_nni(
        "oxidized", nni_engine, root, LibreNMSIdentityResolver(nni_engine), selected
    )
    assert result.matched_files == ["routers/local.example.net.cfg"]
    assert len(result.topology.links) == 1
    assert not result.skipped_files


def test_nni_peer_pattern_is_case_insensitive_and_guard_is_configurable(
    tmp_path: Path,
    nni_engine: Engine,
) -> None:
    (tmp_path / "local.example.net").write_text(NNI_CONFIG)
    resolver = LibreNMSIdentityResolver(nni_engine)
    assert (
        len(
            inspect_ciena_nni(
                "oxidized",
                nni_engine,
                tmp_path,
                resolver,
                definition(peer_hostname_regex=r"^CAR1\.SCHOOL"),
            ).topology.links
        )
        == 1
    )
    absent = definition(peer_hostname_regex=r"^aggregation", zero_output_guard=True)
    result = inspect_ciena_nni("oxidized", nni_engine, tmp_path, resolver, absent)
    assert not result.topology.links and result.blocked
    unguarded = inspect_ciena_nni(
        "oxidized",
        nni_engine,
        tmp_path,
        resolver,
        absent.model_copy(update={"zero_output_guard": False}),
    )
    assert not unguarded.blocked


@pytest.mark.parametrize(
    "version,address,prefix,default_prefix",
    [
        (4, "192.0.2.10", 29, 31),
        (6, "2001:db8::10", 124, 127),
    ],
)
def test_nni_interface_types_prefix_lengths_and_context(
    version: int,
    address: str,
    prefix: int,
    default_prefix: int,
) -> None:
    content = (
        'oc-if:interfaces interface "UPLINK" config type routed vrfName "campus-vrf"\n'
        f'oc-if:interfaces interface "UPLINK" ipv{version} addresses address "{address}" '
        f'config ip "{address}" prefix-length {prefix}'
    )
    assert not _interfaces(content)
    selected = definition(interface_types=["routed"], **{f"ipv{version}_prefix_lengths": [prefix]})
    assert _interfaces(content, selected) == [
        ("UPLINK", "campus-vrf", ipaddress.ip_interface(f"{address}/{prefix}")),
    ]
    assert not _interfaces(
        content, selected.model_copy(update={f"ipv{version}_prefix_lengths": [default_prefix]})
    )


@pytest.mark.parametrize("scenario", ["bound", "no-peer", "unknown-local"])
def test_nni_preview_captures_are_structured_and_include_unresolved_interfaces(
    tmp_path: Path,
    nni_engine: Engine,
    scenario: str,
) -> None:
    name = "unknown.example.net" if scenario == "unknown-local" else "local.example.net"
    (tmp_path / name).write_text(NNI_CONFIG + "\ncredential secret-do-not-expose\n")
    captures: list[dict[str, Any]] = []
    selected = definition(peer_hostname_regex=r"^absent" if scenario == "no-peer" else r"^CAR1")
    inspect_ciena_nni(
        "oxidized",
        nni_engine,
        tmp_path,
        LibreNMSIdentityResolver(nni_engine),
        selected,
        captures=captures,
    )
    assert len(captures) == 1
    record = captures[0]
    assert record["path"] == name and record["rule"] == PARSER_NAME and record["block"] == 1
    extracted = record["captures"]
    assert extracted["local_hostname"] == name
    assert extracted["local_interface"] == "IF_ACCESS"
    assert extracted["address"] == "192.0.2.10/31" and extracted["context"] == ""
    if scenario == "bound":
        assert extracted["local_device_id"] == 1
        assert extracted["local_port_id"] is None and extracted["local_interface_unbound"]
        assert extracted["remote_device_id"] == 3 and extracted["remote_port_id"] == 31
    else:
        assert extracted["reason"]
    assert "secret-do-not-expose" not in str(captures)


def test_nni_preview_caps_display_without_truncating_topology(
    tmp_path: Path,
    nni_engine: Engine,
) -> None:
    (tmp_path / "local.example.net").write_text(
        "\n".join(NNI_CONFIG.replace("IF_ACCESS", f"IF_{index}") for index in range(105))
    )
    captures: list[dict[str, Any]] = []
    result = inspect_ciena_nni(
        "oxidized", nni_engine, tmp_path, LibreNMSIdentityResolver(nni_engine), captures=captures
    )
    assert len(captures) == 100
    assert len(result.topology.links) == 105
