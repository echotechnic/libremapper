from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from libremapper.config import AppConfig
from libremapper.parser_definitions import (
    BUILTIN_PARSERS,
    DEFINITION_ADAPTER,
    MAX_DEFINITION_BYTES,
    MAX_SAMPLE_BYTES,
    SAOS10_NEIGHBORS,
    NniParserDefinition,
    RegexParserDefinition,
    parser_catalog,
    preview_definition,
    validate_parser_id,
)


def regex_definition(**changes: Any) -> dict[str, Any]:
    return {**BUILTIN_PARSERS["saos10-lldp-neighbors"].model_dump(mode="json"), **changes}


def config_data() -> dict[str, Any]:
    return {
        "providers": {"configs": {"type": "oxidized", "settings": {"repository": "/unused.git"}}},
        "maps": [{"key": "a", "name": "A", "sources": [{"provider": "configs"}]}],
    }


def test_packaged_parser_definitions_preserve_existing_defaults() -> None:
    lldp = BUILTIN_PARSERS["saos10-lldp-neighbors"]
    assert isinstance(lldp, RegexParserDefinition)
    assert lldp.model_dump() == {
        "type": "regex",
        "label": "Ciena SAOS 10 LLDP neighbors",
        "file_globs": ["topology/*--lldp-neighbors"],
        "path_regex": r"^topology/(?P<device_name>.+)--lldp-neighbors$",
        "zero_output_guard": True,
        "protocol": "lldp",
        "rules": [
            {
                "id": "saos10-lldp-neighbors",
                "label": "",
                "regex": SAOS10_NEIGHBORS,
                "flags": ["MULTILINE", "DOTALL"],
            }
        ],
    }
    nni = BUILTIN_PARSERS["saos10-nni-config"]
    assert isinstance(nni, NniParserDefinition)
    assert nni.model_dump() == {
        "type": "saos10_nni",
        "label": "Ciena SAOS 10 NNI configuration",
        "file_globs": ["*", "configs/*"],
        "path_regex": r"^(?:configs/)?(?P<device_name>[^/]+)$",
        "zero_output_guard": False,
        "peer_hostname_regex": r"^car\d+(?:\.|$)",
        "interface_types": ["point-to-point"],
        "ipv4_prefix_lengths": [30, 31],
        "ipv6_prefix_lengths": [126, 127],
    }


def test_lldp_port_description_stays_with_its_neighbor() -> None:
    block = (
        "| interface | 7 |\n| chassis-id | 123 |\n{description}"
        "| port-id | sfp28-1 |\n| system-name | car1 |\n"
    )
    text = block.format(description="") + block.format(description="| port-desc | ae1/sfp28-1 |\n")
    matches = list(re.finditer(SAOS10_NEIGHBORS, text, re.MULTILINE | re.DOTALL))
    assert [match.group("remote_port_description") for match in matches] == [None, "ae1/sfp28-1 "]


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/etc/passwd",
        "../*",
        "configs/../*",
        "./configs/*",
        "configs//a",
        "configs/",
        ".git/config",
        "configs/.git/*",
        "configs\\*",
        "configs/\x00",
        "x" * 1025,
    ],
)
def test_definition_rejects_unsafe_artifact_globs(pattern: str) -> None:
    with pytest.raises(ValidationError):
        DEFINITION_ADAPTER.validate_python(regex_definition(file_globs=[pattern]))


@pytest.mark.parametrize("parser_id", ["", "Upper", "a/b", "-start", "a\n", "a" * 65])
def test_parser_and_rule_ids_require_stable_lowercase_names(parser_id: str) -> None:
    with pytest.raises(ValueError):
        validate_parser_id(parser_id)
    with pytest.raises(ValidationError):
        DEFINITION_ADAPTER.validate_python(
            regex_definition(
                rules=[
                    {
                        "id": parser_id,
                        "regex": r"(?P<remote_hostname>.+)",
                    }
                ]
            )
        )
    payload = config_data()
    payload["oxidized_parsers"] = {parser_id: regex_definition()}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


