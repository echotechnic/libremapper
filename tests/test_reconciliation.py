from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine, text

from libremapper.config import SelectorConfig, ShortHostnameOverride, load_config
from libremapper.models import Plan
from libremapper.service import LibremapperService
from libremapper.state import StateStore


def write_topology(path: Path, include_b: bool = True) -> None:
    devices = [
        {
            "source_id": "a",
            "hostname": "a.site.example.net",
            "librenms_device_id": 1,
            "interfaces": [{"source_id": "a-port", "device_source_id": "a", "name": "Ethernet1"}],
        }
    ]
    links = []
    if include_b:
        devices.append(
            {
                "source_id": "b",
                "hostname": "b.site.example.net",
                "librenms_device_id": 2,
                "interfaces": [
                    {"source_id": "b-port", "device_source_id": "b", "name": "Ethernet2"}
                ],
            }
        )
        links.append(
            {
                "source_id": "ab",
                "protocol": "lldp",
                "a": {"device": "a", "interface": "a-port"},
                "b": {"device": "b", "interface": "b-port"},
            }
        )
    path.write_text(yaml.safe_dump({"version": 1, "devices": devices, "links": links}))


def service(tmp_path: Path, engine: Engine) -> tuple[LibremapperService, Path]:
    topology_path = tmp_path / "topology.yaml"
    write_topology(topology_path)
    config_path = tmp_path / "libremapper.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "state_path": str(tmp_path / "state.sqlite3"),
                "librenms": {"url": str(engine.url)},
                "providers": {
                    "external": {
                        "type": "file",
                        "settings": {"path": str(topology_path)},
                    }
                },
                "maps": [
                    {
                        "key": "generated",
                        "name": "Generated",
                        "sources": [{"provider": "external"}],
                        "layout": {
                            "type": "top",
                            "width": 800,
                            "height": 600,
                            "margin": 100,
                            "node_spacing": 200,
                            "auto_grow": True,
                        },
                    }
                ],
            }
        )
    )
    config = load_config(config_path)
    return (
        LibremapperService(config, engine, StateStore(config.state_path)),
        topology_path,
    )


