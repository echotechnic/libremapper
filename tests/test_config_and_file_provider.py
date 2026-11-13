from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from libremapper.config import LayoutConfig, MapConfig, SelectorConfig, SourceConfig, load_config
from libremapper.models import Device
from libremapper.providers.file import FileProvider


def write_fragment_config(tmp_path: Path, maps: list[dict[str, object]] | None = None) -> Path:
    (tmp_path / "maps").mkdir()
    config_path = tmp_path / "libremapper.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "map_directory": "maps",
                "providers": {},
                "maps": maps or [],
            }
        )
    )
    return config_path


def test_shape_controls_are_opt_in() -> None:
    config = MapConfig(name="Test", sources=[])

    assert (config.width, config.height, config.edge_separation) == ("1600px", "800px", 24)
    assert (
        config.layout.width,
        config.layout.height,
        config.layout.margin,
        config.layout.layer_spacing,
        config.layout.node_spacing,
        config.layout.radius,
    ) == (1600, 800, 60, 180, 120, 180)
    assert config.node_label == "full"
    assert config.short_hostname_parts == 2
    assert config.layout.auto_grow is False
    assert config.layout.type == "force"
    assert config.layout.infer_layers is False
    assert config.layout.center_layer is None
    assert config.layout.separation == "packed"
    assert config.layout.layer_margins == {}
    assert config.layer_overrides == []
    assert config.device_exclusions == []


def test_map_text_has_contrast_on_light_and_dark_backgrounds() -> None:
    from libremapper.layouts import LayoutResult
    from libremapper.service import LibremapperService

    config = MapConfig(name="Test", sources=[])
    values = LibremapperService._map_values(config, LayoutResult({}, 1600, 800))
    assert config.node_defaults.text_colour == config.edge_defaults.text_colour == "#243A54"
    assert config.logical_edge_defaults.text_colour == "#243A54"
    assert values["options"]["nodes"]["font"] == values["options"]["edges"]["font"] == {
        "strokeWidth": 3,
        "strokeColor": "#FFFFFF",
    }


@pytest.mark.parametrize("parts", [0, -1, 128, 1.5, "two"])
def test_short_hostname_component_count_is_validated(parts: object) -> None:
    with pytest.raises(ValidationError, match="short_hostname_parts"):
        MapConfig.model_validate({"name": "Labels", "sources": [], "short_hostname_parts": parts})
    with pytest.raises(ValidationError, match="short_hostname_parts"):
        Device.model_validate({"source_id": "router", "short_hostname_parts": parts})
    with pytest.raises(ValidationError, match="parts"):
        MapConfig.model_validate({"name": "Labels", "sources": [], "short_hostname_overrides": [
            {"device_id": 1, "parts": parts},
        ]})


def test_short_hostname_overrides_reject_duplicate_devices() -> None:
    with pytest.raises(ValidationError, match="each device only once"):
        MapConfig.model_validate({"name": "Labels", "sources": [], "short_hostname_overrides": [
            {"device_id": 1, "parts": 2}, {"device_id": 1, "parts": 3},
        ]})


@pytest.mark.parametrize("selector_type", ["hostname", "location", "attribute"])
def test_selector_regex_is_validated_before_loading_a_provider(selector_type: str) -> None:
    with pytest.raises(ValidationError, match="invalid selector regex"):
        SelectorConfig.model_validate(
            {"type": selector_type, "attribute": "sys_name", "value": "["}
        )

    selector = SelectorConfig.model_validate(
        {"type": selector_type, "attribute": "sys_name", "value": r"^core-\d+$"}
    )
    assert selector.value == r"^core-\d+$"


@pytest.mark.parametrize("selector_type", ["group", "canonical"])
def test_literal_selectors_preserve_regex_metacharacters(selector_type: str) -> None:
    assert SelectorConfig.model_validate({"type": selector_type, "value": "["}).value == "["


@pytest.mark.parametrize("included_id", [7, "7"])
def test_device_exclusions_reject_an_exact_inclusion_in_any_source(included_id: int | str) -> None:
    with pytest.raises(ValidationError, match="both explicitly included and excluded"):
        MapConfig.model_validate(
            {
                "name": "Conflicting exceptions",
                "sources": [
                    {"provider": "automatic", "selectors": [{"type": "group", "value": "Core"}]},
                    {
                        "provider": "manual",
                        "selectors": [{"type": "device_id", "value": included_id}],
                    },
                ],
                "device_exclusions": [7],
            }
        )


@pytest.mark.parametrize("device_id", [0, -1])
def test_device_exclusions_require_positive_device_ids(device_id: int) -> None:
    with pytest.raises(ValidationError, match="positive device IDs"):
        MapConfig(name="Invalid exclusion", sources=[], device_exclusions=[device_id])


