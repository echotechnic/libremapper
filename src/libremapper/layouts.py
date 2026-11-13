from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace

import networkx as nx

from libremapper.config import LayoutConfig
from libremapper.models import FederatedTopology, Position


@dataclass(frozen=True)
class LayoutResult:
    positions: dict[str, Position]
    width: int
    height: int
    requested_layout: str = "force"
    resolved_layout: str = "force"
    hierarchy_inferred: bool = False


def infer_device_layers(topology: FederatedTopology) -> None:
    graph: nx.Graph[str] = nx.Graph()
    graph.add_nodes_from(sorted(topology.devices))
    graph.add_edges_from(
        sorted(
            (min(link.a_device, link.b_device), max(link.a_device, link.b_device))
            for link in topology.links.values()
            if link.a_device != link.b_device
            and link.a_device in topology.devices
            and link.b_device in topology.devices
        )
    )
    direct_edges = {
        frozenset((link.a_device, link.b_device))
        for link in topology.links.values()
        if link.attributes.get("relationship_kind") != "logical"
    }
    hierarchy: nx.Graph[str] = nx.Graph()
    hierarchy.add_nodes_from(graph)
    for component in nx.connected_components(graph):
        edges = list(graph.subgraph(component).edges())
        direct = [edge for edge in edges if frozenset(edge) in direct_edges]
        hierarchy.add_edges_from(direct or edges)

    layers: dict[str, int] = {}
    for component in nx.connected_components(hierarchy):
        if len(component) == 1:
            continue
        connected = hierarchy.subgraph(component)
        cores = nx.core_number(connected)
        highest_core = max(cores.values())
        candidates = [node for node in sorted(component) if cores[node] == highest_core]
        highest_degree = max(connected.degree(node) for node in candidates)
        roots = [node for node in candidates if connected.degree(node) == highest_degree]
        layers.update(
            (node, int(distance))
            for node, distance in nx.multi_source_dijkstra_path_length(
                connected, roots, weight=None
            ).items()
        )
    outer_layer = max(layers.values(), default=-1) + 1
    for node, device in topology.devices.items():
        device.layer = layers.get(node, outer_layer)


def generate_layout(topology: FederatedTopology, config: LayoutConfig) -> LayoutResult:
    requested = config.type
    inferred = config.infer_layers or requested == "auto"
    if inferred:
        infer_device_layers(topology)
    if requested == "auto":
        connected: set[str] = set()
        tiered: set[str] = set()
        for link in topology.links.values():
            if link.a_device not in topology.devices or link.b_device not in topology.devices:
                continue
            connected.update((link.a_device, link.b_device))
            if topology.devices[link.a_device].layer != topology.devices[link.b_device].layer:
                tiered.update((link.a_device, link.b_device))
        resolved = "top" if len(tiered) > len(connected) / 2 else "force"
        config = config.model_copy(
            update={
                "type": resolved,
                "separation": "constrained",
                "center_layer": None,
                "layer_margins": {},
            }
        )
        if resolved == "force" and config.auto_grow and topology.devices:
            usable_width = max(1, config.width - 2 * config.margin)
            usable_height = max(1, config.height - 2 * config.margin)
            scale = math.sqrt(
                len(topology.devices) * config.node_spacing**2 / (usable_width * usable_height)
            )
            if scale > 1:
                config = config.model_copy(
                    update={
                        "width": math.ceil(2 * config.margin + usable_width * scale),
                        "height": math.ceil(2 * config.margin + usable_height * scale),
                    }
                )
    if not topology.devices:
        result = LayoutResult({}, config.width, config.height)
    elif config.type == "force":
        result = LayoutResult(_force_layout(topology, config), config.width, config.height)
    else:
        result = _layer_layout(topology, config)
    return replace(
        result,
        requested_layout=requested,
        resolved_layout=config.type,
        hierarchy_inferred=inferred and bool(topology.devices),
    )