def test_one_sided_discovery_is_selectable_and_excluded_by_default(
    tmp_path: Path, custom_map_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from libremapper.config import LinkGraphOverride
    from libremapper.providers.file import FileProvider

    mapper, path = service(tmp_path, custom_map_engine)
    provider = FileProvider("external", {"path": str(path)})
    monkeypatch.setattr(mapper, "_provider", lambda name: provider)
    mapper.config.providers["external"].type = "oxidized"
    topology = yaml.safe_load(path.read_text())
    del topology["links"][0]["b"]["interface"]
    path.write_text(yaml.safe_dump(topology))
    config = mapper.config.maps[0]
    plan = mapper.create_plan("generated")
    choice = plan.link_choices[0]
    assert choice["one_sided"] and not choice["included"]
    assert not [op for op in plan.operations if op.kind == "edge"]

    config.link_inclusions = [choice["canonical_id"]]
    included = mapper.create_plan("generated")
    assert included.link_choices[0]["included"]
    assert [op for op in included.operations if op.kind == "edge"]
    mapper.apply(included)
    config.link_inclusions = []
    config.link_graph_overrides = [LinkGraphOverride(canonical_id=choice["canonical_id"])]
    excluded = mapper.create_plan("generated")
    assert not excluded.conflicts
    assert any(op.kind == "edge" and op.action == "delete" for op in excluded.operations)
    mapper.apply(excluded)

    topology["links"][0]["b"]["interface"] = "b-port"
    path.write_text(yaml.safe_dump(topology))
    config.link_graph_overrides = []
    complete = mapper.create_plan("generated")
    assert complete.link_choices[0]["included"]
    config.link_exclusions = [complete.link_choices[0]["canonical_id"]]
    assert not mapper.create_plan("generated").link_choices[0]["included"]
    config.link_exclusions = []
    del topology["links"][0]["a"]["interface"]
    del topology["links"][0]["b"]["interface"]
    path.write_text(yaml.safe_dump(topology))
    assert mapper.create_plan("generated").link_choices[0]["included"]


def test_device_hostname_definition_and_map_overrides_reach_published_labels(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    mapper, topology_path = service(tmp_path, custom_map_engine)
    topology = yaml.safe_load(topology_path.read_text())
    topology["devices"][0]["short_hostname_parts"] = 3
    topology_path.write_text(yaml.safe_dump(topology))
    config = mapper.config.maps[0]
    config.node_label = "short_hostname"
    desired = mapper.build_desired(config)
    assert sorted(node.label for node in desired.nodes.values()) == ["a.site.example", "b.site"]
    assert set(desired.node_display_names.values()) == {"a.site.example", "b.site"}
    assert next(iter(desired.edge_display_names.values())).startswith("a.site.example [Ethernet1]")

    config.short_hostname_overrides = [ShortHostnameOverride(device_id=1, parts=1)]
    plan = mapper.create_plan("generated")
    assert {
        operation.generated_values["label"]
        for operation in plan.operations
        if operation.kind == "node"
    } == {"a", "b.site"}
    assert {
        operation.display_name for operation in plan.operations if operation.kind == "node"
    } == {"a", "b.site"}
    mapper.apply(plan)
    assert mapper.create_plan("generated").changes == 0

    config.short_hostname_overrides = []
    restored = mapper.create_plan("generated")
    assert (
        next(
            operation
            for operation in restored.operations
            if operation.kind == "node" and operation.generated_values["device_id"] == 1
        ).generated_values["label"]
        == "a.site.example"
    )


def test_create_noop_manual_preservation_relayout_and_stale_delete(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    mapper, topology_path = service(tmp_path, custom_map_engine)
    first = mapper.create_plan("generated")

    assert [item.action for item in first.operations].count("create") == 4
    assert next(item for item in first.operations if item.kind == "map").display_name == "Generated"
    assert {item.display_name for item in first.operations if item.kind == "node"} == {
        "a.site",
        "b.site",
    }
    assert {item.layer for item in first.operations if item.kind == "node"} == {0}
    edge = next(item for item in first.operations if item.kind == "edge")
    assert edge.display_name == "a.site [Ethernet1] ↔ b.site [Ethernet2] — LLDP"
    assert edge.protocol == "lldp"
    assert {source.provider for source in edge.sources} == {"external"}
    assert all(
        {source.provider for source in item.sources} == {"external"}
        for item in first.operations
        if item.kind == "node"
    )

    serialized = first.model_dump(mode="json")
    assert all("display_name" in item for item in serialized["operations"])
    for item in serialized["operations"]:
        item.pop("display_name")
        item.pop("layer")
        item.pop("sources")
        item.pop("protocol")
    assert all(
        item.display_name is None
        and item.layer is None
        and item.sources == []
        and item.protocol is None
        for item in Plan.model_validate(serialized).operations
    )
    mapper.apply(first)

    with custom_map_engine.connect() as connection:
        dimensions = connection.execute(
            text("SELECT width, height FROM custom_maps WHERE custom_map_id = 1")
        ).one()
    assert tuple(dimensions) == ("800", "600")

    second = mapper.create_plan("generated")
    assert second.changes == 0

    state = mapper.state.show("generated")
    node = next(
        item
        for item in state["objects"]
        if item["kind"] == "node" and item["canonical_id"].endswith(":a")
    )
    with custom_map_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE custom_map_nodes
                SET x_pos = 777, colour_bg = '#abcdef'
                WHERE custom_map_node_id = :id
                """
            ),
            {"id": node["librenms_id"]},
        )

    preserved = mapper.create_plan("generated")
    node_operation = next(
        item
        for item in preserved.operations
        if item.kind == "node" and item.canonical_id == node["canonical_id"]
    )
    assert node_operation.action == "noop"
    assert {"x_pos", "colour_bg"} <= set(node_operation.preserved_fields)

    relayout = mapper.create_plan("generated", relayout=True)
    node_operation = next(
        item
        for item in relayout.operations
        if item.kind == "node" and item.canonical_id == node["canonical_id"]
    )
    assert node_operation.action == "update"
    assert node_operation.values["x_pos"] != 777
    assert "colour_bg" in node_operation.preserved_fields
    mapper.apply(relayout)

    with custom_map_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO custom_map_nodes (
                    custom_map_id, device_id, linked_custom_map_id, label, style,
                    icon, image, node_image_id, size, border_width, text_face,
                    text_size, text_colour, colour_bg, colour_bdr, x_pos, y_pos
                ) VALUES (
                    1, NULL, NULL, 'Manual', 'box', NULL, '', NULL, 25, 1,
                    'arial', 14, '#343434', '#ffffff', '#000000', 10, 10
                )
                """
            )
        )
    write_topology(topology_path, include_b=False)
    stale = mapper.create_plan("generated")
    stale_edge = next(
        item for item in stale.operations if item.action == "delete" and item.kind == "edge"
    )
    stale_node = next(
        item for item in stale.operations if item.action == "delete" and item.kind == "node"
    )
    assert stale_edge.display_name == "a.site ↔ b.site"
    assert stale_node.display_name == "b.site"
    mapper.apply(stale)

    with custom_map_engine.connect() as connection:
        labels = (
            connection.execute(
                text("SELECT label FROM custom_map_nodes ORDER BY custom_map_node_id")
            )
            .scalars()
            .all()
        )
    assert labels == ["a.site.example.net", "Manual"]


def draft_marker(mapper: LibremapperService, map_key: str) -> Path:
    path = (
        mapper.state.path.parent
        / "drafts"
        / f"{hashlib.sha256(map_key.encode()).hexdigest()}.pending"
    )
    path.parent.mkdir(exist_ok=True)
    path.write_text("unpublished")
    return path


@pytest.mark.parametrize("already_published", [False, True])
def test_successful_manual_publish_clears_only_its_own_draft_including_noop_plans(
    tmp_path: Path, custom_map_engine: Engine, already_published: bool
) -> None:
    mapper, _ = service(tmp_path, custom_map_engine)
    if already_published:
        mapper.apply(mapper.create_plan("generated"))
    own = draft_marker(mapper, "generated")
    other = draft_marker(mapper, "other-map")
    assert mapper.has_draft("generated") and mapper.has_draft("other-map")
    plan = mapper.create_plan("generated")
    assert (plan.changes == 0) is already_published

    mapper.apply(plan)

    assert not mapper.has_draft("generated") and not own.exists()
    assert mapper.has_draft("other-map") and other.read_text() == "unpublished"
    assert mapper.create_plan("generated").changes == 0


@pytest.mark.parametrize("changed", ["config", "source", "database"])
def test_stale_noop_publish_rechecks_freshness_and_retains_its_draft(
    tmp_path: Path, custom_map_engine: Engine, changed: str
) -> None:
    mapper, topology_path = service(tmp_path, custom_map_engine)
    mapper.apply(mapper.create_plan("generated"))
    marker = draft_marker(mapper, "generated")
    plan = mapper.create_plan("generated")
    assert plan.changes == 0
    if changed == "config":
        mapper.config.maps[0].node_defaults.size += 1
    elif changed == "source":
        write_topology(topology_path, include_b=False)
    else:
        with custom_map_engine.begin() as connection:
            connection.execute(text("UPDATE custom_maps SET height = '999'"))
    before = mapper.repository.snapshot("Generated", 1).fingerprint()

    with pytest.raises(RuntimeError, match="changed after this plan was generated"):
        mapper.apply(plan)

    assert mapper.has_draft("generated") and marker.read_text() == "unpublished"
    assert mapper.repository.snapshot("Generated", 1).fingerprint() == before


def test_failed_publish_rolls_back_and_retains_its_draft(
    tmp_path: Path, custom_map_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapper, _ = service(tmp_path, custom_map_engine)
    mapper.apply(mapper.create_plan("generated"))
    marker = draft_marker(mapper, "generated")
    mapper.config.maps[0].name = "Changed"
    plan = mapper.create_plan("generated")
    before = mapper.repository.snapshot("Generated", 1).fingerprint()

    def fail_after_map_update(*_: object) -> None:
        raise RuntimeError("forced publish failure")

    monkeypatch.setattr(mapper, "_apply_nodes", fail_after_map_update)

    with pytest.raises(RuntimeError, match="forced publish failure"):
        mapper.apply(plan)

    assert mapper.has_draft("generated") and marker.read_text() == "unpublished"
    assert mapper.repository.snapshot("Generated", 1).fingerprint() == before


def test_plan_display_names_preserve_ip_and_non_dns_labels(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    mapper, topology_path = service(tmp_path, custom_map_engine)
    topology_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "devices": [
                    {"source_id": "ip", "management_ip": "192.0.2.1"},
                    {"source_id": "plain", "hostname": "router"},
                ],
            }
        )
    )

    plan = mapper.create_plan("generated")

    assert {item.display_name for item in plan.operations if item.kind == "node"} == {
        "192.0.2.1",
        "router",
    }