def test_explicit_larger_layout_and_auto_growth_are_preserved() -> None:
    config = MapConfig(
        name="Large",
        sources=[],
        width="2400px",
        height="1400px",
        layout={"width": 2400, "height": 1400, "auto_grow": True},
    )

    assert (config.width, config.height) == ("2400px", "1400px")
    assert (config.layout.width, config.layout.height, config.layout.auto_grow) == (
        2400,
        1400,
        True,
    )


@pytest.mark.parametrize("layout_type", ["force", "radial"])
def test_layer_margins_reject_unsupported_layouts(layout_type: str) -> None:
    with pytest.raises(ValidationError, match="only supported for auto, top and left"):
        LayoutConfig(type=layout_type, layer_margins={0: (10, 20)})


def test_layer_margins_must_be_non_negative() -> None:
    with pytest.raises(ValidationError, match="must be non-negative"):
        LayoutConfig(type="left", layer_margins={0: (-1, 20)})


def test_relative_provider_and_state_paths_are_resolved(tmp_path: Path) -> None:
    topology = tmp_path / "topology.yaml"
    topology.write_text("version: 1\ndevices: []\nlinks: []\n")
    config_path = tmp_path / "libremapper.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "state_path": "state.sqlite3",
                "providers": {
                    "external": {
                        "type": "file",
                        "settings": {"path": "topology.yaml"},
                    }
                },
                "maps": [
                    {
                        "name": "Test Map",
                        "sources": [{"provider": "external"}],
                    }
                ],
            }
        )
    )

    config = load_config(config_path)

    assert config.state_path == tmp_path / "state.sqlite3"
    assert config.providers["external"].settings["path"] == str(topology)
    assert config.maps[0].key == "test-map"


def test_map_fragments_follow_inline_maps_in_lexical_order(tmp_path: Path) -> None:
    config_path = write_fragment_config(
        tmp_path, [{"key": "inline", "name": "Inline", "sources": []}]
    )
    (tmp_path / "maps" / "z-last.yaml").write_text(
        yaml.safe_dump({"key": "z-last", "name": "Last", "sources": []})
    )
    (tmp_path / "maps" / "a-first.yml").write_text(
        yaml.safe_dump({"key": "a-first", "name": "First", "sources": []})
    )

    config = load_config(config_path)

    assert [item.key for item in config.maps] == ["inline", "a-first", "z-last"]


def test_map_fragment_filename_must_match_safe_lowercase_key(tmp_path: Path) -> None:
    config_path = write_fragment_config(tmp_path)
    (tmp_path / "maps" / "wrong.yaml").write_text(
        yaml.safe_dump({"key": "right", "name": "Right", "sources": []})
    )

    with pytest.raises(ValueError, match="filename must match"):
        load_config(config_path)


def test_map_fragments_reject_duplicate_keys(tmp_path: Path) -> None:
    config_path = write_fragment_config(
        tmp_path, [{"key": "duplicate", "name": "Inline", "sources": []}]
    )
    (tmp_path / "maps" / "duplicate.yaml").write_text(
        yaml.safe_dump({"key": "duplicate", "name": "Fragment", "sources": []})
    )

    with pytest.raises(ValidationError, match="duplicate map key"):
        load_config(config_path)


def test_map_fragments_reject_malformed_nested_and_symlink_paths(tmp_path: Path) -> None:
    config_path = write_fragment_config(tmp_path)
    malformed = tmp_path / "maps" / "malformed.yaml"
    malformed.write_text("- not-a-mapping\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(config_path)

    malformed.unlink()
    (tmp_path / "maps" / "nested").mkdir()
    with pytest.raises(ValueError, match="nested map paths"):
        load_config(config_path)

    (tmp_path / "maps" / "nested").rmdir()
    target = tmp_path / "target.yaml"
    target.write_text("key: linked\nname: Linked\nsources: []\n")
    (tmp_path / "maps" / "linked.yaml").symlink_to(target)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        load_config(config_path)


def test_display_metadata_does_not_change_config_fingerprint(tmp_path: Path) -> None:
    config_path = write_fragment_config(tmp_path)
    fragment = tmp_path / "maps" / "scheduled.yaml"
    fragment.write_text("key: scheduled\nname: Scheduled\nscheduled: false\nsources: []\n")
    before = load_config(config_path)

    fragment.write_text("key: scheduled\nname: Scheduled\nscheduled: true\nsources: []\n")
    after_schedule = load_config(config_path)
    fragment.write_text(
        "key: scheduled\nname: Scheduled\nscheduled: true\nexample: true\nsources: []\n"
    )
    after_example = load_config(config_path)
    fragment.write_text("key: scheduled\nname: Changed\nscheduled: true\nsources: []\n")
    after_content = load_config(config_path)

    assert before.maps[0].scheduled is False
    assert after_schedule.maps[0].scheduled is True
    assert after_example.maps[0].example is True
    assert "example" not in after_example.maps[0].model_dump(mode="json")
    assert before.fingerprint() == after_schedule.fingerprint()
    assert after_schedule.fingerprint() == after_example.fingerprint()
    assert after_example.fingerprint() != after_content.fingerprint()


def test_state_path_can_be_overridden_for_host_orchestration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topology = tmp_path / "topology.yaml"
    topology.write_text("version: 1\ndevices: []\nlinks: []\n")
    config_path = tmp_path / "libremapper.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "state_path": "/state/container.sqlite3",
                "providers": {
                    "external": {
                        "type": "file",
                        "settings": {"path": str(topology)},
                    }
                },
                "maps": [{"name": "Test Map", "sources": [{"provider": "external"}]}],
            }
        )
    )
    override = tmp_path / "host-state.sqlite3"
    monkeypatch.setenv("LIBREMAPPER_STATE_PATH", str(override))

    config = load_config(config_path)

    assert config.state_path == override


