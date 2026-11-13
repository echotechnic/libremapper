import math

from libremapper.config import LayoutConfig
from libremapper.layouts import generate_layout, infer_device_layers
from libremapper.models import FederatedDevice, FederatedLink, FederatedTopology


def topology() -> FederatedTopology:
    return FederatedTopology(
        devices={
            "a": FederatedDevice(canonical_id="a", layer=0),
            "b": FederatedDevice(canonical_id="b", layer=1),
            "c": FederatedDevice(canonical_id="c", layer=1),
        },
        links={
            "ab": FederatedLink(canonical_id="ab", a_device="a", b_device="b"),
            "ac": FederatedLink(canonical_id="ac", a_device="a", b_device="c"),
        },
    )


def test_force_layout_is_deterministic_and_bounded() -> None:
    config = LayoutConfig(type="force", width=1000, height=600, margin=50, seed=7)

    first = generate_layout(topology(), config)
    second = generate_layout(topology(), config)

    assert first == second
    assert (first.width, first.height) == (1000, 600)
    assert all(50 <= point.x <= 950 for point in first.positions.values())
    assert all(50 <= point.y <= 550 for point in first.positions.values())


def test_legacy_layouts_place_every_node() -> None:
    for layout_type in ("top", "left", "radial"):
        config = LayoutConfig(
            type=layout_type,
            width=300,
            height=240,
            margin=20,
            layer_spacing=500,
            node_spacing=500,
            radius=500,
        )
        result = generate_layout(topology(), config)
        positions = result.positions
        assert set(positions) == {"a", "b", "c"}
        assert result == generate_layout(topology(), config)
        assert all(20 <= point.x <= 280 for point in positions.values())
        assert all(20 <= point.y <= 220 for point in positions.values())

        if layout_type == "top":
            assert positions["a"].y < positions["b"].y == positions["c"].y
        elif layout_type == "left":
            assert positions["a"].x < positions["b"].x == positions["c"].x


def centered_topology() -> FederatedTopology:
    layers = {
        "external": -20,
        "aggregation": -10,
        "core-a": 0,
        "core-b": 0,
        "edge-a": 10,
        "edge-b": 10,
        "edge-c": 10,
    }
    return FederatedTopology(
        devices={
            name: FederatedDevice(canonical_id=name, layer=layer) for name, layer in layers.items()
        },
        links={
            name: FederatedLink(canonical_id=name, a_device=a, b_device=b)
            for name, a, b in (
                ("external-aggregation", "external", "aggregation"),
                ("aggregation-core-a", "aggregation", "core-a"),
                ("aggregation-core-b", "aggregation", "core-b"),
                ("core-a-edge-a", "core-a", "edge-a"),
                ("core-a-edge-b", "core-a", "edge-b"),
                ("core-b-edge-c", "core-b", "edge-c"),
            )
        },
    )


def test_auto_grow_preserves_spacing_margins_and_center_layer() -> None:
    result = generate_layout(
        centered_topology(),
        LayoutConfig(
            type="top",
            width=120,
            height=100,
            margin=20,
            layer_spacing=100,
            node_spacing=60,
            auto_grow=True,
            center_layer=0,
        ),
    )

    assert (result.width, result.height) == (160, 440)
    assert result.positions["external"].y == 20
    assert result.positions["aggregation"].y == 120
    assert result.positions["core-a"].y == result.positions["core-b"].y == 220
    assert result.positions["edge-a"].y == 320
    assert sorted(result.positions[name].x for name in ("edge-a", "edge-b", "edge-c")) == [
        20,
        80,
        140,
    ]