def test_stale_display_names_fallback_to_canonical_ids(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    mapper, topology_path = service(tmp_path, custom_map_engine)
    mapper.apply(mapper.create_plan("generated"))
    objects = mapper.state.objects("generated")
    stale_node = next(
        item
        for item in objects.values()
        if item["kind"] == "node" and item["canonical_id"].endswith(":b")
    )
    stale_edge = next(item for item in objects.values() if item["kind"] == "edge")
    with mapper.state.transaction() as connection:
        connection.execute(
            "UPDATE managed_objects SET generated_json = '{}' WHERE map_key = ?",
            ("generated",),
        )
    with custom_map_engine.begin() as connection:
        connection.execute(
            text("UPDATE custom_map_nodes SET label = '' WHERE custom_map_node_id = :id"),
            {"id": stale_node["librenms_id"]},
        )
    write_topology(topology_path, include_b=False)

    plan = mapper.create_plan("generated")

    assert (
        next(
            item for item in plan.operations if item.action == "delete" and item.kind == "node"
        ).display_name
        == stale_node["canonical_id"]
    )
    assert (
        next(
            item for item in plan.operations if item.action == "delete" and item.kind == "edge"
        ).display_name
        == stale_edge["canonical_id"]
    )


def test_layer_overrides_apply_after_manual_merge_with_first_match_winning(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "devices": [
                    {
                        "source_id": "inventory",
                        "hostname": "inventory.example.net",
                        "librenms_device_id": 1,
                    }
                ],
            }
        )
    )
    manual_path = tmp_path / "manual.yaml"
    manual_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "devices": [
                    {"source_id": "inventory", "hostname": "inventory.example.net"},
                    {"source_id": "manual", "hostname": "manual.example.net"},
                ],
                "links": [
                    {
                        "source_id": "manual-link",
                        "a": {"device": "inventory"},
                        "b": {"device": "manual"},
                    }
                ],
            }
        )
    )
    config_path = tmp_path / "libremapper.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "state_path": str(tmp_path / "state.sqlite3"),
                "providers": {
                    "inventory": {
                        "type": "file",
                        "settings": {"path": str(inventory_path)},
                    },
                    "manual": {
                        "type": "file",
                        "settings": {"path": str(manual_path)},
                    },
                },
                "manual_provider": "manual",
                "maps": [
                    {
                        "key": "overridden",
                        "name": "Overridden",
                        "sources": [{"provider": "inventory"}],
                        "layer_overrides": [
                            {"type": "hostname", "value": "^manual", "layer": -10},
                            {"type": "hostname", "value": ".*", "layer": 10},
                        ],
                        "layout": {
                            "type": "top",
                            "width": 400,
                            "height": 600,
                            "margin": 100,
                            "layer_spacing": 200,
                            "center_layer": 0,
                        },
                    }
                ],
            }
        )
    )
    config = load_config(config_path)
    mapper = LibremapperService(config, custom_map_engine, StateStore(config.state_path))

    desired = mapper.build_desired(config.maps[0])
    y_by_label = {node.label: node.y_pos for node in desired.nodes.values()}

    assert y_by_label == {
        "inventory.example.net": 500,
        "manual.example.net": 100,
    }