@pytest.mark.parametrize("parser_id", ["a", "campus-main.config_2", "a" * 64])
def test_parser_id_accepts_documented_stable_names(parser_id: str) -> None:
    assert validate_parser_id(parser_id) == parser_id


@pytest.mark.parametrize(
    "changes",
    [
        {"path_regex": "["},
        {"file_globs": []},
        {"rules": [{"id": "neighbors", "regex": "["}]},
        {"rules": [{"id": "neighbors", "regex": ".+"}]},
        {"rules": [{"id": "neighbors", "regex": r"(?P<remote_hostname>.+)", "flags": ["LOCALE"]}]},
        {
            "rules": [
                {
                    "id": "neighbors",
                    "regex": r"(?P<remote_hostname>.+)",
                    "flags": ["IGNORECASE", "IGNORECASE"],
                }
            ]
        },
        {"rules": [{"id": "neighbors", "regex": r"(?P<remote_hostname>.+)"}] * 2},
        {"protocol": "bad protocol"},
        {"unexpected": True},
    ],
)
def test_regex_definition_validates_patterns_rules_and_flags(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DEFINITION_ADAPTER.validate_python(regex_definition(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"path_regex": r"^configs/(.+)$"},
        {"peer_hostname_regex": "["},
        {"interface_types": []},
        {"ipv4_prefix_lengths": [-1]},
        {"ipv4_prefix_lengths": [33]},
        {"ipv6_prefix_lengths": [-1]},
        {"ipv6_prefix_lengths": [129]},
        {"ipv4_prefix_lengths": [31, 31]},
        {"ipv6_prefix_lengths": [127, 127]},
        {"ipv4_prefix_lengths": [], "ipv6_prefix_lengths": []},
    ],
)
def test_nni_definition_validates_matching_controls(changes: dict[str, Any]) -> None:
    payload = {**BUILTIN_PARSERS["saos10-nni-config"].model_dump(mode="json"), **changes}
    with pytest.raises(ValidationError):
        DEFINITION_ADAPTER.validate_python(payload)


def test_definition_size_limit_applies_to_complete_definition() -> None:
    payload = regex_definition(
        rules=[
            {"id": f"fallback-{index}", "regex": r"(?P<remote_hostname>x)" + "a" * 33000}
            for index in range(2)
        ]
    )
    assert all(len(rule["regex"].encode()) < MAX_DEFINITION_BYTES for rule in payload["rules"])
    assert len(json.dumps(payload).encode()) > MAX_DEFINITION_BYTES
    with pytest.raises(ValidationError, match="64 KiB"):
        DEFINITION_ADAPTER.validate_python(payload)
    with pytest.raises(ValueError, match="64 KiB"):
        preview_definition(
            {
                "parser_id": "preview",
                "definition": payload,
                "filename": "topology/local--lldp-neighbors",
                "sample": "sample",
            }
        )


def test_catalog_overrides_are_complete_and_do_not_modify_packaged_defaults() -> None:
    original = BUILTIN_PARSERS["saos10-lldp-neighbors"].model_dump()
    override = DEFINITION_ADAPTER.validate_python(
        regex_definition(label="Site LLDP", protocol="cdp")
    )
    custom = DEFINITION_ADAPTER.validate_python(regex_definition(label="Other platform"))
    result = parser_catalog({"saos10-lldp-neighbors": override, "other-platform": custom})
    assert set(result) == {*BUILTIN_PARSERS, "other-platform"}
    assert result["saos10-lldp-neighbors"] == override
    assert result["other-platform"] == custom
    assert BUILTIN_PARSERS["saos10-lldp-neighbors"].model_dump() == original


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "../sample",
        "/sample",
        ".git/config",
        "configs\\sample",
        "configs/*",
        "configs/?",
        "configs/[abc]",
        ".complete",
        "configs/.complete",
    ],
)
def test_preview_rejects_unsafe_or_pattern_filenames(filename: str) -> None:
    with pytest.raises(ValueError):
        preview_definition(
            {
                "parser_id": "preview",
                "definition": regex_definition(),
                "filename": filename,
                "sample": "",
            }
        )