def _spring_layout(
    topology: FederatedTopology,
    config: LayoutConfig,
    positions: dict[str, Position] | None = None,
) -> dict[str, tuple[float, float]]:
    graph: nx.Graph[str] = nx.Graph()
    graph.add_nodes_from(sorted(topology.devices))
    graph.add_edges_from((link.a_device, link.b_device) for link in topology.links.values())
    initial = (
        {node_id: (float(point.x), float(point.y)) for node_id, point in positions.items()}
        if positions
        else None
    )
    result = nx.spring_layout(
        graph,
        pos=initial,
        seed=config.seed,
        iterations=config.iterations,
        scale=1.0,
        center=(0.0, 0.0),
    )
    return {node_id: (float(point[0]), float(point[1])) for node_id, point in result.items()}


def _force_layout(topology: FederatedTopology, config: LayoutConfig) -> dict[str, Position]:
    positions = _spring_layout(topology, config)
    usable_width = max(1, config.width - (2 * config.margin))
    usable_height = max(1, config.height - (2 * config.margin))
    return {
        node_id: Position(
            x=round((point[0] + 1.0) * usable_width / 2 + config.margin),
            y=round((point[1] + 1.0) * usable_height / 2 + config.margin),
        )
        for node_id, point in positions.items()
    }


def _layer_layout(topology: FederatedTopology, config: LayoutConfig) -> LayoutResult:
    layers: dict[int, list[str]] = defaultdict(list)
    for device in topology.devices.values():
        layers[device.layer].append(device.canonical_id)
    ordered_layers = sorted(layers)
    ranks = _layer_ranks(ordered_layers, config.center_layer)
    centered_order: (
        tuple[dict[int, list[str]], dict[str, tuple[int, ...]], dict[str, int]] | None
    ) = None
    if config.separation == "constrained" and config.center_layer is not None:
        centered_order = _center_out_order(
            topology, layers, ordered_layers, ranks, config.center_layer
        )
    ordered_devices = (
        centered_order[0] if centered_order else _ordered_devices(topology, layers, ordered_layers)
    )

    if config.type == "radial":
        return _radial_layout(
            topology,
            config,
            ordered_devices,
            ordered_layers,
            ranks,
            centered_order[1] if centered_order else None,
            centered_order[2] if centered_order else None,
        )
    return _linear_layout(topology, config, ordered_devices, ordered_layers, ranks)


def _layer_ranks(ordered_layers: list[int], center_layer: int | None) -> dict[int, int]:
    if center_layer is None:
        return {layer: rank for rank, layer in enumerate(ordered_layers)}
    lower = [layer for layer in ordered_layers if layer < center_layer]
    higher = [layer for layer in ordered_layers if layer > center_layer]
    ranks = {layer: rank - len(lower) for rank, layer in enumerate(lower)}
    ranks.update({layer: rank + 1 for rank, layer in enumerate(higher)})
    if center_layer in ordered_layers:
        ranks[center_layer] = 0
    return ranks