def test_stale_node_is_retained_when_an_unmanaged_edge_references_it(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    mapper, topology_path = service(tmp_path, custom_map_engine)
    mapper.apply(mapper.create_plan("generated"))
    objects = mapper.state.show("generated")["objects"]
    node_ids = {
        item["canonical_id"]: item["librenms_id"] for item in objects if item["kind"] == "node"
    }
    b_id = next(value for key, value in node_ids.items() if key.endswith(":b"))

    with custom_map_engine.begin() as connection:
        manual_id = int(
            connection.execute(
                text(
                    """
                    INSERT INTO custom_map_nodes (
                        custom_map_id, device_id, linked_custom_map_id, label, style,
                        icon, image, node_image_id, size, border_width, text_face,
                        text_size, text_colour, colour_bg, colour_bdr, x_pos, y_pos
                    ) VALUES (
                        1, NULL, NULL, 'Manual', 'box', NULL, '', NULL, 25, 1,
                        'arial', 14, '#343434', '#ffffff', '#000000', 10, 10
                    )
                    """
                )
            ).lastrowid
        )
        connection.execute(
            text(
                """
                INSERT INTO custom_map_edges (
                    custom_map_id, custom_map_node1_id, custom_map_node2_id,
                    port_id, reverse, style, showpct, showbps, label, fixed_width,
                    text_face, text_size, text_colour, text_align, mid_x, mid_y
                ) VALUES (
                    1, :managed, :manual, NULL, 0, 'dynamic', 0, 0, 'Manual',
                    NULL, 'arial', 12, '#343434', 'horizontal', 20, 20
                )
                """
            ),
            {"managed": b_id, "manual": manual_id},
        )

    write_topology(topology_path, include_b=False)
    plan = mapper.create_plan("generated")

    assert any("retained stale managed node" in warning for warning in plan.warnings)
    assert not any(
        item.action == "delete" and item.kind == "node" and item.librenms_id == b_id
        for item in plan.operations
    )
    mapper.apply(plan)

    with custom_map_engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM custom_map_edges "
                    "WHERE custom_map_node1_id = :id OR custom_map_node2_id = :id"
                ),
                {"id": b_id},
            ).scalar_one()
            == 1
        )


