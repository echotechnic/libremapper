from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import yaml

from libremapper.config import load_config
from libremapper.layouts import generate_layout
from libremapper.models import FederatedDevice, FederatedTopology
from libremapper.providers.regex_artifacts import RegexArtifactSettings
from libremapper.service import _apply_layer_overrides


def test_synthetic_fixture_is_current() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run([str(root / "scripts/generate-test-fixture"), "--check"], check=True)

    fixture = root / "tests/fixtures/synthetic-core"
    data = json.loads((fixture / "topology.json").read_text())
    assert len(data["devices"]) == 43
    assert len(data["ports"]) == 97
    assert len(data["ip_addresses"]) == 26
    assert len(data["mac_adjacencies"]) == 20
    assert len(data["bgp_peers"]) == 16
    assert len(data["ospf_neighbors"]) == 4
    assert len(data["groups"]) == 4
    assert len(data["reference_map"]["nodes"]) == 26
    assert len(data["reference_map"]["edges"]) == 31
    config = load_config(fixture / "libremapper.yaml")
    assert config.manual_provider == "manual"
    assert {map_config.key for map_config in config.maps} == {
        "generated-core-top",
        "generated-core-left",
        "generated-core-radial",
        "generated-core-reference-layout",
    }
    expected_selectors = {
        ("Reference Aggregation", -10),
        ("Reference Core", 0),
        ("Reference POP", 10),
        ("Reference Customer", 20),
    }
    assert all(
        {(selector.value, selector.layer) for selector in source.selectors} == expected_selectors
        for map_config in config.maps
        for source in map_config.sources
    )
    manual = yaml.safe_load((fixture / "manual-topology.yaml").read_text())
    assert len(manual["devices"]) == 26
    assert all(device["attributes"]["map_image"] for device in manual["devices"])
    inventory_hostnames = {device["hostname"] for device in data["devices"]}
    assert {
        device["hostname"]
        for device in manual["devices"]
        if str(device["source_id"]).startswith("d")
    } <= inventory_hostnames
    layers = {layer for _, layer in expected_selectors}
    layers.update(device.get("layer", 0) for device in manual["devices"])
    assert layers == {-20, -10, 0, 10, 20}

    maps = {map_config.key: map_config for map_config in config.maps}
    assert all(map_config.layout.separation == "constrained" for map_config in maps.values())
    assert all(map_config.edge_separation == 24 for map_config in maps.values())
    assert all(
        (map_config.width, map_config.height) == ("1600", "800")
        for map_config in maps.values()
    )
    assert all(
        (
            map_config.layout.width,
            map_config.layout.height,
            map_config.layout.margin,
            map_config.layout.layer_spacing,
            map_config.layout.node_spacing,
            map_config.layout.radius,
            map_config.layout.auto_grow,
        )
        == (1600, 800, 60, 180, 120, 180, False)
        for map_config in maps.values()
    )

    group_layers = {
        str(selector.value): selector.layer
        for selector in maps["generated-core-top"].sources[0].selectors
    }
    device_layers = {
        device_id: group_layers[group["name"]]
        for group in data["groups"]
        for device_id in group["device_ids"]
    }
    devices = {device["id"]: device for device in data["devices"]}
    manual_layers = {device["hostname"]: device.get("layer", 0) for device in manual["devices"]}
    fixture_topology = FederatedTopology(
        devices={
            str(index): FederatedDevice(
                canonical_id=str(index),
                hostname=(
                    devices[node["device_id"]]["hostname"]
                    if node["device_id"] is not None
                    else node["label"]
                ),
                layer=(
                    device_layers[node["device_id"]]
                    if node["device_id"] is not None
                    else manual_layers[node["label"]]
                ),
            )
            for index, node in enumerate(data["reference_map"]["nodes"])
        }
    )
    expected_dimensions = {key: (1600, 800) for key in maps}
    layouts = {}
    for key, dimensions in expected_dimensions.items():
        topology = fixture_topology.model_copy(deep=True)
        _apply_layer_overrides(topology, maps[key].layer_overrides)
        result = generate_layout(topology, maps[key].layout)
        assert (result.width, result.height) == dimensions
        assert result == generate_layout(topology, maps[key].layout)
        assert all(
            0 <= position.x <= result.width and 0 <= position.y <= result.height
            for position in result.positions.values()
        )
        layouts[key] = (topology, result)

    top_positions = layouts["generated-core-top"][1].positions
    left_positions = layouts["generated-core-left"][1].positions
    assert {position.y for position in top_positions.values()} == {60, 230, 400, 570, 740}
    assert {
        left_positions[device_id].x
        for device_id, device in layouts["generated-core-left"][0].devices.items()
        if device.hostname.startswith("cust")
    } == {440}

    radial_topology, radial_layout = layouts["generated-core-radial"]
    radial_center = (radial_layout.width // 2, radial_layout.height // 2)
    expected_rings = {
        85: {"Transit 01", "Transit 02", "OOB Provider 01"},
        170: {"oob01", "oob02", "oob03", "oob04", "border01", "border02"},
        255: {"core01", "core02", "pop01", "pop02"},
        340: {f"cust{index:02d}" for index in range(1, 14)},
    }
    actual_rings: dict[int, set[str]] = {}
    for device_id, position in radial_layout.positions.items():
        radius = round(
            ((position.x - radial_center[0]) ** 2 + (position.y - radial_center[1]) ** 2)
            ** 0.5
        )
        expected_radius = min(expected_rings, key=lambda item: abs(item - radius))
        assert abs(expected_radius - radius) <= 1
        actual_rings.setdefault(expected_radius, set()).add(
            radial_topology.devices[device_id].hostname.split(".", 1)[0]
        )
    assert actual_rings == expected_rings

    reference = fixture_topology.model_copy(deep=True)
    reference_map = maps["generated-core-reference-layout"]
    _apply_layer_overrides(reference, reference_map.layer_overrides)
    reference_layout = generate_layout(reference, reference_map.layout)
    columns = {
        -30: 260,
        -20: 440,
        -10: 620,
        0: 800,
        10: 980,
        20: 1160,
        30: 1340,
    }
    assert all(
        reference_layout.positions[device_id].x == columns[device.layer]
        for device_id, device in reference.devices.items()
    )
    provider_id = next(
        device_id
        for device_id, device in reference.devices.items()
        if device.hostname == "OOB Provider 01"
    )
    assert reference_layout.positions[provider_id].y == 40
    settings = RegexArtifactSettings.model_validate(config.providers["artifacts"].settings)
    pattern = settings.rules[0].compile()
    files = list((fixture / "artifacts/topology").glob("*"))
    assert len(files) == 9
    assert sum(len(list(pattern.finditer(path.read_text()))) for path in files) == 31

    reference_nodes = {node["id"]: node for node in data["reference_map"]["nodes"]}
    assert set(reference_nodes) == set(range(4001, 4028)) - {4008}
    assert (reference_nodes[4009]["x"], reference_nodes[4009]["y"]) == (900, 60)
    assert reference_nodes[4026]["x"] == reference_nodes[4024]["x"]
    assert reference_nodes[4026]["y"] < reference_nodes[4024]["y"]
    assert reference_nodes[4027]["x"] == reference_nodes[4015]["x"]
    assert reference_nodes[4027]["y"] < reference_nodes[4015]["y"]

    labels = {node_id: node["label"] for node_id, node in reference_nodes.items()}
    images = {label: reference_nodes[node_id]["image"] for node_id, label in labels.items()}
    assert {images[name] for name in ("Transit 01", "Transit 02", "OOB Provider 01")} == {"gtm.svg"}
    assert {images[name] for name in ("border01", "border02")} == {"firewall.svg"}
    assert {images[f"oob{index:02d}"] for index in range(1, 5)} == {"switch-l2.svg"}
    assert {images[name] for name in ("core01", "core02")} == {"router.svg"}
    assert {images[name] for name in ("pop01", "pop02")} == {"switch-l3.svg"}
    assert {images[f"cust{index:02d}"] for index in range(1, 14)} == {"router-square.svg"}

    reference_edges = data["reference_map"]["edges"]
    assert {edge["id"] for edge in reference_edges} == (
        set(range(5001, 5030)) - {5002, 5005}
    ) | set(range(5030, 5034))
    endpoint_counts = Counter(
        frozenset((labels[edge["node1"]], labels[edge["node2"]])) for edge in reference_edges
    )
    expected_edges = {
        frozenset(pair)
        for pair in (
            ("border01", "core01"),
            ("border02", "core02"),
            ("border01", "border02"),
            ("core01", "core02"),
            ("border01", "Transit 01"),
            ("border02", "Transit 02"),
            ("border01", "oob01"),
            ("core01", "oob01"),
            ("border02", "oob02"),
            ("core02", "oob02"),
            ("pop01", "oob03"),
            ("pop02", "oob04"),
            ("oob01", "OOB Provider 01"),
            ("oob02", "OOB Provider 01"),
            ("oob03", "OOB Provider 01"),
            ("oob04", "OOB Provider 01"),
            ("core01", "pop01"),
            ("core02", "pop02"),
            ("pop01", "cust10"),
            ("pop02", "cust13"),
            *(("core01", f"cust{index:02d}") for index in range(1, 9)),
            *(("core02", f"cust{index:02d}") for index in (9, 11, 12)),
        )
    }
    assert endpoint_counts == Counter({edge: 1 for edge in expected_edges})
    core_border_ports = {
        edge["port_id"]
        for edge in reference_edges
        if frozenset((labels[edge["node1"]], labels[edge["node2"]]))
        in {
            frozenset(("border01", "core01")),
            frozenset(("border02", "core02")),
        }
    }
    assert core_border_ports == {2004, 2007}