def test_radial_uses_circular_signed_rings_and_square_auto_growth() -> None:
    result = generate_layout(
        centered_topology(),
        LayoutConfig(
            type="radial",
            width=300,
            height=180,
            margin=20,
            radius=50,
            auto_grow=True,
            center_layer=0,
        ),
    )
    center = (result.width // 2, result.height // 2)

    def radius(device_id: str) -> int:
        point = result.positions[device_id]
        return round(math.hypot(point.x - center[0], point.y - center[1]))

    assert (result.width, result.height) == (340, 340)
    assert {radius("core-a"), radius("core-b")} == {50}
    assert {radius("aggregation"), radius("edge-a"), radius("edge-b"), radius("edge-c")} == {100}
    assert radius("external") == 150


def test_constrained_layout_projects_back_to_shapes_with_minimum_separation() -> None:
    base = LayoutConfig(
        type="top",
        width=120,
        height=100,
        margin=20,
        layer_spacing=100,
        node_spacing=60,
        auto_grow=True,
        center_layer=0,
    )
    packed = generate_layout(centered_topology(), base)
    constrained = generate_layout(
        centered_topology(), base.model_copy(update={"separation": "constrained"})
    )

    assert constrained == generate_layout(
        centered_topology(), base.model_copy(update={"separation": "constrained"})
    )
    for layer in {-20, -10, 0, 10}:
        devices = [
            device_id
            for device_id, device in centered_topology().devices.items()
            if device.layer == layer
        ]
        assert {constrained.positions[item].y for item in devices} == {
            packed.positions[item].y for item in devices
        }
        coordinates = sorted(constrained.positions[item].x for item in devices)
        assert all(
            right - left >= 60 for left, right in zip(coordinates, coordinates[1:], strict=False)
        )


def test_constrained_radial_auto_grows_for_minimum_node_separation() -> None:
    topology = FederatedTopology(
        devices={
            "core": FederatedDevice(canonical_id="core", layer=0),
            **{
                f"edge-{index}": FederatedDevice(canonical_id=f"edge-{index}", layer=10)
                for index in range(12)
            },
        }
    )
    result = generate_layout(
        topology,
        LayoutConfig(
            type="radial",
            width=100,
            height=100,
            margin=10,
            radius=10,
            node_spacing=50,
            auto_grow=True,
            center_layer=0,
            separation="constrained",
        ),
    )
    ring = [result.positions[f"edge-{index}"] for index in range(12)]
    distances = [
        math.dist((left.x, left.y), (right.x, right.y))
        for index, left in enumerate(ring)
        for right in ring[index + 1 :]
    ]

    assert result.width == result.height > 100
    assert min(distances) >= 49


def test_layer_margins_evenly_distribute_constrained_slots_by_orientation() -> None:
    layered = FederatedTopology(
        devices={
            **{
                f"inner-{index}": FederatedDevice(canonical_id=f"inner-{index}", layer=0)
                for index in range(3)
            },
            **{
                f"outer-{index}": FederatedDevice(canonical_id=f"outer-{index}", layer=10)
                for index in range(2)
            },
        }
    )
    for layout_type, width, height in (("top", 400, 300), ("left", 300, 400)):
        config = LayoutConfig(
            type=layout_type,
            width=width,
            height=height,
            margin=20,
            layer_spacing=100,
            node_spacing=60,
            separation="constrained",
            layer_margins={0: (10, 30), 10: (100, 100)},
        )
        result = generate_layout(layered, config)
        inner = (
            [result.positions[f"inner-{index}"].x for index in range(3)]
            if layout_type == "top"
            else [result.positions[f"inner-{index}"].y for index in range(3)]
        )
        outer = (
            [result.positions[f"outer-{index}"].x for index in range(2)]
            if layout_type == "top"
            else [result.positions[f"outer-{index}"].y for index in range(2)]
        )

        assert result == generate_layout(layered, config)
        assert sorted(inner) == [10, 190, 370]
        assert sorted(outer) == [100, 300]


def branching_topology(*, reverse: bool = False) -> FederatedTopology:
    layers = {
        "root-a": 0,
        "root-b": 0,
        "branch-a": 10,
        "branch-b": 10,
        "a-1": 20,
        "a-2": 20,
        "a-deep": 20,
        "b-deep": 20,
        "b-1": 20,
        "b-2": 20,
    }
    links = [
        ("root-a-branch-a", "root-a", "branch-a"),
        ("branch-a-deep", "branch-a", "a-deep"),
        ("root-b-branch-b", "root-b", "branch-b"),
        ("branch-b-deep", "branch-b", "b-deep"),
        ("root-a-a-1", "root-a", "a-1"),
        ("root-a-a-2", "root-a", "a-2"),
        ("root-b-b-1", "root-b", "b-1"),
        ("root-b-b-2", "root-b", "b-2"),
    ]
    devices = list(layers.items())
    if reverse:
        devices.reverse()
        links.reverse()
    return FederatedTopology(
        devices={name: FederatedDevice(canonical_id=name, layer=layer) for name, layer in devices},
        links={
            name: FederatedLink(canonical_id=name, a_device=a, b_device=b) for name, a, b in links
        },
    )


def test_center_out_order_keeps_branches_contiguous_and_deep_nodes_centered() -> None:
    result = generate_layout(
        branching_topology(),
        LayoutConfig(
            type="top",
            width=1000,
            height=600,
            margin=50,
            layer_spacing=200,
            node_spacing=100,
            center_layer=0,
            separation="constrained",
        ),
    )
    customers = sorted(
        (device_id for device_id in result.positions if device_id.startswith(("a-", "b-"))),
        key=lambda device_id: result.positions[device_id].x,
    )

    assert customers == ["a-1", "a-2", "a-deep", "b-deep", "b-1", "b-2"]


def test_center_root_ties_use_stable_canonical_indexes() -> None:
    config = LayoutConfig(
        type="left",
        width=600,
        height=1000,
        margin=50,
        layer_spacing=200,
        node_spacing=100,
        center_layer=0,
        separation="constrained",
    )

    assert generate_layout(branching_topology(), config) == generate_layout(
        branching_topology(reverse=True), config
    )


def test_radial_projects_contiguous_branch_order() -> None:
    config = LayoutConfig(
        type="radial",
        width=800,
        height=800,
        margin=50,
        radius=100,
        center_layer=0,
        separation="constrained",
    )
    result = generate_layout(branching_topology(), config)
    center = (result.width / 2, result.height / 2)
    customers = sorted(
        (device_id for device_id in result.positions if device_id.startswith(("a-", "b-"))),
        key=lambda device_id: math.atan2(
            result.positions[device_id].y - center[1],
            result.positions[device_id].x - center[0],
        ),
    )
    branches = [device_id[0] for device_id in customers]
    changes = sum(
        left != right for left, right in zip(branches, branches[1:] + branches[:1], strict=True)
    )

    assert changes == 2
    assert result == generate_layout(branching_topology(reverse=True), config)


def inferred_topology(*, reverse: bool = False) -> FederatedTopology:
    devices = ["core", "a", "b", "c", "logical-a", "logical-b", "logical-c", "isolated"]
    links = [
        ("core", "a", "direct"),
        ("core", "b", "direct"),
        ("core", "c", "direct"),
        ("a", "b", "logical"),
        ("logical-a", "logical-b", "logical"),
        ("logical-b", "logical-c", "logical"),
    ]
    if reverse:
        devices.reverse()
        links.reverse()
    return FederatedTopology(
        devices={name: FederatedDevice(canonical_id=name, layer=20) for name in devices},
        links={
            f"{a}-{b}": FederatedLink(
                canonical_id=f"{a}-{b}",
                a_device=a,
                b_device=b,
                attributes={"relationship_kind": kind},
            )
            for a, b, kind in links
        },
    )


def test_inferred_layers_prefer_direct_evidence_and_put_isolates_last() -> None:
    graph = inferred_topology()
    infer_device_layers(graph)

    assert {name: device.layer for name, device in graph.devices.items()} == {
        "core": 0,
        "a": 1,
        "b": 1,
        "c": 1,
        "logical-a": 1,
        "logical-b": 0,
        "logical-c": 1,
        "isolated": 2,
    }
    reversed_graph = inferred_topology(reverse=True)
    infer_device_layers(reversed_graph)
    assert {name: device.layer for name, device in graph.devices.items()} == {
        name: device.layer for name, device in reversed_graph.devices.items()
    }


def test_automatic_hierarchy_is_deterministic_and_ignores_saved_layers() -> None:
    config = LayoutConfig(type="auto", infer_layers=True, width=100, height=100, auto_grow=True)
    graph = inferred_topology()
    reversed_graph = inferred_topology(reverse=True)
    for selected in (graph, reversed_graph):
        infer_device_layers(selected)
        selected.devices["a"].layer = 3
    result = generate_layout(graph, config)

    assert result == generate_layout(reversed_graph, config)
    assert (result.requested_layout, result.resolved_layout, result.hierarchy_inferred) == (
        "auto",
        "top",
        True,
    )
    assert result.positions["core"].y < result.positions["b"].y
    assert result.positions["a"].y == result.positions["b"].y
    assert result.positions["b"].y < result.positions["isolated"].y
    assert result.height > config.height


def test_automatic_flat_graph_uses_organic_layout() -> None:
    graph = FederatedTopology(
        devices={name: FederatedDevice(canonical_id=name) for name in ("a", "b", "c", "alone")},
        links={
            a + b: FederatedLink(canonical_id=a + b, a_device=a, b_device=b)
            for a, b in (("a", "b"), ("b", "c"), ("c", "a"))
        },
    )
    infer_device_layers(graph)
    config = LayoutConfig(type="auto", infer_layers=True)
    result = generate_layout(graph, config)

    assert {graph.devices[name].layer for name in ("a", "b", "c")} == {0}
    assert graph.devices["alone"].layer == 1
    assert result.resolved_layout == "force"
    assert result.positions == generate_layout(graph, LayoutConfig(type="force")).positions


def test_inference_handles_empty_and_isolated_graphs() -> None:
    for names in ([], ["a"], ["a", "b"]):
        graph = FederatedTopology(
            devices={name: FederatedDevice(canonical_id=name, layer=12) for name in names}
        )
        infer_device_layers(graph)
        result = generate_layout(graph, LayoutConfig(type="auto", infer_layers=True))

        assert all(device.layer == 0 for device in graph.devices.values())
        assert set(result.positions) == set(names)
        assert result.resolved_layout == "force"
        assert result.hierarchy_inferred == bool(names)


def test_automatic_ignores_isolated_device_layers_but_manual_layout_uses_them() -> None:
    graph = FederatedTopology(
        devices={name: FederatedDevice(canonical_id=name) for name in ("a", "b")}
    )
    config = LayoutConfig(type="auto", infer_layers=True)
    infer_device_layers(graph)
    organic = generate_layout(graph, config)
    graph.devices["b"].layer = 3
    automatic = generate_layout(graph, config)

    assert organic.resolved_layout == "force"
    assert automatic == organic
    graph.devices["b"].layer = 3
    placed = generate_layout(graph, LayoutConfig(type="top"))
    assert placed.resolved_layout == "top"
    assert placed.positions["a"].y < placed.positions["b"].y
    assert organic.positions != placed.positions


def test_organic_layout_is_stable_across_link_and_device_insertion_order() -> None:
    config = LayoutConfig(type="force")

    assert generate_layout(inferred_topology(), config) == generate_layout(
        inferred_topology(reverse=True), config
    )


def test_automatic_layout_ignores_persisted_center_and_numeric_layer_margins() -> None:
    graph = inferred_topology()
    infer_device_layers(graph)
    config = LayoutConfig(type="auto", infer_layers=True, auto_grow=True)
    legacy_settings = config.model_copy(
        update={"center_layer": 10, "layer_margins": {0: (500, 700), 1: (400, 100)}}
    )
    before = legacy_settings.model_dump()

    assert generate_layout(graph, legacy_settings) == generate_layout(graph, config)
    assert legacy_settings.model_dump() == before


def test_automatic_organic_layout_grows_canvas_without_changing_legacy_force() -> None:
    names = [f"node-{index}" for index in range(16)]
    graph = FederatedTopology(
        devices={name: FederatedDevice(canonical_id=name) for name in names},
        links={
            first: FederatedLink(canonical_id=first, a_device=first, b_device=second)
            for first, second in zip(names, names[1:] + names[:1], strict=True)
        },
    )
    config = LayoutConfig(
        type="auto", width=200, height=160, margin=20, node_spacing=120, auto_grow=True
    )
    result = generate_layout(graph, config)

    assert result.resolved_layout == "force"
    assert result.width > config.width and result.height > config.height
    assert (result.width - 2 * config.margin) * (result.height - 2 * config.margin) >= (
        len(names) * config.node_spacing**2
    )
    assert result == generate_layout(graph, config)
    legacy = generate_layout(graph, config.model_copy(update={"type": "force"}))
    assert (legacy.width, legacy.height) == (config.width, config.height)
    assert legacy == generate_layout(
        graph, config.model_copy(update={"type": "force", "auto_grow": False})
    )
    assert (config.width, config.height) == (200, 160)


def test_legacy_layout_reports_metadata_without_inference() -> None:
    graph = topology()
    result = generate_layout(graph, LayoutConfig(type="top"))

    assert (result.requested_layout, result.resolved_layout, result.hierarchy_inferred) == (
        "top",
        "top",
        False,
    )
    assert {name: device.layer for name, device in graph.devices.items()} == {
        "a": 0,
        "b": 1,
        "c": 1,
    }