def manual_service(tmp_path: Path, engine: Engine) -> tuple[LibremapperService, Path, dict]:
    from libremapper.config import ProviderConfig

    mapper, path = service(tmp_path, engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE ports (port_id INTEGER PRIMARY KEY, "
            "device_id INTEGER, deleted INTEGER, ifName TEXT)"
        )
        connection.exec_driver_sql("INSERT INTO ports VALUES (101, 1, 0, 'Ethernet1')")
    original = yaml.safe_load(path.read_text())
    original["devices"][0]["interfaces"][0]["librenms_port_id"] = 101
    path.write_text(yaml.safe_dump(original))
    manual_path = tmp_path / "manual.yaml"
    manual_path.write_text(
        yaml.safe_dump(
            {
                "devices": [
                    {"source_id": name, "librenms_device_id": device_id}
                    for name, device_id in [("a", 1), ("b", 2)]
                ],
                "links": [
                    {
                        "source_id": "guided:old:ab",
                        "protocol": "manual",
                        "relationship_kind": "logical",
                        "a": {"device": "a"},
                        "b": {"device": "b"},
                    }
                ],
            }
        )
    )
    mapper.config.manual_provider = "manual"
    mapper.config.providers["manual"] = ProviderConfig(
        type="file", settings={"path": str(manual_path)}
    )
    return mapper, path, original


@pytest.mark.parametrize("excluded_id", [2, 3])
def test_device_exclusions_remove_discovered_and_manual_added_devices_after_federation(
    tmp_path: Path, custom_map_engine: Engine, excluded_id: int
) -> None:
    mapper, _, _ = manual_service(tmp_path, custom_map_engine)
    manual_path = tmp_path / "manual.yaml"
    manual = yaml.safe_load(manual_path.read_text())
    manual["devices"].append({"source_id": "c", "librenms_device_id": 3})
    manual["links"].append(
        {
            "source_id": "manual-ac",
            "protocol": "manual",
            "relationship_kind": "logical",
            "a": {"device": "a"},
            "b": {"device": "c"},
        }
    )
    manual_path.write_text(yaml.safe_dump(manual))
    original_manual = manual_path.read_bytes()
    first = mapper.create_plan("generated")
    assert {item.status for item in first.link_fallbacks} == {"active", "corroborated"}
    assert {item.values["device_id"] for item in first.operations if item.kind == "node"} == {
        1,
        2,
        3,
    }
    mapper.apply(first)

    mapper.config.maps[0].device_exclusions = [excluded_id]
    desired = mapper.build_desired(mapper.config.maps[0])
    assert {node.device_id for node in desired.nodes.values()} == {1, 2, 3} - {excluded_id}
    assert len(desired.edges) == len(desired.link_fallbacks) == 1
    assert all(
        edge.from_node in desired.nodes and edge.to_node in desired.nodes
        for edge in desired.edges.values()
    )
    assert all(
        fallback.a_device in desired.nodes and fallback.b_device in desired.nodes
        for fallback in desired.link_fallbacks
    )
    plan = mapper.create_plan("generated")
    removed = [item for item in plan.operations if item.action == "delete"]
    assert sorted(item.kind for item in removed) == ["edge", "node"]
    assert next(item for item in removed if item.kind == "node").canonical_id not in desired.nodes
    mapper.apply(plan)
    assert mapper.create_plan("generated").changes == 0
    assert manual_path.read_bytes() == original_manual


@pytest.mark.parametrize(
    ("requested", "infer_layers", "resolved"),
    [("top", False, "top"), ("auto", True, "force")],
)
def test_reviewed_plan_roundtrips_layout_summary_and_accepts_legacy_plans(
    tmp_path: Path,
    custom_map_engine: Engine,
    requested: str,
    infer_layers: bool,
    resolved: str,
) -> None:
    mapper, _ = service(tmp_path, custom_map_engine)
    mapper.config.maps[0].layout = mapper.config.maps[0].layout.model_copy(
        update={"type": requested, "infer_layers": infer_layers}
    )

    plan = mapper.create_plan("generated")

    assert plan.layout_summary == {
        "requested": requested,
        "resolved": resolved,
        "width": 800,
        "height": 600,
        "hierarchy_inferred": infer_layers,
    }
    restored = Plan.model_validate_json(plan.model_dump_json())
    assert restored.layout_summary == plan.layout_summary
    assert restored.source_hash == plan.source_hash
    legacy = plan.model_dump(mode="json")
    del legacy["layout_summary"]
    assert Plan.model_validate(legacy).layout_summary == {}


@pytest.mark.parametrize(
    ("layout_type", "infer_layers"),
    [
        ("auto", False),
        ("auto", True),
        ("top", True),
        ("left", True),
        ("radial", True),
        ("force", True),
    ],
)
def test_automatic_and_inferred_layouts_ignore_saved_layers_without_deleting_them(
    tmp_path: Path, custom_map_engine: Engine, layout_type: str, infer_layers: bool
) -> None:
    mapper, _ = service(tmp_path, custom_map_engine)
    map_config = mapper.config.maps[0]
    map_config.layout = map_config.layout.model_copy(
        update={"type": layout_type, "infer_layers": infer_layers}
    )
    map_config.sources[0].selectors = [SelectorConfig(type="hostname", value=".*", layer=9)]
    inferred = mapper.build_desired(map_config)
    assert set(inferred.node_layers.values()) == {0}
    assert inferred.layout_summary["hierarchy_inferred"] is True

    map_config.layer_overrides = [SelectorConfig(type="device_id", value=1, layer=3)]
    saved = map_config.model_dump()
    automatic = mapper.build_desired(map_config)
    assert automatic.nodes == inferred.nodes
    assert automatic.node_layers == inferred.node_layers
    assert automatic.layout_summary == inferred.layout_summary
    assert map_config.model_dump() == saved
    reviewed = mapper.create_plan("generated")
    assert {item.layer for item in reviewed.operations if item.kind == "node"} == {0}

    map_config.layout = map_config.layout.model_copy(update={"type": "top", "infer_layers": False})
    placed = mapper.build_desired(map_config)
    layers = {
        node.device_id: placed.node_layers[canonical_id]
        for canonical_id, node in placed.nodes.items()
    }
    assert layers == {1: 3, 2: 9}
    assert placed.layout_summary["resolved"] == "top"
    positions = {node.device_id: node.y_pos for node in placed.nodes.values()}
    assert positions[1] < positions[2]
    reviewed = mapper.create_plan("generated")
    assert {item.layer for item in reviewed.operations if item.kind == "node"} == {3, 9}


def test_empty_review_does_not_claim_hierarchy_was_inferred(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    mapper, topology_path = service(tmp_path, custom_map_engine)
    topology_path.write_text("devices: []\nlinks: []\n")
    mapper.config.maps[0].layout = mapper.config.maps[0].layout.model_copy(
        update={"type": "auto", "infer_layers": True}
    )

    plan = mapper.create_plan("generated")

    assert plan.layout_summary["hierarchy_inferred"] is False
    assert not any(item.kind in {"node", "edge"} for item in plan.operations)


@pytest.mark.parametrize("legacy", [False, True])
def test_manual_discovered_rebinding_preserves_id_style_and_provenance(
    tmp_path: Path, custom_map_engine: Engine, legacy: bool
) -> None:
    mapper, path, discovered = manual_service(tmp_path, custom_map_engine)
    path.write_text(yaml.safe_dump({**discovered, "links": []}))
    first = mapper.create_plan("generated")
    assert first.link_fallbacks[0].status == "active"
    mapper.apply(first)
    record = next(iter(mapper.state.objects("generated", "edge").values()))
    edge_id = record["librenms_id"]
    if legacy:
        with mapper.state.transaction() as connection:
            connection.execute(
                "UPDATE managed_objects SET metadata_json = '{}' WHERE kind = 'edge'"
            )
    with custom_map_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE custom_map_edges SET style = 'straight', label = 'operator label', "
                "mid_x = 567 WHERE custom_map_edge_id = :id"
            ),
            {"id": edge_id},
        )
    path.write_text(yaml.safe_dump(discovered))
    upgraded = mapper.create_plan("generated")
    edge = next(operation for operation in upgraded.operations if operation.kind == "edge")
    assert edge.action == "update"
    assert edge.values["port_id"] == 101
    assert edge.librenms_id == edge_id
    assert edge.previous_canonical_id == record["canonical_id"]
    assert edge.reconciliation_reason == "manual fallback upgraded to external LLDP"
    assert {"style", "label", "mid_x"} <= set(edge.preserved_fields)
    assert upgraded.link_fallbacks[0].status == "corroborated"
    mapper.apply(upgraded)
    mapper.state = StateStore(mapper.state.path)
    assert mapper.create_plan("generated").changes == 0
    records = mapper.state.objects("generated", "edge")
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record["metadata"]["protocol"] == "lldp"
    mapper.config.maps[0].manual_link_exclusions = ["guided:old:ab"]
    path.write_text(yaml.safe_dump({**discovered, "links": []}))
    missing = mapper.create_plan("generated")
    removed_discovery = next(item for item in missing.operations if item.kind == "edge")
    assert removed_discovery.action == "delete" and removed_discovery.protocol == "lldp"
    assert {source.provider for source in removed_discovery.sources} == {"manual", "external"}
    mapper.config.maps[0].manual_link_exclusions = []
    assert {source["provider"] for source in record["metadata"]["sources"]} == {
        "manual",
        "external",
    }
    path.write_text(yaml.safe_dump({**discovered, "links": []}))
    restored = mapper.create_plan("generated")
    edge = next(operation for operation in restored.operations if operation.kind == "edge")
    assert edge.librenms_id == edge_id and edge.previous_canonical_id == record["canonical_id"]
    assert edge.reconciliation_reason == "discovery unavailable; manual fallback restored"
    assert edge.values["port_id"] is None
    mapper.apply(restored)
    assert mapper.create_plan("generated").changes == 0
    with custom_map_engine.connect() as connection:
        row = connection.execute(
            text("SELECT style, label, mid_x FROM custom_map_edges WHERE custom_map_edge_id = :id"),
            {"id": edge_id},
        ).one()
    assert tuple(row) == ("straight", "operator label", 567)
    mapper.config.maps[0].manual_link_exclusions = ["guided:old:ab"]
    excluded = mapper.create_plan("generated")
    assert excluded.link_fallbacks == []
    removed = next(operation for operation in excluded.operations if operation.kind == "edge")
    assert removed.action == "delete" and removed.protocol == "manual"
    assert [source.provider for source in removed.sources] == ["manual"]
    assert "no longer selected" in (removed.reconciliation_reason or "")
    assert len(yaml.safe_load((tmp_path / "manual.yaml").read_text())["links"]) == 1