def _linear_layout(
    topology: FederatedTopology,
    config: LayoutConfig,
    ordered_devices: dict[int, list[str]],
    ordered_layers: list[int],
    ranks: dict[int, int],
) -> LayoutResult:
    densest = max(
        (
            len(devices)
            for layer, devices in ordered_devices.items()
            if layer not in config.layer_margins
        ),
        default=1,
    )
    cross_required = 2 * config.margin + max(0, densest - 1) * config.node_spacing
    cross_required = max(
        cross_required,
        max(
            (
                sum(config.layer_margins[layer])
                for layer in ordered_layers
                if layer in config.layer_margins
            ),
            default=0,
        ),
    )
    if config.center_layer is None:
        layer_required = 2 * config.margin + max(0, len(ordered_layers) - 1) * config.layer_spacing
    else:
        layer_required = (
            2 * config.margin + 2 * max(map(abs, ranks.values())) * config.layer_spacing
        )
    required_width, required_height = (
        (cross_required, layer_required)
        if config.type == "top"
        else (layer_required, cross_required)
    )
    width = max(config.width, required_width) if config.auto_grow else config.width
    height = max(config.height, required_height) if config.auto_grow else config.height

    layer_extent = height if config.type == "top" else width
    cross_extent = width if config.type == "top" else height
    if config.center_layer is None:
        layer_spacing = _spacing(
            config.layer_spacing,
            layer_extent - 2 * config.margin,
            len(ordered_layers),
            config.auto_grow,
        )
        layer_coordinates = {
            layer: round(config.margin + index * layer_spacing)
            for index, layer in enumerate(ordered_layers)
        }
    else:
        center = layer_extent // 2
        max_rank = max(map(abs, ranks.values()))
        layer_spacing = _spacing(
            config.layer_spacing,
            2 * max(0, min(center, layer_extent - center) - config.margin),
            2 * max_rank + 1,
            config.auto_grow,
        )
        layer_coordinates = {
            layer: round(center + ranks[layer] * layer_spacing) for layer in ordered_layers
        }

    positions: dict[str, Position] = {}
    for layer in ordered_layers:
        devices = ordered_devices[layer]
        if layer in config.layer_margins:
            start_margin, end_margin = config.layer_margins[layer]
            available = max(0, cross_extent - start_margin - end_margin)
            cross_slots = (
                [(start_margin + cross_extent - end_margin) / 2]
                if len(devices) == 1
                else [
                    start_margin + index * available / (len(devices) - 1)
                    for index in range(len(devices))
                ]
            )
        else:
            node_spacing = _spacing(
                config.node_spacing,
                cross_extent - 2 * config.margin,
                len(devices),
                config.auto_grow,
            )
            total = (len(devices) - 1) * node_spacing
            cross_start = cross_extent / 2 - total / 2
            cross_slots = [cross_start + index * node_spacing for index in range(len(devices))]
        for device_id, slot in zip(devices, cross_slots, strict=True):
            cross = round(slot)
            positions[device_id] = (
                Position(x=cross, y=layer_coordinates[layer])
                if config.type == "top"
                else Position(x=layer_coordinates[layer], y=cross)
            )

    if config.separation == "constrained" and config.center_layer is None:
        spring = _spring_layout(topology, config, positions)
        for layer in ordered_layers:
            devices = ordered_devices[layer]
            ordered = sorted(
                devices,
                key=lambda device_id: (
                    spring[device_id][0 if config.type == "top" else 1],
                    device_id,
                ),
            )
            ordered_slots = sorted(
                positions[device_id].x if config.type == "top" else positions[device_id].y
                for device_id in devices
            )
            for device_id, slot in zip(ordered, ordered_slots, strict=True):
                positions[device_id] = (
                    Position(x=slot, y=layer_coordinates[layer])
                    if config.type == "top"
                    else Position(x=layer_coordinates[layer], y=slot)
                )
    return LayoutResult(positions, width, height)