def test_preview_sample_byte_limit_and_unsaved_definition() -> None:
    payload = {
        "parser_id": "unsaved",
        "definition": regex_definition(label="Unsaved parser"),
        "filename": "topology/local--lldp-neighbors",
        "sample": "é" * (MAX_SAMPLE_BYTES // 2),
    }
    original = copy.deepcopy(payload)
    parser_id, selected, filename = preview_definition(payload)
    assert parser_id == "unsaved" and selected.label == "Unsaved parser"
    assert filename == payload["filename"] and payload == original
    assert "unsaved" not in BUILTIN_PARSERS
    payload["sample"] += "é"
    with pytest.raises(ValueError, match="256 KiB"):
        preview_definition(payload)
    payload["sample"] = 42
    with pytest.raises(ValueError, match="256 KiB"):
        preview_definition(payload)


@pytest.mark.parametrize("selection", [None, ["saos10-nni-config"], []])
def test_source_selections_inherit_provider_legacy_defaults_or_disable_explicitly(
    selection: list[str] | None,
) -> None:
    payload = config_data()
    payload["providers"]["configs"]["settings"]["parsers"] = ["saos10-nni-config"]
    payload["maps"][0]["sources"][0]["parsers"] = selection
    config = AppConfig.model_validate(payload)
    source = config.maps[0].sources[0]
    assert config.effective_parsers(source) == (
        ["saos10-nni-config"] if selection is None else selection
    )
    assert source.parsers == selection
    assert payload["maps"][0]["sources"][0]["parsers"] == selection
    fresh = AppConfig.model_validate(config_data())
    assert fresh.effective_parsers(fresh.maps[0].sources[0]) == ["saos10-lldp-neighbors"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("selection", [["unknown"], ["saos10-lldp-neighbors"] * 2])
def test_unknown_or_duplicate_parser_selection_rejected(legacy: bool, selection: list[str]) -> None:
    payload = config_data()
    if legacy:
        payload["providers"]["configs"]["settings"]["parsers"] = selection
    else:
        payload["maps"][0]["sources"][0]["parsers"] = selection
    with pytest.raises(ValidationError, match="unknown or duplicate"):
        AppConfig.model_validate(payload)


@pytest.mark.parametrize("selection", [[], ["saos10-lldp-neighbors"]])
def test_parser_selection_is_forbidden_on_non_oxidized_sources(selection: list[str]) -> None:
    payload = config_data()
    payload["providers"]["configs"] = {"type": "file", "settings": {"path": "/unused.yaml"}}
    payload["maps"][0]["sources"][0]["parsers"] = selection
    with pytest.raises(ValidationError, match="Oxidized source"):
        AppConfig.model_validate(payload)


def test_empty_legacy_defaults_require_explicit_map_disable_instead() -> None:
    payload = config_data()
    payload["providers"]["configs"]["settings"]["parsers"] = []
    with pytest.raises(ValidationError, match="legacy provider parser defaults cannot be empty"):
        AppConfig.model_validate(payload)


def test_unicode_definition_byte_limit_matches_preview_and_structural_validation() -> None:
    raw = regex_definition()
    raw["rules"][0]["regex"] = "(?P<remote_hostname>" + "界" * 11000 + ")"
    definition = DEFINITION_ADAPTER.validate_python(raw)
    _, preview, _ = preview_definition(
        {"parser_id": "unicode", "definition": raw, "filename": "sample"}
    )
    assert preview == definition


def test_catalog_usage_unions_multiple_scopes_of_the_same_provider() -> None:
    from libremapper.parser_definitions import describe_catalog

    raw = config_data()
    raw["maps"][0]["sources"] = [
        {"provider": "configs", "parsers": ["saos10-lldp-neighbors"]},
        {"provider": "configs", "parsers": ["saos10-nni-config"]},
        {"provider": "configs", "parsers": ["saos10-lldp-neighbors"]},
    ]
    selected = AppConfig.model_validate(raw)
    result = describe_catalog(selected)
    key = str(selected.maps[0].key)
    assert result["selections"][key]["configs"] == ["saos10-lldp-neighbors", "saos10-nni-config"]
    assert all(item["maps"] == [key] for item in result["items"])