def test_parallel_replacement_and_duplicate_cleanup_are_explicit(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    mapper, path, discovered = manual_service(tmp_path, custom_map_engine)
    path.write_text(yaml.safe_dump({**discovered, "links": []}))
    mapper.apply(mapper.create_plan("generated"))
    fallback_record = next(iter(mapper.state.objects("generated", "edge").values()))
    for device in discovered["devices"]:
        device["interfaces"].append(
            {
                "source_id": device["source_id"] + "-parallel",
                "device_source_id": device["source_id"],
                "name": "Ethernet3",
            }
        )
    discovered["links"].append(
        {
            "source_id": "parallel",
            "protocol": "lldp",
            "a": {"device": "a", "interface": "a-parallel"},
            "b": {"device": "b", "interface": "b-parallel"},
        }
    )
    path.write_text(yaml.safe_dump(discovered))
    parallel = mapper.create_plan("generated")
    edges = [operation for operation in parallel.operations if operation.kind == "edge"]
    assert sorted(edge.action for edge in edges) == ["create", "create", "delete"]
    assert all(edge.previous_canonical_id is None for edge in edges)
    assert next(edge for edge in edges if edge.action == "delete").reconciliation_reason == (
        "manual fallback replaced by discovered edges; remove/create required"
    )
    assert parallel.link_fallbacks[0].status == "parallel-covered"
    assert all(
        [source.provider for source in edge.sources] == ["external"]
        for edge in edges
        if edge.action == "create"
    )
    mapper.apply(parallel)
    assert mapper.create_plan("generated").changes == 0
    with custom_map_engine.begin() as connection:
        duplicate_id = mapper.repository.create_edge(connection, 1, fallback_record["generated"])
    with mapper.state.transaction() as connection:
        StateStore.save_object(
            connection,
            "generated",
            "edge",
            fallback_record["canonical_id"],
            duplicate_id,
            fallback_record["generated"],
            "legacy",
        )
    cleanup = mapper.create_plan("generated")
    deleted = next(
        operation
        for operation in cleanup.operations
        if operation.kind == "edge" and operation.action == "delete"
    )
    assert "duplicate fallback removed; discovered edge retained" in deleted.reconciliation_reason
    assert "legacy ownership record has no provenance" in deleted.reconciliation_reason
    mapper.apply(cleanup)
    assert mapper.create_plan("generated").changes == 0


def test_ownership_metadata_migrates_legacy_state_without_losing_records(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    state = StateStore(path)
    with state.transaction() as connection:
        StateStore.save_map(connection, "map", "Map", 1)
        StateStore.save_object(connection, "map", "edge", "ab", 2, {"port_id": 3}, "hash")
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE managed_objects DROP COLUMN metadata_json")
    migrated = StateStore(path).objects("map")["edge:ab"]
    assert migrated["librenms_id"] == 2
    assert migrated["generated"] == {"port_id": 3}
    assert migrated["metadata"] == {}


def test_provenance_only_changes_are_applied_and_then_idempotent(
    tmp_path: Path, custom_map_engine: Engine
) -> None:
    from libremapper.config import ProviderConfig, SourceConfig

    mapper, path = service(tmp_path, custom_map_engine)
    mapper.apply(mapper.create_plan("generated"))
    mapper.config.providers["corroborating"] = ProviderConfig(
        type="file", settings={"path": str(path)}
    )
    mapper.config.maps[0].sources.append(SourceConfig(provider="corroborating"))
    plan = mapper.create_plan("generated")
    edge = next(operation for operation in plan.operations if operation.kind == "edge")
    assert edge.action == "update" and edge.values == {}
    assert edge.reconciliation_reason == "ownership provenance updated"
    mapper.apply(plan)
    record = next(iter(mapper.state.objects("generated", "edge").values()))
    assert {source["provider"] for source in record["metadata"]["sources"]} == {
        "external",
        "corroborating",
    }
    assert mapper.create_plan("generated").changes == 0


def test_draft_markers_use_shared_runtime_with_custom_state_path(
    tmp_path: Path, custom_map_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapper, _ = service(tmp_path, custom_map_engine)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LIBREMAPPER_DATA_ROOT", str(runtime))
    marker = runtime / "drafts" / f"{hashlib.sha256(b'generated').hexdigest()}.pending"
    marker.parent.mkdir(parents=True)
    marker.write_text("draft\n")
    assert mapper.has_draft("generated")
    mapper.apply(mapper.create_plan("generated"))
    assert not marker.exists()