def test_file_provider_applies_union_selectors_layers_and_protocols(
    tmp_path: Path,
) -> None:
    topology = tmp_path / "topology.yaml"
    topology.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "devices": [
                    {
                        "source_id": "a",
                        "hostname": "core-a.example.net",
                        "attributes": {"location": "HQ"},
                    },
                    {
                        "source_id": "b",
                        "hostname": "edge-b.example.net",
                        "attributes": {"location": "Branch 1"},
                    },
                    {"source_id": "c", "hostname": "unused.example.net"},
                ],
                "links": [
                    {
                        "source_id": "ab",
                        "protocol": "lldp",
                        "a": {"device": "a"},
                        "b": {"device": "b"},
                    },
                    {
                        "source_id": "bc",
                        "protocol": "external",
                        "a": {"device": "b"},
                        "b": {"device": "c"},
                    },
                ],
            }
        )
    )
    provider = FileProvider("external", {"path": str(topology)})
    source = SourceConfig.model_validate(
        {
            "provider": "external",
            "selectors": [
                {"type": "hostname", "value": "^core-", "layer": 0},
                {"type": "location", "value": "^Branch", "layer": 10},
            ],
            "protocols": ["lldp"],
        }
    )

    selected = provider.load(source)

    assert {device.source_id for device in selected.devices} == {"a", "b"}
    assert {device.source_id: device.layer for device in selected.devices} == {
        "a": 0,
        "b": 10,
    }
    assert [link.source_id for link in selected.links] == ["ab"]


def test_librenms_sources_require_explicit_selectors(tmp_path: Path) -> None:
    config_path = tmp_path / "libremapper.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "providers": {"native": {"type": "librenms"}},
                "maps": [
                    {
                        "key": "unsafe",
                        "name": "Unsafe",
                        "sources": [{"provider": "native"}],
                    }
                ],
            }
        )
    )

    with pytest.raises(ValidationError, match="entire LibreNMS database"):
        load_config(config_path)


def test_manual_provider_must_be_a_global_file_provider(tmp_path: Path) -> None:
    topology = tmp_path / "manual.yaml"
    topology.write_text("version: 1\ndevices: []\nlinks: []\n")
    base = {
        "version": 1,
        "providers": {
            "manual": {"type": "file", "settings": {"path": str(topology)}},
            "native": {"type": "librenms"},
        },
        "manual_provider": "manual",
        "maps": [
            {
                "name": "Test",
                "sources": [
                    {
                        "provider": "native",
                        "selectors": [{"type": "hostname", "value": ".*"}],
                    }
                ],
            }
        ],
    }
    config_path = tmp_path / "libremapper.yaml"
    config_path.write_text(yaml.safe_dump(base))

    assert load_config(config_path).manual_provider == "manual"

    base["maps"][0]["sources"] = [{"provider": "manual"}]
    config_path.write_text(yaml.safe_dump(base))
    with pytest.raises(ValidationError, match="cannot also be a map source"):
        load_config(config_path)

    base["maps"][0]["sources"] = [
        {
            "provider": "native",
            "selectors": [{"type": "hostname", "value": ".*"}],
        }
    ]
    base["manual_provider"] = "native"
    config_path.write_text(yaml.safe_dump(base))
    with pytest.raises(ValidationError, match="must reference a file provider"):
        load_config(config_path)


def test_file_topology_validates_suppressions(tmp_path: Path) -> None:
    topology = tmp_path / "manual.yaml"
    topology.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "suppress_links": [{"a": {"device": "a"}}],
            }
        )
    )

    with pytest.raises(ValidationError, match="suppress_links"):
        FileProvider("manual", {"path": str(topology)}).read()