def _radial_layout(
    topology: FederatedTopology,
    config: LayoutConfig,
    ordered_devices: dict[int, list[str]],
    ordered_layers: list[int],
    ranks: dict[int, int],
    anchors: dict[str, tuple[int, ...]] | None,
    depths: dict[str, int] | None,
) -> LayoutResult:
    center_device: str | None = None
    rings: dict[int, list[str]] = defaultdict(list)
    if config.center_layer is None:
        first_is_center = len(ordered_devices[ordered_layers[0]]) == 1
        for index, layer in enumerate(ordered_layers):
            if first_is_center and index == 0:
                center_device = ordered_devices[layer][0]
            else:
                rings[index + int(not first_is_center)].extend(ordered_devices[layer])
    else:
        core = ordered_devices.get(config.center_layer, [])
        core_ring = int(len(core) > 1)
        if len(core) == 1:
            center_device = core[0]
        elif core:
            rings[core_ring].extend(core)
        for layer in ordered_layers:
            if layer == config.center_layer:
                continue
            rings[abs(ranks[layer]) + core_ring].extend(ordered_devices[layer])

    ring_count = max(rings, default=0)
    if config.auto_grow:
        radius_step = float(config.radius)
        if config.separation == "constrained":
            minimum_steps = [
                config.node_spacing / (2 * ring * math.sin(math.pi / len(devices)))
                for ring, devices in rings.items()
                if len(devices) > 1
            ]
            radius_step = max([radius_step, *minimum_steps])
        size = max(
            config.width,
            config.height,
            math.ceil(2 * config.margin + 2 * ring_count * radius_step),
        )
        width = height = size
    else:
        width, height = config.width, config.height
        radius_step = min(
            config.radius,
            max(0, min(width, height) // 2 - config.margin) / max(1, ring_count),
        )
    center_x, center_y = width // 2, height // 2
    positions: dict[str, Position] = {}
    if center_device:
        positions[center_device] = Position(x=center_x, y=center_y)
    for ring, devices in sorted(rings.items()):
        if anchors is not None and depths is not None:
            devices = _project_ring_order(devices, ordered_devices, anchors, depths)
            devices = _least_crossing_rotation(
                topology,
                devices,
                positions,
                center_x,
                center_y,
                radius_step * ring,
            )
            rings[ring] = devices
        for index, device_id in enumerate(devices):
            angle = (2 * math.pi * index) / len(devices)
            positions[device_id] = Position(
                x=round(center_x + radius_step * ring * math.cos(angle)),
                y=round(center_y + radius_step * ring * math.sin(angle)),
            )

    if config.separation == "constrained" and config.center_layer is None:
        spring = _spring_layout(topology, config, positions)
        for ring, devices in sorted(rings.items()):
            ordered = sorted(
                devices,
                key=lambda device_id: (
                    math.atan2(spring[device_id][1], spring[device_id][0]),
                    device_id,
                ),
            )
            for index, device_id in enumerate(ordered):
                angle = (2 * math.pi * index) / len(devices)
                positions[device_id] = Position(
                    x=round(center_x + radius_step * ring * math.cos(angle)),
                    y=round(center_y + radius_step * ring * math.sin(angle)),
                )
    return LayoutResult(positions, width, height)


def _center_out_order(
    topology: FederatedTopology,
    layers: dict[int, list[str]],
    ordered_layers: list[int],
    ranks: dict[int, int],
    center_layer: int,
) -> tuple[dict[int, list[str]], dict[str, tuple[int, ...]], dict[str, int]] | None:
    roots = sorted(layers.get(center_layer, []))
    if not roots:
        return None

    graph: nx.Graph[str] = nx.Graph()
    graph.add_nodes_from(sorted(topology.devices))
    graph.add_edges_from(
        sorted(
            (min(link.a_device, link.b_device), max(link.a_device, link.b_device))
            for link in topology.links.values()
        )
    )
    distances = [nx.single_source_shortest_path_length(graph, root) for root in roots]
    anchors: dict[str, tuple[int, ...]] = {}
    branches: dict[str, tuple[tuple[int, str], ...]] = {}
    depths: dict[str, int] = {}
    for device_id in sorted(topology.devices):
        reachable = [
            (distance[device_id], index)
            for index, distance in enumerate(distances)
            if device_id in distance
        ]
        if reachable:
            depth = min(item[0] for item in reachable)
            anchors[device_id] = tuple(index for distance, index in reachable if distance == depth)
            branches[device_id] = tuple(
                (
                    index,
                    (
                        nx.shortest_path(graph, roots[index], device_id)[1]
                        if depth
                        else roots[index]
                    ),
                )
                for index in anchors[device_id]
            )
            depths[device_id] = depth
        else:
            anchors[device_id] = (len(roots),)
            branches[device_id] = ((len(roots), device_id),)
            depths[device_id] = len(topology.devices)

    midpoint = (len(roots) - 1) / 2

    def anchor_position(device_id: str) -> float:
        return sum(anchors[device_id]) / len(anchors[device_id])

    def depth_key(device_id: str) -> int:
        position = anchor_position(device_id)
        return depths[device_id] if position <= midpoint else -depths[device_id]

    result: dict[int, list[str]] = {}
    for layer in ordered_layers:
        if layer == center_layer:
            result[layer] = roots
            continue
        groups: dict[tuple[tuple[int, str], ...], list[str]] = defaultdict(list)
        for device_id in layers[layer]:
            groups[branches[device_id]].append(device_id)
        result[layer] = [
            device_id
            for group in sorted(
                groups,
                key=lambda item: (
                    sum(index for index, _ in item) / len(item),
                    item,
                ),
            )
            for device_id in sorted(groups[group], key=lambda item: (depth_key(item), item))
        ]

    neighbors = {device_id: sorted(graph[device_id]) for device_id in graph}

    def state() -> tuple[tuple[int, tuple[str, ...]], ...]:
        return tuple((layer, tuple(result[layer])) for layer in ordered_layers)

    def sweep(layer: int) -> None:
        positions = {
            device_id: index / max(1, len(devices) - 1)
            for devices in result.values()
            for index, device_id in enumerate(devices)
        }

        def barycenter(device_id: str) -> float:
            related = [
                positions[neighbor]
                for neighbor in neighbors[device_id]
                if topology.devices[neighbor].layer != layer
            ]
            return (
                sum(related) / len(related)
                if related
                else anchor_position(device_id) / max(1, len(roots) - 1)
            )

        groups: dict[tuple[tuple[int, str], ...], list[str]] = defaultdict(list)
        for device_id in result[layer]:
            groups[branches[device_id]].append(device_id)
        ordered_groups = sorted(
            groups,
            key=lambda group: (
                sum(barycenter(device_id) for device_id in groups[group]) / len(groups[group]),
                sum(index for index, _ in group) / len(group),
                group,
            ),
        )
        result[layer] = [
            device_id
            for group in ordered_groups
            for device_id in sorted(
                groups[group],
                key=lambda item: (barycenter(item), depth_key(item), item),
            )
        ]

    outward = sorted(
        (layer for layer in ordered_layers if layer != center_layer),
        key=lambda layer: (abs(ranks[layer]), ranks[layer]),
    )
    seen = {state()}
    for _ in range(max(1, 2 * len(topology.devices))):
        for layer in outward:
            sweep(layer)
        for layer in reversed(outward):
            sweep(layer)
        current = state()
        if current in seen:
            break
        seen.add(current)
    for layer in ordered_layers:
        if layer != center_layer:
            result[layer] = _center_deep_branches(result[layer], branches, depths)
    return result, anchors, depths


def _center_deep_branches(
    devices: list[str],
    branches: dict[str, tuple[tuple[int, str], ...]],
    depths: dict[str, int],
) -> list[str]:
    grouped: dict[tuple[tuple[int, str], ...], list[str]] = defaultdict(list)
    order: list[tuple[tuple[int, str], ...]] = []
    for device_id in devices:
        branch = branches[device_id]
        if branch not in grouped:
            order.append(branch)
        grouped[branch].append(device_id)
    deepest = max(depths[device_id] for device_id in devices)
    center = [
        branch
        for branch in order
        if max(depths[device_id] for device_id in grouped[branch]) == deepest
    ]
    outer = [branch for branch in order if branch not in center]
    if not outer or len(center) == len(order):
        return devices
    center_size = sum(len(grouped[branch]) for branch in center)
    target = (len(devices) - center_size) / 2
    prefix_sizes = [
        sum(len(grouped[branch]) for branch in outer[:index]) for index in range(len(outer) + 1)
    ]
    split = min(
        range(len(prefix_sizes)),
        key=lambda index: (abs(prefix_sizes[index] - target), index),
    )
    return [
        device_id
        for branch in [*outer[:split], *center, *outer[split:]]
        for device_id in grouped[branch]
    ]


def _project_ring_order(
    devices: list[str],
    ordered_devices: dict[int, list[str]],
    anchors: dict[str, tuple[int, ...]],
    depths: dict[str, int],
) -> list[str]:
    indexes = {
        device_id: index / max(1, len(layer_devices) - 1)
        for layer_devices in ordered_devices.values()
        for index, device_id in enumerate(layer_devices)
    }
    midpoint = max((max(value) for value in anchors.values()), default=0) / 2

    def key(device_id: str) -> tuple[float, tuple[int, ...], float, int, str]:
        anchor = anchors[device_id]
        anchor_position = sum(anchor) / len(anchor)
        depth = depths[device_id] if anchor_position <= midpoint else -depths[device_id]
        return (
            anchor_position,
            anchor,
            indexes[device_id],
            depth,
            device_id,
        )

    return sorted(devices, key=key)


def _least_crossing_rotation(
    topology: FederatedTopology,
    devices: list[str],
    inner_positions: dict[str, Position],
    center_x: int,
    center_y: int,
    radius: float,
) -> list[str]:
    if len(devices) < 2:
        return devices
    candidates: list[list[str]] = []
    for ordered in (devices, list(reversed(devices))):
        candidates.extend(ordered[index:] + ordered[:index] for index in range(len(ordered)))

    def score(ordered: list[str]) -> tuple[int, tuple[str, ...]]:
        positions = {
            device_id: Position(
                x=round(center_x + radius * math.cos(2 * math.pi * index / len(ordered))),
                y=round(center_y + radius * math.sin(2 * math.pi * index / len(ordered))),
            )
            for index, device_id in enumerate(ordered)
        }
        ring = set(ordered)
        segments = [
            (
                link.a_device,
                link.b_device,
                positions[link.a_device],
                inner_positions[link.b_device],
            )
            if link.a_device in ring and link.b_device in inner_positions
            else (
                link.b_device,
                link.a_device,
                positions[link.b_device],
                inner_positions[link.a_device],
            )
            for link in topology.links.values()
            if (link.a_device in ring and link.b_device in inner_positions)
            or (link.b_device in ring and link.a_device in inner_positions)
        ]
        crossings = sum(
            left[0] not in right[:2]
            and left[1] not in right[:2]
            and _segments_cross(left[2], left[3], right[2], right[3])
            for index, left in enumerate(segments)
            for right in segments[index + 1 :]
        )
        return crossings, tuple(ordered)

    return min(candidates, key=score)


def _segments_cross(a: Position, b: Position, c: Position, d: Position) -> bool:
    def side(first: Position, second: Position, point: Position) -> int:
        return (second.x - first.x) * (point.y - first.y) - (second.y - first.y) * (
            point.x - first.x
        )

    return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def _spacing(configured: int, extent: int, count: int, preserve: bool) -> float:
    if preserve or count <= 1:
        return float(configured)
    return min(configured, max(0, extent) / (count - 1))


def _ordered_devices(
    topology: FederatedTopology,
    layers: dict[int, list[str]],
    ordered_layers: list[int],
) -> dict[int, list[str]]:
    neighbors: dict[str, set[str]] = defaultdict(set)
    for link in topology.links.values():
        neighbors[link.a_device].add(link.b_device)
        neighbors[link.b_device].add(link.a_device)

    ranks: dict[str, float] = {}
    result: dict[int, list[str]] = {}

    def rank(device_id: str) -> tuple[float, str]:
        related = [ranks[item] for item in neighbors[device_id] if item in ranks]
        return (sum(related) / len(related) if related else 2.0, device_id)

    for layer in ordered_layers:
        devices = sorted(layers[layer], key=rank)
        result[layer] = devices
        for index, device_id in enumerate(devices):
            ranks[device_id] = index / max(1, len(devices) - 1)
    return result
